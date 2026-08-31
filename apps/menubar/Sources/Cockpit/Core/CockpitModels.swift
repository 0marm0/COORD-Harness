import Foundation

struct CockpitState: Equatable {
    var schemaVersion: Int
    var writerSeq: Int64
    var builtAt: String?
    var sourceVersion: String?
    var stale: Bool
    var refreshing: Bool
    var mode: String?
    var liveMode: String?
    var summary: CockpitSummary
    var rows: [CockpitRow]
    var columns: [CockpitColumn]
    var groups: [CockpitGroup]
    var filterOptions: [CockpitFilterOption]
    var sessions: [CockpitSession]
    var diagnostics: [CockpitDiagnostic]
    var capabilityInventory: CockpitCapabilityInventory? = nil
    var error: CockpitLoadErrorState?

    static func error(_ error: CockpitLoadErrorState) -> CockpitState {
        CockpitState(
            schemaVersion: 0,
            writerSeq: 0,
            builtAt: nil,
            sourceVersion: nil,
            stale: true,
            refreshing: false,
            mode: nil,
            liveMode: nil,
            summary: CockpitSummary(),
            rows: [],
            columns: CockpitColumn.webDefaults,
            groups: [],
            filterOptions: [],
            sessions: [],
            diagnostics: [],
            error: error
        )
    }

    func row(dedupKey: String) -> CockpitRow? {
        rows.first { $0.dedupKey == dedupKey }
    }
}

struct CockpitLoadErrorState: Error, Equatable {
    var kind: CockpitLoadErrorKind
    var message: String
}

enum CockpitLoadErrorKind: String, Equatable {
    case missingSchema
    case missingProjectionMeta
    case unsupportedSchema
    case sqlite
    case transport
}

struct CockpitSummary: Equatable {
    var running: Int = 0
    var attention: Int = 0
    var next: Int = 0
    var local: Int = 0
    var doneToday: Int = 0
    var stale: Int = 0
    var blocked: Int = 0
    var launchable: Int = 0
}

struct CockpitRow: Equatable {
    var dedupKey: String
    var workID: String?
    var parentID: String? = nil
    var jobID: String? = nil
    var title: String
    var status: String
    var scope: String
    var owner: String?
    var ownerGroup: String?
    var ownerSessionID: String? = nil
    var ownerSessionActor: String? = nil
    var ownerSessionLabel: String? = nil
    var ownerExternalThreadID: String? = nil
    var ownerConversationTitle: String? = nil
    var ownerWorktreeID: String? = nil
    var module: String?
    var moduleLabel: String?
    var domainLabel: String?
    var resourceClass: String?
    var live: Bool? = nil
    var paused: Bool? = nil
    var stale: Bool? = nil
    var pct: Double?
    var pctDisplay: String?
    var etaSeconds: Double?
    var etaText: String?
    var etaDerived: Bool? = nil
    var rate: Double? = nil
    var done: Int? = nil
    var total: Int? = nil
    var progressKind: String? = nil
    var hasProgress: Bool? = nil
    var determinate: Bool? = nil
    var whyText: String?
    var noteText: String?
    var priority: String?
    var rowKind: String?
    var pid: Int? = nil
    var pgid: Int? = nil
    var sidecarAgeSeconds: Double? = nil
    var doneSignal: String? = nil
    var acceptanceSummary: String? = nil
    var contextPackRef: String? = nil
    var groupKey: String?
    var groupLabel: String?
    var displayOrder: Int
    var hierarchyDepth: Int = 0
    var actions: [CockpitRowAction]
    var workVersion: Int? = nil
    var currentAssignee: String? = nil
    var assignmentHeadEventIDs: [Int] = []
    var activeClaimIDs: [String] = []
    var claimStatus: String? = nil
    var claimLive: Bool = false
    var liveRunCount: Int = 0
    var nativeOperatorWritesEnabled: Bool = false
    var nativeOperatorWritesReason: String? = nil

    var effectivePct: Double? {
        guard let pct else { return nil }
        return max(0, min(100, pct))
    }

    var pctText: String {
        if let pctDisplay, !pctDisplay.isEmpty { return pctDisplay }
        if let effectivePct { return "\(Int(effectivePct.rounded()))%" }
        return "-"
    }
}

struct CockpitColumn: Codable, Equatable {
    var id: String
    var label: String
    var displayOrder: Int
    var width: Int
    var minWidth: Int
    var isVisible: Bool
    var alignment: String?

    static let webDefaults: [CockpitColumn] = [
        CockpitColumn(id: "state", label: "", displayOrder: 10, width: 4, minWidth: 2, isVisible: true, alignment: "center"),
        CockpitColumn(id: "owner", label: "", displayOrder: 20, width: 22, minWidth: 14, isVisible: true, alignment: "center"),
        CockpitColumn(id: "work", label: "Work", displayOrder: 30, width: 420, minWidth: 180, isVisible: true, alignment: "leading"),
        CockpitColumn(id: "module", label: "Module", displayOrder: 40, width: 142, minWidth: 92, isVisible: true, alignment: "leading"),
        CockpitColumn(id: "progress", label: "Progress", displayOrder: 50, width: 220, minWidth: 100, isVisible: true, alignment: "leading"),
        CockpitColumn(id: "eta", label: "ETA", displayOrder: 60, width: 74, minWidth: 56, isVisible: true, alignment: "trailing"),
        CockpitColumn(id: "note", label: "Note", displayOrder: 70, width: 260, minWidth: 120, isVisible: true, alignment: "leading"),
        CockpitColumn(id: "priority", label: "Prio", displayOrder: 80, width: 60, minWidth: 46, isVisible: true, alignment: "center"),
        CockpitColumn(id: "resource", label: "Resource", displayOrder: 90, width: 118, minWidth: 82, isVisible: true, alignment: "leading"),
        CockpitColumn(id: "id", label: "ID", displayOrder: 100, width: 190, minWidth: 110, isVisible: true, alignment: "leading"),
        CockpitColumn(id: "control", label: "", displayOrder: 110, width: 42, minWidth: 34, isVisible: true, alignment: "center"),
        CockpitColumn(id: "domain", label: "Domain", displayOrder: 120, width: 168, minWidth: 96, isVisible: false, alignment: "leading"),
        CockpitColumn(id: "why", label: "Why", displayOrder: 130, width: 300, minWidth: 120, isVisible: false, alignment: "leading"),
    ]
}

struct CockpitGroup: Equatable {
    var key: String
    var label: String
    var displayOrder: Int
    var count: Int
    var isCollapsed: Bool
}

struct CockpitFilterOption: Equatable {
    var kind: CockpitFilterKind
    var value: String
    var label: String
    var count: Int
    var displayOrder: Int
}

enum CockpitFilterKind: String, Equatable {
    case owner
    case module
    case status
    case domain
    case resource
    case scope
    case unknown

    init(raw: String?) {
        self = CockpitFilterKind(rawValue: (raw ?? "").lowercased()) ?? .unknown
    }
}

struct CockpitRowAction: Equatable {
    var id: String
    var label: String
    var isEnabled: Bool
    var requiresConfirmation: Bool
    var disabledReason: String?
    var displayOrder: Int
}

struct CockpitSession: Equatable {
    var id: String
    var actor: String
    var label: String
    var status: String
    var heartbeatAgeSeconds: Double?
    var isStale: Bool
}

struct CockpitDiagnostic: Equatable {
    var id: String
    var category: String
    var label: String
    var status: String
    var detail: String?
    var displayOrder: Int
}
