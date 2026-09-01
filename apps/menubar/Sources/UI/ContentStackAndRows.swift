import AppKit


enum PanelAction {
    case setMode(String)
    case pauseAll([String])
    case toggleCaffeine
    case openBatteryDetails
    case toggleChargeLimit(expected: Int, target: Int)
    case pauseResume(String, Bool)
    case kill(String)
    case refresh
    case openSettings
    case openUsage
    case setUsagePeekCollapsed(Bool)
    case toggleDetachedPanel
    case openCockpit
    case openDashboard(String)
    case taskAction(job: String, action: String, assignee: String?)
    case handoff(job: String, to: String, task: String, contextRef: String?)
    case capability(job: String, action: String, contextRef: String?)
}

/// Image-only brand control used by the main dropdown. Keeping the action target on the control
/// makes the complete visible wordmark a reliable hit target for mouse and accessibility presses.
final class CockpitWordmarkButton: NSButton {
    private var onPress: (() -> Void)?

    init(
        frame: NSRect,
        image: NSImage?,
        identifier: String,
        accessibilityLabel: String,
        accessibilityHelp: String,
        onPress: @escaping () -> Void
    ) {
        self.onPress = onPress
        super.init(frame: frame)
        self.image = image
        imagePosition = .imageOnly
        imageScaling = .scaleProportionallyDown
        isBordered = false
        alignment = .left
        setButtonType(.momentaryChange)
        self.identifier = NSUserInterfaceItemIdentifier(identifier)
        setAccessibilityLabel(accessibilityLabel)
        setAccessibilityHelp(accessibilityHelp)
        toolTip = accessibilityHelp
        target = self
        action = #selector(pressed)
    }

    @objc private func pressed() { onPress?() }
    required init?(coder: NSCoder) { nil }
}


protocol ContentStack: AnyObject {
    var onAction: ((PanelAction) -> Void)? { get set }
    var onRelayout: (() -> Void)? { get set }
    var contentHeight: CGFloat { get }
    func updateConfig(_ c: Config)
    func updateUsage(_ state: UsageDashboardState)
    func updateSystemTelemetry(_ snapshot: SystemTelemetrySnapshot?)
    func updateLocalPower(battery: LocalBatterySnapshot, caffeineActive: Bool)
    func resetSystemTelemetryDisclosure()
    @discardableResult func build(state: MenubarState, into container: NSView) -> [NSView]
}


final class AppKitContentStack: ContentStack {
    private static let telemetryFollowupGap: CGFloat = 4

    var onAction: ((PanelAction) -> Void)?
    var onRelayout: (() -> Void)?
    private(set) var contentHeight: CGFloat = 0
    private var localBattery = LocalBatterySnapshot.unavailable
    private var caffeineActive = false

    func updateLocalPower(battery: LocalBatterySnapshot, caffeineActive: Bool) {
        localBattery = battery
        self.caffeineActive = caffeineActive
    }

    private var config: Config
    func updateConfig(_ c: Config) {
        config = c
        FooterTelemetryBridge.shared.config = c
    }
    private var usageState = UsageDashboardState()
    func updateUsage(_ state: UsageDashboardState) { usageState = state }
    private var systemTelemetry: SystemTelemetrySnapshot?
    private var systemTelemetryExpanded = false
    private let systemTelemetryHistory = SystemTelemetryHistoryBuffer()
    private weak var inlineSystemTelemetryDetail: InlineSystemTelemetryDetailView?
    func updateSystemTelemetry(_ snapshot: SystemTelemetrySnapshot?) {
        systemTelemetry = snapshot
        FooterTelemetryBridge.shared.update(snapshot: snapshot)
        systemTelemetryHistory.append(snapshot)
        inlineSystemTelemetryDetail?.update(snapshot: snapshot, history: systemTelemetryHistory.samples)
    }
    func resetSystemTelemetryDisclosure() {
        systemTelemetryExpanded = false
        FooterTelemetryBridge.shared.expanded = false
    }

    private var nextCollapsed = false
    private var nextExpanded = false


    private var laterCollapsed = true
    private var laterExpanded = false


    private var backlogCollapsed = true
    private var backlogExpanded = false


    private var initiativesCollapsed = true
    private var provCollapsed: Set<OwnerKind> = []
    private var attentionCollapsed: Bool
    private var followupCollapsed: Bool
    private var localQueueCollapsed: Bool
    private var expandedKey: String?

    init(config: Config) {
        self.config = config
        self.attentionCollapsed = config.attentionCollapsed
        self.followupCollapsed = config.followupCollapsed
        self.localQueueCollapsed = config.localQueueCollapsed
    }

