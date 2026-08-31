import Foundation
import XCTest

final class SystemTelemetryDecodeTests: XCTestCase {
    func testCanonicalSnakeCaseMetricsDecode() throws {
        let data = Data(#"""
        {
          "schema_version": 1,
          "generated_at": "2026-08-27T10:51:47Z",
          "stale_after_seconds": 45,
          "cadence": {"mode":"balanced","interval_seconds":2,"demand_active":true},
          "freshness": {"state":"fresh","age_seconds":1.5},
          "cpu": {"availability":"available","source":"macmon","usage_percent":41.0,"p_core_usage_percent":52.5,"e_core_usage_percent":18.25,"p_core_count":10,"e_core_count":4,"temperature_c":72.4},
          "gpu": {"availability":"available","source":"macmon","usage_percent":45.2,"renderer_percent":44.1,"tiler_percent":12.5,"temperature_c":64.8,"power_w":19.3,"ane_power_w":2.4},
          "memory": {"availability":"available","source":"macmon","used_percent":81.5,"used_bytes":56032247808,"total_bytes":68719476736,"free_bytes":12687228928,"app_bytes":42000000000,"wired_bytes":6000000000,"compressed_bytes":8032247808,"swap_used_bytes":2930049024,"swap_total_bytes":8589934592},
          "disk": {"availability":"available","source":"vitals_cache","used_percent":95.4,"used_bytes":1903566888960,"total_bytes":1995165736960,"free_bytes":91598848000}
        }
        """#.utf8)

        let snapshot = try SystemTelemetryClient.decode(data)
        XCTAssertEqual(snapshot.cpu.availablePercent, 41.0)
        XCTAssertEqual(snapshot.gpu.availablePercent, 45.2)
        XCTAssertEqual(snapshot.memory.availablePercent, 81.5)
        XCTAssertEqual(snapshot.disk.availablePercent, 95.4)
        XCTAssertEqual(snapshot.cadence?.intervalSeconds, 2)
        XCTAssertEqual(snapshot.freshness?.ageSeconds, 1.5)
        XCTAssertEqual(snapshot.cpu.pCoreUsagePercent, 52.5)
        XCTAssertEqual(snapshot.cpu.eCoreUsagePercent, 18.25)
        XCTAssertEqual(snapshot.cpu.pCoreCount, 10)
        XCTAssertEqual(snapshot.cpu.eCoreCount, 4)
        XCTAssertEqual(snapshot.cpu.temperatureC, 72.4)
        XCTAssertEqual(snapshot.gpu.rendererPercent, 44.1)
        XCTAssertEqual(snapshot.gpu.tilerPercent, 12.5)
        XCTAssertEqual(snapshot.gpu.temperatureC, 64.8)
        XCTAssertEqual(snapshot.gpu.powerW, 19.3)
        XCTAssertEqual(snapshot.gpu.anePowerW, 2.4)
        XCTAssertEqual(snapshot.memory.freeBytes, 12_687_228_928)
        XCTAssertEqual(snapshot.memory.appBytes, 42_000_000_000)
        XCTAssertEqual(snapshot.memory.wiredBytes, 6_000_000_000)
        XCTAssertEqual(snapshot.memory.compressedBytes, 8_032_247_808)
        XCTAssertEqual(snapshot.memory.swapTotalBytes, 8_589_934_592)
        XCTAssertFalse(snapshot.isStale)
    }
    func testRAMRingReconcilesFourPhysicalComponentsAndKeepsSwapSeparate() {
        let composition = SystemTelemetrySnapshot.MemoryRingComposition.make(
            usedPercent: 50, usedBytes: 900, totalBytes: 1_000,
            freeBytes: 100, appBytes: 300, wiredBytes: 100,
            compressedBytes: 100, swapUsedBytes: 250
        )
        XCTAssertEqual(composition.physicalUsedBytes, 500)
        XCTAssertEqual(composition.physicalFreeBytes, 500)
        XCTAssertEqual(composition.appBytes, 300)
        XCTAssertEqual(composition.wiredBytes, 100)
        XCTAssertEqual(composition.compressedBytes, 100)
        XCTAssertEqual(composition.swapUsedBytes, 250)
        XCTAssertEqual(composition.denominatorBytes, 1_000)
        XCTAssertEqual(composition.centerUsedPercent, 50)
        XCTAssertEqual(composition.segments.map(\.kind), [.app, .wired, .compressed, .free])
        XCTAssertEqual(composition.segments.reduce(0) { $0 + $1.fraction }, 1, accuracy: 0.000_001)
        XCTAssertFalse(composition.usesFallbackArc)

        let legacyPayload = SystemTelemetrySnapshot.MemoryRingComposition.make(
            usedPercent: nil, usedBytes: 300, totalBytes: 1_000,
            freeBytes: 100, appBytes: nil, wiredBytes: nil,
            compressedBytes: nil, swapUsedBytes: nil
        )
        XCTAssertEqual(legacyPayload.physicalUsedBytes, 300)
        XCTAssertEqual(legacyPayload.physicalFreeBytes, 700)
        XCTAssertTrue(legacyPayload.segments.isEmpty)
        XCTAssertTrue(legacyPayload.usesFallbackArc)

        let fallback = SystemTelemetrySnapshot.MemoryRingComposition.make(
            usedPercent: 43, usedBytes: nil, totalBytes: nil,
            freeBytes: nil, appBytes: nil, wiredBytes: nil,
            compressedBytes: nil, swapUsedBytes: nil
        )
        XCTAssertEqual(fallback.centerUsedPercent, 43)
        XCTAssertTrue(fallback.segments.isEmpty)
        XCTAssertTrue(fallback.usesFallbackArc)
    }

}
