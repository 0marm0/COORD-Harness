import Foundation

final class NativeCockpitActionBroker {
    private let endpoint = URL(string: "\(HarnessEndpoint.base)/api/native/action")!
    private let resultEndpoint = URL(string: "\(HarnessEndpoint.base)/api/native/action_result")!
    private let session: URLSession

    init(timeout: TimeInterval = 12) {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = timeout
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.httpMaximumConnectionsPerHost = 1
        self.session = URLSession(configuration: config)
    }

    func perform(action: String, row: CockpitRow? = nil, payload: [String: Any] = [:]) async -> Result<NativeCockpitActionResult, Error> {
        let actionID = "native-\(UUID().uuidString.lowercased())"
        let body = NativeCockpitActionRequestBuilder.body(actionID: actionID, action: action, row: row, payload: payload)
        do {
            var request = URLRequest(url: endpoint)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.setValue(actionID, forHTTPHeaderField: "X-Request-Id")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            let (data, response) = try await session.data(for: request)
            let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
            let actionResult = NativeCockpitActionResult(dictionary: object)
            if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                if object["status"] != nil || object["action_id"] != nil || object["ok"] != nil {
                    return .success(actionResult)
                }
                return .failure(NativeCockpitActionError(message: object["reason"] as? String ?? "HTTP \(http.statusCode)"))
            }
            if object["ok"] as? Bool == false {
                return .success(actionResult)
            }
            return .success(actionResult)
        } catch {
            return .failure(error)
        }
    }

    func fetchResult(actionID: String) async -> Result<NativeCockpitActionResult, Error> {
        guard var components = URLComponents(url: resultEndpoint, resolvingAgainstBaseURL: false) else {
            return .failure(NativeCockpitActionError(message: "invalid action result endpoint"))
        }
        components.queryItems = [URLQueryItem(name: "id", value: actionID)]
        guard let url = components.url else {
            return .failure(NativeCockpitActionError(message: "invalid action result URL"))
        }
        do {
            let (data, response) = try await session.data(from: url)
            let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
            if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                return .failure(NativeCockpitActionError(message: object["reason"] as? String ?? "HTTP \(http.statusCode)"))
            }
            return .success(NativeCockpitActionResult(dictionary: object))
        } catch {
            return .failure(error)
        }
    }
}

struct NativeCockpitActionError: Error, LocalizedError {
    var message: String
    var errorDescription: String? { message }
}
