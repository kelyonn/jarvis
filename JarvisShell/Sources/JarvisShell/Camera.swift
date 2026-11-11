import AVFoundation
import AppKit
import CoreImage
import Foundation
import ImageIO
import JarvisShellCore
import UniformTypeIdentifiers

/// One camera frame, on explicit request only. The capture session is started, one frame is taken after a short
/// warm-up (so exposure settles), and the session is stopped again before returning. Nothing is written to disk.
/// The OS shows its own camera-in-use indicator while the session runs.
enum Camera {
    static func capture(args: [String: Any]) throws -> [String: Any] {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized: break
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .video) { _ in }  // shows the system prompt; the user asks again afterwards
            throw AXFailure(message: "Camera permission is not granted")
        default:
            throw AXFailure(message: "Camera permission is not granted")
        }
        guard let device = AVCaptureDevice.default(for: .video) else { throw AXFailure(message: "no camera found") }
        let maxDim = (args["max_dimension"] as? Int) ?? 1280

        let session = AVCaptureSession()
        session.sessionPreset = .high
        let grabber = FrameGrabber(skip: 8)  // ~0.3 s of frames while auto-exposure settles
        let output = AVCaptureVideoDataOutput()
        output.alwaysDiscardsLateVideoFrames = true
        let queue = DispatchQueue(label: "jarvis.camera")
        output.setSampleBufferDelegate(grabber, queue: queue)
        do {
            let input = try AVCaptureDeviceInput(device: device)
            guard session.canAddInput(input), session.canAddOutput(output) else {
                throw AXFailure(message: "camera could not be opened")
            }
            session.addInput(input)
            session.addOutput(output)
        } catch let e as AXFailure { throw e } catch { throw AXFailure(message: "camera could not be opened") }

        session.startRunning()
        defer { session.stopRunning() }  // off again whatever happens below
        guard grabber.wait(timeout: 6), let buffer = grabber.pixelBuffer else {
            throw AXFailure(message: "the camera gave no picture")
        }
        let ci = CIImage(cvPixelBuffer: buffer)
        guard let img = CIContext().createCGImage(ci, from: ci.extent) else { throw AXFailure(message: "image failed") }
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
        return ["image_b64": (out as Data).base64EncodedString(), "mime": "image/jpeg", "width": w, "height": h,
                "scale_x": 1.0, "scale_y": 1.0]
    }

    private final class FrameGrabber: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
        private let sem = DispatchSemaphore(value: 0)
        private var remaining: Int
        private(set) var pixelBuffer: CVPixelBuffer?
        private var done = false
        init(skip: Int) { remaining = skip }

        func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
            guard !done else { return }
            if remaining > 0 { remaining -= 1; return }
            done = true
            pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer)
            sem.signal()
        }

        func wait(timeout: Double) -> Bool { sem.wait(timeout: .now() + timeout) == .success }
    }
}
