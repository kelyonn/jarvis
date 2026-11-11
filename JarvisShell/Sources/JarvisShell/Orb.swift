import AppKit
import Combine
import JarvisShellCore
import SwiftUI

struct QueueItem: Identifiable {
    let id: String
    let title: String
    let priority: String
}

struct ChatLine: Identifiable, Equatable {
    let id = UUID()
    let fromUser: Bool
    var text: String
    var rich: String? = nil   // formatted version (markdown) for code, working, tables, steps
    var cards: [CardData] = []   // structured cards (email, schedule, weather...) that came with this reply
}

final class OrbModel: ObservableObject {
    @Published var state: OrbStateName = .booting
    @Published var messages: [ChatLine] = []   // the last few things you said and Jarvis answered
    @Published var notice = ""                 // prompts and errors from the core / Shell
    @Published var queue: [QueueItem] = []
    @Published var muted = false
    @Published var showQueue = false
    @Published var dismissed = false   // you closed the box; it comes back with the next activity
    @Published var alignRight = true   // which side of the orb the card extends to
    @Published var alignBottom = true  // card grows upward when the orb sits in the lower half
    @Published var typing = false      // the panel is the key window and the type box has your keystrokes
    @Published var draft = ""
    @Published var ghost = false       // you clicked another app: Jarvis sits behind its windows until it has something to say
    @Published var focusTick = 0       // bumped to ask the type box to take focus
    @Published var debugMarker = false // self-test: draw a magenta dot on the orb so its pixels can be found
    /// Card size you set by dragging the corner grip (nil = automatic). Remembered between launches.
    @Published var customWidth: CGFloat? = OrbModel.load("orbCardWidth") { didSet { OrbModel.save(customWidth, "orbCardWidth") } }
    @Published var customHeight: CGFloat? = OrbModel.load("orbChatHeight") { didSet { OrbModel.save(customHeight, "orbChatHeight") } }

    private static func load(_ key: String) -> CGFloat? {
        OrbPanel.store.object(forKey: key) == nil ? nil : CGFloat(OrbPanel.store.double(forKey: key))
    }
    private static func save(_ v: CGFloat?, _ key: String) {
        if let v { OrbPanel.store.set(Double(v), forKey: key) } else { OrbPanel.store.removeObject(forKey: key) }
    }

    var autoChatHeight: CGFloat = 40   // chat area height while sizing is automatic (read when you start a resize)
    var cardOverhead: CGFloat = 100    // card height besides the chat area (header, type box, padding)
    /// The most room the card has between the orb and the screen edges. The card never grows past it, so the window
    /// never has to be pushed back onto the screen (which would drag the orb along).
    @Published var room = CGSize(width: 1000, height: 1000)

    var hasText: Bool { !messages.isEmpty || !notice.isEmpty }
    var cardVisible: Bool { !dismissed && (hasText || (showQueue && !queue.isEmpty) || state.isBusy || typing) }
}

private struct HeightKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

private struct SizeKey: PreferenceKey {
    static var defaultValue: CGSize = .zero
    static func reduce(value: inout CGSize, nextValue: () -> CGSize) { value = nextValue() }
}

// MARK: - Views

/// Small, flat indicator: a ring and a dot. No glow; motion is the only signal.
struct StatusOrb: View {
    let state: OrbStateName
    let muted: Bool

    private var accent: Color {
        switch state {
        case .error: return Color(red: 0.95, green: 0.36, blue: 0.33)
        case .confirming: return Color(red: 0.96, green: 0.72, blue: 0.2)
        default: return Color.white
        }
    }

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30)) { ctx in
            let t = ctx.date.timeIntervalSinceReferenceDate
            let active = state == .listening || state == .followUp || state == .speaking || state == .announcing
            let thinking = state == .thinking || state == .transcribing
            let dot = active ? 1.0 + 0.18 * sin(t * (state == .speaking ? 7 : 3)) : 1.0
            ZStack {
                Circle().fill(Color(white: 0.09))
                Circle().strokeBorder(accent.opacity(muted ? 0.25 : 0.55), lineWidth: 1.5)
                if thinking {
                    Circle().trim(from: 0, to: 0.25)
                        .stroke(accent, style: StrokeStyle(lineWidth: 1.5, lineCap: .round))
                        .rotationEffect(.degrees(t * 240))
                }
                Circle().fill(accent.opacity(muted ? 0.3 : 0.92))
                    .frame(width: 12, height: 12)
                    .scaleEffect(dot)
                if muted {
                    Rectangle().fill(Color(white: 0.09)).frame(width: 22, height: 2).rotationEffect(.degrees(-45))
                    Rectangle().fill(accent.opacity(0.55)).frame(width: 22, height: 1.5).rotationEffect(.degrees(-45))
                }
            }
            .frame(width: 36, height: 36)
            .shadow(color: .black.opacity(0.28), radius: 6, y: 2)
        }
        .frame(width: 48, height: 48)
    }
}

struct OrbView: View {
    @ObservedObject var model: OrbModel
    var onClick: (String) -> Void
    var onSize: (CGSize) -> Void
    var onDrag: (Bool) -> Void  // true while dragging, false when released
    var onSend: (String) -> Void = { _ in }
    var onCardAction: (CardAction) -> Void = { _ in }
    var onEscape: () -> Void = {}

