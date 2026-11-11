import XCTest
@testable import JarvisShellCore

final class CardDataTests: XCTestCase {
    func testParsesATileWithActions() {
        let d: [String: Any] = ["kind": "tile", "title": "Boomi drive", "chips": [["text": "17 Nov", "tone": ""]],
                                "actions": [["label": "Open in Gmail", "kind": "url", "value": "https://mail.google.com", "primary": true],
                                            ["label": "Undo", "kind": "say", "value": "undo that"]]]
        let c = CardData.parse(d)
        XCTAssertEqual(c?.title, "Boomi drive")
        XCTAssertEqual(c?.actions.count, 2)
        XCTAssertEqual(c?.actions.first?.primary, true)
        XCTAssertEqual(c?.chips.first?.text, "17 Nov")
    }

    func testUnknownKindAndEmptyCardsAreIgnored() {
        XCTAssertNil(CardData.parse(["kind": "webview", "title": "x"]))
        XCTAssertNil(CardData.parse(["kind": "tile"]))
    }

    func testUnsafeLinksAreDropped() {
        let d: [String: Any] = ["kind": "tile", "title": "t", "actions": [
            ["label": "A", "kind": "url", "value": "javascript:alert(1)"],
            ["label": "B", "kind": "url", "value": "file:///etc/passwd"],
            ["label": "C", "kind": "url", "value": "https://example.com"],
            ["label": "D", "kind": "shell", "value": "rm -rf /"]]]
        XCTAssertEqual(CardData.parse(d)?.actions.map(\.label), ["C"])
    }

    func testListsAreBoundedAndProgressClamped() {
        let items = (0..<20).map { ["title": "t\($0)"] as [String: Any] }
        let c = CardData.parse(["kind": "list", "items": items, "progress": 4.0])
        XCTAssertEqual(c?.items.count, 8)
        XCTAssertEqual(c?.progress, 1.0)
    }

    func testPlainTextForCopy() {
        let c = CardData.parse(["kind": "facts", "kicker": "Markets", "rows": [["label": "Nifty", "value": "24,310 +0.6%", "tone": "up"]]])
        XCTAssertEqual(c?.plainText, "Markets\nNifty: 24,310 +0.6%")
    }
}
