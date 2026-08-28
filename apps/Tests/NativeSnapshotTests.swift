import Foundation
import XCTest
@testable import CoordCockpitMac

final class NativeSnapshotTests: XCTestCase {
    func testDeterministicFixtureDecodesAndValidates() throws {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "snapshot-v1", withExtension: "json"))
        let snapshot = try SnapshotCoding.decoder()
            .decode(NativeSnapshotV1.self, from: Data(contentsOf: url))
            .validated()

        XCTAssertEqual(snapshot.schemaVersion, "1")
        XCTAssertEqual(snapshot.summary.total, 2)
        XCTAssertEqual(snapshot.rows.map(\.id), ["work-001", "work-002"])
        XCTAssertEqual(snapshot.sessions?.first?.live, true)
    }

    func testValidationRejectsDuplicateRows() throws {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "snapshot-v1", withExtension: "json"))
        let fixture = try SnapshotCoding.decoder().decode(NativeSnapshotV1.self, from: Data(contentsOf: url))
        let invalid = NativeSnapshotV1(
            schemaVersion: fixture.schemaVersion,
            generatedAt: fixture.generatedAt,
            source: fixture.source,
            stale: fixture.stale,
            summary: fixture.summary,
            rows: [fixture.rows[0], fixture.rows[0]],
            sessions: fixture.sessions
        )

        XCTAssertThrowsError(try invalid.validated())
    }

    func testCacheRoundTrip() throws {
        let fixtureURL = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "snapshot-v1", withExtension: "json"))
        let snapshot = try SnapshotCoding.decoder().decode(NativeSnapshotV1.self, from: Data(contentsOf: fixtureURL))
        let cacheURL = FileManager.default.temporaryDirectory
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
            .appending(path: "snapshot.json")
        let cache = SnapshotCache(fileURL: cacheURL)

        try cache.save(snapshot)
        XCTAssertEqual(try cache.load(), snapshot)
        try? FileManager.default.removeItem(at: cacheURL.deletingLastPathComponent())
    }
}