    static let widthRange: ClosedRange<CGFloat> = 320...1000
    static let heightRange: ClosedRange<CGFloat> = 80...760
    private var autoWidth: CGFloat { model.messages.contains { $0.rich != nil || !$0.cards.isEmpty } ? 600 : 460 }
    private var autoMaxHeight: CGFloat { model.messages.contains { $0.rich != nil || !$0.cards.isEmpty } ? 540 : 380 }
    private var cardWidth: CGFloat { max(min(model.customWidth ?? autoWidth, model.room.width), Self.widthRange.lowerBound) }
    /// Tallest the chat area can be without the card running off the screen.
    private var chatRoom: CGFloat { max(model.room.height - model.cardOverhead, Self.heightRange.lowerBound) }
    private var chatMaxHeight: CGFloat { min(model.customHeight ?? autoMaxHeight, chatRoom) }
    /// Automatic: as tall as the conversation, up to a limit. Resized by you: exactly the height you chose (it scrolls).
    private var chatFrameHeight: CGFloat { model.customHeight.map { min($0, chatRoom) } ?? min(max(chatHeight, 24), chatMaxHeight) }
    @State private var chatHeight: CGFloat = 40
    @FocusState private var inputFocused: Bool

    private var label: String {
        switch model.state {
        case .listening: return "Listening"
        case .followUp: return "Your turn"
        case .transcribing: return "Transcribing"
        case .thinking: return "Thinking"
        case .speaking: return "Speaking"
        case .announcing: return "Update"
        case .confirming: return "Needs your OK"
        case .error: return "Problem"
        case .resuming: return "Welcome back"
        default: return "Jarvis"
        }
    }

    private var dotColor: Color {
        switch model.state {
        case .error: return Color(red: 0.95, green: 0.36, blue: 0.33)
        case .confirming: return Color(red: 0.96, green: 0.72, blue: 0.2)
        default: return Color(white: 0.75)
        }
    }

    private var showCard: Bool { model.cardVisible }

    private var draftIsEmpty: Bool { model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }

    private func send() {
        let t = model.draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        model.draft = ""
        onSend(t)
    }

