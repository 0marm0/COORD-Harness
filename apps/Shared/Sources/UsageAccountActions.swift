import Foundation

#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

enum UsageAccountAction: String, Codable, CaseIterable, Sendable {
    case codexLoginStart = "codex_login_start"
    case codexLoginCancel = "codex_login_cancel"
    case claudeConnectOpen = "claude_connect_open"
}

enum UsageAccountProvider: String, Codable, CaseIterable, Sendable {
    case claude
    case codex
}

struct UsageProviderProfile: Decodable, Identifiable, Equatable, Sendable {
    let id: String
    let label: String
    let active: Bool
    let isolated: Bool
}

struct UsageProviderProfileCollection: Decodable, Equatable, Sendable {
    let active: String
    let profiles: [UsageProviderProfile]
}

struct UsageProviderProfiles: Decodable, Equatable, Sendable {
    let claude: UsageProviderProfileCollection
    let codex: UsageProviderProfileCollection

    func collection(for provider: UsageAccountProvider) -> UsageProviderProfileCollection {
        switch provider {
        case .claude: claude
        case .codex: codex
        }
    }
}

enum UsageProfileMutation: Equatable, Sendable {
    case add(provider: UsageAccountProvider, label: String)
    case select(provider: UsageAccountProvider, profileID: String)
    case remove(provider: UsageAccountProvider, profileID: String)

    var document: [String: String] {
        switch self {
        case let .add(provider, label):
            ["action": "profile_add", "provider": provider.rawValue, "label": label]
        case let .select(provider, profileID):
            ["action": "profile_select", "provider": provider.rawValue, "profile_id": profileID]
        case let .remove(provider, profileID):
            ["action": "profile_remove", "provider": provider.rawValue, "profile_id": profileID]
        }
    }
}

enum UsageCodexLoginState: String, Decodable, Sendable {
    case idle
    case starting
    case waitingBrowser = "waiting_browser"
    case completed
    case failed
    case cancelled
    case expired
    case unavailable
}

enum UsageCodexReasonCode: String, Decodable, Sendable {
    case loginExpired = "login_expired"
    case loginStartFailed = "login_start_failed"
    case loginFailed = "login_failed"
    case loginInterrupted = "login_interrupted"
}

enum UsageClaudeConnectionState: String, Decodable, Sendable {
    case connected
    case signInRequired = "sign_in_required"
    case waitingUser = "waiting_user"
    case manualConnectRequired = "manual_connect_required"
    case unavailable

    var safeStatusLabel: String {
        switch self {
        case .connected: "Connected"
        case .signInRequired: "Sign-in required"
        case .waitingUser: "Waiting for sign-in"
        case .manualConnectRequired: "Ready to connect"
        case .unavailable: "Connect unavailable"
        }
    }

    var safeStatusCopy: String {
        switch self {
        case .connected:
            "Claude is connected through Claude Code via the local provider service."
        case .signInRequired:
            "Claude needs direct Claude Code sign-in via the local provider service."
        case .waitingUser:
            "Claude Code sign-in is open. Finish the provider-owned browser flow."
        case .manualConnectRequired:
            "Open direct Claude Code sign-in via the local provider service."
        case .unavailable:
            "Direct Claude Code sign-in is currently unavailable from the local provider service."
        }
    }
}

enum UsageAccountActionResult: String, Decodable, Sendable {
    case browserOpened = "browser_opened"
    case loginAlreadyActive = "login_already_active"
    case loginStartFailed = "login_start_failed"
    case cancelled
    case noActiveLogin = "no_active_login"
    case connectWindowOpened = "connect_window_opened"
    case connectAlreadyConnected = "connect_already_connected"
    case connectAlreadyActive = "connect_already_active"
    case connectUnavailable = "connect_unavailable"
    case profileAdd = "profile_add"
    case profileSelect = "profile_select"
    case profileRemove = "profile_remove"
}

struct UsageCodexAccountStatus: Decodable, Sendable {
    let state: UsageCodexLoginState
    let canStart: Bool
    let canCancel: Bool
    let reasonCode: UsageCodexReasonCode?

    private enum CodingKeys: String, CodingKey {
        case state
        case canStart = "can_start"
        case canCancel = "can_cancel"
        case reasonCode = "reason_code"
    }
}

struct UsageClaudeAccountStatus: Decodable, Sendable {
    let state: UsageClaudeConnectionState
    let connectAvailable: Bool
    let opened: Bool?

    private enum CodingKeys: String, CodingKey {
        case state
        case connectAvailable = "connect_available"
        case opened
    }
}

struct UsageAccountActionResponse: Decodable, Sendable {
    static let schema = "coord.usage-account-actions.v1"

    let codex: UsageCodexAccountStatus
    let claude: UsageClaudeAccountStatus
    let profiles: UsageProviderProfiles?
    let ok: Bool?
    let result: UsageAccountActionResult?

    private enum CodingKeys: String, CodingKey {
        case schema, codex, claude, profiles, ok, result
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        guard try values.decode(String.self, forKey: .schema) == Self.schema else {
            throw UsageAccountActionError.invalidResponse
        }
        codex = try values.decode(UsageCodexAccountStatus.self, forKey: .codex)
        claude = try values.decode(UsageClaudeAccountStatus.self, forKey: .claude)
        profiles = try values.decodeIfPresent(UsageProviderProfiles.self, forKey: .profiles)
        ok = try values.decodeIfPresent(Bool.self, forKey: .ok)
        result = try values.decodeIfPresent(UsageAccountActionResult.self, forKey: .result)
        try Self.validateCoherence(codex: codex, claude: claude, ok: ok, result: result)
    }

