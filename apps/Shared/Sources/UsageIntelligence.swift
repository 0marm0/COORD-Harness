import Foundation

enum UsageBarPalette: String, Codable, CaseIterable, Sendable {
    case colored
    case neutral

    static func resolve(_ rawValue: String?) -> UsageBarPalette {
        UsageBarPalette(rawValue: rawValue?.lowercased() ?? "") ?? .colored
    }
}

enum UsageIntelligenceContract {
    static let identifier = "coordharness.usage-intelligence.v1"

    static func validate(_ schema: String) throws {
        guard schema == identifier else {
            throw UsageIntelligenceError.unsupportedContract(schema)
        }
    }
}

enum UsageIntelligenceError: LocalizedError, Equatable {
    case unsupportedContract(String)

    var errorDescription: String? {
        switch self {
        case let .unsupportedContract(schema):
            "Unsupported usage contract: \(schema)"
        }
    }
}

struct UsageCalendarDay: Codable, Equatable, Hashable, Sendable, CustomStringConvertible {
    let rawValue: String

    init?(_ rawValue: String) {
        let parts = rawValue.split(separator: "-", omittingEmptySubsequences: false)
        guard parts.count == 3,
              parts[0].count == 4,
              parts[1].count == 2,
              parts[2].count == 2,
              let year = Int(parts[0]),
              let month = Int(parts[1]),
              let day = Int(parts[2]),
              year > 0,
              rawValue == String(format: "%04d-%02d-%02d", year, month, day) else {
            return nil
        }

        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        guard let date = calendar.date(from: DateComponents(year: year, month: month, day: day)) else {
            return nil
        }
        let verified = calendar.dateComponents([.year, .month, .day], from: date)
        guard verified.year == year, verified.month == month, verified.day == day else {
            return nil
        }
        self.rawValue = rawValue
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        let value = try container.decode(String.self)
        guard let day = Self(value) else {
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "Expected a valid calendar day in YYYY-MM-DD format."
            )
        }
        self = day
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawValue)
    }

    var description: String { rawValue }
}

struct UsageIntelligenceSnapshot: Codable, Equatable, Sendable {
    let schema: String
    let generatedAt: Date?
    let staleAfter: Date?
    let refresh: UsageRefresh?
    let providers: [String: UsageProvider]
    let errors: [UsageServiceError]

    enum CodingKeys: String, CodingKey {
        case schema
        case generatedAt = "generated_at"
        case staleAfter = "stale_after"
        case refresh, providers, errors
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        schema = try values.decode(String.self, forKey: .schema)
        try UsageIntelligenceContract.validate(schema)
        generatedAt = try values.decodeIfPresent(Date.self, forKey: .generatedAt)
        staleAfter = try values.decodeIfPresent(Date.self, forKey: .staleAfter)
        refresh = try values.decodeIfPresent(UsageRefresh.self, forKey: .refresh)
        providers = try values.decodeIfPresent([String: UsageProvider].self, forKey: .providers) ?? [:]
        errors = try values.decodeIfPresent([UsageServiceError].self, forKey: .errors) ?? []
    }

    init(
        schema: String,
        generatedAt: Date?,
        staleAfter: Date?,
        refresh: UsageRefresh?,
        providers: [String: UsageProvider],
        errors: [UsageServiceError]
    ) {
        self.schema = schema
        self.generatedAt = generatedAt
        self.staleAfter = staleAfter
        self.refresh = refresh
        self.providers = providers
        self.errors = errors
    }

    var isProducerStale: Bool {
        ["stale", "stale_refreshing"].contains(refresh?.state?.lowercased() ?? "")
            || staleAfter.map { $0 <= Date() } == true
            || providers.values.contains { $0.hasStaleQuotaObservation }
    }

    /// A successful HTTP response is not necessarily a usable observation. The
    /// proxy intentionally returns bounded empty/error envelopes when its
    /// upstream is unavailable; those envelopes must never erase last-good
    /// provider data in long-lived clients.
    var hasMeaningfulProviderData: Bool {
        guard !["error", "unavailable"].contains(refresh?.state?.lowercased() ?? "") else {
            return false
        }
        return providers.values.contains { $0.hasMeaningfulUsageData }
    }
}

struct UsageRefresh: Codable, Equatable, Sendable {
    let state: String?
    let generatedAt: Date?

    enum CodingKeys: String, CodingKey {
        case state
        case generatedAt = "generated_at"
    }
}

struct UsageServiceError: Codable, Equatable, Identifiable, Sendable {
    let code: String
    var id: String { code }
    var displayLabel: String { UsagePresentationText.errorLabel(code) }
}

struct UsageProvider: Codable, Equatable, Sendable {
    let source: UsageSource?
    let quotaSource: UsageQuotaSource?
    let account: UsageAccount?
    let windows: [UsageQuotaWindow]
    let quotaGroups: [UsageQuotaGroup]
    let resetCredits: [UsageResetCredit]
    let runout: UsageRunout?
    let history: UsageHistory?
    let costs: UsageCosts?
    let activeSessions: UsageActiveSessions?
    let breakdowns: UsageBreakdowns?
    let errors: [UsageServiceError]
    let liveObservedAt: Date?
    let liveObservationState: String?

    enum CodingKeys: String, CodingKey {
        case source, account, windows, runout, history, costs, breakdowns, errors
        case quotaSource = "quota_source"
        case quotaGroups = "quota_groups"
        case resetCredits = "reset_credits"
        case activeSessions = "active_sessions"
        case liveObservedAt = "live_observed_at"
        case liveObservationState = "live_observation_state"
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        source = try values.decodeIfPresent(UsageSource.self, forKey: .source)
        quotaSource = try values.decodeIfPresent(UsageQuotaSource.self, forKey: .quotaSource)
        account = try values.decodeIfPresent(UsageAccount.self, forKey: .account)
        windows = try values.decodeIfPresent([UsageQuotaWindow].self, forKey: .windows) ?? []
        quotaGroups = Array((try values.decodeIfPresent([UsageQuotaGroup].self, forKey: .quotaGroups) ?? []).prefix(16))
        resetCredits = try values.decodeIfPresent([UsageResetCredit].self, forKey: .resetCredits) ?? []
        runout = try values.decodeIfPresent(UsageRunout.self, forKey: .runout)
        history = try values.decodeIfPresent(UsageHistory.self, forKey: .history)
        costs = try values.decodeIfPresent(UsageCosts.self, forKey: .costs)
        activeSessions = try values.decodeIfPresent(UsageActiveSessions.self, forKey: .activeSessions)
        breakdowns = try values.decodeIfPresent(UsageBreakdowns.self, forKey: .breakdowns)
        errors = try values.decodeIfPresent([UsageServiceError].self, forKey: .errors) ?? []
        liveObservedAt = try values.decodeIfPresent(Date.self, forKey: .liveObservedAt)
        liveObservationState = try values.decodeIfPresent(String.self, forKey: .liveObservationState)
    }

    init(
        source: UsageSource?,
        quotaSource: UsageQuotaSource?,
        account: UsageAccount?,
        windows: [UsageQuotaWindow],
        quotaGroups: [UsageQuotaGroup],
        resetCredits: [UsageResetCredit],
        runout: UsageRunout?,
        history: UsageHistory?,
        costs: UsageCosts?,
        activeSessions: UsageActiveSessions?,
        breakdowns: UsageBreakdowns?,
        errors: [UsageServiceError],
        liveObservedAt: Date?,
        liveObservationState: String?
    ) {
        self.source = source
        self.quotaSource = quotaSource
        self.account = account
        self.windows = windows
        self.quotaGroups = quotaGroups
        self.resetCredits = resetCredits
        self.runout = runout
        self.history = history
        self.costs = costs
        self.activeSessions = activeSessions
        self.breakdowns = breakdowns
        self.errors = errors
        self.liveObservedAt = liveObservedAt
        self.liveObservationState = liveObservationState
    }
}

