import Foundation
import XCTest

final class ConfigPersistenceTests: XCTestCase {
    func testFirstRunAtomicWriteCreatesParentAndFile() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("coord-config-persistence-\(UUID().uuidString)", isDirectory: true)
        let url = root.appendingPathComponent("nested/menubar_panel_config.json")
        defer { try? FileManager.default.removeItem(at: root) }

        XCTAssertFalse(FileManager.default.fileExists(atPath: root.path))
        let first = Data(#"{"usage_peek_collapsed":false}"#.utf8)
        try ConfigPersistence.write(first, to: url)
        XCTAssertEqual(try Data(contentsOf: url), first)

        let second = Data(#"{"usage_peek_collapsed":true}"#.utf8)
        try ConfigPersistence.write(second, to: url)
        XCTAssertEqual(try Data(contentsOf: url), second)
    }

    func testStatusTelemetryMigrationIsVersionedAndDefaultsOn() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        let source = try String(contentsOf: root.appendingPathComponent("menubar/Sources/Data/Config.swift"))
        XCTAssertTrue(source.contains("var systemTelemetryInStatusItem: Bool = true"))
        XCTAssertTrue(source.contains("var systemTelemetryStatusPreferenceVersion: Int = 1"))
        XCTAssertTrue(source.contains("let storedTelemetryVisibility"))
        XCTAssertTrue(source.contains("systemTelemetryInStatusItem = storedTelemetryVisibility ?? fallback.systemTelemetryInStatusItem"))
        XCTAssertTrue(source.contains("persistTelemetryStatusMigration"))
        XCTAssertTrue(source.contains("if persistTelemetryStatusMigration"))
        XCTAssertTrue(source.contains("config.save()"))

        let primary = try String(contentsOf: root.appendingPathComponent("menubar/Sources/App/StatusItemController.swift"))
        let stats = try String(contentsOf: root.appendingPathComponent("menubar/Sources/App/StatsStatusItemController.swift"))
        let settings = try String(contentsOf: root.appendingPathComponent("menubar/Sources/UI/SettingsView.swift"))
        XCTAssertTrue(primary.contains("item.autosaveName = \"org.coordharness.menubar.primary\""))
        XCTAssertTrue(primary.contains("item.isVisible = true"))
        XCTAssertTrue(primary.contains("let telemetry: [RingRenderer.TelemetryPresentation] = []"))
        XCTAssertTrue(stats.contains("next.autosaveName = \"org.coordharness.menubar.stats\""))
        XCTAssertTrue(stats.contains("func setEnabled(_ enabled: Bool)"))
        XCTAssertTrue(settings.contains("Show Stats as a separate menu-bar item"))
    }

    func testBatteryAndStatsVisibilityRoundTripPreservesExplicitSelections() throws {
        let selected = MenuBarVisibilityPersistence(
            batteryStatusItemEnabled: true,
            systemTelemetryInStatusItem: false,
            systemTelemetryShowCPU: false,
            systemTelemetryShowGPU: true,
            systemTelemetryShowRAM: false,
            systemTelemetryShowDisk: true
        )
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase

        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("coord-visibility-roundtrip-\(UUID().uuidString)", isDirectory: true)
        let url = root.appendingPathComponent("menubar_panel_config.json")
        defer { try? FileManager.default.removeItem(at: root) }
        try ConfigPersistence.write(encoder.encode(selected), to: url)
        XCTAssertEqual(try decoder.decode(MenuBarVisibilityPersistence.self, from: Data(contentsOf: url)), selected)

        let sourceRoot = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        let source = try String(contentsOf: sourceRoot.appendingPathComponent("menubar/Sources/Data/Config.swift"))
        XCTAssertTrue(source.contains("let storedTelemetryVisibility"))
        XCTAssertTrue(source.contains("systemTelemetryInStatusItem = storedTelemetryVisibility ?? fallback.systemTelemetryInStatusItem"))
        XCTAssertTrue(source.contains("let visibility = try MenuBarVisibilityPersistence(from: decoder)"))
        XCTAssertTrue(source.contains("batteryStatusItemEnabled = visibility.batteryStatusItemEnabled"))
        for key in ["systemTelemetryShowCPU", "systemTelemetryShowGPU", "systemTelemetryShowRAM", "systemTelemetryShowDisk"] {
            XCTAssertTrue(source.contains("\(key) = visibility.\(key)"))
        }
    }


}
