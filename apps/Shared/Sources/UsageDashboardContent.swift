import SwiftUI

struct UsageCompactBoardStrip: View {
    let state: UsageDashboardState
    let systemTelemetry: SystemTelemetrySnapshot?
    var showSystemTelemetry = true
    var showDisk = true
    var barPalette: UsageBarPalette = .colored
    var onOpenDetails: (() -> Void)?
    @AppStorage("coord.cockpit.usage-strip-expanded") private var expanded = false

    private var providers: [UsageCompactProviderSummary] {
        UsageCompactProviderSummary.summaries(from: state.snapshot)
            .filter { ["claude", "codex"].contains($0.id.lowercased()) }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Button {
                    withAnimation(.easeInOut(duration: 0.16)) { expanded.toggle() }
                } label: {
                    HStack(spacing: 5) {
                        Image(systemName: expanded ? "chevron.down" : "chevron.right")
                            .font(.system(size: 8, weight: .bold))
                        Text("USAGE")
                            .font(.system(size: 9, weight: .bold))
                            .tracking(1.05)
                    }
                    .foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
                .accessibilityLabel(expanded ? "Collapse usage and system stats" : "Expand usage and system stats")

                if providers.isEmpty {
                    Text("Provider quota unavailable")
                        .font(.caption.weight(.medium))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                } else {
                    ForEach(providers) { provider in
                        compactProvider(provider)
                        if provider.id != providers.last?.id {
                            Divider().frame(height: 18).opacity(0.28)
                        }
                    }
                }
                if showSystemTelemetry {
                    Divider().frame(height: 18).opacity(0.28)
                    SystemTelemetryStrip(
                        snapshot: systemTelemetry, showDisk: showDisk, embedded: true
                    )
                }

                Spacer(minLength: 4)
                if state.refreshing { ProgressView().controlSize(.mini) }
                if let onOpenDetails {
                    Button(action: onOpenDetails) {
                        Image(systemName: "gauge.with.dots.needle.33percent")
                            .font(.system(size: 10, weight: .semibold))
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(.secondary)
                    .help("Open full usage details")
                }
            }
            .padding(.horizontal, 11)
            .frame(height: 38)

            if expanded {
                Divider().opacity(0.24)
                VStack(alignment: .leading, spacing: 7) {
                    if !providers.isEmpty {
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: 230), spacing: 12, alignment: .topLeading)], alignment: .leading, spacing: 8) {
                            ForEach(providers) { provider in
                                expandedProvider(provider)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                        }
                    }
                    if showSystemTelemetry {
                        if !providers.isEmpty {
                            Divider().opacity(0.22)
                        }
                        SystemTelemetryStrip(
                            snapshot: systemTelemetry,
                            expanded: true,
                            showDisk: showDisk,
                            embedded: true
                        )
                    }
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
                .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(Color.primary.opacity(0.09), lineWidth: 1)
        }
    }

    private func compactWindows(_ provider: UsageCompactProviderSummary) -> [(String, UsageQuotaWindow)] {
        var windows: [(String, UsageQuotaWindow)] = []
        if let session = provider.session { windows.append(("S", session)) }
        if let weekly = provider.weekly { windows.append(("W", weekly)) }
        if let fable = provider.fable { windows.append(("F", fable)) }
        return windows
    }

    private func tint(_ provider: UsageCompactProviderSummary) -> Color {
        provider.id.lowercased() == "claude"
            ? .orange
            : Color(red: 0.64, green: 0.43, blue: 0.96)
    }

    private func quotaTint(_ providerColor: Color) -> Color {
        barPalette == .colored ? providerColor : Color.primary.opacity(0.82)
    }

    private func compactProvider(_ provider: UsageCompactProviderSummary) -> some View {
        let windows = compactWindows(provider)
        let color = tint(provider)
        let barColor = quotaTint(color)
        return HStack(spacing: 6) {
            Image(provider.id.lowercased() == "claude" ? "claude-menu" : "codex-menu")
                .resizable()
                .renderingMode(.template)
                .foregroundStyle(color)
                .scaledToFit()
                .frame(width: 14, height: 14)
            if windows.isEmpty {
                Text("—")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(.secondary)
            } else {
                ForEach(windows, id: \.0) { label, window in
                    HStack(spacing: 2) {
                        Text(label)
                            .font(.system(size: 8, weight: .semibold))
                            .foregroundStyle(.secondary)
                        Text(window.resolvedRemainingPercent.map { "\(Int($0.rounded()))%" } ?? "N/A")
                            .font(.system(size: 10, weight: .bold).monospacedDigit())
                            .foregroundStyle(window.resolvedRemainingPercent == nil ? Color.secondary : barColor)
                    }
                }
            }
        }
        .accessibilityElement(children: .combine)
    }

    private func expandedProvider(_ provider: UsageCompactProviderSummary) -> some View {
        let color = tint(provider)
        let barColor = quotaTint(color)
        let windows: [(String, UsageQuotaWindow?)] = [
            ("Session", provider.session), ("Weekly", provider.weekly), ("Fable", provider.fable),
        ]
        return VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 6) {
                Image(provider.id.lowercased() == "claude" ? "claude-menu" : "codex-menu")
                    .resizable().renderingMode(.template).foregroundStyle(color).scaledToFit()
                    .frame(width: 14, height: 14)
                Text(provider.displayName).font(.caption.weight(.bold))
                Spacer(minLength: 4)
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text("Cost")
                        .font(.system(size: 8.5, weight: .regular))
                    Text(UsageFormat.costNanos(provider.retainedUSDEstimateNanos, currency: "USD"))
                        .font(.system(size: 8.5, weight: .regular).monospacedDigit())
                }
                .foregroundStyle(.secondary)
                .accessibilityElement(children: .combine)
                .accessibilityLabel("Cost \(UsageFormat.costNanos(provider.retainedUSDEstimateNanos, currency: "USD"))")
            }
            ForEach(windows, id: \.0) { label, window in
                if let window {
                    HStack(spacing: 7) {
                        Text(label).frame(width: 46, alignment: .leading)
                        ProgressView(value: window.resolvedRemainingPercent ?? 0, total: 100).tint(barColor)
                        Text(window.resolvedRemainingPercent.map { "\(Int($0.rounded()))%" } ?? "N/A")
                            .fontWeight(.bold)
                            .foregroundStyle(window.resolvedRemainingPercent == nil ? Color.secondary : barColor)
                            .frame(width: 34, alignment: .trailing)
                        Text("↻ \(duration(window.countdownSeconds))")
                        if window.pace?.secondsToExhaustion != nil {
                            Text("out \(duration(window.pace?.secondsToExhaustion))")
                        }
                    }
                    .font(.system(size: 9.5).monospacedDigit())
                    .foregroundStyle(.secondary)
                }
            }
        }
    }

    private func duration(_ seconds: Int?) -> String {
        seconds.map(UsageFormat.duration) ?? "—"
    }
}

