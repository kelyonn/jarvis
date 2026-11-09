import AppKit
import JarvisShellCore
import SwiftUI

/// Draws a formatted reply (code, working, tables, steps) inside the chat card.
struct RichView: View {
    let blocks: [RichBlock]
    var bodySize: CGFloat = 13.5

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                switch block {
                case .heading(let level, let text):
                    inline(text).font(.system(size: level == 1 ? 17 : level == 2 ? 15.5 : 14.5, weight: .semibold))
                case .paragraph(let text):
                    inline(text).font(.system(size: bodySize)).lineSpacing(2.5)
                case .code(let lang, let text):
                    CodeBlockView(language: lang, code: text)
                case .math(let text):
                    MathBlockView(text: text)
                case .bullets(let items):
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                            HStack(alignment: .firstTextBaseline, spacing: 7) {
                                Text("•").foregroundColor(.secondary)
                                inline(item).font(.system(size: bodySize)).lineSpacing(2)
                            }
                        }
                    }
                case .numbered(let items):
                    VStack(alignment: .leading, spacing: 5) {
                        ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                            HStack(alignment: .firstTextBaseline, spacing: 8) {
                                Text("\(item.number)").font(.system(size: 12, weight: .semibold, design: .rounded))
                                    .foregroundColor(.secondary).frame(minWidth: 16, alignment: .trailing)
                                inline(item.text).font(.system(size: bodySize)).lineSpacing(2)
                            }
                        }
                    }
                case .table(let header, let rows):
                    TableBlockView(header: header, rows: rows)
                case .quote(let text):
                    HStack(spacing: 8) {
                        Rectangle().fill(Color.white.opacity(0.25)).frame(width: 2)
                        inline(text).font(.system(size: bodySize)).foregroundColor(.secondary)
                    }
                    .fixedSize(horizontal: false, vertical: true)
                case .rule:
                    Divider().opacity(0.4)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func inline(_ s: String) -> some View {
        let attr = (try? AttributedString(markdown: s, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(s)
        return Text(attr).foregroundColor(.primary).fixedSize(horizontal: false, vertical: true)
    }
}

struct CodeBlockView: View {
    let language: String
    let code: String
    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(language.isEmpty ? "code" : language)
                    .font(.system(size: 10.5, weight: .medium, design: .monospaced)).foregroundColor(.secondary)
                Spacer()
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(code, forType: .string)
                    copied = true
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.4) { copied = false }
                } label: {
                    Label(copied ? "Copied" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc")
                        .font(.system(size: 10.5, weight: .medium)).foregroundColor(.secondary)
                }
                .buttonStyle(.plain)
            }
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background(Color.white.opacity(0.05))
            ScrollView(.horizontal, showsIndicators: false) {
                Text(CodeHighlighter.highlight(code, language: language))
                    .fixedSize()
                    .padding(10)
            }
        }
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.black.opacity(0.38)))
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(Color.white.opacity(0.08), lineWidth: 0.5))
    }
}

struct MathBlockView: View {
    let text: String
    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            Text(text)
                .font(.system(size: 17, design: .serif))
                .foregroundColor(.primary)
                .fixedSize()
                .padding(.horizontal, 14).padding(.vertical, 9)
                .frame(maxWidth: .infinity)
        }
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.white.opacity(0.07)))
    }
}

