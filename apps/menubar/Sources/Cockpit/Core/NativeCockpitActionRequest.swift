import Foundation

enum NativeCockpitActionRequestBuilder {
    static func body(
        actionID: String,
        action: String,
        row: CockpitRow?,
        payload: [String: Any]
    ) -> [String: Any]? {
        guard let row,
              let workID = nonempty(row.workID),
              let expectedVersion = row.workVersion,
              let expectedAssignee = nonempty(row.currentAssignee),
              ["claude", "codex"].contains(expectedAssignee)
        else { return nil }

        let nativeAction: String
        let ownerLane: String
        switch action {
        case "task.assign.claude":
            nativeAction = "work.reassign"
            ownerLane = "claude"
        case "task.assign.codex":
            nativeAction = "work.reassign"
            ownerLane = "codex"
        case "handoff.create":
            nativeAction = "handoff.create"
            ownerLane = nonempty(payload["to"] as? String) ?? ""
        default:
            return nil
        }
        guard ["claude", "codex"].contains(ownerLane), ownerLane != expectedAssignee,
              payload["confirmed"] as? Bool == true else { return nil }

        let task = nonempty(payload["task"] as? String)
            ?? "Transfer \(workID) to \(ownerLane)"
        let why = nonempty(payload["why"] as? String)
            ?? "Operator confirmed native reassignment."
        let acceptance = nonempty(payload["acceptance"] as? String)
            ?? nonempty(row.acceptanceSummary)
            ?? "Continue against the existing declared done signal."
        let refs = stringList(payload["refs"], fallback: ["coord://work/\(workID)"])
        let constraints = stringList(
            payload["constraints"],
            fallback: ["Preserve the declared done signal and current evidence."]
        )
        return [
            "schema_version": 1,
            "action_id": actionID,
            "source_face": "native_cockpit",
            "actor": "operator",
            "action": nativeAction,
            "target": [
                "work_id": workID,
                "expected_version": expectedVersion,
                "expected_assignee": expectedAssignee,
                "expected_head_event_ids": row.assignmentHeadEventIDs,
            ],
            "payload": [
                "owner_lane": ownerLane,
                "target_intent": "queued",
                "task": task,
                "why": why,
                "acceptance": acceptance,
                "refs": refs,
                "constraints": constraints,
                "release_held_claim": false,
                "confirmed": true,
            ],
            "dry_run": false,
        ]
    }

    private static func nonempty(_ raw: String?) -> String? {
        guard let value = raw?.trimmingCharacters(in: .whitespacesAndNewlines),
              !value.isEmpty else { return nil }
        return value
    }

    private static func stringList(_ raw: Any?, fallback: [String]) -> [String] {
        let values = (raw as? [String] ?? [])
            .compactMap(nonempty)
        return values.isEmpty ? fallback : values
    }
}
