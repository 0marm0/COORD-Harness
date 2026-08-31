import AppKit
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

        XCTAssertFalse(popover.contains("let costLabel = UI.label(\"Cost\""))
        XCTAssertTrue(popover.contains("weight: .regular"))
        XCTAssertTrue(popover.contains("costValue.font = .monospacedDigitSystemFont(ofSize: 8.5, weight: .regular)"))
        XCTAssertTrue(popover.contains("private static let costValueWidth: CGFloat = 132"))
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
        let tokens = try String(
            contentsOf: root.appendingPathComponent("apps/menubar/Sources/App/DesignTokens.swift"),
            encoding: .utf8
        )

        XCTAssertTrue(content.contains("private static let providerIconX: CGFloat = Tokens.Layout.rowPadL"))
        XCTAssertTrue(content.contains("private static let quotaLabelBarGap: CGFloat = 4"))
        XCTAssertTrue(content.contains("private static var quotaTrackX: CGFloat { quotaLabelX + quotaLabelWidth + quotaLabelBarGap }"))
        XCTAssertTrue(content.contains("private static let quotaTrackWidth: CGFloat = 119"))
        XCTAssertTrue(content.contains("private static let resetWidth: CGFloat = 64"))
        XCTAssertTrue(content.contains("private static let runoutWidth: CGFloat = 64"))
        XCTAssertTrue(content.contains("align: .center)"))
        XCTAssertTrue(content.contains("reset.font = .monospacedDigitSystemFont(ofSize: 8.5, weight: .medium)"))
        XCTAssertTrue(content.contains("runout.font = .monospacedDigitSystemFont(ofSize: 8.5, weight: .medium)"))
        XCTAssertTrue(content.contains("color: Tokens.Color.lightGray, align: .right"))
        XCTAssertTrue(content.contains("width: max(1, track.bounds.width * CGFloat(clamped) / 100)"))
        XCTAssertTrue(content.contains("private static let telemetryFollowupGap: CGFloat = 4"))
        XCTAssertTrue(content.contains("y += Self.telemetryFollowupGap"))

        XCTAssertTrue(telemetry.contains("let contentWidth = panelWidth"))
        XCTAssertTrue(telemetry.contains("let groupX = max(0, (contentWidth - groupWidth) / 2)"))
        XCTAssertTrue(telemetry.contains("let metricFrames = Self.metricFrames(metricCount: metrics.count, panelWidth: bounds.width)"))
        XCTAssertTrue(telemetry.contains("y: 4, width: frame.width, height: 11"))
        XCTAssertTrue(telemetry.contains("y: 17, width: frame.width, height: 13"))
        XCTAssertTrue(telemetry.contains("for (metric, frame) in zip(metrics, metricFrames)"))
        XCTAssertEqual(telemetry.components(separatedBy: "align: .center").count - 1, 2)
        XCTAssertFalse(telemetry.contains("let cellWidth ="))
        XCTAssertTrue(tokens.contains("static let headerHeight: CGFloat = 44"))
        XCTAssertTrue(tokens.contains("static let wordmarkH: CGFloat   = 28"))
        XCTAssertTrue(tokens.contains("static let headerControlsWidth: CGFloat = CoordPowerControlsLayout.width"))
        XCTAssertTrue(tokens.contains("static let headerControlsHeight: CGFloat = CoordPowerControlsLayout.height"))
        XCTAssertTrue(tokens.contains("static let footerHeight: CGFloat = 34"))
        XCTAssertTrue(tokens.contains("static let footerToolRailWidth: CGFloat = 84"))
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
        XCTAssertTrue(settings.contains("cfg.usageBarPalette = represented(usagePalettePopup"))
        XCTAssertTrue(settings.contains("cfg.systemTelemetryCompactSpacing = represented(telemetrySpacingPopup"))
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
        let statsItem = try String(
            contentsOf: root.appendingPathComponent("apps/menubar/Sources/App/StatsStatusItemController.swift"),
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
        XCTAssertFalse(stats.contains("showsCloseControl: false"))
        XCTAssertTrue(stats.contains("popover.show(relativeTo: button.bounds"))
        XCTAssertFalse(stats.contains("styleMask: [.titled"))
        XCTAssertTrue(statusItem.contains("onClick?(button)"))
        XCTAssertTrue(statusItem.contains("let telemetry: [RingRenderer.TelemetryPresentation] = []"))
        XCTAssertFalse(statusItem.contains("clickedSystemTelemetrySegment"))
        XCTAssertFalse(statusItem.contains("onStatsClick"))
        XCTAssertFalse(delegate.contains("statusItem.onStatsClick"))
        XCTAssertTrue(statsItem.contains("onClick?(button)"))
        XCTAssertTrue(statsItem.contains("size: 12,"))
        XCTAssertTrue(statsItem.contains("weight: .regular"))
        XCTAssertTrue(delegate.contains("statsPopover.toggle("))
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
        XCTAssertTrue(content.contains("func resetSystemTelemetryDisclosure() {"))
        XCTAssertTrue(content.contains("FooterTelemetryBridge.shared.expanded = false"))
        XCTAssertGreaterThanOrEqual(popover.components(separatedBy: "resetSystemTelemetryDisclosure()").count - 1, 4)
        XCTAssertFalse(content.contains("config.systemTelemetryInPopover"))
        XCTAssertFalse(content.contains("systemTelemetryExpanded = config."))
        XCTAssertTrue(telemetry.contains(".volumeAvailableCapacityForImportantUsageKey"))
        XCTAssertTrue(telemetry.contains("importantUsageAvailableBytes ?? availableBytes"))
    }

    func testStandaloneStatsDetailMaintainsPublicContract() throws {
        let coordRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let premium = try String(
            contentsOf: coordRoot.appendingPathComponent("apps/menubar/Sources/UI/PremiumSystemTelemetryDetailView.swift"),
            encoding: .utf8
        )
        let routing = try String(
            contentsOf: coordRoot.appendingPathComponent("apps/menubar/Sources/UI/SystemTelemetryDetailView.swift"),
            encoding: .utf8
        )

        for token in [
            "static let preferredWidth: CGFloat = 276",
            "static let preferredHeight: CGFloat = 472",
            "static let minimumWidth: CGFloat = 268",
            "static let batteryWidth: CGFloat = 72",
            "static let batteryHeight: CGFloat = 16",
            "static let moduleHeight: CGFloat = 88",
            "static let utilizationHeight: CGFloat = 60",
            #"static let moduleOrder = ["RAM", "GPU", "CPU", "DISK"]"#,
        ] {
            XCTAssertTrue(premium.contains(token), "COORD Stats contract lost \(token)")
        }
        XCTAssertTrue(premium.contains("static let powerToolbarHeight: CGFloat = 24"))
        XCTAssertTrue(premium.contains("static let ringDiameter: CGFloat = 56"))
        for token in [
            "metricModules()",
            #"("CPU", snapshot?.cpu.availablePercent"#,
            #"("GPU", snapshot?.gpu.availablePercent"#,
            #"("RAM", composition.centerUsedPercent"#,
            #"("DISK", diskPercent"#,
            "PremiumSegmentedMemoryRing",
            "SystemTelemetrySnapshot.MemoryRingComposition.make",
            "Tokens.Color.statsWarningOrange",
            "PremiumTelemetrySurface",
            "PremiumUtilizationHistoryView",
            "let bodyRect = NSRect",
            "NSColor.systemGreen.withAlphaComponent(0.82)",
            "let terminal = NSBezierPath",
            "PremiumStatsEnergyModeButton",
            "LocalEnergyMode.allCases",
            #"identifier = NSUserInterfaceItemIdentifier("coord.stats.energy-mode."#,
            "contentTintColor = selected ? .white : Tokens.Color.lightGray.withAlphaComponent(0.32)",
            "private let backgroundGlass = NSVisualEffectView()",
            "backgroundGlass.material = .hudWindow",
            "backgroundGlass.blendingMode = .behindWindow",
            "calibratedRed: 0.018, green: 0.021, blue: 0.026, alpha: 0.92",
        ] {
            XCTAssertTrue(premium.contains(token), "COORD compact Stats lost \(token)")
        }

        let coordStart = try XCTUnwrap(premium.range(of: "private func rebuild()"))
        let coordEnd = try XCTUnwrap(premium.range(of: "private var cpuFacts:", range: coordStart.upperBound..<premium.endIndex))
        let coordStandalone = String(premium[coordStart.lowerBound..<coordEnd.lowerBound])
        for removed in ["freshness", "cadence", "Unavailable", "ageSeconds", "metadata()"] {
            XCTAssertFalse(coordStandalone.contains(removed))
        }
        XCTAssertFalse(coordStandalone.contains("addLegend"))
        XCTAssertFalse(coordStandalone.contains("PremiumTelemetryPanel"))
        XCTAssertFalse(premium.contains("private let selected: Bool"))
        XCTAssertTrue(premium.contains("focusRingType = .none"))
        for label in ["App", "Wired", "Compressed", "Free"] {
            XCTAssertTrue(premium.contains("(\"\(label)\", composition."))
        }
        XCTAssertFalse(premium.contains("(\"Swap\", composition.swapUsedBytes"))
        XCTAssertEqual(coordStandalone.components(separatedBy: "arrow.clockwise").count - 1, 0)

        let coordRAM = try XCTUnwrap(coordStandalone.range(of: #"("RAM","#))
        let coordGPU = try XCTUnwrap(coordStandalone.range(of: #"("GPU","#))
        let coordCPU = try XCTUnwrap(coordStandalone.range(of: #"("CPU","#))
        let coordDisk = try XCTUnwrap(coordStandalone.range(of: #"("DISK","#))
        XCTAssertLessThan(coordRAM.lowerBound, coordGPU.lowerBound)
        XCTAssertLessThan(coordGPU.lowerBound, coordCPU.lowerBound)
        XCTAssertLessThan(coordCPU.lowerBound, coordDisk.lowerBound)
        XCTAssertTrue(coordStandalone.contains(#"("Read", snapshot?.disk.readBps.map(SystemTelemetryDetailFormatter.rate)"#))
        XCTAssertTrue(coordStandalone.contains(#"("Write", snapshot?.disk.writeBps.map(SystemTelemetryDetailFormatter.rate)"#))
        XCTAssertFalse(coordStandalone.contains(#"("R / W","#))

        let premiumContractEnd = try XCTUnwrap(premium.range(of: "final class PremiumSystemTelemetryDetailView"))
        XCTAssertFalse(premium[..<premiumContractEnd.lowerBound].contains("\"light\""))

        let route = try XCTUnwrap(routing.range(of: "final class TransientSystemTelemetryPopover"))
        let routeSource = String(routing[route.lowerBound...])
        XCTAssertTrue(routeSource.contains("SystemTelemetryStatsLayoutContract.minimumWidth"))
        XCTAssertTrue(routeSource.contains("max(404, min(SystemTelemetryStatsLayoutContract.preferredHeight"))
        XCTAssertTrue(routeSource.contains("onToggleChargeLimit"))
        XCTAssertTrue(routeSource.contains("onSetEnergyMode"))
        XCTAssertTrue(routeSource.contains("let detail = PremiumSystemTelemetryDetailView("))
        XCTAssertFalse(routeSource.contains("onRefresh"))
        XCTAssertFalse(routeSource.contains("let detail = SystemTelemetryDetailView("))

        let coordStatus = try String(
            contentsOf: coordRoot.appendingPathComponent("apps/menubar/Sources/App/StatusItemController.swift"),
            encoding: .utf8
        )
        XCTAssertTrue(coordStatus.contains(#"percent.map { "\(Int($0.rounded()))" }"#))
        XCTAssertFalse(coordStatus.contains(#"percent.map { "\(Int($0.rounded()))%" }"#))

        let coordStatsStatus = try String(
            contentsOf: coordRoot.appendingPathComponent("apps/menubar/Sources/App/StatsStatusItemController.swift"),
            encoding: .utf8
        )
        XCTAssertTrue(coordStatsStatus.contains(#"percent.map { "\(Int($0.rounded()))" }"#))
        XCTAssertFalse(coordStatsStatus.contains(#"percent.map { "\(Int($0.rounded()))%" }"#))
    }

    func testCompactStatsGeometryFitsMinimumWidth() {
        let bounds = NSRect(x: 0, y: 0, width: 268, height: 472)
        let selectorWidth: CGFloat = 70
        let frames = [
            NSRect(x: (bounds.width - 72) / 2, y: 6, width: 72, height: 16),
            NSRect(x: bounds.width - selectorWidth - 8, y: 4, width: selectorWidth, height: 18),
            NSRect(x: 10, y: 34, width: bounds.width - 20, height: 88 * 4),
            NSRect(x: 10, y: 394, width: bounds.width - 20, height: 60),
        ]
        for frame in frames {
            XCTAssertGreaterThanOrEqual(frame.minX, bounds.minX)
            XCTAssertGreaterThanOrEqual(frame.minY, bounds.minY)
            XCTAssertLessThanOrEqual(frame.maxX, bounds.maxX)
            XCTAssertLessThanOrEqual(frame.maxY, bounds.maxY)
        }
    }



}
