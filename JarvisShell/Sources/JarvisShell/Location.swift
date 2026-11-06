import CoreLocation
import Foundation

/// One-shot location lookup (city-level is enough for weather).
final class LocationProvider: NSObject, CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    private var waiters: [(Result<[String: Any], AXFailure>) -> Void] = []

    override init() {
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyKilometer
    }

    var authorized: Bool {
        switch manager.authorizationStatus {
        case .authorizedAlways: return true
        default: return false
        }
    }

    func requestAuthorization() { manager.requestAlwaysAuthorization() }

    func current(_ done: @escaping (Result<[String: Any], AXFailure>) -> Void) {
        switch manager.authorizationStatus {
        case .denied, .restricted:
            return done(.failure(AXFailure(message: "Location permission is denied")))
        case .notDetermined:
            manager.requestAlwaysAuthorization()
        default: break
        }
        waiters.append(done)
        manager.requestLocation()
    }

    func locationManager(_ m: CLLocationManager, didUpdateLocations locs: [CLLocation]) {
        guard let loc = locs.last else { return }
        CLGeocoder().reverseGeocodeLocation(loc) { [weak self] marks, _ in
            let pm = marks?.first
            let out: [String: Any] = [
                "lat": loc.coordinate.latitude, "lon": loc.coordinate.longitude,
                "city": pm?.locality ?? "", "region": pm?.administrativeArea ?? "",
                "country": pm?.country ?? "", "timezone": pm?.timeZone?.identifier ?? "",
            ]
            self?.flush(.success(out))
        }
    }

    func locationManager(_ m: CLLocationManager, didFailWithError error: Error) {
        flush(.failure(AXFailure(message: "Location unavailable: \(error.localizedDescription)")))
    }

    func locationManagerDidChangeAuthorization(_ m: CLLocationManager) {
        if !waiters.isEmpty, authorized { m.requestLocation() }
    }

    private func flush(_ r: Result<[String: Any], AXFailure>) {
        let ws = waiters
        waiters.removeAll()
        for w in ws { w(r) }
    }
}
