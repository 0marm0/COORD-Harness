import Foundation

final class CockpitHTTPFallbackSource {
    static let defaultURL = URL(string: "\(HarnessEndpoint.base)/api/state/compact?profile=native&plane=all")!

    private let url: URL
    private let session: URLSession
    init(url: URL = CockpitHTTPFallbackSource.defaultURL, session: URLSession = .shared) {
        self.url = url
        self.session = session
    }

    func load() async -> CockpitState {
        // Shared with CockpitProjectionRefresher, which polls this same URL:
        // latching only one of them left the other asking every 1.5 seconds.
        if let declined = DeclinedRoutes.declinedStatus(url) {
            return CockpitState.error(CockpitLoadErrorState(
                kind: .transport,
                message: "HTTP fallback is not served here (status \(declined)); this client stays on its own read model."
            ))
        }
        do {
            let (data, response) = try await session.data(from: url)
            if let http = response as? HTTPURLResponse, http.statusCode != 200 {
                DeclinedRoutes.record(url, status: http.statusCode)
                return CockpitState.error(CockpitLoadErrorState(
                    kind: .transport,
                    message: "HTTP fallback returned status \(http.statusCode)"
                ))
            }
            return try Self.decode(data)
        } catch let error as CockpitLoadErrorState {
            return CockpitState.error(error)
        } catch {
            return CockpitState.error(CockpitLoadErrorState(
                kind: .transport,
                message: "HTTP fallback failed: \(error)"
            ))
        }
    }

    static func decode(_ data: Data) throws -> CockpitState {
        guard let root = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw CockpitLoadErrorState(kind: .transport, message: "HTTP fallback payload is not an object")
        }

        if !root.dictionary("work_model").isEmpty && root.dictionaries("rows").isEmpty {
            return decodeCompactState(root)
        }

        if !root.dictionary("meta").isEmpty
            && (root.string("contract") == "native_cockpit.v1" || root.dictionary("meta").string("contract") == "native_cockpit.v1") {
            return decodeNativeEnvelope(root)
        }