struct UsageDashboardContent: View {
    let state: UsageDashboardState
    var forceCompact = false
    var onClose: (() -> Void)?
    var onOpenSettings: (() -> Void)?

    private var cards: [UsageProviderCardState] {
        UsageProviderCardState.cards(from: state.snapshot)
    }

    private var dailyTrends: [UsageDailyCostTrendProjection] {
        cards.flatMap { card in
            UsageHistoryPresentation.sources(for: card.provider).map { history in
                UsageDailyCostTrendProjection.make(
                    providerID: card.id,
                    history: history,
                    costs: card.provider.costs
                )
            }
            .filter { $0.points.count >= 2 }
        }
    }

    var body: some View {
        GeometryReader { proxy in
            let layout = UsageDashboardLayout.plan(
                forWidth: Double(proxy.size.width),
                forceCompact: forceCompact
            )
            VStack(spacing: 0) {
                UsageDashboardHeader(
                    state: state,
                    layout: layout,
                    onClose: onClose,
                    onOpenSettings: onOpenSettings
                )
                .padding(.horizontal, layout.widthClass == .compact ? 14 : 22)
                .padding(.top, layout.widthClass == .compact ? 14 : 22)
                .padding(.bottom, 10)

                Divider().opacity(0.28)

                ScrollView {
                    LazyVStack(alignment: .leading, spacing: sectionSpacing(for: layout)) {
                        if cards.isEmpty {
                            ContentUnavailableView(
                                "Provider usage unavailable",
                                systemImage: "gauge.with.dots.needle.33percent"
                            )
                            .frame(maxWidth: .infinity, minHeight: 280)
                        } else {
                            LazyVGrid(
                                columns: usageColumns(layout.providerColumnCount, spacing: sectionSpacing(for: layout)),
                                alignment: .leading,
                                spacing: sectionSpacing(for: layout)
                            ) {
                                ForEach(cards) { card in
                                    UsageProviderCard(card: card, layout: layout)
                                }
                            }

                            UsageDailyTrendOverview(
                                projections: dailyTrends,
                                layout: layout
                            )
                        }
                    }
                    .padding(layout.widthClass == .compact ? 14 : 22)
                }
            }
            .background(Color.clear)
        }
    }

    private func sectionSpacing(for layout: UsageDashboardLayout) -> CGFloat {
        layout.widthClass == .compact ? 14 : 18
    }
}

private struct UsageDailyTrendOverview: View {
    let projections: [UsageDailyCostTrendProjection]
    let layout: UsageDashboardLayout

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            UsageSectionTitle(
                title: "Daily estimated cost",
                detail: "Dollars per observed day. Zero-baseline bars keep providers and evidence sources separate.",
                symbol: "chart.bar.fill"
            )

            if projections.isEmpty {
                Text("Daily cost history unavailable.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                LazyVGrid(
                    columns: usageColumns(layout.historyColumnCount, spacing: 10),
                    alignment: .leading,
                    spacing: 10
                ) {
                    ForEach(Array(projections.enumerated()), id: \.offset) { _, projection in
                        VStack(alignment: .leading, spacing: 7) {
                            Text(projection.providerID.capitalized)
                                .font(.callout.weight(.bold))
                            UsageDailyGraph(projection: projection)
                        }
                        .padding(12)
                        .frame(maxWidth: .infinity, alignment: .topLeading)
                        .background(Color.clear)
                        .overlay {
                            RoundedRectangle(cornerRadius: 12, style: .continuous)
                                .stroke(Color.primary.opacity(0.10), lineWidth: 1)
                        }
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
    }
}

private struct UsageDashboardHeader: View {
    let state: UsageDashboardState
    let layout: UsageDashboardLayout
    let onClose: (() -> Void)?
    let onOpenSettings: (() -> Void)?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .center, spacing: 10) {
                Text("Provider usage")
                    .font(layout.widthClass == .compact ? .title2.bold() : .largeTitle.bold())
                Spacer(minLength: 8)
                if state.refreshing {
                    ProgressView().controlSize(.small)
                }
                if state.stale {
                    Label("Last good", systemImage: "clock.badge.exclamationmark")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.orange)
                }
                if let onClose {
                    Button(action: onClose) {
                        Image(systemName: "xmark.circle.fill")
                            .font(.title3)
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(.secondary)
                    .accessibilityLabel("Close usage")
                }
            }


            if let onOpenSettings {
                Button(action: onOpenSettings) {
                    Label("Account / Settings", systemImage: "person.crop.circle.badge.gearshape")
                        .font(.callout.weight(.semibold))
                }
                .buttonStyle(.bordered)
                .help("Connect or refresh Codex and Claude provider accounts.")
            }

            HStack(spacing: 8) {
                if let generated = state.snapshot?.generatedAt {
                    Text("Generated \(UsageFormat.timestamp(generated))")
                }
                if let error = state.error {
                    Text(error).foregroundStyle(.orange)
                }
            }
            .font(.caption)
            .foregroundStyle(.tertiary)
        }
    }
}

