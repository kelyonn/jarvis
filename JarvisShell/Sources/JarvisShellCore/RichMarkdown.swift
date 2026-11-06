import Foundation

/// The small slice of markdown the chat card shows: headings, paragraphs, lists, tables, quotes, code and display maths.
/// Inline styling (bold, italic, `code`, links) is left in the text for the view to render.
public enum RichBlock: Equatable {
    case heading(level: Int, text: String)
    case paragraph(String)
    case code(language: String, text: String)
    case math(String)
    case bullets([String])
    case numbered([NumberedItem])
    case table(header: [String], rows: [[String]])
    case quote(String)
    case rule
}

public struct NumberedItem: Equatable {
    public let number: Int
    public let text: String
    public init(number: Int, text: String) { self.number = number; self.text = text }
}

public enum RichMarkdown {
    public static func parse(_ source: String) -> [RichBlock] {
        let lines = source.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n")
        var blocks: [RichBlock] = []
        var para: [String] = []
        var i = 0

        func flushPara() {
            let text = para.joined(separator: " ").trimmingCharacters(in: .whitespaces)
            if !text.isEmpty { blocks.append(.paragraph(text)) }
            para = []
        }
        func isTableSeparator(_ s: String) -> Bool {
            let t = s.trimmingCharacters(in: .whitespaces)
            guard t.contains("-"), t.contains("|") else { return false }
            return t.allSatisfy { "|-: ".contains($0) }
        }
        func cells(_ s: String) -> [String] {
            var t = s.trimmingCharacters(in: .whitespaces)
            if t.hasPrefix("|") { t.removeFirst() }
            if t.hasSuffix("|") { t.removeLast() }
            return t.components(separatedBy: "|").map { $0.trimmingCharacters(in: .whitespaces) }
        }

        while i < lines.count {
            let line = lines[i]
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            if trimmed.hasPrefix("```") {
                flushPara()
                let lang = String(trimmed.dropFirst(3)).trimmingCharacters(in: .whitespaces).lowercased()
                var body: [String] = []
                i += 1
                while i < lines.count, !lines[i].trimmingCharacters(in: .whitespaces).hasPrefix("```") { body.append(lines[i]); i += 1 }
                i += 1  // closing fence
                let text = body.joined(separator: "\n")
                blocks.append(lang == "math" ? .math(text.trimmingCharacters(in: .whitespacesAndNewlines)) : .code(language: lang, text: text))
                continue
            }
            if trimmed.isEmpty { flushPara(); i += 1; continue }
            if let h = heading(trimmed) { flushPara(); blocks.append(.heading(level: h.0, text: h.1)); i += 1; continue }
            if trimmed.count >= 3, trimmed.allSatisfy({ "-*_".contains($0) }), Set(trimmed).count == 1 { flushPara(); blocks.append(.rule); i += 1; continue }
            if trimmed.contains("|"), i + 1 < lines.count, isTableSeparator(lines[i + 1]) {
                flushPara()
                let header = cells(trimmed)
                var rows: [[String]] = []
                i += 2
                while i < lines.count, lines[i].contains("|"), !lines[i].trimmingCharacters(in: .whitespaces).isEmpty {
                    var r = cells(lines[i])
                    while r.count < header.count { r.append("") }
                    rows.append(Array(r.prefix(max(header.count, 1))))
                    i += 1
                }
                blocks.append(.table(header: header, rows: rows))
                continue
            }
            if bulletText(trimmed) != nil {
                flushPara()
                var items: [String] = []
                while i < lines.count, let t = bulletText(lines[i].trimmingCharacters(in: .whitespaces)) { items.append(t); i += 1 }
                blocks.append(.bullets(items))
                continue
            }
            if numberedText(trimmed) != nil {
                flushPara()
                var items: [NumberedItem] = []
                while i < lines.count, let n = numberedText(lines[i].trimmingCharacters(in: .whitespaces)) { items.append(NumberedItem(number: n.0, text: n.1)); i += 1 }
                blocks.append(.numbered(items))
                continue
            }
            if trimmed.hasPrefix(">") {
                flushPara()
                var q: [String] = []
                while i < lines.count, lines[i].trimmingCharacters(in: .whitespaces).hasPrefix(">") {
                    q.append(String(lines[i].trimmingCharacters(in: .whitespaces).dropFirst()).trimmingCharacters(in: .whitespaces)); i += 1
                }
                blocks.append(.quote(q.joined(separator: " ")))
                continue
            }
            para.append(trimmed)
            i += 1
        }
        flushPara()
        return blocks
    }

    private static func heading(_ s: String) -> (Int, String)? {
        guard s.hasPrefix("#") else { return nil }
        let hashes = s.prefix { $0 == "#" }.count
        guard (1...4).contains(hashes), s.dropFirst(hashes).first == " " else { return nil }
        return (hashes, String(s.dropFirst(hashes)).trimmingCharacters(in: .whitespaces))
    }

    private static func bulletText(_ s: String) -> String? {
        for marker in ["- ", "* ", "• "] where s.hasPrefix(marker) { return String(s.dropFirst(marker.count)) }
        return nil
    }

    private static func numberedText(_ s: String) -> (Int, String)? {
        let digits = s.prefix { $0.isNumber }
        guard !digits.isEmpty, digits.count <= 3, let n = Int(digits) else { return nil }
        let rest = s.dropFirst(digits.count)
        guard let sep = rest.first, sep == "." || sep == ")", rest.dropFirst().first == " " else { return nil }
        return (n, String(rest.dropFirst(2)))
    }
}