        let rows = root.dictionaries("rows").map { Self.row($0) }
        let columns = root.dictionaries("columns").map(Self.column)
        return CockpitState(
            schemaVersion: root.int("schema_version") ?? 0,
            writerSeq: root.int64("writer_seq") ?? 0,
            builtAt: root.string("built_at"),
            sourceVersion: root.string("source_version"),
            stale: root.bool("stale") ?? false,
            refreshing: root.bool("refreshing") ?? false,
            mode: root.string("mode"),
            liveMode: root.string("live_mode"),
            summary: summary(root.dictionary("summary")),
            rows: rows,
            columns: columns.isEmpty ? CockpitColumn.webDefaults : columns,
            groups: root.dictionaries("groups").map(Self.group),
            filterOptions: root.dictionaries("filter_options").map(Self.filterOption),
            sessions: root.dictionaries("sessions").map(Self.session),
            diagnostics: root.dictionaries("diagnostics").map(Self.diagnostic),
            error: nil
        )
    }

    private static func decodeNativeEnvelope(_ root: [String: Any]) -> CockpitState {
        let meta = root.dictionary("meta")
        let actionsByRow = nativeActionsByRow(root.dictionaries("row_actions"))
        let rows = root.dictionaries("rows").map { rowDict -> CockpitRow in
            var row = Self.row(rowDict)
            let keys = [
                row.dedupKey,
                row.workID,
                row.jobID,
                rowDict.string("row_id"),
                rowDict.string("id"),
                rowDict.string("coord_work_id"),
                rowDict.string("roadmap_id"),
            ]
            .compactMap { $0?.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            for key in keys {
                if let actions = actionsByRow[key] {
                    row.actions = actions
                    break
                }
            }
            return row
        }
        let columns = root.dictionaries("column_model").map(Self.column)
        return CockpitState(
            schemaVersion: meta.int("schema_version") ?? root.int("schema_version") ?? 0,
            writerSeq: meta.int64("writer_seq") ?? root.int64("writer_seq") ?? 0,
            builtAt: meta.string("built_at") ?? root.string("built_at"),
            sourceVersion: meta.string("source_version") ?? root.string("source_version"),
            stale: meta.bool("stale") ?? root.bool("stale") ?? false,
            refreshing: meta.bool("refreshing") ?? root.bool("refreshing") ?? false,
            mode: meta.string("mode") ?? root.string("mode"),
            liveMode: meta.string("live_mode") ?? root.string("live_mode"),
            summary: summary(root.dictionary("summary")),
            rows: rows,
            columns: columns.isEmpty ? CockpitColumn.webDefaults : columns,
            groups: root.dictionaries("group_model").map(Self.group),
            filterOptions: root.dictionaries("filter_options").map(Self.filterOption),
            sessions: root.dictionaries("sessions").map(Self.session),
            diagnostics: root.dictionaries("diagnostics").map(Self.diagnostic),
            error: nil
        )
    }

    private static func decodeCompactState(_ root: [String: Any]) -> CockpitState {
        let workModel = root.dictionary("work_model")
        let bucketSpecs: [(key: String, scope: String, label: String)] = [
            ("running_rows", "running", "Running"),
            ("attention_rows", "attention", "Needs attention"),
            ("followup_rows", "followup", "Follow-up"),
            ("next_rows", "next", "Up next"),
            ("queue_active_rows", "queue_active", "Local queue"),
            ("queue_blocked_rows", "queue_blocked", "Queue blocked"),
            ("queue_rows", "queue", "Queue"),
            ("queue_terminal_rows", "queue_terminal", "Queue terminal"),
            ("done_rows", "done", "Done"),
        ]
        var rows: [CockpitRow] = []
        var groups: [CockpitGroup] = []
        var order = 0
        var seen = Set<String>()
        for spec in bucketSpecs {
            let bucketRows = workModel.dictionaries(spec.key)
            guard !bucketRows.isEmpty else { continue }
            groups.append(CockpitGroup(
                key: spec.scope,
                label: spec.label,
                displayOrder: order,
                count: bucketRows.count,
                isCollapsed: false
            ))
            for rowDict in bucketRows {
                let row = row(rowDict, fallbackScope: spec.scope, fallbackGroupLabel: spec.label, fallbackOrder: order)
                guard !row.dedupKey.isEmpty, !seen.contains(row.dedupKey) else { continue }
                seen.insert(row.dedupKey)
                rows.append(row)
                order += 1
            }
        }

        return CockpitState(
            schemaVersion: root.int("schema_version") ?? 0,
            writerSeq: root.int64("writer_seq") ?? root.int64("ts") ?? 0,
            builtAt: root.string("built_at") ?? root.string("ts"),
            sourceVersion: root.string("source"),
            stale: root.bool("stale") ?? false,
            refreshing: root.bool("refreshing") ?? false,
            mode: root.string("mode"),
            liveMode: root.string("live_mode"),
            summary: summary(workModel.dictionary("summary")),
            rows: rows,
            columns: CockpitColumn.webDefaults,
            groups: groups,
            filterOptions: compactFilterOptions(rows),
            sessions: root.dictionaries("coord_sessions").map(Self.session),
            diagnostics: compactDiagnostics(root.dictionary("diagnostics")),
            error: nil
        )
    }

    private static func summary(_ row: [String: Any]) -> CockpitSummary {
        CockpitSummary(
            running: row.int("running") ?? 0,
            attention: row.int("attention") ?? 0,
            next: row.int("next") ?? 0,
            local: row.int("local") ?? 0,
            doneToday: row.int("done_today") ?? 0,
            stale: row.int("stale") ?? 0,
            blocked: row.int("blocked") ?? 0,
            launchable: row.int("launchable") ?? 0
        )
    }

    private static func row(
        _ row: [String: Any],
        fallbackScope: String? = nil,
        fallbackGroupLabel: String? = nil,
        fallbackOrder: Int? = nil
    ) -> CockpitRow {
        let workID = row.string("work_id") ?? row.string("coord_work_id") ?? row.string("roadmap_id") ?? row.string("id")
        let scope = row.string("scope") ?? fallbackScope ?? ""
        let groupKey = row.string("group_key") ?? fallbackScope
        return CockpitRow(
            dedupKey: row.string("dedup_key") ?? workID ?? row.string("job_id") ?? "",
            workID: workID,
            parentID: row.string("parent") ?? row.string("parent_id"),
            jobID: row.string("job_id"),
            title: row.string("title") ?? row.string("display") ?? row.string("name") ?? "Task",
            status: row.string("status") ?? "unknown",
            scope: scope,
            owner: row.string("owner"),
            ownerGroup: row.string("owner_group"),
            module: row.string("module"),
            moduleLabel: row.string("module_label"),
            domainLabel: row.string("domain_label"),
            resourceClass: row.string("resource_class"),
            live: row.bool("live"),
            paused: row.bool("paused"),
            stale: row.bool("stale"),
            pct: row.double("pct"),
            pctDisplay: row.string("pct_display"),
            etaSeconds: row.double("eta_s"),
            etaText: row.string("eta_text"),
            etaDerived: row.bool("eta_derived"),
            rate: row.double("rate"),
            done: row.int("done"),
            total: row.int("total"),
            progressKind: row.string("progress_kind"),
            hasProgress: row.bool("has_progress"),
            determinate: row.bool("determinate"),
            whyText: row.string("why_text") ?? row.string("why_next") ?? row.string("current_step"),
            noteText: row.string("note_text") ?? row.string("note"),
            priority: row.string("priority"),
            rowKind: row.string("row_kind") ?? row.string("kind") ?? row.string("surface"),
            pid: row.int("pid"),
            pgid: row.int("pgid"),
            sidecarAgeSeconds: row.double("sidecar_age_s"),
            doneSignal: row.string("done_signal"),
            acceptanceSummary: row.string("acceptance_summary") ?? row.string("acceptance"),
            contextPackRef: row.string("context_pack_ref") ?? row.string("context_ref"),
            groupKey: groupKey,
            groupLabel: row.string("group_label") ?? fallbackGroupLabel,
            displayOrder: row.int("display_order") ?? fallbackOrder ?? 0,
            actions: compactActions(row)
        )
    }

    private static func action(_ row: [String: Any]) -> CockpitRowAction {
        CockpitRowAction(
            id: row.string("id") ?? row.string("action_id") ?? row.string("action") ?? "",
            label: row.string("label") ?? row.string("id") ?? row.string("action_id") ?? row.string("action") ?? "Action",
            isEnabled: row.bool("enabled") ?? false,
            requiresConfirmation: row.bool("requires_confirmation") ?? false,
            disabledReason: row.string("disabled_reason"),
            displayOrder: row.int("display_order") ?? row.int("sort_order") ?? 0
        )
    }

    private static func nativeActionsByRow(_ rows: [[String: Any]]) -> [String: [CockpitRowAction]] {
        var out: [String: [CockpitRowAction]] = [:]
        for row in rows {
            let item = action(row)
            let keys = [
                row.string("row_id"),
                row.string("row_dedup_key"),
                row.string("work_id"),
                row.string("job_id"),
            ]
            .compactMap { $0?.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            for key in Set(keys) {
                out[key, default: []].append(item)
            }
        }
        return out
    }

    private static func compactActions(_ row: [String: Any]) -> [CockpitRowAction] {
        let actions = row.dictionaries("actions").map(action)
        if !actions.isEmpty { return actions }
        guard let raw = row["available_actions"] else { return [] }
        let labels: [String]
        if let array = raw as? [String] {
            labels = array
        } else if let array = raw as? [Any] {
            labels = array.compactMap { ($0 as? String) ?? (($0 as? NSNumber)?.stringValue) }
        } else if let text = raw as? String {
            labels = text.split(separator: ",").map { String($0).trimmingCharacters(in: .whitespacesAndNewlines) }
        } else {
            labels = []
        }
        return labels.enumerated().map { idx, label in
            CockpitRowAction(
                id: label,
                label: label.replacingOccurrences(of: "_", with: " ").capitalized,
                isEnabled: true,
                requiresConfirmation: ["jobs.kill", "kill", "daemon.restart"].contains(label.lowercased()),
                disabledReason: nil,
                displayOrder: idx
            )
        }
    }

    private static func compactFilterOptions(_ rows: [CockpitRow]) -> [CockpitFilterOption] {
        var out: [CockpitFilterOption] = []
        out.append(contentsOf: filterOptions(rows.compactMap { $0.ownerGroup ?? $0.owner }, kind: .owner))
        out.append(contentsOf: filterOptions(rows.compactMap { $0.module ?? $0.moduleLabel }, kind: .module))
        out.append(contentsOf: filterOptions(rows.map(\.status), kind: .status))
        return out
    }

    private static func filterOptions(_ values: [String], kind: CockpitFilterKind) -> [CockpitFilterOption] {
        let counts = Dictionary(grouping: values.filter { !$0.isEmpty }, by: { $0.lowercased() })
            .mapValues(\.count)
        return counts.keys.sorted().enumerated().map { idx, value in
            CockpitFilterOption(kind: kind, value: value, label: value.capitalized, count: counts[value] ?? 0, displayOrder: idx)
        }
    }

    private static func compactDiagnostics(_ row: [String: Any]) -> [CockpitDiagnostic] {
        row.keys.sorted().enumerated().map { idx, key in
            CockpitDiagnostic(
                id: key,
                category: "legacy_compact",
                label: key.replacingOccurrences(of: "_", with: " "),
                status: String(describing: row[key] ?? ""),
                detail: nil,
                displayOrder: idx
            )
        }
    }

    private static func column(_ row: [String: Any]) -> CockpitColumn {
        let id = row.string("id") ?? row.string("column_id") ?? row.string("column_key") ?? ""
        let fallback = CockpitColumn.webDefaults.first { $0.id == id }
        let compactIconColumn = ["state", "owner", "control"].contains(id)
        return CockpitColumn(
            id: id,
            label: compactIconColumn ? (fallback?.label ?? "") : (row.string("label") ?? fallback?.label ?? ""),
            displayOrder: fallback?.displayOrder ?? row.int("display_order") ?? row.int("sort_order") ?? 0,
            width: fallback?.width ?? row.int("width") ?? 100,
            minWidth: fallback?.minWidth ?? row.int("min_width") ?? 40,
            isVisible: fallback?.isVisible ?? row.bool("visible") ?? row.bool("is_visible") ?? row.bool("default_visible") ?? true,
            alignment: row.string("alignment") ?? fallback?.alignment
        )
    }

    private static func group(_ row: [String: Any]) -> CockpitGroup {
        CockpitGroup(
            key: row.string("key") ?? row.string("group_key") ?? "",
            label: row.string("label") ?? row.string("key") ?? row.string("group_key") ?? "",
            displayOrder: row.int("display_order") ?? 0,
            count: row.int("count") ?? 0,
            isCollapsed: row.bool("collapsed") ?? row.bool("is_collapsed") ?? false
        )
    }

    private static func filterOption(_ row: [String: Any]) -> CockpitFilterOption {
        CockpitFilterOption(
            kind: CockpitFilterKind(raw: row.string("kind") ?? row.string("filter_key")),
            value: row.string("value") ?? "",
            label: row.string("label") ?? row.string("value") ?? "",
            count: row.int("count") ?? 0,
            displayOrder: row.int("display_order") ?? row.int("sort_order") ?? 0
        )
    }

    private static func session(_ row: [String: Any]) -> CockpitSession {
        CockpitSession(
            id: row.string("id") ?? row.string("session_id") ?? "",
            actor: row.string("actor") ?? "unknown",
            label: row.string("label") ?? row.string("display") ?? row.string("id") ?? row.string("session_id") ?? "Session",
            status: row.string("status") ?? "unknown",
            heartbeatAgeSeconds: row.double("heartbeat_age_s"),
            isStale: row.bool("is_stale") ?? false
        )
    }

    private static func diagnostic(_ row: [String: Any]) -> CockpitDiagnostic {
        CockpitDiagnostic(
            id: row.string("id") ?? row.string("diagnostic_id") ?? row.string("diagnostic_key") ?? "",
            category: row.string("category") ?? row.string("severity") ?? "unknown",
            label: row.string("label") ?? row.string("id") ?? row.string("diagnostic_id") ?? row.string("diagnostic_key") ?? "Diagnostic",
            status: row.string("status") ?? row.string("value_text") ?? row.string("severity") ?? "unknown",
            detail: row.string("detail") ?? row.string("value_text"),
            displayOrder: row.int("display_order") ?? row.int("sort_order") ?? 0
        )
    }
}

private extension Dictionary where Key == String, Value == Any {
    func dictionary(_ key: String) -> [String: Any] {
        self[key] as? [String: Any] ?? [:]
    }

    func dictionaries(_ key: String) -> [[String: Any]] {
        self[key] as? [[String: Any]] ?? []
    }

    func string(_ key: String) -> String? {
        if let value = self[key] as? String { return value }
        if let number = self[key] as? NSNumber { return number.stringValue }
        return nil
    }

    func int(_ key: String) -> Int? {
        if let value = self[key] as? Int { return value }
        if let value = self[key] as? Double { return Int(value) }
        if let value = self[key] as? String { return Int(value) }
        if let number = self[key] as? NSNumber { return number.intValue }
        return nil
    }

    func int64(_ key: String) -> Int64? {
        if let value = self[key] as? Int64 { return value }
        if let value = self[key] as? Int { return Int64(value) }
        if let value = self[key] as? Double { return Int64(value) }
        if let value = self[key] as? String { return Int64(value) }
        if let number = self[key] as? NSNumber { return number.int64Value }
        return nil
    }

    func double(_ key: String) -> Double? {
        if let value = self[key] as? Double { return value }
        if let value = self[key] as? Int { return Double(value) }
        if let value = self[key] as? String { return Double(value) }
        if let number = self[key] as? NSNumber { return number.doubleValue }
        return nil
    }

    func bool(_ key: String) -> Bool? {
        if let value = self[key] as? Bool { return value }
        if let value = self[key] as? Int { return value != 0 }
        if let value = self[key] as? String {
            let normalized = value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            if ["1", "true", "yes"].contains(normalized) { return true }
            if ["0", "false", "no"].contains(normalized) { return false }
        }
        if let number = self[key] as? NSNumber { return number.boolValue }
        return nil
    }
}