private struct UsageProviderCard: View {
    let card: UsageProviderCardState
    let layout: UsageDashboardLayout
    @State private var showingDetails = false

    private var provider: UsageProvider { card.provider }
    private var compactSummary: UsageCompactProviderSummary {
        UsageCompactProviderSummary.summary(for: card)
    }
    private var latestDailyCost: (nanos: Int64?, currency: String?) {
        let row = provider.history?.daily
            .sorted { $0.date < $1.date }
            .last(where: { $0.apiRateEstimateNanos != nil || $0.providerNativeCostNanos != nil })
        if let nanos = row?.apiRateEstimateNanos {
            return (nanos, provider.costs?.apiRateEstimate?.currency)
        }
        if let nanos = row?.providerNativeCostNanos {
            return (nanos, provider.costs?.providerNative?.currency)
        }
        return (nil, nil)
    }
    private var histories: [UsageHistoryPresentation] {
        UsageHistoryPresentation.sources(for: provider)
    }
    private var quotaGroups: [UsageQuotaGroup] {
        if !provider.quotaGroups.isEmpty { return provider.quotaGroups }
        guard !provider.windows.isEmpty else { return [] }
        return [UsageQuotaGroup(
            key: "legacy-flat-quota",
            label: "\(card.id.capitalized) quota",
            semantics: "legacy_flat_quota_compatibility",
            windows: provider.windows,
            runout: provider.runout
        )]
    }
    private var sourceLabel: String {
        if provider.source?.canonical == true { return "Canonical retained source" }
        if let source = provider.source {
            return "Retained source · \(source.displayLabel)"
        }
        return "Source unknown"
    }

    private func topCostModel(apiEstimate: Bool) -> String? {
        let items = provider.breakdowns?.models?.items ?? []
        return items
            .filter { (apiEstimate ? $0.apiRateEstimateNanos : $0.providerNativeCostNanos) != nil }
            .max { left, right in
                let leftCost = apiEstimate ? left.apiRateEstimateNanos : left.providerNativeCostNanos
                let rightCost = apiEstimate ? right.apiRateEstimateNanos : right.providerNativeCostNanos
                if leftCost != rightCost { return (leftCost ?? 0) < (rightCost ?? 0) }
                if left.totalTokens != right.totalTokens { return (left.totalTokens ?? 0) < (right.totalTokens ?? 0) }
                return (left.label ?? left.key ?? "") > (right.label ?? right.key ?? "")
            }
            .flatMap { $0.label ?? $0.key }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: layout.widthClass == .compact ? 14 : 17) {
            providerHeader
            compactQuotaSection
            compactMetrics
            DisclosureGroup("Details, provenance & history", isExpanded: $showingDetails) {
                VStack(alignment: .leading, spacing: layout.widthClass == .compact ? 14 : 17) {
                    detailedProviderIdentity
                    quotaSection
                    Divider()
                    historySection

                    if let models = provider.breakdowns?.models {
                        Divider()
                        UsageModelBreakdownSection(breakdown: models, providerNativeCurrency: provider.costs?.providerNative?.currency, apiCurrency: provider.costs?.apiRateEstimate?.currency)
                    }
                    if let projects = provider.breakdowns?.projects {
                        Divider()
                        UsageProjectBreakdownSection(breakdown: projects)
                    }

                    Divider()
                    costSection
                    operationalDetails
                    sourceNotes
                }
                .padding(.top, 8)
            }
            .font(.callout.weight(.semibold))
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .padding(layout.widthClass == .compact ? 14 : 18)
        .background(Color.clear)
        .overlay {
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .stroke(Color.primary.opacity(0.12), lineWidth: 1)
        }
    }

