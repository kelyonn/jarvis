import AppKit
import ApplicationServices
import Foundation
import JarvisShellCore

struct AXFailure: Error { let message: String }

/// Accessibility operations: window management, AX tree dump, element actions.
/// Element ids ("e12") are only valid until the next `tree` call.
final class AXWindows {
    private var elements: [String: AXUIElement] = [:]

    // MARK: dispatch

    func handle(op: String, args: [String: Any]) throws -> [String: Any] {
        guard AXIsProcessTrusted() else { throw AXFailure(message: "Accessibility permission is not granted") }
        switch op {
        case "frontmost": return frontmost()
        case "list_windows": return ["windows": try listWindows(app: args["app"] as? String)]
        case "focus": return try focus(app: try need(args, "app"))
        case "snap": return try snap(args)
        case "set_frame": return try setFrame(args)
        case "minimize": return try setBool(args, kAXMinimizedAttribute, true)
        case "unminimize": return try setBool(args, kAXMinimizedAttribute, false)
        case "fullscreen": return try setBool(args, "AXFullScreen", (args["on"] as? Bool) ?? true)
        case "tree": return try tree(args)
        case "press": return try press(args)
        case "set_value": return try setValue(args)
        case "set_number": return try setNumber(args)
        case "adjust": return try adjust(args)
        default: throw AXFailure(message: "unknown ax op \(op)")
        }
    }

    private func need(_ a: [String: Any], _ k: String) throws -> String {
        guard let v = a[k] as? String, !v.isEmpty else { throw AXFailure(message: "missing \(k)") }
        return v
    }

    // MARK: apps and windows

    private func runningApp(named name: String) -> NSRunningApplication? {
        let n = name.lowercased()
        let apps = NSWorkspace.shared.runningApplications.filter { $0.activationPolicy == .regular }
        return apps.first { $0.localizedName?.lowercased() == n }
            ?? apps.first { $0.bundleIdentifier?.lowercased() == n }
            ?? apps.first { ($0.localizedName ?? "").lowercased().contains(n) }
    }

    private func frontmost() -> [String: Any] {
        guard let app = NSWorkspace.shared.frontmostApplication else { return [:] }
        var out: [String: Any] = ["app": app.localizedName ?? "", "bundle_id": app.bundleIdentifier ?? ""]
        let el = AXUIElementCreateApplication(app.processIdentifier)
        if let w = attr(el, kAXFocusedWindowAttribute) as! AXUIElement?, let t = str(w, kAXTitleAttribute) {
            out["window"] = t
        }
        return out
    }

    private func windows(of app: NSRunningApplication) -> [AXUIElement] {
        let el = AXUIElementCreateApplication(app.processIdentifier)
        return (attr(el, kAXWindowsAttribute) as? [AXUIElement]) ?? []
    }

    private func listWindows(app: String?) throws -> [[String: Any]] {
        let apps: [NSRunningApplication]
        if let app {
            guard let a = runningApp(named: app) else { throw AXFailure(message: "\(app) is not running") }
            apps = [a]
        } else {
            apps = NSWorkspace.shared.runningApplications.filter { $0.activationPolicy == .regular }
        }
        var out: [[String: Any]] = []
        for a in apps {
            for (i, w) in windows(of: a).enumerated() {
                var d: [String: Any] = ["app": a.localizedName ?? "", "index": i,
                                        "title": str(w, kAXTitleAttribute) ?? ""]
                if let f = frame(of: w) {
                    d["x"] = f.minX; d["y"] = f.minY; d["w"] = f.width; d["h"] = f.height
                }
                d["minimized"] = (attr(w, kAXMinimizedAttribute) as? Bool) ?? false
                out.append(d)
            }
        }
        return out
    }

    private func window(_ args: [String: Any]) throws -> (NSRunningApplication, AXUIElement) {
        let name = try need(args, "app")
        guard let app = runningApp(named: name) else { throw AXFailure(message: "\(name) is not running") }
        let ws = windows(of: app)
        let idx = (args["index"] as? Int) ?? 0
        guard idx >= 0, idx < ws.count else { throw AXFailure(message: "\(name) has no window #\(idx)") }
        return (app, ws[idx])
    }

