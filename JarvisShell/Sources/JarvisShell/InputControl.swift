import AppKit
import ApplicationServices
import Carbon.HIToolbox
import Foundation
import JarvisShellCore

/// Synthetic mouse/keyboard for computer use. Guard rails live here as well as in the core:
/// no typing into password fields, no Return/Enter unless explicitly allowed, and a mouse-movement
/// kill switch while a computer-use session is active.
final class InputControl {
    var onKill: (() -> Void)?
    private var session = false
    private var monitor: Any?
    private var lastSynthetic = Date.distantPast
    private var expected = CGPoint.zero
    private var moved: CGFloat = 0

    // MARK: session / kill switch

    func beginSession() {
        session = true
        moved = 0
        expected = NSEvent.mouseLocation
        monitor = NSEvent.addGlobalMonitorForEvents(matching: [.mouseMoved, .leftMouseDragged]) { [weak self] ev in
            self?.userMoved(ev)
        }
    }

    func endSession() {
        session = false
        if let monitor { NSEvent.removeMonitor(monitor) }
        monitor = nil
    }

    private func userMoved(_ ev: NSEvent) {
        guard session else { return }
        if Date().timeIntervalSince(lastSynthetic) < 0.25 { expected = NSEvent.mouseLocation; moved = 0; return }
        let p = NSEvent.mouseLocation
        moved += hypot(ev.deltaX, ev.deltaY)
        if moved >= 50 { moved = 0; onKill?() }
        expected = p
    }

    private func markSynthetic() { lastSynthetic = Date() }

    // MARK: dispatch

    func handle(op: String, args: [String: Any]) throws -> [String: Any] {
        switch op {
        case "begin": beginSession(); return ["ok": true]
        case "end": endSession(); return ["ok": true]
        case "move": try move(args); return ["ok": true]
        case "click": try click(args); return ["ok": true]
        case "scroll": scroll(args); return ["ok": true]
        case "drag": try drag(args); return ["ok": true]
        case "type": try type(args); return ["ok": true]
        case "key": try key(args); return ["ok": true]
        case "cursor":
            let p = CGEvent(source: nil)?.location ?? .zero
            return ["x": p.x, "y": p.y]
        default: throw AXFailure(message: "unknown input op \(op)")
        }
    }

    private func point(_ a: [String: Any]) throws -> CGPoint {
        func num(_ k: String) -> Double? { (a[k] as? Double) ?? (a[k] as? Int).map(Double.init) }
        guard let x = num("x"), let y = num("y") else { throw AXFailure(message: "x and y are required") }
        return CGPoint(x: x, y: y)
    }

    private func post(_ e: CGEvent?) {
        markSynthetic()
        e?.post(tap: .cghidEventTap)
    }

