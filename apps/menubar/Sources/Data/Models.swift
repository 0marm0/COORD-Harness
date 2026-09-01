import Foundation


enum OwnerKind: String {
    case claude, codex, mixed, local
    case operatorUser
    init(_ raw: String?) {
        switch (raw ?? "").lowercased() {
        case "claude":   self = .claude
        case "codex":    self = .codex
        case "mixed":    self = .mixed
        case "operator": self = .operatorUser
        default:         self = .local
        }
    }
}

struct MenubarState: Codable {
    var schemaVersion: Int?
    var version: Int?
    var mode: String?
    var liveMode: String?
    var governor: String?
    var govLive: GovLive?
    var diagnostics: Diagnostics?
    var sidecarErrors: [SidecarError]?
    var source: String?
    var coordActive: Bool?
    var coordCutover: Bool?
    var coordNativeSource: String?
    var stale: Bool?
    var refreshing: Bool?
    var schemaAhead: Bool?
    var error: String?
    var ts: Double?

    var workModel: WorkModel?
    var localLanes: LocalLanes?
    var agentMilestones: [Row]?
    var agentSessions: [Row]?


    var healthSummary: HealthSummary?


    var displayMode: String { liveMode ?? mode ?? "full" }
    var hasProjectionWarning: Bool {
        guard stale == true else { return false }
        if workModel == nil { return true }
        return error?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false
    }


    var normalizedAgentMilestones: [Row] { workModel?.agentMilestoneRows ?? agentMilestones ?? [] }
    var normalizedLocalLanes: LocalLanes? { workModel?.localLanes ?? localLanes }
}

struct GovLive: Codable {
    var mode: String?; var floorGb: Double?; var freeGb: Double?
    var alive: Bool?; var lastAction: String?
}

struct Diagnostics: Codable {
    var agentSidecars: Int?; var agentMilestones: Int?; var jobSidecars: Int?
    var roadmapItems: Int?; var unifiedRows: Int?; var sidecarParseErrors: Int?
    var projectionTs: Double?
}

struct SidecarError: Codable { var path: String?; var error: String? }


struct HealthSummary: Codable {
    var open: Int?
    var running: Int?
    var blocked: Int?
    var attention: Int?
    var stale14d: Int?
    var reviewOpen: Int?
    var reviewCreated7d: Int?
    var reviewClosed7d: Int?
    var phantomNoClaim: Int?
    var wipByLane: [String: Int]?
    var generatedAt: Double?

    enum CodingKeys: String, CodingKey {
        case open, running, blocked, attention
        case stale14d = "stale_14d"
        case reviewOpen = "review_open"
        case reviewCreated7d = "review_created_7d"
        case reviewClosed7d = "review_closed_7d"
        case phantomNoClaim = "phantom_no_claim"
        case wipByLane = "wip_by_lane"
        case generatedAt = "generated_at"
    }


    var stripText: String {
        let claude = wipByLane?["claude"] ?? 0
        let codex = wipByLane?["codex"] ?? 0
        return "open \(open ?? 0) · running \(running ?? 0) · blocked \(blocked ?? 0) · "
            + "attention \(attention ?? 0) · stale \(stale14d ?? 0) · reviews \(reviewOpen ?? 0) · "
            + "phantom \(phantomNoClaim ?? 0) · WIP \(claude)/\(codex)"
    }


    var isWarn: Bool { (phantomNoClaim ?? 0) > 0 || (reviewOpen ?? 0) > 40 }
}

struct Summary: Codable {
    var running: Int?; var planned: Int?; var queued: Int?; var blocked: Int?; var done: Int?
    var doneToday: Int?; var open: Int?; var total: Int?
    var attention: Int?; var next: Int?; var queueOpen: Int?; var queueBlocked: Int?
    var queueTerminal: Int?; var followup: Int?


    var nextQueued: Int?
    var nextPlanned: Int?


    var nextUpnext: Int?
    var nextBacklog: Int?
}


struct HierarchyCounts: Codable {
    var total: Int?; var running: Int?; var attention: Int?; var next: Int?; var done: Int?
}
struct HierarchyEpic: Codable {
    var id: String?
    var label: String?
    var domainShortLabel: String?
    var counts: HierarchyCounts?
    var jobs: [Row]?
    var jobsTruncated: Int?
}
struct HierarchySummary: Codable { var epics: Int?; var rows: Int? }
struct WorkHierarchy: Codable {
    var epics: [HierarchyEpic]?
    var summary: HierarchySummary?
}


struct RowsTruncation: Codable { var shown: Int?; var total: Int? }

