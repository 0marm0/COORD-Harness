import Combine
import Foundation

#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

struct SystemTelemetrySnapshot: Decodable, Equatable, Sendable {
    struct Metric: Decodable, Equatable, Sendable {
        let usagePercent: Double?
        let availability: String
        let source: String?
        let error: String?
        let pCoreUsagePercent: Double?
        let eCoreUsagePercent: Double?
        let pCoreCount: Int?
        let eCoreCount: Int?
        let temperatureC: Double?
        let rendererPercent: Double?
        let tilerPercent: Double?
        let powerW: Double?
        let anePowerW: Double?

        var availablePercent: Double? {
            guard availability.lowercased() == "available", let usagePercent, usagePercent.isFinite else { return nil }
            return min(100, max(0, usagePercent))
        }
    }

    struct Memory: Decodable, Equatable, Sendable {
        let usedPercent: Double?
        let usedBytes: Int64?
        let totalBytes: Int64?
        let freeBytes: Int64?
        let appBytes: Int64?
        let wiredBytes: Int64?
        let compressedBytes: Int64?
        let swapUsedBytes: Int64?
        let swapTotalBytes: Int64?
        let pressure: String?
        let availability: String
        let source: String?
        let error: String?

        var availablePercent: Double? {
            guard availability.lowercased() == "available", let usedPercent, usedPercent.isFinite else { return nil }
            return min(100, max(0, usedPercent))
        }
    }

    /// App, Wired, Compressed, and Free reconcile to installed physical memory.
    /// Swap remains metadata and is never drawn as a physical-memory slice.
    struct MemoryRingComposition: Equatable, Sendable {
        enum SegmentKind: Equatable, Sendable { case app, wired, compressed, free }
        struct Segment: Equatable, Sendable {
            let kind: SegmentKind
            let bytes: Int64
            let fraction: Double
        }

        let physicalUsedBytes: Int64?
        let physicalFreeBytes: Int64?
        let appBytes: Int64?
        let wiredBytes: Int64?
        let compressedBytes: Int64?
        let swapUsedBytes: Int64?
        let denominatorBytes: Int64?
        let centerUsedPercent: Double?
        let segments: [Segment]
        let usesFallbackArc: Bool

        static func make(
            usedPercent: Double?, usedBytes: Int64?, totalBytes: Int64?,
            freeBytes: Int64?, appBytes: Int64?, wiredBytes: Int64?,
            compressedBytes: Int64?, swapUsedBytes: Int64?
        ) -> Self {
            let finitePercent = usedPercent.flatMap { $0.isFinite ? min(100, max(0, $0)) : nil }
            guard let totalBytes, totalBytes > 0 else {
                return Self(
                    physicalUsedBytes: nil, physicalFreeBytes: nil,
                    appBytes: nil, wiredBytes: nil, compressedBytes: nil,
                    swapUsedBytes: swapUsedBytes.map { max(0, $0) }, denominatorBytes: nil,
                    centerUsedPercent: finitePercent, segments: [], usesFallbackArc: finitePercent != nil
                )
            }

            if let appBytes, let wiredBytes, let compressedBytes {
                let wired = min(totalBytes, max(0, wiredBytes))
                let compressed = min(totalBytes - wired, max(0, compressedBytes))
                let app = min(totalBytes - wired - compressed, max(0, appBytes))
                let physicalUsed = app + wired + compressed
                let physicalFree = totalBytes - physicalUsed
                let segments = [
                    Segment(kind: .app, bytes: app, fraction: Double(app) / Double(totalBytes)),
                    Segment(kind: .wired, bytes: wired, fraction: Double(wired) / Double(totalBytes)),
                    Segment(kind: .compressed, bytes: compressed, fraction: Double(compressed) / Double(totalBytes)),
                    Segment(kind: .free, bytes: physicalFree, fraction: Double(physicalFree) / Double(totalBytes)),
                ]
                return Self(
                    physicalUsedBytes: physicalUsed, physicalFreeBytes: physicalFree,
                    appBytes: app, wiredBytes: wired, compressedBytes: compressed,
                    swapUsedBytes: swapUsedBytes.map { max(0, $0) }, denominatorBytes: totalBytes,
                    centerUsedPercent: Double(physicalUsed) / Double(totalBytes) * 100,
                    segments: segments, usesFallbackArc: false
                )
            }

            let physicalUsed = finitePercent.map { Int64((Double(totalBytes) * $0 / 100).rounded()) }
                ?? usedBytes.map { min(totalBytes, max(0, $0)) }
                ?? freeBytes.map { totalBytes - min(totalBytes, max(0, $0)) }
            let physicalFree = physicalUsed.map { totalBytes - $0 }
            return Self(
                physicalUsedBytes: physicalUsed, physicalFreeBytes: physicalFree,
                appBytes: nil, wiredBytes: nil, compressedBytes: nil,
                swapUsedBytes: swapUsedBytes.map { max(0, $0) }, denominatorBytes: totalBytes,
                centerUsedPercent: physicalUsed.map { Double($0) / Double(totalBytes) * 100 },
                segments: [], usesFallbackArc: physicalUsed != nil
            )
        }