    /// Type instead of talk: Return sends, Esc gives the keyboard back to the app you were using.
    private var inputRow: some View {
        HStack(spacing: 8) {
            TextField("Type to Jarvis…", text: $model.draft)
                .textFieldStyle(.plain)
                .font(.system(size: 13.5))
                .focused($inputFocused)
                .onSubmit { send() }
                .onExitCommand { model.draft = ""; inputFocused = false; onEscape() }
            Button { send() } label: {
                Image(systemName: "arrow.up.circle.fill").font(.system(size: 18))
                    .foregroundColor(draftIsEmpty ? Color.secondary.opacity(0.45) : Color.primary)
            }
            .buttonStyle(.plain)
            .disabled(draftIsEmpty)
            .help("Send (Return)")
        }
        .padding(.horizontal, 10).padding(.vertical, 6)
        .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Color.white.opacity(0.08)))
        .onChange(of: model.focusTick) { inputFocused = true }
    }

    /// The content is pinned to the orb's corner of the window, so the orb stays where it is even while the window is
    /// still catching up with a new card height.
    private var pin: Alignment {
        switch (model.alignBottom, model.alignRight) {
        case (true, true): return .bottomTrailing
        case (true, false): return .bottomLeading
        case (false, true): return .topTrailing
        case (false, false): return .topLeading
        }
    }

    private var card: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 7) {
                Circle().fill(dotColor).frame(width: 6, height: 6)
                Text(label).font(.system(size: 11.5, weight: .medium)).foregroundColor(.secondary)
                Spacer(minLength: 4)
                if model.messages.contains(where: { !$0.fromUser }) {
                    CopyButton(help: "Copy the conversation") { conversationText }
                }
                if model.state.isBusy {
                    Button { onClick("dismiss") } label: {
                        Text("Stop").font(.system(size: 11.5, weight: .medium))
                            .padding(.horizontal, 9).padding(.vertical, 3)
                            .background(RoundedRectangle(cornerRadius: 6).fill(Color.primary.opacity(0.1)))
                    }
                    .buttonStyle(.plain)
                    .help("Stop speaking and cancel (you can also say \"stop\")")
                }
                Button { onClick("hide") } label: {
                    Image(systemName: "xmark").font(.system(size: 9.5, weight: .semibold)).foregroundColor(.secondary)
                        .frame(width: 18, height: 18)
                }
                .buttonStyle(.plain)
                .help("Hide")
            }
            .contentShape(Rectangle())
            .gesture(dragGesture)  // drag the card by its header; the text below is for selecting
            if !model.messages.isEmpty {
                ScrollViewReader { proxy in
                    ScrollView(.vertical, showsIndicators: chatHeight > chatFrameHeight) {
                        VStack(alignment: .leading, spacing: 10) {
                            ForEach(model.messages) { line in
                                if line.fromUser {
                                    HStack {
                                        Spacer(minLength: 40)
                                        Text(line.text)
                                            .font(.system(size: 13))
                                            .foregroundColor(.primary)
                                            .textSelection(.enabled)
                                            .padding(.horizontal, 10).padding(.vertical, 6)
                                            .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Color.primary.opacity(0.12)))
                                            .fixedSize(horizontal: false, vertical: true)
                                    }
                                } else {
                                    ReplyView(line: line, conversation: { conversationText }, onCardAction: onCardAction)
                                }
                            }
                            Color.clear.frame(height: 1).id("bottom")
                        }
                        .background(GeometryReader { g in Color.clear.preference(key: HeightKey.self, value: g.size.height) })
                    }
                    .frame(height: chatFrameHeight)
                    .onPreferenceChange(HeightKey.self) { chatHeight = $0; model.autoChatHeight = min(max($0, 24), chatMaxHeight) }
                    .onChange(of: model.messages) { withAnimation { proxy.scrollTo("bottom", anchor: .bottom) } }
                }
            }
            if !model.notice.isEmpty {
                Text(model.notice)
                    .textSelection(.enabled)
                    .font(.system(size: 13.5))
                    .foregroundColor(.primary)
                    .lineSpacing(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if model.showQueue && !model.queue.isEmpty {
                VStack(alignment: .leading, spacing: 5) {
                    ForEach(model.queue.prefix(5)) { item in
                        Text(item.title).font(.system(size: 13)).foregroundColor(.primary).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            inputRow
        }
        .padding(.horizontal, 14).padding(.vertical, 11)
        .frame(width: cardWidth, alignment: .leading)
        .background(GeometryReader { g in
            Color.clear.onAppear { noteOverhead(g.size.height) }.onChange(of: g.size.height) { noteOverhead($1) }
        })
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(.regularMaterial)
                .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.black.opacity(0.32)))
                .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Color.white.opacity(0.1), lineWidth: 0.5))
                .shadow(color: .black.opacity(0.3), radius: 10, y: 3)
        )
        // Fully transparent pixels let clicks fall through to the window below. This all-but-invisible margin keeps the
        // rounded corners and the band just outside the edges clickable, so the card can be grabbed there to resize it.
        .background(Rectangle().fill(Color.black.opacity(0.01)).padding(-6))
        .environment(\.colorScheme, .dark)
    }

    private func noteOverhead(_ cardHeight: CGFloat) {
        model.cardOverhead = max(60, cardHeight - (model.messages.isEmpty ? 0 : chatFrameHeight))
    }

    private var conversationText: String {
        model.messages.map { $0.fromUser ? "You: \($0.text)" : "Jarvis: \(Self.plain($0))" }.joined(separator: "\n\n")
    }

    static func plain(_ line: ChatLine) -> String {
        ([line.text, line.rich ?? ""] + line.cards.map(\.plainText)).filter { !$0.isEmpty }.joined(separator: "\n\n")
    }

    static func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    private var orbView: some View {
        ZStack {
            StatusOrb(state: model.state, muted: model.muted)
            if model.debugMarker { Circle().fill(Color(red: 1, green: 0, blue: 1)).frame(width: 14, height: 14) }
            if !model.queue.isEmpty {
                Text("\(model.queue.count)")
                    .font(.system(size: 10, weight: .semibold)).foregroundColor(.white)
                    .frame(minWidth: 16, minHeight: 16).background(Circle().fill(Color.red))
                    .offset(x: 16, y: -16)
            }
        }
        .contentShape(Circle())
        .gesture(dragGesture)
        .onTapGesture { onClick("toggle_card") }  // always the chat; anything waiting in the queue is listed in it
        .contextMenu {
            Button("Clear conversation") { onClick("clear") }
            Button(model.muted ? "Turn Jarvis on" : "Turn Jarvis off") { onClick("mute_toggle") }
            Button("Pause for 1 hour") { onClick("pause_hour") }
            Button("Stop everything") { onClick("dismiss") }
            Button("Brief me") { onClick("brief_now") }
            Button("Type to Jarvis") { onClick("type") }
            Divider()
            Button("Move to top left") { onClick("corner:tl") }
            Button("Move to top right") { onClick("corner:tr") }
            Button("Move to bottom left") { onClick("corner:bl") }
            Button("Move to bottom right") { onClick("corner:br") }
        }
    }

    private var dragGesture: some Gesture {
        DragGesture(minimumDistance: 3, coordinateSpace: .global)
            .onChanged { _ in onDrag(true) }
            .onEnded { _ in onDrag(false) }
    }

    var body: some View {
        VStack(alignment: model.alignRight ? .trailing : .leading, spacing: 4) {
            if model.alignBottom {
                if showCard { card.transition(.opacity) }
                orbView
            } else {
                orbView
                if showCard { card.transition(.opacity) }
            }
        }
        .padding(10)
        .fixedSize()
        .background(GeometryReader { g in Color.clear.preference(key: SizeKey.self, value: g.size) })
        .onPreferenceChange(SizeKey.self) { onSize($0) }
        .animation(.easeInOut(duration: 0.15), value: showCard)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: pin)
    }
}

/// The panel never becomes the key window, so without this the first click on a button would be swallowed.
final class FirstClickHostingView: NSHostingView<OrbView> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override var mouseDownCanMoveWindow: Bool { false }
}

// MARK: - Panel

/// Floating HUD. It only appears while you are talking to Jarvis or it is talking to you, then fades away.
/// Drag it anywhere; it remembers where you left it and the card grows toward the middle of the screen.
final class OrbPanel: NSPanel {
    let model = OrbModel()
    var onClick: ((String) -> Void)?
    var onText: ((String) -> Void)?
    var onCardAction: ((CardAction) -> Void)?

    private static let pad: CGFloat = 10
    private static let orbHalf: CGFloat = 24  // half of the 48pt orb box
    private var contentSize = CGSize(width: 68, height: 68)
    private var orbCenter = NSPoint.zero
    private var dragStartMouse: NSPoint?
    private var dragStartCenter = NSPoint.zero
    private var clearWork: DispatchWorkItem?
    private var hideWork: DispatchWorkItem?
    private var flashUntil = Date.distantPast
    private var lingerUntil = Date.distantPast  // the conversation stays up for a while after the last activity
    private static let lingerSeconds = 30.0
    private var host: FirstClickHostingView!
    private var bag = Set<AnyCancellable>()

