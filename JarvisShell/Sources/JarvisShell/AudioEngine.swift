import AVFoundation
import CoreAudio
import CoreMedia
import Foundation
import JarvisShellCore

/// Audio I/O with two deliberately separate engines, so Jarvis never holds a device it isn't using:
///  * playback: output only, started when something plays and shut down a few seconds after it ends;
///  * capture: input only, opened only while the core asks for it (`startCapture` / `stopCapture`).
/// Neither uses voice-processing I/O. That unit ducks and degrades all other audio system-wide (and flips
/// Bluetooth headsets into call mode), which is what silenced the speakers when Jarvis ran all day.
final class AudioEngine {
    var onMicFrame: ((Data) -> Void)?
    /// (utteranceId, "started" | "finished" | "interrupted")
    var onPlayback: ((String, String) -> Void)?
    var onError: ((String) -> Void)?

    private let engine = AVAudioEngine()  // playback
    private let player = AVAudioPlayerNode()
    private let earconPlayer = AVAudioPlayerNode()
    private var attached = false
    private var idleStop: DispatchWorkItem?
    private static let idleSeconds = 4.0

    private var capture: AVAudioEngine?
    private var session: AVCaptureSession?            // the built-in microphone, used instead of a Bluetooth headset's
    private var sessionDelegate: MicSampleDelegate?
    private var tappedRate: Double = 0
    private var tappedChannels: AVAudioChannelCount = 0
    private var restartTimes: [Date] = []
    private var silentBuffers = 0  // consecutive buffers that were all exactly zero (a dead input stream)
    private var deviceListenerInstalled = false
    private let playFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: Wire.ttsSampleRate,
                                           channels: 1, interleaved: false)!
    private let targetFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: Wire.micSampleRate,
                                             channels: 1, interleaved: true)!
    private var converter: AVAudioConverter?
    private var converterInputRate: Double = 0
    private var chunker = FrameChunker()

    private let lock = NSLock()
    private var current: String?
    private var generation = 0
    private var pending = 0
    private var ended = false
    private var micAllowed = true
    private var epoch = 0                           // bumped when the output is rebuilt; old completion callbacks are ignored
    private var unplayed: [AVAudioPCMBuffer] = []   // scheduled but not yet heard, so a rebuild can replay them

    /// Frames are dropped when false (muted / laptop closed). The device itself is closed by the caller.
    var micEnabled: Bool {
        get { lock.lock(); defer { lock.unlock() }; return micAllowed }
        set { lock.lock(); micAllowed = newValue; lock.unlock() }
    }

    var capturing: Bool { capture != nil || session != nil }
    var playbackRunning: Bool { engine.isRunning }

    // MARK: playback lifecycle

    /// Starts the output engine if it isn't running, and keeps it alive for a while after the last sound.
    private func ensurePlayback() -> Bool {
        idleStop?.cancel()
        if engine.isRunning { scheduleIdleStop(); return true }
        if !attached {
            engine.attach(player)
            engine.attach(earconPlayer)
            attached = true
        }
        let outFormat = engine.outputNode.inputFormat(forBus: 0)
        engine.connect(player, to: engine.mainMixerNode, format: playFormat)
        engine.connect(earconPlayer, to: engine.mainMixerNode, format: playFormat)
        engine.connect(engine.mainMixerNode, to: engine.outputNode, format: outFormat)
        engine.prepare()
        do { try engine.start() } catch {
            onError?("Playback failed to start: \(error.localizedDescription)")
            return false
        }
        NotificationCenter.default.removeObserver(self, name: .AVAudioEngineConfigurationChange, object: engine)
        NotificationCenter.default.addObserver(self, selector: #selector(playbackConfigChanged),
                                               name: .AVAudioEngineConfigurationChange, object: engine)
        scheduleIdleStop()
        return true
    }

    private func scheduleIdleStop() {
        idleStop?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.lock.lock(); let busy = self.current != nil; self.lock.unlock()
            if busy { self.scheduleIdleStop(); return }
            self.player.stop()
            self.earconPlayer.stop()
            self.engine.stop()  // releases the output device so other apps get it back untouched
        }
        idleStop = work
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.idleSeconds, execute: work)
    }

    @objc private func playbackConfigChanged() {
        // The output changed (headphones, AirPods switching to call mode when the mic opens, another app grabbing the
        // device). This used to cut Jarvis off mid-sentence. Now the output is rebuilt on the new device and whatever
        // had not been heard yet is played again, so the reply carries on.
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.lock.lock()
            let busy = self.current != nil
            self.epoch += 1
            let gen = self.generation, ep = self.epoch
            let replay = self.unplayed
            self.lock.unlock()
            self.player.stop()
            self.earconPlayer.stop()
            self.engine.stop()
            guard busy else { return }  // nothing playing: the next sound restarts the engine
            guard self.ensurePlayback() else {
                NSLog("Jarvis audio: output could not be restarted after a device change")
                self.stopAll()
                return
            }
            NSLog("Jarvis audio: output changed mid-reply; resuming \(replay.count) chunks")
            for buf in replay { self.schedule(buf, gen: gen, epoch: ep) }
            self.player.play()
        }
    }

    private func schedule(_ buf: AVAudioPCMBuffer, gen: Int, epoch ep: Int) {
        player.scheduleBuffer(buf, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            self?.chunkDone(gen, epoch: ep, buf)
        }
    }

    // MARK: capture lifecycle

    /// Bluetooth earbuds that reconnect or switch profile leave the open input stream delivering silence (all zeros)
    /// without any error. Reopen the microphone when that happens, and whenever the default input device changes.
    private func installInputDeviceListener() {
        guard !deviceListenerInstalled else { return }
        deviceListenerInstalled = true
        var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultInputDevice,
                                              mScope: kAudioObjectPropertyScopeGlobal,
                                              mElement: kAudioObjectPropertyElementMain)
        AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &addr, DispatchQueue.main) { [weak self] _, _ in
            self?.reopenCapture(reason: "default input device changed")
        }
    }

    private func reopenCapture(reason: String) {
        guard capturing else { return }
        let now = Date()
        restartTimes = restartTimes.filter { now.timeIntervalSince($0) < 20 }
        guard restartTimes.count < 5 else { return }
        restartTimes.append(now)
        NSLog("Jarvis audio: reopening the microphone (\(reason))")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in
            guard let self, self.capturing else { return }
            self.stopCapture()
            self.startCapture()
        }
    }

    /// Listens on the Mac's own microphone while the default input is a Bluetooth headset (see InputRouting): opening the
    /// headset's mic would switch it to low-quality call mode for as long as Jarvis listens.
    private func startBuiltInSession() -> Bool {
        guard let dev = InputRouting.replacementForBluetoothInput(), let uid = InputRouting.uid(dev),
              let mic = AVCaptureDevice(uniqueID: uid), let input = try? AVCaptureDeviceInput(device: mic) else { return false }
        let s = AVCaptureSession()
        guard s.canAddInput(input) else { return false }
        s.addInput(input)
        let out = AVCaptureAudioDataOutput()
        out.audioSettings = [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: Wire.micSampleRate,
                             AVNumberOfChannelsKey: 1, AVLinearPCMBitDepthKey: 16, AVLinearPCMIsFloatKey: false,
                             AVLinearPCMIsBigEndianKey: false, AVLinearPCMIsNonInterleaved: false]
        let delegate = MicSampleDelegate { [weak self] samples in self?.handleBuiltIn(samples) }
        out.setSampleBufferDelegate(delegate, queue: DispatchQueue(label: "jarvis.mic.builtin"))
        guard s.canAddOutput(out) else { return false }
        s.addOutput(out)
        chunker.reset()
        s.startRunning()
        guard s.isRunning else { return false }
        session = s
        sessionDelegate = delegate
        NSLog("Jarvis audio: listening on the built-in microphone, so the Bluetooth headset stays in music mode")
        return true
    }

    private func handleBuiltIn(_ samples: [Int16]) {
        guard micEnabled, !samples.isEmpty else { return }
        for frame in chunker.push(samples) { onMicFrame?(frame) }
    }

    func startCapture() {
        installInputDeviceListener()
        guard capture == nil, session == nil else { return }
        if startBuiltInSession() { return }
        let e = AVAudioEngine()
        let input = e.inputNode
        // The hardware side: after choosing a different device the node's cached output format can still describe the old
        // one (a headset's 16 kHz), and a tap with the wrong format throws, so ask the device and let the tap follow the node.
        let inFormat = input.inputFormat(forBus: 0)
        guard inFormat.sampleRate > 0, inFormat.channelCount > 0 else {
            onError?("No microphone available")
            return
        }
        converter = nil
        chunker.reset()
        tappedRate = inFormat.sampleRate
        tappedChannels = inFormat.channelCount
        input.installTap(onBus: 0, bufferSize: 1024, format: nil) { [weak self] buf, _ in
            self?.handleMic(buf)
        }
        e.prepare()
        do { try e.start() } catch {
            input.removeTap(onBus: 0)
            onError?("Microphone failed to start: \(error.localizedDescription)")
            return
        }
        capture = e
        NotificationCenter.default.addObserver(self, selector: #selector(captureConfigChanged),
                                               name: .AVAudioEngineConfigurationChange, object: e)
    }

    func stopCapture() {
        if let s = session {
            session = nil
            sessionDelegate = nil
            s.stopRunning()  // the OS microphone indicator goes off
        }
        guard let e = capture else { return }
        capture = nil
        NotificationCenter.default.removeObserver(self, name: .AVAudioEngineConfigurationChange, object: e)
        e.inputNode.removeTap(onBus: 0)
        e.stop()  // the engine is released with the reference: the OS microphone indicator goes off
    }

    @objc private func captureConfigChanged() {
        // Input device changed mid-capture. Bluetooth earbuds fire this themselves when the mic opens (music mode ->
        // call mode), so only reopen if the engine really stopped or the input format changed, and never in a tight loop.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
            guard let self, let e = self.capture else { return }
            let fmt = e.inputNode.inputFormat(forBus: 0)
            let formatChanged = fmt.sampleRate != self.tappedRate || fmt.channelCount != self.tappedChannels
            guard !e.isRunning || formatChanged else { return }
            let now = Date()
            self.restartTimes = self.restartTimes.filter { now.timeIntervalSince($0) < 10 }
            guard self.restartTimes.count < 4 else {
                self.onError?("Microphone keeps changing format; giving up for now")
                return
            }
            self.restartTimes.append(now)
            self.stopCapture()
            self.startCapture()
        }
    }

    func stop() {
        stopAll()
        stopCapture()
        idleStop?.cancel()
        engine.stop()
        NotificationCenter.default.removeObserver(self)
    }

    // MARK: capture

    private func handleMic(_ buf: AVAudioPCMBuffer) {
        guard micEnabled, buf.frameLength > 0, let ch = buf.floatChannelData else { return }
        var peak: Float = 0
        for i in 0..<Int(buf.frameLength) { peak = max(peak, abs(ch[0][i])) }
        if peak == 0 {
            silentBuffers += 1
            if silentBuffers == 80 { silentBuffers = 0; reopenCapture(reason: "no signal from the microphone") }  // about 2 s
        } else {
            silentBuffers = 0
        }
        let rate = buf.format.sampleRate
        // Take channel 0 only: with voice processing the input may expose several channels.
        guard let monoFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate, channels: 1,
                                             interleaved: false),
              let mono = AVAudioPCMBuffer(pcmFormat: monoFormat, frameCapacity: buf.frameLength) else { return }
        mono.frameLength = buf.frameLength
        memcpy(mono.floatChannelData![0], ch[0], Int(buf.frameLength) * MemoryLayout<Float>.size)

        if converter == nil || converterInputRate != rate {
            converter = AVAudioConverter(from: monoFormat, to: targetFormat)
            converterInputRate = rate
        }
        guard let conv = converter else { return }
        let cap = AVAudioFrameCount(Double(mono.frameLength) * Wire.micSampleRate / rate) + 32
        guard let out = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: cap) else { return }
        var supplied = false
        var err: NSError?
        conv.convert(to: out, error: &err) { _, status in
            if supplied { status.pointee = .noDataNow; return nil }
            supplied = true
            status.pointee = .haveData
            return mono
        }
        guard err == nil, out.frameLength > 0, let p = out.int16ChannelData?[0] else { return }
        let samples = Array(UnsafeBufferPointer(start: p, count: Int(out.frameLength)))
        for frame in chunker.push(samples) { onMicFrame?(frame) }
    }

    // MARK: playback

    func beginUtterance(_ id: String) {
        lock.lock()
        let old = current
        generation += 1
        current = id
        pending = 0
        ended = false
        unplayed = []
        lock.unlock()
        if let old, old != id { onPlayback?(old, "interrupted") }
        guard ensurePlayback() else {
            lock.lock(); current = nil; lock.unlock()
            onPlayback?(id, "interrupted")
            return
        }
        player.stop()
        player.play()
        onPlayback?(id, "started")
    }

    func enqueue(_ frame: TTSFrame) {
        let samples = int16ToFloat(frame.pcm16)
        guard !samples.isEmpty,
              let buf = AVAudioPCMBuffer(pcmFormat: playFormat, frameCapacity: AVAudioFrameCount(samples.count))
        else { return }
        buf.frameLength = AVAudioFrameCount(samples.count)
        samples.withUnsafeBufferPointer { src in
            buf.floatChannelData![0].update(from: src.baseAddress!, count: samples.count)
        }

        lock.lock()
        guard current == frame.utteranceId else { lock.unlock(); return }
        let gen = generation, ep = epoch
        pending += 1
        unplayed.append(buf)
        lock.unlock()

        // The output can stop on its own (device asleep, another app took it). Restart it rather than playing into nothing.
        if !engine.isRunning, !ensurePlayback() {
            stopAll()
            return
        }
        schedule(buf, gen: gen, epoch: ep)
        if !player.isPlaying { player.play() }
    }

    func endUtterance(_ id: String) {
        lock.lock()
        guard current == id else { lock.unlock(); return }
        ended = true
        let finishedNow = pending == 0
        if finishedNow { current = nil }
        lock.unlock()
        if finishedNow { onPlayback?(id, "finished") }
    }

    private func chunkDone(_ gen: Int, epoch ep: Int, _ buf: AVAudioPCMBuffer) {
        lock.lock()
        guard gen == generation, ep == epoch, let id = current else { lock.unlock(); return }
        if let i = unplayed.firstIndex(where: { $0 === buf }) { unplayed.remove(at: i) }
        pending -= 1
        let done = pending <= 0 && ended
        if done { current = nil }
        lock.unlock()
        if done { onPlayback?(id, "finished") }
    }

    /// Stops TTS immediately (barge-in, "stop", sleep, lock).
    func stopAll() {
        lock.lock()
        let id = current
        current = nil
        generation += 1
        pending = 0
        ended = false
        unplayed = []
        lock.unlock()
        player.stop()
        earconPlayer.stop()
        if let id { onPlayback?(id, "interrupted") }
    }

    // MARK: earcons

    func playEarcon(_ name: String) {
        guard let spec = Earcons.spec(name), ensurePlayback() else { return }
        let buf = Earcons.render(spec, format: playFormat)
        earconPlayer.scheduleBuffer(buf, completionHandler: nil)
        if !earconPlayer.isPlaying { earconPlayer.play() }
    }
}

