import AppKit
import AVFoundation
import Foundation
import JarvisShellCore
import UserNotifications

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let ipcFile = DaemonSupervisor.home.appendingPathComponent("run/ipc.json")
    private lazy var client = IPCClient(ipcFile: ipcFile)
    private lazy var supervisor = DaemonSupervisor(ipcFile: ipcFile)
    private let audio = AudioEngine()
    private let orb = OrbPanel()
    private let hotkey = Hotkey()
    private let typeHotkey = Hotkey.typing()  // Option+Shift+Space: type instead of talk
    private let talkOverHotkey = Hotkey.talkOver()  // plain Space, but only while Jarvis is talking: stop it and speak
    private var spaceToInterrupt = false  // the core says whether this mode is on (it also stops listening while it talks)
    private let dictateHotkey = Hotkey.dictation()  // hold Control+Option+Space: speak, and the words are typed in the front app
    private let lifecycle = Lifecycle()
    private let usage = DeviceUsage()
    private let ax = AXWindows()
    private let input = InputControl()
    private let location = LocationProvider()
    private var statusItem: NSStatusItem!
    private let memoryWindow = MemoryWindowController()
    private var statusMenuItem: NSMenuItem!
    private var muteItem: NSMenuItem!
    private var loginItem: NSMenuItem!
    private var pinItem: NSMenuItem!
    private var coreMicEnabled = true
    private var coreWantsCapture = false  // the core opens the mic only while it is listening
    private var lastState: OrbStateName = .booting
    private var supervisorStatus: DaemonSupervisor.Status = .stopped
    private var coreConnected = false
    private let work = DispatchQueue(label: "jarvis.requests", qos: .userInitiated)

    func applicationDidFinishLaunching(_ n: Notification) {
        if ProcessInfo.processInfo.environment["JARVIS_LOGIN_STATUS"] != nil {
            let approval = LoginItem.ensureRegistered()
            print("login item: \(LoginItem.statusName)\(approval ? " (needs approval in System Settings > General > Login Items)" : "")")
            exit(0)
        }
        // Only one Jarvis at a time (a login item plus a manual launch would otherwise start two).
        let me = NSRunningApplication.current
        if let id = Bundle.main.bundleIdentifier,
           NSRunningApplication.runningApplications(withBundleIdentifier: id).contains(where: { $0 != me }) {
            NSLog("Jarvis is already running; exiting this copy")
            exit(0)
        }
        if ProcessInfo.processInfo.environment["JARVIS_ORB_SELFTEST"] != nil { OrbSelfTest.run(); exit(0) }
        if let dir = ProcessInfo.processInfo.environment["JARVIS_ORB_SNAPSHOT"] { OrbSnapshot.write(to: dir); exit(0) }
        buildMenu()
        if LoginItem.ensureRegistered() {
            notify(title: "Allow Jarvis to start at login",
                   body: "Open System Settings > General > Login Items and switch Jarvis on, so it is there after a restart.")
            LoginItem.openSettings()
        }
        loginItem.state = LoginItem.enabled ? .on : .off
        orb.announce("Jarvis is running. Say \"Hey Jarvis\" or press Option+Space (Option+Shift+Space to type). Its icon is in the menu bar; right-click the orb for options.")
        orb.onClick = { [weak self] a in self?.client.send(Control(MessageType.orbClick, ["action": a])) }
        orb.onText = { [weak self] t in self?.client.send(Control(MessageType.textInput, ["text": t])) }
        orb.onCardAction = { [weak self] a in self?.client.send(Control(MessageType.cardAction, ["kind": a.kind.rawValue, "value": a.value])) }
        orb.startWatchingOtherApps()

        lifecycle.start()
        wireIPC()
        wireAudio()
        wireHotkey()
        wireLifecycle()
        wireUsage()
        input.onKill = { [weak self] in self?.client.send(Control(MessageType.command, ["name": "kill"])) }

        Permissions.request("mic", location: location)
        client.start()
        supervisor.onStatus = { [weak self] s in DispatchQueue.main.async { self?.supervisorChanged(s) } }
        if ProcessInfo.processInfo.environment["JARVIS_NO_DAEMON"] == nil { supervisor.start() }
        // `kill`, logout and Activity Monitor's Quit send signals that would end the app without applicationWillTerminate,
        // leaving the daemon running on its own. Turn them into a normal quit so the daemon is always stopped with us.
        for sig in [SIGTERM, SIGINT, SIGHUP] {
            signal(sig, SIG_IGN)
            let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            src.setEventHandler { NSApp.terminate(nil) }
            src.resume()
            signalSources.append(src)
        }
        usage.start()
        refreshStatus()
    }

    private var signalSources: [DispatchSourceSignal] = []

    func applicationWillTerminate(_ n: Notification) {
        input.endSession()
        hotkey.unregister()
        typeHotkey.unregister()
        dictateHotkey.unregister()
        talkOverHotkey.unregister()
        audio.stop()
        client.stop()
        supervisor.stop()
    }

    // MARK: wiring

    private func wireIPC() {
        client.helloData = { [weak self] in
            guard let self else { return [:] }
            return ["shell_version": "0.1.0", "permissions": Permissions.status(location: self.location),
                    "asleep": self.lifecycle.suspended]
        }
        client.onConnectionChange = { [weak self] up in
            DispatchQueue.main.async {
                self?.coreConnected = up
                if !up { self?.coreWantsCapture = false; self?.applyMic() }  // never leave the mic open without the core
                self?.refreshStatus()
                if up, let self {
                    // Re-announce the current lifecycle state to a freshly started core.
                    if self.lifecycle.locked { self.client.send(Control(MessageType.lifecycle, ["event": "screen_locked"])) }
                    if self.lifecycle.asleep { self.client.send(Control(MessageType.lifecycle, ["event": "will_sleep"])) }
                }
            }
        }
        // Audio goes through the main queue like the play_begin/play_end controls, so the chunks always land after
        // their utterance has started. Played straight from the socket queue they raced ahead of play_begin whenever
        // the main thread was busy (laying out the orb card, say) and were dropped: Jarvis went silent half the time.
        client.onTTS = { [weak self] f in DispatchQueue.main.async { self?.audio.enqueue(f) } }
        client.onControl = { [weak self] c in DispatchQueue.main.async { self?.route(c) } }
        memoryWindow.bridge.send = { [weak self] c in self?.client.send(c) }
    }

    private func wireAudio() {
        audio.onMicFrame = { [weak self] d in self?.client.sendMic(d) }
        audio.onPlayback = { [weak self] id, ev in
            self?.client.send(Control(MessageType.playback, ["utterance_id": id, "event": ev]))
        }
        audio.onError = { msg in NSLog("Jarvis audio: \(msg)") }
    }

    private func wireHotkey() {
        hotkey.isBusy = { [weak self] in self?.lastState.isBusy ?? false }
        hotkey.onAction = { [weak self] a in
            guard let self, !self.lifecycle.suspended else { return }
            self.client.send(Control(MessageType.hotkey, ["action": a]))
        }
        hotkey.register()
        typeHotkey.onAction = { [weak self] a in
            guard let self, a == "down", !self.lifecycle.suspended else { return }
            self.orb.beginTyping()
        }
        typeHotkey.register()
        dictateHotkey.onAction = { [weak self] a in
            guard let self, !self.lifecycle.suspended, a == "down" || a == "up" else { return }
            self.client.send(Control(MessageType.hotkey, ["action": "dictate_" + a]))
        }
        dictateHotkey.register()
        talkOverHotkey.onAction = { [weak self] a in
            guard let self, !self.lifecycle.suspended, a == "down" || a == "up" else { return }
            self.client.send(Control(MessageType.hotkey, ["action": a]))  // the same down/up as Option+Space: the core stops and listens
        }
    }

    /// Space belongs to Jarvis only while it is talking (and never while you are typing to it).
    private func updateTalkOverKey() {
        let talking = lastState == .speaking || lastState == .announcing
        talkOverHotkey.setEnabled(spaceToInterrupt && talking && !orb.model.typing && !lifecycle.suspended)
    }

    private func wireLifecycle() {
        lifecycle.onEvent = { [weak self] ev in
            guard let self else { return }
            switch ev {
            case "will_sleep", "screen_locked":
                self.audio.stopAll()
                self.applyMic()
            case "did_wake", "screen_unlocked":
                self.applyMic()
            default: break
            }
            self.client.send(Control(MessageType.lifecycle, ["event": ev]))
        }
    }

    private func wireUsage() {
        usage.onChange = { [weak self] mic, cam, hint in
            self?.client.send(Control(MessageType.deviceUsage,
                                      ["mic_in_use_by_other": mic, "camera_in_use": cam, "app_hint": hint]))
        }
    }

    /// The microphone device is open only when the core wants it, it isn't muted and the laptop isn't asleep/locked.
    private func applyMic() {
        let open = coreWantsCapture && coreMicEnabled && !lifecycle.suspended
        audio.micEnabled = open
        if open { audio.startCapture() } else { audio.stopCapture() }
    }

    // MARK: routing

    private func route(_ c: Control) {
        switch c.type {
        case MessageType.memoryResult:
            memoryWindow.bridge.handle(c)
        case MessageType.state:
            let name = c.string("name") ?? ""
            if let s = OrbStateName(rawValue: name) { lastState = s }
            orb.apply(state: name, caption: c.string("caption"))
            updateTalkOverKey()
            refreshStatus()
        case MessageType.options:
            spaceToInterrupt = c.bool("space_to_interrupt") ?? false
            updateTalkOverKey()
        case MessageType.caption:
            orb.showCaption(c.string("text") ?? "", final: c.bool("final") ?? false)
        case MessageType.rich:
            orb.showRich(c.string("text") ?? "")
        case MessageType.card:
            if let d = c.data["card"] as? [String: Any], let card = CardData.parse(d) { orb.showCard(card) }
        case MessageType.hideUI:
            orb.handle("hide")
        case MessageType.orbQueue:
            orb.setQueue((c.data["items"] as? [[String: Any]]) ?? [])
            statusItem.button?.title = orb.model.queue.isEmpty ? "" : " \(orb.model.queue.count)"
        case MessageType.playBegin:
            if let id = c.string("utterance_id"), !lifecycle.suspended { audio.beginUtterance(id) }
            else if let id = c.string("utterance_id") {
                client.send(Control(MessageType.playback, ["utterance_id": id, "event": "interrupted"]))
            }
        case MessageType.playEnd:
            if let id = c.string("utterance_id") { audio.endUtterance(id) }
        case MessageType.stopAudio:
            audio.stopAll()
        case MessageType.earcon:
            if !lifecycle.suspended, let n = c.string("name") { audio.playEarcon(n) }
        case MessageType.mic:
            coreMicEnabled = c.bool("enabled") ?? true
            orb.model.muted = !coreMicEnabled
            muteItem?.state = coreMicEnabled ? .off : .on
            applyMic()
        case MessageType.micOpen:
            coreWantsCapture = c.bool("open") ?? false
            applyMic()
        case MessageType.notify:
            notify(title: c.string("title") ?? "Jarvis", body: c.string("body") ?? "")
        case MessageType.permissionPrompt:
            let hint = Permissions.request(c.string("which") ?? "", location: location)
            orb.apply(state: "confirming", caption: "Please allow \(hint) for Jarvis")
        case MessageType.axRequest, MessageType.inputRequest, MessageType.screenshotRequest,
             MessageType.locationRequest:
            handleRequest(c)
        default:
            break
        }
    }

    private func handleRequest(_ c: Control) {
        let rid = c.string("request_id") ?? ""
        let op = c.string("op") ?? ""
        let args = (c.data["args"] as? [String: Any]) ?? [:]
        func reply(_ r: Result<[String: Any], AXFailure>) {
            var body: [String: Any] = ["request_id": rid]
            switch r {
            case .success(let d): body["ok"] = true; body["data"] = d
            case .failure(let e): body["ok"] = false; body["error"] = e.message
            }
            client.send(Control(MessageType.axResult, body))
        }
        if c.type == MessageType.locationRequest {
            location.current { reply($0) }
            return
        }
        work.async {
            let result: Result<[String: Any], AXFailure>
            do {
                switch c.type {
                case MessageType.axRequest: result = .success(try self.ax.handle(op: op, args: args))
                case MessageType.inputRequest:
                    guard AXIsProcessTrusted() else { throw AXFailure(message: "Accessibility permission is not granted") }
                    result = .success(try self.input.handle(op: op, args: args))
                default: result = .success(op == "camera" ? try Camera.capture(args: args) : try Screenshot.capture(args: args))
                }
            } catch let e as AXFailure { result = .failure(e) }
            catch { result = .failure(AXFailure(message: error.localizedDescription)) }
            DispatchQueue.main.async { reply(result) }
        }
    }

    private func notify(title: String, body: String) {
        guard Bundle.main.bundleIdentifier != nil else { return }
        let center = UNUserNotificationCenter.current()
        center.requestAuthorization(options: [.alert]) { ok, _ in
            guard ok else { return }
            let content = UNMutableNotificationContent()
            content.title = title
            content.body = body
            center.add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
        }
    }

    // MARK: menu bar

    /// Launching Jarvis.app again (Spotlight, Finder, Dock) while it runs brings the orb back and keeps it on screen.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        orb.announce("Jarvis is here. Say \"Hey Jarvis\" or press Option+Space (Option+Shift+Space to type). Use \"Keep orb on screen\" in the menu bar icon to pin the orb.")
        return true
    }

    private func buildMenu() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = NSImage(systemSymbolName: "circle.hexagongrid.fill", accessibilityDescription: "Jarvis")
        let m = NSMenu()
        statusMenuItem = NSMenuItem(title: "Starting…", action: nil, keyEquivalent: "")
        m.addItem(statusMenuItem)
        m.addItem(.separator())
        muteItem = item("Turn Jarvis off (stop listening)", #selector(toggleMute))
        m.addItem(muteItem)
        m.addItem(item("Pause for 1 hour", #selector(pauseHour)))
        m.addItem(item("Forget my voice (respond to anyone)", #selector(forgetVoice)))
        m.addItem(item("Brief me now", #selector(briefNow)))
        m.addItem(item("Type to Jarvis…  (⌥⇧Space)", #selector(typeToJarvis)))
        pinItem = item("Keep orb on screen", #selector(togglePin))
        pinItem.state = orb.pinned ? .on : .off
        m.addItem(pinItem)
        m.addItem(item("Show conversation", #selector(showConversation)))
        m.addItem(item("Memory…", #selector(openMemory)))
        let moveMenu = NSMenu()
        for (title, key) in [("Top left", "tl"), ("Top right", "tr"), ("Bottom left", "bl"), ("Bottom right", "br")] {
            let i = item(title, #selector(moveOrb(_:)))
            i.representedObject = key
            moveMenu.addItem(i)
        }
        let moveItem = NSMenuItem(title: "Move orb", action: nil, keyEquivalent: "")
        moveItem.submenu = moveMenu
        m.addItem(moveItem)
        m.addItem(item("Stop everything", #selector(stopEverything)))
        m.addItem(.separator())
        m.addItem(item("Open config", #selector(openConfig)))
        m.addItem(item("Open logs", #selector(openLogs)))
        m.addItem(item("Restart Jarvis", #selector(restartDaemon)))
        m.addItem(item("Permissions…", #selector(openPermissions)))
        loginItem = item("Start at login", #selector(toggleLogin))
        loginItem.state = LoginItem.enabled ? .on : .off
        m.addItem(loginItem)
        m.addItem(.separator())
        m.addItem(item("Quit Jarvis", #selector(quit)))
        statusItem.menu = m
    }

    @objc private func openMemory() { memoryWindow.show() }

    private func item(_ title: String, _ sel: Selector) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: sel, keyEquivalent: "")
        i.target = self
        return i
    }

    private func refreshStatus() {
        var text: String
        switch supervisorStatus {
        case .failed: text = "Jarvis stopped (crashing). Check logs."
        case .backoff(let s): text = "Restarting in \(Int(s))s…"
        case .stopped: text = "Jarvis is off"
        default: text = coreConnected ? "Jarvis: \(lastState.rawValue.replacingOccurrences(of: "_", with: " "))" : "Connecting…"
        }
        if lifecycle.suspended { text = "Paused (laptop closed or locked)" }
        statusMenuItem?.title = text
    }

    private func supervisorChanged(_ s: DaemonSupervisor.Status) {
        supervisorStatus = s
        if s == .failed {
            orb.apply(state: "error", caption: "Jarvis keeps crashing. See logs.")
            // The orb is hidden unless something is happening, so also say it where you will see it.
            notify(title: "Jarvis needs attention", body: "The assistant keeps crashing. Open the menu bar icon and choose Open logs.")
        }
        refreshStatus()
    }

    @objc private func toggleMute() { client.send(Control(MessageType.orbClick, ["action": "mute_toggle"])) }
    @objc private func showConversation() { orb.showConversation() }
    @objc private func typeToJarvis() { orb.beginTyping() }
    @objc private func togglePin() { orb.pinned.toggle(); pinItem.state = orb.pinned ? .on : .off }
    @objc private func moveOrb(_ sender: NSMenuItem) { if let k = sender.representedObject as? String { orb.moveToCorner(k) } }
    @objc private func forgetVoice() { client.send(Control(MessageType.orbClick, ["action": "voice_id_forget"])) }
    @objc private func pauseHour() { client.send(Control(MessageType.orbClick, ["action": "pause_hour"])) }
    @objc private func briefNow() { client.send(Control(MessageType.command, ["name": "brief_now"])) }
    @objc private func stopEverything() { client.send(Control(MessageType.command, ["name": "kill"])) }
    @objc private func restartDaemon() { supervisor.restart() }
    @objc private func openConfig() { NSWorkspace.shared.open(DaemonSupervisor.home.appendingPathComponent("config.toml")) }
    @objc private func openLogs() { NSWorkspace.shared.open(DaemonSupervisor.home.appendingPathComponent("logs")) }
    @objc private func openPermissions() { Permissions.openSettings("Privacy") }
    @objc private func toggleLogin() {
        if LoginItem.set(!LoginItem.enabled) { loginItem.state = LoginItem.enabled ? .on : .off }
    }
    @objc private func quit() { NSApp.terminate(nil) }
}
