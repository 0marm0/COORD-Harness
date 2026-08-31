import Foundation
import Darwin

enum NativeOperatorTokenSource {
    static let fixedActionEndpoint: URL = {
        var components = URLComponents()
        components.scheme = "http"
        components.host = "127.0.0.1"
        components.port = HarnessEndpoint.defaultPort
        components.path = "/api/native/action"
        return components.url!
    }()

    static func load(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        databasePath: String? = CoordDatabasePath.resolve(),
        fileManager: FileManager = .default
    ) -> String? {
        if let direct = valid(environment["COORD_NATIVE_OPERATOR_TOKEN"]) {
            return direct
        }
        let tokenURL: URL
        if let configured = nonempty(environment["COORD_NATIVE_OPERATOR_TOKEN_FILE"]) {
            tokenURL = URL(fileURLWithPath: (configured as NSString).expandingTildeInPath)
        } else if let databasePath = nonempty(databasePath) {
            tokenURL = URL(fileURLWithPath: databasePath)
                .deletingLastPathComponent()
                .appendingPathComponent("operator-token", isDirectory: false)
        } else {
            return nil
        }
        guard let attributes = try? fileManager.attributesOfItem(atPath: tokenURL.path),
              attributes[.type] as? FileAttributeType == .typeRegular,
              let permissions = (attributes[.posixPermissions] as? NSNumber)?.intValue,
              permissions & 0o077 == 0,
              let ownerID = (attributes[.ownerAccountID] as? NSNumber)?.uint32Value,
              ownerID == getuid(),
              let data = try? Data(contentsOf: tokenURL, options: .mappedIfSafe),
              data.count <= 512,
              let raw = String(data: data, encoding: .utf8)
        else { return nil }
        return valid(raw)
    }

    private static func valid(_ raw: String?) -> String? {
        guard let token = nonempty(raw), (32...256).contains(token.count),
              token.range(of: #"^[A-Za-z0-9._~-]+$"#, options: .regularExpression) != nil
        else { return nil }
        return token
    }

    private static func nonempty(_ raw: String?) -> String? {
        guard let value = raw?.trimmingCharacters(in: .whitespacesAndNewlines),
              !value.isEmpty else { return nil }
        return value
    }
}
