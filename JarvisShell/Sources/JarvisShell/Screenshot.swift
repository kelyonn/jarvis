import AppKit
import CoreGraphics
import Foundation
import ImageIO
import JarvisShellCore
import UniformTypeIdentifiers

/// Screen capture on request only. Returns a downscaled JPEG (base64) for the vision model.
enum Screenshot {
    static func capture(args: [String: Any]) throws -> [String: Any] {
        guard CGPreflightScreenCaptureAccess() else {
            throw AXFailure(message: "Screen Recording permission is not granted")
        }
        let maxDim = (args["max_dimension"] as? Int) ?? 1568
        let tmp = FileManager.default.temporaryDirectory.appendingPathComponent("jarvis-\(UUID().uuidString).png")
        defer { try? FileManager.default.removeItem(at: tmp) }

        var argv = ["-x", "-t", "png"]
        if args["include_cursor"] as? Bool == true { argv.append("-C") }
        let target = (args["target"] as? String) ?? "screen"
        if target == "window", let wid = frontWindowID() {
            argv += ["-l", String(wid)]
        } else if target == "selection" {
            argv.append("-s")  // the user drags a region; Esc cancels and writes no file
        } else if target.hasPrefix("display:"), let n = Int(target.dropFirst("display:".count)), n > 0 {
            argv += ["-D", String(n)]
        }
        argv.append(tmp.path)
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
        p.arguments = argv
        try p.run()
        p.waitUntilExit()
        if target == "selection", !FileManager.default.fileExists(atPath: tmp.path) {
            throw AXFailure(message: "Screenshot cancelled")
        }
        guard p.terminationStatus == 0, let src = CGImageSourceCreateWithURL(tmp as CFURL, nil),
              let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
            throw AXFailure(message: "screencapture failed")
        }
        let (w, h) = fitSize(width: img.width, height: img.height, maxDimension: maxDim)
        guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                  bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else {
            throw AXFailure(message: "image context failed")
        }
        ctx.interpolationQuality = .high
        ctx.draw(img, in: CGRect(x: 0, y: 0, width: w, height: h))
        guard let scaled = ctx.makeImage() else { throw AXFailure(message: "scale failed") }
        let out = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(out, UTType.jpeg.identifier as CFString, 1, nil) else {
            throw AXFailure(message: "encoder failed")
        }
        CGImageDestinationAddImage(dest, scaled, [kCGImageDestinationLossyCompressionQuality: 0.7] as CFDictionary)
        CGImageDestinationFinalize(dest)

        let screen = NSScreen.main?.frame.size ?? .zero
        return [
            "image_b64": (out as Data).base64EncodedString(),
            "mime": "image/jpeg",
            "width": w, "height": h,
            "screen_width": Int(screen.width), "screen_height": Int(screen.height),
            "scale_x": Double(screen.width) / Double(w), "scale_y": Double(screen.height) / Double(h),
        ]
    }

    private static func frontWindowID() -> CGWindowID? {
        guard let pid = NSWorkspace.shared.frontmostApplication?.processIdentifier,
              let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
                as? [[String: Any]] else { return nil }
        for w in list where (w[kCGWindowOwnerPID as String] as? Int32) == pid && (w[kCGWindowLayer as String] as? Int) == 0 {
            if let n = w[kCGWindowNumber as String] as? UInt32 { return n }
        }
        return nil
    }
}
