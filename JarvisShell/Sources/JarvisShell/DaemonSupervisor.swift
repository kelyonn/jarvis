import Darwin
import Foundation
import JarvisShellCore

/// Launches and babysits the Python daemon: restart with backoff, give up after repeated crashes.
final class DaemonSupervisor {
    enum Status: Equatable { case stopped, starting, running, backoff(TimeInterval), failed }

    var onStatus: ((Status) -> Void)?
    private(set) var status: Status = .stopped { didSet { if status != oldValue { onStatus?(status) } } }

    private let queue = DispatchQueue(label: "jarvis.supervisor")
    private var process: Process?
    private var backoff = Backoff()
    private var crashes = CrashTracker()
    private var wanted = false
    private var startedAt = Date()
    private let ipcFile: URL

    init(ipcFile: URL) { self.ipcFile = ipcFile }

    // MARK: paths

    static var home: URL {
        if let h = ProcessInfo.processInfo.environment["JARVIS_HOME"] { return URL(fileURLWithPath: h) }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".jarvis")
    }

    static func pythonPath() -> String? {
        let env = ProcessInfo.processInfo.environment
        let candidates: [String?] = [env["JARVIS_PYTHON"], home.appendingPathComponent("venv/bin/python").path]
        return candidates.compactMap { $0 }.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    // MARK: control

    func start() {
        queue.async {
            self.wanted = true
            self.crashes.reset()
            self.backoff.reset()
            self.launch()
        }
    }

    func restart() {
        queue.async {
            self.wanted = true
            self.crashes.reset()
            self.backoff.reset()
            if let p = self.process, p.isRunning { p.terminate() } else { self.launch() }
        }
    }

    func stop() {
        queue.sync {
            wanted = false
            guard let p = process, p.isRunning else { status = .stopped; return }
            p.terminate()
            let deadline = Date().addingTimeInterval(3)
            while p.isRunning && Date() < deadline { usleep(50_000) }
            if p.isRunning { kill(p.processIdentifier, SIGKILL) }
            status = .stopped
        }
    }

    // MARK: internals

    private func launch() {
        guard wanted else { return }
        guard let py = Self.pythonPath() else {
            NSLog("Jarvis: no Python venv found (run scripts/install.sh)")
            status = .failed
            return
        }
        let logs = Self.home.appendingPathComponent("logs")
        try? FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
        let out = logs.appendingPathComponent("daemon.out")
        if !FileManager.default.fileExists(atPath: out.path) { FileManager.default.createFile(atPath: out.path, contents: nil) }
        let handle = try? FileHandle(forWritingTo: out)
        handle?.seekToEndOfFile()

        let p = Process()
        p.executableURL = URL(fileURLWithPath: py)
        p.arguments = ["-m", "jarvis_core", "run", "--ipc-file", ipcFile.path]
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
        p.environment = env
        p.standardOutput = handle
        p.standardError = handle
        p.terminationHandler = { [weak self] proc in
            self?.queue.async { self?.exited(proc) }
        }
        do {
            try p.run()
            process = p
            startedAt = Date()
            status = .starting
            // Consider it healthy after 20s of uptime.
            queue.asyncAfter(deadline: .now() + 20) { [weak self] in
                guard let self, self.process === p, p.isRunning else { return }
                self.status = .running
                self.backoff.reset()
            }
        } catch {
            NSLog("Jarvis: failed to start daemon: \(error)")
            scheduleRetry()
        }
    }

    private func exited(_ p: Process) {
        guard process === p else { return }
        process = nil
        guard wanted else { status = .stopped; return }
        let ranFor = Date().timeIntervalSince(startedAt)
        NSLog("Jarvis daemon exited (status \(p.terminationStatus)) after \(Int(ranFor))s")
        if p.terminationStatus == 0 && p.terminationReason == .exit && ranFor > 1 {
            // Clean exit (e.g. restart requested by the core, or Shell was lost): relaunch quickly.
            queue.asyncAfter(deadline: .now() + 1) { [weak self] in self?.launch() }
            return
        }
        if p.terminationStatus == 3 {
            // Another daemon holds the lock: one left behind by an earlier Jarvis.app that was killed or crashed.
            // That is not a crash of ours. Stop the leftover (only if nothing else owns it) and start our own.
            if crashes.record() { status = .failed; wanted = false; return }
            Self.stopOrphanDaemon()
            queue.asyncAfter(deadline: .now() + 2) { [weak self] in self?.launch() }
            return
        }
        if crashes.record() {
            status = .failed
            wanted = false
            return
        }
        scheduleRetry()
    }

    /// The pid in the daemon lock file, if that process is a Jarvis daemon that has lost its app (parent is launchd).
    static func stopOrphanDaemon() {
        let lock = home.appendingPathComponent("run/daemon.lock")
        guard let text = try? String(contentsOf: lock, encoding: .utf8),
              let pid = Int32(text.trimmingCharacters(in: .whitespacesAndNewlines)), pid > 1 else { return }
        var info = kinfo_proc()
        var size = MemoryLayout<kinfo_proc>.stride
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_PID, pid]
        guard sysctl(&mib, 4, &info, &size, nil, 0) == 0, size > 0 else { return }
        let ppid = info.kp_eproc.e_ppid
        var argsMib: [Int32] = [CTL_KERN, KERN_PROCARGS2, pid]
        var argSize = 0
        guard sysctl(&argsMib, 3, nil, &argSize, nil, 0) == 0, argSize > 0 else { return }
        var buf = [CChar](repeating: 0, count: argSize)
        guard sysctl(&argsMib, 3, &buf, &argSize, nil, 0) == 0 else { return }
        let args = String(decoding: buf.prefix(argSize).map { UInt8(bitPattern: $0 == 0 ? 32 : $0) }, as: UTF8.self)
        guard ppid == 1, args.contains("jarvis_core"), args.contains(" run") else { return }
        NSLog("Jarvis: stopping a leftover daemon (pid \(pid)) from an earlier run")
        kill(pid, SIGTERM)
    }

    private func scheduleRetry() {
        let d = backoff.next()
        status = .backoff(d)
        queue.asyncAfter(deadline: .now() + d) { [weak self] in self?.launch() }
    }
}
