import AVFoundation
import AppKit
import ApplicationServices
import CoreGraphics
import Foundation
import ServiceManagement

enum Permissions {
    static func status(location: LocationProvider) -> [String: Any] {
        [
            "mic": AVCaptureDevice.authorizationStatus(for: .audio) == .authorized,
            "camera_probe": AVCaptureDevice.authorizationStatus(for: .video) != .denied,
            "accessibility": AXIsProcessTrusted(),
            "screen": CGPreflightScreenCaptureAccess(),
            "location": location.authorized,
        ]
    }

    /// Opens the right prompt or Settings pane. Returns a short human hint for the orb caption.
    @discardableResult
    static func request(_ which: String, location: LocationProvider) -> String {
        switch which {
        case "mic":
            AVCaptureDevice.requestAccess(for: .audio) { _ in }
            return "Microphone"
        case "accessibility":
            let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
            _ = AXIsProcessTrustedWithOptions([key: true] as CFDictionary)
            return "Accessibility"
        case "screen":
            _ = CGRequestScreenCaptureAccess()
            return "Screen Recording"
        case "location":
            location.requestAuthorization()
            return "Location"
        case "automation":
            openSettings("Privacy_Automation")
            return "Automation"
        case "full_disk":
            openSettings("Privacy_AllFiles")
            return "Full Disk Access"
        default:
            return which
        }
    }

    static func openSettings(_ anchor: String) {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?\(anchor)") {
            NSWorkspace.shared.open(url)
        }
    }
}

enum LoginItem {
    private static let optOutKey = "loginItemOptOut"

    static var status: SMAppService.Status { SMAppService.mainApp.status }
    static var enabled: Bool { status == .enabled }

    static var statusName: String {
        switch status {
        case .enabled: return "enabled"
        case .requiresApproval: return "requiresApproval"
        case .notRegistered: return "notRegistered"
        case .notFound: return "notFound"
        @unknown default: return "unknown"
        }
    }

    @discardableResult
    static func set(_ on: Bool) -> Bool {
        UserDefaults.standard.set(!on, forKey: optOutKey)  // remember a deliberate "off" so we never re-enable it
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            return true
        } catch {
            NSLog("Login item change failed: \(error.localizedDescription)")
            return false
        }
    }

    /// Jarvis must come back after a reboot without anyone opening it. Register on launch unless you switched it off.
    /// Returns true when macOS wants the user to allow it under System Settings > General > Login Items.
    @discardableResult
    static func ensureRegistered() -> Bool {
        if UserDefaults.standard.bool(forKey: optOutKey) { return false }
        if status != .enabled {
            do { try SMAppService.mainApp.register() } catch { NSLog("Login item registration failed: \(error.localizedDescription)") }
        }
        return status == .requiresApproval
    }

    static func openSettings() { SMAppService.openSystemSettingsLoginItems() }
}
