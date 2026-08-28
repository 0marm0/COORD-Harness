import XCTest
@testable import CoordCockpitMac

final class NativeNavigationTests: XCTestCase {
    func testTopBarNavigationStartsBesideBrandAndKeepsAllDestinationsAtMinimumWindowWidth() {
        let layout = CockpitTopBarLayout.compute(
            width: 980,
            surfaceWidths: [52, 68, 54, 56],
            showsResume: false,
            showsPause: false
        )

        XCTAssertEqual(layout.surfaceIndices, [0, 1, 2, 3])
        XCTAssertEqual(layout.surfaces.first?.minX, 214)
        XCTAssertEqual(layout.surfaces.count, 4)
        XCTAssertLessThan(layout.surfaces.last?.maxX ?? .infinity, layout.stats.minX)
    }

    func testNarrowTopBarAlwaysRetainsMoreAsRouteRecovery() {
        let layout = CockpitTopBarLayout.compute(
            width: 860,
            surfaceWidths: [52, 68, 54, 56],
            showsResume: true,
            showsPause: true
        )

        XCTAssertFalse(layout.surfaceIndices.isEmpty)
        XCTAssertEqual(layout.surfaceIndices.last, 3)
        XCTAssertEqual(layout.surfaceIndices.count, layout.surfaces.count)
        XCTAssertEqual(layout.surfaces.first?.minX, 214)
    }

    func testNativeNavigationUsesUnifiedCommsWithoutFleetOrPulsePseudoRoutes() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let source = try String(
            contentsOf: root.appendingPathComponent("apps/menubar/Sources/Cockpit/UI/CockpitRootView.swift"),
            encoding: .utf8
        )

        for primary in ["(\"Jobs\", \"jobs\")", "(\"Comms\", \"comms\")", "(\"Atlas\", \"atlas\")", "(\"More\", \"more\")"] {
            XCTAssertTrue(source.contains(primary))
        }
        for route in [
            "/?embedded=1#v=comms",
            "/cockpit?native_map=1&embedded=1&lens=deps",
            "/ops?embedded=1",
        ] {
            XCTAssertTrue(source.contains(route), "missing route \(route)")
        }
        for child in ["(\"Explorer\", \"context:explorer\")", "(\"Startup\", \"context:startup\")", "(\"Operations\", Surface.atlas.rawValue)"] {
            XCTAssertTrue(source.contains(child))
        }
        XCTAssertTrue(source.contains("identifier == \"jobs\" || identifier == \"comms\""))
        XCTAssertFalse(source.contains("(\"Fleet\", Surface.fleet.rawValue)"))
        XCTAssertFalse(source.contains("(\"Pulse\", Surface.pulse.rawValue)"))
        XCTAssertFalse(source.contains("case fleet"))
        XCTAssertFalse(source.contains("case pulse"))
        XCTAssertFalse(source.contains("section=fleet"))
        XCTAssertFalse(source.contains("section=pulse"))
        XCTAssertTrue(source.contains("showContextPalette(initialQuery: \"startup orientation\")"))
        XCTAssertTrue(source.contains("guard let commandsButton else { return }"))
        XCTAssertTrue(source.contains("commandsPressed(commandsButton)"))
        XCTAssertFalse(source.contains("showContextPalette()\n        return"))
    }

    func testBothNativeCockpitHostsMountTheExactUnifiedCommsSurface() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let macRoot = try String(
            contentsOf: root.appendingPathComponent("apps/macOS/Sources/MacCockpitView.swift"),
            encoding: .utf8
        )
        let menuRoot = try String(
            contentsOf: root.appendingPathComponent("apps/menubar/Sources/Cockpit/UI/CockpitRootView.swift"),
            encoding: .utf8
        )
        let menuWebView = try String(
            contentsOf: root.appendingPathComponent("apps/menubar/Sources/Cockpit/UI/CockpitMapWebView.swift"),
            encoding: .utf8
        )

        XCTAssertTrue(macRoot.contains("case comms = \"Comms\""))
        XCTAssertTrue(macRoot.contains("MacCommsView(store: commsSurface, baseURLText: model.baseURLText)"))
        XCTAssertTrue(macRoot.contains("static let unifiedRoute = \"/?embedded=1#v=comms\""))
        XCTAssertTrue(macRoot.contains("target != requestedURL"), "polling must not reload the retained WKWebView")

        XCTAssertTrue(menuRoot.contains("case .comms: return \"/?embedded=1#v=comms\""))
        let pathAssignment = try XCTUnwrap(menuRoot.range(of: "view.surfacePath = surface.embedPath"))
        let activation = try XCTUnwrap(menuRoot.range(of: "view.activate()", range: pathAssignment.upperBound..<menuRoot.endIndex))
        XCTAssertLessThan(pathAssignment.lowerBound, activation.lowerBound, "the exact path must be assigned before first activation")
        XCTAssertTrue(menuWebView.contains("activate(forceReload: false)"))
        XCTAssertTrue(menuWebView.contains("loadIfNeeded(force: forceReload)"))
        XCTAssertTrue(menuWebView.contains("guard isProductMapSurface else { return }"), "Comms must not be forced into Product Map")
    }

    func testStandaloneMacCommsRoutePreservesOriginAndExactHash() {
        XCTAssertEqual(
            MacCommsView.targetURL(baseURLText: EndpointTestFixtures.loopbackOrigin)?.absoluteString,
            EndpointTestFixtures.loopbackUnifiedComms
        )
        XCTAssertEqual(
            MacCommsView.targetURL(baseURLText: "https://coord.example.test/base")?.absoluteString,
            "https://coord.example.test/?embedded=1#v=comms"
        )
        XCTAssertNil(MacCommsView.targetURL(baseURLText: "file:///tmp/coord"))
    }
}