struct WorkModel: Codable {
    var summary: Summary?
    var runningRows: [Row]?
    var attentionRows: [Row]?
    var followupRows: [Row]?
    var nextRows: [Row]?
    var queueActiveRows: [Row]?
    var queueBlockedRows: [Row]?
    var queueTerminalRows: [Row]?
    var statusRows: [Row]?
    var localLanes: LocalLanes?
    var agentMilestoneRows: [Row]?
    var hierarchy: WorkHierarchy?
    var truncation: [String: RowsTruncation]?
    var partial: Bool?
}

struct LocalLanes: Codable {
    var summary: LaneSummary?
    var lanes: [String: Lane]?
}
struct LaneSummary: Codable { var running: Int?; var ready: Int?; var held: Int?; var done: Int? }
struct Lane: Codable {
    var running: [Row]?; var ready: [Row]?; var held: [Row]?
    var doneRecent: [Row]?; var doneCount: Int?
}

@propertyWrapper
struct LossyStringArray: Codable {
    var wrappedValue: [String]?

    init() { wrappedValue = nil }
    init(wrappedValue: [String]?) { self.wrappedValue = wrappedValue }

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() {
            wrappedValue = nil
        } else if let values = try? c.decode([String].self) {
            wrappedValue = values
        } else if let raw = try? c.decode(String.self) {
            let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmed.isEmpty {
                wrappedValue = nil
            } else if trimmed.hasPrefix("["),
                      let data = trimmed.data(using: .utf8),
                      let values = try? JSONDecoder().decode([String].self, from: data) {
                wrappedValue = values
            } else {
                wrappedValue = [trimmed]
            }
        } else {
            wrappedValue = nil
        }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        if let wrappedValue { try c.encode(wrappedValue) }
        else { try c.encodeNil() }
    }
}

@propertyWrapper
struct LossyInt: Codable {
    var wrappedValue: Int?

    init() { wrappedValue = nil }
    init(wrappedValue: Int?) { self.wrappedValue = wrappedValue }

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() {
            wrappedValue = nil
        } else if (try? c.decode(Bool.self)) != nil {
            wrappedValue = nil
        } else if let value = try? c.decode(Int.self) {
            wrappedValue = value
        } else if let value = try? c.decode(Double.self) {
            wrappedValue = Int(value)
        } else if let raw = try? c.decode(String.self),
                  let value = Double(raw.trimmingCharacters(in: .whitespacesAndNewlines)) {
            wrappedValue = Int(value)
        } else {
            wrappedValue = nil
        }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        if let wrappedValue { try c.encode(wrappedValue) }
        else { try c.encodeNil() }
    }
}

@propertyWrapper
struct LossyDouble: Codable {
    var wrappedValue: Double?

    init() { wrappedValue = nil }
    init(wrappedValue: Double?) { self.wrappedValue = wrappedValue }

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() {
            wrappedValue = nil
        } else if (try? c.decode(Bool.self)) != nil {
            wrappedValue = nil
        } else if let value = try? c.decode(Double.self) {
            wrappedValue = value
        } else if let value = try? c.decode(Int.self) {
            wrappedValue = Double(value)
        } else if let raw = try? c.decode(String.self) {
            let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            if let value = Double(trimmed) {
                wrappedValue = value
            } else if let date = ISO8601DateFormatter().date(from: trimmed) {
                wrappedValue = date.timeIntervalSince1970
            } else {
                wrappedValue = nil
            }
        } else {
            wrappedValue = nil
        }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        if let wrappedValue { try c.encode(wrappedValue) }
        else { try c.encodeNil() }
    }
}

extension KeyedDecodingContainer {
    func decode(_ type: LossyStringArray.Type, forKey key: Key) throws -> LossyStringArray {
        try decodeIfPresent(type, forKey: key) ?? LossyStringArray()
    }

    func decode(_ type: LossyInt.Type, forKey key: Key) throws -> LossyInt {
        try decodeIfPresent(type, forKey: key) ?? LossyInt()
    }

    func decode(_ type: LossyDouble.Type, forKey key: Key) throws -> LossyDouble {
        try decodeIfPresent(type, forKey: key) ?? LossyDouble()
    }
}


struct Row: Codable {

    var id: String?
    var roadmapId: String?
    var jobId: String?
    var dedupKey: String?
    var section: String?
    var lane: String?
    var source: String?


    var name: String?
    var display: String?


    var status: String?
    var paused: Bool?
    var live: Bool?
    var stale: Bool?


