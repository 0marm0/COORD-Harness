import Foundation

final class CockpitCapabilityInventorySource {
    private let endpoint: URL
    private let session: URLSession
    private let decoder = JSONDecoder()

    init(endpoint: URL = URL(string: "\(HarnessEndpoint.base)/api/capability_inventory")!, timeout: TimeInterval = 4) {
        self.endpoint = endpoint
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = timeout
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        self.session = URLSession(configuration: config)
    }

    func load() async -> CockpitCapabilityInventory? {
        // The third never-served probe. This board publishes no capability
        // facts at all, so an empty inventory would assert the harness has
        // none; nil is the honest answer and it does not need re-asking.
        if DeclinedRoutes.declinedStatus(endpoint) != nil { return nil }
        do {
            let (data, response) = try await session.data(from: endpoint)
            if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                DeclinedRoutes.record(endpoint, status: http.statusCode)
                return nil
            }
            return try decoder.decode(CockpitCapabilityInventory.self, from: data)
        } catch {
            return nil
        }
    }
}
