import Foundation

struct CockpitMapState: Equatable {
    var generatedAt: String?
    var trustScore: Int?
    var meta: [String: String]
    var products: [CockpitMapProduct]
    var models: [CockpitMapModel]
    var integrityWarnings: [CockpitMapIntegrityWarning]
    var systemNodes: [CockpitMapSystemNode]
    var systemEdges: [CockpitMapSystemEdge]
    var intelligence: [CockpitMapIntelligenceRow]
    var funnel: [CockpitMapFunnelStage]
    var moatAssets: [CockpitMapMoatAsset]
    var runwayItems: [CockpitMapRunwayItem]
    var history: [CockpitMapHistoryRow]
    var changes: [CockpitMapChange]
    var annotations: [CockpitMapAnnotation]
    var alerts: [CockpitMapAlert]
    var error: CockpitLoadErrorState?

    static func error(_ error: CockpitLoadErrorState) -> CockpitMapState {
        CockpitMapState(
            generatedAt: nil,
            trustScore: nil,
            meta: [:],
            products: [],
            models: [],
            integrityWarnings: [],
            systemNodes: [],
            systemEdges: [],
            intelligence: [],
            funnel: [],
            moatAssets: [],
            runwayItems: [],
            history: [],
            changes: [],
            annotations: [],
            alerts: [],
            error: error
        )
    }

    func product(vertical: String) -> CockpitMapProduct? {
        products.first { $0.vertical == vertical }
    }

    var pipelineBuckets: [CockpitMapPipelineBucket] {
        let stages = ["idea", "built", "wired", "live", "sold"]
        return stages.map { stage in
            let bucketProducts = products.filter { product in
                if stage == "sold" { return product.isSellable }
                return !product.isSellable && product.stage == stage
            }
            return CockpitMapPipelineBucket(stage: stage, products: bucketProducts)
        }
    }

    var systemLanes: [CockpitMapSystemLane] {
        let layers = ["source", "pipeline", "model", "surface"]
        return layers.map { layer in
            CockpitMapSystemLane(layer: layer, nodes: systemNodes.filter { $0.layer == layer })
        }
    }

    var nextDollarRows: [CockpitMapIntelligenceRow] {
        intelligenceRows(kind: "next_dollar")
    }

    var actionRows: [CockpitMapIntelligenceRow] {
        intelligenceRows(kind: "action")
    }

    var wireRows: [CockpitMapIntelligenceRow] {
        intelligenceRows(kind: "wire")
    }

    var trustHistory: [CockpitMapHistoryRow] {
        history
            .filter { $0.entityKind == "meta" && $0.entity == "trust_score" }
            .sorted { $0.timestamp < $1.timestamp }
    }

    var snapshotTimestamps: [String] {
        Array(Set(history.map(\.timestamp))).sorted()
    }

    var criticalAlertCount: Int {
        if let metaCount = meta["n_critical"].flatMap(Int.init) {
            return metaCount
        }
        return alerts.filter { $0.level.lowercased() == "critical" }.count
    }

    var summary: CockpitMapSummary {
        CockpitMapSummary(products: products, models: models, integrityWarnings: integrityWarnings, trustScore: trustScore)
    }

    func intelligenceRows(kind: String) -> [CockpitMapIntelligenceRow] {
        intelligence
            .filter { $0.kind == kind }
            .sorted { lhs, rhs in
                if lhs.rank != rhs.rank { return lhs.rank < rhs.rank }
                return lhs.title.localizedCaseInsensitiveCompare(rhs.title) == .orderedAscending
            }
    }

    func productsSnapshot(timestamp: String) -> [CockpitMapProduct] {
        let historyByProduct = Dictionary(
            uniqueKeysWithValues: history
                .filter { $0.timestamp == timestamp && $0.entityKind == "product" }
                .map { ($0.entity, $0) }
        )
        return products.map { product in
            guard let historical = historyByProduct[product.vertical] else { return product }
            var copy = product
            if let stage = historical.stage { copy.stage = stage }
            if let health = historical.health { copy.health = health }
            if let metric = historical.metric { copy.headlineMetric = metric }
            if let value = historical.metricValue { copy.metricValue = value }
            return copy
        }
    }
}