    @discardableResult
    func build(state: MenubarState, into container: NSView) -> [NSView] {
        RowActions.shared = onAction
        RowActions.expand = { [weak self] key in self?.toggleExpand(key) }
        container.subviews.forEach { $0.removeFromSuperview() }
        let W = Tokens.Layout.popoverWidth
        FooterTelemetryBridge.shared.config = config
        FooterTelemetryBridge.shared.expanded = systemTelemetryExpanded
        FooterTelemetryBridge.shared.onToggle = { [weak self] in
            guard let self else { return }
            self.systemTelemetryExpanded.toggle()
            FooterTelemetryBridge.shared.expanded = self.systemTelemetryExpanded
            self.onRelayout?()
        }
        var y: CGFloat = Tokens.Layout.topPad

        func place(_ v: NSView) {
            let x = v.frame.origin.x
            let w = v.frame.width > 0 ? v.frame.width : W
            v.frame = NSRect(x: x, y: y, width: w, height: v.frame.height)
            container.addSubview(v); y += v.frame.height
        }

        func placeRow(_ v: RowView, _ r: Row) {
            v.makeExpandable(r.stableKey)


            if r.stale == true && r.iconOwnerKind != .claude && r.iconOwnerKind != .codex { v.alphaValue = 0.5 }
            place(v)
            if r.stableKey == expandedKey { place(RowDetailView(r, taskActionsEnabled: config.taskActionsEnabled)) }
        }


        place(HeaderView(
            state: state,
            battery: localBattery,
            caffeineActive: caffeineActive
        ) { [weak self] in self?.onAction?($0) })
        place(UsagePeekRow(
            UsagePopoverPeekPresentation.make(
                from: usageState,
                showResetETA: config.usageShowResetETA,
                showRunoutETA: config.usageShowRunoutETA,
                showUsed: config.usageBarsShowUsed
            ),
            collapsed: config.usagePeekCollapsed,
            paceMarkersVisible: config.usageWarningMarkersVisible,
            barPalette: UsageBarPalette.resolve(config.usageBarPalette),
            onOpen: { [weak self] in self?.onAction?(.openUsage) },
            onToggle: { [weak self] in
                self?.onAction?(.setUsagePeekCollapsed(!(self?.config.usagePeekCollapsed ?? false)))
            }
        ))


        if config.systemTelemetryEnabled && systemTelemetryExpanded {
            let detail = InlineSystemTelemetryDetailView(snapshot: systemTelemetry, config: config, history: systemTelemetryHistory.samples)
            inlineSystemTelemetryDetail = detail
            place(detail)
            y += Self.telemetryFollowupGap
        } else {
            inlineSystemTelemetryDetail = nil
        }

        if let hs = state.healthSummary { place(CoordHealthRow(hs)) }
        y += Tokens.Layout.belowHeaderGap

        let wm = state.workModel
        let running = wm?.runningRows ?? []


        if wm == nil {
            let label = (state.error?.isEmpty == false)
                ? "Menu-bar data unavailable"
                : (state.stale == true ? "Stale — last-good unavailable" : "Loading…")
            place(EmptyStateRow(label))
            contentHeight = y
            return container.subviews
        }
        let nextCount = wm?.nextRows?.count ?? 0
        let attentionCount = wm?.attentionRows?.count ?? 0
        let followupCount = wm?.followupRows?.count ?? 0
        var totalWork = running.count
        totalWork += nextCount
        totalWork += attentionCount
        totalWork += followupCount
        totalWork += state.normalizedAgentMilestones.count
        if totalWork == 0 { place(EmptyStateRow("All quiet — no active work")) }


        if config.showVitalsInPopover || state.hasProjectionWarning {
            place(HealthStrip(state: state, showVitals: config.showVitalsInPopover))
        }


        let runRows = dedupRunning((running + state.normalizedAgentMilestones)
            .filter {
                let status = ($0.status ?? "RUNNING").uppercased()
                return status == "RUNNING" || ($0.isAgentCoordination && ["RUNNING", "PAUSED", "STALLED"].contains(status))
            })


        var placedGroup = false
        func runningGap() { if placedGroup { y += Tokens.Layout.groupGap } }

        let barRows = runRows.filter { $0.showsBar }.sorted { ($0.isGPU ? 0 : 1) < ($1.isGPU ? 0 : 1) }
        if !barRows.isEmpty {
            for (i, r) in barRows.enumerated() { placeRow(RunningLocalRow(r, showIcon: i == 0), r) }
            placedGroup = true
        }


        if localQueueHasRows(state) { runningGap(); renderLocalQueue(state, place: place, placeRow: placeRow); placedGroup = true }


        let agents = runRows.filter { !$0.showsBar }
        var needsServerToAgentGap = !barRows.isEmpty
        func placeAgentGroup(_ rows: [Row]) {
            guard !rows.isEmpty else { return }
            if placedGroup { y += needsServerToAgentGap ? Tokens.Layout.serverToAgentGap : Tokens.Layout.groupGap }
            needsServerToAgentGap = false
            for (i, r) in rows.enumerated() { placeRow(RunningAgentRow(r, showIcon: i == 0), r) }
            placedGroup = true
        }
        // One group per orchestrating CHAT is the default here, and the
        // projection's resolved key is what makes that correct: it is the only
        // place that can fold a chat's several registered identities together
        // and roll a subagent's claim up under the chat that spawned it.
        // Deriving the key locally cannot do either, so it is kept only as the
        // fallback for a projection that predates session_group_key -- which
        // degrades to the previous behaviour rather than to an empty view.
        func agentSessionKey(_ r: Row) -> String {
            let resolved = r.sessionGroupKey?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            if !resolved.isEmpty {
                return resolved.lowercased()
            }
            let candidates = [
                r.ownerSessionId,
                r.ownerExternalThreadId,
                r.ownerWorktreeId,
                r.ownerSessionLabel
            ].compactMap { $0?.trimmingCharacters(in: .whitespacesAndNewlines) }
            if let key = candidates.first(where: { !$0.isEmpty }) {
                return "\(r.ownerGroupKind.rawValue):\(key.lowercased())"
            }
            return "\(r.ownerGroupKind.rawValue):owner"
        }
        func placeAgentSessionGroups(_ rows: [Row]) {
            var order: [String] = []
            var grouped: [String: [Row]] = [:]
            for r in rows {
                let key = agentSessionKey(r)
                if grouped[key] == nil { order.append(key) }
                grouped[key, default: []].append(r)
            }
            for key in order { placeAgentGroup(grouped[key] ?? []) }
        }
        placeAgentSessionGroups(agents.filter { $0.ownerGroupKind == .codex })
        placeAgentSessionGroups(agents.filter { $0.ownerGroupKind == .claude })
        placeAgentSessionGroups(agents.filter { $0.ownerGroupKind != .codex && $0.ownerGroupKind != .claude })


        let next = wm?.nextRows ?? []
        let attn = wm?.attentionRows ?? []
        let follow = wm?.followupRows ?? []
        let queuedRows = next.filter { ($0.status ?? "").uppercased() != "PLANNED" }
        let plannedRows = next.filter { ($0.status ?? "").uppercased() == "PLANNED" }


        let upNextPri: (Row) -> Int = { r in
            guard let raw = r.priority?.trimmingCharacters(in: .whitespaces), !raw.isEmpty else { return 0 }
            let digits = (raw.first == "P" || raw.first == "p") ? String(raw.dropFirst()) : raw
            guard let n = Int(digits), n > 0 else { return 0 }
            return n
        }
        let anyRanked = queuedRows.contains { upNextPri($0) > 0 }
        let upNextRows = anyRanked
            ? queuedRows.filter { upNextPri($0) > 0 }.sorted { upNextPri($0) < upNextPri($1) }
            : queuedRows
        let backlogRows = anyRanked ? queuedRows.filter { upNextPri($0) == 0 } : []
        let queuedTotal = anyRanked
            ? max(upNextRows.count, wm?.summary?.nextUpnext ?? 0)
            : max(queuedRows.count, wm?.summary?.nextQueued ?? 0)
        let backlogTotal = max(backlogRows.count, wm?.summary?.nextBacklog ?? 0)
        let plannedTotal = max(plannedRows.count, wm?.summary?.nextPlanned ?? 0)
        if !next.isEmpty {
            if placedGroup { y += Tokens.Layout.sectionGap }
            let h = NextUpHeader(shown: min(upNextRows.count, queuedTotal), total: queuedTotal, collapsed: nextCollapsed)
            h.onToggle = { [weak self] in self?.nextCollapsed.toggle(); self?.onRelayout?() }
            place(h)
            if !nextCollapsed {
                let limitNext = nextExpanded ? config.expandCount : config.nextVisible
                let perCap = nextExpanded ? 10_000 : max(3, limitNext / 2)
                var byProv: [OwnerKind: [Row]] = [:]
                for r in upNextRows { byProv[nextDisplayGroup(r), default: []].append(r) }
                var shownTotal = 0
                let groups: [OwnerKind] = [.claude, .codex, .local]
                    .filter { !(byProv[$0] ?? []).isEmpty }
                for (pi, prov) in groups.enumerated() {
                    if pi > 0 { y += Tokens.Layout.groupGap }
                    let rows = byProv[prov] ?? []
                    let ph = SectionHeader(label: "", count: rows.count,
                                           collapsed: provCollapsed.contains(prov),
                                           ownerIcon: prov == .local ? nil : prov,
                                           appleIcon: prov == .local,
                                           iconOnly: true, font: 11)
                    ph.onToggle = { [weak self] in
                        guard let self else { return }
                        if self.provCollapsed.contains(prov) { self.provCollapsed.remove(prov) } else { self.provCollapsed.insert(prov) }
                        self.onRelayout?()
                    }
                    place(ph)
                    if !provCollapsed.contains(prov) {
                        if rows.isEmpty { place(PlaceholderRow("no queued work")) }
                        for r in rows.prefix(perCap) { placeRow(NextUpRow(r, showIcon: false), r); shownTotal += 1 }
                    }
                }
                let remaining = max(0, queuedTotal - shownTotal)
                if remaining > 0 || nextExpanded {
                    place(MoreRow(nextExpanded ? "Show less  ▴" : "Show \(remaining) more  ▾") { [weak self] in
                        self?.nextExpanded.toggle(); self?.onRelayout?()
                    })
                }


                if let trunc = wm?.truncation?["next_rows"], let total = trunc.total, let shown = trunc.shown, total > shown {
                    place(PlaceholderRow("+\(total - shown) more (truncated)"))
                }
            }


            if !backlogRows.isEmpty {
                y += Tokens.Layout.groupGap
                let bh = SectionHeader(label: "Queued (backlog)", count: backlogTotal, collapsed: backlogCollapsed, font: 9)
                bh.onToggle = { [weak self] in self?.backlogCollapsed.toggle(); self?.onRelayout?() }
                place(bh)
                if !backlogCollapsed {
                    let cap = backlogExpanded ? 10_000 : max(3, config.nextVisible / 2)
                    for r in backlogRows.prefix(cap) { placeRow(NextUpRow(r, showIcon: true), r) }
                    let remainingBacklog = max(0, backlogTotal - min(backlogRows.count, cap))
                    if remainingBacklog > 0 || backlogExpanded {
                        place(MoreRow(backlogExpanded ? "Show less  ▴" : "Show \(remainingBacklog) more  ▾") { [weak self] in
                            self?.backlogExpanded.toggle(); self?.onRelayout?()
                        })
                    }
                }
            }

            if !plannedRows.isEmpty {
                y += Tokens.Layout.groupGap
                let lh = SectionHeader(label: "Later", count: plannedTotal, collapsed: laterCollapsed, font: 9)
                lh.onToggle = { [weak self] in self?.laterCollapsed.toggle(); self?.onRelayout?() }
                place(lh)
                if !laterCollapsed {
                    let cap = laterExpanded ? 10_000 : max(3, config.nextVisible / 2)
                    for r in plannedRows.prefix(cap) { placeRow(NextUpRow(r, showIcon: true), r) }
                    let remainingLater = max(0, plannedTotal - min(plannedRows.count, cap))
                    if remainingLater > 0 || laterExpanded {
                        place(MoreRow(laterExpanded ? "Show less  ▴" : "Show \(remainingLater) more  ▾") { [weak self] in
                            self?.laterExpanded.toggle(); self?.onRelayout?()
                        })
                    }
                }
            }
        }


        let attnTotal = max(attn.count + follow.count,
                            (wm?.summary?.attention ?? 0) + (wm?.summary?.followup ?? 0))
        if attnTotal > 0 {
            y += Tokens.Layout.sectionGap
            let h = SectionHeader(label: "Needs Attention", count: attnTotal, collapsed: attentionCollapsed, font: 9)
            h.onToggle = { [weak self] in
                guard let self else { return }
                self.attentionCollapsed.toggle()
                if !self.attentionCollapsed { self.followupCollapsed = false }
                self.onRelayout?()
            }
            place(h)
            if !attentionCollapsed {
                for r in attn { placeRow(AttentionRow(r), r) }
                if !follow.isEmpty {
                    let fh = SectionHeader(label: "Follow-up", count: follow.count, collapsed: followupCollapsed, font: 8.8)
                    fh.onToggle = { [weak self] in self?.followupCollapsed.toggle(); self?.onRelayout?() }
                    place(fh)
                    if !followupCollapsed { for r in follow { placeRow(AttentionRow(r, inset: 10), r) } }
                }
            }
        }


        if let epics = wm?.hierarchy?.epics, !epics.isEmpty {
            y += Tokens.Layout.sectionGap
            let ih = SectionHeader(label: "By Initiative", count: epics.count, collapsed: initiativesCollapsed, font: 9)
            ih.onToggle = { [weak self] in self?.initiativesCollapsed.toggle(); self?.onRelayout?() }
            place(ih)
            if !initiativesCollapsed {
                for epic in epics {
                    let label = epic.label ?? epic.domainShortLabel ?? epic.id ?? "Unassigned"
                    place(InitiativeRow(label: label, counts: epic.counts))
                    for job in (epic.jobs ?? []) {
                        placeRow(AttentionRow(job, suppressDot: true, inset: 12), job)
                        for task in (job.tasks ?? []) {
                            placeRow(AttentionRow(task, suppressDot: true, inset: 24), task)
                        }
                        if let tt = job.tasksTruncated, tt > 0 {
                            place(PlaceholderRow("+\(tt) more tasks"))
                        }
                    }
                    if let jt = epic.jobsTruncated, jt > 0 {
                        place(PlaceholderRow("+\(jt) more jobs"))
                    }
                }
            }
        }

        contentHeight = y
        return container.subviews
    }

