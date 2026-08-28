import Foundation

enum NativeCockpitActionRequestBuilder {
    static func body(
        actionID: String,
        action: String,
        row: CockpitRow?,
        payload: [String: Any]
    ) -> [String: Any] {
        [
            "schema_version": 1,
            "action_id": actionID,
            "crid": actionID,
            "source_face": "native_cockpit",
            "actor": "operator",
            "action": action,
            "target": target(for: row, action: action),
            "payload": payload,
            "dry_run": false,
        ]
    }

    static func target(for row: CockpitRow?, action: String) -> [String: Any] {
        guard let row else { return [:] }
        var out: [String: Any] = [:]
        let workID = row.workID?.trimmingCharacters(in: .whitespacesAndNewlines)
        if let workID, !workID.isEmpty {
            out["work_id"] = workID
        }
        if action.hasPrefix("jobs.") {
            let jobID = row.jobID?.trimmingCharacters(in: .whitespacesAndNewlines)
            if let jobID, !jobID.isEmpty {
                out["job_id"] = jobID
            } else if let workID, !workID.isEmpty {
                out["job_id"] = workID
            }
        }
        out["dedup_key"] = row.dedupKey
        return out
    }
}