struct CockpitMapProduct: Equatable, Identifiable {
    var id: String { vertical }
    var vertical: String
    var displayName: String
    var blurb: String?
    var stage: String
    var isSellable: Bool
    var health: String
    var headlineMetric: String?
    var metricValue: String?
    var metricHonestNote: String?
    var status: String?
    var buyerReadiness: String?
    var surfaceURL: String?
    var openCount: Int
    var runningCount: Int
    var darkCount: Int
    var updatedAt: String
}

struct CockpitMapModel: Equatable, Identifiable {
    var id: String { name }
    var name: String
    var vertical: String?
    var status: String
    var metric: String?
    var metricValue: String?
    var powers: String?
    var valueScore: Int?
    var effort: String?
    var ledgerRef: String?
}

struct CockpitMapIntegrityWarning: Equatable, Identifiable {
    var id: String { surface }
    var surface: String
    var servedValue: String?
    var honestValue: String?
    var severity: String?
    var note: String?
}

struct CockpitMapSystemNode: Equatable, Identifiable {
    var id: String
    var layer: String
    var name: String
    var status: String
    var sizeNote: String?
}

struct CockpitMapSystemEdge: Equatable {
    var fromID: String
    var toID: String
    var status: String
}

struct CockpitMapIntelligenceRow: Equatable, Identifiable {
    var id: String { "\(kind)-\(rank)-\(title)" }
    var kind: String
    var rank: Int
    var vertical: String?
    var title: String
    var detail: String?
    var score: Double?
    var effort: String?
    var impact: Int?
    var ctaKind: String?
    var ctaTarget: String?
}

struct CockpitMapFunnelStage: Equatable, Identifiable {
    var id: String { stage }
    var stage: String
    var productCount: Int
    var sellableCount: Int
    var verticals: String?
    var note: String?
}

struct CockpitMapMoatAsset: Equatable, Identifiable {
    var id: String { asset }
    var asset: String
    var vertical: String?
    var moatType: String?
    var strength: Int?
    var valueScore: Int?
    var note: String?

    var isAtRisk: Bool {
        let normalized = (note ?? "").uppercased()
        return normalized.contains("DARK") || normalized.contains("PARKED") || normalized.contains("LLM-ONLY")
    }
}

struct CockpitMapRunwayItem: Equatable, Identifiable {
    var id: String { "\(category)-\(item)" }
    var category: String
    var item: String
    var sizeNote: String?
    var status: String?
    var note: String?
}

struct CockpitMapHistoryRow: Equatable, Identifiable {
    var id: String { "\(timestamp)-\(entityKind)-\(entity)-\(metric ?? "")" }
    var timestamp: String
    var entityKind: String
    var entity: String
    var stage: String?
    var health: String?
    var metric: String?
    var metricValue: String?
    var valueScore: Int?
}

struct CockpitMapChange: Equatable, Identifiable {
    var id: String { "\(timestamp)-\(entityKind)-\(entity)-\(field)" }
    var timestamp: String
    var entityKind: String
    var entity: String
    var field: String
    var oldValue: String?
    var newValue: String?
    var severity: String?
}

struct CockpitMapAnnotation: Equatable, Identifiable {
    var id: Int
    var timestamp: String
    var entityKind: String?
    var entity: String?
    var note: String
}

struct CockpitMapAlert: Equatable, Identifiable {
    var id: String { "\(level)-\(kind)-\(title)" }
    var level: String
    var kind: String
    var title: String
    var detail: String?
    var ctaKind: String?
    var ctaTarget: String?
}

struct CockpitMapKnowledgeGraph: Codable, Equatable {
    var nodes: [CockpitMapKnowledgeNode]
    var edges: [CockpitMapKnowledgeEdge]
    var counts: [String: Int]?
}

struct CockpitMapKnowledgeNode: Codable, Equatable, Identifiable {
    var id: String
    var type: String
    var label: String
    var status: String?
    var vertical: String?
    var stage: String?
    var sellable: Bool?

