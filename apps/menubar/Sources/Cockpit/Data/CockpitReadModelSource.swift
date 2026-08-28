import Foundation

protocol CockpitReadModelLoading: AnyObject {
    func load() -> CockpitState
}

final class CockpitReadModelSource: CockpitReadModelLoading {
    static let supportedSchemaVersion = 1

    private let databasePath: String
    private let rowLimit: Int
    private let actionLimit: Int
    private let optionLimit: Int
    private let sessionLimit: Int
    private let diagnosticLimit: Int

    init(
        databasePath: String = CoordSQLite.defaultPath,
        rowLimit: Int = 1_000,
        actionLimit: Int = 4_000,
        optionLimit: Int = 500,
        sessionLimit: Int = 100,
        diagnosticLimit: Int = 100
    ) {
        self.databasePath = databasePath
        self.rowLimit = rowLimit
        self.actionLimit = actionLimit
        self.optionLimit = optionLimit
        self.sessionLimit = sessionLimit
        self.diagnosticLimit = diagnosticLimit
    }

    func load() -> CockpitState {
        do {
            let db = try CoordSQLite.openReadOnly(path: databasePath)
            defer { db.close() }
            try validateSchema(db)
            let meta = try loadMeta(db)
            if meta.schemaVersion > Self.supportedSchemaVersion {
                return CockpitState.error(CockpitLoadErrorState(
                    kind: .unsupportedSchema,
                    message: "native_cockpit schema_version \(meta.schemaVersion) is newer than supported \(Self.supportedSchemaVersion)"
                ))
            }

            let actionsByRow = try loadActions(db, writerSeq: meta.writerSeq)
            let rows = try loadRows(db, writerSeq: meta.writerSeq, actionsByRow: actionsByRow)
            return CockpitState(
                schemaVersion: meta.schemaVersion,
                writerSeq: meta.writerSeq,
                builtAt: meta.builtAt,
                sourceVersion: meta.sourceVersion,
                stale: meta.stale,
                refreshing: meta.refreshing,
                mode: meta.mode,
                liveMode: meta.liveMode,
                summary: try loadSummary(db, writerSeq: meta.writerSeq),
                rows: rows,
                columns: try loadColumns(db, writerSeq: meta.writerSeq),
                groups: try loadGroups(db, writerSeq: meta.writerSeq),
                filterOptions: try loadFilterOptions(db, writerSeq: meta.writerSeq),
                sessions: try loadSessions(db, writerSeq: meta.writerSeq),
                diagnostics: try loadDiagnostics(db, writerSeq: meta.writerSeq),
                error: nil
            )
        } catch let error as CockpitLoadErrorState {
            return CockpitState.error(error)
        } catch let error as CoordSQLiteError {
            return Self.errorState(from: error)
        } catch {
            return Self.errorState(from: error)
        }
    }

    func reloadIfChanged(since state: CockpitState) -> CockpitState? {
        do {
            let db = try CoordSQLite.openReadOnly(path: databasePath)
            defer { db.close() }
            try validateSchema(db)
            let writerSeq = try loadMeta(db).writerSeq
            guard writerSeq != state.writerSeq else { return nil }
            return load()
        } catch let error as CockpitLoadErrorState {
            return CockpitState.error(error)
        } catch let error as CoordSQLiteError {
            return Self.errorState(from: error)
        } catch {
            return Self.errorState(from: error)
        }
    }

    private static func errorState(from error: CoordSQLiteError) -> CockpitState {
        CockpitState.error(CockpitLoadErrorState(kind: .sqlite, message: error.description))
    }

    private static func errorState(from error: Error) -> CockpitState {
        CockpitState.error(CockpitLoadErrorState(kind: .sqlite, message: String(describing: error)))
    }

