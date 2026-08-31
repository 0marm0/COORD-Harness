import XCTest
@testable import CoordCockpitMac

final class NativeOperatorWritesTests: XCTestCase {
    private var root: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }

    private func source(_ path: String) throws -> String {
        try String(contentsOf: root.appendingPathComponent(path), encoding: .utf8)
    }

    func testBrokerUsesOnlyFixedLoopbackEndpointAndPrivateBearerToken() throws {
        let token = try source("apps/menubar/Sources/Cockpit/Core/NativeOperatorTokenSource.swift")
        let broker = try source("apps/menubar/Sources/Cockpit/UI/NativeCockpitActionBroker.swift")

        XCTAssertTrue(token.contains("components.host = \"127.0.0.1\""))
        XCTAssertTrue(token.contains("components.port = HarnessEndpoint.defaultPort"))
        XCTAssertTrue(token.contains("components.path = \"/api/native/action\""))
        XCTAssertTrue(token.contains("permissions & 0o077 == 0"))
        XCTAssertTrue(token.contains("ownerID == getuid()"))
        XCTAssertTrue(token.contains("operator-token"))
        XCTAssertTrue(broker.contains("NativeOperatorTokenSource.fixedActionEndpoint"))
        XCTAssertTrue(broker.contains(#"Bearer \(token)"#))
        XCTAssertFalse(broker.contains("HarnessEndpoint.base"))
        XCTAssertFalse(broker.contains("api/native/action_result"))
    }

    func testRequestCarriesAllCanonicalFencesAndNeverReleasesClaims() throws {
        let request = try source("apps/menubar/Sources/Cockpit/Core/NativeCockpitActionRequest.swift")

        for field in [
            "expected_version", "expected_assignee", "expected_head_event_ids",
            "work_id", "owner_lane", "release_held_claim", "confirmed",
        ] {
            XCTAssertTrue(request.contains("\"\(field)\""), "missing \(field)")
        }
        XCTAssertTrue(request.contains(#""release_held_claim": false"#))
        XCTAssertTrue(request.contains(#"payload["confirmed"] as? Bool == true"#))
        XCTAssertTrue(request.contains("ownerLane != expectedAssignee"))
        XCTAssertTrue(request.contains("case \"task.assign.claude\""))
        XCTAssertTrue(request.contains("nativeAction = \"work.reassign\""))
    }

    func testUIConfirmsTransfersAndDisablesTheCurrentLane() throws {
        let ui = try source("apps/menubar/Sources/Cockpit/UI/CockpitRootView.swift")
        let model = try source("apps/menubar/Sources/Cockpit/Core/CockpitModels.swift")

        XCTAssertTrue(ui.contains("Confirm operator transfer"))
        XCTAssertTrue(ui.contains("alert.alertStyle = .warning"))
        XCTAssertTrue(ui.contains("isCurrentAssignmentAction"))
        XCTAssertTrue(ui.contains("filter { $0 != currentLane }"))
        XCTAssertTrue(ui.contains(#""confirmed": true"#))
        let controller = try source("apps/menubar/Sources/Cockpit/UI/CockpitWindowController.swift")
        XCTAssertTrue(controller.contains("guard actionResult.ok else { return }"))
        for field in [
            "workVersion", "currentAssignee", "assignmentHeadEventIDs",
            "activeClaimIDs", "claimLive", "liveRunCount",
            "nativeOperatorWritesEnabled", "nativeOperatorWritesReason",
        ] {
            XCTAssertTrue(model.contains("var \(field)"), "missing projected field \(field)")
        }
    }
}
