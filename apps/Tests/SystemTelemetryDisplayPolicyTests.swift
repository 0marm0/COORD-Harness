import XCTest

final class SystemTelemetryDisplayPolicyTests: XCTestCase {
    func testDefaultThresholdsAndSeverityBoundaries() {
        let policy = SystemTelemetryDisplayPolicy(
            warningThreshold: SystemTelemetryDisplayPolicy.defaultWarningThreshold,
            criticalThreshold: SystemTelemetryDisplayPolicy.defaultCriticalThreshold
        )

        XCTAssertEqual(policy.warningThreshold, 70)
        XCTAssertEqual(policy.criticalThreshold, 90)
        XCTAssertEqual(policy.severity(for: nil), .unavailable)
        XCTAssertEqual(policy.severity(for: 69.9), .normal)
        XCTAssertEqual(policy.severity(for: 70), .warning)
        XCTAssertEqual(policy.severity(for: 89.9), .warning)
        XCTAssertEqual(policy.severity(for: 90), .critical)
    }

    func testThresholdsClampAndCriticalNeverFallsBelowWarning() {
        let policy = SystemTelemetryDisplayPolicy(warningThreshold: 120, criticalThreshold: 40)
        XCTAssertEqual(policy.warningThreshold, 100)
        XCTAssertEqual(policy.criticalThreshold, 100)
        XCTAssertEqual(policy.severity(for: 100), .critical)
        XCTAssertEqual(policy.severity(for: .nan), .unavailable)
    }

    func testDetailFormattingIsBoundedAndHumanReadable() {
        XCTAssertEqual(SystemTelemetryDetailFormatter.percent(65.4), "65%")
        XCTAssertEqual(SystemTelemetryDetailFormatter.percent(nil), "N/A")
        XCTAssertEqual(SystemTelemetryDetailFormatter.bytes(68_719_476_736), "64 GB")
        XCTAssertEqual(SystemTelemetryDetailFormatter.bytes(nil), "—")
        XCTAssertEqual(SystemTelemetryDetailFormatter.rate(1_048_576), "1.0 MB/s")
        XCTAssertEqual(SystemTelemetryDetailFormatter.rate(nil), "—")
        XCTAssertEqual(SystemTelemetryDetailFormatter.age(2.45), "2.5s ago")
        XCTAssertEqual(SystemTelemetryDetailFormatter.age(125), "2m ago")
        XCTAssertEqual(SystemTelemetryDetailFormatter.interval(2), "2.0s")
    }
    func testDiskCapacityPrefersImportantUsageAvailabilityAndFallsBack() throws {
        let important = try XCTUnwrap(SystemTelemetryDiskCapacity.resolve(
            totalBytes: 2_000,
            importantUsageAvailableBytes: 220,
            availableBytes: 80
        ))
        XCTAssertEqual(important.freeBytes, 220)
        XCTAssertEqual(important.usedBytes, 1_780)
        XCTAssertEqual(try XCTUnwrap(important.usedPercent), 89, accuracy: 0.001)

        let fallback = try XCTUnwrap(SystemTelemetryDiskCapacity.resolve(
            totalBytes: 2_000,
            importantUsageAvailableBytes: nil,
            availableBytes: 80
        ))
        XCTAssertEqual(fallback.freeBytes, 80)
        XCTAssertEqual(try XCTUnwrap(fallback.usedPercent), 96, accuracy: 0.001)
        XCTAssertNil(SystemTelemetryDiskCapacity.resolve(
            totalBytes: 0,
            importantUsageAvailableBytes: 1,
            availableBytes: 1
        ))
    }

}
