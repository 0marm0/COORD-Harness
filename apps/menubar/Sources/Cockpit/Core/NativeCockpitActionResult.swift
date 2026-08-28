import Foundation

struct NativeCockpitActionResult: Equatable {
    var ok: Bool
    var actionID: String
    var crid: String
    var status: String
    var reason: String?
    var whereText: String?
    var eventID: Int?
    var resultID: String?
    var refreshHint: String?
    var httpStatus: Int?
    var replayCount: Int?
    var capabilityResultID: String?
    var cached: Bool?
    var operatorSummary: String?
    var matchedCount: Int?
    var appliedCount: Int?
    var failedCount: Int?
    var skippedCount: Int?

    init(dictionary raw: [String: Any]) {
        let response = raw["response"] as? [String: Any]
        let object = response ?? raw
        let detail = object["detail"] as? [String: Any]

        self.ok = NativeCockpitActionResult.bool(object["ok"] ?? raw["ok"])
        self.actionID = NativeCockpitActionResult.string(object["action_id"] ?? raw["action_id"])
        self.crid = NativeCockpitActionResult.string(object["crid"] ?? raw["crid"])
        self.status = NativeCockpitActionResult.string(object["status"] ?? raw["status"], fallback: ok ? "applied" : "denied")
        self.reason = NativeCockpitActionResult.optionalString(object["reason"] ?? raw["reason"])
        self.whereText = NativeCockpitActionResult.optionalString(object["where"] ?? raw["where"])
        self.eventID = NativeCockpitActionResult.int(object["event_id"] ?? raw["event_id"])
        self.resultID = NativeCockpitActionResult.optionalString(object["result_id"] ?? raw["result_id"])
        self.refreshHint = NativeCockpitActionResult.optionalString(object["refresh_hint"] ?? raw["refresh_hint"])
        self.httpStatus = NativeCockpitActionResult.int(object["http_status"] ?? raw["http_status"])
        self.replayCount = NativeCockpitActionResult.int(raw["replay_count"])
        self.capabilityResultID = NativeCockpitActionResult.optionalString(detail?["result_id"] ?? object["capability_result_id"])
        self.cached = NativeCockpitActionResult.optionalBool(detail?["cached"] ?? object["cached"])
        self.operatorSummary = NativeCockpitActionResult.summaryText(from: detail?["operator_summary"] ?? object["operator_summary"])
        self.matchedCount = NativeCockpitActionResult.int(detail?["matched"] ?? object["matched"])
        self.appliedCount = NativeCockpitActionResult.int(detail?["applied"] ?? object["applied"])
        self.failedCount = NativeCockpitActionResult.int(detail?["failed"] ?? object["failed"])
        self.skippedCount = NativeCockpitActionResult.count(detail?["skipped"] ?? object["skipped"])
    }

    init(data: Data) throws {
        let raw = try JSONSerialization.jsonObject(with: data)
        self.init(dictionary: raw as? [String: Any] ?? [:])
    }

    var statusLine: String {
        var parts: [String] = []
        let verb = ok ? "Applied" : status.capitalized
        parts.append(actionID.isEmpty ? verb : "\(verb) \(shortActionID)")
        if let whereText, !whereText.isEmpty { parts.append(whereText) }
        if let eventID { parts.append("event \(eventID)") }
        if let bulkSummary, !bulkSummary.isEmpty { parts.append(bulkSummary) }
        if let resultID, !resultID.isEmpty { parts.append("result \(resultID)") }
        if let capabilityResultID, !capabilityResultID.isEmpty { parts.append("capability \(capabilityResultID)") }
        if let reason, !reason.isEmpty { parts.append(reason) }
        return parts.joined(separator: " | ")
    }

    var cardValue: String {
        var parts = [status]
        if let whereText, !whereText.isEmpty { parts.append(whereText) }
        if let eventID { parts.append("event \(eventID)") }
        if replayCount.map({ $0 > 0 }) == true { parts.append("replayed \(replayCount!)") }
        return parts.joined(separator: " / ")
    }

    var cardDetail: String {
        var parts: [String] = []
        if let reason, !reason.isEmpty { parts.append(reason) }
        if let bulkSummary, !bulkSummary.isEmpty { parts.append(bulkSummary) }
        if let operatorSummary, !operatorSummary.isEmpty { parts.append(operatorSummary) }
        if let capabilityResultID, !capabilityResultID.isEmpty { parts.append("capability result \(capabilityResultID)") }
        if let resultID, !resultID.isEmpty { parts.append("native result \(resultID)") }
        if let refreshHint, !refreshHint.isEmpty { parts.append("refresh \(refreshHint)") }
        return parts.joined(separator: " | ")
    }

    private var shortActionID: String {
        if actionID.hasPrefix("native-"), actionID.count > 15 {
            return String(actionID.prefix(15))
        }
        return actionID
    }

    private var bulkSummary: String? {
        let parts = [
            appliedCount.map { "\($0) applied" },
            failedCount.flatMap { $0 > 0 ? "\($0) failed" : nil },
            skippedCount.flatMap { $0 > 0 ? "\($0) skipped" : nil },
            matchedCount.flatMap { count in
                (appliedCount == nil && failedCount == nil && skippedCount == nil) ? "\(count) matched" : nil
            },
        ].compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: ", ")
    }

    private static func string(_ value: Any?, fallback: String = "") -> String {
        optionalString(value) ?? fallback
    }

    private static func optionalString(_ value: Any?) -> String? {
        if let string = value as? String {
            let trimmed = string.trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.isEmpty ? nil : trimmed
        }
        if let number = value as? NSNumber {
            return number.stringValue
        }
        return nil
    }

    private static func bool(_ value: Any?) -> Bool {
        optionalBool(value) ?? false
    }

    private static func optionalBool(_ value: Any?) -> Bool? {
        if let bool = value as? Bool { return bool }
        if let number = value as? NSNumber { return number.boolValue }
        if let string = value as? String {
            switch string.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
            case "true", "1", "yes": return true
            case "false", "0", "no": return false
            default: return nil
            }
        }
        return nil
    }

    private static func int(_ value: Any?) -> Int? {
        if let int = value as? Int { return int }
        if let number = value as? NSNumber { return number.intValue }
        if let string = value as? String { return Int(string.trimmingCharacters(in: .whitespacesAndNewlines)) }
        return nil
    }

    private static func count(_ value: Any?) -> Int? {
        if let array = value as? [Any] { return array.count }
        return int(value)
    }

    private static func summaryText(from value: Any?) -> String? {
        if let string = optionalString(value) { return string }
        guard let dict = value as? [String: Any] else { return nil }
        let priority = ["title", "summary", "status", "message", "next_step", "command", "path"]
        let parts = priority.compactMap { key -> String? in
            guard let item = optionalString(dict[key]) else { return nil }
            return "\(key.replacingOccurrences(of: "_", with: " ")): \(item)"
        }
        if !parts.isEmpty { return parts.joined(separator: " / ") }
        return dict.keys.sorted().prefix(4).compactMap { key in
            optionalString(dict[key]).map { "\(key): \($0)" }
        }.joined(separator: " / ")
    }
}