    var pct: Double?
    var pctDisplay: String?
    var etaS: Double?
    var etaText: String?
    var etaDerived: Bool?
    var rate: Double?
    var rateUnit: String?
    @LossyInt var done: Int?
    @LossyInt var total: Int?
    var indeterminate: Bool?
    var loading: Bool?
    var hasProgress: Bool?
    var determinate: Bool?


    var owner: String?
    var owners: [String]?
    var ownerGroup: String?

    var ownerSessionId: String?
    var ownerSessionActor: String?
    var ownerSessionLabel: String?
    var ownerExternalThreadId: String?
    var ownerConversationTitle: String?
    var ownerWorktreeId: String?
    // The projection's resolved orchestrating-chat key. Only the server can
    // bridge one chat's several identities and roll a subagent up under the
    // chat that spawned it, so the agent view prefers this over anything it
    // could derive locally. Absent on a projection older than this field.
    var sessionGroupKey: String?
    var sessionGroupLabel: String?
    var crossAgentHandoff: Bool?
    var handoffFrom: String?
    var handoffTo: String?
    var handoffLabel: String?
    var kind: String?
    var rowKind: String?
    var workKind: String?
    var platform: String?
    var hasProcess: Bool?
    var progressKind: String?
    var resourceClass: String?
    var module: String?
    var moduleLabel: String?
    var domainLabel: String?
    var domainShortLabel: String?
    var priority: String?
    var tier: String?
    var epic: String?
    var effectiveEpic: String?
    var sublane: String?
    var parent: String?
    var surface: String?


    var tasks: [Row]?
    var tasksTruncated: Int?


    var detail: String?
    var note: String?
    var step: String?
    var currentStep: String?
    var nextRankReason: String?
    var whyNext: String?


    var pid: Int?; var pgid: Int?; var cpu: Double?; var ramMb: Double?; var sidecarAgeS: Double?


    var operatorState: String?
    var operatorLastAction: String?
    var visibility: String?
    var blockedReasonClass: String?
    var coordClaimStatus: String?


    var queuePosition: Int?
    var queueStatus: String?
    var queueLaunchable: Bool?


    var assignee: String?
    @LossyStringArray var dependsOn: [String]?
    var doneSignal: String?
    var doneSignalExists: Bool?
    var acceptance: String?
    var acceptanceJson: String?
    var acceptanceSummary: String?
    var contextPackRef: String?
    var rubricState: String?
    var rubricVerdict: String?
    @LossyInt var tokenBudget: Int?
    var heartbeatDueAt: String?
    var dueDate: String?
    var mode: String?
    var reqMode: String?
    var modelLabel: String?
    var nproc: Int?
    var nextRank: [Double]?
    @LossyDouble var nextRankRecent: Double?
    @LossyDouble var createdAt: Double?
    @LossyDouble var updatedAt: Double?
    @LossyDouble var lastSeenAt: Double?


    var title: String { display ?? name ?? "Task" }


    var etaDisplay: String {
        if let s = etaS, let f = ETAFormat.fmtETA(s) { return f }
        if let t = etaText, let secs = ETAFormat.parse(t), let f = ETAFormat.fmtETA(secs) { return f }
        return ""
    }


    var etaLive: String {
        if let t = etaText?.trimmingCharacters(in: .whitespaces), !t.isEmpty, t != "—", t != "~" {
            if let secs = ETAFormat.parse(t), secs >= 3600, let c = ETAFormat.fmtETA(secs) { return ETAFormat.spaced(c) }
            return t
        }
        if let s = etaS, let f = ETAFormat.fmtETA(s) { return ETAFormat.spaced(f) }
        return ""
    }


    var pctText: String {
        if let p = pctDisplay, !p.isEmpty { return p }
        if let v = effectivePct { return "\(Int(v.rounded()))%" }
        return "—"
    }


    var effectivePct: Double? {
        if let pct { return pct }
        if let d = done, let t = total, t > 0 { return max(0, min(100, (Double(d) / Double(t)) * 100.0)) }
        return nil
    }


    var showsIndeterminateBar: Bool { (loading ?? false) || (indeterminate ?? false) || effectivePct == nil }

    var ownerKind: OwnerKind { OwnerKind(owners?.first ?? owner) }
    var iconOwnerKind: OwnerKind {
        let direct = OwnerKind(owners?.first ?? owner)
        return direct == .local ? ownerGroupKind : direct
    }


    var stableKey: String { dedupKey ?? roadmapId ?? jobId ?? id ?? (name ?? "row") }