    private static func validateCoherence(
        codex: UsageCodexAccountStatus,
        claude: UsageClaudeAccountStatus,
        ok: Bool?,
        result: UsageAccountActionResult?
    ) throws {
        guard let result else { return }
        guard let ok else { throw UsageAccountActionError.invalidResponse }
        let coherent = switch result {
        case .browserOpened:
            ok && codex.state == .waitingBrowser && !codex.canStart && codex.canCancel
        case .loginAlreadyActive:
            !ok && (codex.state == .starting || codex.state == .waitingBrowser)
                && !codex.canStart && codex.canCancel
        case .loginStartFailed:
            !ok && codex.state == .failed && codex.canStart && !codex.canCancel
        case .cancelled:
            ok && codex.state == .cancelled && codex.canStart && !codex.canCancel
        case .noActiveLogin:
            !ok && codex.state != .starting && codex.state != .waitingBrowser
                && codex.canStart && !codex.canCancel
        case .connectWindowOpened:
            ok && claude.state != .unavailable
                && claude.connectAvailable && claude.opened == true
        case .connectAlreadyConnected:
            ok && claude.state == .connected && claude.connectAvailable
                && claude.opened != true
        case .connectAlreadyActive:
            !ok && claude.state == .waitingUser && claude.connectAvailable
                && claude.opened != true
        case .connectUnavailable:
            !ok && claude.opened != true
        case .profileAdd, .profileSelect, .profileRemove:
            ok
        }
        guard coherent else { throw UsageAccountActionError.invalidResponse }
    }
}

enum UsageAccountActionError: Error, Equatable {
    case localBoardRequired
    case invalidResponse
    case responseTooLarge
    case serverStatus(Int)
}

protocol UsageAccountActionServing: Sendable {
    func status() async throws -> UsageAccountActionResponse
    func perform(_ action: UsageAccountAction) async throws -> UsageAccountActionResponse
    func perform(_ mutation: UsageProfileMutation) async throws -> UsageAccountActionResponse
}

extension UsageAccountActionServing {
    func perform(_ mutation: UsageProfileMutation) async throws -> UsageAccountActionResponse {
        throw UsageAccountActionError.invalidResponse
    }
}

struct UsageAccountActionClient: UsageAccountActionServing, Sendable {
    private static let maximumResponseBytes = 16 * 1024

    let baseURL: URL?
    let session: URLSession
    let timeout: TimeInterval

    init(baseURL: URL?, session: URLSession? = nil, timeout: TimeInterval = 5) {
        self.baseURL = baseURL
        self.session = session ?? Self.privateSession()
        self.timeout = max(0.2, min(timeout, 10))
    }

    func status() async throws -> UsageAccountActionResponse {
        var request = try request(path: "/api/v1/usage-actions/status", method: "GET")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        return try await send(request)
    }

    func perform(_ action: UsageAccountAction) async throws -> UsageAccountActionResponse {
        try await perform(document: ["action": action.rawValue])
    }

    func perform(_ mutation: UsageProfileMutation) async throws -> UsageAccountActionResponse {
        try await perform(document: mutation.document)
    }

    private func perform(document: [String: String]) async throws -> UsageAccountActionResponse {
        var request = try request(path: "/api/v1/usage-actions", method: "POST")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("v1", forHTTPHeaderField: "X-Coord-Usage-Action")
        request.httpBody = try JSONEncoder().encode(document)
        return try await send(request)
    }

    private func request(path: String, method: String) throws -> URLRequest {
        let endpoint = try endpoint(path: path)
        var request = URLRequest(
            url: endpoint.url,
            cachePolicy: .reloadIgnoringLocalCacheData,
            timeoutInterval: timeout
        )
        request.httpMethod = method
        request.httpShouldHandleCookies = false
        request.setValue(endpoint.origin, forHTTPHeaderField: "Origin")
        return request
    }

    private func endpoint(path: String) throws -> (url: URL, origin: String) {
        guard let baseURL,
              var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false),
              let scheme = components.scheme?.lowercased(),
              let host = components.host,
              ["http", "https"].contains(scheme),
              Self.isLoopback(host),
              components.user == nil,
              components.password == nil,
              components.query == nil,
              components.fragment == nil else {
            throw UsageAccountActionError.localBoardRequired
        }
        components.scheme = scheme
        components.path = path
        components.query = nil
        components.fragment = nil
        guard let url = components.url else {
            throw UsageAccountActionError.localBoardRequired
        }

        var origin = URLComponents()
        origin.scheme = scheme
        origin.host = host
        origin.port = components.port
        guard let originURL = origin.url else {
            throw UsageAccountActionError.localBoardRequired
        }
        return (url, originURL.absoluteString)
    }

    private func send(_ request: URLRequest) async throws -> UsageAccountActionResponse {
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw UsageAccountActionError.invalidResponse
        }
        guard data.count <= Self.maximumResponseBytes else {
            throw UsageAccountActionError.responseTooLarge
        }
        guard [200, 202, 400, 404, 409, 503].contains(http.statusCode) else {
            throw UsageAccountActionError.serverStatus(http.statusCode)
        }
        return try JSONDecoder().decode(UsageAccountActionResponse.self, from: data)
    }

    private static func privateSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpShouldSetCookies = false
        configuration.httpCookieAcceptPolicy = .never
        configuration.httpCookieStorage = nil
        configuration.urlCredentialStorage = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        return URLSession(configuration: configuration)
    }

    private static func isLoopback(_ host: String) -> Bool {
        let normalized = host.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        if normalized == "localhost" || normalized == "::1" { return true }
        let octets = normalized.split(separator: ".", omittingEmptySubsequences: false)
        return octets.count == 4 && Int(octets[0]) == 127 && octets.dropFirst().allSatisfy {
            guard let value = Int($0) else { return false }
            return (0...255).contains(value)
        }
    }
}