extension UsageProvider {
    var hasStaleQuotaObservation: Bool {
        [
            "stale", "stale_last_good", "stale_last_good_no_current_windows",
            "quota_observation_expired", "quota_observation_unavailable",
        ].contains(liveObservationState?.lowercased() ?? "")
    }

    var hasMeaningfulUsageData: Bool {
        account != nil
            || !windows.isEmpty
            || !quotaGroups.isEmpty
            || !resetCredits.isEmpty
            || runout != nil
            || history != nil
            || costs != nil
            || activeSessions != nil
            || breakdowns != nil
            || liveObservedAt != nil
    }
}

extension UsageIntelligenceSnapshot {
    func retainingBoundedClaudeQuota(
        from prior: UsageIntelligenceSnapshot?,
        at date: Date
    ) -> UsageIntelligenceSnapshot {
        guard
            let prior,
            let current = providers["claude"],
            current.account?.authenticated == true,
            !current.hasQuotaWindows,
            !current.explicitlyClearsQuota
        else { return self }

        let priorClaude = prior.providers["claude"]
        let groups = (priorClaude?.quotaGroups ?? []).compactMap { group -> UsageQuotaGroup? in
            let windows = group.windows.filter { $0.resetsAt.map { $0 > date } == true }
            guard !windows.isEmpty else { return nil }
            return UsageQuotaGroup(
                key: group.key,
                label: group.label,
                semantics: group.semantics,
                windows: windows,
                runout: nil
            )
        }
        let windows = (priorClaude?.windows ?? []).filter {
            $0.resetsAt.map { $0 > date } == true
        }
        guard !groups.isEmpty || !windows.isEmpty else { return self }

        let warning = "Showing bounded last-good Claude quota until its recorded reset; current quota windows are unavailable."
        let priorSource = priorClaude?.quotaSource
        let quotaSource = UsageQuotaSource(
            kind: priorSource?.kind,
            canonical: false,
            label: priorSource?.label,
            warning: warning
        )
        var providerErrors = current.errors
        if !providerErrors.contains(where: { $0.code == "claude_quota_windows_retained_last_good" }) {
            providerErrors.append(UsageServiceError(code: "claude_quota_windows_retained_last_good"))
        }
        let retained = UsageProvider(
            source: current.source,
            quotaSource: quotaSource,
            account: current.account,
            windows: windows,
            quotaGroups: groups,
            resetCredits: current.resetCredits,
            runout: current.runout,
            history: current.history,
            costs: current.costs,
            activeSessions: current.activeSessions,
            breakdowns: current.breakdowns,
            errors: providerErrors,
            liveObservedAt: current.liveObservedAt,
            liveObservationState: "stale_last_good_no_current_windows"
        )
        var providers = providers
        providers["claude"] = retained
        var errors = errors
        if !errors.contains(where: { $0.code == "claude_quota_windows_retained_last_good" }) {
            errors.append(UsageServiceError(code: "claude_quota_windows_retained_last_good"))
        }
        return UsageIntelligenceSnapshot(
            schema: schema,
            generatedAt: generatedAt,
            staleAfter: staleAfter,
            refresh: refresh,
            providers: providers,
            errors: errors
        )
    }
}

private extension UsageProvider {
    var hasQuotaWindows: Bool {
        !windows.isEmpty || quotaGroups.contains { !$0.windows.isEmpty }
    }

    var explicitlyClearsQuota: Bool {
        let observation = liveObservationState?.lowercased() ?? ""
        let accountState = account?.status?.lowercased() ?? ""
        return ["expired", "quota_observation_expired"].contains(observation)
            || accountState.contains("expired")
            || accountState.contains("sign_out")
            || accountState.contains("signed_out")
    }
}


struct UsageBreakdowns: Codable, Equatable, Sendable {
    let models: UsageBreakdown<UsageModelBreakdownItem>?
    let projects: UsageBreakdown<UsageProjectBreakdownItem>?
}

protocol UsageBreakdownItem: Codable, Equatable, Sendable {
    var key: String? { get }
    var label: String? { get }
    var totalTokens: Int64? { get }
}

struct UsageBreakdown<Item: UsageBreakdownItem>: Codable, Equatable, Sendable {
    static var itemLimit: Int { 25 }

    let status: String?
    let semantics: String?
    let canonical: Bool?
    let coverageStart: UsageCalendarDay?
    let coverageEnd: UsageCalendarDay?
    let observedAt: Date?
    let omittedCount: Int
    let items: [Item]

    enum CodingKeys: String, CodingKey {
        case status, semantics, canonical, items
        case coverageStart = "coverage_start"
        case coverageEnd = "coverage_end"
        case observedAt = "observed_at"
        case omittedCount = "omitted_count"
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        status = try values.decodeIfPresent(String.self, forKey: .status)
        semantics = try values.decodeIfPresent(String.self, forKey: .semantics)
        canonical = try values.decodeIfPresent(Bool.self, forKey: .canonical)
        coverageStart = try values.decodeIfPresent(UsageCalendarDay.self, forKey: .coverageStart)
        coverageEnd = try values.decodeIfPresent(UsageCalendarDay.self, forKey: .coverageEnd)
        observedAt = try values.decodeIfPresent(Date.self, forKey: .observedAt)
        var retained: [Item] = []
        var decodedCount = 0
        if values.contains(.items), try !values.decodeNil(forKey: .items) {
            var itemContainer = try values.nestedUnkeyedContainer(forKey: .items)
            while !itemContainer.isAtEnd {
                retained.append(try itemContainer.decode(Item.self))
                decodedCount += 1
                if retained.count > Self.itemLimit {
                    retained = Array(Self.ranked(retained).prefix(Self.itemLimit))
                }
            }
        }
        items = Self.ranked(retained)
        omittedCount = max(0, try values.decodeIfPresent(Int.self, forKey: .omittedCount) ?? 0)
            + max(0, decodedCount - Self.itemLimit)
    }

    var rankedItems: [Item] {
        Self.ranked(items)
    }

    private static func ranked(_ items: [Item]) -> [Item] {
        items.sorted { lhs, rhs in
            switch (lhs.totalTokens, rhs.totalTokens) {
            case let (left?, right?) where left != right: return left > right
            case (_?, nil): return true
            case (nil, _?): return false
            default:
                let left = lhs.label ?? lhs.key ?? ""
                let right = rhs.label ?? rhs.key ?? ""
                return left.localizedCaseInsensitiveCompare(right) == .orderedAscending
            }
        }
    }
}

struct UsageModelBreakdownItem: UsageBreakdownItem, Identifiable {
    let key: String?
    let label: String?
    let totalTokens: Int64?
    let todayTotalTokens: Int64?
    let rolling7DTotalTokens: Int64?
    let calendarWeekTotalTokens: Int64?
    let providerNativeCostNanos: Int64?
    let apiRateEstimateNanos: Int64?

    enum CodingKeys: String, CodingKey {
        case key, label
        case totalTokens = "total_tokens"
        case todayTotalTokens = "today_total_tokens"
        case rolling7DTotalTokens = "rolling_7d_total_tokens"
        case calendarWeekTotalTokens = "calendar_week_total_tokens"
        case providerNativeCostNanos = "provider_native_cost_nanos"
        case apiRateEstimateNanos = "api_rate_estimate_nanos"
    }

    var id: String { "\(key ?? "unknown"):models:\(label ?? "unknown")" }
}

