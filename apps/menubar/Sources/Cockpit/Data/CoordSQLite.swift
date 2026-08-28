import Foundation
import SQLite3

enum SQLiteValue: Equatable {
    case integer(Int64)
    case real(Double)
    case text(String)
    case blob(Data)
    case null

    var int: Int? {
        switch self {
        case .integer(let value): return Int(value)
        case .real(let value): return Int(value)
        case .text(let value): return Int(value)
        case .blob, .null: return nil
        }
    }

    var int64: Int64? {
        switch self {
        case .integer(let value): return value
        case .real(let value): return Int64(value)
        case .text(let value): return Int64(value)
        case .blob, .null: return nil
        }
    }

    var double: Double? {
        switch self {
        case .integer(let value): return Double(value)
        case .real(let value): return value
        case .text(let value): return Double(value)
        case .blob, .null: return nil
        }
    }

    var string: String? {
        switch self {
        case .integer(let value): return String(value)
        case .real(let value): return String(value)
        case .text(let value): return value
        case .blob, .null: return nil
        }
    }

    var bool: Bool? {
        switch self {
        case .integer(let value): return value != 0
        case .real(let value): return value != 0
        case .text(let value):
            let normalized = value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            if ["1", "true", "yes"].contains(normalized) { return true }
            if ["0", "false", "no"].contains(normalized) { return false }
            return nil
        case .blob, .null:
            return nil
        }
    }
}

struct CoordSQLiteError: Error, CustomStringConvertible {
    var operation: String
    var code: Int32
    var message: String
    var sql: String?

    var description: String {
        if let sql { return "\(operation) failed (\(code)): \(message) [\(sql)]" }
        return "\(operation) failed (\(code)): \(message)"
    }
}

enum CoordDatabasePath {
    static let persistedPathKey = "coordharness.coordDBPath"

    static func resolve(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        persistedPath: String? = UserDefaults.standard.string(forKey: persistedPathKey),
        homeDirectory: String = NSHomeDirectory()
    ) -> String? {
        if let explicit = nonempty(environment["COORD_DB"]) {
            return normalized(explicit, homeDirectory: homeDirectory)
        }
        if let persistedPath = nonempty(persistedPath) {
            return normalized(persistedPath, homeDirectory: homeDirectory)
        }
        return nil
    }

    private static func nonempty(_ value: String?) -> String? {
        guard let value = value?.trimmingCharacters(in: .whitespacesAndNewlines),
              !value.isEmpty else { return nil }
        return value
    }

    private static func normalized(_ raw: String, homeDirectory: String) -> String {
        let expanded: String
        if raw == "~" {
            expanded = homeDirectory
        } else if raw.hasPrefix("~/") {
            expanded = homeDirectory + raw.dropFirst()
        } else {
            expanded = raw
        }
        return URL(fileURLWithPath: expanded).standardizedFileURL.path
    }
}

final class CoordSQLite {
    static let defaultPath = CoordDatabasePath.resolve() ?? ""

    private var handle: OpaquePointer?

    private init(handle: OpaquePointer) {
        self.handle = handle
    }

    deinit {
        close()
    }

    static func openReadOnly(path: String = defaultPath, busyTimeoutMS: Int32 = 1_500) throws -> CoordSQLite {
        let configuredPath = path.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !configuredPath.isEmpty else {
            throw CoordSQLiteError(
                operation: "resolve database",
                code: SQLITE_CANTOPEN,
                message: "COORD setup is required. Run apps/install.sh or select a coord.db in COORD settings.",
                sql: nil
            )
        }
        var db: OpaquePointer?
        let flags = SQLITE_OPEN_READONLY | SQLITE_OPEN_FULLMUTEX
        let code = sqlite3_open_v2(configuredPath, &db, flags, nil)
        guard code == SQLITE_OK, let opened = db else {
            let message = db.map { String(cString: sqlite3_errmsg($0)) } ?? "unable to open database"
            if let db { sqlite3_close(db) }
            throw CoordSQLiteError(
                operation: "open read-only",
                code: code,
                message: "\(message). Verify the database selected during COORD setup.",
                sql: configuredPath
            )
        }

        sqlite3_extended_result_codes(opened, 1)
        sqlite3_busy_timeout(opened, busyTimeoutMS)
        let connection = CoordSQLite(handle: opened)
        do {
            try connection.execute("PRAGMA query_only=ON")
        } catch {
            connection.close()
            throw error
        }
        return connection
    }

    func close() {
        if let handle {
            sqlite3_close(handle)
            self.handle = nil
        }
    }

    func execute(_ sql: String) throws {
        let statement = try prepare(sql)
        defer { sqlite3_finalize(statement) }
        let code = sqlite3_step(statement)
        guard code == SQLITE_DONE || code == SQLITE_ROW else {
            throw error(operation: "step", code: code, sql: sql)
        }
    }