    private func toggleExpand(_ key: String) {
        expandedKey = (expandedKey == key) ? nil : key
        onRelayout?()
    }


    private func dedupRunning(_ rows: [Row]) -> [Row] {
        var index: [String: Int] = [:]
        var out: [Row] = []
        for r in rows {
            let k = runKey(r)
            if let i = index[k] {
                if preferRunning(r, over: out[i]) { out[i] = r }
            } else {
                index[k] = out.count
                out.append(r)
            }
        }
        return out
    }
    private func runKey(_ r: Row) -> String {
        if let rid = r.roadmapId, !rid.isEmpty { return rid.lowercased() }
        if let k = r.dedupKey, !k.isEmpty { return k.lowercased() }
        if let j = r.jobId ?? r.id, !j.isEmpty { return j.lowercased() }
        return (r.display ?? r.name ?? "").lowercased().trimmingCharacters(in: .whitespaces)
    }
    private func preferRunning(_ a: Row, over b: Row) -> Bool {


        if a.telemetryRichness != b.telemetryRichness { return a.telemetryRichness > b.telemetryRichness }


        if a.detailRichness != b.detailRichness { return a.detailRichness > b.detailRichness }
        if (a.stale == true) != (b.stale == true) { return a.stale != true }
        return false
    }

    private func nextDisplayGroup(_ r: Row) -> OwnerKind {
        let localTokens: Set<String> = ["gpu", "mlx", "cuda", "cpu", "ram", "disk", "api", "local"]
        let explicit = [
            r.kind, r.lane, r.rowKind, r.workKind, r.platform
        ].compactMap { $0?.lowercased() }
        if r.isGPU || r.isLocalProcess || explicit.contains("local_job") || explicit.contains("local") {
            return .local
        }
        if explicit.contains(where: { localTokens.contains($0) }) {
            return .local
        }
        return r.ownerGroupKind
    }


