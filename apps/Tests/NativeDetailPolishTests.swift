import Foundation
import XCTest

final class NativeDetailPolishTests: XCTestCase {
    func testUsageDetailIsDataFirstAndCostHierarchyIsQuiet() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let usage = try String(
            contentsOf: root.appendingPathComponent("apps/Shared/Sources/UsageDashboardContent.swift"),
            encoding: .utf8
        )
        let popover = try String(
            contentsOf: root.appendingPathComponent("apps/menubar/Sources/UI/ContentStackAndRows.swift"),
            encoding: .utf8
        )

        XCTAssertFalse(usage.contains("Total Cost"))
        XCTAssertFalse(popover.contains("Total Cost"))
        XCTAssertTrue(usage.contains("label: \"Cost\""))
        XCTAssertTrue(usage.contains("VStack(alignment: .leading, spacing: 9)"))
        XCTAssertTrue(usage.contains(".callout.weight(.medium).monospacedDigit()"))
        XCTAssertTrue(usage.contains(".accessibilityHint(detail)"))
        XCTAssertFalse(usage.contains("Quota progress, observed token history"))

        let compactStart = try XCTUnwrap(usage.range(of: "private struct UsageCompactMetric"))
        let compactEnd = try XCTUnwrap(usage.range(of: "private struct UsageSectionTitle"))
        XCTAssertFalse(String(usage[compactStart.lowerBound..<compactEnd.lowerBound]).contains("Text(detail)"))

        let sectionStart = compactEnd
        let sectionEnd = try XCTUnwrap(usage.range(of: "private struct UsageQuotaRow"))
        XCTAssertFalse(String(usage[sectionStart.lowerBound..<sectionEnd.lowerBound]).contains("Text(detail)"))