struct UsageProjectBreakdownItem: UsageBreakdownItem, Identifiable {
    let key: String?
    let label: String?
    let totalTokens: Int64?
    let todayTotalTokens: Int64?
    let rolling7DTotalTokens: Int64?
    let calendarWeekTotalTokens: Int64?
    let topModel: String?

    enum CodingKeys: String, CodingKey {
        case key, label
        case totalTokens = "total_tokens"
        case todayTotalTokens = "today_total_tokens"
        case rolling7DTotalTokens = "rolling_7d_total_tokens"
        case calendarWeekTotalTokens = "calendar_week_total_tokens"
        case topModel = "top_model"
    }

    var id: String { "\(sanitizedOpaqueKey):projects:\(sanitizedLabel)" }

    var sanitizedLabel: String {
        UsageProjectSanitizer.safe(label, fallback: "Project")
    }

    var sanitizedOpaqueKey: String {
        UsageProjectSanitizer.safe(key, fallback: "opaque key unavailable")
    }
}

private enum UsageProjectSanitizer {
    static func safe(_ value: String?, fallback: String) -> String {
        guard let value = value?.trimmingCharacters(in: .whitespacesAndNewlines),
              !value.isEmpty,
              value.count <= 96,
              !value.contains("/"),
              !value.contains("\\"),
              !value.hasPrefix("~") else {
            return fallback
        }
        return value
    }
}

struct UsageSource: Codable, Equatable, Sendable {
    let kind: String?
    let canonical: Bool?
    let schema: String?
    let label: String?
    let warning: String?

    var displayLabel: String {
        UsagePresentationText.sourceLabel(kind: kind, label: label)
    }

    var displayWarning: String? {
        UsagePresentationText.warning(warning)
    }
}

struct UsageQuotaSource: Codable, Equatable, Sendable {
    let kind: String?
    let canonical: Bool?
    let label: String?
    let warning: String?

    var displayLabel: String {
        UsagePresentationText.sourceLabel(kind: kind, label: label, fallback: "Live quota source")
    }

    var displayWarning: String? {
        UsagePresentationText.warning(warning)
    }

    var authorityLabel: String {
        canonical == true ? "authoritative live quota" : "noncanonical quota source"
    }
}

struct UsageAccount: Codable, Equatable, Sendable {
    let status: String?
    let plan: String?
    let authenticated: Bool?

    var redactedDisplay: String {
        let candidate = plan ?? "unknown"
        let safePlan = candidate.contains("@") ? "unknown" : candidate
        if safePlan == "unknown" { return status ?? "unknown" }
        return "\(safePlan) · \(status ?? "unknown")"
    }
}

struct UsageQuotaWindow: Codable, Equatable, Identifiable, Sendable {
    let kind: String?
    let name: String?
    let windowMinutes: Int?
    let usedPercent: Double?
    let remainingPercent: Double?
    let resetsAt: Date?
    let countdownSeconds: Int?
    let pace: UsageQuotaPace?

    enum CodingKeys: String, CodingKey {
        case kind, name
        case windowMinutes = "window_minutes"
        case usedPercent = "used_percent"
        case remainingPercent = "remaining_percent"
        case resetsAt = "resets_at"
        case countdownSeconds = "countdown_seconds"
        case pace
    }

    var id: String { "\(kind ?? "bucket"):\(name ?? "usage")" }
    var displayName: String { name?.isEmpty == false ? name! : (kind?.capitalized ?? "Quota") }
    var resolvedRemainingPercent: Double? {
        let value = remainingPercent ?? usedPercent.map { 100 - $0 }
        guard let value, value.isFinite else { return nil }
        return min(max(value, 0), 100)
    }

    var clampedRemainingFraction: Double? { resolvedRemainingPercent.map { $0 / 100 } }
}

struct UsageQuotaPace: Codable, Equatable, Sendable {
    let state: String?
    let deltaPercent: Double?
    let expectedUsedPercent: Double?
    let willLastToReset: Bool?
    let secondsToExhaustion: Int?
    let advisory: Bool?
    let basis: String?
    let source: String?

    enum CodingKeys: String, CodingKey {
        case state, advisory, basis, source
        case deltaPercent = "delta_percent"
        case expectedUsedPercent = "expected_used_percent"
        case willLastToReset = "will_last_to_reset"
        case secondsToExhaustion = "seconds_to_exhaustion"
    }

    var provenanceLabel: String {
        switch source {
        case "codexbar_local_projection":
            return "Legacy compatibility pace projection"
        case "local_projection":
            return "Local pace projection"
        default:
            return "Local pace projection"
        }
    }

    var deltaLabel: String? {
        guard let deltaPercent, deltaPercent.isFinite else { return nil }
        let amount = Int(min(max(deltaPercent, 0), 100).rounded())
        switch state {
        case "reserve": return "\(amount)% in reserve"
        case "deficit": return "\(amount)% in deficit"
        case "on_pace": return "On pace"
        default: return nil
        }
    }

    var runoutLabel: String? {
        guard let willLastToReset else { return nil }
        if willLastToReset {
            return "Projected to last until reset"
        }
        guard let secondsToExhaustion else { return "May run out before reset" }
        return "May run out in \(UsageFormat.duration(secondsToExhaustion))"
    }
}

enum UsagePresentationText {
    private static let compatibilityLabel = "Legacy compatibility source"
    private static let codedDonorPatterns = [
        #"(?i)claude_codexbar_[a-z0-9_]*"#,
        #"(?i)codexbar_[a-z0-9_]*"#,
    ]

    static func sourceLabel(
        kind: String?,
        label: String?,
        fallback: String = "Source unknown"
    ) -> String {
        let candidate = label?.trimmingCharacters(in: .whitespacesAndNewlines)
        let value = candidate?.isEmpty == false
            ? candidate!
            : (kind?.replacingOccurrences(of: "_", with: " ") ?? fallback)
        return containsDonorBranding(value) ? compatibilityLabel : value
    }

    static func warning(_ warning: String?) -> String? {
        guard let warning = warning?.trimmingCharacters(in: .whitespacesAndNewlines), !warning.isEmpty else {
            return nil
        }
        return neutralized(warning)
    }

    static func errorLabel(_ code: String) -> String {
        containsDonorBranding(code)
            ? "Legacy compatibility source unavailable"
            : code
    }

    static func neutralized(_ value: String) -> String {
        var result = value
        for pattern in codedDonorPatterns {
            result = result.replacingOccurrences(
                of: pattern,
                with: "legacy compatibility source",
                options: .regularExpression
            )
        }
        result = result
            .replacingOccurrences(of: "Codex Bar", with: "legacy compatibility source", options: .caseInsensitive)
            .replacingOccurrences(of: "CodexBar", with: "legacy compatibility source", options: .caseInsensitive)
        return result.replacingOccurrences(
            of: "legacy compatibility source source",
            with: "legacy compatibility source",
            options: .caseInsensitive
        )
    }

    private static func containsDonorBranding(_ value: String) -> Bool {
        value
            .lowercased()
            .components(separatedBy: CharacterSet.alphanumerics.inverted)
            .joined()
            .contains("codexbar")
    }
}

struct UsageQuotaGroup: Codable, Equatable, Identifiable, Sendable {
    let key: String?
    let label: String?
    let semantics: String?
    let windows: [UsageQuotaWindow]
    let runout: UsageRunout?

    enum CodingKeys: String, CodingKey {
        case key, label, semantics, windows, runout
    }