    private func validateSchema(_ db: CoordSQLite) throws {
        let rowTableExists = try db.tableExists("native_cockpit_rows") || db.tableExists("v_native_cockpit_rows")
        let required: [(name: String, exists: Bool)] = [
            ("native_projection_meta", try db.tableExists("native_projection_meta")),
            ("native_cockpit_summary", try db.tableExists("native_cockpit_summary")),
            ("native_cockpit_row_actions", try db.tableExists("native_cockpit_row_actions")),
            ("native_cockpit_filter_options", try db.tableExists("native_cockpit_filter_options")),
            ("native_cockpit_column_model", try db.tableExists("native_cockpit_column_model")),
            ("native_cockpit_group_model", try db.tableExists("native_cockpit_group_model")),
            ("native_cockpit_sessions", try db.tableExists("native_cockpit_sessions")),
            ("native_cockpit_diagnostics", try db.tableExists("native_cockpit_diagnostics")),
        ]
        if let missing = required.first(where: { !$0.exists })?.name {
            throw CockpitLoadErrorState(kind: .missingSchema, message: "missing native Cockpit schema table \(missing)")
        }
        if !rowTableExists {
            throw CockpitLoadErrorState(kind: .missingSchema, message: "missing native Cockpit schema table native_cockpit_rows or v_native_cockpit_rows")
        }
    }

    private func rowTableName(_ db: CoordSQLite) throws -> String {
        if try db.tableExists("native_cockpit_rows") { return "native_cockpit_rows" }
        return "v_native_cockpit_rows"
    }

    private func loadMeta(_ db: CoordSQLite) throws -> ProjectionMeta {
        let rows = try db.rows(
            """
            SELECT schema_version, writer_seq, built_at, source_version, stale, refreshing, error_code, error_text, mode, live_mode
              FROM native_projection_meta
             WHERE contract IN ('native_cockpit.v1', 'native_cockpit')
             ORDER BY CASE WHEN contract = 'native_cockpit.v1' THEN 0 ELSE 1 END, writer_seq DESC
             LIMIT 1
            """
        )
        guard let row = rows.first else {
            throw CockpitLoadErrorState(kind: .missingProjectionMeta, message: "native_projection_meta has no native_cockpit or native_cockpit.v1 row")
        }
        return ProjectionMeta(
            schemaVersion: row.int("schema_version") ?? 0,
            writerSeq: row.int64("writer_seq") ?? 0,
            builtAt: row.string("built_at"),
            sourceVersion: row.string("source_version"),
            stale: row.bool("stale") ?? false,
            refreshing: row.bool("refreshing") ?? false,
            mode: row.string("mode"),
            liveMode: row.string("live_mode")
        )
    }