        XCTAssertTrue(popover.contains("let costLabel = UI.label(\"Cost\""))
        XCTAssertTrue(popover.contains("weight: .regular"))
        XCTAssertTrue(popover.contains("costValue.font = .monospacedDigitSystemFont(ofSize: 8.5, weight: .regular)"))
        XCTAssertTrue(popover.contains("private static let costLabelValueGap: CGFloat = 12"))
    }


    func testMenuPanelUsageAndTelemetryGeometryIsCompactAndCentered() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let content = try String(
            contentsOf: root.appendingPathComponent("apps/menubar/Sources/UI/ContentStackAndRows.swift"),
            encoding: .utf8
        )
        let telemetry = try String(
            contentsOf: root.appendingPathComponent("apps/menubar/Sources/UI/SystemTelemetryRow.swift"),
            encoding: .utf8
        )

        XCTAssertTrue(content.contains("private static let quotaLabelBarGap: CGFloat = 4"))
        XCTAssertTrue(content.contains("private static var quotaTrackX: CGFloat { quotaLabelX + quotaLabelWidth + quotaLabelBarGap }"))
        XCTAssertTrue(content.contains("color: Tokens.Color.lightGray, align: .right"))
        XCTAssertTrue(content.contains("width: max(1, track.bounds.width * CGFloat(clamped) / 100)"))
        XCTAssertTrue(content.contains("private static let telemetryFollowupGap: CGFloat = 4"))
        XCTAssertTrue(content.contains("y += Self.telemetryFollowupGap"))

        XCTAssertTrue(telemetry.contains("let contentWidth = panelWidth - Tokens.Layout.rowPadL - Tokens.Layout.rowPadR"))
        XCTAssertTrue(telemetry.contains("let groupX = Tokens.Layout.rowPadL + max(0, (contentWidth - groupWidth) / 2)"))
        XCTAssertTrue(telemetry.contains("let metricFrames = Self.metricFrames(metricCount: metrics.count, panelWidth: bounds.width)"))
        XCTAssertTrue(telemetry.contains("for (metric, frame) in zip(metrics, metricFrames)"))
        XCTAssertEqual(telemetry.components(separatedBy: "align: .center").count - 1, 2)
        XCTAssertFalse(telemetry.contains("let cellWidth ="))
    }


    func testR9PaletteSpacingAndInlineStatsContracts() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        func source(_ path: String) throws -> String {
            try String(contentsOf: root.appendingPathComponent(path), encoding: .utf8)
        }
        let config = try source("apps/menubar/Sources/Data/Config.swift")
        let settings = try source("apps/menubar/Sources/UI/SettingsView.swift")
        let content = try source("apps/menubar/Sources/UI/ContentStackAndRows.swift")
        let row = try source("apps/menubar/Sources/UI/SystemTelemetryRow.swift")
        let detail = try source("apps/menubar/Sources/UI/SystemTelemetryDetailView.swift")
        let status = try source("apps/menubar/Sources/App/StatusItemController.swift")
        let renderer = try source("apps/menubar/Sources/UI/RingRenderer.swift")
        let popover = try source("apps/menubar/Sources/App/PopoverController.swift")
        let shared = try source("apps/Shared/Sources/UsageDashboardContent.swift")
        let web = try source("src/coordharness/board/static/usage-dashboard.js")
        let webCSS = try source("src/coordharness/board/static/usage-dashboard.css")
        let idleTelemetryRow = row.components(
            separatedBy: "private final class TelemetryOpenButton"
        )[0]
        XCTAssertFalse(idleTelemetryRow.contains("backgroundColor"))
        XCTAssertTrue(row.contains("layer?.backgroundColor = NSColor.clear.cgColor"))
        XCTAssertTrue(row.contains("override func mouseEntered"))

        XCTAssertTrue(config.contains("var usageBarPalette: String = UsageBarPalette.colored.rawValue"))
        XCTAssertTrue(config.contains("var systemTelemetryCompactSpacing: Bool = true"))
        XCTAssertTrue(config.contains("decodeIfPresent(String.self, forKey: .usageBarPalette)"))
        XCTAssertTrue(config.contains("decodeIfPresent(Bool.self, forKey: .systemTelemetryCompactSpacing)"))
        XCTAssertTrue(settings.contains("for popup in [telemetryProfilePopup, telemetrySpacingPopup"))
        XCTAssertTrue(settings.contains("cfg.usageBarPalette = usagePalettePopup.indexOfSelectedItem == 0"))
        XCTAssertTrue(settings.contains("cfg.systemTelemetryCompactSpacing = telemetrySpacingPopup.indexOfSelectedItem == 0"))
        XCTAssertTrue(settings.contains("cfg.save()"))

        XCTAssertTrue(content.contains("barPalette: UsageBarPalette.resolve(config.usageBarPalette)"))
        XCTAssertTrue(content.contains("barPalette == .colored ? color : NSColor.labelColor.withAlphaComponent(0.82)"))
        XCTAssertTrue(renderer.contains("palette == .neutral ? NSColor.labelColor.withAlphaComponent(0.82) : providerColor(identity)"))
        XCTAssertTrue(renderer.contains("drawProviderMark("))
        XCTAssertTrue(status.contains("compactTelemetrySpacing: systemTelemetryCompactSpacing"))
        XCTAssertTrue(status.contains("button.imagePosition = .imageOnly"))

        for metric in ["CPU", "GPU", "RAM"] {
            XCTAssertTrue(row.contains("(\"" + metric + "\", snapshot?."))
        }
        XCTAssertTrue(row.contains("(\"DISK\", userVisibleDisk?.usedPercent"))
        XCTAssertFalse(content.contains("config.systemTelemetryEnabled && config.systemTelemetryInPopover"))
        XCTAssertTrue(content.contains("self.systemTelemetryExpanded.toggle()"))
        XCTAssertTrue(detail.contains("final class InlineSystemTelemetryDetailView"))
        XCTAssertTrue(detail.contains("(\"CPU\", snapshot?.cpu.availablePercent"))
        XCTAssertTrue(detail.contains("(\"DISK\", diskPercent"))
        XCTAssertFalse(popover.contains("showingSystemTelemetry"))
        XCTAssertFalse(popover.contains("private func showSystemTelemetry"))

        XCTAssertTrue(shared.contains("var barPalette: UsageBarPalette = .colored"))
        XCTAssertTrue(shared.contains(".font(.system(size: 8.5, weight: .regular).monospacedDigit())"))
        XCTAssertTrue(web.contains("class=\"usage-strip-cost\"><small>Cost</small>"))
        XCTAssertTrue(webCSS.contains(".usage-strip-cost{display:inline-flex;align-items:baseline;gap:.55rem"))
    }

    func testStatsDetailUsesOneTitleAndQuietFreshnessMetadata() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let stats = try String(
            contentsOf: root.appendingPathComponent("apps/menubar/Sources/UI/SystemTelemetryDetailView.swift"),
            encoding: .utf8
        )
        let statusItem = try String(
            contentsOf: root.appendingPathComponent("apps/menubar/Sources/App/StatusItemController.swift"),
            encoding: .utf8
        )
        let delegate = try String(
            contentsOf: root.appendingPathComponent("apps/menubar/Sources/App/AppDelegate.swift"),
            encoding: .utf8
        )

        XCTAssertTrue(stats.contains("addLabel(\"System stats\""))
        XCTAssertFalse(stats.contains("Machine telemetry"))
        XCTAssertFalse(stats.contains("COORD  /  SYSTEM STATS"))
        XCTAssertFalse(stats.contains("badgeView"))
        XCTAssertTrue(stats.contains("snapshot?.freshness?.state.lowercased()"))
        XCTAssertTrue(stats.contains("footerParts.joined(separator: \"  •  \")"))
        XCTAssertTrue(stats.contains("SOURCE  "))
        XCTAssertTrue(stats.contains("final class TransientSystemTelemetryPopover"))
        XCTAssertTrue(stats.contains("popover.behavior = .transient"))
        XCTAssertTrue(stats.contains("showsCloseControl: false"))
        XCTAssertTrue(stats.contains("popover.show(relativeTo: button.bounds"))
        XCTAssertFalse(stats.contains("styleMask: [.titled"))
        XCTAssertTrue(statusItem.contains("onClick?(button)"))
        XCTAssertFalse(statusItem.contains("clickedSystemTelemetrySegment"))
        XCTAssertFalse(statusItem.contains("onStatsClick"))
        XCTAssertFalse(delegate.contains("statusItem.onStatsClick"))
        XCTAssertFalse(delegate.contains("statsPopover.toggle("))
        XCTAssertTrue(delegate.contains("statusItem.onClick"))
    }
    func testInlineStatsUsesRingsHistoryAndPerOpenDisclosureState() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        func source(_ path: String) throws -> String {
            try String(contentsOf: root.appendingPathComponent(path), encoding: .utf8)
        }
        let detail = try source("apps/menubar/Sources/UI/SystemTelemetryDetailView.swift")
        let content = try source("apps/menubar/Sources/UI/ContentStackAndRows.swift")
        let popover = try source("apps/menubar/Sources/App/PopoverController.swift")
        let telemetry = try source("apps/Shared/Sources/SystemTelemetry.swift")

        XCTAssertTrue(detail.contains("private final class TelemetryRingView"))
        XCTAssertTrue(detail.contains("private final class TelemetryBarHistoryView"))
        XCTAssertTrue(detail.contains("private final class TelemetryIOHistoryView"))
        XCTAssertTrue(detail.contains("private final class TelemetryCoreLoadView"))
        XCTAssertTrue(detail.contains("pCore: cpu.pCoreUsagePercent"))
        XCTAssertTrue(detail.contains("eCore: cpu.eCoreUsagePercent"))
        XCTAssertGreaterThanOrEqual(detail.components(separatedBy: "layer?.backgroundColor = NSColor.clear.cgColor").count - 1, 4)
        XCTAssertFalse(detail.contains("module.layer?.backgroundColor = NSColor(calibratedWhite:"))
        XCTAssertFalse(detail.contains("card.layer?.backgroundColor = NSColor(calibratedWhite: 0.075"))
        XCTAssertFalse(detail.contains("card.layer?.backgroundColor = NSColor(calibratedWhite: 0.09"))
        XCTAssertTrue(detail.contains("separator.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.09).cgColor"))
        XCTAssertTrue(detail.contains("static let capacity = 30"))
        XCTAssertTrue(detail.contains("CPU HISTORY"))
        XCTAssertTrue(detail.contains("DISK I/O"))
        XCTAssertTrue(detail.contains("I/O history unavailable"))
        XCTAssertTrue(content.contains("private var systemTelemetryExpanded = false"))
        XCTAssertTrue(content.contains("systemTelemetryHistory.append(snapshot)"))
        XCTAssertTrue(content.contains("func resetSystemTelemetryDisclosure() { systemTelemetryExpanded = false }"))
        XCTAssertGreaterThanOrEqual(popover.components(separatedBy: "resetSystemTelemetryDisclosure()").count - 1, 4)
        XCTAssertFalse(content.contains("config.systemTelemetryInPopover"))
        XCTAssertFalse(content.contains("systemTelemetryExpanded = config."))
        XCTAssertTrue(telemetry.contains(".volumeAvailableCapacityForImportantUsageKey"))
        XCTAssertTrue(telemetry.contains("importantUsageAvailableBytes ?? availableBytes"))
    }

}
