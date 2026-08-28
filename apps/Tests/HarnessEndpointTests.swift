import XCTest
@testable import CoordCockpitMac

final class HarnessEndpointTests: XCTestCase {
    func testPublicDefaultMatchesBoardService() {
        let base = HarnessEndpoint.resolveBase(environment: [:], persistedBaseURL: nil)
        XCTAssertEqual(HarnessEndpoint.defaultPort, EndpointTestFixtures.loopbackPort)
        XCTAssertEqual(base, EndpointTestFixtures.loopbackOrigin)
        XCTAssertEqual(URL(string: base + "/api/v1/system-telemetry")?.port, EndpointTestFixtures.loopbackPort)
        XCTAssertEqual(URL(string: base + "/api/v1/usage-dashboard")?.port, EndpointTestFixtures.loopbackPort)
    }

    func testEnvironmentOverrideWinsAndNormalizesTrailingSlash() {
        let environment = ["COORD_BOARD_URL": "  http://localhost:9911/  "]
        XCTAssertEqual(
            HarnessEndpoint.resolveBase(
                environment: environment,
                persistedBaseURL: EndpointTestFixtures.loopbackOrigin
            ),
            "http://localhost:9911"
        )
    }

    func testPersistedEndpointSupportsGUIAppsWithoutShellEnvironment() {
        XCTAssertEqual(
            HarnessEndpoint.resolveBase(
                environment: [:],
                persistedBaseURL: EndpointTestFixtures.paddedLoopbackOrigin
            ),
            EndpointTestFixtures.loopbackOrigin
        )
    }

    @MainActor
    func testSwiftUICockpitSharesEndpointContract() {
        XCTAssertEqual(CockpitModel.defaultBaseURL, EndpointTestFixtures.loopbackOrigin)
        XCTAssertEqual(
            CockpitModel.resolveBaseURL(
                environment: [:],
                persistedBaseURL: EndpointTestFixtures.paddedLoopbackOrigin
            ),
            HarnessEndpoint.resolveBase(environment: [:])
        )
    }
}
