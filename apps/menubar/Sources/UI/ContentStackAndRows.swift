import AppKit


enum PanelAction {
    case setMode(String)
    case pauseAll([String])
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


protocol ContentStack: AnyObject {
    var onAction: ((PanelAction) -> Void)? { get set }
    var onRelayout: (() -> Void)? { get set }
    var contentHeight: CGFloat { get }
    func updateConfig(_ c: Config)
    func updateUsage(_ state: UsageDashboardState)
    func updateSystemTelemetry(_ snapshot: SystemTelemetrySnapshot?)
    func resetSystemTelemetryDisclosure()
    @discardableResult func build(state: MenubarState, into container: NSView) -> [NSView]
}


final class AppKitContentStack: ContentStack {
    private static let telemetryFollowupGap: CGFloat = 4

    var onAction: ((PanelAction) -> Void)?
    var onRelayout: (() -> Void)?
    private(set) var contentHeight: CGFloat = 0

    private var config: Config
    func updateConfig(_ c: Config) { config = c }
    private var usageState = UsageDashboardState()
    func updateUsage(_ state: UsageDashboardState) { usageState = state }
    private var systemTelemetry: SystemTelemetrySnapshot?
    private var systemTelemetryExpanded = false
    private let systemTelemetryHistory = SystemTelemetryHistoryBuffer()
    private weak var systemTelemetryRow: SystemTelemetryRow?
    private weak var inlineSystemTelemetryDetail: InlineSystemTelemetryDetailView?
    func updateSystemTelemetry(_ snapshot: SystemTelemetrySnapshot?) {
        systemTelemetry = snapshot
        systemTelemetryHistory.append(snapshot)
        systemTelemetryRow?.update(snapshot: snapshot, config: config, expanded: systemTelemetryExpanded)
        inlineSystemTelemetryDetail?.update(snapshot: snapshot, history: systemTelemetryHistory.samples)
    }
    func resetSystemTelemetryDisclosure() { systemTelemetryExpanded = false }

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


        place(HeaderView(state: state) { [weak self] in self?.onAction?($0) })
        place(UsagePeekRow(
            UsagePopoverPeekPresentation.make(
                from: usageState,
                showResetETA: config.usageShowResetETA,
                showRunoutETA: config.usageShowRunoutETA,
                showUsed: config.usageBarsShowUsed
            ),
            collapsed: config.usagePeekCollapsed,
            warningMarkerPercent: config.usageWarningMarkersVisible
                ? (config.usageBarsShowUsed ? 100 - config.usageWarningThreshold : config.usageWarningThreshold)
                : nil,
            barPalette: UsageBarPalette.resolve(config.usageBarPalette),
            onOpen: { [weak self] in self?.onAction?(.openUsage) },
            onToggle: { [weak self] in
                self?.onAction?(.setUsagePeekCollapsed(!(self?.config.usagePeekCollapsed ?? false)))
            }
        ))