    var detailRichness: Int {
        var n = 0
        if (note?.isEmpty == false) { n += 1 }
        if (doneSignal?.isEmpty == false) { n += 1 }
        if (acceptanceSummary?.isEmpty == false) || (acceptanceJson?.isEmpty == false) || (acceptance?.isEmpty == false) { n += 1 }
        if (contextPackRef?.isEmpty == false) { n += 1 }
        if (moduleLabel?.isEmpty == false) || (module?.isEmpty == false) { n += 1 }
        if (priority?.isEmpty == false) { n += 1 }
        if etaS != nil || (etaText?.isEmpty == false && etaText != "—") { n += 1 }
        if effectivePct != nil { n += 1 }
        if done != nil { n += 1 }
        return n
    }


    var telemetryRichness: Int {
        var n = 0
        if live == true { n += 4 }
        if hasProgress == true || determinate == true { n += 4 }
        if (progressKind ?? "").lowercased() == "determinate" { n += 4 }
        if effectivePct != nil { n += 3 }
        if done != nil && total != nil { n += 3 }
        if etaS != nil || (etaText?.isEmpty == false && etaText != "—") { n += 2 }
        if rate != nil { n += 2 }
        if pid != nil || pgid != nil || hasProcess == true { n += 1 }
        return n
    }


    var isGPU: Bool {
        let k = (kind ?? "").lowercased(); let l = (lane ?? "").lowercased()
        if ["cpu", "ram", "disk"].contains(k) { return false }
        if ["gpu", "mlx", "cuda"].contains(k) || ["gpu", "mlx", "cuda"].contains(l) { return true }
        return (name ?? "").lowercased().hasPrefix("gpu")
    }


    var isLocalProcess: Bool { hasProcess == true || pid != nil || pgid != nil || (section ?? "") == "Local Processes" }


    var isAgentCoordination: Bool {
        if (workKind ?? "").lowercased() == "agent_coordination" { return true }
        let rk = (rowKind ?? "").lowercased()
        let k = (kind ?? resourceClass ?? "").lowercased()
        let src = (source ?? "").lowercased()
        if rk == "agent" || k == "agent" || src == "agent_milestone" { return true }
        return (coordClaimStatus?.isEmpty == false) && !isLocalProcess
    }


    var hasProgressSignal: Bool {
        if let h = hasProgress { return h }
        if let d = determinate { return d }
        let pk = (progressKind ?? "").lowercased()
        if pk == "determinate" || pk == "indeterminate" { return true }
        if pk == "none" { return false }
        if effectivePct != nil { return true }
        if let t = total, t > 0, done != nil { return true }
        return false
    }


    var showsBar: Bool { isLocalProcess || hasProgressSignal || ((loading ?? false) && !isAgentCoordination) }


    var ownerGroupKind: OwnerKind {
        if let g = ownerGroup, !g.isEmpty { return OwnerKind(g) }
        return ownerKind
    }


    enum QueueRowKind { case running, done, attention, next }
    var queueRowKind: QueueRowKind {
        let s = (status ?? "").uppercased()
        if pid != nil || live == true || paused == true || s == "RUNNING" || s == "PAUSED" { return .running }
        if s == "DONE" || s == "KILLED" { return .done }
        if ["BLOCKED", "FAILED", "STALLED"].contains(s) || queueLaunchable == false { return .attention }
        return .next
    }


    var isOperatorActionBlock: Bool {
        let values = [
            operatorState,
            blockedReasonClass,
            owner,
            assignee,
            coordClaimStatus,
        ].compactMap { $0?.lowercased() }
        if values.contains("operator") || values.contains("human") { return true }
        let actionTokens = [
            "needs_operator", "operator_required", "human_decision",
            "operator_decision", "interactive_auth", "auth_required",
        ]
        if values.contains(where: { actionTokens.contains($0) }) { return true }
        return (dependsOn ?? []).contains {
            $0.lowercased().contains("operator") || $0.lowercased().contains("human")
        }

    }
}
enum StatusItemTaskSelection {
    static func topRow(in state: MenubarState) -> Row? {
        topRow(from: state.workModel?.runningRows ?? [])
    }

    static func topRow(from rows: [Row]) -> Row? {
        let active = rows.filter(isActivelyRunning)
        if let withPercent = active
            .filter({ $0.effectivePct != nil })
            .max(by: { ($0.effectivePct ?? 0) < ($1.effectivePct ?? 0) }) {
            return withPercent
        }
        return active.first
    }

    private static func isActivelyRunning(_ row: Row) -> Bool {
        row.live == true
            && row.paused != true
            && row.stale != true
            && row.status?.trimmingCharacters(in: .whitespacesAndNewlines).uppercased() == "RUNNING"
    }
}
