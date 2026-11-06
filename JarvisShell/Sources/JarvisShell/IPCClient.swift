import Foundation
import JarvisShellCore

/// WebSocket client to jarvis_core. Re-reads ipc.json on every attempt (the daemon picks a new port
/// each launch) and reconnects with backoff.
final class IPCClient: NSObject {
    var onControl: ((Control) -> Void)?
    var onTTS: ((TTSFrame) -> Void)?
    var onConnectionChange: ((Bool) -> Void)?
    var helloData: () -> [String: Any] = { [:] }

    private let ipcFile: URL
    private let queue = DispatchQueue(label: "jarvis.ipc")
    private var session: URLSession!
    private var task: URLSessionWebSocketTask?
    private var backoff = Backoff()
    private var running = false
    private var pingTimer: DispatchSourceTimer?
    private var connected = false
    private var generation = 0

    init(ipcFile: URL) {
        self.ipcFile = ipcFile
        super.init()
        session = URLSession(configuration: .ephemeral, delegate: nil, delegateQueue: nil)
    }

    var isConnected: Bool { queue.sync { connected } }

    func start() {
        queue.async {
            guard !self.running else { return }
            self.running = true
            self.connect()
        }
    }

    func stop() {
        queue.async {
            self.running = false
            self.generation += 1
            self.pingTimer?.cancel()
            self.task?.cancel(with: .goingAway, reason: nil)
            self.task = nil
        }
    }

    func send(_ c: Control) {
        queue.async {
            guard let t = self.task, self.connected else { return }
            t.send(.string(c.encode())) { _ in }
        }
    }

    func sendMic(_ pcm16: Data) {
        queue.async {
            guard let t = self.task, self.connected else { return }
            t.send(.data(encodeMicFrame(pcm16))) { _ in }
        }
    }

    // MARK: connection

    private func connect() {
        guard running else { return }
        guard let data = try? Data(contentsOf: ipcFile), let info = IPCInfo.parse(data) else {
            return scheduleReconnect()
        }
        generation += 1
        let gen = generation
        let t = session.webSocketTask(with: info.url)
        t.maximumMessageSize = 16 * 1024 * 1024
        task = t
        t.resume()
        var hello = helloData()
        hello["token"] = info.token
        t.send(.string(Control(MessageType.hello, hello).encode())) { [weak self] err in
            guard let self else { return }
            if err != nil { self.queue.async { if gen == self.generation { self.dropped(gen) } } }
        }
        receive(t, gen)
    }

    private func receive(_ t: URLSessionWebSocketTask, _ gen: Int) {
        t.receive { [weak self] result in
            guard let self else { return }
            self.queue.async {
                guard gen == self.generation else { return }
                switch result {
                case .failure:
                    self.dropped(gen)
                case .success(let msg):
                    switch msg {
                    case .string(let s):
                        if let c = try? Control.decode(s) { self.handle(c, gen) }
                    case .data(let d):
                        if let f = try? decodeTTSFrame(d) { self.onTTS?(f) }
                    @unknown default: break
                    }
                    self.receive(t, gen)
                }
            }
        }
    }

    private func handle(_ c: Control, _ gen: Int) {
        if c.type == MessageType.welcome {
            backoff.reset()
            setConnected(true)
            startPinging(gen)
        } else if c.type == MessageType.ping {
            task?.send(.string(Control(MessageType.pong, [:], id: c.id).encode())) { _ in }
            return
        } else if c.type == MessageType.pong {
            return
        }
        onControl?(c)
    }

    private func startPinging(_ gen: Int) {
        pingTimer?.cancel()
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + Wire.pingInterval, repeating: Wire.pingInterval)
        timer.setEventHandler { [weak self] in
            guard let self, gen == self.generation else { return }
            self.task?.send(.string(Control(MessageType.ping).encode())) { _ in }
        }
        timer.resume()
        pingTimer = timer
    }

    private func dropped(_ gen: Int) {
        guard gen == generation else { return }
        generation += 1
        pingTimer?.cancel()
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        setConnected(false)
        scheduleReconnect()
    }

    private func setConnected(_ v: Bool) {
        guard connected != v else { return }
        connected = v
        onConnectionChange?(v)
    }

    private func scheduleReconnect() {
        guard running else { return }
        let delay = min(backoff.next(), 5)
        queue.asyncAfter(deadline: .now() + delay) { [weak self] in self?.connect() }
    }
}