enum Earcons {
    struct Spec { let notes: [(freq: Double, ms: Double)]; let gain: Float }

    static func spec(_ name: String) -> Spec? {
        switch name {
        case "wake": return Spec(notes: [(660, 70), (880, 90)], gain: 0.18)
        case "listening", "ack": return Spec(notes: [(740, 60)], gain: 0.14)
        case "done": return Spec(notes: [(880, 70), (660, 90)], gain: 0.14)
        case "error": return Spec(notes: [(330, 120), (247, 160)], gain: 0.2)
        case "alert": return Spec(notes: [(784, 80), (988, 80), (784, 120)], gain: 0.2)
        case "pause": return Spec(notes: [(520, 80), (390, 120)], gain: 0.12)
        case "resume": return Spec(notes: [(390, 80), (520, 120)], gain: 0.12)
        default: return nil
        }
    }

    static func render(_ spec: Spec, format: AVAudioFormat) -> AVAudioPCMBuffer {
        let sr = format.sampleRate
        let total = Int(spec.notes.reduce(0) { $0 + $1.ms } / 1000 * sr)
        let buf = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(total))!
        buf.frameLength = AVAudioFrameCount(total)
        let out = buf.floatChannelData![0]
        var i = 0
        for note in spec.notes {
            let n = Int(note.ms / 1000 * sr)
            for k in 0..<n where i + k < total {
                let t = Double(k) / sr
                let env = Float(min(1, Double(k) / (0.008 * sr)) * min(1, Double(n - k) / (0.02 * sr)))
                out[i + k] = Float(sin(2 * .pi * note.freq * t)) * env * spec.gain
            }
            i += n
        }
        return buf
    }
}


/// Receives 16 kHz mono Int16 audio from the capture session and hands the samples on.
final class MicSampleDelegate: NSObject, AVCaptureAudioDataOutputSampleBufferDelegate {
    private let onSamples: ([Int16]) -> Void
    init(_ onSamples: @escaping ([Int16]) -> Void) { self.onSamples = onSamples }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard let block = CMSampleBufferGetDataBuffer(sampleBuffer) else { return }
        var length = 0
        var ptr: UnsafeMutablePointer<CChar>?
        guard CMBlockBufferGetDataPointer(block, atOffset: 0, lengthAtOffsetOut: nil, totalLengthOut: &length,
                                          dataPointerOut: &ptr) == kCMBlockBufferNoErr, let ptr, length >= 2 else { return }
        let count = length / MemoryLayout<Int16>.size
        let samples = ptr.withMemoryRebound(to: Int16.self, capacity: count) {
            Array(UnsafeBufferPointer(start: $0, count: count))
        }
        onSamples(samples)
    }
}
