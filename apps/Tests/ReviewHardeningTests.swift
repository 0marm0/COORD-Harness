import Foundation
import XCTest
@testable import CoordCockpitMac

final class ReviewHardeningTests: XCTestCase {
    override func tearDown() {
        HardenedURLProtocol.handler = nil
        super.tearDown()
    }

    func testSchemaVersionMustBeExactlyOne() throws {
        let snapshot = try Self.fixture()
        let unsupported = Self.copy(snapshot, schemaVersion: "2")

        XCTAssertThrowsError(try unsupported.validated()) { error in
            XCTAssertEqual(error as? SnapshotError, .invalidPayload("Unsupported schema version"))
        }
    }

    func testCorruptCacheIsReportedWithoutLoadingData() throws {
        let root = FileManager.default.temporaryDirectory
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        let file = root.appending(path: "snapshot.json")
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        try Data("not-json".utf8).write(to: file)

        XCTAssertThrowsError(try SnapshotCache(fileURL: file).load()) { error in
            XCTAssertEqual(error as? SnapshotCacheError, .corrupt)
        }
    }

    func testUnwritableCacheIsReported() throws {
        let root = FileManager.default.temporaryDirectory
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        let blockedDirectory = root.appending(path: "not-a-directory")
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        try Data("blocker".utf8).write(to: blockedDirectory)

        XCTAssertThrowsError(
            try SnapshotCache(fileURL: blockedDirectory.appending(path: "snapshot.json")).save(Self.fixture())
        ) { error in
            XCTAssertEqual(error as? SnapshotCacheError, .saveFailed)
        }
    }

    func testHTTPIsRestrictedToLoopbackHosts() throws {
        let client = SnapshotClient()
        for endpoint in [
            EndpointTestFixtures.localhostOrigin,
            EndpointTestFixtures.loopbackOrigin,
            EndpointTestFixtures.alternateLoopbackOrigin,
            EndpointTestFixtures.ipv6LoopbackOrigin
        ] {
            XCTAssertNoThrow(try client.snapshotURL(baseURL: XCTUnwrap(URL(string: endpoint))), endpoint)
        }

        let nonLoopbackEndpoints = [
            "http://" + ["192", "168", "1", "4"].joined(separator: "."),
            "http://" + ["10", "0", "0", "2"].joined(separator: ".")
        ]
        for endpoint in ["http://example.test"] + nonLoopbackEndpoints {
            XCTAssertThrowsError(try client.snapshotURL(baseURL: XCTUnwrap(URL(string: endpoint)))) { error in
                XCTAssertEqual(error as? SnapshotError, .insecureEndpoint)
            }
        }
        XCTAssertNoThrow(
            try client.snapshotURL(baseURL: XCTUnwrap(URL(string: "https://example.test")))
        )
    }

    func testHealthEndpointUsesGET() async throws {
        HardenedURLProtocol.handler = { request in
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertEqual(request.url?.path, "/healthz")
            return (try self.response(for: request, status: 204), Data())
        }

        try await stubbedClient().fetchHealth(
            baseURL: XCTUnwrap(URL(string: EndpointTestFixtures.loopbackOrigin))
        )
    }

    func testClientRejectsUnsupportedSchema() async throws {
        var object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Self.fixtureData()) as? [String: Any]
        )
        object["schema_version"] = "2"
        let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        HardenedURLProtocol.handler = { request in
            (try self.response(for: request, status: 200), data)
        }

        do {
            _ = try await stubbedClient().fetchSnapshot(
                baseURL: XCTUnwrap(URL(string: "https://example.test"))
            )
            XCTFail("Unsupported schema unexpectedly succeeded")
        } catch {
            XCTAssertEqual(error as? SnapshotError, .invalidPayload("Unsupported schema version"))
        }
    }

    private func stubbedClient() -> SnapshotClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [HardenedURLProtocol.self]
        return SnapshotClient(session: URLSession(configuration: configuration))
    }

    private func response(for request: URLRequest, status: Int) throws -> HTTPURLResponse {
        try XCTUnwrap(HTTPURLResponse(
            url: try XCTUnwrap(request.url),
            statusCode: status,
            httpVersion: "HTTP/1.1",
            headerFields: nil
        ))
    }

    fileprivate static func fixtureData() throws -> Data {
        let url = try XCTUnwrap(
            Bundle(for: Self.self).url(forResource: "snapshot-v1", withExtension: "json")
        )
        return try Data(contentsOf: url)
    }

    fileprivate static func fixture() throws -> NativeSnapshotV1 {
        try SnapshotCoding.decoder()
            .decode(NativeSnapshotV1.self, from: Self.fixtureData())
            .validated()
    }

    fileprivate static func copy(
        _ snapshot: NativeSnapshotV1,
        schemaVersion: String? = nil,
        stale: Bool? = nil
    ) -> NativeSnapshotV1 {
        NativeSnapshotV1(
            schemaVersion: schemaVersion ?? snapshot.schemaVersion,
            generatedAt: snapshot.generatedAt,
            source: snapshot.source,
            stale: stale ?? snapshot.stale,
            summary: snapshot.summary,
            rows: snapshot.rows,
            sessions: snapshot.sessions
        )
    }
}

@MainActor
final class CockpitModelTransitionTests: XCTestCase {
    func testNormalLaunchDefaultsToInstalledBoardPort() {
        let baseURLText = CockpitModel.resolveBaseURL(environment: [:], persistedBaseURL: nil)

        XCTAssertEqual(baseURLText, EndpointTestFixtures.loopbackOrigin)
        XCTAssertEqual(URL(string: baseURLText)?.port, EndpointTestFixtures.loopbackPort)
    }

