import CoreGraphics
import Foundation

/// Window snap geometry. Pure math in a top-left-origin coordinate space.
public enum SnapLayout {
    public static let positions = [
        "left", "right", "top", "bottom", "full", "center",
        "top_left", "top_right", "bottom_left", "bottom_right",
        "left_third", "middle_third", "right_third", "left_two_thirds", "right_two_thirds",
    ]

    public static func rect(for position: String, in area: CGRect) -> CGRect? {
        let x = area.minX, y = area.minY, w = area.width, h = area.height
        switch position {
        case "left": return CGRect(x: x, y: y, width: w / 2, height: h)
        case "right": return CGRect(x: x + w / 2, y: y, width: w / 2, height: h)
        case "top": return CGRect(x: x, y: y, width: w, height: h / 2)
        case "bottom": return CGRect(x: x, y: y + h / 2, width: w, height: h / 2)
        case "full", "maximize": return area
        case "center":
            return CGRect(x: x + w * 0.15, y: y + h * 0.1, width: w * 0.7, height: h * 0.8)
        case "top_left": return CGRect(x: x, y: y, width: w / 2, height: h / 2)
        case "top_right": return CGRect(x: x + w / 2, y: y, width: w / 2, height: h / 2)
        case "bottom_left": return CGRect(x: x, y: y + h / 2, width: w / 2, height: h / 2)
        case "bottom_right": return CGRect(x: x + w / 2, y: y + h / 2, width: w / 2, height: h / 2)
        case "left_third": return CGRect(x: x, y: y, width: w / 3, height: h)
        case "middle_third": return CGRect(x: x + w / 3, y: y, width: w / 3, height: h)
        case "right_third": return CGRect(x: x + 2 * w / 3, y: y, width: w / 3, height: h)
        case "left_two_thirds": return CGRect(x: x, y: y, width: 2 * w / 3, height: h)
        case "right_two_thirds": return CGRect(x: x + w / 3, y: y, width: 2 * w / 3, height: h)
        default: return nil
        }
    }

    /// Converts a Cocoa (bottom-left origin) rect to AX/CG (top-left origin) given the primary screen height.
    public static func toTopLeft(_ r: CGRect, primaryHeight: CGFloat) -> CGRect {
        CGRect(x: r.minX, y: primaryHeight - r.maxY, width: r.width, height: r.height)
    }
}

/// Downscale math for screenshots sent to the vision model.
public func fitSize(width: Int, height: Int, maxDimension: Int) -> (Int, Int) {
    let longest = max(width, height)
    guard longest > maxDimension, longest > 0 else { return (width, height) }
    let s = Double(maxDimension) / Double(longest)
    return (max(1, Int((Double(width) * s).rounded())), max(1, Int((Double(height) * s).rounded())))
}