        if config.systemTelemetryEnabled {
            let telemetryRow = SystemTelemetryRow(
                snapshot: systemTelemetry,
                config: config,
                expanded: systemTelemetryExpanded,
                onOpen: { [weak self] in
                    guard let self else { return }
                    self.systemTelemetryExpanded.toggle()
                    self.onRelayout?()
                }
            )
            systemTelemetryRow = telemetryRow
            place(telemetryRow)
            if systemTelemetryExpanded {
                let detail = InlineSystemTelemetryDetailView(snapshot: systemTelemetry, config: config, history: systemTelemetryHistory.samples)
                inlineSystemTelemetryDetail = detail
                place(detail)
            } else {
                inlineSystemTelemetryDetail = nil
            }
            y += Self.telemetryFollowupGap
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
        func placeAgentGroup(_ rows: [Row]) {
            guard !rows.isEmpty else { return }
            runningGap()
            for (i, r) in rows.enumerated() { placeRow(RunningAgentRow(r, showIcon: i == 0), r) }
            placedGroup = true
        }
        func agentSessionKey(_ r: Row) -> String {
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
    private var consoleEmit: (() -> Void)?
    private var pauseEmit: (() -> Void)?
    init(state: MenubarState, emit: @escaping (PanelAction) -> Void) {
        super.init(frame: NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: 40))
        typealias L = Tokens.Layout


        let logoX = L.rowPadL - 6
        let mark = NSButton(frame: NSRect(x: logoX, y: 6, width: L.wordmarkW, height: L.wordmarkH))


        mark.image = Art.wordmark
        mark.imagePosition = .imageOnly; mark.imageScaling = .scaleProportionallyDown; mark.isBordered = false
        mark.alignment = .left; mark.setButtonType(.momentaryChange)
        mark.target = self; mark.action = #selector(openConsole); addSubview(mark)


        if state.workModel == nil || state.hasProjectionWarning {
            let off = UI.label(state.hasProjectionWarning ? "● stale" : "● offline", size: 9.5, color: Tokens.Color.red)
            off.frame = NSRect(x: L.rowPadL + L.wordmarkW + 8, y: 12, width: 70, height: 14); addSubview(off)
        }


        let sliderRightInset = L.rowPadR
        let sliderW: CGFloat = 70, sliderH: CGFloat = 26
        let slider = ModeSlider(frame: NSRect(x: bounds.width - sliderRightInset - sliderW, y: 23 - sliderH/2, width: sliderW, height: sliderH))
        slider.setLiveMode(state.displayMode, paused: state.displayMode == "pause")
        slider.onSetMode = { emit(.setMode($0)) }
        addSubview(slider)


        let pause = NSButton(frame: NSRect(x: slider.frame.minX - 27, y: 23 - 26/2, width: 28, height: 26))
        pause.title = state.displayMode == "pause" ? "▶" : "⏸"; pause.isBordered = false
        pause.font = .systemFont(ofSize: 16); pause.contentTintColor = Tokens.Color.dimGray
        let ids = (state.workModel?.runningRows ?? []).filter { $0.live == true }.compactMap { $0.jobId ?? $0.id }
        pauseEmit = { emit(.pauseAll(ids)) }
        pause.target = self; pause.action = #selector(doPause); addSubview(pause)

        consoleEmit = { emit(.openCockpit) }
    }
    @objc private func openConsole() { consoleEmit?() }
    @objc private func doPause() { pauseEmit?() }
    required init?(coder: NSCoder) { nil }
}


final class UsagePeekRow: RowView {
    private let toggle: () -> Void
    private let open: () -> Void
    private let barPalette: UsageBarPalette
    private static let quotaRowHeight: CGFloat = 18
    private static let providerFooterHeight: CGFloat = 29
    private static let columnHeaderHeight: CGFloat = 15
    private static let quotaLabelX: CGFloat = 55
    private static let quotaLabelWidth: CGFloat = 47
    private static let quotaLabelBarGap: CGFloat = 4
    private static let quotaTrackWidth: CGFloat = 72
    private static var quotaTrackX: CGFloat { quotaLabelX + quotaLabelWidth + quotaLabelBarGap }
    private static let costLabelX: CGFloat = 202
    private static let costLabelWidth: CGFloat = 36
    private static let costLabelValueGap: CGFloat = 12
    private static var costValueX: CGFloat { costLabelX + costLabelWidth + costLabelValueGap }

    init(
        _ presentation: UsagePopoverPeekPresentation,
        collapsed: Bool,
        warningMarkerPercent: Double? = nil,
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
        let height: CGFloat = collapsed ? 26 : 26 + detailHeight
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

        let chevron = UI.label(collapsed ? "›" : "⌄", size: 12, weight: .semibold, color: .labelColor)
        chevron.frame = NSRect(x: 10, y: 4, width: 11, height: 16)
        addSubview(chevron)
        let heading = UI.label("Usage", size: 9.5, weight: .semibold, color: .labelColor)
        heading.frame = NSRect(x: 24, y: 5, width: 50, height: 14)
        addSubview(heading)
        let disclosure = NSButton(frame: NSRect(x: 7, y: 1, width: 72, height: 23))
        disclosure.title = ""; disclosure.isBordered = false
        disclosure.target = self; disclosure.action = #selector(toggleUsage)
        disclosure.toolTip = collapsed ? "Expand Usage" : "Collapse Usage"
        addSubview(disclosure)
        if collapsed {
            addCollapsedQuotaSummaries(presentation)
            return
        }
        let resetHeader = UI.label("RESET", size: 8, weight: .bold, color: Tokens.Color.sectionGray)
        resetHeader.frame = NSRect(x: 220, y: 27, width: 52, height: 13)
        addSubview(resetHeader)
        let runoutHeader = UI.label("RUNOUT", size: 8, weight: .bold, color: Tokens.Color.sectionGray)
        runoutHeader.frame = NSRect(x: 280, y: 27, width: 60, height: 13)
        addSubview(runoutHeader)

        var providerY: CGFloat = 26 + Self.columnHeaderHeight
        for provider in presentation.providers {
            let color = providerColor(provider.id)
            let mark = ProviderMenuMark.image(for: provider.id)?.copy() as? NSImage
            let icon = UI.imageView(mark, size: 18, tint: color)
            let rowCount = (provider.hasSession ? 1 : 0) + (provider.hasWeekly ? 1 : 0) + (provider.hasFable ? 1 : 0)
            let quotaHeight = max(Self.quotaRowHeight, CGFloat(rowCount) * Self.quotaRowHeight)
            icon.frame = NSRect(x: 22, y: providerY + quotaHeight / 2 - 9, width: 18, height: 18)
            icon.toolTip = provider.displayName
            addSubview(icon)

            var row = 0
            if provider.hasSession {
                addQuotaRow(label: "S", fullLabel: "Session", remaining: provider.sessionRemainingPercent, timing: provider.sessionTiming, color: color, warningMarkerPercent: warningMarkerPercent, y: providerY + CGFloat(row) * Self.quotaRowHeight)
                row += 1
            }
            if provider.hasWeekly {
                addQuotaRow(label: "W", fullLabel: "Weekly", remaining: provider.weeklyRemainingPercent, timing: provider.weeklyTiming, color: color, warningMarkerPercent: warningMarkerPercent, y: providerY + CGFloat(row) * Self.quotaRowHeight)
                row += 1
            }
            if provider.hasFable {
                addQuotaRow(label: "F", fullLabel: "Fable", remaining: provider.fableRemainingPercent, timing: provider.fableTiming, color: color, warningMarkerPercent: warningMarkerPercent, y: providerY + CGFloat(row) * Self.quotaRowHeight)
            }

            let summaryY = providerY + CGFloat(rowCount) * Self.quotaRowHeight
            let costLabel = UI.label("Cost", size: 8.5, weight: .regular, color: Tokens.Color.sectionGray, align: .right)
            costLabel.frame = NSRect(x: Self.costLabelX, y: summaryY + 3, width: Self.costLabelWidth, height: 14)
            let costValue = UI.label(
                usd(provider.retainedUSDEstimateNanos),
                size: 8.5,
                weight: .regular,
                color: Tokens.Color.lightGray,
                align: .right
            )
            costValue.font = .monospacedDigitSystemFont(ofSize: 8.5, weight: .regular)
            costValue.frame = NSRect(x: Self.costValueX, y: summaryY + 3, width: 90, height: 14)
            costLabel.toolTip = "Retained cumulative USD API-rate estimate; not billed subscription spend"
            costValue.toolTip = costLabel.toolTip
            addSubview(costLabel); addSubview(costValue)
            providerY += providerHeight(provider)
        }

    }

