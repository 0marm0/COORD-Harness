import Foundation

struct NativeContextBridgeRequest: Encodable, Equatable {
    var id: String
    var command: String
    var query: String?
    var mode: String?
    var profile: String?
    var limit: Int?
    var pointer: String?
    var maxBytes: Int?

    static func search(
        id: String = UUID().uuidString,
        query: String,
        mode: NativeContextPaletteMode,
        profile: String = "work",
        limit: Int = 18
    ) -> NativeContextBridgeRequest {
        NativeContextBridgeRequest(
            id: id,
            command: "search",
            query: query,
            mode: mode.rawValue,
            profile: profile,
            limit: limit,
            pointer: nil,
            maxBytes: nil
        )
    }

    static func read(
        id: String = UUID().uuidString,
        pointer: String,
        maxBytes: Int = 12_000
    ) -> NativeContextBridgeRequest {
        NativeContextBridgeRequest(
            id: id,
            command: "read",
            query: nil,
            mode: nil,
            profile: nil,
            limit: nil,
            pointer: pointer,
            maxBytes: maxBytes
        )
    }

    static func stats(id: String = UUID().uuidString) -> NativeContextBridgeRequest {
        NativeContextBridgeRequest(id: id, command: "stats")
    }

    func encodedLineData() throws -> Data {
        let encoder = JSONEncoder()
        var data = try encoder.encode(self)
        data.append(0x0A)
        return data
    }
}

enum NativeContextBridgeClientError: Error, LocalizedError {
    case missingBridgeScript(String)
    case processFailed(Int32, String)
    case invalidOutput

    var errorDescription: String? {
        switch self {
        case .missingBridgeScript(let path):
            return "Native context bridge script not found at \(path)"
        case .processFailed(let status, let output):
            return "Native context bridge failed with status \(status): \(output)"
        case .invalidOutput:
            return "Native context bridge returned invalid output"
        }
    }
}

final class NativeContextBridgeClient {
    private let repoRoot: URL
    private let pythonPath: URL
    private let bridgeScript: URL

    init(
        repoRoot: URL = URL(fileURLWithPath: ProcessInfo.processInfo.environment["COORD_PROJECT_ROOT"]
            ?? FileManager.default.currentDirectoryPath),
        pythonPath: URL? = nil,
        bridgeScript: URL? = nil
    ) {
        self.repoRoot = repoRoot
        self.pythonPath = pythonPath ?? repoRoot.appendingPathComponent("coordharness/.venv/bin/python")
        self.bridgeScript = bridgeScript ?? repoRoot.appendingPathComponent("coordharness/scripts/native_context_bridge.py")
    }

    func search(
        query: String,
        mode: NativeContextPaletteMode,
        limit: Int = 18
    ) async throws -> NativeContextSearchResponse {
        let request = NativeContextBridgeRequest.search(query: query, mode: mode, limit: limit)
        let data = try await execute(request)
        return try JSONDecoder().decode(NativeContextSearchResponse.self, from: data)
    }

    func read(pointer: String, maxBytes: Int = 12_000) async throws -> NativeContextReadResponse {
        let request = NativeContextBridgeRequest.read(pointer: pointer, maxBytes: maxBytes)
        let data = try await execute(request)
        return try JSONDecoder().decode(NativeContextReadResponse.self, from: data)
    }

    func stats() async throws -> NativeContextSearchResponse {
        let request = NativeContextBridgeRequest.stats()
        let data = try await execute(request)
        return try JSONDecoder().decode(NativeContextSearchResponse.self, from: data)
    }

    private func execute(_ request: NativeContextBridgeRequest) async throws -> Data {
        let json = String(data: try request.encodedLineData(), encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? "{}"
        return try await Task.detached(priority: .userInitiated) { [repoRoot, pythonPath, bridgeScript] in
            guard FileManager.default.fileExists(atPath: bridgeScript.path) else {
                throw NativeContextBridgeClientError.missingBridgeScript(bridgeScript.path)
            }
            let process = Process()
            process.currentDirectoryURL = repoRoot
            process.executableURL = FileManager.default.fileExists(atPath: pythonPath.path)
                ? pythonPath
                : URL(fileURLWithPath: "/usr/bin/python3")
            process.arguments = [bridgeScript.path, "--once", json]
            let stdout = Pipe()
            let stderr = Pipe()
            process.standardOutput = stdout
            process.standardError = stderr
            try process.run()
            process.waitUntilExit()
            let out = stdout.fileHandleForReading.readDataToEndOfFile()
            let err = stderr.fileHandleForReading.readDataToEndOfFile()
            if process.terminationStatus != 0 {
                let output = String(data: out + err, encoding: .utf8) ?? ""
                throw NativeContextBridgeClientError.processFailed(process.terminationStatus, output)
            }
            guard !out.isEmpty else {
                throw NativeContextBridgeClientError.invalidOutput
            }
            return out
        }.value
    }
}
