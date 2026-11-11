import Foundation

public struct MemoryItem: Identifiable, Equatable {
    public let id: String
    public var text: String
    public let date: String
    public init(id: String, text: String, date: String) { self.id = id; self.text = text; self.date = date }
}

public struct MemoryGroup: Identifiable, Equatable {
    public var id: String { kind }
    public let kind: String
    public let title: String
    public var items: [MemoryItem]
    public init(kind: String, title: String, items: [MemoryItem]) { self.kind = kind; self.title = title; self.items = items }
}

/// Pure parsing of the daemon's `memory.list` reply, so it can be tested without a window.
public enum MemoryReply {
    public static func groups(from data: [String: Any]) -> [MemoryGroup] {
        guard let raw = data["groups"] as? [[String: Any]] else { return [] }
        return raw.compactMap { g in
            guard let kind = g["kind"] as? String, let title = g["title"] as? String else { return nil }
            let items = (g["items"] as? [[String: Any]] ?? []).compactMap { i -> MemoryItem? in
                guard let id = i["id"] as? String, let text = i["text"] as? String else { return nil }
                return MemoryItem(id: id, text: text, date: i["date"] as? String ?? "")
            }
            return items.isEmpty ? nil : MemoryGroup(kind: kind, title: title, items: items)
        }
    }

    public static func error(from data: [String: Any]) -> String? {
        (data["ok"] as? Bool) == false ? (data["error"] as? String ?? "Something went wrong.") : nil
    }
}
