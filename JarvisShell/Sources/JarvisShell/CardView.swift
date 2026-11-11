import AppKit
import JarvisShellCore
import SwiftUI

/// Draws one rich card (`CardData`) inside a reply. Tokens follow the Jarvis design system: white-on-card fills,
/// amber only for "needs your OK", red only for problems, green/red for up/down always with a sign.
struct CardView: View {
    let card: CardData
    var onAction: (CardAction) -> Void

    private static let tileFill = Color.white.opacity(0.06)
    private static let hairline = Color.white.opacity(0.1)
    private static let link = Color(red: 0.553, green: 0.722, blue: 1.0)
    private static let up = Color(red: 0.498, green: 0.839, blue: 0.604)
    private static let down = Color(red: 1.0, green: 0.541, blue: 0.502)
    private static let amber = Color(red: 0.96, green: 0.72, blue: 0.2)
    private static let red = Color(red: 0.95, green: 0.36, blue: 0.33)

    private var accent: Color? {
        switch card.tone {
        case "confirm": return Self.amber
        case "error": return Self.red
        case "success": return Self.up
        default: return nil
        }
    }

    private func tone(_ t: String) -> Color {
        switch t {
        case "up": return Self.up
        case "down": return Self.down
        case "warn": return Self.amber
        default: return .primary
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            header
            if !card.big.isEmpty { glance }
            if !card.title.isEmpty && card.kind != "media" {
                Text(card.title).font(.system(size: 14.5, weight: .semibold)).fixedSize(horizontal: false, vertical: true)
            }
            if card.kind == "media" { media }
            if !card.subtitle.isEmpty && card.kind != "media" {
                Text(card.subtitle).font(.system(size: 12.5)).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if !card.body.isEmpty {
                Text(card.body).font(.system(size: 13)).lineSpacing(2).foregroundColor(.primary.opacity(0.85))
                    .lineLimit(6).fixedSize(horizontal: false, vertical: true)
            }
            if !card.code.isEmpty { code }
            if !card.chips.isEmpty { chips }
            if !card.rows.isEmpty { rows }
            if !card.items.isEmpty { items }
            if let p = card.progress, card.kind != "media" { bar(p) }
            if !card.footnote.isEmpty {
                Text(card.footnote).font(.system(size: 11.5)).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if !card.actions.isEmpty { actions }
        }
        .padding(11)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Self.tileFill))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous)
            .strokeBorder(accent?.opacity(card.tone == "success" ? 0.0 : 0.5) ?? Self.hairline, lineWidth: 0.5))
    }

    // MARK: pieces

    @ViewBuilder private var header: some View {
        if !card.kicker.isEmpty || !card.icon.isEmpty {
            HStack(spacing: 6) {
                if !card.icon.isEmpty {
                    Image(systemName: card.icon).font(.system(size: 11.5, weight: .medium)).foregroundColor(accent ?? .secondary)
                }
                if !card.kicker.isEmpty {
                    Text(card.kicker).font(.system(size: 11.5, weight: .medium)).foregroundColor(accent ?? .secondary)
                        .lineLimit(1)
                }
            }
        }
    }

    private var glance: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(card.big).font(.system(size: 30, weight: .semibold, design: .rounded))
            if !card.bigNote.isEmpty { Text(card.bigNote).font(.system(size: 12.5)).foregroundColor(.secondary) }
        }
    }

    private var media: some View {
        HStack(spacing: 10) {
            RoundedRectangle(cornerRadius: 7, style: .continuous).fill(Color.white.opacity(0.1)).frame(width: 44, height: 44)
                .overlay(Image(systemName: "music.note").foregroundColor(.secondary))
            VStack(alignment: .leading, spacing: 1) {
                Text(card.title).font(.system(size: 14, weight: .semibold)).lineLimit(1)
                Text(card.subtitle).font(.system(size: 12)).foregroundColor(.secondary).lineLimit(1)
            }
        }
    }

    private var code: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            Text(card.code).font(.system(size: 11.5, design: .monospaced)).textSelection(.enabled)
                .padding(8).fixedSize(horizontal: true, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.black.opacity(0.38)))
        .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(Color.white.opacity(0.08), lineWidth: 0.5))
    }

    private var chips: some View {
        FlowRow(spacing: 5) {
            ForEach(Array(card.chips.enumerated()), id: \.offset) { _, c in
                Text(c.text).font(.system(size: 11.5)).lineLimit(1)
                    .foregroundColor(c.warn ? Self.amber : .primary.opacity(0.9))
                    .padding(.horizontal, 8).padding(.vertical, 2)
                    .background(RoundedRectangle(cornerRadius: 6, style: .continuous).fill(c.warn ? Self.amber.opacity(0.16) : Color.white.opacity(0.1)))
            }
        }
    }

    private var rows: some View {
        VStack(spacing: 0) {
            ForEach(Array(card.rows.enumerated()), id: \.offset) { i, r in
                VStack(spacing: 0) {
                    if i > 0 || card.kind == "facts" { Rectangle().fill(Self.hairline).frame(height: 0.5).opacity(i == 0 ? 0 : 1) }
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Text(r.label).font(.system(size: 12.5)).foregroundColor(.secondary)
                        Spacer(minLength: 6)
                        Text(r.value).font(.system(size: 12.5)).foregroundColor(tone(r.tone)).multilineTextAlignment(.trailing)
                            .textSelection(.enabled)
                    }
                    .padding(.vertical, 5)
                }
            }
        }
    }

    private var items: some View {
        VStack(spacing: 0) {
            ForEach(Array(card.items.enumerated()), id: \.offset) { i, it in
                VStack(spacing: 0) {
                    if i > 0 { Rectangle().fill(Self.hairline).frame(height: 0.5) }
                    HStack(spacing: 9) {
                        if !it.initial.isEmpty {
                            Text(it.initial).font(.system(size: 10.5, weight: .semibold)).frame(width: 24, height: 24)
                                .background(Circle().fill(Color.white.opacity(0.12)))
                        } else if it.unread {
                            Circle().fill(Self.link).frame(width: 6, height: 6)
                        }
                        VStack(alignment: .leading, spacing: 1) {
                            Text(it.title).font(.system(size: 12.5, weight: it.unread ? .semibold : .regular)).lineLimit(1)
                            if !it.subtitle.isEmpty {
                                Text(it.subtitle).font(.system(size: 11.5)).foregroundColor(.secondary).lineLimit(1)
                            }
                        }
                        Spacer(minLength: 6)
                        if !it.trailing.isEmpty { Text(it.trailing).font(.system(size: 11.5)).foregroundColor(.secondary) }
                    }
                    .padding(.vertical, 6)
                }
            }
        }
    }

    private func bar(_ p: Double) -> some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.white.opacity(0.12))
                Capsule().fill(p < 0.2 ? Self.amber : Color.primary.opacity(0.85)).frame(width: g.size.width * p)
            }
        }
        .frame(height: 4)
    }

    private var actions: some View {
        FlowRow(spacing: 6) {
            ForEach(Array(card.actions.enumerated()), id: \.offset) { _, a in
                Button { onAction(a) } label: {
                    Text(a.label).font(.system(size: 12, weight: .medium))
                        .foregroundColor(a.primary ? Color(white: 0.11) : .primary)
                        .padding(.horizontal, 10).padding(.vertical, 6)
                        .background(RoundedRectangle(cornerRadius: 7, style: .continuous)
                            .fill(a.primary ? Color(white: 0.92) : Color.white.opacity(0.1)))
                }
                .buttonStyle(.plain)
                .help(a.kind == .url ? "Opens in your browser" : a.kind == .copy ? "Copies to the clipboard" : "Same as saying: \(a.value)")
            }
        }
        .padding(.top, 2)
    }
}

/// Lays children out left to right and wraps to the next line when the row is full (chips and buttons).
struct FlowRow: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        arrange(width: proposal.width ?? .infinity, subviews: subviews).size
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let laid = arrange(width: bounds.width, subviews: subviews)
        for (i, p) in laid.points.enumerated() {
            subviews[i].place(at: CGPoint(x: bounds.minX + p.x, y: bounds.minY + p.y), proposal: .unspecified)
        }
    }

    private func arrange(width: CGFloat, subviews: Subviews) -> (size: CGSize, points: [CGPoint]) {
        var x: CGFloat = 0, y: CGFloat = 0, rowH: CGFloat = 0, maxX: CGFloat = 0
        var pts: [CGPoint] = []
        for s in subviews {
            let sz = s.sizeThatFits(.unspecified)
            if x > 0 && x + sz.width > width { x = 0; y += rowH + spacing; rowH = 0 }
            pts.append(CGPoint(x: x, y: y))
            x += sz.width + spacing
            rowH = max(rowH, sz.height)
            maxX = max(maxX, x - spacing)
        }
        return (CGSize(width: maxX, height: y + rowH), pts)
    }
}
