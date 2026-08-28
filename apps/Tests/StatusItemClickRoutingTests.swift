import CoreGraphics
import XCTest
@testable import CoordCockpitMac

final class StatusItemClickRoutingTests: XCTestCase {
    func testEveryHorizontalLocationRoutesToPrimaryPanel() {
        let bounds = CGRect(x: 0, y: 0, width: 180, height: 24)
        for x in stride(from: -20.0, through: 200.0, by: 0.25) {
            XCTAssertEqual(
                StatusItemClickRouting.leftClickDestination(locationX: x, buttonBounds: bounds),
                .primaryPanel,
                "x=\(x)"
            )
        }
    }

    func testControllerHasNoStatsHitRegionOrTransientCallback() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        let controller = try String(contentsOf: root.appendingPathComponent("menubar/Sources/App/StatusItemController.swift"))
        let delegate = try String(contentsOf: root.appendingPathComponent("menubar/Sources/App/AppDelegate.swift"))

        XCTAssertTrue(controller.contains("guard let button = item.button else { return }\n        onClick?(button)"))
        XCTAssertFalse(controller.contains("onStatsClick"))
        XCTAssertFalse(controller.contains("clickedSystemTelemetrySegment"))
        XCTAssertFalse(controller.contains("titleRect(forBounds"))
        XCTAssertFalse(delegate.contains("statsPopover"))
        XCTAssertFalse(delegate.contains("toggleStatsPopover"))
    }

    func testStatsRowOnlyTogglesInlineDetailInsidePrimaryPanel() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        let popover = try String(contentsOf: root.appendingPathComponent("menubar/Sources/App/PopoverController.swift"))
        let rows = try String(contentsOf: root.appendingPathComponent("menubar/Sources/UI/ContentStackAndRows.swift"))

        XCTAssertTrue(rows.contains("self.systemTelemetryExpanded.toggle()"))
        XCTAssertTrue(rows.contains("let detail = InlineSystemTelemetryDetailView(snapshot: systemTelemetry, config: config, history: systemTelemetryHistory.samples)"))
        XCTAssertTrue(rows.contains("place(detail)"))
        XCTAssertFalse(rows.contains(".openSystemTelemetry"))
        XCTAssertFalse(popover.contains("showSystemTelemetry()"))
        XCTAssertFalse(popover.contains("showingSystemTelemetry"))
    }

    func testCompactStatusGeometryStacksTelemetryAndHasNoTrailingPadding() {
        let baseWidth: CGFloat = 56
        let compactWidth = StatusItemImageLayout.imageWidth(
            baseWidth: baseWidth,
            moduleCount: 3,
            compact: true
        )
        XCTAssertEqual(compactWidth, 162)
        XCTAssertEqual(
            StatusItemImageLayout.telemetryTotalWidth(moduleCount: 3, compact: false),
            132
        )

        let frames = (0..<3).map {
            StatusItemImageLayout.telemetryFrame(index: $0, baseWidth: baseWidth, compact: true)
        }
        XCTAssertEqual(frames.map(\.minX), [64, 98, 132])
        XCTAssertEqual(frames.last?.maxX, compactWidth, "the image ends at the final value column")
        XCTAssertEqual(frames[1].minX - frames[0].maxX, 4)
        for frame in frames {
            let label = StatusItemImageLayout.telemetryLabelFrame(in: frame)
            let value = StatusItemImageLayout.telemetryValueFrame(in: frame)
            XCTAssertGreaterThan(label.midY, value.midY, "GPU/RAM/CPU label must sit above its percent")
            XCTAssertLessThanOrEqual(label.maxY, StatusItemImageLayout.height)
            XCTAssertGreaterThanOrEqual(value.minY, 0)
        }
    }

    func testQuotaAndTelemetryUseOneImageOnlyAccessibleClickTarget() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        let controller = try String(contentsOf: root.appendingPathComponent("menubar/Sources/App/StatusItemController.swift"))
        let renderer = try String(contentsOf: root.appendingPathComponent("menubar/Sources/UI/RingRenderer.swift"))
        let render = try XCTUnwrap(controller.components(separatedBy: "private func render()").dropFirst().first?.components(separatedBy: "private func systemTelemetryPresentations").first)

        XCTAssertTrue(render.contains("button.imagePosition = .imageOnly"))
        XCTAssertTrue(render.contains("button.attributedTitle = NSAttributedString()"))
        XCTAssertTrue(render.contains("item.length = resolvedImage?.size.width"))
        XCTAssertTrue(render.contains("button.setAccessibilityLabel(accessibility)"))
        XCTAssertTrue(render.contains("button.setAccessibilityHelp("))
        XCTAssertFalse(render.contains("StatusItemTaskSelection.topRow"))
        XCTAssertFalse(render.contains("No active task"))
        XCTAssertFalse(render.contains("Active task"))
        XCTAssertFalse(renderer.contains("task.compactLabel"))

        let gpu = try XCTUnwrap(controller.range(of: "(\"GPU\","))
        let ram = try XCTUnwrap(controller.range(of: "(\"RAM\",", range: gpu.upperBound..<controller.endIndex))
        let cpu = try XCTUnwrap(controller.range(of: "(\"CPU\",", range: ram.upperBound..<controller.endIndex))
        XCTAssertLessThan(gpu.lowerBound, ram.lowerBound)
        XCTAssertLessThan(ram.lowerBound, cpu.lowerBound)
    }

    func testContextMenuSettingsUsesDedicatedNonToggleRoute() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        let status = try String(contentsOf: root.appendingPathComponent("menubar/Sources/App/StatusItemController.swift"))
        let delegate = try String(contentsOf: root.appendingPathComponent("menubar/Sources/App/AppDelegate.swift"))
        let popover = try String(contentsOf: root.appendingPathComponent("menubar/Sources/App/PopoverController.swift"))

        XCTAssertTrue(status.contains("var onOpenSettings: (() -> Void)?"))
        XCTAssertTrue(status.contains("makeItem(\"Settings…\", #selector(menuOpenSettings))"))
        XCTAssertTrue(status.contains("afterMenuDismissal { [weak self] in self?.onOpenSettings?() }"))
        XCTAssertTrue(delegate.contains("statusItem.onOpenSettings = { [weak self] in self?.openSettingsFromStatusMenu() }"))

        let settingsRoute = try XCTUnwrap(
            delegate.components(separatedBy: "private func openSettingsFromStatusMenu()").dropFirst().first?
                .components(separatedBy: "private func showPopover").first
        )
        XCTAssertTrue(settingsRoute.contains("popover.showSettings(relativeTo: button)"))
        XCTAssertFalse(settingsRoute.contains("togglePopover"), "menu navigation must not race the primary-click toggle")
        XCTAssertTrue(popover.contains("func showSettings(relativeTo button: NSStatusBarButton)"))
    }

    func testEveryPanelClosePathResetsSecondaryNavigation() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        let source = try String(contentsOf: root.appendingPathComponent("menubar/Sources/App/PopoverController.swift"))

        XCTAssertTrue(source.contains("struct PopoverNavigationState"))
        XCTAssertTrue(source.contains("mutating func reset() { destination = .main }"))
        XCTAssertTrue(source.contains("func show(relativeTo button: NSStatusBarButton) {\n        resetNavigationToMain(renderMain: true)"))
        XCTAssertTrue(source.contains("func close() {\n        resetNavigationToMain(renderMain: false)"))
        XCTAssertTrue(source.contains("private func detachedWindowDidClose() {\n        resetNavigationToMain(renderMain: false)"))
        XCTAssertTrue(source.contains("private lazy var popoverDelegate = PopoverDelegate { [weak self] in\n        self?.resetNavigationToMain(renderMain: false)"))
        XCTAssertGreaterThanOrEqual(
            source.components(separatedBy: "resetNavigationToMain(renderMain: false)").count - 1,
            4
        )
    }
}
