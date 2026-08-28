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
        XCTAssertTrue(source.contains("storedTelemetryStatusVersion == nil"))
        XCTAssertTrue(source.contains("? true"))
        XCTAssertTrue(source.contains("persistTelemetryStatusMigration"))
        XCTAssertTrue(source.contains("if persistTelemetryStatusMigration"))
        XCTAssertTrue(source.contains("config.save()"))

        let controller = try String(contentsOf: root.appendingPathComponent("menubar/Sources/App/StatusItemController.swift"))
        XCTAssertTrue(controller.contains("item.autosaveName = \"org.coordharness.menubar.primary\""))
        XCTAssertTrue(controller.contains("item.isVisible = true"))
        XCTAssertTrue(controller.contains("systemTelemetryPresentations"))
        XCTAssertTrue(controller.contains("RingRenderer.statusImage"))
    }

}
