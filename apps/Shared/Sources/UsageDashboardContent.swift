import SwiftUI
#if os(macOS)
import AppKit
#endif

enum UsageWindowGeometry {
    static let preferredWidth: CGFloat = 460
    static let preferredHeight: CGFloat = 880
    static let screenInset: CGFloat = 40
    static let anchorGap: CGFloat = 12
    static let minimumHeight: CGFloat = 360

    static func attachedContentSize(visibleFrame: CGRect?, anchorFrame: CGRect?) -> CGSize {
        let width = visibleFrame.map {
            min(preferredWidth, max(1, $0.width - screenInset))
        } ?? preferredWidth
        return CGSize(width: width, height: attachedHeight(visibleFrame: visibleFrame, anchorFrame: anchorFrame))
    }

    static func attachedHeight(visibleFrame: CGRect?, anchorFrame: CGRect?) -> CGFloat {
        guard let visibleFrame else { return preferredHeight }
        let screenAvailable = max(minimumHeight, visibleFrame.height - screenInset)
        guard let anchorFrame else { return min(preferredHeight, screenAvailable) }
        let belowAnchor = max(0, anchorFrame.minY - visibleFrame.minY - anchorGap)
        let available = belowAnchor >= minimumHeight ? belowAnchor : screenAvailable
        return min(preferredHeight, max(minimumHeight, available))
    }

    static func detachedContentSize(
        currentSize: CGSize,
        visibleFrame: CGRect?
    ) -> CGSize {
        let desiredHeight = max(preferredHeight, currentSize.height)
        let height: CGFloat
        let desiredWidth = max(preferredWidth, currentSize.width)
        let width: CGFloat
        if let visibleFrame {
            height = min(desiredHeight, max(minimumHeight, visibleFrame.height - screenInset))
            width = min(desiredWidth, max(1, visibleFrame.width - screenInset))
        } else {
            height = desiredHeight
            width = desiredWidth
        }
        return CGSize(
            width: width,
            height: height
        )
    }
}

#if os(macOS)
/// Pins route hosts to their container bounds so changing from a previous panel
/// size cannot apply an autoresizing delta to an already-final-sized child.
enum UsageRouteContainerLayout {
    static func pin(_ view: NSView, in container: NSView) {
        view.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(view)
        NSLayoutConstraint.activate([
            view.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            view.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            view.topAnchor.constraint(equalTo: container.topAnchor),
            view.bottomAnchor.constraint(equalTo: container.bottomAnchor),
        ])
    }
}
#endif

private enum UsageProviderVisualStyle {
    static func assetName(_ providerID: String) -> String {
        providerID.lowercased() == "claude" ? "claude-menu" : "codex-menu"
    }

    static func tint(_ providerID: String) -> Color {
        providerID.lowercased() == "claude"
            ? Color(red: 0.95, green: 0.47, blue: 0.24)
            : Color(red: 0.64, green: 0.43, blue: 0.96)
    }
}

struct UsageCompactBoardStrip: View {
    let state: UsageDashboardState
    let systemTelemetry: SystemTelemetrySnapshot?
    var showSystemTelemetry = true
    var showDisk = true
    var barPalette: UsageBarPalette = .colored
    var onOpenDetails: (() -> Void)?
    var onExpandedChange: ((Bool) -> Void)?
    @State private var expanded = false

    private var providers: [UsageCompactProviderSummary] {
        UsageCompactProviderSummary.summaries(from: state.snapshot)
            .filter { ["claude", "codex"].contains($0.id.lowercased()) }
    }