    init(key: String?, label: String?, semantics: String?, windows: [UsageQuotaWindow], runout: UsageRunout?) {
        self.key = Self.sanitizeKey(key)
        self.label = Self.sanitizeLabel(label)
        self.semantics = Self.sanitizeSemantics(semantics)
        self.windows = Array(windows.prefix(8))
        self.runout = runout
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        key = Self.sanitizeKey(try values.decodeIfPresent(String.self, forKey: .key))
        label = Self.sanitizeLabel(try values.decodeIfPresent(String.self, forKey: .label))
        semantics = Self.sanitizeSemantics(try values.decodeIfPresent(String.self, forKey: .semantics))
        windows = Array((try values.decodeIfPresent([UsageQuotaWindow].self, forKey: .windows) ?? []).prefix(8))
        runout = try values.decodeIfPresent(UsageRunout.self, forKey: .runout)
    }

    var id: String { [key ?? safeLabel, semantics ?? "quota"].joined(separator: ":") }

    var safeLabel: String { label ?? "Quota meter" }

    private static func sanitizeLabel(_ value: String?) -> String {
        let candidate = String((value ?? "Quota meter").prefix(80)).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !candidate.isEmpty,
              !candidate.contains("@"),
              !candidate.contains("/"),
              !candidate.contains("\\"),
              !candidate.contains("..")
        else { return "Quota meter" }
        return candidate
    }

    private static func sanitizeKey(_ value: String?) -> String? {
        guard let value else { return nil }
        let candidate = String(value.prefix(64))
        guard !candidate.isEmpty, candidate.allSatisfy({ $0.isLetter || $0.isNumber || "_-.".contains($0) }) else { return nil }
        return candidate
    }

    private static func sanitizeSemantics(_ value: String?) -> String? {
        guard let value, [
            "provider_quota_meter", "provider_rate_limit_group", "legacy_flat_quota_compatibility",
        ].contains(value) else { return nil }
        return value
    }
}


struct UsageResetCredit: Codable, Equatable, Identifiable, Sendable {
    let status: String?
    let count: Double?
    let semantics: String?
    let expiresAt: Date?

    enum CodingKeys: String, CodingKey {
        case status, count, semantics
        case expiresAt = "expires_at"
    }

    var id: String { "\(semantics ?? "unknown"):\(status ?? "unknown"):\(expiresAt?.timeIntervalSince1970 ?? 0)" }

    var isEarnedInventory: Bool {
        semantics == "earned_credit_inventory_not_current_reset_eligibility"
    }

    var sourceHonestLabel: String {
        if isEarnedInventory {
            return "Earned reset inventory: \(count?.formatted() ?? "unknown")"
        }
        return count.map { "\($0.formatted()) reset credits reported" }
            ?? "Reset credits reported"
    }
}

struct UsageRunout: Codable, Equatable, Sendable {
    let kind: String?
    let advisory: Bool?
    let estimatedExhaustsAt: Date?
    let secondsToExhaustion: Int?
    let basis: String?

    enum CodingKeys: String, CodingKey {
        case kind, advisory, basis
        case estimatedExhaustsAt = "estimated_exhausts_at"
        case secondsToExhaustion = "seconds_to_exhaustion"
    }

    var advisoryLabel: String {
        guard advisory == true else { return "No forecast" }
        guard let secondsToExhaustion else {
            return basis == "would_cross_reset_boundary"
                ? "Reset precedes projected runout"
                : "Runout forecast unavailable"
        }
        return "Advisory runout in \(UsageFormat.duration(secondsToExhaustion))"
    }
}

struct UsageHistory: Codable, Equatable, Sendable {
    let daily: [UsageDaily]
    let todayTotalTokens: Int64?
    let rolling7DTotalTokens: Int64?
    let calendarWeekTotalTokens: Int64?
    let allTimeTotalTokens: Int64?
    let semantics: String?
    let everObservedEnvelope: UsageTokenEnvelope?
    let providerReportedAccount: UsageProviderReportedHistory?

    enum CodingKeys: String, CodingKey {
        case daily, semantics
        case todayTotalTokens = "today_total_tokens"
        case rolling7DTotalTokens = "rolling_7d_total_tokens"
        case calendarWeekTotalTokens = "calendar_week_total_tokens"
        case allTimeTotalTokens = "all_time_total_tokens"
        case everObservedEnvelope = "ever_observed_envelope"
        case providerReportedAccount = "provider_reported_account"
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        daily = try values.decodeIfPresent([UsageDaily].self, forKey: .daily) ?? []
        todayTotalTokens = try values.decodeIfPresent(Int64.self, forKey: .todayTotalTokens)
        rolling7DTotalTokens = try values.decodeIfPresent(Int64.self, forKey: .rolling7DTotalTokens)
        calendarWeekTotalTokens = try values.decodeIfPresent(Int64.self, forKey: .calendarWeekTotalTokens)
        allTimeTotalTokens = try values.decodeIfPresent(Int64.self, forKey: .allTimeTotalTokens)
        semantics = try values.decodeIfPresent(String.self, forKey: .semantics)
        everObservedEnvelope = try values.decodeIfPresent(UsageTokenEnvelope.self, forKey: .everObservedEnvelope)
        providerReportedAccount = try values.decodeIfPresent(UsageProviderReportedHistory.self, forKey: .providerReportedAccount)
    }
}

struct UsageProviderReportedHistory: Codable, Equatable, Sendable {
    let daily: [UsageDaily]
    let todayTotalTokens: Int64?
    let rolling7DTotalTokens: Int64?
    let calendarWeekTotalTokens: Int64?
    let allTimeTotalTokens: Int64?
    let semantics: String?

    enum CodingKeys: String, CodingKey {
        case daily, semantics
        case todayTotalTokens = "today_total_tokens"
        case rolling7DTotalTokens = "rolling_7d_total_tokens"
        case calendarWeekTotalTokens = "calendar_week_total_tokens"
        case allTimeTotalTokens = "all_time_total_tokens"
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        daily = try values.decodeIfPresent([UsageDaily].self, forKey: .daily) ?? []
        todayTotalTokens = try values.decodeIfPresent(Int64.self, forKey: .todayTotalTokens)
        rolling7DTotalTokens = try values.decodeIfPresent(Int64.self, forKey: .rolling7DTotalTokens)
        calendarWeekTotalTokens = try values.decodeIfPresent(Int64.self, forKey: .calendarWeekTotalTokens)
        allTimeTotalTokens = try values.decodeIfPresent(Int64.self, forKey: .allTimeTotalTokens)
        semantics = try values.decodeIfPresent(String.self, forKey: .semantics)
    }
}

enum UsageHistoryPresentationKind: String, Equatable, Sendable {
    case canonical
    case providerReported
    case retainedEnvelope
}

struct UsageHistoryPresentation: Equatable, Sendable {
    let kind: UsageHistoryPresentationKind
    let label: String
    let provenance: String
    let daily: [UsageDaily]
    let todayTotalTokens: Int64?
    let rolling7DTotalTokens: Int64?
    let calendarWeekTotalTokens: Int64?
    let allTimeTotalTokens: Int64?
    let semantics: String?

    static func sources(for provider: UsageProvider) -> [Self] {
        guard let history = provider.history else { return [] }
        let retained: Self
        if provider.source?.canonical == true {
            retained = from(
                history,
                kind: .canonical,
                label: "Canonical retained history",
                provenance: "Canonical retained ledger; never merged with provider-reported totals."
            )
        } else {
            retained = from(
                history,
                kind: .retainedEnvelope,
                label: "Retained local envelope",
                provenance: "Locally retained observations; not provider billing or subscription credits."
            )
        }

        guard let reported = history.providerReportedAccount else { return [retained] }
        return [
            retained,
            from(
                reported,
                kind: .providerReported,
                label: "Provider-reported account",
                provenance: "Provider-reported account totals; displayed separately from retained observations."
            ),
        ]
    }

