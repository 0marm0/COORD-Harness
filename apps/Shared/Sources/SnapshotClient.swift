import Foundation

#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

protocol SnapshotFetching: Sendable {
    func fetchSnapshot(baseURL: URL) async throws -> NativeSnapshotV1
    func fetchUsage(baseURL: URL) async throws -> UsageIntelligenceSnapshot
    func fetchHealth(baseURL: URL) async throws
}

struct SnapshotClient: SnapshotFetching, Sendable {
    let session: URLSession

    init(session: URLSession = .shared) {
        self.session = session
    }

    func snapshotURL(baseURL: URL) throws -> URL {
        try endpointURL(baseURL: baseURL, path: "api/v1/snapshot")
    }

    func healthURL(baseURL: URL) throws -> URL {
        try endpointURL(baseURL: baseURL, path: "healthz")
    }

    func usageURL(baseURL: URL) throws -> URL {
        guard let host = baseURL.host, Self.isLoopbackHost(host) else {
            throw SnapshotError.usageRequiresLoopback
        }
        return try endpointURL(baseURL: baseURL, path: "api/v1/usage-dashboard")
    }

    func fetchSnapshot(baseURL: URL) async throws -> NativeSnapshotV1 {
        let data = try await get(url: snapshotURL(baseURL: baseURL))
        return try SnapshotCoding.decoder().decode(NativeSnapshotV1.self, from: data).validated()
    }

    func fetchUsage(baseURL: URL) async throws -> UsageIntelligenceSnapshot {
        let data = try await get(url: usageURL(baseURL: baseURL))
        return try SnapshotCoding.decoder().decode(UsageIntelligenceSnapshot.self, from: data)
    }

    func fetchHealth(baseURL: URL) async throws {
        _ = try await get(url: healthURL(baseURL: baseURL))
    }

    func fetchSystemTelemetry(baseURL: URL) async throws -> SystemTelemetrySnapshot {
        try await SystemTelemetryClient(session: session).fetchSystemTelemetry(baseURL: baseURL)
    }

    private func endpointURL(baseURL: URL, path: String) throws -> URL {
        guard let scheme = baseURL.scheme?.lowercased(),
              let host = baseURL.host,
              scheme == "http" || scheme == "https" else {
            throw SnapshotError.invalidEndpoint
        }
        if scheme == "http", !Self.isLoopbackHost(host) {
            throw SnapshotError.insecureEndpoint
        }
        return baseURL.appending(path: path)
    }

    private static func isLoopbackHost(_ host: String) -> Bool {
        let normalized = host
            .lowercased()
            .trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        if normalized == "localhost" || normalized == "::1" { return true }
        let octets = normalized.split(separator: ".", omittingEmptySubsequences: false)
        guard octets.count == 4,
              let first = Int(octets[0]), first == 127,
              octets.dropFirst().allSatisfy({ octet in
                  guard let value = Int(octet) else { return false }
                  return (0...255).contains(value)
              }) else {
            return false
        }
        return true
    }

    private func get(url: URL) async throws -> Data {
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 10
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw SnapshotError.invalidResponse
        }
        guard (200...299).contains(http.statusCode) else {
            throw SnapshotError.serverStatus(http.statusCode)
        }
        return data
    }
}