    private func addCollapsedQuotaSummaries(_ presentation: UsagePopoverPeekPresentation) {
        let providers = presentation.providers.filter {
            $0.hasSession || $0.hasWeekly || $0.hasFable
        }
        guard !providers.isEmpty else { return }

        let startX: CGFloat = 78
        let slotWidth = (bounds.width - startX - 8) / CGFloat(providers.count)
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
        warningMarkerPercent: Double?,
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
        if let markerPercent = warningMarkerPercent {
            let marker = NSView(frame: NSRect(
                x: min(track.bounds.width - 1, max(0, track.bounds.width * CGFloat(markerPercent) / 100)),
                y: -1,
                width: 1,
                height: track.bounds.height + 2
            ))
            marker.wantsLayer = true
            marker.layer?.backgroundColor = NSColor.systemRed.withAlphaComponent(0.95).cgColor
            track.addSubview(marker)
        }

        let percentage = UI.label(percent(remaining), size: 9.5, weight: .bold, color: quotaColor, align: .right)
        percentage.font = .monospacedDigitSystemFont(ofSize: 9.5, weight: .bold)
        percentage.frame = NSRect(x: 181, y: y, width: 35, height: 16)
        addSubview(percentage)

        let reset = UI.label(timing.resetLabel, size: 8.5, weight: .medium, color: Tokens.Color.sectionGray)
        reset.frame = NSRect(x: 220, y: y, width: 52, height: 16)
        reset.toolTip = timing.accessibilityLabel
        addSubview(reset)
        let runout = UI.label(timing.runoutLabel, size: 8.5, weight: .medium, color: Tokens.Color.sectionGray)
        runout.frame = NSRect(x: 280, y: y, width: 60, height: 16)
        runout.toolTip = timing.accessibilityLabel
        addSubview(runout)
    }

    private func percent(_ value: Double?) -> String {
        guard let value else { return "—" }
        return "\(Int(min(max(value, 0), 100).rounded()))%"
    }

    private func usd(_ nanos: Int64?) -> String {
        guard let nanos else { return "$—" }
        return String(format: "$%.2f", Double(nanos) / 1_000_000_000)
    }

    private func providerColor(_ identity: String) -> NSColor {
        identity.lowercased() == "claude"
            ? NSColor(calibratedRed: 0.96, green: 0.50, blue: 0.32, alpha: 1)
            : NSColor(calibratedRed: 0.58, green: 0.40, blue: 0.96, alpha: 1)
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
    ) -> (text: String, color: NSColor) {
        switch freshness {
        case .unavailable:
            return ("Unavailable", .systemRed)
        case .stale:
            return ("Stale · \(provider.connectionLabel)", .systemOrange)
        case .live:
            if provider.connected == true { return (provider.connectionLabel, .systemGreen) }
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
        super.init(frame: NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: 30))
        refreshEmit = { emit(.refresh) }; gearEmit = { emit(.openSettings) }; detachEmit = { emit(.toggleDetachedPanel) }
        let gear = btn("⚙", x: bounds.width - 28); gear.action = #selector(doGear); addSubview(gear)
        let refr = btn("↻", x: bounds.width - 52); refr.action = #selector(doRefresh); addSubview(refr)
        let detach = btn("⇱", x: bounds.width - 76); detach.action = #selector(doDetach); detach.toolTip = "Pop out panel"; addSubview(detach)
    }
    private func btn(_ t: String, x: CGFloat) -> NSButton {
        let b = NSButton(frame: NSRect(x: x, y: 5, width: 18, height: 18))
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
