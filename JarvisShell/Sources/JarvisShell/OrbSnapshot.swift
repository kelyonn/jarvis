import AppKit
import JarvisShellCore
import SwiftUI

/// Developer aid: `JARVIS_ORB_SNAPSHOT=/some/dir JarvisShell` renders the HUD in a few states to PNG files and exits.
enum OrbSnapshot {
    @MainActor static func write(to dir: String) {
        let cases: [(String, OrbStateName, String, String, String)] = [
            ("listening", .listening, "", "", ""),
            ("thinking", .thinking, "Play Mr. Brightside by The Killers on Spotify", "", ""),
            ("speaking", .speaking, "What is the weather like today?",
             "Right now it's a mild twenty-two degrees in Bengaluru, rising to twenty-nine this afternoon, with a sixty percent chance of rain after four, so an umbrella would not go amiss.", ""),
            ("confirming", .confirming, "Delete my downloads folder", "", "This will move 14 files to the Trash. Shall I proceed, sir?"),
        ]
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let chat = OrbModel()
        chat.state = .followUp
        chat.messages = [
            ChatLine(fromUser: true, text: "How many unread WhatsApp messages do I have?"),
            ChatLine(fromUser: false, text: "You have sixteen unread: nine from Naan, three from Deepika and three from Dhanyatha."),
            ChatLine(fromUser: true, text: "What did Naan say?"),
            ChatLine(fromUser: false, text: "Naan wrote at two fourteen pm: so I don't know how that works. Then at two sixteen: can you send me the notes before tonight? And finally, no rush though, whenever you get a minute."),
            ChatLine(fromUser: true, text: "Reply that I'll send them at eight"),
            ChatLine(fromUser: false, text: "Here's the draft for Naan, sir. \"I'll send them at eight tonight.\" Shall I put it in WhatsApp?"),
        ]
        // A real AppKit view renders scroll views, which ImageRenderer cannot.
        let host = NSHostingView(rootView: OrbView(model: chat, onClick: { _ in }, onSize: { _ in }, onDrag: { _ in })
            .background(Color(red: 0.85, green: 0.87, blue: 0.9)))
        let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 480, height: 520), styleMask: [.borderless], backing: .buffered, defer: false)
        win.contentView = host
        host.frame = NSRect(x: 0, y: 0, width: 480, height: 520)
        RunLoop.current.run(until: Date().addingTimeInterval(0.6))
        host.layoutSubtreeIfNeeded()
        if let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) {
            host.cacheDisplay(in: host.bounds, to: rep)
            try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: dir).appendingPathComponent("chat.png"))
        }
        let rich: [(String, String, String, String)] = [
            ("rich_code", "Write a python script that renames my screenshots by date", "That script renames every screenshot by its creation date. It's on your screen.",
             "```python\nfrom pathlib import Path\nfrom datetime import datetime\n\n# Rename Screenshot*.png to 2026-11-05_21-04.png\nfor f in Path.home().joinpath(\"Desktop\").glob(\"Screenshot*.png\"):\n    stamp = datetime.fromtimestamp(f.stat().st_birthtime)\n    f.rename(f.with_name(stamp.strftime(\"%Y-%m-%d_%H-%M\") + \".png\"))\n```\n- Run it from Terminal with `python3 rename.py`\n- It never overwrites: a clash raises an error"),
            ("rich_math", "Solve x squared minus five x plus six equals zero", "It factors neatly, x is two or three.",
             "1. Factor: x² - 5x + 6 = (x-2)(x-3)\n2. A product is zero when a factor is zero, so x-2=0 or x-3=0.\n\n```math\nx = 2  or  x = 3\n```\n\n**Answer:** x = 2 or x = 3"),
            ("rich_table", "React versus Vue for a small team", "Vue is the gentler pick for a small team.",
             "| | React | Vue |\n|---|---|---|\n| Learning curve | Steeper | Gentle |\n| Ecosystem | Huge | Smaller, tidy |\n| Best for | Big apps | Small teams |\n\n**Pick:** Vue, unless you need React Native."),
            ("rich_steps", "How do I set up an SSH key for GitHub", "Four steps: make a key, add it to your agent, copy it, paste it into GitHub.",
             "1. Create the key: `ssh-keygen -t ed25519 -C \"you@example.com\"`\n2. Start the agent: `eval \"$(ssh-agent -s)\"` then `ssh-add ~/.ssh/id_ed25519`\n3. Copy it: `pbcopy < ~/.ssh/id_ed25519.pub`\n4. GitHub, Settings, SSH keys, New SSH key, paste, save."),
        ]
        for (name, q, spoken, md) in rich {
            let m = OrbModel()
            m.state = .followUp
            m.messages = [ChatLine(fromUser: true, text: q), ChatLine(fromUser: false, text: spoken, rich: md)]
            let h = NSHostingView(rootView: OrbView(model: m, onClick: { _ in }, onSize: { _ in }, onDrag: { _ in })
                .background(Color(red: 0.85, green: 0.87, blue: 0.9)))
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 660, height: 620), styleMask: [.borderless], backing: .buffered, defer: false)
            w.contentView = h
            h.frame = NSRect(x: 0, y: 0, width: 660, height: 620)
            RunLoop.current.run(until: Date().addingTimeInterval(0.6))
            h.layoutSubtreeIfNeeded()
            if let rep = h.bitmapImageRepForCachingDisplay(in: h.bounds) {
                h.cacheDisplay(in: h.bounds, to: rep)
                try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: dir).appendingPathComponent("\(name).png"))
            }
        }
        for (name, state, user, reply, notice) in cases {
            let m = OrbModel()
            m.state = state; m.notice = notice
            if !user.isEmpty { m.messages.append(ChatLine(fromUser: true, text: user)) }
            if !reply.isEmpty { m.messages.append(ChatLine(fromUser: false, text: reply)) }
            let view = OrbView(model: m, onClick: { _ in }, onSize: { _ in }, onDrag: { _ in })
                .background(Color(red: 0.85, green: 0.87, blue: 0.9))
            let r = ImageRenderer(content: view)
            r.scale = 2
            guard let img = r.nsImage, let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
                  let png = rep.representation(using: .png, properties: [:]) else { continue }
            try? png.write(to: URL(fileURLWithPath: dir).appendingPathComponent("\(name).png"))
        }
    }
}