struct TableBlockView: View {
    let header: [String]
    let rows: [[String]]

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            Grid(alignment: .leading, horizontalSpacing: 18, verticalSpacing: 7) {
                GridRow {
                    ForEach(Array(header.enumerated()), id: \.offset) { _, h in
                        cell(h).font(.system(size: 12.5, weight: .semibold))
                    }
                }
                Divider().opacity(0.5).gridCellUnsizedAxes(.horizontal)
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                    GridRow {
                        ForEach(Array(row.enumerated()), id: \.offset) { _, c in cell(c).font(.system(size: 12.5)) }
                    }
                }
            }
            .padding(10)
        }
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.white.opacity(0.05)))
        .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(Color.white.opacity(0.1), lineWidth: 0.5))
    }

    private func cell(_ s: String) -> Text {
        let attr = (try? AttributedString(markdown: s, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(s)
        return Text(attr)
    }
}

/// Light syntax colouring: comments, strings, numbers and keywords. Good enough to read code at a glance.
enum CodeHighlighter {
    private static let keywords: [String: [String]] = [
        "python": ["def", "class", "return", "if", "elif", "else", "for", "while", "in", "import", "from", "as", "with", "try", "except", "finally", "raise", "lambda", "yield", "pass", "break", "continue", "None", "True", "False", "and", "or", "not", "is", "async", "await", "global"],
        "js": ["const", "let", "var", "function", "return", "if", "else", "for", "while", "class", "new", "import", "export", "from", "default", "async", "await", "try", "catch", "throw", "typeof", "null", "undefined", "true", "false", "this", "switch", "case", "break", "continue", "of", "interface", "type", "extends"],
        "swift": ["func", "let", "var", "struct", "class", "enum", "protocol", "extension", "if", "else", "guard", "return", "for", "in", "while", "switch", "case", "import", "self", "nil", "true", "false", "async", "await", "throws", "try", "private", "public", "static", "init", "some", "where"],
        "bash": ["if", "then", "else", "fi", "for", "do", "done", "while", "case", "esac", "function", "echo", "export", "cd", "sudo", "in"],
        "sql": ["select", "from", "where", "join", "on", "group", "by", "order", "limit", "insert", "into", "values", "update", "set", "delete", "create", "table", "and", "or", "not", "null", "as", "having", "distinct", "count"],
        "generic": ["if", "else", "for", "while", "return", "function", "class", "import", "const", "let", "var", "true", "false", "null", "new", "public", "private", "static", "void", "int", "string", "def", "fn", "struct", "enum"],
    ]
    private static let aliases = ["py": "python", "javascript": "js", "typescript": "js", "ts": "js", "jsx": "js", "tsx": "js", "node": "js",
                                  "react": "js", "sh": "bash", "shell": "bash", "zsh": "bash", "console": "bash"]

    private static let comment = NSColor(calibratedRed: 0.50, green: 0.56, blue: 0.62, alpha: 1)
    private static let string = NSColor(calibratedRed: 0.56, green: 0.86, blue: 0.52, alpha: 1)
    private static let number = NSColor(calibratedRed: 0.98, green: 0.70, blue: 0.40, alpha: 1)
    private static let keyword = NSColor(calibratedRed: 0.80, green: 0.56, blue: 0.98, alpha: 1)
    private static let base = NSColor(calibratedWhite: 0.92, alpha: 1)

    static func highlight(_ code: String, language: String) -> AttributedString {
        let lang = aliases[language] ?? language
        let font = NSFont.monospacedSystemFont(ofSize: 12.3, weight: .regular)
        let out = NSMutableAttributedString(string: code, attributes: [.font: font, .foregroundColor: base])
        let ns = code as NSString
        var taken = [Bool](repeating: false, count: ns.length)

        func paint(_ pattern: String, _ color: NSColor, options: NSRegularExpression.Options = []) {
            guard let rx = try? NSRegularExpression(pattern: pattern, options: options) else { return }
            rx.enumerateMatches(in: code, range: NSRange(location: 0, length: ns.length)) { m, _, _ in
                guard let r = m?.range, r.length > 0, !taken[r.location] else { return }
                out.addAttribute(.foregroundColor, value: color, range: r)
                for k in r.location..<(r.location + r.length) { taken[k] = true }
            }
        }
        switch lang {
        case "python", "bash", "yaml", "yml", "toml", "ruby": paint("#[^\\n]*", comment)
        case "sql": paint("--[^\\n]*", comment)
        default: paint("//[^\\n]*", comment); paint("/\\*.*?\\*/", comment, options: [.dotMatchesLineSeparators])
        }
        paint("\"\"\".*?\"\"\"|'''.*?'''", string, options: [.dotMatchesLineSeparators])
        paint("\"(?:\\\\.|[^\"\\\\\\n])*\"|'(?:\\\\.|[^'\\\\\\n])*'|`(?:\\\\.|[^`\\\\])*`", string)
        paint("\\b\\d+(?:\\.\\d+)?\\b", number)
        let words = keywords[lang] ?? keywords["generic"]!
        paint("\\b(?:" + words.joined(separator: "|") + ")\\b", keyword, options: lang == "sql" ? [.caseInsensitive] : [])
        return (try? AttributedString(out, including: \.appKit)) ?? AttributedString(code)
    }
}
