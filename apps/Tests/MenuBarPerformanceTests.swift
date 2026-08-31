import Foundation
import XCTest

final class MenuBarPerformanceTests: XCTestCase {
    private var projectRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }

    private func source(_ path: String) throws -> String {
        try String(contentsOf: projectRoot.appendingPathComponent(path), encoding: .utf8)
    }

    func testTelemetryTicksUpdateRetainedViewsWithoutRebuildingPanel() throws {
        let content = try source("apps/menubar/Sources/UI/ContentStackAndRows.swift")
        let popover = try source("apps/menubar/Sources/App/PopoverController.swift")
        let telemetryUpdate = try XCTUnwrap(
            popover.range(of: "func updateSystemTelemetry(_ snapshot: SystemTelemetrySnapshot?)")
        )
        let nextUpdate = try XCTUnwrap(popover.range(of: "func updateLocalPower(", range: telemetryUpdate.upperBound..<popover.endIndex))
        let telemetryBody = String(popover[telemetryUpdate.lowerBound..<nextUpdate.lowerBound])

        XCTAssertTrue(content.contains("FooterTelemetryBridge.shared.update(snapshot: snapshot)"))
        XCTAssertTrue(content.contains("private weak var inlineSystemTelemetryDetail: InlineSystemTelemetryDetailView?"))
        XCTAssertTrue(content.contains("inlineSystemTelemetryDetail?.update(snapshot: snapshot"))
        XCTAssertFalse(telemetryBody.contains("render(lastState)"))
        XCTAssertFalse(telemetryBody.contains("swapIn"))
    }

    func testSnapshotRefreshIsOffMainAndCoalescesTimerBursts() throws {
        let delegate = try source("apps/menubar/Sources/App/AppDelegate.swift")

        XCTAssertTrue(delegate.contains("private var snapshotRefreshInFlight = false"))
        XCTAssertTrue(delegate.contains("private var snapshotRefreshPending = false"))
        XCTAssertTrue(delegate.contains("guard !snapshotRefreshInFlight else"))
        XCTAssertTrue(delegate.contains("Task.detached(priority: .utility)"))
        XCTAssertTrue(delegate.contains("let state = await requestedSource.current()"))
        XCTAssertTrue(delegate.contains("await MainActor.run"))
        XCTAssertTrue(delegate.contains("if source === requestedSource"))
    }

    func testUnchangedStateAndStableAnchorAvoidScrollChurn() throws {
        let popover = try source("apps/menubar/Sources/App/PopoverController.swift")
        let rows = try source("apps/menubar/Sources/UI/Rows.swift")

        XCTAssertTrue(popover.contains("guard renderedStateSignature != signature else { return }"))
        XCTAssertTrue(popover.contains("stable.ts = nil"))
        XCTAssertTrue(popover.contains("stable.diagnostics?.projectionTs = nil"))
        XCTAssertTrue(popover.contains("stable.healthSummary?.generatedAt = nil"))
        XCTAssertTrue(popover.contains("captureScrollAnchor(document: content"))
        XCTAssertTrue(popover.contains("restoredScrollOrigin("))
        XCTAssertTrue(rows.contains(#"identifier = NSUserInterfaceItemIdentifier("coord-row:" + key)"#))
    }

    func testCompactDefaultsAndExpandedStatsGeometry() throws {
        let config = try source("apps/menubar/Sources/Data/Config.swift")
        let status = try source("apps/menubar/Sources/App/StatusItemController.swift")
        let content = try source("apps/menubar/Sources/UI/ContentStackAndRows.swift")
        let row = try source("apps/menubar/Sources/UI/SystemTelemetryRow.swift")
        let detail = try source("apps/menubar/Sources/UI/SystemTelemetryDetailView.swift")

        XCTAssertTrue(config.contains("var systemTelemetryCompactSpacing: Bool = true"))
        XCTAssertTrue(config.contains("var systemTelemetrySpacingPreferenceVersion: Int = 1"))
        XCTAssertTrue(config.contains("storedTelemetrySpacingVersion == nil"))
        XCTAssertTrue(config.contains("persistTelemetrySpacingMigration"))
        XCTAssertTrue(status.contains("compactTelemetrySpacing: systemTelemetryCompactSpacing"))
        XCTAssertTrue(status.contains("button.imagePosition = .imageOnly"))
        XCTAssertTrue(status.contains("item.length = resolvedImage?.size.width"))
        XCTAssertTrue(content.contains("private static let telemetryFollowupGap: CGFloat = 4"))
        XCTAssertTrue(row.contains("height: Tokens.Layout.footerHeight"))
        XCTAssertTrue(row.contains("private static let metricGap: CGFloat = 8"))
        XCTAssertTrue(detail.contains("static let moduleHeight: CGFloat = 236"))
        XCTAssertTrue(detail.contains("height: Self.moduleHeight + 4"))
        XCTAssertEqual(detail.components(separatedBy: "height: 50").count - 1, 2)
        XCTAssertFalse(content.contains("systemTelemetryCompactSpacing"))
        XCTAssertFalse(detail.contains("systemTelemetryCompactSpacing"))
    }

    func testRefreshFrequencyCannotMultiplyFullStackBuilds() throws {
        let delegate = try source("apps/menubar/Sources/App/AppDelegate.swift")
        let popover = try source("apps/menubar/Sources/App/PopoverController.swift")

        XCTAssertTrue(delegate.contains(#"config.systemTelemetryProfile == "live" ? 2 : 5"#))
        XCTAssertTrue(delegate.contains("snapshotRefreshPending = true"))
        XCTAssertEqual(
            popover.components(separatedBy: "stack.updateSystemTelemetry(snapshot)").count - 1,
            1
        )
        let telemetryStart = try XCTUnwrap(popover.range(of: "func updateSystemTelemetry"))
        let telemetryEnd = try XCTUnwrap(popover.range(of: "func updateLocalPower", range: telemetryStart.upperBound..<popover.endIndex))
        XCTAssertEqual(
            String(popover[telemetryStart.lowerBound..<telemetryEnd.lowerBound])
                .components(separatedBy: "render(").count - 1,
            0
        )
    }
}
