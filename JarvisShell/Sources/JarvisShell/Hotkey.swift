import Carbon.HIToolbox
import Foundation

/// Global Option+Space via Carbon (works without Input Monitoring permission).
/// down -> "down", release -> "up", and a hold >= 1s while Jarvis is busy -> "long_press".
final class Hotkey {
    var onAction: ((String) -> Void)?
    /// Returns true when Jarvis is mid-conversation (a long press then means "cancel everything").
    var isBusy: () -> Bool = { false }

    let hotKeyID: UInt32
    private let keyCode: Int
    private let modifiers: Int
    private let longPressCancels: Bool
    init(id: UInt32 = 1, keyCode: Int = kVK_Space, modifiers: Int = optionKey, longPressCancels: Bool = true) {
        hotKeyID = id; self.keyCode = keyCode; self.modifiers = modifiers; self.longPressCancels = longPressCancels
    }

    /// Plain Space, held. Only registered while Jarvis is talking (see `setEnabled`), so Space types normally the rest of the
    /// time. Pressing it stops Jarvis and listens; letting go sends what you said. Holding it never cancels the conversation.
    static func talkOver() -> Hotkey { Hotkey(id: 4, keyCode: kVK_Space, modifiers: 0, longPressCancels: false) }

    /// Option+Shift+Space: open the type box.
    static func typing() -> Hotkey { Hotkey(id: 2, keyCode: kVK_Space, modifiers: optionKey | shiftKey) }

    /// Control+Option+Space, held: dictate into the front app.
    static func dictation() -> Hotkey { Hotkey(id: 3, keyCode: kVK_Space, modifiers: controlKey | optionKey) }

    private var ref: EventHotKeyRef?
    private var handler: EventHandlerRef?
    private var longPress: DispatchWorkItem?
    private var isDown = false
    private var longFired = false
    private(set) var registered = false
    private var unregisterAfterRelease = false

    /// Grab the key (or give it back). Giving it back while it is held waits for the release, so the "up" is never lost.
    func setEnabled(_ on: Bool) {
        if on {
            unregisterAfterRelease = false
            if !registered { register() }
        } else if registered {
            if isDown { unregisterAfterRelease = true } else { unregister() }
        }
    }

    func register() {
        guard !registered else { return }
        registered = true
        var specs = [
            EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed)),
            EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyReleased)),
        ]
        let me = Unmanaged.passUnretained(self).toOpaque()
        InstallEventHandler(GetApplicationEventTarget(), { _, event, userData in
            guard let event, let userData else { return noErr }
            let hk = Unmanaged<Hotkey>.fromOpaque(userData).takeUnretainedValue()
            var pressedID = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID), nil,
                              MemoryLayout<EventHotKeyID>.size, nil, &pressedID)
            // another Hotkey owns this one: report "not handled" so Carbon offers the event to the next handler
            guard pressedID.id == hk.hotKeyID else { return OSStatus(eventNotHandledErr) }
            switch GetEventKind(event) {
            case UInt32(kEventHotKeyPressed): hk.pressed()
            case UInt32(kEventHotKeyReleased): hk.released()
            default: break
            }
            return noErr
        }, specs.count, &specs, me, &handler)
        let id = EventHotKeyID(signature: OSType(0x4A52_5653), id: hotKeyID)  // 'JRVS'
        RegisterEventHotKey(UInt32(keyCode), UInt32(modifiers), id, GetApplicationEventTarget(), 0, &ref)
    }

    func unregister() {
        if let ref { UnregisterEventHotKey(ref) }
        if let handler { RemoveEventHandler(handler) }
        ref = nil
        handler = nil
        registered = false
        unregisterAfterRelease = false
    }

    private func pressed() {
        guard !isDown else { return }  // ignore key repeat
        isDown = true
        longFired = false
        let busy = isBusy()
        onAction?("down")
        longPress?.cancel()
        if busy && longPressCancels {
            let work = DispatchWorkItem { [weak self] in
                guard let self, self.isDown else { return }
                self.longFired = true
                self.onAction?("long_press")
            }
            longPress = work
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.0, execute: work)
        }
    }

    private func released() {
        guard isDown else { return }
        isDown = false
        longPress?.cancel()
        if !longFired { onAction?("up") }
        if unregisterAfterRelease { unregister() }
    }
}
