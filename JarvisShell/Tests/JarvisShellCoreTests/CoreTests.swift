import XCTest
@testable import JarvisShellCore

final class ProtocolTests: XCTestCase {
    func testControlRoundtrip() throws {
        let c = Control("hotkey", ["action": "down", "n": 3], id: "abc")
        let back = try Control.decode(c.encode())
        XCTAssertEqual(back.type, "hotkey")
        XCTAssertEqual(back.id, "abc")
        XCTAssertEqual(back.string("action"), "down")
        XCTAssertEqual(back.double("n"), 3)
    }

    func testDecodeRejectsGarbage() {
        XCTAssertThrowsError(try Control.decode("[]"))
        XCTAssertThrowsError(try Control.decode("not json"))
    }

    func testTTSFrameRoundtrip() throws {
        let id = String(repeating: "ab", count: 16)
        let frame = try XCTUnwrap(encodeTTSFrame(utteranceId: id, pcm16: Data([1, 2, 3, 4])))
        XCTAssertEqual(frame.count, 1 + 16 + 4)
        let dec = try decodeTTSFrame(frame)
        XCTAssertEqual(dec.utteranceId, id)
        XCTAssertEqual(dec.pcm16, Data([1, 2, 3, 4]))
        XCTAssertNil(encodeTTSFrame(utteranceId: "zz", pcm16: Data()))
        XCTAssertThrowsError(try decodeTTSFrame(Data([2, 1])))
    }

    func testMicFrameHeader() {
        XCTAssertEqual(encodeMicFrame(Data([9, 9])), Data([1, 9, 9]))
    }

    func testChunkerEmitsExactFrames() {
        var c = FrameChunker()
        XCTAssertTrue(c.push([Int16](repeating: 1, count: 300)).isEmpty)
        let frames = c.push([Int16](repeating: 2, count: 700))
        XCTAssertEqual(frames.count, 3)
        XCTAssertTrue(frames.allSatisfy { $0.count == 640 })
        XCTAssertEqual(c.pending, 40)
    }

    func testSampleConversion() {
        XCTAssertEqual(floatToInt16([0, 1, -1, 2, -2]), [0, 32767, -32767, 32767, -32767])
        let d = Data([0x00, 0x40])  // 16384 little-endian
        XCTAssertEqual(int16ToFloat(d)[0], 0.5, accuracy: 0.0001)
    }
}

final class SupervisionTests: XCTestCase {
    func testBackoffSteps() {
        var b = Backoff()
        XCTAssertEqual((0..<7).map { _ in b.next() }, [1, 2, 4, 8, 30, 30, 30])
        b.reset()
        XCTAssertEqual(b.next(), 1)
    }

    func testCrashTrackerGivesUp() {
        var t = CrashTracker(limit: 5, window: 300)
        let base = Date()
        for i in 0..<4 { XCTAssertFalse(t.record(at: base.addingTimeInterval(Double(i) * 10))) }
        XCTAssertTrue(t.record(at: base.addingTimeInterval(50)))
    }

    func testCrashTrackerForgetsOldCrashes() {
        var t = CrashTracker(limit: 3, window: 60)
        let base = Date()
        _ = t.record(at: base)
        _ = t.record(at: base.addingTimeInterval(10))
        XCTAssertFalse(t.record(at: base.addingTimeInterval(500)))
    }

    func testIPCInfoParse() throws {
        let j = #"{"host":"127.0.0.1","port":5123,"token":"t","pid":7}"#.data(using: .utf8)!
        let i = try XCTUnwrap(IPCInfo.parse(j))
        XCTAssertEqual(i.url.absoluteString, "ws://127.0.0.1:5123/ipc")
        XCTAssertNil(IPCInfo.parse(Data("{}".utf8)))
    }

    func testIgnorableClients() {
        XCTAssertTrue(isIgnorableAudioClient(bundleId: "com.apple.corespeechd"))
        XCTAssertFalse(isIgnorableAudioClient(bundleId: "us.zoom.xos"))
    }

    func testOrbStateBusy() {
        XCTAssertTrue(OrbStateName(rawValue: "speaking")!.isBusy)
        XCTAssertTrue(OrbStateName(rawValue: "follow_up")!.isBusy)
        XCTAssertFalse(OrbStateName(rawValue: "idle")!.isBusy)
    }