    private var totalTokensCost: String {
        let values = providers.compactMap(\.retainedUSDEstimateNanos)
        return UsageFormat.costNanos(values.isEmpty ? nil : values.reduce(0, +), currency: "USD")
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Button {
                    let next = !expanded
                    withAnimation(.easeInOut(duration: 0.16)) { expanded = next }
                    onExpandedChange?(next)
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
                }

                VStack(alignment: .leading, spacing: 0) {
                    Text("TOTAL TOKEN COST")
                        .font(.system(size: 6.5, weight: .semibold))
                        .foregroundStyle(.secondary)
                    Text(totalTokensCost)
                        .font(.system(size: 9.5, weight: .semibold, design: .rounded))
                        .monospacedDigit()
                        .lineLimit(1)
                }
                .layoutPriority(4)
                .accessibilityElement(children: .combine)

                if !providers.isEmpty {
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
                        snapshot: systemTelemetry, showDisk: true, embedded: true
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
                        HStack(alignment: .top, spacing: 12) {
                            ForEach(providers) { provider in
                                expandedProvider(provider)
                                    .frame(maxWidth: .infinity, alignment: .topLeading)
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
                            showDisk: true,
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
        .onAppear {
            expanded = false
            onExpandedChange?(false)
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
        UsageProviderVisualStyle.tint(provider.id)
    }

    private func quotaTint(_ providerColor: Color) -> Color {
        barPalette == .colored ? providerColor : Color.primary.opacity(0.82)
    }

    private func compactProvider(_ provider: UsageCompactProviderSummary) -> some View {
        let windows = compactWindows(provider)
        let color = tint(provider)
        let barColor = quotaTint(color)
        return HStack(spacing: 6) {
            Image(UsageProviderVisualStyle.assetName(provider.id))
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
        var windows: [(String, UsageQuotaWindow?)] = [
            ("Session", provider.session), ("Weekly", provider.weekly), ("Fable", provider.fable),
        ]
        if provider.fable == nil {
            windows.removeLast()
        }
        return VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 6) {
                Image(UsageProviderVisualStyle.assetName(provider.id))
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
                HStack(spacing: 7) {
                    Text(label).frame(width: 46, alignment: .leading)
                    ProgressView(value: window?.resolvedRemainingPercent ?? 0, total: 100)
                        .tint(window == nil ? Color.secondary : barColor)
                    Text(window?.resolvedRemainingPercent.map { "\(Int($0.rounded()))%" } ?? "N/A")
                        .fontWeight(.bold)
                        .foregroundStyle(window?.resolvedRemainingPercent == nil ? Color.secondary : barColor)
                        .frame(width: 34, alignment: .trailing)
                    Text("↻ \(duration(window?.countdownSeconds))")
                        .frame(width: 58, alignment: .leading)
                    if window?.pace?.secondsToExhaustion != nil {
                        Text("out \(duration(window?.pace?.secondsToExhaustion))")
                            .frame(width: 48, alignment: .leading)
                    }
                }
                .font(.system(size: 9.5).monospacedDigit())
                .foregroundStyle(.secondary)
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
    /// The installed COORD route uses the deliberately dense cockpit composition.
    /// Other hosts retain the broader adaptive dashboard while they migrate.
    var usesDenseRoute = false
    var onClose: (() -> Void)?
    var onOpenSettings: (() -> Void)?
    var onRefresh: (() -> Void)?

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

    /// The route-level total stays limited to USD API-rate estimates. It is a retained
    /// estimate, never subscription spend or a cross-currency aggregation.
    private var totalEstimatedCostNanos: Int64? {
        let values = state.snapshot?.providers.values.compactMap { provider -> Int64? in
            let estimate = provider.costs?.apiRateEstimate
            return estimate?.byCurrency?["USD"]
                ?? (estimate?.currency == "USD" ? estimate?.amountNanos : nil)
        } ?? []
        return values.isEmpty ? nil : values.reduce(0, +)
    }

    var body: some View {
        if usesDenseRoute {
            UsageDenseRoute(
                state: state,
                cards: cards,
                totalEstimatedCostNanos: totalEstimatedCostNanos,
                onClose: onClose,
                onOpenSettings: onOpenSettings,
                onRefresh: onRefresh
            )
        } else {
        GeometryReader { proxy in
            let layout = UsageDashboardLayout.plan(
                forWidth: Double(proxy.size.width),
                forceCompact: forceCompact
            )
            VStack(spacing: 0) {
                UsageDashboardHeader(
                    state: state,
                    layout: layout,
                    totalEstimatedCostNanos: totalEstimatedCostNanos,
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

                Divider().opacity(0.28)
                UsageDashboardFooter(
                    state: state,
                    onRefresh: onRefresh,
                    onOpenSettings: onOpenSettings
                )
                .padding(.horizontal, layout.widthClass == .compact ? 14 : 22)
                .padding(.vertical, 8)
            }
            .background(Color.clear)
        }
        }
    }

    private func sectionSpacing(for layout: UsageDashboardLayout) -> CGFloat {
        layout.widthClass == .compact ? 14 : 18
    }
}

/// Stable geometry contract for the dense installed Usage route.
private enum UsageDenseRouteLayout {
    static let outerPadding: CGFloat = 12
    static let sectionSpacing: CGFloat = 16
    static let compactChartHeight: CGFloat = 74
    static let regularChartHeight: CGFloat = 92
    /// Space reserved for the daily-cost title and date axis outside the plot.
    static let chartChromeHeight: CGFloat = 36
    static let claudeChartPlotHeight: CGFloat = 82
    /// The final Codex panel uses the otherwise-unused vertical room in the usage route.
    static let codexChartPlotHeight: CGFloat = 120
    static let providerHorizontalPadding: CGFloat = 18
    static let providerVerticalPadding: CGFloat = 14
    static let providerCornerRadius: CGFloat = 12
    static let providerBorderOpacity: CGFloat = 0.23
    static let providerBorderWidth: CGFloat = 0.75
    static let providerFactsSpacing: CGFloat = 22
    static let providerChartWidth: CGFloat = 300
    static let horizontalProviderMinimumWidth: CGFloat = 620
    static let totalCornerRadius: CGFloat = 9
    static let totalBackgroundOpacity: CGFloat = 0.045
    static let visibleLabelOrder = ["Total Tokens Costs", "Claude", "Codex", "Today cost", "Tokens today", "Retained cost", "Daily cost"]

    static let claudeTint = Color(red: 0.95, green: 0.47, blue: 0.24)
    static let codexTint = Color(red: 0.66, green: 0.42, blue: 1.00)

    static func tint(_ providerID: String) -> Color {
        providerID.lowercased() == "claude" ? claudeTint : codexTint
    }
}

/// Installed COORD provider-usage surface. This is intentionally separate from
/// the general shared dashboard so embedded non-menu callers can continue their
/// adaptive transition without altering the installed route.
private struct UsageDenseRoute: View {
    let state: UsageDashboardState
    let cards: [UsageProviderCardState]
    let totalEstimatedCostNanos: Int64?
    let onClose: (() -> Void)?
    let onOpenSettings: (() -> Void)?
    let onRefresh: (() -> Void)?

    private func providerRank(_ id: String) -> Int {
        switch id.lowercased() {
        case "claude": 0
        case "codex": 1
        default: 2
        }
    }

    private var orderedCards: [UsageProviderCardState] {
        cards.sorted {
            let lhsRank = providerRank($0.id)
            let rhsRank = providerRank($1.id)
            return lhsRank == rhsRank ? $0.id < $1.id : lhsRank < rhsRank
        }
    }

    var body: some View {
        GeometryReader { geometry in
            let chartPlotHeight = geometry.size.height < 900
                ? UsageDenseRouteLayout.compactChartHeight
                : UsageDenseRouteLayout.regularChartHeight
            VStack(alignment: .leading, spacing: UsageDenseRouteLayout.sectionSpacing) {
                UsageDenseRouteHeader(
                    state: state,
                    onClose: onClose,
                    onOpenSettings: onOpenSettings
                )
                UsageDenseTotalCostStrip(totalEstimatedCostNanos: totalEstimatedCostNanos)
                    .frame(minHeight: 44)
                    .fixedSize(horizontal: false, vertical: true)
                    .layoutPriority(100)

                if cards.isEmpty {
                    ContentUnavailableView(
                        "Provider usage unavailable",
                        systemImage: "gauge.with.dots.needle.33percent"
                    )
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    ScrollView(.vertical) {
                        LazyVStack(alignment: .leading, spacing: UsageDenseRouteLayout.sectionSpacing) {
                            ForEach(orderedCards) { card in
                                UsageDenseProviderSection(card: card, chartPlotHeight: chartPlotHeight)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .topLeading)
                    }
                    .defaultScrollAnchor(.top)
                    .frame(maxHeight: .infinity)
                }

                UsageDashboardFooter(
                    state: state,
                    onRefresh: onRefresh,
                    onOpenSettings: onOpenSettings
                )
                .fixedSize(horizontal: false, vertical: true)
                .layoutPriority(100)
            }
            .padding(.horizontal, UsageDenseRouteLayout.outerPadding)
            .padding(.vertical, UsageDenseRouteLayout.outerPadding)
            .frame(width: geometry.size.width, height: geometry.size.height, alignment: .topLeading)
        }
        .clipped()
        .background(Color.clear)
    }
}

private struct UsageDenseRouteHeader: View {
    let state: UsageDashboardState
    let onClose: (() -> Void)?
    let onOpenSettings: (() -> Void)?

    var body: some View {
        HStack(spacing: 8) {
            Text("Provider usage")
                .font(.system(size: 12, weight: .bold, design: .rounded))
            if state.refreshing { ProgressView().controlSize(.mini) }
            if state.stale {
                Label("Last good", systemImage: "clock.badge.exclamationmark")
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(.orange)
            }
            Spacer(minLength: 4)
            if let onOpenSettings {
                Button(action: onOpenSettings) {
                    Image(systemName: "person.crop.circle.badge.gearshape")
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .help("Provider settings")
                .accessibilityLabel("Provider settings")
            }
            if let onClose {
                Button(action: onClose) {
                    Image(systemName: "xmark.circle.fill")
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .help("Close usage")
                .accessibilityLabel("Close usage")
            }
        }
        .frame(height: 20)
    }
}

private enum UsageDashboardCostFormat {
    static func display(_ value: Int64?, currency: String? = "USD") -> String {
        let formatted = UsageFormat.costNanos(value, currency: currency)
        return currency?.trimmingCharacters(in: .whitespacesAndNewlines).uppercased() == "USD"
            ? formatted.replacingOccurrences(of: "USD ", with: "$")
            : formatted
    }

}

private struct UsageDenseTotalCostStrip: View {
    let totalEstimatedCostNanos: Int64?

    var body: some View {
        VStack(alignment: .center, spacing: 3) {
            Text("Total Tokens Costs")
                .font(.system(size: 10.5, weight: .semibold))
                .foregroundStyle(.secondary)
            Text(UsageDashboardCostFormat.display(totalEstimatedCostNanos))
                .font(.system(size: 19, weight: .bold, design: .rounded))
                .monospacedDigit()
                .lineLimit(1)
        }
        .multilineTextAlignment(.center)
        .frame(maxWidth: .infinity, alignment: .center)
        .padding(.horizontal, UsageDenseRouteLayout.outerPadding)
        .padding(.vertical, 10)
        // No box. A filled card behind the total sat on top of the sheet's own
        // background and read as a second panel around a single figure. Matches
        // LITAN, which dropped it for the same reason on 2026-09-02.
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Total estimated API-rate cost \(UsageDashboardCostFormat.display(totalEstimatedCostNanos))")
    }
}

private struct UsageDenseProviderSection: View {
    let card: UsageProviderCardState
    let chartPlotHeight: CGFloat

    private var summary: UsageCompactProviderSummary {
        UsageCompactProviderSummary.summary(for: card)
    }
    private var tint: Color { UsageDenseRouteLayout.tint(card.id) }
    private var isClaude: Bool { card.id.lowercased() == "claude" }
    private var providerContentSpacing: CGFloat { isClaude ? 8 : 11 }
    private var factsSpacing: CGFloat { isClaude ? 10 : 16 }
    private var quotaSpacing: CGFloat { isClaude ? 8 : 12 }
    private var metricSpacing: CGFloat { isClaude ? 12 : 18 }
    private var effectiveChartPlotHeight: CGFloat {
        switch card.id.lowercased() {
        case "claude": return UsageDenseRouteLayout.claudeChartPlotHeight
        case "codex": return UsageDenseRouteLayout.codexChartPlotHeight
        default: return chartPlotHeight
        }
    }
    private var chartPanelHeight: CGFloat {
        effectiveChartPlotHeight + UsageDenseRouteLayout.chartChromeHeight
    }
    private var dailyCostProjections: [UsageDailyCostTrendProjection] {
        UsageHistoryPresentation.sources(for: card.provider)
            .map { UsageDailyCostTrendProjection.make(providerID: card.id, history: $0, costs: card.provider.costs) }
    }
    private var dailyProjection: UsageDailyCostTrendProjection? {
        dailyCostProjections.first(where: { $0.points.count >= 2 })
    }
    private var todayCostPoint: UsageDailyCostTrendPoint? {
        dailyCostProjections.lazy.compactMap { $0.currentDayPoint() }.first
    }
    private var todayCostLabel: String {
        UsageDashboardCostFormat.display(
            todayCostPoint?.nanos,
            currency: todayCostPoint?.currency ?? card.provider.costs?.apiRateEstimate?.currency
        )
    }
    private var quotas: [(String, UsageQuotaWindow?)] {
        [("Session", summary.session), ("Weekly", summary.weekly), ("Fable", summary.fable)]
    }

    var body: some View {
        VStack(alignment: .leading, spacing: providerContentSpacing) {
            HStack(spacing: 6) {
                Image(UsageProviderVisualStyle.assetName(card.id))
                    .resizable()
                    .scaledToFit()
                    .frame(width: 14, height: 14)
                Text(summary.displayName)
                    .font(.system(size: 16, weight: .bold, design: .rounded))
                Circle()
                    .fill(summary.connected == true ? tint : Color.secondary)
                    .frame(width: 5, height: 5)
                Text(summary.connectionLabel)
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(summary.connected == true ? tint : .secondary)
                Spacer(minLength: 4)
                Text(planLabel)
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(.secondary)
            }

            ViewThatFits(in: .horizontal) {
                HStack(alignment: .top, spacing: UsageDenseRouteLayout.providerFactsSpacing) {
                    facts
                    chart.frame(width: UsageDenseRouteLayout.providerChartWidth, height: chartPanelHeight)
                }
                .frame(minWidth: UsageDenseRouteLayout.horizontalProviderMinimumWidth)
                VStack(alignment: .leading, spacing: 14) {
                    facts
                    chart.frame(height: chartPanelHeight)
                }
            }
        }
        .padding(.horizontal, UsageDenseRouteLayout.providerHorizontalPadding)
        .padding(.vertical, UsageDenseRouteLayout.providerVerticalPadding)
        .background(tint.opacity(0.035), in: RoundedRectangle(cornerRadius: UsageDenseRouteLayout.providerCornerRadius, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: UsageDenseRouteLayout.providerCornerRadius, style: .continuous)
                .stroke(tint.opacity(UsageDenseRouteLayout.providerBorderOpacity), lineWidth: UsageDenseRouteLayout.providerBorderWidth)
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
    }

    private var facts: some View {
        VStack(alignment: .leading, spacing: factsSpacing) {
            if quotas.allSatisfy({ $0.1 == nil }) {
                Text("Quota unavailable")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(.secondary)
                    .frame(height: 34)
            } else {
                VStack(alignment: .leading, spacing: quotaSpacing) {
                    ForEach(quotas.compactMap { pair in pair.1.map { (pair.0, $0) } }, id: \.1.id) { label, window in
                        UsageDenseQuotaRow(label: label, window: window, tint: tint)
                    }
                }
            }
            HStack(spacing: metricSpacing) {
                UsageDenseMetric(label: "Today cost", value: todayCostLabel)
                // "Tokens" was ambiguous against LITAN, which shows both a daily
                // and a cumulative figure: the same bare label carried today's
                // number on one surface and an 85B lifetime envelope on the
                // other. This one is today's, so it says so.
                UsageDenseMetric(label: "Tokens today", value: UsageFormat.tokens(summary.todayTokens))
                UsageDenseMetric(label: "Retained cost", value: UsageDashboardCostFormat.display(summary.retainedUSDEstimateNanos))
            }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
    }

    private var chart: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text("Daily cost")
                    .font(.system(size: 10, weight: .bold))
                Spacer(minLength: 6)
                Text(peakDailyCost)
                    .font(.system(size: 9.5, weight: .semibold).monospacedDigit())
                    .foregroundStyle(tint)
                    .lineLimit(1)
                    .minimumScaleFactor(0.75)
                    .accessibilityLabel("Peak daily cost \(peakDailyCost)")
            }
            if let dailyProjection {
                UsageDenseDailyCostChart(
                    projection: dailyProjection,
                    tint: tint,
                    plotHeight: effectiveChartPlotHeight
                )
            } else {
                Text("History unavailable")
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: effectiveChartPlotHeight, alignment: .leading)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var peakDailyCost: String {
        guard let peak = dailyProjection?.points.max(by: { lhs, rhs in
            lhs.nanos == rhs.nanos ? lhs.day > rhs.day : lhs.nanos < rhs.nanos
        }) else { return "Peak unavailable" }
        return UsageDashboardCostFormat.display(peak.nanos, currency: peak.currency)
    }
    private var planLabel: String {
        let plan = card.provider.account?.plan
        return plan?.contains("@") == false ? plan ?? "—" : "—"
    }
}

private struct UsageDenseQuotaRow: View {
    let label: String
    let window: UsageQuotaWindow
    let tint: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 5) {
                Text(label).font(.system(size: 10, weight: .semibold)).lineLimit(1)
                Spacer(minLength: 3)
                Text(window.resolvedRemainingPercent.map { String(format: "%.0f%%", $0) } ?? "—")
                    .font(.system(size: 10, weight: .bold).monospacedDigit())
                    .foregroundStyle(window.resolvedRemainingPercent == nil ? Color.secondary : tint)
                Text(window.countdownSeconds.map { "↻ \(UsageFormat.duration($0))" } ?? "↻ —")
                    .font(.system(size: 9.5).monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            ProgressView(value: window.resolvedRemainingPercent ?? 0, total: 100)
                .tint(tint)
                .frame(height: 5)
            HStack(spacing: 5) {
                Text(window.pace?.deltaLabel ?? "Pace unavailable")
                    .foregroundStyle(window.pace?.state == "deficit" ? tint : Color.secondary)
                Spacer(minLength: 3)
                if let seconds = window.pace?.secondsToExhaustion,
                   window.pace?.willLastToReset == false {
                    Text("out \(UsageFormat.duration(seconds))").foregroundStyle(tint)
                } else if window.pace?.willLastToReset == true {
                    Text("lasts to reset")
                }
            }
            .font(.system(size: 9).monospacedDigit())
            .foregroundStyle(.secondary)
        }
        .padding(.vertical, 2)
    }
}

private struct UsageDenseMetric: View {
    let label: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(label).font(.system(size: 9, weight: .medium)).foregroundStyle(.secondary)
            Text(value)
                .font(.system(size: 11.5, weight: .bold).monospacedDigit())
                .lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct UsageDenseDailyCostChart: View {
    let projection: UsageDailyCostTrendProjection
    let tint: Color
    let plotHeight: CGFloat

    @State private var hoveredPoint: UsageDailyCostTrendPoint?
    @State private var hoverLocation = CGPoint.zero

    private func modelDetail(for point: UsageDailyCostTrendPoint) -> String {
        let models = point.modelBreakdowns ?? []
        guard !models.isEmpty else { return "No per-day model detail" }
        return models.map { model in
            "\(model.displayLabel): \(UsageFormat.tokens(model.totalTokens))"
        }.joined(separator: ", ")
    }

    private func hoverDetail(for point: UsageDailyCostTrendPoint) -> String {
        let tokens = point.totalTokens.map(UsageFormat.tokens) ?? "Unavailable"
        return [
            "Date: \(point.day)",
            "Cost: \(UsageDashboardCostFormat.display(point.nanos, currency: point.currency))",
            "Tokens: \(tokens)",
            "Models: \(modelDetail(for: point))",
        ].joined(separator: "\n")
    }

    var body: some View {
        let points = projection.points
        if points.count < 2 {
            Text("History unavailable")
                .font(.system(size: 9, weight: .medium))
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, minHeight: plotHeight, alignment: .leading)
        } else {
            let peak = max(points.map(\.nanos).max() ?? 1, 1)
            let peakPoint = points.max(by: { lhs, rhs in
                lhs.nanos == rhs.nanos ? lhs.day > rhs.day : lhs.nanos < rhs.nanos
            }) ?? points[0]
            let barGap: CGFloat = points.count > 120 ? 0 : 1
            VStack(alignment: .leading, spacing: 3) {
                GeometryReader { proxy in
                    HStack(alignment: .bottom, spacing: barGap) {
                        ForEach(Array(points.enumerated()), id: \.offset) { _, point in
                            RoundedRectangle(cornerRadius: 1)
                                .fill(tint.opacity(projection.sourceKind == .providerReported ? 0.50 : 0.82))
                                .frame(maxWidth: .infinity)
                                .frame(height: max(1, plotHeight * CGFloat(Double(point.nanos) / Double(peak))))
                                .help(hoverDetail(for: point))
                        }
                    }
                    .frame(width: proxy.size.width, height: proxy.size.height, alignment: .bottom)
                    .contentShape(Rectangle())
                    .onContinuousHover { phase in
                        switch phase {
                        case .active(let location):
                            let relativeX = min(max(location.x / max(proxy.size.width, 1), 0), 0.999_999)
                            let index = min(points.count - 1, Int(relativeX * CGFloat(points.count)))
                            hoveredPoint = points[index]
                            hoverLocation = location
                        case .ended:
                            hoveredPoint = nil
                        }
                    }
                    .overlay(alignment: .topLeading) {
                        if let hoveredPoint {
                            let cardWidth = min(CGFloat(210), max(CGFloat(150), proxy.size.width - 8))
                            let minimumX = cardWidth / 2 + 4
                            let maximumX = max(minimumX, proxy.size.width - cardWidth / 2 - 4)
                            let minimumY: CGFloat = 50
                            let maximumY = max(minimumY, proxy.size.height - 50)
                            UsageDenseDailyHoverCard(
                                point: hoveredPoint,
                                tint: tint
                            )
                            .frame(width: cardWidth)
                            .position(
                                x: min(max(minimumX, hoverLocation.x), maximumX),
                                y: min(max(minimumY, hoverLocation.y + 42), maximumY)
                            )
                            .allowsHitTesting(false)
                        }
                    }
                }
                .frame(height: plotHeight)
                .overlay(alignment: .bottom) { Divider().opacity(0.35) }
                .accessibilityLabel("\(projection.providerID) daily estimated cost")
                .accessibilityValue("\(points.count) observed days; peak \(UsageDashboardCostFormat.display(peakPoint.nanos, currency: peakPoint.currency))")
                HStack {
                    Text(points.first?.day ?? "Unknown")
                    Spacer()
                    Text(points.last?.day ?? "Unknown")
                }
                .font(.system(size: 8))
                .foregroundStyle(.secondary)
            }
        }
    }
}

private struct UsageDenseDailyHoverCard: View {
    let point: UsageDailyCostTrendPoint
    let tint: Color

    private var topModels: [UsageDailyModelBreakdown] {
        Array((point.modelBreakdowns ?? []).sorted { lhs, rhs in
            let leftTokens = lhs.totalTokens ?? -1
            let rightTokens = rhs.totalTokens ?? -1
            if leftTokens != rightTokens { return leftTokens > rightTokens }
            return lhs.displayLabel.localizedCaseInsensitiveCompare(rhs.displayLabel) == .orderedAscending
        }.prefix(3))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(point.day)
                .font(.system(size: 9, weight: .bold).monospacedDigit())
            HStack(spacing: 8) {
                Text(UsageDashboardCostFormat.display(point.nanos, currency: point.currency))
                Spacer(minLength: 4)
                Text(UsageFormat.tokens(point.totalTokens))
            }
            .font(.system(size: 9, weight: .semibold).monospacedDigit())
            if topModels.isEmpty {
                Text("No per-day model detail")
                    .font(.system(size: 8.5, weight: .medium))
                    .foregroundStyle(.white.opacity(0.82))
            } else {
                Divider().overlay(Color.white.opacity(0.25))
                ForEach(Array(topModels.enumerated()), id: \.offset) { _, model in
                    HStack(spacing: 6) {
                        Text(model.displayLabel).lineLimit(1)
                        Spacer(minLength: 4)
                        Text(UsageFormat.tokens(model.totalTokens)).monospacedDigit()
                    }
                    .font(.system(size: 8.5, weight: .medium))
                }
            }
        }
        .foregroundStyle(.white)
        .padding(7)
        .background(tint.opacity(0.96), in: RoundedRectangle(cornerRadius: 6, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .stroke(Color.white.opacity(0.28), lineWidth: 0.5)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Daily usage \(point.day), \(UsageDashboardCostFormat.display(point.nanos, currency: point.currency)), \(UsageFormat.tokens(point.totalTokens)) tokens")
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
    let totalEstimatedCostNanos: Int64?
    let onClose: (() -> Void)?
    let onOpenSettings: (() -> Void)?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .center, spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Provider usage")
                        .font(layout.widthClass == .compact ? .title2.bold() : .largeTitle.bold())
                    UsageTotalCostStrip(totalEstimatedCostNanos: totalEstimatedCostNanos)
                }
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

/// Persistent route actions shared by the installed menu app and native cockpit.
private struct UsageDashboardFooter: View {
    let state: UsageDashboardState
    let onRefresh: (() -> Void)?
    let onOpenSettings: (() -> Void)?

    var body: some View {
        HStack(spacing: 10) {
            if state.stale {
                Label("Last good", systemImage: "clock.badge.exclamationmark")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.orange)
            }
            Spacer(minLength: 0)
            if let onRefresh {
                Button(action: onRefresh) {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.plain)
                .help("Refresh provider usage")
                .accessibilityLabel("Refresh provider usage")
            }
            if let onOpenSettings {
                Button(action: onOpenSettings) {
                    Image(systemName: "gearshape")
                }
                .buttonStyle(.plain)
                .help("Provider settings")
                .accessibilityLabel("Provider settings")
            }
        }
        .font(.caption)
        .frame(height: 20)
    }
}

private struct UsageTotalCostStrip: View {
    let totalEstimatedCostNanos: Int64?

    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            Text("Total Tokens Costs")
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(.secondary)
            Text(UsageDashboardCostFormat.display(totalEstimatedCostNanos))
                .font(.system(size: 13, weight: .bold).monospacedDigit())
                .foregroundStyle(.primary)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Total estimated API-rate cost \(UsageDashboardCostFormat.display(totalEstimatedCostNanos))")
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
    private var providerTint: Color {
        UsageProviderVisualStyle.tint(card.id)
    }
    private var connectionNotice: String? {
        guard compactSummary.connected != true || compactSummary.connectionLabel != "Connected" else {
            return nil
        }
        return compactSummary.connectionLabel
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
        HStack(alignment: .center, spacing: 10) {
            Image(UsageProviderVisualStyle.assetName(card.id))
                .resizable()
                .renderingMode(.template)
                .foregroundStyle(providerTint)
                .scaledToFit()
                .frame(width: 18, height: 18)
            Text(compactSummary.displayName).font(.title2.bold())
            Spacer(minLength: 8)
            if let connectionNotice {
                Text(connectionNotice)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(compactSummary.connected == false ? Color.orange : Color.secondary)
            }
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
            UsageCompactQuotaRow(label: "Session", window: compactSummary.session, tint: providerTint)
            UsageCompactQuotaRow(label: "Weekly", window: compactSummary.weekly, tint: providerTint)
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
    let tint: Color

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
                    .tint(tint)
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
        UsageProviderVisualStyle.tint(projection.providerID)
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
