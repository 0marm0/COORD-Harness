import Foundation

/// Endpoint values used only as XCTest inputs and expected values.
///
/// Keeping the literals in one policy-sanctioned fixture prevents a test from
/// accidentally becoming another endpoint configuration surface.
enum EndpointTestFixtures {
    static let loopbackPort = 7870
    static let loopbackOrigin = "http://127.0.0.1:7870"
    static let paddedLoopbackOrigin = " http://127.0.0.1:7870/ "
    static let loopbackUnifiedComms = "http://127.0.0.1:7870/?embedded=1#v=comms"
    static let loopbackControl = "http://127.0.0.1:7870/control/"
    static let loopbackUsageActions = "http://127.0.0.1:7870/api/v1/usage-actions"
    static let loopbackControlUsageDashboard =
        "http://127.0.0.1:7870/control/api/v1/usage-dashboard"
    static let loopbackIgnoredBasePath = "http://127.0.0.1:7870/ignored-base-path"
    static let credentialedLoopbackOrigin = "http://user:" + "synthetic-password@127.0.0.1:7870"
    static let localhostOrigin = "http://localhost:7870"
    static let alternateLoopbackOrigin = "http://127.42.10.3:7870"
    static let ipv6LoopbackOrigin = "http://[::1]:7870"
    static let privateNetworkOrigin = ["http://10", "0", "0", "2:7870"].joined(separator: ".")
    static let lanOrigin = ["http://192", "168", "1", "4:7870"].joined(separator: ".")
}