        static func make(_ metric: Memory?) -> Self {
            make(
                usedPercent: metric?.usedPercent, usedBytes: metric?.usedBytes,
                totalBytes: metric?.totalBytes, freeBytes: metric?.freeBytes,
                appBytes: metric?.appBytes, wiredBytes: metric?.wiredBytes,
                compressedBytes: metric?.compressedBytes,
                swapUsedBytes: metric?.swapUsedBytes
            )
        }
    }

    struct Disk: Decodable, Equatable, Sendable {
        let usedPercent: Double?
        let usedBytes: Int64?
        let totalBytes: Int64?
        let freeBytes: Int64?
        let readBps: Double?
        let writeBps: Double?
        let availability: String
        let source: String?
        let error: String?

        var availablePercent: Double? {
            guard availability.lowercased() == "available", let usedPercent, usedPercent.isFinite else { return nil }
            return min(100, max(0, usedPercent))
        }
    }

    struct Cadence: Decodable, Equatable, Sendable {
        let mode: String?
        let intervalSeconds: Double?
        let demandActive: Bool?
    }

    struct Freshness: Decodable, Equatable, Sendable {
        let state: String
        let ageSeconds: Double?
    }

    let schemaVersion: Int
    let generatedAt: String?
    let sequence: Int?
    let staleAfterSeconds: Double?
    let enabled: Bool?
    let profile: String?
    let cadence: Cadence?
    let freshness: Freshness?
    let cpu: Metric
    let gpu: Metric
    let memory: Memory
    let disk: Disk

    var isStale: Bool { freshness?.state != "fresh" }
}

struct SystemTelemetryDiskCapacity: Equatable, Sendable {
    let totalBytes: Int64
    let freeBytes: Int64

    var usedBytes: Int64 { max(0, totalBytes - min(totalBytes, freeBytes)) }
    var usedPercent: Double? {
        guard totalBytes > 0 else { return nil }
        return Double(usedBytes) / Double(totalBytes) * 100
    }

    static func resolve(
        totalBytes: Int64?,
        importantUsageAvailableBytes: Int64?,
        availableBytes: Int64?
    ) -> SystemTelemetryDiskCapacity? {
        guard let totalBytes, totalBytes > 0 else { return nil }
        guard let candidate = importantUsageAvailableBytes ?? availableBytes, candidate >= 0 else { return nil }
        return SystemTelemetryDiskCapacity(totalBytes: totalBytes, freeBytes: min(totalBytes, candidate))
    }

    static func presentation(for disk: SystemTelemetrySnapshot.Disk) -> SystemTelemetryDiskCapacity? {
        macOSSystemVolume() ?? resolve(
            totalBytes: disk.totalBytes,
            importantUsageAvailableBytes: nil,
            availableBytes: disk.freeBytes
        )
    }

