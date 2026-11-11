import AppKit
import JarvisShellCore
import SwiftUI

/// Asks the daemon about memory over the existing WebSocket and hands each answer back to whoever asked (matched by message id).
final class MemoryBridge {
    var send: (Control) -> Void = { _ in }
    private var pending: [String: ([String: Any]) -> Void] = [:]

    func request(_ op: String, _ args: [String: Any] = [:], completion: @escaping ([String: Any]) -> Void) {
        var data = args
        data["op"] = op
        let c = Control(MessageType.memoryRequest, data)
        pending[c.id] = completion
        send(c)
        DispatchQueue.main.asyncAfter(deadline: .now() + 8) { [weak self] in
            // No answer (daemon restarting or not running): say so rather than leave the window waiting.
            if let done = self?.pending.removeValue(forKey: c.id) {
                done(["ok": false, "error": "Jarvis didn't answer. Is it running?"])
            }
        }
    }

    func handle(_ c: Control) {
        pending.removeValue(forKey: c.id)?(c.data)
    }
}

final class MemoryModel: ObservableObject {
    @Published var groups: [MemoryGroup] = []
    @Published var query = ""
    @Published var error: String?
    @Published var loading = false
    @Published var editingID: String?
    @Published var draft = ""

    private let bridge: MemoryBridge
    init(bridge: MemoryBridge) { self.bridge = bridge }

    var total: Int { groups.reduce(0) { $0 + $1.items.count } }

    func refresh() {
        loading = true
        bridge.request("memory.list", ["query": query]) { [weak self] data in
            guard let self else { return }
            self.loading = false
            self.error = MemoryReply.error(from: data)
            if self.error == nil { self.groups = MemoryReply.groups(from: data) }
        }
    }

    func startEditing(_ item: MemoryItem) { editingID = item.id; draft = item.text }
    func cancelEditing() { editingID = nil; draft = "" }

    func saveEdit() {
        guard let id = editingID else { return }
        let text = draft
        bridge.request("memory.edit", ["id": id, "text": text]) { [weak self] data in
            guard let self else { return }
            self.error = MemoryReply.error(from: data)
            if self.error == nil { self.cancelEditing() }
            self.refresh()
        }
    }

    func forget(_ item: MemoryItem) {
        bridge.request("memory.forget", ["id": item.id]) { [weak self] data in
            self?.error = MemoryReply.error(from: data)
            self?.refresh()
        }
    }

    func forgetEverything() {
        bridge.request("memory.clear") { [weak self] data in
            self?.error = MemoryReply.error(from: data)
            self?.refresh()
        }
    }
}

struct MemoryView: View {
    @ObservedObject var model: MemoryModel
    @State private var toDelete: MemoryItem?
    @State private var confirmClear = false

    var body: some View {
        VStack(spacing: 0) {
            TextField("Search what Jarvis remembers", text: $model.query)
                .textFieldStyle(.roundedBorder)
                .padding(10)
                .onSubmit { model.refresh() }
                .onChange(of: model.query) { _, _ in model.refresh() }
            if let e = model.error {
                Text(e).font(.callout).foregroundStyle(.red).padding(.horizontal, 10).padding(.bottom, 6)
            }
            if model.groups.isEmpty && !model.loading {
                Spacer()
                Text(model.query.isEmpty ? "Jarvis doesn't remember anything about you yet." : "Nothing matches that.")
                    .foregroundStyle(.secondary)
                Spacer()
            } else {
                List {
                    ForEach(model.groups) { group in
                        Section(group.title) {
                            ForEach(group.items) { item in row(item) }
                        }
                    }
                }
            }
            Divider()
            HStack {
                Text("\(model.total) remembered").foregroundStyle(.secondary).font(.callout)
                Spacer()
                Button("Forget everything…", role: .destructive) { confirmClear = true }
                    .disabled(model.total == 0 && model.query.isEmpty)
            }
            .padding(10)
        }
        .frame(minWidth: 460, minHeight: 360)
        .onAppear { model.refresh() }
        .alert("Forget this?", isPresented: Binding(get: { toDelete != nil }, set: { if !$0 { toDelete = nil } })) {
            Button("Forget", role: .destructive) { if let i = toDelete { model.forget(i) }; toDelete = nil }
            Button("Cancel", role: .cancel) { toDelete = nil }
        } message: { Text(toDelete?.text ?? "") }
        .alert("Forget everything?", isPresented: $confirmClear) {
            Button("Forget everything", role: .destructive) { model.forgetEverything() }
            Button("Cancel", role: .cancel) {}
        } message: { Text("Jarvis will lose everything it remembers about you, including past conversations. This can't be undone.") }
    }

    @ViewBuilder private func row(_ item: MemoryItem) -> some View {
        if model.editingID == item.id {
            HStack {
                TextField("", text: $model.draft).textFieldStyle(.roundedBorder).onSubmit { model.saveEdit() }
                Button("Save") { model.saveEdit() }.keyboardShortcut(.defaultAction)
                Button("Cancel") { model.cancelEditing() }.keyboardShortcut(.cancelAction)
            }
        } else {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(item.text)
                    if !item.date.isEmpty { Text(item.date).font(.caption).foregroundStyle(.secondary) }
                }
                Spacer()
                Button { model.startEditing(item) } label: { Image(systemName: "pencil") }.buttonStyle(.borderless).help("Edit")
                Button { toDelete = item } label: { Image(systemName: "trash") }.buttonStyle(.borderless).help("Forget")
            }
        }
    }
}

final class MemoryWindowController {
    let bridge = MemoryBridge()
    private lazy var model = MemoryModel(bridge: bridge)
    private var window: NSWindow?

    func show() {
        if window == nil {
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 520, height: 520),
                             styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
            w.title = "What Jarvis remembers"
            w.isReleasedWhenClosed = false
            w.contentView = NSHostingView(rootView: MemoryView(model: model))
            w.center()
            window = w
        } else {
            model.refresh()
        }
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }
}
