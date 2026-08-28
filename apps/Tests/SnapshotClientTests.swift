import Foundation
import XCTest
@testable import CoordCockpitMac

final class SnapshotClientTests: XCTestCase {
    func testEndpointURLsPreserveBasePathAndUseOnlyGETRoutes() throws {
        let client = SnapshotClient()
        let base = try XCTUnwrap(URL(string: "https://example.test/control/"))

        XCTAssertEqual(try client.snapshotURL(baseURL: base).absoluteString, "https://example.test/control/api/v1/snapshot")
        XCTAssertEqual(try client.healthURL(baseURL: base).absoluteString, "https://example.test/control/healthz")
    }

    func testInvalidSchemeIsRejected() throws {
        let client = SnapshotClient()
        let base = try XCTUnwrap(URL(string: "file:///tmp/coord"))

        XCTAssertThrowsError(try client.snapshotURL(baseURL: base)) { error in
            XCTAssertEqual(error as? SnapshotError, .invalidEndpoint)
        }
    }

    func testFetchSnapshotUsesGETAndDecodesPayload() async throws {
        let fixtureURL = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "snapshot-v1", withExtension: "json"))
        let fixtureData = try Data(contentsOf: fixtureURL)
        URLProtocolStub.handler = { request in
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertEqual(request.url?.path, "/api/v1/snapshot")
            let response = try XCTUnwrap(HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: "HTTP/1.1",
                headerFields: ["Content-Type": "application/json"]
            ))
            return (response, fixtureData)
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [URLProtocolStub.self]
        let client = SnapshotClient(session: URLSession(configuration: configuration))

        let snapshot = try await client.fetchSnapshot(
            baseURL: XCTUnwrap(URL(string: EndpointTestFixtures.loopbackOrigin))
        )
        XCTAssertEqual(snapshot.summary.running, 1)
    }
}

private final class URLProtocolStub: URLProtocol {
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