    private static func macOSSystemVolume() -> SystemTelemetryDiskCapacity? {
        #if os(macOS)
        let keys: Set<URLResourceKey> = [
            .volumeTotalCapacityKey,
            .volumeAvailableCapacityForImportantUsageKey,
            .volumeAvailableCapacityKey,
        ]
        guard let values = try? URL(fileURLWithPath: "/", isDirectory: true).resourceValues(forKeys: keys) else { return nil }
        return resolve(
            totalBytes: values.volumeTotalCapacity.map(Int64.init),
            importantUsageAvailableBytes: values.volumeAvailableCapacityForImportantUsage,
            availableBytes: values.volumeAvailableCapacity.map(Int64.init)
        )
        #else
        return nil
        #endif
    }
}

enum SystemTelemetryError: LocalizedError, Equatable {
    case loopbackRequired
    case invalidResponse
    case httpStatus(Int)
    case unsupportedSchema(Int)

    var errorDescription: String? {
        switch self {
        case .loopbackRequired: "System telemetry is available only from the loopback COORD board."
        case .invalidResponse: "The system telemetry service returned an invalid response."
        case let .httpStatus(status): "The system telemetry service returned HTTP \(status)."
        case let .unsupportedSchema(version): "Unsupported system telemetry schema \(version)."
        }
    }
}

protocol SystemTelemetryFetching: Sendable {
    func fetchSystemTelemetry(baseURL: URL) async throws -> SystemTelemetrySnapshot
}

struct SystemTelemetryClient: SystemTelemetryFetching, Sendable {
    let session: URLSession
    let timeout: TimeInterval

    init(session: URLSession = .shared, timeout: TimeInterval = 5) {
        self.session = session
        self.timeout = timeout
    }

    func fetchSystemTelemetry(baseURL: URL) async throws -> SystemTelemetrySnapshot {
        guard let host = baseURL.host, Self.isLoopback(host) else { throw SystemTelemetryError.loopbackRequired }
        let baseEndpoint = baseURL.appending(path: "api/v1/system-telemetry")
        var components = URLComponents(url: baseEndpoint, resolvingAgainstBaseURL: false)
        components?.queryItems = [URLQueryItem(name: "demand", value: "1")]
        guard let url = components?.url else { throw SystemTelemetryError.invalidResponse }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else { throw SystemTelemetryError.invalidResponse }
        guard (200..<300).contains(response.statusCode) else { throw SystemTelemetryError.httpStatus(response.statusCode) }
        let snapshot = try Self.decode(data)
        guard snapshot.schemaVersion == 1 else { throw SystemTelemetryError.unsupportedSchema(snapshot.schemaVersion) }
        return snapshot
    }

    static func decode(_ data: Data) throws -> SystemTelemetrySnapshot {
        let decoder = JSONDecoder()
        // The canonical loopback contract is snake_case at every nesting level
        // (`usage_percent`, `used_bytes`, `interval_seconds`, ...). Without this
        // strategy the top-level CodingKeys decoded but every metric failed,
        // leaving a permanently visible SYS N/A status item.
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(SystemTelemetrySnapshot.self, from: data)
    }

    private static func isLoopback(_ host: String) -> Bool {
        let normalized = host.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        if normalized == "localhost" || normalized == "::1" { return true }
        let parts = normalized.split(separator: ".", omittingEmptySubsequences: false)
        return parts.count == 4 && Int(parts[0]) == 127 && parts.dropFirst().allSatisfy {
            guard let value = Int($0) else { return false }
            return (0...255).contains(value)
        }
    }
}

@MainActor
final class SystemTelemetryStore: ObservableObject {
    @Published private(set) var snapshot: SystemTelemetrySnapshot?
    @Published private(set) var error: String?
    var onStateChange: ((SystemTelemetrySnapshot?) -> Void)?
    private let client: any SystemTelemetryFetching
    private var refreshing = false

    init(client: any SystemTelemetryFetching = SystemTelemetryClient()) {
        self.client = client
    }

    func refresh(baseURL: URL) async {
        guard !refreshing else { return }
        refreshing = true
        defer { refreshing = false }
        do {
            snapshot = try await client.fetchSystemTelemetry(baseURL: baseURL)
            onStateChange?(snapshot)
            error = nil
        } catch {
            self.error = error.localizedDescription
            snapshot = nil
            onStateChange?(nil)
        }
    }
}
