import CoreGraphics
import XCTest
@testable import CoordCockpitMac

final class StatusItemClickRoutingTests: XCTestCase {
    func testOpeningClickCannotImmediatelyDismissPrimaryPopover() {
        let anchor = CGRect(x: 500, y: 900, width: 120, height: 24)
        XCTAssertFalse(
            PopoverClickPolicy.shouldCloseGlobalClick(
                location: CGPoint(x: 100, y: 100),
                anchorFrame: anchor,
                stayOpen: false,
                openedAt: 10,
                now: 10.1
            )
        )
        XCTAssertFalse(
            PopoverClickPolicy.shouldCloseGlobalClick(
                location: CGPoint(x: 100, y: 100),
                anchorFrame: anchor,
                stayOpen: false,
                openedAt: 10,
                now: 11.01
            ),
            "the measured delayed AXPress event must not dismiss the panel"
        )
        XCTAssertFalse(
            PopoverClickPolicy.shouldCloseGlobalClick(
                location: CGPoint(x: 100, y: 100),
                anchorFrame: anchor,
                stayOpen: false,
                openedAt: 10,
                now: 12
            )
        )
        XCTAssertTrue(
            PopoverClickPolicy.shouldCloseGlobalClick(
                location: CGPoint(x: 100, y: 100),
                anchorFrame: anchor,
                stayOpen: false,
                openedAt: 10,
                now: 14
            )
        )
        XCTAssertFalse(
            PopoverClickPolicy.shouldCloseGlobalClick(
                location: CGPoint(x: anchor.midX, y: anchor.midY),
                anchorFrame: anchor,
                stayOpen: false,
                openedAt: 10,
                now: 11
            )
        )
    }

    func testGlobalClickMonitorNormalizesWindowCoordinatesBeforeHitTesting() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        let source = try String(contentsOf: root.appendingPathComponent("menubar/Sources/App/PopoverController.swift"))

        XCTAssertTrue(source.contains("$0.convertPoint(toScreen: event.locationInWindow)"))
        XCTAssertTrue(source.contains("location: screenLocation"))
        XCTAssertFalse(source.contains("location: event.locationInWindow"))
    }

    func testAttachedCloseCallbackHasSingleOwner() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        let source = try String(contentsOf: root.appendingPathComponent("menubar/Sources/App/PopoverController.swift"))
        let closeBody = try XCTUnwrap(
            source.components(separatedBy: "func close() {").dropFirst().first?
                .components(separatedBy: "private func installClickMonitor()").first
        )

        XCTAssertTrue(closeBody.contains("if popover.isShown"))
        XCTAssertTrue(closeBody.contains("} else if detachedWindow?.isVisible == true"))
        XCTAssertEqual(
            closeBody.components(separatedBy: "popoverDidClose()").count - 1,
            1,
            "only detached orderOut needs a direct close callback; NSPopover uses its delegate"
        )
    }

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

    func testStatsOwnIndependentStatusItemAndDetailRoute() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        let controller = try String(contentsOf: root.appendingPathComponent("menubar/Sources/App/StatusItemController.swift"))
        let stats = try String(contentsOf: root.appendingPathComponent("menubar/Sources/App/StatsStatusItemController.swift"))
        let delegate = try String(contentsOf: root.appendingPathComponent("menubar/Sources/App/AppDelegate.swift"))

        XCTAssertTrue(controller.contains("guard let button = item.button else { return }\n        onClick?(button)"))
        XCTAssertFalse(controller.contains("onStatsClick"))
        XCTAssertFalse(controller.contains("clickedSystemTelemetrySegment"))
        XCTAssertFalse(controller.contains("titleRect(forBounds"))
        XCTAssertTrue(stats.contains("next.autosaveName = \"org.coordharness.menubar.stats\""))
        XCTAssertTrue(stats.contains("onClick?(button)"))
        XCTAssertTrue(delegate.contains("statsStatusItem.onClick"))
        XCTAssertTrue(delegate.contains("statsPopover.toggle("))
        XCTAssertTrue(delegate.contains("snapshot: self.telemetryStore.snapshot"))
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

    func testQuotaAndStatsUseIndependentAccessibleClickTargets() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        let controller = try String(contentsOf: root.appendingPathComponent("menubar/Sources/App/StatusItemController.swift"))
        let stats = try String(contentsOf: root.appendingPathComponent("menubar/Sources/App/StatsStatusItemController.swift"))
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

        XCTAssertTrue(render.contains("let telemetry: [RingRenderer.TelemetryPresentation] = []"))
        XCTAssertFalse(render.contains("showSystemTelemetry ? systemTelemetryPresentations()"))
        XCTAssertTrue(stats.contains("button.imagePosition = .imageOnly"))
        XCTAssertTrue(stats.contains("button.setAccessibilityLabel(label)"))
        XCTAssertTrue(stats.contains("button.setAccessibilityHelp("))
        let gpu = try XCTUnwrap(stats.range(of: "(\"GPU\","))
        let ram = try XCTUnwrap(stats.range(of: "(\"RAM\",", range: gpu.upperBound..<stats.endIndex))
        let cpu = try XCTUnwrap(stats.range(of: "(\"CPU\",", range: ram.upperBound..<stats.endIndex))
        XCTAssertLessThan(gpu.lowerBound, ram.lowerBound)
        XCTAssertLessThan(ram.lowerBound, cpu.lowerBound)

        let tokens = try String(contentsOf: root.appendingPathComponent("menubar/Sources/App/DesignTokens.swift"))
        XCTAssertTrue(tokens.contains("static let claudeOrange = rgb(0.95, 0.47, 0.24)"))
        XCTAssertTrue(tokens.contains("static let statsWarningOrange = rgb(0.90, 0.36, 0.16)"))
        XCTAssertTrue(stats.contains("case .normal: return .systemBlue"))
        XCTAssertTrue(stats.contains("case .warning: return Tokens.Color.statsWarningOrange"))
        XCTAssertTrue(stats.contains("case .critical: return .systemRed"))
        XCTAssertTrue(stats.contains("case .unavailable: return .secondaryLabelColor"))
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
