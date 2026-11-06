import Foundation

/// Restart delays 1, 2, 4, 8, 30 seconds (then stays at 30).
public struct Backoff {
    public static let steps: [TimeInterval] = [1, 2, 4, 8, 30]
    private var attempt = 0

    public init() {}

    public mutating func next() -> TimeInterval {
        let d = Backoff.steps[min(attempt, Backoff.steps.count - 1)]
        attempt += 1
        return d
    }

    public mutating func reset() { attempt = 0 }
}

/// Gives up (red orb) after `limit` crashes inside `window` seconds.
public struct CrashTracker {
    public let limit: Int
    public let window: TimeInterval
    private var times: [Date] = []

    public init(limit: Int = 5, window: TimeInterval = 300) {
        self.limit = limit
        self.window = window
    }

    /// Records a crash and returns true when the crash budget is exhausted.
    public mutating func record(at date: Date = Date()) -> Bool {
        times.append(date)
        times.removeAll { date.timeIntervalSince($0) > window }
        return times.count >= limit
    }

    public mutating func reset() { times.removeAll() }
}

public struct IPCInfo: Equatable {
    public let host: String
    public let port: Int
    public let token: String
    public let pid: Int

    public static func parse(_ data: Data) -> IPCInfo? {
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let port = obj["port"] as? Int,
              let token = obj["token"] as? String else { return nil }
        return IPCInfo(host: (obj["host"] as? String) ?? "127.0.0.1", port: port, token: token,
                       pid: (obj["pid"] as? Int) ?? 0)
    }

    public var url: URL { URL(string: "ws://\(host):\(port)/ipc")! }
}

/// Audio clients that are not "the user on a call" (system voice services etc.).
public func isIgnorableAudioClient(bundleId: String) -> Bool {
    let prefixes = [
        "com.apple.corespeech", "com.apple.assistant", "com.apple.siri", "com.apple.audio.",
        "com.apple.voicememos.helper", "com.apple.accessibility", "com.apple.VoiceOver",
        "com.apple.speech", "com.apple.dictation",
    ]
    let id = bundleId.lowercased()  // macOS reports e.g. "com.apple.CoreSpeech"
    return prefixes.contains { id.hasPrefix($0.lowercased()) } || id.hasPrefix("com.jarvis.")
}

/// macOS's own daemons (historicalaudiod and friends) record audio without a bundle id; they never mean "on a call".
public func isSystemExecutablePath(_ path: String) -> Bool {
    ["/usr/libexec/", "/usr/sbin/", "/System/Library/", "/usr/bin/"].contains { path.hasPrefix($0) }
}

/// Orb states understood by the Shell, as sent by the core.
public enum OrbStateName: String {
    case booting, idle, listening, transcribing, thinking, confirming, speaking
    case followUp = "follow_up"
    case announcing, paused, resuming
    case error

    public var isBusy: Bool {
        switch self {
        case .listening, .transcribing, .thinking, .confirming, .speaking, .announcing, .followUp: return true
        default: return false
        }
    }
}

/// Labels the Shell itself refuses to press/click, as a second line of defence behind the core's filter.
public enum ForbiddenLabels {
    public static let words: [String] = [
        "send", "post", "submit", "buy", "pay", "purchase", "delete", "remove",
        "confirm order", "place order", "checkout", "check out", "publish", "tweet",
    ]

    public static func isForbidden(_ label: String) -> Bool {
        let l = label.lowercased()
        let tokens = l.split(whereSeparator: { !$0.isLetter && !$0.isNumber }).map(String.init)
        for w in words {
            if w.contains(" ") {
                if l.contains(w) { return true }
            } else if tokens.contains(w) || tokens.contains(w + "s") {
                return true
            }
        }
        return false
    }
}
