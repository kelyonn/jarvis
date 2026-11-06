import Foundation

/// Wire constants. Mirrors `jarvis_core/ipc/protocol.py`.
public enum Wire {
    public static let micFrame: UInt8 = 0x01
    public static let ttsFrame: UInt8 = 0x02
    public static let ttsSampleRate: Double = 24_000
    public static let micSampleRate: Double = 16_000
    /// 20 ms at 16 kHz.
    public static let micFrameSamples = 320
    public static let pingInterval: TimeInterval = 5
}

public enum MessageType {
    // Shell -> core
    public static let hello = "hello"
    public static let hotkey = "hotkey"
    public static let lifecycle = "lifecycle"
    public static let deviceUsage = "device_usage"
    public static let playback = "playback"
    public static let axResult = "ax_result"
    public static let location = "location"
    public static let orbClick = "orb_click"
    public static let textInput = "text_input"
    public static let command = "command"
    public static let ping = "ping"
    public static let pong = "pong"
    // core -> Shell
    public static let welcome = "welcome"
    public static let hideUI = "hide_ui"
    public static let rich = "rich"
    public static let state = "state"
    public static let caption = "caption"
    public static let playBegin = "play_begin"
    public static let playEnd = "play_end"
    public static let stopAudio = "stop_audio"
    public static let earcon = "earcon"
    public static let orbQueue = "orb_queue"
    public static let axRequest = "ax_request"
    public static let inputRequest = "input_request"
    public static let screenshotRequest = "screenshot_request"
    public static let locationRequest = "location_request"
    public static let permissionPrompt = "permission_prompt"
    public static let notify = "notify"
    public static let mic = "mic"
    public static let micOpen = "mic_open"
}

public enum ProtocolError: Error, Equatable {
    case malformed
    case shortFrame
}

public struct Control {
    public var type: String
    public var id: String
    public var data: [String: Any]

    public init(_ type: String, _ data: [String: Any] = [:], id: String = "") {
        self.type = type
        self.data = data
        self.id = id.isEmpty ? String(UUID().uuidString.prefix(8)).lowercased() : id
    }

    public func encode() -> String {
        let obj: [String: Any] = ["type": type, "id": id, "data": data]
        guard JSONSerialization.isValidJSONObject(obj),
              let d = try? JSONSerialization.data(withJSONObject: obj, options: [.withoutEscapingSlashes]),
              let s = String(data: d, encoding: .utf8) else { return "{}" }
        return s
    }

    public static func decode(_ text: String) throws -> Control {
        guard let d = text.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
              let type = obj["type"] as? String else { throw ProtocolError.malformed }
        let data = (obj["data"] as? [String: Any]) ?? [:]
        return Control(type, data, id: (obj["id"] as? String) ?? "")
    }

    // Typed accessors keep call sites terse.
    public func string(_ k: String) -> String? { data[k] as? String }
    public func bool(_ k: String) -> Bool? { data[k] as? Bool }
    public func double(_ k: String) -> Double? {
        if let d = data[k] as? Double { return d }
        if let i = data[k] as? Int { return Double(i) }
        return nil
    }
}

public func encodeMicFrame(_ pcm16: Data) -> Data {
    var d = Data([Wire.micFrame])
    d.append(pcm16)
    return d
}

public struct TTSFrame: Equatable {
    public let utteranceId: String  // 32 hex chars
    public let pcm16: Data
}

public func decodeTTSFrame(_ frame: Data) throws -> TTSFrame {
    guard frame.count >= 17 else { throw ProtocolError.shortFrame }
    let bytes = [UInt8](frame)
    guard bytes[0] == Wire.ttsFrame else { throw ProtocolError.malformed }
    let id = bytes[1..<17].map { String(format: "%02x", $0) }.joined()
    return TTSFrame(utteranceId: id, pcm16: Data(bytes[17...]))
}

public func encodeTTSFrame(utteranceId: String, pcm16: Data) -> Data? {
    var out = Data([Wire.ttsFrame])
    var idx = utteranceId.startIndex
    var idBytes = [UInt8]()
    while idx < utteranceId.endIndex {
        guard let next = utteranceId.index(idx, offsetBy: 2, limitedBy: utteranceId.endIndex),
              let b = UInt8(utteranceId[idx..<next], radix: 16) else { return nil }
        idBytes.append(b)
        idx = next
    }
    guard idBytes.count == 16 else { return nil }
    out.append(contentsOf: idBytes)
    out.append(pcm16)
    return out
}

/// Accumulates int16 samples and emits fixed 20 ms frames (640 bytes, little-endian).
public struct FrameChunker {
    private var buffer: [Int16] = []
    public let frameSamples: Int

    public init(frameSamples: Int = Wire.micFrameSamples) {
        self.frameSamples = frameSamples
        buffer.reserveCapacity(frameSamples * 4)
    }

    public mutating func push(_ samples: [Int16]) -> [Data] {
        buffer.append(contentsOf: samples)
        var frames: [Data] = []
        while buffer.count >= frameSamples {
            let chunk = Array(buffer[0..<frameSamples])
            buffer.removeFirst(frameSamples)
            frames.append(chunk.withUnsafeBufferPointer { Data(buffer: $0) })
        }
        return frames
    }

    public mutating func reset() { buffer.removeAll(keepingCapacity: true) }
    public var pending: Int { buffer.count }
}

public func floatToInt16(_ samples: [Float]) -> [Int16] {
    samples.map { s in
        let c = max(-1.0, min(1.0, s))
        return Int16(c * 32767.0)
    }
}

public func int16ToFloat(_ data: Data) -> [Float] {
    let n = data.count / 2
    var out = [Float](repeating: 0, count: n)
    data.withUnsafeBytes { raw in
        let p = raw.bindMemory(to: Int16.self)
        for i in 0..<n { out[i] = Float(Int16(littleEndian: p[i])) / 32768.0 }
    }
    return out
}
