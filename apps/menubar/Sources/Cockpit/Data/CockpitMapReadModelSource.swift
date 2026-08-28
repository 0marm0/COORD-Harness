import Foundation

final class CockpitMapReadModelSource: @unchecked Sendable {
    static let defaultPath = resolvedDefaultPath()

    private let databasePath: String
    private let productLimit: Int
    private let modelLimit: Int
    private let integrityLimit: Int
    private let nodeLimit: Int
    private let edgeLimit: Int
    private let intelligenceLimit: Int
    private let funnelLimit: Int
    private let moatLimit: Int
    private let runwayLimit: Int
    private let historyLimit: Int
    private let changeLimit: Int
    private let annotationLimit: Int
    private let alertLimit: Int

    init(
        databasePath: String = CockpitMapReadModelSource.defaultPath,
        productLimit: Int = 500,
        modelLimit: Int = 1_000,
        integrityLimit: Int = 200,
        nodeLimit: Int = 2_000,
        edgeLimit: Int = 4_000,
        intelligenceLimit: Int = 500,
        funnelLimit: Int = 50,
        moatLimit: Int = 500,
        runwayLimit: Int = 500,
        historyLimit: Int = 5_000,
        changeLimit: Int = 1_000,
        annotationLimit: Int = 500,
        alertLimit: Int = 500
    ) {
        self.databasePath = databasePath
        self.productLimit = productLimit
        self.modelLimit = modelLimit
        self.integrityLimit = integrityLimit
        self.nodeLimit = nodeLimit
        self.edgeLimit = edgeLimit
        self.intelligenceLimit = intelligenceLimit
        self.funnelLimit = funnelLimit
        self.moatLimit = moatLimit
        self.runwayLimit = runwayLimit
        self.historyLimit = historyLimit
        self.changeLimit = changeLimit
        self.annotationLimit = annotationLimit
        self.alertLimit = alertLimit
    }

    func load() -> CockpitMapState {
        do {
            let db = try CoordSQLite.openReadOnly(path: databasePath)
            defer { db.close() }
            try validateSchema(db)
            try db.execute("BEGIN DEFERRED TRANSACTION")
            defer { try? db.execute("ROLLBACK") }
            let meta = try loadMeta(db)
            return CockpitMapState(
                generatedAt: meta["generated_at"],
                trustScore: meta["trust_score"].flatMap(Int.init),
                meta: meta,
                products: try loadProducts(db),
                models: try loadModels(db),
                integrityWarnings: try loadIntegrityWarnings(db),
                systemNodes: try loadSystemNodes(db),
                systemEdges: try loadSystemEdges(db),
                intelligence: try loadIntelligence(db),
                funnel: try loadFunnel(db),
                moatAssets: try loadMoatAssets(db),
                runwayItems: try loadRunwayItems(db),
                history: try loadHistory(db),
                changes: try loadChanges(db),
                annotations: try loadAnnotations(db),
                alerts: try loadAlerts(db),
                error: nil
            )
        } catch let error as CockpitLoadErrorState {
            return CockpitMapState.error(error)
        } catch let error as CoordSQLiteError {
            return CockpitMapState.error(CockpitLoadErrorState(kind: .sqlite, message: error.description))
        } catch {
            return CockpitMapState.error(CockpitLoadErrorState(kind: .sqlite, message: String(describing: error)))
        }
    }

    private static func resolvedDefaultPath() -> String {
        let fm = FileManager.default
        let env = ProcessInfo.processInfo.environment["COORD_MAP_DB"]?.trimmingCharacters(in: .whitespacesAndNewlines)
        let home = NSHomeDirectory()
        let candidates = [
            env,
            fm.currentDirectoryPath + "/.coordharness/graph.db",
            home + "/.coordharness/graph.db",
        ].compactMap { $0 }.filter { !$0.isEmpty }
        return candidates.first(where: { fm.fileExists(atPath: $0) })
            ?? candidates.first
            ?? home + "/.coordharness/graph.db"
    }

    private func validateSchema(_ db: CoordSQLite) throws {
        let required = [
            "map_products",
            "map_models",
            "map_integrity",
            "map_system_nodes",
            "map_system_edges",
            "map_intelligence",
            "map_funnel",
            "map_moat",
            "map_runway",
            "map_history",
            "map_change_log",
            "map_annotations",
            "map_alerts",
            "map_meta",
        ]
        for table in required {
            if try !db.tableExists(table) {
                throw CockpitLoadErrorState(kind: .missingSchema, message: "missing map schema table \(table)")
            }
        }
    }

    private func loadMeta(_ db: CoordSQLite) throws -> [String: String] {
        let rows = try db.rows("SELECT key, value FROM map_meta ORDER BY key ASC")
        var meta: [String: String] = [:]
        for row in rows {
            guard let key = row.string("key") else { continue }
            meta[key] = row.string("value") ?? ""
        }
        return meta
    }