    private func localQueueHasRows(_ state: MenubarState) -> Bool {
        guard let lanes = state.normalizedLocalLanes?.lanes else { return false }
        return ["gpu", "cpu", "ram", "disk", "api", "local"].contains {
            !((lanes[$0]?.ready ?? []) + (lanes[$0]?.held ?? [])).isEmpty
        }
    }

    private func renderLocalQueue(_ state: MenubarState, place: (NSView) -> Void, placeRow: (RowView, Row) -> Void) {
        guard let lanes = state.normalizedLocalLanes?.lanes else { return }
        let order = ["gpu", "cpu", "ram", "disk", "api", "local"]
        func laneRows(_ k: String) -> [Row] { (lanes[k]?.ready ?? []) + (lanes[k]?.held ?? []) }
        let present = order.filter { !laneRows($0).isEmpty }
        let total = present.reduce(0) { $0 + laneRows($1).count }
        guard total > 0 else { return }

        let h = SectionHeader(label: "Local Queue", count: total, collapsed: localQueueCollapsed, appleIcon: true)
        h.onToggle = { [weak self] in self?.localQueueCollapsed.toggle(); self?.onRelayout?() }
        place(h)
        guard !localQueueCollapsed else { return }

        let suppressLaneHeaders = present.count <= 1
        let cap = 4
        for lane in present {
            let rows = laneRows(lane)
            if !suppressLaneHeaders { place(LaneHeader(lane.uppercased(), count: rows.count)) }
            for r in rows.prefix(cap) { placeRow(queueRow(r), r) }
            if rows.count > cap { place(PlaceholderRow("+\(rows.count - cap) more", height: Tokens.Layout.attnRowH)) }
        }
    }