    private static func from(
        _ history: UsageHistory,
        kind: UsageHistoryPresentationKind,
        label: String,
        provenance: String
    ) -> Self {
        Self(
            kind: kind, label: label, provenance: provenance, daily: history.daily,
            todayTotalTokens: history.todayTotalTokens,
            rolling7DTotalTokens: history.rolling7DTotalTokens,
            calendarWeekTotalTokens: history.calendarWeekTotalTokens,
            allTimeTotalTokens: history.allTimeTotalTokens,
            semantics: history.semantics
        )
    }

    private static func from(
        _ history: UsageProviderReportedHistory,
        kind: UsageHistoryPresentationKind,
        label: String,
        provenance: String
    ) -> Self {
        Self(
            kind: kind, label: label, provenance: provenance, daily: history.daily,
            todayTotalTokens: history.todayTotalTokens,
            rolling7DTotalTokens: history.rolling7DTotalTokens,
            calendarWeekTotalTokens: history.calendarWeekTotalTokens,
            allTimeTotalTokens: history.allTimeTotalTokens,
            semantics: history.semantics
        )
    }
}

struct UsageDailyTrendPoint: Equatable, Sendable {
    let day: String
    let totalTokens: Int64
}

struct UsageDailyTrendProjection: Equatable, Sendable {
    let providerID: String
    let sourceKind: UsageHistoryPresentationKind
    let sourceLabel: String
    let points: [UsageDailyTrendPoint]

    static func make(
        providerID: String,
        history: UsageHistoryPresentation,
        limit: Int = 56
    ) -> Self {
        let points = history.daily
            .compactMap { row -> UsageDailyTrendPoint? in
                guard let total = row.totalTokens, total >= 0 else { return nil }
                return UsageDailyTrendPoint(day: row.date, totalTokens: total)
            }
            .sorted { $0.day < $1.day }
        return Self(
            providerID: providerID.lowercased(),
            sourceKind: history.kind,
            sourceLabel: history.label,
            points: Array(points.suffix(max(0, limit)))
        )
    }
}

struct UsageDailyCostTrendPoint: Equatable, Sendable {
    let day: String
    let nanos: Int64
    let costKind: String
    let currency: String?
}

struct UsageDailyCostTrendProjection: Equatable, Sendable {
    let providerID: String
    let sourceKind: UsageHistoryPresentationKind
    let sourceLabel: String
    let points: [UsageDailyCostTrendPoint]

    static func make(
        providerID: String,
        history: UsageHistoryPresentation,
        costs: UsageCosts?,
        limit: Int = 365
    ) -> Self {
        let points = history.daily
            .compactMap { row -> UsageDailyCostTrendPoint? in
                if let nanos = row.apiRateEstimateNanos, nanos >= 0 {
                    return UsageDailyCostTrendPoint(
                        day: row.date,
                        nanos: nanos,
                        costKind: "API-rate estimate",
                        currency: costs?.apiRateEstimate?.currency
                    )
                }
                if let nanos = row.providerNativeCostNanos, nanos >= 0 {
                    return UsageDailyCostTrendPoint(
                        day: row.date,
                        nanos: nanos,
                        costKind: "provider-native",
                        currency: costs?.providerNative?.currency
                    )
                }
                return nil
            }
            .sorted { $0.day < $1.day }
        return Self(
            providerID: providerID.lowercased(),
            sourceKind: history.kind,
            sourceLabel: history.label,
            points: Array(points.suffix(max(0, limit)))
        )
    }
}

struct UsageTokenEnvelope: Codable, Equatable, Sendable {
    let totalTokens: Int64?

    enum CodingKeys: String, CodingKey { case totalTokens = "total_tokens" }
}

struct UsageDaily: Codable, Equatable, Identifiable, Sendable {
    let date: String
    let totalTokens: Int64?
    let inputTokens: Int64?
    let outputTokens: Int64?
    let cacheReadTokens: Int64?
    let cacheCreate5MTokens: Int64?
    let cacheCreate1HTokens: Int64?
    let cacheCreateOtherTokens: Int64?
    let providerNativeCostNanos: Int64?
    let apiRateEstimateNanos: Int64?

    enum CodingKeys: String, CodingKey {
        case date
        case totalTokens = "total_tokens"
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case cacheReadTokens = "cache_read_tokens"
        case cacheCreate5MTokens = "cache_create_5m_tokens"
        case cacheCreate1HTokens = "cache_create_1h_tokens"
        case cacheCreateOtherTokens = "cache_create_other_tokens"
        case providerNativeCostNanos = "provider_native_cost_nanos"
        case apiRateEstimateNanos = "api_rate_estimate_nanos"
    }

    var id: String { date }
}

struct UsageCosts: Codable, Equatable, Sendable {
    let providerBilled: UsageCost?
    let providerNative: UsageCost?
    let apiRateEstimate: UsageCost?

    enum CodingKeys: String, CodingKey {
        case providerBilled = "provider_billed"
        case providerNative = "provider_native"
        case apiRateEstimate = "api_rate_estimate"
    }
}

private func normalizedUsageCurrency(_ value: String?) -> String? {
    guard let value else { return nil }
    let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
    guard trimmed.utf8.count <= 7 else { return nil }
    let code = trimmed.uppercased()
    guard code.utf8.count == 3,
          code.unicodeScalars.allSatisfy({ (65 ... 90).contains(Int($0.value)) })
    else { return nil }
    return code
}

struct UsageCost: Codable, Equatable, Sendable {
    let amountNanos: Int64?
    let currency: String?
    let byCurrency: [String: Int64]?
    let semantics: String?

    enum CodingKeys: String, CodingKey {
        case amountNanos = "amount_nanos"
        case currency, semantics
        case byCurrency = "by_currency"
    }

    private static let currencyLimit = 8

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        amountNanos = try values.decodeIfPresent(Int64.self, forKey: .amountNanos)
        currency = normalizedUsageCurrency(try values.decodeIfPresent(String.self, forKey: .currency))
        semantics = try values.decodeIfPresent(String.self, forKey: .semantics)

        let decoded = try values.decodeIfPresent([String: Int64].self, forKey: .byCurrency) ?? [:]
        var retained: [String: Int64] = [:]
        for (rawCurrency, amount) in decoded.sorted(by: { $0.key < $1.key }) {
            guard retained.count < Self.currencyLimit,
                  let code = normalizedUsageCurrency(rawCurrency),
                  retained[code] == nil
            else { continue }
            retained[code] = amount
        }
        byCurrency = retained.isEmpty ? nil : retained
    }
}

struct UsageActiveSessions: Codable, Equatable, Sendable {
    let status: String?
    let count: Int?
    let providers: [String]?
    let items: [UsageActiveSessionItem]?
}

struct UsageActiveSessionItem: Codable, Equatable, Identifiable, Sendable {
    let provider: String?
    let state: String?
    let startedAt: Date?
    let lastActivityAt: Date?
    let durationSeconds: Int?
    let idleSeconds: Int?

    enum CodingKeys: String, CodingKey {
        case provider, state
        case startedAt = "started_at"
        case lastActivityAt = "last_activity_at"
        case durationSeconds = "duration_seconds"
        case idleSeconds = "idle_seconds"
    }
    var id: String { "\(provider ?? "provider"):\(startedAt?.timeIntervalSince1970 ?? 0):\(state ?? "unknown")" }
}