    private func loadRows(_ db: CoordSQLite, writerSeq: Int64, actionsByRow: [String: [CockpitRowAction]]) throws -> [CockpitRow] {
        let tableName = try rowTableName(db)
        let table = CoordSQLite.quotedIdentifier(tableName)
        let columns = try db.columnNames(table: tableName)
        let order = columns.contains("bucket_order")
            ? "bucket_order ASC, display_order ASC, dedup_key ASC"
            : "display_order ASC, dedup_key ASC"
        return try db.rows(
            """
            SELECT *
              FROM \(table)
             WHERE writer_seq = ?
             ORDER BY \(order)
             LIMIT ?
            """,
            bindings: [.integer(writerSeq), .integer(Int64(rowLimit))]
        ).map { row in
            let key = row.string("dedup_key") ?? row.string("work_id") ?? row.string("coord_work_id") ?? row.string("roadmap_id") ?? row.string("job_id") ?? row.string("id") ?? ""
            let workID = row.string("work_id") ?? row.string("coord_work_id") ?? row.string("roadmap_id")
            let jobID = row.string("job_id") ?? row.string("queue_job_id")
            let bucket = row.string("bucket") ?? row.string("scope")
            return CockpitRow(
                dedupKey: key,
                workID: workID,
                parentID: row.string("parent") ?? row.string("parent_id"),
                jobID: jobID,
                title: row.string("title") ?? row.string("display") ?? row.string("name") ?? "Task",
                status: row.string("status") ?? row.string("operator_state") ?? row.string("intent_state") ?? "unknown",
                scope: row.string("scope") ?? bucket ?? "",
                owner: row.string("owner"),
                ownerGroup: row.string("owner_group"),
                ownerSessionID: row.string("owner_session_id") ?? row.string("session_id"),
                ownerSessionActor: row.string("owner_session_actor") ?? row.string("session_actor"),
                ownerSessionLabel: row.string("owner_session_label") ?? row.string("session_label"),
                ownerExternalThreadID: row.string("owner_external_thread_id") ?? row.string("external_thread_id"),
                ownerConversationTitle: row.string("owner_conversation_title") ?? row.string("conversation_title"),
                ownerWorktreeID: row.string("owner_worktree_id") ?? row.string("worktree_id"),
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
                whyText: row.string("why_text"),
                noteText: row.string("note_text"),
                priority: row.string("priority"),
                rowKind: row.string("row_kind") ?? row.string("kind"),
                pid: row.int("pid"),
                pgid: row.int("pgid"),
                sidecarAgeSeconds: row.double("sidecar_age_s"),
                doneSignal: row.string("done_signal"),
                acceptanceSummary: row.string("acceptance_summary"),
                contextPackRef: row.string("context_pack_ref"),
                groupKey: row.string("group_key") ?? bucket,
                groupLabel: row.string("group_label") ?? bucket.map(labelize),
                displayOrder: row.int("display_order") ?? 0,
                actions: actionsByRow[key] ?? workID.flatMap { actionsByRow[$0] } ?? jobID.flatMap { actionsByRow[$0] } ?? []
            )
        }
    }