    func rows(_ sql: String, bindings: [SQLiteValue] = []) throws -> [[String: SQLiteValue]] {
        let statement = try prepare(sql)
        defer { sqlite3_finalize(statement) }
        try bind(bindings, to: statement, sql: sql)

        var output: [[String: SQLiteValue]] = []
        while true {
            let code = sqlite3_step(statement)
            if code == SQLITE_DONE { break }
            guard code == SQLITE_ROW else {
                throw error(operation: "step", code: code, sql: sql)
            }
            var row: [String: SQLiteValue] = [:]
            for index in 0..<sqlite3_column_count(statement) {
                let name = String(cString: sqlite3_column_name(statement, index))
                row[name] = value(statement, index: index)
            }
            output.append(row)
        }
        return output
    }

    func intValue(_ sql: String, bindings: [SQLiteValue] = []) throws -> Int? {
        try rows(sql, bindings: bindings).first?.values.first?.int
    }

    func stringValue(_ sql: String, bindings: [SQLiteValue] = []) throws -> String? {
        try rows(sql, bindings: bindings).first?.values.first?.string
    }

    func tableExists(_ name: String) throws -> Bool {
        let count = try intValue(
            "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
            bindings: [.text(name)]
        ) ?? 0
        return count > 0
    }

    func columnNames(table name: String) throws -> Set<String> {
        let table = Self.quotedIdentifier(name)
        let rows = try rows("PRAGMA table_info(\(table))")
        return Set(rows.compactMap { $0["name"]?.string })
    }

    static func quotedIdentifier(_ raw: String) -> String {
        "\"\(raw.replacingOccurrences(of: "\"", with: "\"\""))\""
    }

    private func prepare(_ sql: String) throws -> OpaquePointer {
        guard let handle else {
            throw CoordSQLiteError(operation: "prepare", code: SQLITE_MISUSE, message: "database is closed", sql: sql)
        }
        var statement: OpaquePointer?
        let code = sqlite3_prepare_v2(handle, sql, -1, &statement, nil)
        guard code == SQLITE_OK, let prepared = statement else {
            throw error(operation: "prepare", code: code, sql: sql)
        }
        return prepared
    }

    private func bind(_ values: [SQLiteValue], to statement: OpaquePointer, sql: String) throws {
        for (offset, value) in values.enumerated() {
            let index = Int32(offset + 1)
            let code: Int32
            switch value {
            case .integer(let value):
                code = sqlite3_bind_int64(statement, index, value)
            case .real(let value):
                code = sqlite3_bind_double(statement, index, value)
            case .text(let value):
                code = sqlite3_bind_text(statement, index, value, -1, SQLITE_TRANSIENT)
            case .blob(let data):
                code = data.withUnsafeBytes { buffer in
                    sqlite3_bind_blob(statement, index, buffer.baseAddress, Int32(data.count), SQLITE_TRANSIENT)
                }
            case .null:
                code = sqlite3_bind_null(statement, index)
            }
            guard code == SQLITE_OK else {
                throw error(operation: "bind", code: code, sql: sql)
            }
        }
    }

    private func value(_ statement: OpaquePointer, index: Int32) -> SQLiteValue {
        switch sqlite3_column_type(statement, index) {
        case SQLITE_INTEGER:
            return .integer(sqlite3_column_int64(statement, index))
        case SQLITE_FLOAT:
            return .real(sqlite3_column_double(statement, index))
        case SQLITE_TEXT:
            guard let text = sqlite3_column_text(statement, index) else { return .null }
            return .text(String(cString: text))
        case SQLITE_BLOB:
            let bytes = sqlite3_column_blob(statement, index)
            let count = Int(sqlite3_column_bytes(statement, index))
            guard let bytes, count > 0 else { return .blob(Data()) }
            return .blob(Data(bytes: bytes, count: count))
        default:
            return .null
        }
    }

    private func error(operation: String, code: Int32, sql: String?) -> CoordSQLiteError {
        let message = handle.map { String(cString: sqlite3_errmsg($0)) } ?? "database is closed"
        return CoordSQLiteError(operation: operation, code: code, message: message, sql: sql)
    }
}

private let SQLITE_TRANSIENT = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

extension Dictionary where Key == String, Value == SQLiteValue {
    func string(_ key: String) -> String? { self[key]?.string }
    func int(_ key: String) -> Int? { self[key]?.int }
    func int64(_ key: String) -> Int64? { self[key]?.int64 }
    func double(_ key: String) -> Double? { self[key]?.double }
    func bool(_ key: String) -> Bool? { self[key]?.bool }
}
