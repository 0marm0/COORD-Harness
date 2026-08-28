import Foundation

enum CockpitProjectionRefreshResult: Equatable {
    case nativeReadModel
    case compactFallback
}

final class CockpitProjectionRefresher {
    static let defaultURL = CockpitHTTPFallbackSource.defaultURL

    private let url: URL
    private let session: URLSession

    init(url: URL = CockpitProjectionRefresher.defaultURL, timeout: TimeInterval = 4) {
        self.url = url
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = timeout
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.httpMaximumConnectionsPerHost = 1
        self.session = URLSession(configuration: config)
    }

    func refresh() async -> Result<CockpitProjectionRefreshResult, Error> {
        // This is the poller that actually ran: it shares a URL with
        // CockpitHTTPFallbackSource and fired on the same 1.5-second tick.
        if let declined = DeclinedRoutes.declinedStatus(url) {
            return .failure(CockpitLoadErrorState(
                kind: .transport,
                message: "projection refresh is not served here (status \(declined))"
            ))
        }
        do {
            let (data, response) = try await session.data(from: url)
            if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                DeclinedRoutes.record(url, status: http.statusCode)
                return .failure(CockpitLoadErrorState(
                    kind: .transport,
                    message: "projection refresh returned HTTP \(http.statusCode)"
                ))
            }
            return .success(try Self.classify(data))
        } catch {
            return .failure(error)
        }
    }

    static func classify(_ data: Data) throws -> CockpitProjectionRefreshResult {
        guard let root = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw CockpitLoadErrorState(kind: .transport, message: "projection refresh payload is not an object")
        }
        if let meta = root["meta"] as? [String: Any],
           (meta["contract"] as? String) == "native_cockpit.v1" {
            return .nativeReadModel
        }
        return .compactFallback
    }
}