    private func loadProducts(_ db: CoordSQLite) throws -> [CockpitMapProduct] {
        try db.rows(
            """
            SELECT vertical, display_name, blurb, stage, sellable, health,
                   headline_metric, metric_value, metric_honest_note, status,
                   buyer_readiness, surface_url, n_open, n_running, n_dark, updated_at
              FROM map_products
             ORDER BY rowid ASC
             LIMIT ?
            """,
            bindings: [.integer(Int64(productLimit))]
        ).map { row in
            CockpitMapProduct(
                vertical: row.string("vertical") ?? "",
                displayName: row.string("display_name") ?? row.string("vertical") ?? "",
                blurb: row.string("blurb"),
                stage: row.string("stage") ?? "",
                isSellable: row.bool("sellable") ?? false,
                health: row.string("health") ?? "",
                headlineMetric: row.string("headline_metric"),
                metricValue: row.string("metric_value"),
                metricHonestNote: row.string("metric_honest_note"),
                status: row.string("status"),
                buyerReadiness: row.string("buyer_readiness"),
                surfaceURL: row.string("surface_url"),
                openCount: row.int("n_open") ?? 0,
                runningCount: row.int("n_running") ?? 0,
                darkCount: row.int("n_dark") ?? 0,
                updatedAt: row.string("updated_at") ?? ""
            )
        }
    }

    private func loadModels(_ db: CoordSQLite) throws -> [CockpitMapModel] {
        try db.rows(
            """
            SELECT name, vertical, status, metric, metric_value, powers, value_score, effort, ledger_ref
              FROM map_models
             ORDER BY rowid ASC
             LIMIT ?
            """,
            bindings: [.integer(Int64(modelLimit))]
        ).map { row in
            CockpitMapModel(
                name: row.string("name") ?? "",
                vertical: row.string("vertical"),
                status: row.string("status") ?? "",
                metric: row.string("metric"),
                metricValue: row.string("metric_value"),
                powers: row.string("powers"),
                valueScore: row.int("value_score"),
                effort: row.string("effort"),
                ledgerRef: row.string("ledger_ref")
            )
        }
    }

    private func loadIntegrityWarnings(_ db: CoordSQLite) throws -> [CockpitMapIntegrityWarning] {
        try db.rows(
            """
            SELECT surface, served_value, honest_value, severity, note
              FROM map_integrity
             ORDER BY CASE LOWER(COALESCE(severity, ''))
                        WHEN 'critical' THEN 0
                        WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2
                        ELSE 3
                      END,
                      rowid ASC
             LIMIT ?
            """,
            bindings: [.integer(Int64(integrityLimit))]
        ).map { row in
            CockpitMapIntegrityWarning(
                surface: row.string("surface") ?? "",
                servedValue: row.string("served_value"),
                honestValue: row.string("honest_value"),
                severity: row.string("severity"),
                note: row.string("note")
            )
        }
    }

    private func loadSystemNodes(_ db: CoordSQLite) throws -> [CockpitMapSystemNode] {
        try db.rows(
            """
            SELECT id, layer, name, status, size_note
              FROM map_system_nodes
             ORDER BY rowid ASC
             LIMIT ?
            """,
            bindings: [.integer(Int64(nodeLimit))]
        ).map { row in
            CockpitMapSystemNode(
                id: row.string("id") ?? "",
                layer: row.string("layer") ?? "",
                name: row.string("name") ?? "",
                status: row.string("status") ?? "",
                sizeNote: row.string("size_note")
            )
        }
    }

    private func loadSystemEdges(_ db: CoordSQLite) throws -> [CockpitMapSystemEdge] {
        try db.rows(
            """
            SELECT from_id, to_id, status
              FROM map_system_edges
             ORDER BY rowid ASC
             LIMIT ?
            """,
            bindings: [.integer(Int64(edgeLimit))]
        ).map { row in
            CockpitMapSystemEdge(
                fromID: row.string("from_id") ?? "",
                toID: row.string("to_id") ?? "",
                status: row.string("status") ?? ""
            )
        }
    }

    private func loadIntelligence(_ db: CoordSQLite) throws -> [CockpitMapIntelligenceRow] {
        try db.rows(
            """
            SELECT kind, rank, vertical, title, detail, score, effort, impact, cta_kind, cta_target
              FROM map_intelligence
             ORDER BY kind ASC, rank ASC, rowid ASC
             LIMIT ?
            """,
            bindings: [.integer(Int64(intelligenceLimit))]
        ).map { row in
            CockpitMapIntelligenceRow(
                kind: row.string("kind") ?? "",
                rank: row.int("rank") ?? 0,
                vertical: row.string("vertical"),
                title: row.string("title") ?? "",
                detail: row.string("detail"),
                score: row.double("score"),
                effort: row.string("effort"),
                impact: row.int("impact"),
                ctaKind: row.string("cta_kind"),
                ctaTarget: row.string("cta_target")
            )
        }
    }