    private func queueRow(_ r: Row) -> RowView {
        switch r.queueRowKind {
        case .running:   return RunningLocalRow(r, showIcon: false)
        case .attention: return AttentionRow(r, suppressDot: true)
        case .done, .next: return NextUpRow(r, showIcon: false)
        }
    }

}


final class HeaderView: RowView {
    init(
        state: MenubarState,
        battery: LocalBatterySnapshot,
        caffeineActive: Bool,
        emit: @escaping (PanelAction) -> Void
    ) {
        super.init(frame: NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: Tokens.Layout.headerHeight))
        typealias L = Tokens.Layout


        let logoX = L.rowPadL - 6
        let mark = CockpitWordmarkButton(
            frame: NSRect(x: logoX, y: 8, width: L.wordmarkW, height: L.wordmarkH),
            image: Art.wordmark,
            identifier: "main.coord-wordmark",
            accessibilityLabel: "Open COORD Cockpit",
            accessibilityHelp: "Open COORD's full native Cockpit window."
        ) {
            emit(.openCockpit)
        }
        addSubview(mark)


        if state.workModel == nil || state.hasProjectionWarning {
            let off = UI.label(state.hasProjectionWarning ? "● stale" : "● offline", size: 9.5, color: Tokens.Color.red)
            off.frame = NSRect(x: L.rowPadL + L.wordmarkW + 8, y: 12, width: 70, height: 14); addSubview(off)
        }


        let ids = (state.workModel?.runningRows ?? []).filter { $0.live == true }.compactMap { $0.jobId ?? $0.id }
        let controls = CoordPowerControlsView(
            frame: NSRect(
                x: bounds.width - L.rowPadR - L.headerControlsWidth,
                y: 7,
                width: L.headerControlsWidth,
                height: L.headerControlsHeight
            ),
            battery: battery,
            caffeineActive: caffeineActive,
            mode: state.displayMode
        )
        controls.identifier = NSUserInterfaceItemIdentifier("coord.header.power-controls")
        controls.onToggleChargeLimit = { expected, target in
            emit(.toggleChargeLimit(expected: expected, target: target))
        }
        controls.onToggleCaffeine = { emit(.toggleCaffeine) }
        controls.onSetMode = { mode in
            if mode == "pause" { emit(.pauseAll(ids)) }
            else { emit(.setMode(mode)) }
        }
        addSubview(controls)

    }
    required init?(coder: NSCoder) { nil }
}


final class UsagePeekRow: RowView {
    private let toggle: () -> Void
    private let open: () -> Void
    private let barPalette: UsageBarPalette
    private static let quotaRowHeight: CGFloat = 18
    private static let providerFooterHeight: CGFloat = 22
    private static let columnHeaderHeight: CGFloat = 14
    private static let headerHeight: CGFloat = 28
    private static let quotaLabelX: CGFloat = 55
    private static let providerIconX: CGFloat = Tokens.Layout.rowPadL
    private static let quotaLabelWidth: CGFloat = 13
    private static let quotaLabelBarGap: CGFloat = 4
    private static let quotaTrackWidth: CGFloat = 119
    private static var quotaTrackX: CGFloat { quotaLabelX + quotaLabelWidth + quotaLabelBarGap }
    private static let percentageX: CGFloat = 195
    private static let percentageWidth: CGFloat = 36
    private static let resetX: CGFloat = 239
    private static let resetWidth: CGFloat = 64
    private static let runoutX: CGFloat = 311
    private static let runoutWidth: CGFloat = 64
    private static let costValueX: CGFloat = 55
    private static let costValueWidth: CGFloat = 132
    private static let stateX: CGFloat = 196
    private static let stateWidth: CGFloat = 179