    private func focus(app name: String) throws -> [String: Any] {
        guard let app = runningApp(named: name) else {
            // Not running: launch it.
            let cfg = NSWorkspace.OpenConfiguration()
            cfg.activates = true
            if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: name)
                ?? Self.appURL(named: name) {
                NSWorkspace.shared.openApplication(at: url, configuration: cfg)
                return ["launched": true]
            }
            throw AXFailure(message: "\(name) is not installed")
        }
        app.activate(options: [.activateAllWindows])
        if let w = windows(of: app).first { AXUIElementPerformAction(w, kAXRaiseAction as CFString) }
        return ["focused": app.localizedName ?? name]
    }

    static func appURL(named name: String) -> URL? {
        let dirs = ["/Applications", "/System/Applications", "/System/Applications/Utilities",
                    NSHomeDirectory() + "/Applications"]
        for d in dirs {
            let u = URL(fileURLWithPath: d).appendingPathComponent(name + ".app")
            if FileManager.default.fileExists(atPath: u.path) { return u }
        }
        return nil
    }

    // MARK: geometry

    private func screenArea(containing rect: CGRect) -> CGRect {
        let primaryH = NSScreen.screens.first?.frame.height ?? 0
        let center = CGPoint(x: rect.midX, y: rect.midY)
        for s in NSScreen.screens {
            let tl = SnapLayout.toTopLeft(s.frame, primaryHeight: primaryH)
            if tl.contains(center) { return SnapLayout.toTopLeft(s.visibleFrame, primaryHeight: primaryH) }
        }
        let s = NSScreen.main ?? NSScreen.screens[0]
        return SnapLayout.toTopLeft(s.visibleFrame, primaryHeight: primaryH)
    }

    private func snap(_ args: [String: Any]) throws -> [String: Any] {
        let pos = try need(args, "position")
        let (app, w) = try window(args)
        let current = frame(of: w) ?? .zero
        guard let r = SnapLayout.rect(for: pos, in: screenArea(containing: current)) else {
            throw AXFailure(message: "unknown position \(pos)")
        }
        app.activate()
        try apply(r, to: w)
        return ["frame": [r.minX, r.minY, r.width, r.height]]
    }

    private func setFrame(_ args: [String: Any]) throws -> [String: Any] {
        let (_, w) = try window(args)
        let cur = frame(of: w) ?? .zero
        func n(_ k: String, _ d: CGFloat) -> CGFloat {
            if let v = args[k] as? Double { return CGFloat(v) }
            if let v = args[k] as? Int { return CGFloat(v) }
            return d
        }
        let r = CGRect(x: n("x", cur.minX), y: n("y", cur.minY), width: n("w", cur.width), height: n("h", cur.height))
        try apply(r, to: w)
        return ["frame": [r.minX, r.minY, r.width, r.height]]
    }

    private func apply(_ r: CGRect, to w: AXUIElement) throws {
        var origin = r.origin, size = r.size
        guard let ov = AXValueCreate(.cgPoint, &origin), let sv = AXValueCreate(.cgSize, &size) else {
            throw AXFailure(message: "bad geometry")
        }
        // Size, position, size again: some apps clamp size against the old origin.
        AXUIElementSetAttributeValue(w, kAXSizeAttribute as CFString, sv)
        let e = AXUIElementSetAttributeValue(w, kAXPositionAttribute as CFString, ov)
        AXUIElementSetAttributeValue(w, kAXSizeAttribute as CFString, sv)
        if e != .success { throw AXFailure(message: "could not move window (\(e.rawValue))") }
    }

    private func setBool(_ args: [String: Any], _ key: String, _ v: Bool) throws -> [String: Any] {
        let (_, w) = try window(args)
        let e = AXUIElementSetAttributeValue(w, key as CFString, v as CFBoolean)
        if e != .success { throw AXFailure(message: "could not change \(key) (\(e.rawValue))") }
        return ["ok": true]
    }

    // MARK: tree

    private func tree(_ args: [String: Any]) throws -> [String: Any] {
        let app: NSRunningApplication
        if let name = args["app"] as? String {
            guard let a = runningApp(named: name) else { throw AXFailure(message: "\(name) is not running") }
            app = a
        } else if let a = NSWorkspace.shared.frontmostApplication { app = a } else {
            throw AXFailure(message: "no frontmost app")
        }
        let root = AXUIElementCreateApplication(app.processIdentifier)
        let start = (attr(root, kAXFocusedWindowAttribute) as! AXUIElement?) ?? windows(of: app).first ?? root
        elements.removeAll()
        var counter = 0
        let maxDepth = (args["max_depth"] as? Int) ?? 7
        let maxNodes = (args["max_nodes"] as? Int) ?? 400

        func walk(_ el: AXUIElement, depth: Int) -> [String: Any]? {
            guard counter < maxNodes else { return nil }
            counter += 1
            let id = "e\(counter)"
            elements[id] = el
            var node: [String: Any] = ["id": id, "role": str(el, kAXRoleAttribute) ?? ""]
            if let t = str(el, kAXTitleAttribute), !t.isEmpty { node["title"] = t }
            if let d = str(el, kAXDescriptionAttribute), !d.isEmpty { node["desc"] = d }
            if let v = attr(el, kAXValueAttribute) {
                let role = node["role"] as? String
                if role == "AXSecureTextField" { node["value"] = "•••" }
                else if let s = v as? String, !s.isEmpty { node["value"] = String(s.prefix(200)) }
            }
            if let f = frame(of: el) { node["frame"] = [Int(f.minX), Int(f.minY), Int(f.width), Int(f.height)] }
            var acts: CFArray?
            if AXUIElementCopyActionNames(el, &acts) == .success, let a = acts as? [String], !a.isEmpty {
                node["actions"] = a.map { $0.replacingOccurrences(of: "AX", with: "") }
            }
            if depth < maxDepth, let kids = attr(el, kAXChildrenAttribute) as? [AXUIElement] {
                let sub = kids.compactMap { walk($0, depth: depth + 1) }
                if !sub.isEmpty { node["children"] = sub }
            }
            return node
        }
        let tree = walk(start, depth: 0) ?? [:]
        return ["app": app.localizedName ?? "", "tree": tree, "count": counter]
    }

    private func element(_ args: [String: Any]) throws -> AXUIElement {
        let id = try need(args, "element_id")
        guard let el = elements[id] else { throw AXFailure(message: "unknown element \(id); take a new tree first") }
        return el
    }

    private func press(_ args: [String: Any]) throws -> [String: Any] {
        let el = try element(args)
        let label = [str(el, kAXTitleAttribute), str(el, kAXDescriptionAttribute), str(el, "AXIdentifier")]
            .compactMap { $0 }.joined(separator: " ")
        if ForbiddenLabels.isForbidden(label) {
            throw AXFailure(message: "refusing to press '\(label)': send/post/buy/delete style controls are blocked")
        }
        let e = AXUIElementPerformAction(el, kAXPressAction as CFString)
        if e != .success { throw AXFailure(message: "press failed (\(e.rawValue))") }
        return ["pressed": label]
    }

    /// Sliders and steppers (Clock's timer picker): set a numeric value directly.
    private func setNumber(_ args: [String: Any]) throws -> [String: Any] {
        let el = try element(args)
        guard let v = (args["value"] as? NSNumber)?.doubleValue else { throw AXFailure(message: "missing numeric 'value'") }
        let e = AXUIElementSetAttributeValue(el, kAXValueAttribute as CFString, NSNumber(value: v))
        if e != .success { throw AXFailure(message: "set_number failed (\(e.rawValue))") }
        return ["ok": true]
    }

    /// Step a slider or stepper up or down (the accessibility Increment / Decrement actions).
    private func adjust(_ args: [String: Any]) throws -> [String: Any] {
        let el = try element(args)
        let up = (args["direction"] as? String ?? "increment") != "decrement"
        let times = min(max((args["times"] as? NSNumber)?.intValue ?? 1, 1), 120)
        for _ in 0..<times {
            let e = AXUIElementPerformAction(el, (up ? "AXIncrement" : "AXDecrement") as CFString)
            if e != .success { throw AXFailure(message: "adjust failed (\(e.rawValue))") }
        }
        return ["ok": true, "steps": times]
    }

    private func setValue(_ args: [String: Any]) throws -> [String: Any] {
        let el = try element(args)
        if str(el, kAXRoleAttribute) == "AXSecureTextField" || str(el, kAXSubroleAttribute) == "AXSecureTextField" {
            throw AXFailure(message: "refusing to write into a password field")
        }
        let text = try need(args, "text")
        let e = AXUIElementSetAttributeValue(el, kAXValueAttribute as CFString, text as CFString)
        if e != .success { throw AXFailure(message: "set_value failed (\(e.rawValue))") }
        return ["ok": true]
    }

    // MARK: AX helpers

    private func attr(_ el: AXUIElement, _ name: String) -> AnyObject? {
        var v: CFTypeRef?
        return AXUIElementCopyAttributeValue(el, name as CFString, &v) == .success ? (v as AnyObject?) : nil
    }

    private func str(_ el: AXUIElement, _ name: String) -> String? { attr(el, name) as? String }

    private func frame(of el: AXUIElement) -> CGRect? {
        var p = CGPoint.zero, s = CGSize.zero
        guard let pv = attr(el, kAXPositionAttribute), let sv = attr(el, kAXSizeAttribute),
              AXValueGetValue(pv as! AXValue, .cgPoint, &p), AXValueGetValue(sv as! AXValue, .cgSize, &s)
        else { return nil }
        return CGRect(origin: p, size: s)
    }
}