    private func move(_ a: [String: Any]) throws {
        let p = try point(a)
        post(CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: p, mouseButton: .left))
    }

    private func click(_ a: [String: Any]) throws {
        let p = try point(a)
        if let label = a["label"] as? String, ForbiddenLabels.isForbidden(label) {
            throw AXFailure(message: "refusing to click '\(label)'")
        }
        let right = (a["button"] as? String) == "right"
        let count = max(1, min(3, (a["count"] as? Int) ?? 1))
        let down: CGEventType = right ? .rightMouseDown : .leftMouseDown
        let up: CGEventType = right ? .rightMouseUp : .leftMouseUp
        let btn: CGMouseButton = right ? .right : .left
        post(CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: p, mouseButton: btn))
        for i in 1...count {
            let d = CGEvent(mouseEventSource: nil, mouseType: down, mouseCursorPosition: p, mouseButton: btn)
            d?.setIntegerValueField(.mouseEventClickState, value: Int64(i))
            post(d)
            let u = CGEvent(mouseEventSource: nil, mouseType: up, mouseCursorPosition: p, mouseButton: btn)
            u?.setIntegerValueField(.mouseEventClickState, value: Int64(i))
            post(u)
        }
    }

    private func drag(_ a: [String: Any]) throws {
        let from = try point(["x": a["x"] as Any, "y": a["y"] as Any])
        let to = try point(["x": a["to_x"] as Any, "y": a["to_y"] as Any])
        post(CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: from, mouseButton: .left))
        for i in 1...10 {
            let t = CGFloat(i) / 10
            let p = CGPoint(x: from.x + (to.x - from.x) * t, y: from.y + (to.y - from.y) * t)
            post(CGEvent(mouseEventSource: nil, mouseType: .leftMouseDragged, mouseCursorPosition: p, mouseButton: .left))
            usleep(8_000)
        }
        post(CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: to, mouseButton: .left))
    }

    private func scroll(_ a: [String: Any]) {
        let dy = Int32((a["dy"] as? Int) ?? 0), dx = Int32((a["dx"] as? Int) ?? 0)
        post(CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 2, wheel1: dy, wheel2: dx, wheel3: 0))
    }

    // MARK: keyboard

    private func focusedIsSecure() -> Bool {
        let sys = AXUIElementCreateSystemWide()
        var f: CFTypeRef?
        guard AXUIElementCopyAttributeValue(sys, kAXFocusedUIElementAttribute as CFString, &f) == .success,
              let el = f else { return false }
        var r: CFTypeRef?
        _ = AXUIElementCopyAttributeValue(el as! AXUIElement, kAXRoleAttribute as CFString, &r)
        var sr: CFTypeRef?
        _ = AXUIElementCopyAttributeValue(el as! AXUIElement, kAXSubroleAttribute as CFString, &sr)
        return (r as? String) == "AXSecureTextField" || (sr as? String) == "AXSecureTextField"
    }

    private func type(_ a: [String: Any]) throws {
        guard let text = a["text"] as? String else { throw AXFailure(message: "text is required") }
        if focusedIsSecure() { throw AXFailure(message: "refusing to type into a password field") }
        let allowReturn = (a["allow_return"] as? Bool) ?? false
        if !allowReturn && (text.contains("\n") || text.contains("\r")) {
            throw AXFailure(message: "refusing to emit Return while typing; Jarvis never sends anything")
        }
        for chunk in text.map(String.init) {
            let utf16 = Array(chunk.utf16)
            let d = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: true)
            d?.keyboardSetUnicodeString(stringLength: utf16.count, unicodeString: utf16)
            post(d)
            let u = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: false)
            u?.keyboardSetUnicodeString(stringLength: utf16.count, unicodeString: utf16)
            post(u)
            usleep(6_000)
        }
    }

    private static let keyCodes: [String: Int] = [
        "tab": kVK_Tab, "escape": kVK_Escape, "esc": kVK_Escape, "space": kVK_Space, "delete": kVK_Delete,
        "backspace": kVK_Delete, "left": kVK_LeftArrow, "right": kVK_RightArrow, "up": kVK_UpArrow,
        "down": kVK_DownArrow, "home": kVK_Home, "end": kVK_End, "pageup": kVK_PageUp, "pagedown": kVK_PageDown,
        "return": kVK_Return, "enter": kVK_Return,
        "a": kVK_ANSI_A, "c": kVK_ANSI_C, "f": kVK_ANSI_F, "l": kVK_ANSI_L, "n": kVK_ANSI_N, "t": kVK_ANSI_T,
        "v": kVK_ANSI_V, "w": kVK_ANSI_W, "x": kVK_ANSI_X, "z": kVK_ANSI_Z, "r": kVK_ANSI_R, "s": kVK_ANSI_S,
        "q": kVK_ANSI_Q, "k": kVK_ANSI_K, "o": kVK_ANSI_O, "p": kVK_ANSI_P, "1": kVK_ANSI_1, "2": kVK_ANSI_2,
        "3": kVK_ANSI_3, "[": kVK_ANSI_LeftBracket, "]": kVK_ANSI_RightBracket, ",": kVK_ANSI_Comma,
    ]

    private func key(_ a: [String: Any]) throws {
        guard let name = (a["key"] as? String)?.lowercased(), let code = Self.keyCodes[name] else {
            throw AXFailure(message: "unsupported key")
        }
        if (name == "return" || name == "enter") && !((a["allow_return"] as? Bool) ?? false) {
            throw AXFailure(message: "refusing to press Return; Jarvis never sends anything")
        }
        var flags: CGEventFlags = []
        for m in (a["modifiers"] as? [String]) ?? [] {
            switch m.lowercased() {
            case "cmd", "command": flags.insert(.maskCommand)
            case "shift": flags.insert(.maskShift)
            case "option", "alt": flags.insert(.maskAlternate)
            case "ctrl", "control": flags.insert(.maskControl)
            default: break
            }
        }
        // Cmd+Return is "send" in many mail apps; never allow it unless explicitly permitted.
        if flags.contains(.maskCommand) && (name == "return" || name == "enter") {
            throw AXFailure(message: "refusing Cmd+Return")
        }
        for down in [true, false] {
            let e = CGEvent(keyboardEventSource: nil, virtualKey: CGKeyCode(code), keyDown: down)
            e?.flags = flags
            post(e)
        }
    }
}
