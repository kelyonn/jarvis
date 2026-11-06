import AVFoundation
import CoreAudio
import Darwin
import Foundation
import JarvisShellCore

/// Detects "the user is on a call": another process is capturing audio input, or a camera is in use.
/// Polls the CoreAudio process-object list (macOS 14.4+) and reports only changes.
final class DeviceUsage {
    var onChange: ((_ micInUse: Bool, _ cameraInUse: Bool, _ appHint: String) -> Void)?

    private var timer: DispatchSourceTimer?
    private var last: (Bool, Bool, String)?
    private let ownPid = ProcessInfo.processInfo.processIdentifier
    private let queue = DispatchQueue(label: "jarvis.deviceusage")

    func start(interval: TimeInterval = 2.0) {
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now() + 1, repeating: interval)
        t.setEventHandler { [weak self] in self?.poll() }
        t.resume()
        timer = t
    }

    func stop() { timer?.cancel() }

    private func poll() {
        let (mic, hint) = otherProcessCapturingAudio()
        let cam = cameraInUse()
        let now = (mic, cam, hint)
        if let l = last, l == now { return }
        last = now
        DispatchQueue.main.async { self.onChange?(mic, cam, hint) }
    }

    private func cameraInUse() -> Bool {
        let types: [AVCaptureDevice.DeviceType] = [.builtInWideAngleCamera, .external]
        let session = AVCaptureDevice.DiscoverySession(deviceTypes: types, mediaType: .video, position: .unspecified)
        return session.devices.contains { $0.isInUseByAnotherApplication }
    }

    private func otherProcessCapturingAudio() -> (Bool, String) {
        var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyProcessObjectList,
                                              mScope: kAudioObjectPropertyScopeGlobal,
                                              mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size) == noErr,
              size > 0 else { return (false, "") }
        var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids) == noErr
        else { return (false, "") }

        for obj in ids {
            guard let running: UInt32 = prop(obj, kAudioProcessPropertyIsRunningInput), running != 0 else { continue }
            let pid: Int32 = prop(obj, kAudioProcessPropertyPID) ?? 0
            if pid == ownPid || pid == 0 { continue }
            let bundle = bundleId(obj) ?? ""
            if isIgnorableAudioClient(bundleId: bundle) { continue }
            if bundle.isEmpty && isSystemProcess(pid) { continue }  // e.g. /usr/libexec/historicalaudiod is not a call
            return (true, bundle)
        }
        return (false, "")
    }

    private func isSystemProcess(_ pid: Int32) -> Bool {
        var buf = [CChar](repeating: 0, count: 4096)
        guard proc_pidpath(pid, &buf, UInt32(buf.count)) > 0 else { return false }
        let path = String(cString: buf)
        return isSystemExecutablePath(path)
    }
    private func prop<T>(_ obj: AudioObjectID, _ selector: AudioObjectPropertySelector) -> T? where T: FixedWidthInteger {
        var addr = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                              mElement: kAudioObjectPropertyElementMain)
        var value: T = 0
        var size = UInt32(MemoryLayout<T>.size)
        return AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &value) == noErr ? value : nil
    }

    private func bundleId(_ obj: AudioObjectID) -> String? {
        var addr = AudioObjectPropertyAddress(mSelector: kAudioProcessPropertyBundleID,
                                              mScope: kAudioObjectPropertyScopeGlobal,
                                              mElement: kAudioObjectPropertyElementMain)
        var size = UInt32(MemoryLayout<CFString?>.size)
        var cf: Unmanaged<CFString>?
        let status = withUnsafeMutablePointer(to: &cf) {
            AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, $0)
        }
        guard status == noErr, let cf else { return nil }
        return cf.takeRetainedValue() as String
    }
}