    private func loadSummary(_ db: CoordSQLite, writerSeq: Int64) throws -> CockpitSummary {
        let columns = try db.columnNames(table: "native_cockpit_summary")
        if columns.contains("metric") && columns.contains("value") {
            var summary = CockpitSummary()
            for row in try db.rows(
                "SELECT metric, value FROM native_cockpit_summary WHERE writer_seq = ?",
                bindings: [.integer(writerSeq)]
            ) {
                setSummary(metric: row.string("metric"), value: row.int("value") ?? 0, summary: &summary)
            }
            return summary
        }
        if columns.contains("summary_key") && (columns.contains("value_num") || columns.contains("value_text")) {
            var summary = CockpitSummary()
            for row in try db.rows(
                "SELECT summary_key, value_num, value_text FROM native_cockpit_summary WHERE writer_seq = ?",
                bindings: [.integer(writerSeq)]
            ) {
                setSummary(
                    metric: row.string("summary_key"),
                    value: row.int("value_num") ?? row.int("value_text") ?? 0,
                    summary: &summary
                )
            }
            return summary
        }

        guard let row = try db.rows(
            "SELECT * FROM native_cockpit_summary WHERE writer_seq = ? LIMIT 1",
            bindings: [.integer(writerSeq)]
        ).first else { return CockpitSummary() }
        return CockpitSummary(
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

    private func setSummary(metric: String?, value: Int, summary: inout CockpitSummary) {
        switch metric?.lowercased() {
        case "running": summary.running = value
        case "attention": summary.attention = value
        case "next": summary.next = value
        case "local": summary.local = value
        case "done_today", "done-today": summary.doneToday = value
        case "stale": summary.stale = value
        case "blocked": summary.blocked = value
        case "launchable": summary.launchable = value
        default: break
        }
    }

    private func loadActions(_ db: CoordSQLite, writerSeq: Int64) throws -> [String: [CockpitRowAction]] {
        let columns = try db.columnNames(table: "native_cockpit_row_actions")
        let order = columns.contains("display_order")
            ? "row_id ASC, display_order ASC, action_id ASC"
            : "row_dedup_key ASC, sort_order ASC, action ASC"
        let rows = try db.rows(
            """
            SELECT *
              FROM native_cockpit_row_actions
             WHERE writer_seq = ?
             ORDER BY \(order)
             LIMIT ?
            """,
            bindings: [.integer(writerSeq), .integer(Int64(actionLimit))]
        )
        var grouped: [String: [CockpitRowAction]] = [:]
        for row in rows {
            let actionID = row.string("action_id") ?? row.string("action") ?? ""
            let action = CockpitRowAction(
                id: actionID,
                label: row.string("label") ?? actionID,
                isEnabled: row.bool("enabled") ?? false,
                requiresConfirmation: row.bool("requires_confirmation") ?? false,
                disabledReason: row.string("disabled_reason"),
                displayOrder: row.int("display_order") ?? row.int("sort_order") ?? 0
            )
            let ids = [
                row.string("row_id"),
                row.string("row_dedup_key"),
                row.string("work_id"),
                row.string("job_id"),
            ]
            .compactMap { $0?.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            for id in Set(ids) {
                grouped[id, default: []].append(action)
            }
        }
        return grouped
    }

    private func loadFilterOptions(_ db: CoordSQLite, writerSeq: Int64) throws -> [CockpitFilterOption] {
        let columns = try db.columnNames(table: "native_cockpit_filter_options")
        let order = columns.contains("display_order")
            ? "kind ASC, display_order ASC, label ASC"
            : "filter_key ASC, sort_order ASC, label ASC"
        return try db.rows(
            """
            SELECT *
              FROM native_cockpit_filter_options
             WHERE writer_seq = ?
             ORDER BY \(order)
             LIMIT ?
            """,
            bindings: [.integer(writerSeq), .integer(Int64(optionLimit))]
        ).map { row in
            CockpitFilterOption(
                kind: CockpitFilterKind(raw: row.string("kind") ?? row.string("filter_key")),
                value: row.string("value") ?? "",
                label: row.string("label") ?? row.string("value") ?? "",
                count: row.int("count") ?? 0,
                displayOrder: row.int("display_order") ?? row.int("sort_order") ?? 0
            )
        }
    }

    private func loadColumns(_ db: CoordSQLite, writerSeq: Int64) throws -> [CockpitColumn] {
        let tableColumns = try db.columnNames(table: "native_cockpit_column_model")
        let order = tableColumns.contains("display_order")
            ? "display_order ASC, column_id ASC"
            : "sort_order ASC, column_key ASC"
        let materialized = try db.rows(
            """
            SELECT *
              FROM native_cockpit_column_model
             WHERE writer_seq = ?
             ORDER BY \(order)
            """,
            bindings: [.integer(writerSeq)]
        ).map { row in
            let id = row.string("column_id") ?? row.string("column_key") ?? ""
            let fallback = defaultColumn(id: id)
            let compactIconColumn = ["state", "owner", "control"].contains(id)
            let explicitVisible = row.bool("visible")
            let nativeDefaultVisible = fallback?.isVisible
            let serverDefaultVisible = row.bool("default_visible")
            return CockpitColumn(
                id: id,
                label: compactIconColumn ? (fallback?.label ?? "") : (row.string("label") ?? fallback?.label ?? ""),
                displayOrder: fallback?.displayOrder ?? row.int("display_order") ?? row.int("sort_order") ?? 0,
                width: fallback?.width ?? row.int("width") ?? max(row.int("min_width") ?? 40, 100),
                minWidth: fallback?.minWidth ?? row.int("min_width") ?? 40,
                isVisible: explicitVisible ?? nativeDefaultVisible ?? serverDefaultVisible ?? true,
                alignment: row.string("alignment") ?? fallback?.alignment
            )
        }.filter { !$0.id.isEmpty }
        return mergeNativeDefaultColumns(with: materialized)
    }

    private func mergeNativeDefaultColumns(with materialized: [CockpitColumn]) -> [CockpitColumn] {
        let nativeIDs = Set(CockpitColumn.webDefaults.map(\.id))
        var out = CockpitColumn.webDefaults
        for column in materialized.sorted(by: { $0.displayOrder < $1.displayOrder }) where !nativeIDs.contains(column.id) {
            var extra = column
            extra.isVisible = false
            extra.minWidth = max(40, extra.minWidth)
            extra.width = min(1_800, max(extra.minWidth, extra.width))
            extra.displayOrder = ((out.map(\.displayOrder).max() ?? 0) / 10 + 1) * 10
            out.append(extra)
        }
        return out
    }

    private func loadGroups(_ db: CoordSQLite, writerSeq: Int64) throws -> [CockpitGroup] {
        let columns = try db.columnNames(table: "native_cockpit_group_model")
        let order = columns.contains("display_order")
            ? "display_order ASC, group_key ASC"
            : "sort_order ASC, group_key ASC"
        return try db.rows(
            """
            SELECT *
              FROM native_cockpit_group_model
             WHERE writer_seq = ?
             ORDER BY \(order)
            """,
            bindings: [.integer(writerSeq)]
        ).map { row in
            CockpitGroup(
                key: row.string("group_key") ?? "",
                label: row.string("label") ?? row.string("group_key") ?? "",
                displayOrder: row.int("display_order") ?? row.int("sort_order") ?? 0,
                count: row.int("count") ?? 0,
                isCollapsed: row.bool("collapsed") ?? false
            )
        }
    }

    private func loadSessions(_ db: CoordSQLite, writerSeq: Int64) throws -> [CockpitSession] {
        let columns = try db.columnNames(table: "native_cockpit_sessions")
        let order = columns.contains("sort_order")
            ? "sort_order ASC, actor ASC, session_id ASC"
            : "actor ASC, session_id ASC"
        return try db.rows(
            """
            SELECT *
              FROM native_cockpit_sessions
             WHERE writer_seq = ?
             ORDER BY \(order)
             LIMIT ?
            """,
            bindings: [.integer(writerSeq), .integer(Int64(sessionLimit))]
        ).map { row in
            CockpitSession(
                id: row.string("session_id") ?? "",
                actor: row.string("actor") ?? "unknown",
                label: row.string("label") ?? row.string("display") ?? row.string("session_id") ?? "Session",
                status: row.string("status") ?? "unknown",
                heartbeatAgeSeconds: row.double("heartbeat_age_s"),
                isStale: row.bool("is_stale") ?? row.bool("heartbeat_fresh").map { !$0 } ?? false
            )
        }
    }

    private func loadDiagnostics(_ db: CoordSQLite, writerSeq: Int64) throws -> [CockpitDiagnostic] {
        let columns = try db.columnNames(table: "native_cockpit_diagnostics")
        let order = columns.contains("display_order")
            ? "display_order ASC, diagnostic_id ASC"
            : "sort_order ASC, diagnostic_key ASC"
        return try db.rows(
            """
            SELECT *
              FROM native_cockpit_diagnostics
             WHERE writer_seq = ?
             ORDER BY \(order)
             LIMIT ?
            """,
            bindings: [.integer(writerSeq), .integer(Int64(diagnosticLimit))]
        ).map { row in
            let id = row.string("diagnostic_id") ?? row.string("diagnostic_key") ?? ""
            return CockpitDiagnostic(
                id: id,
                category: row.string("category") ?? "projection",
                label: row.string("label") ?? id,
                status: row.string("status") ?? row.string("severity") ?? "unknown",
                detail: row.string("detail"),
                displayOrder: row.int("display_order") ?? row.int("sort_order") ?? 0
            )
        }
    }

    private func defaultColumn(id: String) -> CockpitColumn? {
        CockpitColumn.webDefaults.first { $0.id == id }
    }

    private func labelize(_ raw: String) -> String {
        raw.replacingOccurrences(of: "_", with: " ")
            .split(separator: " ")
            .map { $0.prefix(1).uppercased() + $0.dropFirst() }
            .joined(separator: " ")
    }
}

private struct ProjectionMeta {
    var schemaVersion: Int
    var writerSeq: Int64
    var builtAt: String?
    var sourceVersion: String?
    var stale: Bool
    var refreshing: Bool
    var mode: String?
    var liveMode: String?
}