/// `JARVIS_ORB_SELFTEST=1 JarvisShell` drives the real panel logic (no clicks needed) and prints PASS/FAIL lines.
enum OrbSelfTest {
    @MainActor static func run() {
        var failures = 0
        func check(_ name: String, _ ok: Bool) { print(ok ? "PASS" : "FAIL", name); if !ok { failures += 1 } }
        // Run against a scratch settings suite so the test never changes your real orb position or pin.
        let defaults = UserDefaults(suiteName: "com.jarvis.shell.selftest")!
        defaults.removePersistentDomain(forName: "com.jarvis.shell.selftest")
        OrbPanel.store = defaults
        defer { defaults.removePersistentDomain(forName: "com.jarvis.shell.selftest") }
        check("orb is kept on screen by default", OrbPanel().pinned)
        defaults.set(false, forKey: "orbPinnedV3")
        let p = OrbPanel()
        check("hidden at start", !p.wantsVisible)
        p.apply(state: "listening", caption: nil)
        check("shown while listening", p.wantsVisible && p.model.cardVisible)
        p.showCaption("what's the weather", final: true)
        p.showCaption("Twenty one degrees and overcast.", final: false)
        check("two chat lines", p.model.messages.count == 2)
        p.apply(state: "idle", caption: nil)
        check("conversation lingers after the reply", p.wantsVisible && p.model.cardVisible)
        p.handle("hide")
        check("close button hides the box", !p.model.cardVisible)
        check("close button hides the orb when idle", !p.wantsVisible)
        check("history kept after closing", p.model.messages.count == 2)
        p.handle("toggle_card")
        check("tapping the orb brings the conversation back", p.model.cardVisible && p.wantsVisible)
        p.handle("hide")
        p.apply(state: "follow_up", caption: nil)
        check("close works during the follow-up window too (box hidden)", !p.model.cardVisible)
        p.apply(state: "listening", caption: nil)
        p.showCaption("next question", final: true)
        check("new question reopens the box", p.model.cardVisible)
        p.handle("clear")
        check("clear empties the history", p.model.messages.isEmpty)
        p.pinned = true
        p.handle("hide")
        check("pinned orb stays but the box can be closed", p.wantsVisible && !p.model.cardVisible)
        p.pinned = false

        // The orb must stay exactly where it is while the conversation opens, grows, shrinks and closes, wherever it sits.
        func settle() { RunLoop.current.run(until: Date().addingTimeInterval(0.45)) }
        func drift(_ panel: OrbPanel) -> CGFloat {
            let a = panel.orbScreenCenter, b = panel.orbAnchor
            return max(abs(a.x - b.x), abs(a.y - b.y))
        }
        guard let vis = NSScreen.main?.visibleFrame else { print(failures == 0 ? "ALL PASSED" : "\(failures) FAILED"); return }
        let spots: [(String, NSPoint)] = [
            ("bottom-right", NSPoint(x: vis.maxX - 40, y: vis.minY + 40)), ("top-left", NSPoint(x: vis.minX + 40, y: vis.maxY - 40)),
            ("top-right", NSPoint(x: vis.maxX - 40, y: vis.maxY - 40)), ("bottom-left", NSPoint(x: vis.minX + 40, y: vis.minY + 40)),
            ("middle", NSPoint(x: vis.midX, y: vis.midY)), ("just below middle", NSPoint(x: vis.midX + 5, y: vis.midY - 5)),
        ]
        let long = String(repeating: "Naan wrote that he cannot make it tonight and asked whether we could move the whole thing to Friday. ", count: 6)
        for (name, pt) in spots {
            let q = OrbPanel()
            q.place(orbAt: pt); q.pinned = true; q.orderFrontRegardless(); settle()
            var worst: CGFloat = drift(q)
            q.apply(state: "listening", caption: nil); settle(); worst = max(worst, drift(q))
            q.showCaption("what did Naan say", final: true); settle(); worst = max(worst, drift(q))
            q.showCaption(long, final: false); settle(); worst = max(worst, drift(q))
            q.showCaption("Short again.", final: false); settle(); worst = max(worst, drift(q))
            q.handle("hide"); settle(); worst = max(worst, drift(q))
            q.handle("toggle_card"); settle(); worst = max(worst, drift(q))
            q.handle("clear"); settle(); worst = max(worst, drift(q))
            check("orb stays put at \(name) (drift \(Int(worst)) pt)", worst < 1.5)
            q.pinned = false
        }
        // The window grows before the content has re-laid out. Reproduce that moment: enlarge the window with the card
        // open and look at where the orb is actually drawn.
        for (name, pt) in spots {
            let q = OrbPanel()
            q.place(orbAt: pt); q.pinned = true; q.model.debugMarker = true
            q.apply(state: "listening", caption: nil); q.showCaption("hello", final: true); q.showCaption("Hi there.", final: false)
            q.orderFrontRegardless(); settle()
            guard let before = q.orbPixelCenter() else { check("orb pixels found at \(name)", false); continue }
            let f = q.frame
            let big = NSRect(x: q.model.alignRight ? f.minX - 180 : f.minX, y: q.model.alignBottom ? f.minY : f.minY - 220,
                             width: f.width + 180, height: f.height + 220)
            q.setFrame(big, display: false)  // content not yet re-laid out for the new size
            guard let during = q.orbPixelCenter() else { check("orb pixels found while growing at \(name)", false); continue }
            let moved = max(abs(during.x - before.x), abs(during.y - before.y))
            check("orb does not jump while the window grows at \(name) (moved \(Int(moved)) pt)", moved < 1.5)
            q.pinned = false
        }
        // Typing: the typed text reaches the core, and the type box keeps the card (and the orb) on screen
        var sent: [String] = []
        let t = OrbPanel()
        t.onText = { sent.append($0) }
        t.sendText("what is on my calendar")
        check("typed text is sent to the core", sent == ["what is on my calendar"])
        t.model.dismissed = false; t.model.typing = true
        check("typing keeps the card up", t.model.cardVisible && t.wantsVisible)
        t.model.typing = false
        // Stepping aside: clicking another app ghosts the card; a new question or tapping the orb brings it back
        let g = OrbPanel()
        g.apply(state: "listening", caption: nil); g.showCaption("hello", final: true); g.showCaption("Hi.", final: false)
        g.orderFrontRegardless(); g.stepAside()
        check("never drops behind while Jarvis is busy", !g.model.ghost && g.level == .floating)
        g.apply(state: "idle", caption: nil); g.stepAside()
        check("a finished conversation drops behind other windows when you click another app", g.model.ghost && g.level == .normal)
        g.apply(state: "announcing", caption: nil)
        check("an alert brings it back on top", !g.model.ghost && g.level == .floating)
        g.apply(state: "idle", caption: nil); g.stepAside()
        g.showRich("**done**")
        check("a formatted reply brings it back on top", !g.model.ghost && g.level == .floating)
        g.apply(state: "idle", caption: nil); g.stepAside()
        g.announce("Battery at 10%")
        check("an announcement brings it back on top", !g.model.ghost && g.level == .floating)
        g.handle("hide"); g.apply(state: "idle", caption: nil); g.apply(state: "speaking", caption: nil)
        check("Jarvis speaking after you closed the box opens it again", g.model.cardVisible && g.wantsVisible)
        g.apply(state: "idle", caption: nil); g.stepAside()
        g.handle("toggle_card")
        check("tapping the orb brings it back on top with the card", !g.model.ghost && g.level == .floating && g.model.cardVisible)
        g.stepAside(); g.showCaption("again", final: true)
        check("a new question brings it back on top", !g.model.ghost && g.level == .floating)
        g.model.typing = true; g.stepAside()
        check("never drops behind while you are typing", !g.model.ghost)
        g.model.typing = false
        print(failures == 0 ? "ALL PASSED" : "\(failures) FAILED")
    }
}
