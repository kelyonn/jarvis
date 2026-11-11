import Foundation

public struct CardRow: Equatable {
    public let label: String
    public let value: String
    public let tone: String   // "", "up", "down", "warn"
}

public struct CardItem: Equatable {
    public let title: String
    public let subtitle: String
    public let trailing: String
    public let unread: Bool
    public let initial: String
}

public struct CardChip: Equatable {
    public let text: String
    public let warn: Bool
}

public struct CardAction: Equatable {
    public enum Kind: String { case say, url, copy }
    public let label: String
    public let kind: Kind
    public let value: String
    public let primary: Bool

    /// A link may only be http(s). The daemon already filters, and the Shell checks again before opening anything.
    public var isSafe: Bool {
        guard kind == .url else { return true }
        guard let u = URL(string: value), let s = u.scheme?.lowercased() else { return false }
        return (s == "http" || s == "https") && u.host != nil
    }
}

/// A rich card sent with a reply: plain data the window draws. See `jarvis_core/cards.py` for the schema.
public struct CardData: Equatable, Identifiable {
    public let id = UUID()
    public let kind: String   // tile, facts, glance, list, media, code
    public let tone: String   // normal, confirm, error, success
    public let icon: String
    public let kicker: String
    public let title: String
    public let subtitle: String
    public let big: String
    public let bigNote: String
    public let body: String
    public let code: String
    public let lang: String
    public let rows: [CardRow]
    public let items: [CardItem]
    public let chips: [CardChip]
    public let progress: Double?
    public let footnote: String
    public let actions: [CardAction]

    public static func == (a: CardData, b: CardData) -> Bool { a.id == b.id }

    /// Parse the daemon's `card` dictionary. Unknown kinds and empty cards return nil, so the reply shows as plain text.
    public static func parse(_ d: [String: Any]) -> CardData? {
        let kinds: Set<String> = ["tile", "facts", "glance", "list", "media", "code"]
        guard let kind = d["kind"] as? String, kinds.contains(kind) else { return nil }
        func str(_ k: String) -> String { (d[k] as? String) ?? "" }
        let tone = ["normal", "confirm", "error", "success"].contains(str("tone")) ? str("tone") : "normal"
        let rows = (d["rows"] as? [[String: Any]] ?? []).prefix(12).map {
            CardRow(label: ($0["label"] as? String) ?? "", value: ($0["value"] as? String) ?? "", tone: ($0["tone"] as? String) ?? "")
        }
        let items = (d["items"] as? [[String: Any]] ?? []).prefix(8).compactMap { i -> CardItem? in
            guard let t = i["title"] as? String, !t.isEmpty else { return nil }
            return CardItem(title: t, subtitle: (i["subtitle"] as? String) ?? "", trailing: (i["trailing"] as? String) ?? "",
                            unread: (i["unread"] as? Bool) ?? false, initial: (i["initial"] as? String) ?? "")
        }
        let chips = (d["chips"] as? [[String: Any]] ?? []).prefix(6).compactMap { c -> CardChip? in
            guard let t = c["text"] as? String, !t.isEmpty else { return nil }
            return CardChip(text: t, warn: (c["tone"] as? String) == "warn")
        }
        var actions: [CardAction] = (d["actions"] as? [[String: Any]] ?? []).prefix(3).compactMap { a in
            guard let label = a["label"] as? String, let kindRaw = a["kind"] as? String, let k = CardAction.Kind(rawValue: kindRaw),
                  let value = a["value"] as? String, !label.isEmpty, !value.isEmpty else { return nil }
            return CardAction(label: label, kind: k, value: value, primary: (a["primary"] as? Bool) ?? false)
        }
        actions = actions.filter { $0.isSafe }
        let card = CardData(kind: kind, tone: tone, icon: str("icon"), kicker: str("kicker"), title: str("title"), subtitle: str("subtitle"),
                            big: str("big"), bigNote: str("big_note"), body: str("body"), code: str("code"), lang: str("lang"),
                            rows: Array(rows), items: Array(items), chips: Array(chips),
                            progress: (d["progress"] as? Double).map { min(max($0, 0), 1) },
                            footnote: str("footnote"), actions: actions)
        let hasContent = !card.title.isEmpty || !card.big.isEmpty || !card.rows.isEmpty || !card.items.isEmpty
            || !card.body.isEmpty || !card.code.isEmpty || !card.chips.isEmpty
        return hasContent ? card : nil
    }

    /// The card as plain text, for Copy conversation.
    public var plainText: String {
        var lines: [String] = []
        for s in [kicker, title, big.isEmpty ? "" : "\(big) \(bigNote)", subtitle, body, code] where !s.isEmpty { lines.append(s) }
        lines += rows.map { "\($0.label): \($0.value)" }
        lines += items.map { [$0.title, $0.subtitle, $0.trailing].filter { !$0.isEmpty }.joined(separator: " · ") }
        if !chips.isEmpty { lines.append(chips.map(\.text).joined(separator: ", ")) }
        if !footnote.isEmpty { lines.append(footnote) }
        return lines.joined(separator: "\n")
    }
}
