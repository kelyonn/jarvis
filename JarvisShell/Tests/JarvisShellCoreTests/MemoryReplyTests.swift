import XCTest
@testable import JarvisShellCore

final class MemoryReplyTests: XCTestCase {
    func testGroupsAreParsedInOrderAndEmptyOnesDropped() {
        let data: [String: Any] = ["ok": true, "groups": [
            ["kind": "person", "title": "People", "items": [["id": "a1", "text": "Brother is Arjun", "date": "2026-11-01"]]],
            ["kind": "fact", "title": "Facts", "items": []],
            ["kind": "loop", "title": "Loops", "items": [["id": "b2", "text": "Call the dentist"]]],
        ]]
        let groups = MemoryReply.groups(from: data)
        XCTAssertEqual(groups.map(\.kind), ["person", "loop"])
        XCTAssertEqual(groups[0].items, [MemoryItem(id: "a1", text: "Brother is Arjun", date: "2026-11-01")])
        XCTAssertEqual(groups[1].items[0].date, "")  // a missing date is fine
    }

    func testMalformedRowsAreSkippedNotFatal() {
        let data: [String: Any] = ["groups": [
            ["kind": "fact", "title": "Facts", "items": [["text": "no id"], ["id": "x", "text": "ok"], ["id": 5]]],
            ["title": "no kind", "items": []],
        ]]
        let groups = MemoryReply.groups(from: data)
        XCTAssertEqual(groups.count, 1)
        XCTAssertEqual(groups[0].items.map(\.id), ["x"])
        XCTAssertTrue(MemoryReply.groups(from: [:]).isEmpty)
    }

    func testErrorsComeFromOkFalseOnly() {
        XCTAssertNil(MemoryReply.error(from: ["ok": true]))
        XCTAssertNil(MemoryReply.error(from: [:]))
        XCTAssertEqual(MemoryReply.error(from: ["ok": false, "error": "gone"]), "gone")
        XCTAssertEqual(MemoryReply.error(from: ["ok": false]), "Something went wrong.")
    }

    func testMessageTypesMatchThePythonSide() {
        XCTAssertEqual(MessageType.memoryRequest, "memory_request")
        XCTAssertEqual(MessageType.memoryResult, "memory_result")
    }
}