    init(id: String, type: String, label: String, status: String? = nil, vertical: String? = nil, stage: String? = nil, sellable: Bool? = nil) {
        self.id = id
        self.type = type
        self.label = label
        self.status = status
        self.vertical = vertical
        self.stage = stage
        self.sellable = sellable
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        type = try c.decode(String.self, forKey: .type)
        label = try c.decode(String.self, forKey: .label)
        status = try c.decodeIfPresent(String.self, forKey: .status)
        vertical = try c.decodeIfPresent(String.self, forKey: .vertical)
        stage = try c.decodeIfPresent(String.self, forKey: .stage)
        if let value = try? c.decodeIfPresent(Bool.self, forKey: .sellable) {
            sellable = value
        } else if let value = try? c.decodeIfPresent(Int.self, forKey: .sellable) {
            sellable = value != 0
        } else {
            sellable = nil
        }
    }
}

struct CockpitMapKnowledgeEdge: Codable, Equatable {
    var source: String
    var target: String
    var kind: String
}

struct CockpitMachineHealth: Codable, Equatable {
    var light: String
    var mode: String?
    var freeRAMGB: Double?
    var floorGB: Double?
    var swapUsedGB: Double?
    var ramHeadroomGB: Double?
    var load1m: Double?
    var gpuEmbedRunning: Bool?
    var reasons: [String]?

    enum CodingKeys: String, CodingKey {
        case light
        case mode
        case freeRAMGB = "free_ram_gb"
        case floorGB = "floor_gb"
        case swapUsedGB = "swap_used_gb"
        case ramHeadroomGB = "ram_headroom_gb"
        case load1m = "load_1m"
        case gpuEmbedRunning = "gpu_embed_running"
        case reasons
    }
}

struct CockpitMapProvenance: Codable, Equatable {
    var vertical: String
    var facts: [CockpitMapProvenanceFact]
    var models: [CockpitMapProvenanceModel]?
    var commits: [CockpitMapProvenanceCommit]?
}

struct CockpitMapProvenanceFact: Codable, Equatable, Identifiable {
    var id: String
    var statement: String?
    var value: String?
    var unit: String?
    var status: String?
    var evidencePointer: String?
    var supersedes: String?
    var supersededBy: String?
    var ownerLane: String?
    var updatedAt: String?
    var notes: String?

    enum CodingKeys: String, CodingKey {
        case id
        case statement
        case value
        case unit
        case status
        case evidencePointer = "evidence_pointer"
        case supersedes
        case supersededBy = "superseded_by"
        case ownerLane = "owner_lane"
        case updatedAt = "updated_at"
        case notes
    }
}

struct CockpitMapProvenanceModel: Codable, Equatable {
    var name: String?
    var status: String?
}

struct CockpitMapProvenanceCommit: Codable, Equatable {
    var sha: String?
    var message: String?
    var ts: String?
}

struct CockpitMapSummary: Equatable {
    var sellableCount: Int
    var liveNotSoldCount: Int
    var darkProductCount: Int
    var liveProductCount: Int
    var darkModelCount: Int
    var integrityCount: Int
    var trustScore: Int?

    init(products: [CockpitMapProduct], models: [CockpitMapModel], integrityWarnings: [CockpitMapIntegrityWarning], trustScore: Int?) {
        self.sellableCount = products.filter(\.isSellable).count
        self.liveNotSoldCount = products.filter { !$0.isSellable && $0.stage == "live" }.count
        self.darkProductCount = products.filter { $0.health.lowercased() == "dark" || $0.status?.lowercased() == "dark" }.count
        self.liveProductCount = products.filter { $0.stage.lowercased() == "live" }.count
        self.darkModelCount = models.filter { $0.status.lowercased() == "dark" }.count
        self.integrityCount = integrityWarnings.count
        self.trustScore = trustScore
    }
}

struct CockpitMapPipelineBucket: Equatable {
    var stage: String
    var products: [CockpitMapProduct]
}

struct CockpitMapSystemLane: Equatable {
    var layer: String
    var nodes: [CockpitMapSystemNode]
}