    /// Where the orb keeps its settings. The self-test swaps in a scratch suite so it never touches your real ones.
    static var store = UserDefaults.standard
    private static let pinKey = "orbPinnedV3"

    /// The orb stays on screen unless you turn "Keep orb on screen" off in the menu bar icon.
    var pinned: Bool {
        get { Self.store.object(forKey: Self.pinKey) as? Bool ?? true }
        set { Self.store.set(newValue, forKey: Self.pinKey); refreshVisibility() }
    }

    init() {
        super.init(contentRect: NSRect(x: 0, y: 0, width: 68, height: 68),
                   styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        isFloatingPanel = true
        level = .floating
        backgroundColor = .clear
        isOpaque = false
        hasShadow = false
        hidesOnDeactivate = false
        becomesKeyOnlyIfNeeded = true  // only clicking the type box takes the keyboard; the orb and buttons never steal it
        isMovableByWindowBackground = false
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        alphaValue = 0

        let view = OrbView(
            model: model,
            onClick: { [weak self] a in self?.handle(a) },
            onSize: { [weak self] s in DispatchQueue.main.async { self?.resize(to: s) } },
            onDrag: { [weak self] dragging in self?.dragging(dragging) },
            onSend: { [weak self] t in self?.sendText(t) },
            onCardAction: { [weak self] a in self?.perform(a) },
            onEscape: { [weak self] in self?.endTyping() }
        )
        let host = FirstClickHostingView(rootView: view)
        host.frame = NSRect(x: 0, y: 0, width: 68, height: 68)
        host.autoresizingMask = [.width, .height]
        host.sizingOptions = [.intrinsicContentSize]  // report the ideal size; we place the window ourselves, anchored on the orb
        contentView = host
        acceptsMouseMovedEvents = true
        host.addTrackingArea(NSTrackingArea(rect: .zero, options: [.mouseMoved, .activeAlways, .inVisibleRect], owner: host, userInfo: nil))
        self.host = host

        orbCenter = Self.savedCenter() ?? Self.defaultCenter(.br)
        updateAlignment()
        relayout()
        // Re-measure shortly after every model change (the preference callback alone proved unreliable).
        model.objectWillChange.sink { [weak self] _ in
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.06) { self?.measure() }
        }.store(in: &bag)
    }

    /// Where the orb actually is on screen (for the self-test: it must not move when the card opens, grows or closes).
    var orbScreenCenter: NSPoint {
        let reach = Self.pad + Self.orbHalf
        return NSPoint(x: model.alignRight ? frame.maxX - reach : frame.minX + reach,
                       y: model.alignBottom ? frame.minY + reach : frame.maxY - reach)
    }
    var orbAnchor: NSPoint { orbCenter }

