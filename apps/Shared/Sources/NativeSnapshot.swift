import Foundation

struct NativeSnapshotV1: Codable, Equatable, Sendable {
    let schemaVersion: String
    let generatedAt: Date
    let source: String
    let stale: Bool
    let summary: Summary
    let rows: [Row]
    let sessions: [Session]?

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case generatedAt = "generated_at"
        case source
        case stale
        case summary
        case rows
        case sessions
    }

    struct Summary: Codable, Equatable, Sendable {
        let running: Int
        let attention: Int
        let next: Int
        let done: Int
        let total: Int
    }

    struct Row: Codable, Equatable, Identifiable, Sendable {
        let id: String
        let title: String
        let status: String
        let bucket: String
        let owner: String
        let module: String
        let group: String
        let priority: Int
        let progressFraction: Double?
        let etaSeconds: Int?
        let stale: Bool
        let currentStep: String

        enum CodingKeys: String, CodingKey {
            case id, title, status, bucket, owner, module, group, priority, stale
            case progressFraction = "progress_fraction"
            case etaSeconds = "eta_seconds"
            case currentStep = "current_step"
        }
    }

    struct Session: Codable, Equatable, Identifiable, Sendable {
        let id: String
        let actor: String
        let label: String
        let live: Bool
    }

    func validated() throws -> NativeSnapshotV1 {
        guard schemaVersion == "1" else {
            throw SnapshotError.invalidPayload("Unsupported schema version")
        }
        guard summary.running >= 0, summary.attention >= 0, summary.next >= 0,
              summary.done >= 0, summary.total >= 0 else {
            throw SnapshotError.invalidPayload("Summary counts must not be negative")
        }
        guard rows.allSatisfy({ (0...1).contains($0.progressFraction ?? 0) }) else {
            throw SnapshotError.invalidPayload("Progress must be between zero and one")
        }
        guard Set(rows.map(\.id)).count == rows.count else {
            throw SnapshotError.invalidPayload("Row identifiers must be unique")
        }
        return self
    }
}

enum SnapshotCoding {
    static func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }

    static func encoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.sortedKeys]
        return encoder
    }
}

enum SnapshotError: LocalizedError, Equatable {
    case invalidEndpoint
    case insecureEndpoint
    case usageRequiresLoopback
    case invalidResponse
    case serverStatus(Int)
    case invalidPayload(String)

    var errorDescription: String? {
        switch self {
        case .invalidEndpoint: "Enter a valid HTTPS endpoint or a loopback HTTP endpoint."
        case .insecureEndpoint: "HTTP is allowed only for loopback hosts. Use HTTPS for every other host."
        case .usageRequiresLoopback: "Provider usage is available only from the loopback CORD board."
        case .invalidResponse: "The server returned an invalid response."
        case let .serverStatus(code): "The server returned HTTP \(code)."
        case let .invalidPayload(reason): "The snapshot is invalid: \(reason)."
        }
    }
}