    private var providerHeader: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(compactSummary.displayName).font(.title2.bold())
            Spacer(minLength: 8)
            Text(compactSummary.connectionLabel)
                .font(.caption.weight(.semibold))
                .foregroundStyle(compactSummary.connected == true ? .green : .secondary)
        }
    }

    private var detailedProviderIdentity: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(sourceLabel)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(Color.primary.opacity(0.07), in: Capsule())
            Label(provider.account?.redactedDisplay ?? "Account unknown", systemImage: "person.crop.circle")
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var compactQuotaSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let group = compactSummary.quotaGroupLabel {
                Text(group)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
            }
            UsageCompactQuotaRow(label: "Session", window: compactSummary.session)
            UsageCompactQuotaRow(label: "Weekly", window: compactSummary.weekly)
        }
    }

    private var compactMetrics: some View {
        HStack(alignment: .top, spacing: 10) {
            UsageCompactMetric(
                label: "Today Est.",
                value: UsageFormat.costNanos(latestDailyCost.nanos, currency: latestDailyCost.currency),
                detail: "Daily cost estimate · not billed"
            )
            UsageCompactMetric(
                label: "Cost",
                value: UsageFormat.costNanos(
                    compactSummary.retainedUSDEstimateNanos,
                    currency: "USD"
                ),
                detail: "Cumulative API-rate estimate · not billed"
            )
        }
    }

    private var quotaSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            UsageSectionTitle(
                title: "Live quota windows",
                detail: "Bars show quota progress toward a reset, not token history.",
                symbol: "gauge.with.needle"
            )
            if let quotaSource = provider.quotaSource {
                VStack(alignment: .leading, spacing: 2) {
                    Text("\(quotaSource.displayLabel) · \(quotaSource.authorityLabel)")
                        .font(.caption.weight(.semibold))
                    if let warning = quotaSource.displayWarning, !warning.isEmpty {
                        Text(warning).foregroundStyle(.orange)
                    }
                    Text("Remaining percentage is a local display transform of provider-reported usage.")
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            }
            if quotaGroups.isEmpty {
                Text("Live quota and reset windows unavailable.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(quotaGroups) { group in
                    VStack(alignment: .leading, spacing: 9) {
                        Text(group.safeLabel)
                            .font(.callout.weight(.bold))
                            .fixedSize(horizontal: false, vertical: true)
                        if let semantics = group.semantics {
                            Text(humanized(semantics))
                                .font(.caption2)
                                .foregroundStyle(.tertiary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        ForEach(group.windows) { window in
                            UsageQuotaRow(window: window)
                        }
                        if group.windows.allSatisfy({ $0.pace == nil }), let runout = group.runout, runout.advisory == true {
                            Label(runout.advisoryLabel, systemImage: "exclamationmark.triangle")
                                .font(.caption)
                                .foregroundStyle(.orange)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .padding(10)
                    .background(Color.clear)
                    .overlay {
                        RoundedRectangle(cornerRadius: 12)
                            .stroke(Color.primary.opacity(0.10), lineWidth: 1)
                    }
                }
            }
        }
    }

    private var historySection: some View {
        VStack(alignment: .leading, spacing: 10) {
            UsageSectionTitle(
                title: "Usage and daily cost",
                detail: "Token totals remain as metrics; charts show dollars per observed day. Sources are never merged.",
                symbol: "chart.bar.fill"
            )
            if histories.isEmpty {
                Text("Usage history unavailable.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                LazyVGrid(
                    columns: usageColumns(layout.historyColumnCount, spacing: 10),
                    alignment: .leading,
                    spacing: 10
                ) {
                    ForEach(histories, id: \.kind) { history in
                        UsageHistoryPanel(
                            history: history,
                            envelopeTotal: history.kind == .providerReported
                                ? nil
                                : provider.history?.everObservedEnvelope?.totalTokens,
                            metricColumnCount: layout.metricColumnCount,
                            providerName: card.id,
                            observedDay: card.observedDay,
                            apiCurrency: provider.costs?.apiRateEstimate?.currency,
                            providerNativeCurrency: provider.costs?.providerNative?.currency,
                            topAPIEstimateModel: history.kind == .providerReported ? nil : topCostModel(apiEstimate: true),
                            topProviderNativeModel: history.kind == .providerReported ? nil : topCostModel(apiEstimate: false)
                        )
                    }
                }
            }
        }
    }

    private var costSection: some View {
        VStack(alignment: .leading, spacing: 9) {
            UsageSectionTitle(
                title: "Cost semantics",
                detail: "Invoice, provider-native, and API-rate values are different evidence classes.",
                symbol: "banknote"
            )
            VStack(spacing: 8) {
                UsageCostMetric(
                    label: "Provider billed",
                    value: UsageFormat.cost(provider.costs?.providerBilled),
                    detail: "Invoice or independent source only"
                )
                UsageCostMetric(
                    label: "Provider native",
                    value: UsageFormat.cost(provider.costs?.providerNative),
                    detail: "Provider-reported semantics; not an invoice unless declared"
                )
                UsageCostMetric(
                    label: "API-rate estimate",
                    value: UsageFormat.cost(provider.costs?.apiRateEstimate),
                    detail: "Non-billed estimate; not subscription spend"
                )
            }
        }
    }

    @ViewBuilder
    private var operationalDetails: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(sessionLabel, systemImage: "person.2")
            Label(resetCreditLabel, systemImage: "arrow.counterclockwise.circle")
        }
        .font(.callout)
        .foregroundStyle(.secondary)

        if !provider.resetCredits.isEmpty {
            VStack(alignment: .leading, spacing: 5) {
                Text("Reset inventory & eligibility").font(.callout.weight(.semibold))
                ForEach(provider.resetCredits) { credit in
                    Text("\(credit.sourceHonestLabel)\(credit.expiresAt.map { " · expires \(UsageFormat.timestamp($0))" } ?? "")")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if provider.resetCredits.contains(where: \.isEarnedInventory) {
                    Text("Current reset eligibility unverified")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }

        if let sessions = provider.activeSessions?.items, !sessions.isEmpty {
            VStack(alignment: .leading, spacing: 7) {
                Text("Sanitized active sessions").font(.callout.weight(.semibold))
                ForEach(sessions) { session in
                    VStack(alignment: .leading, spacing: 3) {
                        Text("\(session.provider?.capitalized ?? card.id.capitalized) · \(humanized(session.state ?? "state unknown"))")
                            .font(.callout.weight(.semibold))
                        Text("Active \(UsageFormat.duration(session.durationSeconds)) · idle \(UsageFormat.duration(session.idleSeconds))")
                        Text("Started \(UsageFormat.timestamp(session.startedAt)) · last activity \(UsageFormat.timestamp(session.lastActivityAt))")
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    @ViewBuilder
    private var sourceNotes: some View {
        let warning = provider.source?.displayWarning
        if warning?.isEmpty == false || !provider.errors.isEmpty {
            DisclosureGroup("Source notes") {
                VStack(alignment: .leading, spacing: 5) {
                    if let warning, !warning.isEmpty {
                        Text(warning)
                    }
                    if !provider.errors.isEmpty {
                        Text(provider.errors.map(\.displayLabel).joined(separator: " · "))
                            .foregroundStyle(.orange)
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 5)
            }
            .font(.callout.weight(.semibold))
        }
    }

    private var sessionLabel: String {
        let status = provider.activeSessions?.status
        guard status == "ok" || status == "available", let count = provider.activeSessions?.count else {
            return "Active sessions unknown"
        }
        return "\(count) active session\(count == 1 ? "" : "s")"
    }

    private var resetCreditLabel: String {
        guard !provider.resetCredits.isEmpty else { return "Reset credits unknown or none" }
        let earnedInventory = provider.resetCredits.filter(\.isEarnedInventory)
        if !earnedInventory.isEmpty {
            let total = earnedInventory.compactMap(\.count).reduce(0, +)
            return "Earned reset inventory: \(total.formatted()) · eligibility unverified"
        }
        let total = provider.resetCredits.compactMap(\.count).reduce(0, +)
        return total > 0 ? "\(total.formatted()) reset credits reported" : "Reset credits reported"
    }
}

private struct UsageCompactQuotaRow: View {
    let label: String
    let window: UsageQuotaWindow?

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 8) {
                Text(label).font(.callout.weight(.semibold))
                Spacer(minLength: 8)
                Text(window?.resolvedRemainingPercent.map { "\(Int($0.rounded()))% left" } ?? "Unavailable")
                    .font(.callout.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            if let remaining = window?.clampedRemainingFraction {
                ProgressView(value: remaining)
                    .progressViewStyle(.linear)
            }
            Text(resetLabel)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .combine)
    }

    private var resetLabel: String {
        if let seconds = window?.countdownSeconds {
            return "Resets in \(UsageFormat.duration(seconds))"
        }
        if let date = window?.resetsAt {
            return "Resets \(UsageFormat.timestamp(date))"
        }
        return "Reset unavailable"
    }
}

private struct UsageCompactMetric: View {
    let label: String
    let value: String
    let detail: String

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            Text(label).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.callout.weight(.medium).monospacedDigit())
        }
        .accessibilityElement(children: .combine)
        .accessibilityHint(detail)
        .help(detail)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .overlay {
            RoundedRectangle(cornerRadius: 10)
                .stroke(Color.primary.opacity(0.10), lineWidth: 1)
        }
    }
}

private struct UsageSectionTitle: View {
    let title: String
    let detail: String
    let symbol: String

    var body: some View {
        Label(title, systemImage: symbol).font(.headline)
            .accessibilityHint(detail)
            .help(detail)
    }
}

private struct UsageQuotaRow: View {
    let window: UsageQuotaWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(window.displayName)
                    .font(.callout.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8)
                Text(window.resolvedRemainingPercent.map { "\(Int($0.rounded()))% left" } ?? "Unknown")
                    .font(.callout.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            if let remaining = window.clampedRemainingFraction {
                ProgressView(value: remaining)
                    .progressViewStyle(.linear)
                    .tint(quotaTint)
                    .accessibilityLabel("\(window.displayName) quota remaining")
                    .accessibilityValue(
                        window.resolvedRemainingPercent.map {
                            "\(Int($0.rounded())) percent remaining"
                        } ?? "Unknown"
                    )
            } else {
                Capsule()
                    .fill(Color.secondary.opacity(0.18))
                    .frame(height: 4)
                    .accessibilityLabel("\(window.displayName) quota remaining unknown")
            }
            HStack(spacing: 8) {
                if let countdown = window.countdownSeconds {
                    Label("Resets in \(UsageFormat.duration(countdown))", systemImage: "arrow.clockwise")
                } else if let resetsAt = window.resetsAt {
                    Label("Resets \(UsageFormat.timestamp(resetsAt))", systemImage: "arrow.clockwise")
                } else {
                    Label("Reset time unknown", systemImage: "arrow.clockwise")
                }
                Spacer(minLength: 8)
                if let minutes = window.windowMinutes {
                    Text("\(minutes / 60)h window")
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
            if let pace = window.pace {
                VStack(alignment: .leading, spacing: 2) {
                    if let delta = pace.deltaLabel {
                        Text(delta).fontWeight(.semibold).foregroundStyle(paceColor(pace.state))
                    }
                    if let expected = pace.expectedUsedPercent {
                        Text("Expected \(Int(expected.rounded()))% used by now")
                    }
                    if let runout = pace.runoutLabel { Text(runout) }
                    Text("\(pace.provenanceLabel); advisory, not a provider quota or billing fact.")
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(10)
        .background(Color.clear)
    }

    private var quotaTint: Color {
        guard let remaining = window.resolvedRemainingPercent else { return .secondary }
        if remaining <= 10 { return .red }
        if remaining <= 30 { return .orange }
        return .accentColor
    }

    private func paceColor(_ state: String?) -> Color {
        switch state {
        case "reserve": return .green
        case "deficit": return .orange
        default: return .secondary
        }
    }
}

private struct UsageHistoryPanel: View {
    let history: UsageHistoryPresentation
    let envelopeTotal: Int64?
    let metricColumnCount: Int
    let providerName: String
    let observedDay: UsageCalendarDay?
    let apiCurrency: String?
    let providerNativeCurrency: String?
    let topAPIEstimateModel: String?
    let topProviderNativeModel: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            Label(history.label, systemImage: historySymbol)
                .font(.callout.weight(.bold))
                .fixedSize(horizontal: false, vertical: true)
            Text(humanized(history.semantics ?? "semantics unknown"))
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Text(history.provenance)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            LazyVGrid(
                columns: usageColumns(metricColumnCount, spacing: 7),
                alignment: .leading,
                spacing: 7
            ) {
                UsageMetric(label: "Today", value: UsageFormat.tokens(history.todayTotalTokens))
                UsageMetric(label: "Rolling 7d", value: UsageFormat.tokens(history.rolling7DTotalTokens))
                UsageMetric(label: "Calendar week", value: UsageFormat.tokens(history.calendarWeekTotalTokens))
                UsageMetric(label: "All time", value: UsageFormat.tokens(history.allTimeTotalTokens))
            }

            if let envelopeTotal {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text("Ever-observed envelope").font(.caption.weight(.semibold))
                    Spacer(minLength: 6)
                    Text(UsageFormat.tokens(envelopeTotal))
                        .font(.callout.weight(.bold))
                        .monospacedDigit()
                }
                .foregroundStyle(.secondary)
            }

            Text(coverageDisclosure)
                .font(.caption)
                .foregroundStyle(history.kind == .canonical ? Color.secondary : Color.orange)
                .fixedSize(horizontal: false, vertical: true)
            if history.daily.contains(where: { $0.apiRateEstimateNanos != nil || $0.providerNativeCostNanos != nil }) {
                UsageDailyCostPanel(
                    rows: history.daily,
                    observedDay: observedDay,
                    apiCurrency: apiCurrency,
                    providerNativeCurrency: providerNativeCurrency,
                    topAPIEstimateModel: topAPIEstimateModel,
                    topProviderNativeModel: topProviderNativeModel,
                    providerName: providerName
                )
            }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .padding(11)
        .background(Color.clear)
        .overlay {
            RoundedRectangle(cornerRadius: 12)
                .stroke(historyTint.opacity(0.22), lineWidth: 1)
        }
    }

    private var historySymbol: String {
        history.kind == .providerReported ? "building.columns" : "externaldrive"
    }

    private var historyTint: Color {
        history.kind == .providerReported ? .purple : .cyan
    }

    private var coverageDisclosure: String {
        let dates = Array(Set(history.daily.compactMap { UsageCalendarDay($0.date) })).sorted { $0.rawValue < $1.rawValue }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        func date(_ day: UsageCalendarDay) -> Date? {
            let parts = day.rawValue.split(separator: "-").compactMap { Int($0) }
            guard parts.count == 3 else { return nil }
            return calendar.date(from: DateComponents(year: parts[0], month: parts[1], day: parts[2]))
        }
        guard let first = dates.first, let last = dates.last,
              let firstDate = date(first), let lastDate = date(last) else {
            return "Historical coverage unavailable; missing days are not inferred as zero."
        }
        let span = max(1, (calendar.dateComponents([.day], from: firstDate, to: lastDate).day ?? 0) + 1)
        let missing = max(0, span - dates.count)
        let authority: String
        switch history.kind {
        case .canonical:
            authority = missing == 0 ? "canonical coverage" : "canonical rows with incomplete calendar coverage"
        case .providerReported:
            authority = "provider-reported; completeness not guaranteed"
        case .retainedEnvelope:
            authority = "retained local envelope; noncanonical and incomplete"
        }
        return "Coverage \(first.rawValue)–\(last.rawValue) · \(dates.count)/\(span) observed days · \(missing) missing · \(authority)."
    }
}

private struct UsageDailyCostPanel: View {
    private enum Kind {
        case apiEstimate
        case providerNative
    }

    let rows: [UsageDaily]
    let observedDay: UsageCalendarDay?
    let apiCurrency: String?
    let providerNativeCurrency: String?
    let topAPIEstimateModel: String?
    let topProviderNativeModel: String?
    let providerName: String

    private var kind: Kind? {
        if rows.contains(where: { $0.apiRateEstimateNanos != nil }) { return .apiEstimate }
        if rows.contains(where: { $0.providerNativeCostNanos != nil }) { return .providerNative }
        return nil
    }

    private var points: [UsageDailyCostPoint] {
        guard let kind else { return [] }
        return Array(rows.compactMap { row in
            let nanos = kind == .apiEstimate ? row.apiRateEstimateNanos : row.providerNativeCostNanos
            return nanos.map { UsageDailyCostPoint(day: row.date, nanos: $0) }
        }.suffix(365))
    }

    private var todayNanos: Int64? {
        guard let observedDay else { return nil }
        return safeSum(points.filter { $0.day == observedDay.rawValue }.map(\.nanos))
    }

    private var coverageNanos: Int64? { safeSum(points.map(\.nanos)) }
    private var currency: String? { kind == .apiEstimate ? apiCurrency : providerNativeCurrency }
    private var topModel: String? { kind == .apiEstimate ? topAPIEstimateModel : topProviderNativeModel }

    var body: some View {
        if let kind {
            VStack(alignment: .leading, spacing: 7) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Label("Dollars per observed day", systemImage: "chart.bar.fill")
                        .font(.caption.weight(.bold))
                    Spacer(minLength: 8)
                    Text(points.count == 1 ? "1 observed day" : "\(points.count) observed days")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Text(kind == .apiEstimate
                    ? "Zero-baseline local API-rate estimate · non-billed and noncanonical."
                    : "Zero-baseline provider-native daily values · not an invoice unless explicitly declared.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                LazyVGrid(columns: usageColumns(2, spacing: 7), alignment: .leading, spacing: 7) {
                    UsageMetric(
                        label: kind == .apiEstimate ? "Today API estimate" : "Today provider native",
                        value: UsageFormat.costNanos(todayNanos, currency: currency)
                    )
                    UsageMetric(
                        label: "Observed coverage cost",
                        value: UsageFormat.costNanos(coverageNanos, currency: currency)
                    )
                    UsageMetric(
                        label: "Observed range",
                        value: points.first.map { "\($0.day)–\(points.last?.day ?? $0.day)" } ?? "Unknown"
                    )
                    UsageMetric(label: "Top model (declared coverage)", value: topModel ?? "Unknown")
                }
                UsageDailyCostGraph(
                    points: points,
                    currency: currency,
                    label: "\(providerName) daily \(kind == .apiEstimate ? "API-rate estimate" : "provider-native cost")"
                )
            }
        }
    }

    private func safeSum(_ values: [Int64]) -> Int64? {
        guard !values.isEmpty else { return nil }
        return values.reduce(into: Int64(0)) { total, value in
            let result = total.addingReportingOverflow(value)
            total = result.overflow ? Int64.max : result.partialValue
        }
    }
}

private struct UsageDailyCostPoint: Equatable {
    let day: String
    let nanos: Int64
}

private struct UsageDailyCostGraph: View {
    let points: [UsageDailyCostPoint]
    let currency: String?
    let label: String

    var body: some View {
        if points.count < 2 {
            Text("A daily cost chart needs at least two observed cost rows.")
                .font(.caption)
                .foregroundStyle(.secondary)
        } else {
            let peak = max(points.map(\.nanos).max() ?? 1, 1)
            HStack(alignment: .bottom, spacing: 2) {
                ForEach(Array(points.enumerated()), id: \.offset) { _, point in
                    Capsule()
                        .fill(Color.accentColor.opacity(0.72))
                        .frame(maxWidth: .infinity)
                        .frame(height: max(2, 68 * CGFloat(Double(point.nanos) / Double(peak))))
                }
            }
            .frame(height: 68, alignment: .bottom)
            .accessibilityLabel(label)
            .accessibilityValue("\(points.count) observed days; peak \(UsageFormat.costNanos(peak, currency: currency))")
        }
    }
}

private struct UsageMetric: View {
    let label: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(value)
                .font(.callout.weight(.bold))
                .monospacedDigit()
                .fixedSize(horizontal: false, vertical: true)
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, minHeight: 50, alignment: .topLeading)
        .padding(8)
        .background(Color.primary.opacity(0.045), in: RoundedRectangle(cornerRadius: 9))
    }
}

private struct UsageCostMetric: View {
    let label: String
    let value: String
    let detail: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 20) {
            Text(label)
                .font(.callout)
                .foregroundStyle(.secondary)
            Spacer(minLength: 20)
            Text(value)
                .font(.callout.weight(.medium))
                .monospacedDigit()
                .multilineTextAlignment(.trailing)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .combine)
        .accessibilityHint(detail)
        .help(detail)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(Color.primary.opacity(0.045), in: RoundedRectangle(cornerRadius: 10))
    }
}

private struct UsageBreakdownHeader: View {
    let title: String
    let status: String?
    let semantics: String?
    let canonical: Bool?
    let coverageStart: UsageCalendarDay?
    let coverageEnd: UsageCalendarDay?
    let observedAt: Date?

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(title).font(.headline)
                Spacer(minLength: 8)
                Text(humanized(status ?? "status unknown"))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Text(canonical == true ? "Canonical source" : "Provider/source semantics")
                .font(.caption.weight(.semibold))
            Text(humanized(semantics ?? "semantics unknown"))
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Text("Coverage \(UsageFormat.calendarDay(coverageStart)) – \(UsageFormat.calendarDay(coverageEnd))")
            Text("Observed \(UsageFormat.timestamp(observedAt)) · totals cover this interval, not provider lifetime.")
        }
        .font(.caption)
        .foregroundStyle(.secondary)
        .fixedSize(horizontal: false, vertical: true)
    }
}