enum UsageTransientRefreshRetry {
    static func resolve(
        warmingRetryDelay: TimeInterval,
        staleRefreshingRetryDelay: TimeInterval,
        retryLimit: Int,
        load: @Sendable () async throws -> UsageIntelligenceSnapshot,
        sleep: @Sendable (TimeInterval) async throws -> Void
    ) async throws -> UsageIntelligenceSnapshot {
        var snapshot = try await load()
        var retryCount = 0
        while let delay = retryDelay(
            for: snapshot,
            warmingRetryDelay: warmingRetryDelay,
            staleRefreshingRetryDelay: staleRefreshingRetryDelay
        ), retryCount < max(0, retryLimit) {
            retryCount += 1
            try await sleep(max(0, delay))
            snapshot = try await load()
        }
        return snapshot
    }

    private static func retryDelay(
        for snapshot: UsageIntelligenceSnapshot,
        warmingRetryDelay: TimeInterval,
        staleRefreshingRetryDelay: TimeInterval
    ) -> TimeInterval? {
        switch snapshot.refresh?.state?.lowercased() {
        case "stale_refreshing":
            return staleRefreshingRetryDelay
        case "warming" where snapshot.providers.isEmpty:
            return warmingRetryDelay
        default:
            return nil
        }
    }
}

struct UsageDashboardState: Equatable, Sendable {
    var snapshot: UsageIntelligenceSnapshot?
    var stale = false
    var refreshing = false
    var error: String?
    var lastGoodAt: Date?

    static func success(_ snapshot: UsageIntelligenceSnapshot, at date: Date) -> Self {
        Self(
            snapshot: snapshot,
            stale: snapshot.isProducerStale,
            refreshing: snapshot.refresh?.state == "stale_refreshing",
            error: nil,
            lastGoodAt: date
        )
    }

    static func accepting(
        _ snapshot: UsageIntelligenceSnapshot,
        preserving current: Self,
        at date: Date,
        grace: TimeInterval
    ) -> Self {
        let accepted = snapshot.retainingBoundedClaudeQuota(from: current.snapshot, at: date)
        guard accepted.hasMeaningfulProviderData else {
            return preserving(
                current,
                error: UsageIntelligenceSnapshotError.emptyOrErrorEnvelope,
                at: date,
                grace: grace
            )
        }
        return success(accepted, at: date)
    }

    static func preserving(_ current: Self, error: Error, at date: Date, grace: TimeInterval) -> Self {
        guard current.snapshot != nil else {
            return Self(snapshot: nil, stale: true, refreshing: false, error: error.localizedDescription, lastGoodAt: nil)
        }
        let age = current.lastGoodAt.map { date.timeIntervalSince($0) } ?? .infinity
        var preserved = current
        preserved.refreshing = age <= grace
        preserved.stale = age > grace
        preserved.error = age > grace ? error.localizedDescription : nil
        return preserved
    }
}

enum UsageIntelligenceSnapshotError: LocalizedError, Equatable {
    case emptyOrErrorEnvelope

    var errorDescription: String? {
        "Usage refresh returned no meaningful provider data."
    }
}

struct UsageProviderCardState: Equatable, Identifiable, Sendable {
    let id: String
    let provider: UsageProvider
    let observedDay: UsageCalendarDay?

    static func cards(from snapshot: UsageIntelligenceSnapshot?) -> [Self] {
        (snapshot?.providers ?? [:])
            .map { Self(id: $0.key, provider: $0.value, observedDay: utcDay(snapshot?.generatedAt)) }
            .sorted {
                let left = providerOrder($0.id)
                let right = providerOrder($1.id)
                if left != right { return left < right }
                return $0.id.localizedCaseInsensitiveCompare($1.id) == .orderedAscending
            }
    }

    private static func providerOrder(_ provider: String) -> Int {
        switch provider.lowercased() {
        case "claude": 0
        case "codex": 1
        default: 2
        }
    }

    private static func utcDay(_ date: Date?) -> UsageCalendarDay? {
        guard let date else { return nil }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        let values = calendar.dateComponents([.year, .month, .day], from: date)
        guard let year = values.year, let month = values.month, let day = values.day else { return nil }
        return UsageCalendarDay(String(format: "%04d-%02d-%02d", year, month, day))
    }
}

struct UsageCompactProviderSummary: Equatable, Identifiable, Sendable {
    let id: String
    let displayName: String
    let connectionLabel: String
    let connected: Bool?
    let quotaGroupLabel: String?
    let session: UsageQuotaWindow?
    let weekly: UsageQuotaWindow?
    let fable: UsageQuotaWindow?
    let todayTokens: Int64?
    let retainedUSDEstimateNanos: Int64?

    static func summaries(from snapshot: UsageIntelligenceSnapshot?) -> [Self] {
        UsageProviderCardState.cards(from: snapshot).map(summary)
    }

    static func summary(for card: UsageProviderCardState) -> Self {
        let provider = card.provider
        let connection = connectionPresentation(provider)
        let group = primaryQuotaGroup(for: provider)
        let fableGroup = fableQuotaGroup(for: provider)
        let windows = group.map { isFableGroup($0) ? [] : $0.windows } ?? provider.windows
        let estimate = provider.costs?.apiRateEstimate
        let retainedUSD = estimate?.byCurrency?["USD"]
            ?? (estimate?.currency?.uppercased() == "USD" ? estimate?.amountNanos : nil)
        return Self(
            id: card.id,
            displayName: card.id.capitalized,
            connectionLabel: connection.label,
            connected: connection.connected,
            quotaGroupLabel: group?.safeLabel,
            session: windows.first { $0.kind?.lowercased() == "session" },
            weekly: windows.first { $0.kind?.lowercased() == "weekly" },
            fable: fableGroup?.windows.first,
            todayTokens: provider.history?.todayTotalTokens,
            retainedUSDEstimateNanos: retainedUSD
        )
    }

    private static func primaryQuotaGroup(for provider: UsageProvider) -> UsageQuotaGroup? {
        guard !provider.quotaGroups.isEmpty else { return nil }
        return provider.quotaGroups.first { group in
            let identity = "\(group.key ?? "") \(group.safeLabel)".lowercased()
            return identity.contains("account")
        } ?? provider.quotaGroups.first { group in
            group.windows.contains { $0.kind?.lowercased() == "session" }
        } ?? provider.quotaGroups.first
    }

    private static func fableQuotaGroup(for provider: UsageProvider) -> UsageQuotaGroup? {
        provider.quotaGroups.first(where: isFableGroup)
    }

    private static func isFableGroup(_ group: UsageQuotaGroup) -> Bool {
        let identity = "\(group.key ?? "") \(group.safeLabel) \(group.windows.compactMap(\.name).joined(separator: " "))"
            .lowercased()
        return identity.contains("fable")
    }

    private static func connectionPresentation(_ provider: UsageProvider) -> (label: String, connected: Bool?) {
        let authenticated = provider.account?.authenticated
        if authenticated == true, provider.quotaSource?.canonical == false {
            return ("Fallback snapshot", nil)
        }
        return (connectionLabel(provider.account), authenticated)
    }

    private static func connectionLabel(_ account: UsageAccount?) -> String {
        guard let authenticated = account?.authenticated else {
            return "Connection unavailable"
        }
        return authenticated ? "Connected" : "Sign-in required"
    }
}

enum UsageStatusMode: String, CaseIterable, Equatable, Sendable {
    case bars = "quota_bars"
    case rings = "quota_rings"
    case minimal

    static func resolve(_ raw: String?) -> Self {
        switch raw?.lowercased() {
        case bars.rawValue, "bar", "bars": .bars
        case rings.rawValue, "ring", "rings", "eta_ring": .rings
        case minimal.rawValue, "count", "mode_glyph": .minimal
        default: .bars
        }
    }
}

enum UsageMetricMode: String, CaseIterable, Equatable, Sendable {
    case auto
    case weekly
    case session

