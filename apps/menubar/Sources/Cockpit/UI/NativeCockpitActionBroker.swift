import Foundation

final class NativeCockpitActionBroker {
    private let endpoint: URL
    private let token: String?
    private let session: URLSession

    init(
        endpoint: URL = NativeOperatorTokenSource.fixedActionEndpoint,
        token: String? = NativeOperatorTokenSource.load(),
        timeout: TimeInterval = 12
    ) {
        self.endpoint = endpoint
        self.token = token
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = timeout
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.httpMaximumConnectionsPerHost = 1
        self.session = URLSession(configuration: config)
    }

    func perform(action: String, row: CockpitRow? = nil, payload: [String: Any] = [:]) async -> Result<NativeCockpitActionResult, Error> {
        guard let token else {
            return .failure(NativeCockpitActionError(message: "Native operator transfers are disabled: no private operator token is configured."))
        }
        let actionID = "native-\(UUID().uuidString.lowercased())"
        guard let body = NativeCockpitActionRequestBuilder.body(
            actionID: actionID,
            action: action,
            row: row,
            payload: payload
        ) else {
            return .failure(NativeCockpitActionError(message: "The selected row lacks current reassignment fences or transfer confirmation."))
        }
        do {
            var request = URLRequest(url: endpoint)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            request.setValue(actionID, forHTTPHeaderField: "X-Request-Id")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            let (data, response) = try await session.data(for: request)
            let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
            let actionResult = NativeCockpitActionResult(dictionary: object)
            if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                if object["status"] != nil || object["action_id"] != nil || object["ok"] != nil {
                    return .success(actionResult)
                }
                return .failure(NativeCockpitActionError(message: "Native transfer returned HTTP \(http.statusCode)."))
            }
            return .success(actionResult)
        } catch {
            return .failure(error)
        }
    }
}

struct NativeCockpitActionError: Error, LocalizedError {
    var message: String
    var errorDescription: String? { message }
}