    func testCachedSnapshotStartsAsLastGood() throws {
        let fixture = try ReviewHardeningTests.fixture()
        let cache = RecordingCache(loadResult: .success(fixture))
        let model = makeModel(
            client: StubSnapshotClient(snapshotResult: .success(fixture), healthResult: .success(())),
            cache: cache
        )

        XCTAssertEqual(model.snapshot, fixture)
        XCTAssertTrue(model.isShowingLastGood)
        XCTAssertEqual(model.health, .unknown)
        XCTAssertEqual(model.snapshotStateLabel, "Last good")
    }

    func testRefreshFailurePreservesAndLabelsLastGood() async throws {
        let fixture = try ReviewHardeningTests.fixture()
        let cache = RecordingCache(loadResult: .success(fixture))
        let client = StubSnapshotClient(
            snapshotResult: .failure(.serverStatus(503)),
            healthResult: .success(())
        )
        let model = makeModel(client: client, cache: cache)

        await model.refresh()

        XCTAssertEqual(model.snapshot, fixture)
        XCTAssertTrue(model.isShowingLastGood)
        XCTAssertEqual(model.health, .unavailable("The server returned HTTP 503."))
    }

    func testFreshStaleSnapshotRemainsStaleButNotLastGood() async throws {
        let fixture = try ReviewHardeningTests.fixture()
        let stale = ReviewHardeningTests.copy(fixture, stale: true)
        let cache = RecordingCache(loadResult: .success(fixture))
        let model = makeModel(
            client: StubSnapshotClient(snapshotResult: .success(stale), healthResult: .success(())),
            cache: cache
        )

        await model.refresh()

        XCTAssertEqual(model.snapshot, stale)
        XCTAssertTrue(model.snapshot?.stale == true)
        XCTAssertFalse(model.isShowingLastGood)
        XCTAssertEqual(model.health, .healthy)
        XCTAssertEqual(model.snapshotStateLabel, "Stale")
    }

    func testCacheLoadAndSaveFailuresAreSurfaced() async throws {
        let fixture = try ReviewHardeningTests.fixture()
        let loadFailure = RecordingCache(loadResult: .failure(.corrupt))
        let loadModel = makeModel(
            client: StubSnapshotClient(snapshotResult: .success(fixture), healthResult: .success(())),
            cache: loadFailure
        )
        XCTAssertNil(loadModel.snapshot)
        XCTAssertEqual(loadModel.cacheIssue, SnapshotCacheError.corrupt.localizedDescription)
        XCTAssertEqual(loadModel.snapshotStateLabel, "Unavailable")

        let saveFailure = RecordingCache(loadResult: .success(nil), saveError: .saveFailed)
        let saveModel = makeModel(
            client: StubSnapshotClient(snapshotResult: .success(fixture), healthResult: .success(())),
            cache: saveFailure
        )
        await saveModel.refresh()
        XCTAssertEqual(saveModel.snapshot, fixture)
        XCTAssertEqual(saveModel.health, .healthy)
        XCTAssertEqual(saveModel.cacheIssue, SnapshotCacheError.saveFailed.localizedDescription)
    }

    func testHealthFailureExposesAssociatedErrorDetail() async throws {
        let fixture = try ReviewHardeningTests.fixture()
        let model = makeModel(
            client: StubSnapshotClient(
                snapshotResult: .success(fixture),
                healthResult: .failure(.serverStatus(502))
            ),
            cache: RecordingCache(loadResult: .success(nil))
        )

        await model.refresh()

        XCTAssertEqual(model.snapshot, fixture)
        XCTAssertFalse(model.isShowingLastGood)
        XCTAssertEqual(model.health, .unavailable("The server returned HTTP 502."))
    }

    private func makeModel(
        client: StubSnapshotClient,
        cache: RecordingCache
    ) -> CockpitModel {
        let suite = "org.coordharness.tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        return CockpitModel(client: client, cache: cache, defaults: defaults)
    }
}

private struct StubSnapshotClient: SnapshotFetching {
    let snapshotResult: Result<NativeSnapshotV1, SnapshotError>
    let healthResult: Result<Void, SnapshotError>

    func fetchSnapshot(baseURL: URL) async throws -> NativeSnapshotV1 {
        try snapshotResult.get()
    }

    func fetchUsage(baseURL: URL) async throws -> UsageIntelligenceSnapshot {
        throw SnapshotError.serverStatus(503)
    }

    func fetchHealth(baseURL: URL) async throws {
        try healthResult.get()
    }
}

private final class RecordingCache: SnapshotCaching, @unchecked Sendable {
    private let loadResult: Result<NativeSnapshotV1?, SnapshotCacheError>
    private let saveError: SnapshotCacheError?
    private(set) var savedSnapshots: [NativeSnapshotV1] = []

    init(
        loadResult: Result<NativeSnapshotV1?, SnapshotCacheError>,
        saveError: SnapshotCacheError? = nil
    ) {
        self.loadResult = loadResult
        self.saveError = saveError
    }

    func load() throws -> NativeSnapshotV1? {
        try loadResult.get()
    }

    func save(_ snapshot: NativeSnapshotV1) throws {
        if let saveError { throw saveError }
        savedSnapshots.append(snapshot)
    }
}

private final class HardenedURLProtocol: URLProtocol {
    static var handler: ((URLRequest) throws -> (HTTPURLResponse, Data))?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        do {
            let handler = try XCTUnwrap(Self.handler)
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }
    override func stopLoading() {}
}
