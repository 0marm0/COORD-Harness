import Foundation
import XCTest
@testable import CoordCockpitMac

final class UsageWindowRefreshLifecycleTests: XCTestCase {
    @MainActor
    func testStartupRefreshIsImmediateAndStartingTwiceDoesNotDuplicateIt() async {
        let loaded = expectation(description: "startup usage loaded")
        var refreshCount = 0
        let lifecycle = UsageWindowRefreshLifecycle(refresh: {
            refreshCount += 1
            loaded.fulfill()
        })
        lifecycle.start()
        lifecycle.start()
        await fulfillment(of: [loaded], timeout: 2)
        XCTAssertEqual(refreshCount, 1)
        lifecycle.stop()
        await Task.yield()
        XCTAssertEqual(refreshCount, 1)
    }

    @MainActor
    func testVisibleWindowRefreshesAgainAndClosingStopsItsLoop() async {
        let repeated = expectation(description: "periodic usage loaded")
        var refreshCount = 0
        let lifecycle = UsageWindowRefreshLifecycle(interval: 0.02, refresh: {
            refreshCount += 1
            if refreshCount == 2 { repeated.fulfill() }
        })
        lifecycle.start()
        await fulfillment(of: [repeated], timeout: 2)
        lifecycle.stop()
        let stoppedCount = refreshCount
        try? await Task.sleep(nanoseconds: 60_000_000)
        XCTAssertEqual(refreshCount, stoppedCount)
    }
}