    /// Finds the self-test marker in a rendering of the panel and returns its screen position (nil if it is not drawn).
    func orbPixelCenter() -> NSPoint? {
        host.layoutSubtreeIfNeeded()
        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return nil }
        host.cacheDisplay(in: host.bounds, to: rep)
        let scale = Double(rep.pixelsWide) / Double(max(1, host.bounds.width))
        var sx = 0.0, sy = 0.0, n = 0.0
        for y in 0..<rep.pixelsHigh {
            for x in 0..<rep.pixelsWide {
                guard let c = rep.colorAt(x: x, y: y)?.usingColorSpace(.deviceRGB) else { continue }
                if c.redComponent > 0.78 && c.blueComponent > 0.78 && c.greenComponent < 0.4 { sx += Double(x); sy += Double(y); n += 1 }
            }
        }
        guard n > 0 else { return nil }
        return NSPoint(x: frame.minX + sx / n / scale, y: frame.maxY - sy / n / scale)
    }
    func place(orbAt p: NSPoint) { orbCenter = p; updateAlignment(); relayout() }

    private func measure() {
        host.layoutSubtreeIfNeeded()
        let fit = host.fittingSize
        resize(to: fit)
    }

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }

    /// Clicking into the card (not the orb) takes the keyboard, so text can be selected and copied with Cmd+C.
    override func sendEvent(_ event: NSEvent) {
        if handleResize(event) { return }
        if event.type == .leftMouseDown, !isKeyWindow {
            let p = convertPoint(toScreen: event.locationInWindow)
            let o = orbScreenCenter
            if hypot(p.x - o.x, p.y - o.y) > Self.orbHalf + 4 { makeKey() }
        }
        super.sendEvent(event)
        if event.type == .mouseMoved { updateResizeCursor(at: convertPoint(toScreen: event.locationInWindow)) }
    }

    // MARK: resizing: grab the card's outer side edge, its outer top/bottom edge, or the corner between them.
    // (The two edges next to the orb stay put: moving them would move the orb.)

    private struct ResizeDrag { var mouse: NSPoint; var width: CGFloat; var height: CGFloat; var horizontal: Bool; var vertical: Bool }
    private var resizeDrag: ResizeDrag?
    private var showingResizeCursor = false

    /// The card's rectangle on screen (nil while it is not showing).
    private var cardScreenRect: NSRect? {
        guard isVisible, model.cardVisible, frame.width > 2 * Self.pad + Self.orbHalf * 2 + 1 else { return nil }
        let strip = Self.orbHalf * 2 + 4  // the orb and the gap above/below it
        let minY = frame.minY + Self.pad + (model.alignBottom ? strip : 0), maxY = frame.maxY - Self.pad - (model.alignBottom ? 0 : strip)
        guard maxY - minY > 20 else { return nil }
        return NSRect(x: frame.minX + Self.pad, y: minY, width: frame.width - 2 * Self.pad, height: maxY - minY)
    }

    /// Which way the pointer would resize the card from here: (width, height), or nil when it is not on a resizable edge.
    private func resizeZone(at p: NSPoint) -> (h: Bool, v: Bool)? {
        guard let c = cardScreenRect else { return nil }
        let inside: CGFloat = 8, outside: CGFloat = 5  // the band straddles the edge, mostly on the card
        func near(_ v: CGFloat, _ edge: CGFloat, _ outward: CGFloat) -> Bool { let d = (v - edge) * outward; return d <= outside && d >= -inside }
        guard p.x >= c.minX - outside, p.x <= c.maxX + outside, p.y >= c.minY - outside, p.y <= c.maxY + outside else { return nil }
        let h = model.alignRight ? near(p.x, c.minX, -1) : near(p.x, c.maxX, 1)
        let v = !model.messages.isEmpty && (model.alignBottom ? near(p.y, c.maxY, 1) : near(p.y, c.minY, -1))
        return h || v ? (h, v) : nil
    }

    private func cursor(for z: (h: Bool, v: Bool)) -> NSCursor {
        if z.h && z.v {
            if #available(macOS 15.0, *) {
                let pos: NSCursor.FrameResizePosition = model.alignBottom ? (model.alignRight ? .topLeft : .topRight) : (model.alignRight ? .bottomLeft : .bottomRight)
                return NSCursor.frameResize(position: pos, directions: .all)
            }
            return .crosshair
        }
        return z.h ? .resizeLeftRight : .resizeUpDown
    }

    /// Runs after SwiftUI has handled the move, so the resize cursor is not immediately replaced by the arrow.
    private func updateResizeCursor(at p: NSPoint) {
        if let d = resizeDrag { cursor(for: (d.horizontal, d.vertical)).set(); return }
        if let z = resizeZone(at: p) { cursor(for: z).set(); showingResizeCursor = true }
        else if showingResizeCursor { NSCursor.arrow.set(); showingResizeCursor = false }
    }

    private func handleResize(_ event: NSEvent) -> Bool {
        switch event.type {
        case .leftMouseDown:
            let p = convertPoint(toScreen: event.locationInWindow)
            guard let z = resizeZone(at: p), let c = cardScreenRect else { return false }
            if event.clickCount == 2 { resizeDrag = nil; model.customWidth = nil; model.customHeight = nil; return true }
            resizeDrag = ResizeDrag(mouse: NSEvent.mouseLocation, width: c.width,
                                    height: model.customHeight ?? model.autoChatHeight, horizontal: z.h, vertical: z.v)
            cursor(for: z).set()
            return true
        case .leftMouseDragged:
            guard let d = resizeDrag else { return false }
            let m = NSEvent.mouseLocation  // screen position: the window itself changes size under the pointer
            if d.horizontal {
                let dx = model.alignRight ? d.mouse.x - m.x : m.x - d.mouse.x  // the card grows away from the orb
                let hi = min(OrbView.widthRange.upperBound, model.room.width)
                model.customWidth = min(max(d.width + dx, OrbView.widthRange.lowerBound), hi)
            }
            if d.vertical {
                let dy = model.alignBottom ? m.y - d.mouse.y : d.mouse.y - m.y
                let hi = max(OrbView.heightRange.lowerBound, min(OrbView.heightRange.upperBound, model.room.height - model.cardOverhead))
                model.customHeight = min(max(d.height + dy, OrbView.heightRange.lowerBound), hi)
            }
            cursor(for: (d.horizontal, d.vertical)).set()
            return true
        case .leftMouseUp:
            guard resizeDrag != nil else { return false }
            resizeDrag = nil
            updateResizeCursor(at: NSEvent.mouseLocation)
            return true
        default:
            return false
        }
    }

    /// Jarvis has no menu bar menu, so the standard editing shortcuts are routed here.
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if flags == .command, let key = event.charactersIgnoringModifiers?.lowercased() {
            let action: Selector? = switch key {
            case "c": #selector(NSText.copy(_:))
            case "a": #selector(NSText.selectAll(_:))
            case "v": #selector(NSText.paste(_:))
            case "x": #selector(NSText.cut(_:))
            default: nil
            }
            if let action, NSApp.sendAction(action, to: nil, from: self) { return true }
        }
        return super.performKeyEquivalent(with: event)
    }

    override func becomeKey() { super.becomeKey(); model.typing = true; refreshVisibility() }
    override func resignKey() {
        super.resignKey()
        model.typing = false
        model.draft = ""
        touch()
        refreshVisibility()
    }

    // MARK: stepping aside

    private var mouseMonitor: Any?
    private var appObserver: NSObjectProtocol?

    /// Click on another app and Jarvis drops behind its windows instead of floating over them. Anything new from Jarvis
    /// (you start talking, it answers a fresh question, asks you something, or you open the type box) brings it back on top.
    func startWatchingOtherApps() {
        guard mouseMonitor == nil else { return }
        mouseMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
            DispatchQueue.main.async { self?.stepAside() }
        }
        appObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main) { [weak self] n in
            let app = n.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication
            if app?.processIdentifier != ProcessInfo.processInfo.processIdentifier { self?.stepAside() }
        }
        // A display was plugged, unplugged or rearranged: bring the orb back onto a screen that exists.
        screenObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { [weak self] _ in
            self?.keepOnScreen()
        }
        // Switching desktops or entering a full-screen app: make sure the orb is drawn on the new one.
        spaceObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.activeSpaceDidChangeNotification, object: nil, queue: .main) { [weak self] _ in
            guard let self, self.wantsVisible, !self.model.ghost else { return }
            self.orderFrontRegardless()
        }
    }

    private var screenObserver: NSObjectProtocol?
    private var spaceObserver: NSObjectProtocol?

    func keepOnScreen() {
        if !NSScreen.screens.contains(where: { $0.frame.contains(orbCenter) }) {
            orbCenter = Self.defaultCenter(.br)
            saveCenter()
        }
        updateAlignment()
        relayout()
        if wantsVisible && !model.ghost { orderFrontRegardless() }
    }

    func stepAside() {
        // Never while Jarvis is listening, thinking, talking, asking you something or showing a message: only a finished
        // conversation (or the idle orb) drops behind the app you clicked.
        guard isVisible, !model.typing, !model.ghost, !model.state.isBusy, model.state != .error, model.notice.isEmpty else { return }
        model.ghost = true
        level = .normal
        orderBack(nil)
    }

    func comeBack() {
        guard model.ghost else { return }
        model.ghost = false
        level = .floating
        if wantsVisible { orderFrontRegardless() }
    }

    // MARK: typing

    /// Open the card and put the cursor in the type box (Option+Shift+Space, the menu, or the orb's context menu).
    func beginTyping() {
        comeBack()
        model.dismissed = false
        model.typing = true
        flash(seconds: 60)
        refreshVisibility()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) { [weak self] in
            guard let self else { return }
            self.orderFrontRegardless()
            self.makeKey()
            self.model.focusTick += 1
        }
    }

    func endTyping() {
        model.draft = ""
        model.typing = false
        giveKeyboardBack()
        refreshVisibility()
    }

    /// Hands the keyboard back to the app you were using, without the orb blinking.
    private func giveKeyboardBack() {
        let alpha = alphaValue
        orderOut(nil)
        if wantsVisible { orderFrontRegardless(); alphaValue = alpha }
    }

    /// A card button: links open in the browser and copies go to the clipboard right here; "say" is sent to the core,
    /// which treats it exactly like something you typed (so confirmations and safety checks still apply).
    func perform(_ a: CardAction) {
        guard a.isSafe else { return }
        switch a.kind {
        case .url:
            if let u = URL(string: a.value) { NSWorkspace.shared.open(u) }
        case .copy:
            OrbView.copy(a.value)
        case .say:
            onCardAction?(a)
        }
    }

    /// A card for the reply being given. It sits with that reply and survives the text growing sentence by sentence.
    func showCard(_ card: CardData) {
        model.dismissed = false
        comeBack()
        if model.messages.last == nil || model.messages.last!.fromUser {
            model.messages.append(ChatLine(fromUser: false, text: ""))
        }
        model.messages[model.messages.count - 1].cards.append(card)
        touch()
        refreshVisibility()
    }

    func sendText(_ t: String) {
        model.dismissed = false
        touch()
        onText?(t)
    }

    // MARK: geometry

    private enum Corner { case tl, tr, bl, br }

    private static func screen(containing p: NSPoint) -> NSScreen? {
        NSScreen.screens.first { $0.frame.contains(p) } ?? NSScreen.main
    }

    private static func defaultCenter(_ c: Corner) -> NSPoint {
        guard let f = NSScreen.main?.visibleFrame else { return NSPoint(x: 200, y: 200) }
        let inset = pad + orbHalf + 6
        switch c {
        case .tl: return NSPoint(x: f.minX + inset, y: f.maxY - inset)
        case .tr: return NSPoint(x: f.maxX - inset, y: f.maxY - inset)
        case .bl: return NSPoint(x: f.minX + inset, y: f.minY + inset)
        case .br: return NSPoint(x: f.maxX - inset, y: f.minY + inset)
        }
    }

    private static func savedCenter() -> NSPoint? {
        let d = store
        guard d.object(forKey: "orbCenterX") != nil else { return nil }
        let p = NSPoint(x: d.double(forKey: "orbCenterX"), y: d.double(forKey: "orbCenterY"))
        return NSScreen.screens.contains { $0.frame.contains(p) } ? p : nil
    }

    private func saveCenter() {
        Self.store.set(Double(orbCenter.x), forKey: "orbCenterX")
        Self.store.set(Double(orbCenter.y), forKey: "orbCenterY")
    }

    /// Card extends toward the middle of the screen so it never runs off an edge.
    private func updateAlignment() {
        guard let f = Self.screen(containing: orbCenter)?.visibleFrame else { return }
        model.alignRight = orbCenter.x > f.midX
        model.alignBottom = orbCenter.y < f.midY
        let reach = Self.pad + Self.orbHalf
        let windowW = model.alignRight ? orbCenter.x + reach - f.minX : f.maxX - (orbCenter.x - reach)
        let windowH = model.alignBottom ? f.maxY - (orbCenter.y - reach) : orbCenter.y + reach - f.minY
        let room = CGSize(width: floor(windowW - 2 * Self.pad), height: floor(windowH - 2 * Self.pad - Self.orbHalf * 2 - 4))
        if room != model.room { model.room = room }
    }

    private func relayout() {
        let w = max(contentSize.width, 68), h = max(contentSize.height, 68)
        let reach = Self.pad + Self.orbHalf
        var x = model.alignRight ? orbCenter.x + reach - w : orbCenter.x - reach
        var y = model.alignBottom ? orbCenter.y - reach : orbCenter.y + reach - h
        if let vis = Self.screen(containing: orbCenter)?.visibleFrame {  // never leave the screen
            x = min(max(x, vis.minX), max(vis.minX, vis.maxX - w))
            y = min(max(y, vis.minY), max(vis.minY, vis.maxY - h))
        }
        setFrame(NSRect(x: x, y: y, width: w, height: h), display: true)
    }

    private var shrinkWork: DispatchWorkItem?

    /// Growing is immediate; shrinking waits for the card to finish fading, so nothing is clipped mid-fade.
    private func resize(to s: CGSize) {
        guard s.width > 0, s.height > 0, s.width < 1200, s.height < 1200 else { return }
        shrinkWork?.cancel(); shrinkWork = nil
        if abs(s.width - contentSize.width) < 0.5 && abs(s.height - contentSize.height) < 0.5 { return }
        let shrinking = s.width < contentSize.width - 0.5 || s.height < contentSize.height - 0.5
        if shrinking && isVisible {
            let keep = CGSize(width: max(s.width, contentSize.width), height: max(s.height, contentSize.height))
            if abs(keep.width - contentSize.width) > 0.5 || abs(keep.height - contentSize.height) > 0.5 { contentSize = keep; relayout() }
            let work = DispatchWorkItem { [weak self] in self?.contentSize = s; self?.relayout() }
            shrinkWork = work
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.22, execute: work)
            return
        }
        contentSize = s
        relayout()
    }

    // MARK: dragging (uses the mouse position, not gesture deltas, because the window moves under the cursor)

    private func dragging(_ active: Bool) {
        if active {
            if dragStartMouse == nil { dragStartMouse = NSEvent.mouseLocation; dragStartCenter = orbCenter }
            guard let s = dragStartMouse else { return }
            let m = NSEvent.mouseLocation
            orbCenter = NSPoint(x: dragStartCenter.x + m.x - s.x, y: dragStartCenter.y + m.y - s.y)
            relayout()
        } else if dragStartMouse != nil {
            dragStartMouse = nil
            updateAlignment()
            relayout()
            saveCenter()
        }
    }

    private func move(to c: Corner) {
        orbCenter = Self.defaultCenter(c)
        updateAlignment()
        relayout()
        saveCenter()
        flash()
    }

    /// Show the orb for a few seconds even when idle (used after moving it).
    func flash(seconds: Double = 3) {
        flashUntil = Date().addingTimeInterval(seconds)
        refreshVisibility()
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds + 0.1) { [weak self] in self?.refreshVisibility() }
    }

    // MARK: visibility

    var wantsVisible: Bool {
        pinned || model.typing || (!model.dismissed && model.state.isBusy) || model.state == .error || !model.notice.isEmpty || model.showQueue
            || Date() < flashUntil || (!model.dismissed && !model.messages.isEmpty && Date() < lingerUntil)
    }

    private func refreshVisibility() {
        if wantsVisible {
            hideWork?.cancel(); hideWork = nil
            if !isVisible { alphaValue = 0; orderFrontRegardless() }
            if alphaValue < 1 { NSAnimationContext.runAnimationGroup { $0.duration = 0.18; animator().alphaValue = 1 } }
        } else if isVisible && hideWork == nil {
            let work = DispatchWorkItem { [weak self] in
                guard let self else { return }
                self.hideWork = nil
                if self.wantsVisible { return }
                NSAnimationContext.runAnimationGroup({ $0.duration = 0.35; self.animator().alphaValue = 0 },
                                                     completionHandler: { if !self.wantsVisible { self.orderOut(nil) } })
            }
            hideWork = work
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2, execute: work)
        }
    }

    func handle(_ action: String) {
        switch action {
        case "open_queue":
            DispatchQueue.main.async { self.model.showQueue.toggle(); self.refreshVisibility() }
        case "hide":
            comeBack(); model.dismissed = true; model.notice = ""; model.showQueue = false
            if isKeyWindow { giveKeyboardBack() }
            clearHistoryWhenGone()
            flashUntil = .distantPast; lingerUntil = .distantPast; clearWork?.cancel(); refreshVisibility()
        case "toggle_card":
            // Clicking the orb opens the chat (with the type box ready); clicking it again closes it.
            if model.cardVisible && !model.ghost {
                model.dismissed = true; lingerUntil = .distantPast; model.showQueue = false
                clearHistoryWhenGone()
                if isKeyWindow { giveKeyboardBack() }
            } else {
                comeBack()
                model.dismissed = false; if !model.messages.isEmpty { touch() }
                model.showQueue = !model.queue.isEmpty
                beginTyping()
            }
            refreshVisibility()
        case "show":
            comeBack(); model.dismissed = false; if !model.messages.isEmpty { touch() }; flash(seconds: 30); refreshVisibility()
        case "clear":
            model.messages = []; model.notice = ""; refreshVisibility()
        case "type": beginTyping()
        case "corner:tl": move(to: .tl)
        case "corner:tr": move(to: .tr)
        case "corner:bl": move(to: .bl)
        case "corner:br": move(to: .br)
        default: break
        }
        if !["hide", "clear", "open_queue", "toggle_card", "show", "type"].contains(action) && !action.hasPrefix("corner:") { onClick?(action) }
    }

    func showConversation() { handle("show") }
    func moveToCorner(_ name: String) { handle("corner:\(name)") }
    func resetPosition() { move(to: .br) }

    // MARK: model updates (main thread)

    private func touch() {
        lingerUntil = Date().addingTimeInterval(Self.lingerSeconds)
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.lingerSeconds + 0.2) { [weak self] in self?.refreshVisibility() }
    }

    /// Closing the conversation also empties it: reopening starts fresh. Waits for the fade so the text does not vanish mid-fade.
    private func clearHistoryWhenGone() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { [weak self] in
            guard let self, self.model.dismissed else { return }
            self.model.messages = []
            self.refreshVisibility()
        }
    }

    private func clearNotice() { clearWork?.cancel(); model.notice = "" }

    func apply(state name: String, caption: String?) {
        let new = OrbStateName(rawValue: name) ?? model.state
        let startsSomething = (new.isBusy && new != .followUp && !model.state.isBusy) || new == .listening || new == .confirming || new == .error
        if startsSomething { model.dismissed = false }  // you closed the box; new activity opens it again
        if new.isBusy || new == .error { comeBack() }  // anything Jarvis is doing is shown on top, never behind windows
        model.state = new
        clearWork?.cancel()
        if let caption {
            model.notice = caption
        } else if new != .confirming && new != .error {
            model.notice = ""
        }
        if new.isBusy || !model.messages.isEmpty { touch() }
        refreshVisibility()
    }

    /// Show a message with the orb for a few seconds regardless of state.
    func announce(_ text: String) {
        model.dismissed = false
        comeBack()
        flash(seconds: 8)
        model.notice = text
        clearWork?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.clearNotice(); self?.refreshVisibility() }
        clearWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 8, execute: work)
        refreshVisibility()
    }

    /// `final` is what you said; the other captions are Jarvis's reply growing sentence by sentence.
    func showCaption(_ text: String, final: Bool) {
        if final { model.dismissed = false }  // your next question brings the box back
        comeBack()  // and a reply is never hidden behind other windows
        if final {
            model.messages.append(ChatLine(fromUser: true, text: text))
            model.messages.append(ChatLine(fromUser: false, text: ""))
        } else if let last = model.messages.last, !last.fromUser {
            model.messages[model.messages.count - 1].text = text
        } else {
            model.messages.append(ChatLine(fromUser: false, text: text))
        }
        model.messages.removeAll { !$0.fromUser && $0.text.isEmpty && $0.rich == nil && $0.cards.isEmpty && $0.id != model.messages.last?.id }
        if model.messages.count > 8 { model.messages.removeFirst(model.messages.count - 8) }
        touch()
        refreshVisibility()
    }

    /// The formatted version of Jarvis's reply (code, working, tables, steps) arrives next to the spoken one.
    func showRich(_ markdown: String) {
        let text = markdown.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        model.dismissed = false
        comeBack()
        if let last = model.messages.last, !last.fromUser {
            model.messages[model.messages.count - 1].rich = text
        } else {
            model.messages.append(ChatLine(fromUser: false, text: "", rich: text))
        }
        touch()
        refreshVisibility()
    }

    func setQueue(_ items: [[String: Any]]) {
        model.queue = items.compactMap {
            guard let id = $0["id"] as? String, let t = $0["title"] as? String else { return nil }
            return QueueItem(id: id, title: t, priority: ($0["priority"] as? String) ?? "normal")
        }
        if model.queue.isEmpty { model.showQueue = false }
        refreshVisibility()
    }
}

