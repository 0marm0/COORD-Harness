import Foundation
import XCTest

final class WordmarkClickRoutingTests: XCTestCase {
    func testCoordWordmarkOpensNativeCockpitThroughPanelActionRoute() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let stack = try String(
            contentsOf: root.appendingPathComponent(
                "apps/menubar/Sources/UI/ContentStackAndRows.swift"
            ),
            encoding: .utf8
        )
        let popover = try String(
            contentsOf: root.appendingPathComponent(
                "apps/menubar/Sources/App/PopoverController.swift"
            ),
            encoding: .utf8
        )

        XCTAssertTrue(stack.contains("let mark = CockpitWordmarkButton("))
        XCTAssertTrue(stack.contains("identifier: \"main.coord-wordmark\""))
        XCTAssertTrue(stack.contains("accessibilityLabel: \"Open COORD Cockpit\""))
        XCTAssertTrue(stack.contains("emit(.openCockpit)"))
        XCTAssertTrue(popover.contains("case .openCockpit:"))
        XCTAssertTrue(popover.contains("(NSApp.delegate as? AppDelegate)?.openCockpitWindow()"))
    }
}