private struct UsageModelBreakdownSection: View {
    let breakdown: UsageBreakdown<UsageModelBreakdownItem>
    let providerNativeCurrency: String?
    let apiCurrency: String?
    private let limit = 8
    private var ranked: [UsageModelBreakdownItem] { Array(breakdown.rankedItems.prefix(limit)) }
    private var omitted: Int { breakdown.omittedCount + max(0, breakdown.items.count - limit) }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            UsageBreakdownHeader(
                title: "Models",
                status: breakdown.status,
                semantics: breakdown.semantics,
                canonical: breakdown.canonical,
                coverageStart: breakdown.coverageStart,
                coverageEnd: breakdown.coverageEnd,
                observedAt: breakdown.observedAt
            )
            ForEach(Array(ranked.enumerated()), id: \.element.id) { rank, item in
                VStack(alignment: .leading, spacing: 5) {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text("#\(rank + 1)").font(.caption.monospacedDigit()).foregroundStyle(.tertiary)
                        Text(item.label ?? "Model unknown")
                            .font(.callout.weight(.semibold))
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 8)
                        Text(UsageFormat.tokens(item.totalTokens))
                            .font(.callout.weight(.bold))
                            .monospacedDigit()
                    }
                    Text(item.key ?? "key unknown").font(.caption.monospaced()).foregroundStyle(.secondary)
                    Text("Today \(UsageFormat.tokens(item.todayTotalTokens)) · 7d \(UsageFormat.tokens(item.rolling7DTotalTokens)) · week \(UsageFormat.tokens(item.calendarWeekTotalTokens))")
                    Text("Provider native \(UsageFormat.costNanos(item.providerNativeCostNanos, currency: providerNativeCurrency)) · API estimate \(UsageFormat.costNanos(item.apiRateEstimateNanos, currency: apiCurrency))")
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .padding(9)
                .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 9))
            }
            if ranked.isEmpty {
                Text("No model breakdown items available.").font(.callout).foregroundStyle(.secondary)
            }
            if omitted > 0 {
                Text("\(omitted) additional model item\(omitted == 1 ? "" : "s") omitted.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

private struct UsageProjectBreakdownSection: View {
    let breakdown: UsageBreakdown<UsageProjectBreakdownItem>
    private let limit = 8
    private var ranked: [UsageProjectBreakdownItem] { Array(breakdown.rankedItems.prefix(limit)) }
    private var omitted: Int { breakdown.omittedCount + max(0, breakdown.items.count - limit) }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            UsageBreakdownHeader(
                title: "Projects",
                status: breakdown.status,
                semantics: breakdown.semantics,
                canonical: breakdown.canonical,
                coverageStart: breakdown.coverageStart,
                coverageEnd: breakdown.coverageEnd,
                observedAt: breakdown.observedAt
            )
            ForEach(Array(ranked.enumerated()), id: \.element.id) { rank, item in
                VStack(alignment: .leading, spacing: 5) {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text("#\(rank + 1)").font(.caption.monospacedDigit()).foregroundStyle(.tertiary)
                        Text(item.sanitizedLabel)
                            .font(.callout.weight(.semibold))
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 8)
                        Text(UsageFormat.tokens(item.totalTokens))
                            .font(.callout.weight(.bold))
                            .monospacedDigit()
                    }
                    Text(item.sanitizedOpaqueKey).font(.caption.monospaced()).foregroundStyle(.secondary)
                    Text("Today \(UsageFormat.tokens(item.todayTotalTokens)) · 7d \(UsageFormat.tokens(item.rolling7DTotalTokens)) · week \(UsageFormat.tokens(item.calendarWeekTotalTokens))")
                    Text("Top model \(item.topModel ?? "Unknown")")
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .padding(9)
                .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 9))
            }
            if ranked.isEmpty {
                Text("No sanitized project breakdown items available.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            if omitted > 0 {
                Text("\(omitted) additional project item\(omitted == 1 ? "" : "s") omitted.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

private struct UsageDailyGraph: View {
    let projection: UsageDailyCostTrendProjection

    private var points: [UsageDailyCostTrendPoint] { projection.points }
    private var providerTint: Color {
        projection.providerID == "claude"
            ? Color(red: 0.96, green: 0.50, blue: 0.32)
            : Color(red: 0.58, green: 0.40, blue: 0.96)
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Label("Dollars per observed day", systemImage: "chart.bar.fill")
                    .font(.caption.weight(.semibold))
                Spacer(minLength: 6)
                Text(points.count >= 2 ? "\(points.count) observed days" : "Not enough observations")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if points.count < 2 {
                Text("A daily cost chart needs at least two observed cost rows.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 72, alignment: .leading)
            } else {
                let peak = max(points.map(\.nanos).max() ?? 1, 1)
                HStack(alignment: .bottom, spacing: slots.count > 120 ? 0 : 1) {
                    ForEach(Array(slots.enumerated()), id: \.offset) { _, point in
                        if let point {
                            RoundedRectangle(cornerRadius: 1.5)
                                .fill(providerTint.opacity(projection.sourceKind == .providerReported ? 0.50 : 0.82))
                                .frame(maxWidth: .infinity)
                                .frame(height: max(1, 76 * CGFloat(Double(point.nanos) / Double(peak))))
                                .help("\(point.day) · \(UsageFormat.costNanos(point.nanos, currency: point.currency)) · \(point.costKind)")
                        } else {
                            Color.clear.frame(maxWidth: .infinity, minHeight: 1)
                        }
                    }
                }
                .overlay(alignment: .bottom) { Divider().opacity(0.45) }
                .frame(height: 84, alignment: .bottom)
                .accessibilityLabel("\(projection.providerID) \(projection.sourceLabel) daily estimated cost")
                .accessibilityValue("\(points.count) observed days; peak \(UsageFormat.costNanos(peak, currency: points.first?.currency))")
                HStack {
                    Text(points.first?.day ?? "Unknown")
                    Spacer()
                    Text("Peak \(UsageFormat.costNanos(peak, currency: points.first?.currency))")
                    Spacer()
                    Text(points.last?.day ?? "Unknown")
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            Text("\(projection.sourceLabel)  ·  \(points.count)/\(slots.count) observed")
                .font(.caption2)
                .foregroundStyle(projection.sourceKind == .canonical ? Color.secondary : Color.orange)
                .lineLimit(1)
                .help("\(points.first?.costKind ?? "Cost semantics unavailable") · \(max(0, slots.count - points.count)) missing · missing days are not zero")
                .accessibilityHint("\(max(0, slots.count - points.count)) missing days are not plotted as zero")
        }
    }

    private var slots: [UsageDailyCostTrendPoint?] {
        guard let first = points.first, let last = points.last,
              let firstDay = UsageCalendarDay(first.day),
              let lastDay = UsageCalendarDay(last.day) else { return points.map(Optional.some) }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        let formatter = DateFormatter()
        formatter.calendar = calendar
        formatter.timeZone = calendar.timeZone
        formatter.dateFormat = "yyyy-MM-dd"
        guard let start = formatter.date(from: firstDay.rawValue),
              let end = formatter.date(from: lastDay.rawValue) else { return points.map(Optional.some) }
        let byDay = Dictionary(uniqueKeysWithValues: points.map { ($0.day, $0) })
        let count = max(1, (calendar.dateComponents([.day], from: start, to: end).day ?? 0) + 1)
        return (0..<count).map { offset in
            calendar.date(byAdding: .day, value: offset, to: start)
                .map(formatter.string)
                .flatMap { byDay[$0] }
        }
    }
}

private func usageColumns(_ count: Int, spacing: CGFloat) -> [GridItem] {
    Array(
        repeating: GridItem(.flexible(minimum: 0), spacing: spacing, alignment: .top),
        count: max(1, count)
    )
}

private func humanized(_ value: String) -> String {
    UsagePresentationText.neutralized(value)
        .replacingOccurrences(of: "_", with: " ")
}
