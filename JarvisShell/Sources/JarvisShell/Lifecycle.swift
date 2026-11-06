import AppKit
import Foundation

/// Sleep / wake / lock / screen observers. The Shell reacts locally first (stops audio, mutes the mic)
/// and then tells the core, so nothing is spoken into a closed laptop.
final class Lifecycle {
    var onEvent: ((String) -> Void)?
    private(set) var asleep = false
    private(set) var locked = false
    private(set) var screensOff = false

    var suspended: Bool { asleep || locked }

    func start() {
        let ws = NSWorkspace.shared.notificationCenter
        ws.addObserver(forName: NSWorkspace.willSleepNotification, object: nil, queue: .main) { [weak self] _ in
            self?.asleep = true
            self?.onEvent?("will_sleep")
        }
        ws.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
            self?.asleep = false
            self?.onEvent?("did_wake")
        }
        ws.addObserver(forName: NSWorkspace.screensDidSleepNotification, object: nil, queue: .main) { [weak self] _ in
            self?.screensOff = true
            self?.onEvent?("screens_slept")
        }
        ws.addObserver(forName: NSWorkspace.screensDidWakeNotification, object: nil, queue: .main) { [weak self] _ in
            self?.screensOff = false
            self?.onEvent?("screens_woke")
        }
        let dc = DistributedNotificationCenter.default()
        dc.addObserver(forName: .init("com.apple.screenIsLocked"), object: nil, queue: .main) { [weak self] _ in
            self?.locked = true
            self?.onEvent?("screen_locked")
        }
        dc.addObserver(forName: .init("com.apple.screenIsUnlocked"), object: nil, queue: .main) { [weak self] _ in
            self?.locked = false
            self?.onEvent?("screen_unlocked")
        }
        // Launched while already locked? CGSession tells us.
        if let d = CGSessionCopyCurrentDictionary() as? [String: Any],
           let l = d["CGSSessionScreenIsLocked"] as? Bool, l {
            locked = true
        }
    }
}