    init(
        _ presentation: UsagePopoverPeekPresentation,
        collapsed: Bool,
        paceMarkersVisible: Bool = true,
        barPalette: UsageBarPalette = .colored,
        onOpen: @escaping () -> Void,
        onToggle: @escaping () -> Void
    ) {
        self.toggle = onToggle
        self.open = onOpen
        self.barPalette = barPalette
        let width = Tokens.Layout.popoverWidth
        let providerHeight: (UsagePopoverPeekProvider) -> CGFloat = { provider in
            let rows = (provider.hasSession ? 1 : 0) + (provider.hasWeekly ? 1 : 0) + (provider.hasFable ? 1 : 0)
            return Self.providerFooterHeight + CGFloat(rows) * Self.quotaRowHeight
        }
        let detailHeight = Self.columnHeaderHeight + presentation.providers.reduce(CGFloat(0)) { $0 + providerHeight($1) }
        let height: CGFloat = collapsed ? Self.headerHeight : Self.headerHeight + detailHeight
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: height))
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor
        let disclosureState = collapsed ? "collapsed" : "expanded"
        toolTip = presentation.accessibilityLabel
        setAccessibilityElement(true)
        setAccessibilityRole(.button)
        setAccessibilityLabel("Usage section, \(disclosureState). \(presentation.accessibilityLabel)")
        setAccessibilityHelp(collapsed ? "Expands inline provider usage." : "Collapses inline provider usage.")
        addGestureRecognizer(NSClickGestureRecognizer(target: self, action: #selector(openUsage)))

        let heading = UI.label("USAGE", size: 9.5, weight: .semibold, color: .labelColor)
        heading.frame = NSRect(x: 31, y: 8, width: 45, height: 14)
        addSubview(heading)
        let chevron = UI.label(collapsed ? "›" : "⌄", size: 12, weight: .semibold, color: .labelColor)
        chevron.frame = NSRect(x: 77, y: 7, width: 11, height: 16)
        addSubview(chevron)
        let disclosure = NSButton(frame: NSRect(x: 25, y: 3, width: 68, height: 22))
        disclosure.title = ""; disclosure.isBordered = false
        disclosure.target = self; disclosure.action = #selector(toggleUsage)
        disclosure.toolTip = collapsed ? "Expand Usage" : "Collapse Usage"
        disclosure.identifier = NSUserInterfaceItemIdentifier("coord.usage.disclosure")
        addSubview(disclosure)
        let details = NSButton(frame: NSRect(x: bounds.width - 27, y: 4, width: 19, height: 19))
        details.title = "↗"; details.isBordered = false; details.font = .systemFont(ofSize: 10)
        details.contentTintColor = Tokens.Color.sectionGray
        details.target = self; details.action = #selector(openUsage)
        details.identifier = NSUserInterfaceItemIdentifier("coord.usage.details")
        details.toolTip = "Open Usage details"
        addSubview(details)
        if collapsed {
            addCollapsedQuotaSummaries(presentation)
            return
        }
        let resetHeader = UI.label("RESET", size: 7.5, weight: .semibold, color: Tokens.Color.sectionGray, align: .center)
        resetHeader.frame = NSRect(x: Self.resetX, y: Self.headerHeight, width: Self.resetWidth, height: 13)
        addSubview(resetHeader)
        let runoutHeader = UI.label("RUNOUT", size: 7.5, weight: .semibold, color: Tokens.Color.sectionGray, align: .center)
        runoutHeader.frame = NSRect(x: Self.runoutX, y: Self.headerHeight, width: Self.runoutWidth, height: 13)
        addSubview(runoutHeader)

        var providerY: CGFloat = Self.headerHeight + Self.columnHeaderHeight
        for provider in presentation.providers {
            let color = providerColor(provider.id)
            let mark = ProviderMenuMark.image(for: provider.id)?.copy() as? NSImage
            let icon = UI.imageView(mark, size: 18, tint: color)
            let rowCount = (provider.hasSession ? 1 : 0) + (provider.hasWeekly ? 1 : 0) + (provider.hasFable ? 1 : 0)
            icon.frame = NSRect(x: Self.providerIconX, y: providerY + (Self.quotaRowHeight - 18) / 2, width: 18, height: 18)
            icon.toolTip = provider.displayName
            addSubview(icon)

            var row = 0
            if provider.hasSession {
                addQuotaRow(label: "S", fullLabel: "Session", remaining: provider.sessionRemainingPercent, timing: provider.sessionTiming, color: color, paceMarkersVisible: paceMarkersVisible, y: providerY + CGFloat(row) * Self.quotaRowHeight)
                row += 1
            }
            if provider.hasWeekly {
                addQuotaRow(label: "W", fullLabel: "Weekly", remaining: provider.weeklyRemainingPercent, timing: provider.weeklyTiming, color: color, paceMarkersVisible: paceMarkersVisible, y: providerY + CGFloat(row) * Self.quotaRowHeight)
                row += 1
            }
            if provider.hasFable {
                addQuotaRow(label: "F", fullLabel: "Fable", remaining: provider.fableRemainingPercent, timing: provider.fableTiming, color: color, paceMarkersVisible: paceMarkersVisible, y: providerY + CGFloat(row) * Self.quotaRowHeight)
            }

            let summaryY = providerY + CGFloat(rowCount) * Self.quotaRowHeight
            let costValue = UI.label(
                usd(provider.retainedUSDEstimateNanos),
                size: 8.5,
                weight: .regular,
                color: Tokens.Color.lightGray,
                align: .right
            )
            costValue.font = .monospacedDigitSystemFont(ofSize: 8.5, weight: .regular)
            costValue.frame = NSRect(x: Self.costValueX, y: summaryY + 3, width: Self.costValueWidth, height: 14)
            costValue.toolTip = "Retained cumulative USD API-rate estimate; not billed subscription spend"
            addSubview(costValue)
            if let state = visibleState(provider, freshness: presentation.freshness) {
                let stateLabel = UI.label(state.text, size: 8.5, weight: .medium, color: state.color, align: .right)
                stateLabel.frame = NSRect(x: Self.stateX, y: summaryY + 3, width: Self.stateWidth, height: 14)
                stateLabel.toolTip = provider.displayName + ": " + state.text
                addSubview(stateLabel)
            }
            providerY += providerHeight(provider)
        }

    }

    private func addCollapsedQuotaSummaries(_ presentation: UsagePopoverPeekPresentation) {
        let startX: CGFloat = 104
        let providers = presentation.providers.filter {
            $0.hasSession || $0.hasWeekly || $0.hasFable
        }
        guard !providers.isEmpty else {
            let state = presentation.providers.map { $0.connectionLabel }.joined(separator: " · ")
            let label = UI.label(state.isEmpty ? "Usage unavailable" : state, size: 8.5, weight: .medium, color: Tokens.Color.sectionGray, align: .right)
            label.frame = NSRect(x: startX, y: 7, width: bounds.width - startX - 32, height: 14)
            addSubview(label)
            return
        }

        let slotWidth = (bounds.width - startX - 32) / CGFloat(providers.count)
        for (index, provider) in providers.enumerated() {
            let x = startX + CGFloat(index) * slotWidth
            let color = providerColor(provider.id)
            let mark = ProviderMenuMark.image(for: provider.id)?.copy() as? NSImage
            let icon = UI.imageView(mark, size: 12, tint: color)
            icon.frame = NSRect(x: x, y: 7, width: 12, height: 12)
            icon.toolTip = provider.displayName
            addSubview(icon)

            var items: [String] = []
            if provider.hasSession { items.append("S\(percent(provider.sessionRemainingPercent))") }
            if provider.hasWeekly { items.append("W\(percent(provider.weeklyRemainingPercent))") }
            if provider.hasFable { items.append("F\(percent(provider.fableRemainingPercent))") }
            let summary = UI.label(items.joined(separator: "  "), size: 8.5, weight: .semibold, color: color)
            summary.font = .monospacedDigitSystemFont(ofSize: 8.5, weight: .semibold)
            summary.frame = NSRect(x: x + 16, y: 6, width: slotWidth - 18, height: 14)
            summary.lineBreakMode = .byClipping
            summary.toolTip = provider.displayName + ": " + items.joined(separator: ", ")
            addSubview(summary)
        }
    }
    private func addQuotaRow(
        label: String,
        fullLabel: String,
        remaining: Double?,
        timing: UsageCompactQuotaTiming,
        color: NSColor,
        paceMarkersVisible: Bool,
        y: CGFloat
    ) {
        let labelView = UI.label(label, size: 9.5, weight: .semibold, color: Tokens.Color.lightGray, align: .right)
        labelView.frame = NSRect(x: Self.quotaLabelX, y: y, width: Self.quotaLabelWidth, height: 16)
        labelView.toolTip = fullLabel
        addSubview(labelView)

        let quotaColor = barPalette == .colored ? color : NSColor.labelColor.withAlphaComponent(0.82)
        let track = NSView(frame: NSRect(x: Self.quotaTrackX, y: y + 6, width: Self.quotaTrackWidth, height: 5))
        track.wantsLayer = true
        track.layer?.cornerRadius = 2.5
        track.layer?.backgroundColor = NSColor.labelColor.withAlphaComponent(0.16).cgColor
        addSubview(track)
        if let remaining {
            let clamped = min(max(remaining, 0), 100)
            let fill = NSView(frame: NSRect(x: 0, y: 0, width: max(1, track.bounds.width * CGFloat(clamped) / 100), height: 5))
            fill.wantsLayer = true
            fill.layer?.cornerRadius = 2.5
            fill.layer?.backgroundColor = quotaColor.cgColor
            track.addSubview(fill)
        }
        if paceMarkersVisible, let markerPercent = timing.paceMarkerPercent {
            let marker = NSView(frame: NSRect(
                x: min(track.bounds.width - 1, max(0, track.bounds.width * CGFloat(markerPercent) / 100)),
                y: -1,
                width: 1,
                height: track.bounds.height + 2
            ))
            marker.wantsLayer = true
            marker.layer?.backgroundColor = (timing.paceMarkerIsDeficit ? NSColor.systemRed : NSColor.systemGreen)
                .withAlphaComponent(0.95).cgColor
            track.addSubview(marker)
        }

        let percentage = UI.label(percent(remaining), size: 9.5, weight: .bold, color: quotaColor, align: .right)
        percentage.font = .monospacedDigitSystemFont(ofSize: 9.5, weight: .bold)
        percentage.frame = NSRect(x: Self.percentageX, y: y, width: Self.percentageWidth, height: 16)
        addSubview(percentage)

        let reset = UI.label(timing.resetLabel, size: 8.5, weight: .medium, color: Tokens.Color.sectionGray, align: .center)
        reset.frame = NSRect(x: Self.resetX, y: y, width: Self.resetWidth, height: 16)
        reset.font = .monospacedDigitSystemFont(ofSize: 8.5, weight: .medium)
        reset.toolTip = timing.accessibilityLabel
        addSubview(reset)
        let runout = UI.label(timing.runoutLabel, size: 8.5, weight: .medium, color: Tokens.Color.sectionGray, align: .center)
        runout.frame = NSRect(x: Self.runoutX, y: y, width: Self.runoutWidth, height: 16)
        runout.font = .monospacedDigitSystemFont(ofSize: 8.5, weight: .medium)
        runout.toolTip = timing.accessibilityLabel
        addSubview(runout)
    }

    private func percent(_ value: Double?) -> String {
        guard let value else { return "—" }
        return "\(Int(min(max(value, 0), 100).rounded()))%"
    }

    private func usd(_ nanos: Int64?) -> String {
        guard let nanos else { return "$—" }
        return UsageFormat.costNanos(nanos, currency: "USD")
            .replacingOccurrences(of: "USD ", with: "$")
    }

    private func providerColor(_ identity: String) -> NSColor {
        identity.lowercased() == "claude"
            ? NSColor(srgbRed: 0.95, green: 0.47, blue: 0.24, alpha: 1)
            : NSColor(srgbRed: 0.64, green: 0.43, blue: 0.96, alpha: 1)
    }

    private func freshnessLabel(_ freshness: UsagePeekFreshness) -> (text: String, color: NSColor) {
        switch freshness {
        case .live: ("LIVE", .systemGreen)
        case .stale: ("STALE", .systemOrange)
        case .unavailable: ("UNAVAILABLE", .systemRed)
        }
    }

    private func visibleState(
        _ provider: UsagePopoverPeekProvider,
        freshness: UsagePeekFreshness
    ) -> (text: String, color: NSColor)? {
        switch freshness {
        case .unavailable:
            return ("Unavailable", .systemRed)
        case .stale:
            return ("Stale", .systemOrange)
        case .live:
            if provider.connected == true { return nil }
            if provider.connected == false { return (provider.connectionLabel, .systemOrange) }
            return (provider.connectionLabel, Tokens.Color.sectionGray)
        }
    }

    @objc private func openUsage() { open() }
    @objc private func toggleUsage() { toggle() }

    override func accessibilityPerformPress() -> Bool {
        toggle()
        return true
    }

    required init?(coder: NSCoder) { nil }
}