    static func resolve(_ raw: String?) -> Self {
        Self(rawValue: raw?.lowercased() ?? "") ?? .auto
    }

    func selected(
        session: UsageQuotaWindow?,
        weekly: UsageQuotaWindow?,
        threshold: Double
    ) -> (label: String, window: UsageQuotaWindow)? {
        switch self {
        case .weekly:
            if let weekly { return ("Weekly", weekly) }
            if let session { return ("Session", session) }
        case .session:
            if let session { return ("Session", session) }
            if let weekly { return ("Weekly", weekly) }
        case .auto:
            if let session, let remaining = session.resolvedRemainingPercent,
               remaining <= min(100, max(0, threshold)) { return ("Session", session) }
            if let weekly { return ("Weekly", weekly) }
            if let session { return ("Session", session) }
        }
        return nil
    }
}

struct UsageStatusQuotaSelection: Equatable, Sendable {
    let provider: String
    let label: String
    let window: UsageQuotaWindow?
}

struct UsageStatusPresentation: Equatable, Sendable {
    let providers: [UsageCompactProviderSummary]
    let selections: [UsageStatusQuotaSelection]
    let stale: Bool
    let unavailable: Bool
    let showsUsed: Bool
    let warningMarkerPercent: Double?

    static func make(
        from state: UsageDashboardState,
        metricMode: String = UsageMetricMode.auto.rawValue,
        sessionThreshold: Double = 50,
        showUsed: Bool = false,
        warningMarkersVisible: Bool = true,
        warningThreshold: Double = 20
    ) -> Self {
        let summaries = UsageCompactProviderSummary.summaries(from: state.snapshot)
            .filter { ["claude", "codex"].contains($0.id.lowercased()) }
        let mode = UsageMetricMode.resolve(metricMode)
        let selections = summaries.map { provider -> UsageStatusQuotaSelection in
            let selected = mode.selected(
                session: provider.session,
                weekly: provider.weekly,
                threshold: sessionThreshold
            )
            return .init(provider: provider.id, label: selected?.label ?? "Quota", window: selected?.window)
        }
        return Self(
            providers: summaries,
            selections: selections,
            stale: state.stale || state.snapshot?.isProducerStale == true,
            unavailable: summaries.isEmpty,
            showsUsed: showUsed,
            warningMarkerPercent: warningMarkersVisible
                ? min(100, max(0, showUsed ? 100 - warningThreshold : warningThreshold))
                : nil
        )
    }

    func displayPercent(_ window: UsageQuotaWindow?) -> Double? {
        window?.resolvedRemainingPercent.map { showsUsed ? 100 - $0 : $0 }
    }

    var accessibilityLabel: String {
        if unavailable { return "Provider quota unavailable" }
        let providerCopy = providers.map { provider in
            let selected = selections.first { $0.provider.lowercased() == provider.id.lowercased() }
            return "\(provider.displayName): \(provider.connectionLabel), \(selected?.label.lowercased() ?? "quota") \(quotaCopy(selected?.window))"
        }.joined(separator: ". ")
        return stale ? "Provider quota stale. \(providerCopy)" : providerCopy
    }

    private func quotaCopy(_ window: UsageQuotaWindow?) -> String {
        guard let window else { return "unavailable" }
        let remaining = displayPercent(window).map { "\(Int($0.rounded())) percent \(showsUsed ? "used" : "remaining")" }
            ?? "quota unavailable"
        let reset = window.countdownSeconds.map { "resets in \(UsageFormat.duration($0))" }
            ?? "reset unavailable"
        return "\(remaining), \(reset)"
    }
}

struct UsageStatusTaskPresentation: Equatable, Sendable {
    let title: String
    let percent: Double?
    let eta: String?

    init(title: String, percent: Double?, eta: String?) {
        self.title = title
        self.percent = percent.map { min(max($0, 0), 100) }
        let cleanedETA = eta?.trimmingCharacters(in: .whitespacesAndNewlines)
        self.eta = cleanedETA?.isEmpty == false && cleanedETA != "—" && cleanedETA != "~"
            ? cleanedETA
            : nil
    }

    var compactLabel: String {
        let components = [
            percent.map { "\(Int($0.rounded()))%" },
            eta,
        ].compactMap { $0 }
        return components.isEmpty ? "Active" : components.joined(separator: " · ")
    }
}

enum UsageDashboardWidthClass: String, Equatable, Sendable {
    case compact
    case regular
    case wide
}

struct UsageDashboardLayout: Equatable, Sendable {
    let widthClass: UsageDashboardWidthClass
    let providerColumnCount: Int
    let historyColumnCount: Int
    let metricColumnCount: Int
    let costColumnCount: Int

    static func plan(forWidth width: Double, forceCompact: Bool = false) -> Self {
        let safeWidth = max(0, width)
        let widthClass: UsageDashboardWidthClass
        if forceCompact || safeWidth < 700 {
            widthClass = .compact
        } else if safeWidth < 1_100 {
            widthClass = .regular
        } else {
            widthClass = .wide
        }
        return Self(
            widthClass: widthClass,
            providerColumnCount: 1,
            historyColumnCount: 1,
            metricColumnCount: 2,
            costColumnCount: 1
        )
    }
}

enum UsageStatusLayout {
    static let imageHeight = 22.0
    static let activeTaskWidth = 60.0
    static let providerIconX = 1.0
    static let providerIconSize = 11.0
    static let quotaBarX = 14.0
    static let quotaPercentX = 35.0
    static let quotaBarWidth = 20.0
    static let percentWidth = 21.0

    static func baseWidth(mode: UsageStatusMode) -> Double {
        switch mode {
        case .bars: 56
        case .rings: 50
        case .minimal: 34
        }
    }

    static func imageWidth(mode: UsageStatusMode, hasActiveTask: Bool) -> Double {
        baseWidth(mode: mode) + (hasActiveTask ? activeTaskWidth : 0)
    }
}

enum UsageFormat {
    static func tokens(_ value: Int64?) -> String {
        guard let value else { return "Unknown" }
        let absolute = abs(Double(value))
        if absolute >= 1_000_000_000 { return String(format: "%.1fB", Double(value) / 1_000_000_000) }
        if absolute >= 1_000_000 { return String(format: "%.1fM", Double(value) / 1_000_000) }
        if absolute >= 1_000 { return String(format: "%.1fK", Double(value) / 1_000) }
        return value.formatted()
    }

    static func duration(_ seconds: Int?) -> String {
        guard let seconds, seconds >= 0 else { return "Unknown" }
        let safe = max(0, seconds)
        let days = safe / 86_400
        let hours = (safe % 86_400) / 3_600
        let minutes = (safe % 3_600) / 60
        if days > 0 { return "\(days)d \(hours)h" }
        if hours > 0 { return "\(hours)h \(minutes)m" }
        return "\(minutes)m"
    }

    static func timestamp(_ date: Date?) -> String {
        guard let date else { return "Unknown" }
        return date.formatted(date: .abbreviated, time: .shortened)
    }

    static func calendarDay(_ day: UsageCalendarDay?) -> String {
        day?.rawValue ?? "Unknown"
    }

    static func cost(_ cost: UsageCost?) -> String {
        guard let cost else { return "Unknown" }
        if let values = cost.byCurrency, !values.isEmpty {
            return values.keys.sorted().map { key in
                costNanos(values[key], currency: key)
            }.joined(separator: ", ")
        }
        return costNanos(cost.amountNanos, currency: cost.currency)
    }

    static func modelBreakdownNanos(_ value: Int64?) -> String {
        guard let value else { return "Unknown" }
        return "\(value.formatted()) nanos"
    }