    func testForbiddenLabels() {
        for l in ["Send", "Post", "Submit order", "Buy now", "Pay $5", "Delete", "Confirm order", "Remove item"] {
            XCTAssertTrue(ForbiddenLabels.isForbidden(l), l)
        }
        for l in ["Cancel", "Sender info", "Compose", "Reply", "Postcode", "Save draft"] {
            XCTAssertFalse(ForbiddenLabels.isForbidden(l), l)
        }
    }
}

final class GeometryTests: XCTestCase {
    func testSnap() throws {
        let area = CGRect(x: 0, y: 25, width: 1440, height: 875)
        XCTAssertEqual(SnapLayout.rect(for: "left", in: area), CGRect(x: 0, y: 25, width: 720, height: 875))
        XCTAssertEqual(SnapLayout.rect(for: "right", in: area), CGRect(x: 720, y: 25, width: 720, height: 875))
        XCTAssertEqual(SnapLayout.rect(for: "full", in: area), area)
        XCTAssertNil(SnapLayout.rect(for: "nonsense", in: area))
        for p in SnapLayout.positions { XCTAssertNotNil(SnapLayout.rect(for: p, in: area), p) }
    }

    func testCoordinateFlip() {
        let cocoa = CGRect(x: 0, y: 0, width: 100, height: 800)  // bottom-left origin on a 900pt screen
        let ax = SnapLayout.toTopLeft(cocoa, primaryHeight: 900)
        XCTAssertEqual(ax.minY, 100)
    }

    func testFitSize() {
        XCTAssertEqual(fitSize(width: 3000, height: 1500, maxDimension: 1500).0, 1500)
        XCTAssertEqual(fitSize(width: 3000, height: 1500, maxDimension: 1500).1, 750)
        XCTAssertEqual(fitSize(width: 800, height: 600, maxDimension: 1500).0, 800)
    }

    func testRichMarkdownBlocks() {
        let md = """
        Intro with **bold**.

        ## Steps
        1. First
        2. Second

        - a
        - b

        | Name | Speed |
        |---|---|
        | A | fast |
        | B | slow |

        ```python
        print("hi")
        ```

        ```math
        x = (-b ± √(b²-4ac))/2a
        ```

        > note
        """
        let blocks = RichMarkdown.parse(md)
        XCTAssertEqual(blocks.count, 8)
        XCTAssertEqual(blocks[0], .paragraph("Intro with **bold**."))
        XCTAssertEqual(blocks[1], .heading(level: 2, text: "Steps"))
        XCTAssertEqual(blocks[2], .numbered([NumberedItem(number: 1, text: "First"), NumberedItem(number: 2, text: "Second")]))
        XCTAssertEqual(blocks[3], .bullets(["a", "b"]))
        XCTAssertEqual(blocks[4], .table(header: ["Name", "Speed"], rows: [["A", "fast"], ["B", "slow"]]))
        XCTAssertEqual(blocks[5], .code(language: "python", text: "print(\"hi\")"))
        XCTAssertEqual(blocks[6], .math("x = (-b ± √(b²-4ac))/2a"))
        XCTAssertEqual(blocks[7], .quote("note"))
    }

    func testRichMarkdownUnclosedFenceAndPipesInParagraph() {
        XCTAssertEqual(RichMarkdown.parse("```js\nlet a = 1"), [.code(language: "js", text: "let a = 1")])
        XCTAssertEqual(RichMarkdown.parse("use a | b to pipe"), [.paragraph("use a | b to pipe")])
    }

    func testSystemDaemonsAreNotCalls() {
        XCTAssertTrue(isSystemExecutablePath("/usr/libexec/historicalaudiod"))
        XCTAssertFalse(isSystemExecutablePath("/Applications/zoom.us.app/Contents/MacOS/zoom.us"))
        XCTAssertTrue(isIgnorableAudioClient(bundleId: "com.apple.CoreSpeech"))
        XCTAssertFalse(isIgnorableAudioClient(bundleId: "us.zoom.xos"))
    }
}