    private func loadFunnel(_ db: CoordSQLite) throws -> [CockpitMapFunnelStage] {
        try db.rows(
            """
            SELECT stage, n_products, n_sellable, verticals, note
              FROM map_funnel
             ORDER BY CASE stage
                        WHEN 'idea' THEN 0
                        WHEN 'built' THEN 1
                        WHEN 'wired' THEN 2
                        WHEN 'live' THEN 3
                        WHEN 'sold' THEN 4
                        ELSE 5
                      END,
                      rowid ASC
             LIMIT ?
            """,
            bindings: [.integer(Int64(funnelLimit))]
        ).map { row in
            CockpitMapFunnelStage(
                stage: row.string("stage") ?? "",
                productCount: row.int("n_products") ?? 0,
                sellableCount: row.int("n_sellable") ?? 0,
                verticals: row.string("verticals"),
                note: row.string("note")
            )
        }
    }

    private func loadMoatAssets(_ db: CoordSQLite) throws -> [CockpitMapMoatAsset] {
        try db.rows(
            """
            SELECT asset, vertical, moat_type, strength, value_score, note
              FROM map_moat
             ORDER BY COALESCE(value_score, 0) DESC, COALESCE(strength, 0) DESC, rowid ASC
             LIMIT ?
            """,
            bindings: [.integer(Int64(moatLimit))]
        ).map { row in
            CockpitMapMoatAsset(
                asset: row.string("asset") ?? "",
                vertical: row.string("vertical"),
                moatType: row.string("moat_type"),
                strength: row.int("strength"),
                valueScore: row.int("value_score"),
                note: row.string("note")
            )
        }
    }

    private func loadRunwayItems(_ db: CoordSQLite) throws -> [CockpitMapRunwayItem] {
        try db.rows(
            """
            SELECT category, item, size_note, status, note
              FROM map_runway
             ORDER BY CASE category
                        WHEN 'storage' THEN 0
                        WHEN 'models' THEN 1
                        WHEN 'compute' THEN 2
                        ELSE 3
                      END,
                      rowid ASC
             LIMIT ?
            """,
            bindings: [.integer(Int64(runwayLimit))]
        ).map { row in
            CockpitMapRunwayItem(
                category: row.string("category") ?? "",
                item: row.string("item") ?? "",
                sizeNote: row.string("size_note"),
                status: row.string("status"),
                note: row.string("note")
            )
        }
    }

    private func loadHistory(_ db: CoordSQLite) throws -> [CockpitMapHistoryRow] {
        try db.rows(
            """
            SELECT ts, entity_kind, entity, stage, health, metric, metric_value, value_score
              FROM map_history
             ORDER BY ts DESC, rowid ASC
             LIMIT ?
            """,
            bindings: [.integer(Int64(historyLimit))]
        ).map { row in
            CockpitMapHistoryRow(
                timestamp: row.string("ts") ?? "",
                entityKind: row.string("entity_kind") ?? "",
                entity: row.string("entity") ?? "",
                stage: row.string("stage"),
                health: row.string("health"),
                metric: row.string("metric"),
                metricValue: row.string("metric_value"),
                valueScore: row.int("value_score")
            )
        }
    }

    private func loadChanges(_ db: CoordSQLite) throws -> [CockpitMapChange] {
        try db.rows(
            """
            SELECT ts, entity_kind, entity, field, old_value, new_value, severity
              FROM map_change_log
             ORDER BY ts DESC, rowid DESC
             LIMIT ?
            """,
            bindings: [.integer(Int64(changeLimit))]
        ).map { row in
            CockpitMapChange(
                timestamp: row.string("ts") ?? "",
                entityKind: row.string("entity_kind") ?? "",
                entity: row.string("entity") ?? "",
                field: row.string("field") ?? "",
                oldValue: row.string("old_value"),
                newValue: row.string("new_value"),
                severity: row.string("severity")
            )
        }
    }

    private func loadAnnotations(_ db: CoordSQLite) throws -> [CockpitMapAnnotation] {
        try db.rows(
            """
            SELECT id, ts, entity_kind, entity, note
              FROM map_annotations
             ORDER BY ts DESC, id DESC
             LIMIT ?
            """,
            bindings: [.integer(Int64(annotationLimit))]
        ).map { row in
            CockpitMapAnnotation(
                id: row.int("id") ?? 0,
                timestamp: row.string("ts") ?? "",
                entityKind: row.string("entity_kind"),
                entity: row.string("entity"),
                note: row.string("note") ?? ""
            )
        }
    }

    private func loadAlerts(_ db: CoordSQLite) throws -> [CockpitMapAlert] {
        try db.rows(
            """
            SELECT level, kind, title, detail, cta_kind, cta_target
              FROM map_alerts
             ORDER BY CASE LOWER(level)
                        WHEN 'critical' THEN 0
                        WHEN 'alert' THEN 1
                        WHEN 'warn' THEN 2
                        WHEN 'warning' THEN 2
                        ELSE 3
                      END,
                      rowid ASC
             LIMIT ?
            """,
            bindings: [.integer(Int64(alertLimit))]
        ).map { row in
            CockpitMapAlert(
                level: row.string("level") ?? "",
                kind: row.string("kind") ?? "",
                title: row.string("title") ?? "",
                detail: row.string("detail"),
                ctaKind: row.string("cta_kind"),
                ctaTarget: row.string("cta_target")
            )
        }
    }
}