    static func costNanos(_ value: Int64?, currency: String?) -> String {
        guard let value else { return "Unknown" }
        guard let code = normalizedUsageCurrency(currency) else {
            return "units \(decimalNanos(value))"
        }
        return "\(code) \(decimalNanos(value))"
    }

    private static func decimalNanos(_ value: Int64) -> String {
        let magnitude = value.magnitude
        var whole = magnitude / 1_000_000_000
        var hundredths = ((magnitude % 1_000_000_000) + 5_000_000) / 10_000_000
        if hundredths == 100 {
            whole += 1
            hundredths = 0
        }
        let sign = value < 0 ? "-" : ""
        return "\(sign)\(groupedDecimal(whole)).\(String(format: "%02d", Int(hundredths)))"
    }

    private static func groupedDecimal(_ value: UInt64) -> String {
        var output: [Character] = []
        for (index, character) in String(value).reversed().enumerated() {
            if index > 0, index.isMultiple(of: 3) {
                output.append(",")
            }
            output.append(character)
        }
        return String(output.reversed())
    }
}

enum UsagePeekFreshness: String, Equatable, Sendable {
    case live
    case stale
    case unavailable
}

struct UsageCompactQuotaTiming: Equatable, Sendable {
    let resetLabel: String
    let runoutLabel: String
    let accessibilityLabel: String

    static let unavailable = Self(
        resetLabel: "—",
        runoutLabel: "—",
        accessibilityLabel: "reset unavailable; run-out unavailable"
    )

    static func make(from window: UsageQuotaWindow?) -> Self {
        guard let window else { return .unavailable }
        let reset = window.countdownSeconds.flatMap(compactDuration)
        let runout = window.pace?.secondsToExhaustion.flatMap(compactDuration)
        let resetAccessibility = window.countdownSeconds.map {
            "resets in \(UsageFormat.duration($0))"
        } ?? "reset unavailable"
        let runoutAccessibility = window.pace?.secondsToExhaustion.map {
            "run-out in \(UsageFormat.duration($0))"
        } ?? "run-out unavailable"
        return Self(
            resetLabel: reset ?? "—",
            runoutLabel: runout ?? "—",
            accessibilityLabel: "\(resetAccessibility); \(runoutAccessibility)"
        )
    }

    private static func compactDuration(_ seconds: Int) -> String? {
        guard seconds >= 0 else { return nil }
        if seconds >= 86_400 {
            let days = seconds / 86_400
            let hours = (seconds % 86_400) / 3_600
            return hours > 0 ? "\(days)d \(hours)h" : "\(days)d"
        }
        if seconds >= 3_600 {
            let hours = seconds / 3_600
            let minutes = (seconds % 3_600) / 60
            return minutes > 0 ? "\(hours)h \(minutes)m" : "\(hours)h"
        }
        return "\(seconds / 60)m"
    }
}

struct UsagePopoverPeekProvider: Equatable, Identifiable, Sendable {
    let id: String
    let displayName: String
    let connectionLabel: String
    let connected: Bool?
    let hasSession: Bool
    let hasWeekly: Bool
    let sessionRemainingPercent: Double?
    let weeklyRemainingPercent: Double?
    let hasFable: Bool
    let fableRemainingPercent: Double?
    let sessionTiming: UsageCompactQuotaTiming
    let weeklyTiming: UsageCompactQuotaTiming
    let fableTiming: UsageCompactQuotaTiming
    let todayTokens: Int64?
    let retainedUSDEstimateNanos: Int64?
}

struct UsagePopoverPeekPresentation: Equatable, Sendable {
    let freshness: UsagePeekFreshness
    let providers: [UsagePopoverPeekProvider]

    static func make(
        from state: UsageDashboardState,
        showResetETA: Bool = true,
        showRunoutETA: Bool = true,
        showUsed: Bool = false
    ) -> Self {
        let summaries = Dictionary(
            uniqueKeysWithValues: UsageCompactProviderSummary.summaries(from: state.snapshot)
                .map { ($0.id.lowercased(), $0) }
        )
        let providers = ["claude", "codex"].map { identity -> UsagePopoverPeekProvider in
            guard let summary = summaries[identity] else {
                return UsagePopoverPeekProvider(
                    id: identity,
                    displayName: identity.capitalized,
                    connectionLabel: "Unavailable",
                    connected: nil,
                    hasSession: false,
                    hasWeekly: false,
                    sessionRemainingPercent: nil,
                    weeklyRemainingPercent: nil,
                    hasFable: false,
                    fableRemainingPercent: nil,
                    sessionTiming: .unavailable,
                    weeklyTiming: .unavailable,
                    fableTiming: .unavailable,
                    todayTokens: nil,
                    retainedUSDEstimateNanos: nil
                )
            }
            return UsagePopoverPeekProvider(
                id: identity,
                displayName: summary.displayName,
                connectionLabel: summary.connectionLabel,
                connected: summary.connected,
                hasSession: summary.session != nil,
                hasWeekly: summary.weekly != nil,
                sessionRemainingPercent: summary.session?.resolvedRemainingPercent.map { showUsed ? 100 - $0 : $0 },
                weeklyRemainingPercent: summary.weekly?.resolvedRemainingPercent.map { showUsed ? 100 - $0 : $0 },
                hasFable: summary.fable != nil,
                fableRemainingPercent: summary.fable?.resolvedRemainingPercent.map { showUsed ? 100 - $0 : $0 },
                sessionTiming: .make(from: summary.session).filtered(reset: showResetETA, runout: showRunoutETA),
                weeklyTiming: .make(from: summary.weekly).filtered(reset: showResetETA, runout: showRunoutETA),
                fableTiming: .make(from: summary.fable).filtered(reset: showResetETA, runout: showRunoutETA),
                todayTokens: summary.todayTokens,
                retainedUSDEstimateNanos: summary.retainedUSDEstimateNanos
            )
        }
        let freshness: UsagePeekFreshness
        if state.snapshot == nil {
            freshness = .unavailable
        } else if state.stale || state.snapshot?.isProducerStale == true {
            freshness = .stale
        } else {
            freshness = .live
        }
        return Self(freshness: freshness, providers: providers)
    }

    var accessibilityLabel: String {
        let prefix = switch freshness {
        case .live: "Provider usage live."
        case .stale: "Provider usage stale."
        case .unavailable: "Provider usage unavailable."
        }
        let details = providers.map { provider in
            let session = provider.sessionRemainingPercent.map { "\(Int($0.rounded())) percent remaining" }
                ?? "unavailable"
            let weekly = provider.weeklyRemainingPercent.map { "\(Int($0.rounded())) percent remaining" }
                ?? "unavailable"
            let fable = provider.fableRemainingPercent.map { "\(Int($0.rounded())) percent remaining" }
                ?? "unavailable"
            let today = provider.todayTokens.map { "\($0) retained tokens today" } ?? "today tokens unavailable"
            let retainedUSD = provider.retainedUSDEstimateNanos
                .map { String(format: "USD %.2f retained estimate", Double($0) / 1_000_000_000) } ?? "retained USD estimate unavailable"
            return "\(provider.displayName): \(provider.connectionLabel), session \(session), \(provider.sessionTiming.accessibilityLabel), weekly \(weekly), \(provider.weeklyTiming.accessibilityLabel), Fable \(fable), \(provider.fableTiming.accessibilityLabel), \(today), \(retainedUSD)"
        }.joined(separator: ". ")
        return "\(prefix) \(details)"
    }
}

private extension UsageCompactQuotaTiming {
    func filtered(reset: Bool, runout: Bool) -> Self {
        Self(
            resetLabel: reset ? resetLabel : "",
            runoutLabel: runout ? runoutLabel : "",
            accessibilityLabel: accessibilityLabel
        )
    }
}