/// A small copy icon that briefly turns into a tick.
struct CopyButton: View {
    let help: String
    let text: () -> String
    @State private var done = false

    var body: some View {
        Button {
            OrbView.copy(text())
            done = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { done = false }
        } label: {
            Image(systemName: done ? "checkmark" : "doc.on.doc")
                .font(.system(size: 10.5, weight: .medium))
                .foregroundColor(.secondary)
                .frame(width: 18, height: 18)
        }
        .buttonStyle(.plain)
        .help(help)
    }
}

/// One of Jarvis's replies: formatted, selectable, with a copy button that shows while the pointer is over it.
struct ReplyView: View {
    let line: ChatLine
    let conversation: () -> String
    var onCardAction: (CardAction) -> Void = { _ in }
    @State private var hovering = false

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            if !line.text.isEmpty {
                // Replies are written in Markdown: show bold, lists and headings, not the raw ** and -.
                RichView(blocks: RichMarkdown.parse(line.text), bodySize: 14)
            }
            if let rich = line.rich { RichView(blocks: RichMarkdown.parse(rich)) }
            ForEach(line.cards) { CardView(card: $0, onAction: onCardAction) }
        }
        .textSelection(.enabled)
        .padding(.trailing, 4)
        .overlay(alignment: .bottomTrailing) {
            if hovering && (!line.text.isEmpty || line.rich != nil || !line.cards.isEmpty) {
                CopyButton(help: "Copy this reply") { OrbView.plain(line) }
                    .offset(x: 4, y: 4)
            }
        }
        .onHover { hovering = $0 }
        .contextMenu {
            Button("Copy reply") { OrbView.copy(OrbView.plain(line)) }
            Button("Copy conversation") { OrbView.copy(conversation()) }
        }
    }
}