final class FooterView: RowView {
    private var refreshEmit: (() -> Void)?; private var gearEmit: (() -> Void)?; private var detachEmit: (() -> Void)?
    init(emit: @escaping (PanelAction) -> Void) {
        super.init(frame: NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: Tokens.Layout.footerHeight))
        refreshEmit = { emit(.refresh) }; gearEmit = { emit(.openSettings) }; detachEmit = { emit(.toggleDetachedPanel) }
        let telemetryState = FooterTelemetryBridge.shared
        if telemetryState.config.systemTelemetryEnabled {
            let telemetry = SystemTelemetryRow(
                snapshot: telemetryState.snapshot,
                config: telemetryState.config,
                expanded: telemetryState.expanded,
                panelWidth: Tokens.Layout.telemetryRailWidth,
                onOpen: { telemetryState.onToggle?() }
            )
            telemetry.frame.origin = .zero
            telemetryState.row = telemetry
            addSubview(telemetry)
        }
        let gear = btn("⚙", x: bounds.width - 28); gear.action = #selector(doGear); gear.identifier = NSUserInterfaceItemIdentifier("coord.footer.settings"); addSubview(gear)
        let refr = btn("↻", x: bounds.width - 52); refr.action = #selector(doRefresh); refr.identifier = NSUserInterfaceItemIdentifier("coord.footer.refresh"); addSubview(refr)
        let detach = btn("⇱", x: bounds.width - 76); detach.action = #selector(doDetach); detach.identifier = NSUserInterfaceItemIdentifier("coord.footer.detach"); detach.toolTip = "Pop out panel"; addSubview(detach)
    }
    private func btn(_ t: String, x: CGFloat) -> NSButton {
        let b = NSButton(frame: NSRect(x: x, y: 7, width: 18, height: 20))
        b.title = t; b.isBordered = false; b.font = .systemFont(ofSize: 12)
        b.contentTintColor = Tokens.Color.sectionGray; b.target = self; return b
    }
    @objc private func doRefresh() { refreshEmit?() }
    @objc private func doGear() { gearEmit?() }
    @objc private func doDetach() { detachEmit?() }
    required init?(coder: NSCoder) { nil }
}


final class MoreRow: RowView {
    private let tapped: () -> Void
    init(_ text: String, _ tapped: @escaping () -> Void) {
        self.tapped = tapped
        super.init(frame: NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: Tokens.Layout.nextRowH))
        let lab = UI.label(text, size: 10, color: Tokens.Color.dimGray.withAlphaComponent(0.85))
        lab.frame = NSRect(x: Tokens.Layout.titleX + 16, y: 7, width: 200, height: 14); addSubview(lab)
        let b = NSButton(frame: bounds); b.isBordered = false; b.title = ""
        b.target = self; b.action = #selector(go); b.autoresizingMask = [.width]; addSubview(b)
    }
    @objc private func go() { tapped() }
    required init?(coder: NSCoder) { nil }
}
