import AppKit

protocol CockpitMapViewDelegate: AnyObject {
    func cockpitMapView(_ view: CockpitMapView, open path: String)
}

final class CockpitMapView: NSView {
    weak var delegate: CockpitMapViewDelegate?

    private enum Mode: Int, CaseIterable {
        case intelligence
        case myCockpit
        case pipeline
        case portfolio
        case system
        case models
        case knowledge
        case funnel
        case moat
        case runway
        case changes

        var label: String {
            switch self {
            case .intelligence: return "Intelligence"
            case .myCockpit: return "My Cockpit"
            case .pipeline: return "Pipeline"
            case .portfolio: return "Portfolio"
            case .system: return "System"
            case .models: return "Models"
            case .knowledge: return "Knowledge"
            case .funnel: return "Funnel"
            case .moat: return "Moat"
            case .runway: return "Runway"
            case .changes: return "Changes"
            }
        }
    }

    private enum Selection: Equatable {
        case product(String)
        case model(String)
        case node(String)
        case integrity(String)
        case alert(String)
    }

    private let modeControl = CockpitSegmentedControl(items: Mode.allCases.map(\.label))
    private let title = CockpitUI.label("Operator Map", size: 15, weight: .bold, color: CockpitTokens.Color.text)
    private let subtitle = CockpitUI.label("", size: 11.5, weight: .medium, color: CockpitTokens.Color.muted)
    private let integrityStrip = CockpitMapIntegrityStrip()
    private let summaryStrip = CockpitMapSummaryStrip()
    private let snapshotPopup = NSPopUpButton()
    private let scrollView = CockpitEdgeScrollView()
    private let contentView = CockpitMapFlippedView()
    private let detailPanel = CockpitMapDetailPanel()
    private let endpointSource = CockpitMapEndpointSource()
    private var state: CockpitMapState?
    private var knowledgeGraph: CockpitMapKnowledgeGraph?
    private var machineHealth: CockpitMachineHealth?
    private var provenanceByVertical: [String: CockpitMapProvenance] = [:]
    private var loadingProvenance = Set<String>()
    private var knowledgeGraphLoadStarted = false
    private var machineHealthLoadStarted = false
    private var mode: Mode = .intelligence
    private var selection: Selection?
    private var selectedSnapshot: String?
    private var lastContentSignature: String?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.backgroundColor = CockpitTokens.Color.bg.cgColor

        modeControl.selectedIndex = mode.rawValue
        modeControl.onSelection = { [weak self] index in
            guard let next = Mode(rawValue: index) else { return }
            self?.mode = next
            self?.selection = nil
            self?.loadMapOverlaysForCurrentModeIfNeeded()
            self?.rebuild()
        }
        addSubview(title)
        addSubview(subtitle)
        addSubview(modeControl)
        addSubview(integrityStrip)
        addSubview(summaryStrip)
        snapshotPopup.target = self
        snapshotPopup.action = #selector(snapshotChanged)
        snapshotPopup.font = .systemFont(ofSize: 11, weight: .semibold)
        snapshotPopup.bezelStyle = .rounded
        addSubview(snapshotPopup)

        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = false
        scrollView.documentView = contentView
        CockpitScrollChrome.apply(to: scrollView)
        addSubview(scrollView)

        detailPanel.onOpen = { [weak self] path in
            guard let self else { return }
            self.delegate?.cockpitMapView(self, open: path)
        }
        addSubview(detailPanel)

        integrityStrip.onSelect = { [weak self] warning in
            self?.selection = .integrity(warning.surface)
            self?.syncDetail()
        }
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    func render(_ state: CockpitMapState) {
        self.state = state
        loadMapOverlaysForCurrentModeIfNeeded()
        if let selection, selectedObject(for: selection) == nil {
            self.selection = nil
        }
        rebuild()
    }

    override func layout() {
        super.layout()
        let pad: CGFloat = 34
        let headerY: CGFloat = 18
        title.frame = NSRect(x: pad, y: headerY, width: min(280, max(120, bounds.width * 0.24)), height: 24)
        let subtitleWidth = min(680, max(280, bounds.width - pad * 2 - title.frame.width - 24))
        subtitle.frame = NSRect(x: bounds.width - pad - subtitleWidth, y: headerY, width: subtitleWidth, height: 24)
        let modeY: CGFloat = 54
        modeControl.frame = NSRect(x: pad, y: modeY, width: max(320, bounds.width - pad * 2), height: 38)

        let detailWidth: CGFloat = bounds.width > 920 ? min(340, max(280, bounds.width * 0.26)) : 0
        let bodyY: CGFloat = 108
        let contentWidth = max(160, bounds.width - detailWidth - pad * 2 - 12)
        integrityStrip.frame = NSRect(x: pad, y: bodyY, width: contentWidth, height: 48)
        summaryStrip.frame = NSRect(x: pad, y: bodyY + 58, width: contentWidth, height: 52)
        let snapshotY = bodyY + 118
        snapshotPopup.frame = NSRect(x: pad, y: snapshotY, width: min(320, contentWidth), height: 30)
        let scrollY = bodyY + (summaryStrip.isHidden ? 60 : (snapshotPopup.isHidden ? 122 : 158))
        scrollView.frame = NSRect(
            x: pad,
            y: scrollY,
            width: max(180, bounds.width - detailWidth - pad * 2 - 12),
            height: max(140, bounds.height - scrollY - 18)
        )
        if detailWidth > 0 {
            detailPanel.isHidden = false
            detailPanel.frame = NSRect(x: bounds.width - detailWidth - pad, y: bodyY, width: detailWidth, height: max(180, bounds.height - bodyY - 18))
        } else {
            detailPanel.isHidden = true
            detailPanel.frame = .zero
        }
        layoutContent()
    }

    private func rebuild() {
        guard let state else {
            subtitle.stringValue = "No map projection"
            integrityStrip.configure([])
            detailPanel.render(nil, state: nil, provenanceByVertical: [:])
            lastContentSignature = nil
            clearContent()
            return
        }
        subtitle.stringValue = mapSubtitle(state)
        integrityStrip.configure(state.integrityWarnings)
        summaryStrip.configure(state.summary)
        summaryStrip.isHidden = !(mode == .pipeline || mode == .portfolio || mode == .funnel || mode == .intelligence || mode == .myCockpit)
        configureSnapshotPopup(state)
        let nextContentSignature = contentSignature(for: state)
        if nextContentSignature != lastContentSignature {
            rebuildContent(for: state)
            lastContentSignature = nextContentSignature
        }
        syncDetail()
        needsLayout = true
    }

    private func contentSignature(for state: CockpitMapState) -> String {
        [
            "mode=\(mode.rawValue)",
            "snapshot=\(selectedSnapshot ?? "current")",
            "generated=\(state.generatedAt ?? "-")",
            "trust=\(state.trustScore.map(String.init) ?? "-")",
            "products=\(state.products.count)",
            "models=\(state.models.count)",
            "integrity=\(state.integrityWarnings.count)",
            "nodes=\(state.systemNodes.count)",
            "edges=\(state.systemEdges.count)",
            "intel=\(state.intelligence.count)",
            "funnel=\(state.funnel.count)",
            "moat=\(state.moatAssets.count)",
            "runway=\(state.runwayItems.count)",
            "history=\(state.history.count)",
            "changes=\(state.changes.count)",
            "alerts=\(state.alerts.count)",
            "error=\(state.error?.message ?? "-")",
            "health=\(machineHealth?.light ?? "-"):\(machineHealth?.mode ?? "-")",
            "graph=\(knowledgeGraph?.nodes.count ?? 0):\(knowledgeGraph?.edges.count ?? 0)",
        ].joined(separator: "|")
    }

    private func rebuildContent(for state: CockpitMapState) {
        clearContent()
        switch mode {
        case .intelligence:
            renderIntelligence(state)
        case .myCockpit:
            renderMyCockpit(state)
        case .pipeline:
            renderPipeline(state)
        case .portfolio:
            renderPortfolio(state)
        case .system:
            renderSystem(state)
        case .models:
            renderModels(state)
        case .knowledge:
            renderKnowledge(state)
        case .funnel:
            renderFunnel(state)
        case .moat:
            renderMoat(state)
        case .runway:
            renderRunway(state)
        case .changes:
            renderChanges(state)
        }
    }

    private func clearContent() {
        contentView.subviews.forEach { $0.removeFromSuperview() }
    }

    private func renderIntelligence(_ state: CockpitMapState) {
        for alert in state.alerts.prefix(4) {
            contentView.addSubview(alertCard(alert))
        }
        if !state.trustHistory.isEmpty {
            contentView.addSubview(CockpitMapTrendChartView(
                title: "Trust trend",
                rows: state.trustHistory,
                tint: trustTint(state.trustScore)
            ))
        }
        if let machineHealth {
            contentView.addSubview(healthCard(machineHealth))
        }
        contentView.addSubview(infoCard(
            title: "Trust score",
            eyebrow: "MAP META",
            body: state.trustScore.map { "\($0) / 100" } ?? "not materialized",
            foot: state.trustHistory.isEmpty ? "No trust history rows" : "Trend: " + state.trustHistory.compactMap(\.metricValue).joined(separator: " -> "),
            tint: trustTint(state.trustScore),
            badge: trustBand(state.trustScore)
        ))
        if let top = state.nextDollarRows.first {
            contentView.addSubview(intelligenceCard(top, eyebrow: "NEXT DOLLAR", tint: CockpitTokens.Color.green))
        }
        for row in state.nextDollarRows.dropFirst().prefix(3) {
            contentView.addSubview(intelligenceCard(row, eyebrow: "RUNNER-UP", tint: CockpitTokens.Color.blue2))
        }
        for row in state.actionRows.prefix(8) {
            contentView.addSubview(intelligenceCard(row, eyebrow: "TODAY", tint: CockpitTokens.Color.amber))
        }
        for row in state.wireRows.prefix(8) {
            contentView.addSubview(intelligenceCard(row, eyebrow: "DARK WIRE", tint: CockpitTokens.Color.violet))
        }
    }

    private func renderMyCockpit(_ state: CockpitMapState) {
        contentView.addSubview(infoCard(title: "Summary", eyebrow: "WIDGET", body: "\(state.products.count) products  \(state.models.count) models", foot: "Trust \(state.trustScore.map(String.init) ?? "-")  Integrity \(state.integrityWarnings.count)", tint: CockpitTokens.Color.blue2, badge: "PINNED"))
        if let next = state.nextDollarRows.first { contentView.addSubview(intelligenceCard(next, eyebrow: "WIDGET", tint: CockpitTokens.Color.green)) }
        for row in state.actionRows.prefix(3) { contentView.addSubview(intelligenceCard(row, eyebrow: "ACTION", tint: CockpitTokens.Color.amber)) }
        for stage in state.funnel { contentView.addSubview(funnelCard(stage)) }
        for change in state.changes.prefix(4) { contentView.addSubview(changeCard(change)) }
    }

    private func renderPipeline(_ state: CockpitMapState) {
        for bucket in state.pipelineBuckets {
            let column = CockpitMapColumnView(title: bucketTitle(bucket.stage), count: bucket.products.count)
            for product in bucket.products {
                column.addCard(productCard(product))
            }
            contentView.addSubview(column)
        }
    }

    private func renderPortfolio(_ state: CockpitMapState) {
        for product in portfolioProducts(state) {
            contentView.addSubview(productCard(product, style: .portfolio))
        }
    }

    private func renderSystem(_ state: CockpitMapState) {
        if let risk = topSystemRisk(state) {
            contentView.addSubview(infoCard(
                title: risk.node.name,
                eyebrow: "RISK / SPOF",
                body: "\(risk.reach) downstream surfaces over wired edges",
                foot: risk.node.sizeNote,
                tint: CockpitTokens.Color.red,
                badge: risk.node.layer.uppercased()
            ))
        }
        for lane in state.systemLanes {
            let column = CockpitMapColumnView(title: laneTitle(lane.layer), count: lane.nodes.count)
            for node in lane.nodes {
                column.addCard(nodeCard(node, edges: state.systemEdges))
            }
            contentView.addSubview(column)
        }
    }

    private func renderModels(_ state: CockpitMapState) {
        contentView.addSubview(CockpitMapControlBoardView(title: "Models", state: state, mode: "Dark value + saved views"))
        contentView.addSubview(CockpitMapIntegrityGapBoardView(warnings: state.integrityWarnings))
        contentView.addSubview(CockpitMapModelValueChartView(models: state.models))
        let sorted = state.models.sorted { lhs, rhs in
            if modelRank(lhs.status) != modelRank(rhs.status) { return modelRank(lhs.status) < modelRank(rhs.status) }
            return lhs.name.localizedCaseInsensitiveCompare(rhs.name) == .orderedAscending
        }
        for model in sorted {
            contentView.addSubview(modelCard(model))
        }
    }

    private func renderFunnel(_ state: CockpitMapState) {
        contentView.addSubview(CockpitMapControlBoardView(title: "Funnel", state: state, mode: "Stage conversion"))
        contentView.addSubview(CockpitMapFunnelChartView(stages: state.funnel))
        for stage in state.funnel {
            contentView.addSubview(funnelCard(stage))
        }
    }

    private func renderKnowledge(_ state: CockpitMapState) {
        contentView.addSubview(CockpitMapControlBoardView(title: "Knowledge", state: state, mode: "Products -> models -> facts"))
        if let graph = knowledgeGraph {
            let graphView = CockpitKnowledgeGraphView(graph: graph)
            graphView.onSelect = { [weak self] node in
                guard let self else { return }
                if node.type == "product", let vertical = node.vertical ?? node.id.split(separator: ":").last.map(String.init) {
                    self.selection = .product(vertical)
                    self.rebuild()
                } else if node.type == "model" {
                    let name = self.state?.models.first(where: { $0.name == node.label || $0.name == node.id })?.name ?? node.label
                    self.selection = .model(name)
                    self.rebuild()
                }
            }
            contentView.addSubview(graphView)
            let products = graph.nodes.filter { $0.type == "product" }.count
            let models = graph.nodes.filter { $0.type == "model" }.count
            let facts = graph.nodes.filter { $0.type == "fact" }.count
            contentView.addSubview(infoCard(
                title: "Knowledge graph",
                eyebrow: "PRODUCTS / MODELS / FACTS",
                body: "\(products) products  \(models) models  \(facts) facts",
                foot: "\(graph.edges.count) lineage edges; facts include supersession history where published",
                tint: CockpitTokens.Color.blue2,
                badge: "GRAPH"
            ))
            for node in graph.nodes.prefix(12) {
                contentView.addSubview(infoCard(
                    title: node.label,
                    eyebrow: node.type.uppercased(),
                    body: [node.vertical, node.stage, node.sellable == true ? "sellable" : nil].compactMap { $0 }.joined(separator: "  "),
                    foot: node.id,
                    tint: color(forStatus: node.status ?? node.type),
                    badge: (node.status ?? node.type).uppercased()
                ))
            }
        } else {
            contentView.addSubview(infoCard(
                title: "Knowledge graph loading",
                eyebrow: "WAVE 8-12",
                body: "Fetching /api/map/graph for products -> models -> facts lineage",
                foot: "Core map state remains read from map.db",
                tint: CockpitTokens.Color.blue2,
                badge: "GRAPH"
            ))
        }
        if let machineHealth {
            contentView.addSubview(healthCard(machineHealth))
        }
    }

    private func renderMoat(_ state: CockpitMapState) {
        contentView.addSubview(CockpitMapControlBoardView(title: "Moat", state: state, mode: "Asset strength"))
        contentView.addSubview(CockpitMapMoatMatrixView(assets: state.moatAssets))
        for asset in state.moatAssets {
            contentView.addSubview(infoCard(
                title: asset.asset,
                eyebrow: [asset.moatType?.uppercased(), asset.vertical?.uppercased()].compactMap { $0 }.joined(separator: " / "),
                body: "Strength \(asset.strength.map(String.init) ?? "-")  Value \(asset.valueScore.map(String.init) ?? "-")",
                foot: asset.note,
                tint: asset.isAtRisk ? CockpitTokens.Color.amber : CockpitTokens.Color.green,
                badge: asset.isAtRisk ? "AT RISK" : "MOAT"
            ))
        }
    }

    private func renderRunway(_ state: CockpitMapState) {
        contentView.addSubview(CockpitMapControlBoardView(title: "Runway", state: state, mode: "Local capacity"))
        contentView.addSubview(CockpitMapRunwayBoardView(items: state.runwayItems, health: machineHealth))
        for item in state.runwayItems {
            contentView.addSubview(infoCard(
                title: item.item,
                eyebrow: item.category.uppercased(),
                body: [item.sizeNote, item.status].compactMap { $0 }.joined(separator: "  "),
                foot: item.note,
                tint: color(forStatus: item.status ?? ""),
                badge: (item.status ?? item.category).uppercased()
            ))
        }
    }

    private func renderChanges(_ state: CockpitMapState) {
        contentView.addSubview(CockpitMapControlBoardView(title: "Changes", state: state, mode: "Alerts + changelog"))
        contentView.addSubview(CockpitMapChangeTimelineView(changes: state.changes, alerts: state.alerts))
        for alert in state.alerts {
            contentView.addSubview(alertCard(alert))
        }
        for warning in state.integrityWarnings {
            let card = infoCard(
                title: warning.surface,
                eyebrow: "INTEGRITY",
                body: "\(warning.servedValue ?? "-") -> \(warning.honestValue ?? "-")",
                foot: warning.note,
                tint: CockpitTokens.Color.red,
                badge: (warning.severity ?? "flag").uppercased()
            )
            card.onPress = { [weak self] in
                self?.selection = .integrity(warning.surface)
                self?.rebuild()
            }
            contentView.addSubview(card)
        }
        for change in state.changes {
            contentView.addSubview(changeCard(change))
        }
    }

    private func productCard(_ product: CockpitMapProduct, style: CockpitMapCard.Style = .compact) -> CockpitMapCard {
        let card = CockpitMapCard(style: style)
        card.configure(
            title: product.displayName,
            eyebrow: product.vertical.uppercased(),
            body: product.metricLine,
            foot: product.metricHonestNote ?? product.status ?? product.blurb,
            tint: color(forHealth: product.health),
            badge: product.isSellable ? "SOLD" : product.stage.uppercased(),
            isSelected: selection == .product(product.vertical)
        )
        card.onPress = { [weak self] in
            self?.selection = .product(product.vertical)
            self?.rebuild()
        }
        return card
    }

    private func modelCard(_ model: CockpitMapModel) -> CockpitMapCard {
        let card = CockpitMapCard(style: .model)
        card.configure(
            title: model.name,
            eyebrow: (model.vertical ?? "unmapped").uppercased(),
            body: [model.metric, model.metricValue].compactMap { $0 }.joined(separator: "  "),
            foot: model.ledgerRef,
            tint: color(forStatus: model.status),
            badge: model.status.uppercased(),
            trailing: [model.effort, model.valueScore.map { "BV \($0)" }].compactMap { $0 }.joined(separator: "  "),
            isSelected: selection == .model(model.name)
        )
        card.onPress = { [weak self] in
            self?.selection = .model(model.name)
            self?.rebuild()
        }
        return card
    }

    private func nodeCard(_ node: CockpitMapSystemNode, edges: [CockpitMapSystemEdge]) -> CockpitMapCard {
        let downstream = edges.filter { $0.fromID == node.id }
        let darkEdges = downstream.filter { $0.status.lowercased() == "dark" }.count
        let card = CockpitMapCard(style: .node)
        card.configure(
            title: node.name,
            eyebrow: node.id,
            body: node.sizeNote,
            foot: downstream.isEmpty ? "terminal" : "\(downstream.count) downstream\(darkEdges > 0 ? " / \(darkEdges) dark" : "")",
            tint: color(forStatus: node.status),
            badge: node.status.uppercased(),
            isSelected: selection == .node(node.id)
        )
        card.onPress = { [weak self] in
            self?.selection = .node(node.id)
            self?.rebuild()
        }
        return card
    }

    private func intelligenceCard(_ row: CockpitMapIntelligenceRow, eyebrow: String, tint: NSColor) -> CockpitMapCard {
        let score = row.score.map { String(format: "%.1f", $0) }
        let card = infoCard(
            title: row.title,
            eyebrow: [eyebrow, row.vertical?.uppercased()].compactMap { $0 }.joined(separator: " / "),
            body: [score.map { "Score \($0)" }, row.effort.map { "Effort \($0)" }, row.impact.map { "Impact \($0)" }].compactMap { $0 }.joined(separator: "  "),
            foot: row.detail,
            tint: tint,
            badge: row.ctaKind?.uppercased() ?? row.kind.uppercased()
        )
        if row.ctaKind == "product", let vertical = row.ctaTarget {
            card.onPress = { [weak self] in
                self?.selection = .product(vertical)
                self?.rebuild()
            }
        } else if row.ctaKind == "model", let model = row.ctaTarget {
            card.onPress = { [weak self] in
                self?.selection = .model(model)
                self?.rebuild()
            }
        } else if row.ctaKind == "integrity", let surface = row.ctaTarget {
            card.onPress = { [weak self] in
                self?.selection = .integrity(surface)
                self?.rebuild()
            }
        }
        return card
    }

    private func funnelCard(_ stage: CockpitMapFunnelStage) -> CockpitMapCard {
        infoCard(
            title: bucketTitle(stage.stage),
            eyebrow: "FUNNEL",
            body: "\(stage.productCount) products  \(stage.sellableCount) sellable",
            foot: [stage.verticals, stage.note].compactMap { $0 }.joined(separator: " - "),
            tint: color(forStage: stage.stage),
            badge: stage.stage.uppercased()
        )
    }

    private func changeCard(_ change: CockpitMapChange) -> CockpitMapCard {
        infoCard(
            title: change.entity,
            eyebrow: [change.entityKind.uppercased(), change.field.uppercased()].joined(separator: " / "),
            body: "\(change.oldValue ?? "-") -> \(change.newValue ?? "-")",
            foot: change.timestamp,
            tint: color(forSeverity: change.severity),
            badge: (change.severity ?? "info").uppercased()
        )
    }

    private func alertCard(_ alert: CockpitMapAlert) -> CockpitMapCard {
        let card = infoCard(
            title: alert.title,
            eyebrow: ["ALERT", alert.kind.uppercased()].joined(separator: " / "),
            body: alert.detail,
            foot: [alert.ctaKind, alert.ctaTarget].compactMap { $0 }.joined(separator: " -> "),
            tint: color(forAlertLevel: alert.level),
            badge: alert.level.uppercased()
        )
        card.onPress = { [weak self] in
            self?.selection = .alert(alert.id)
            self?.rebuild()
        }
        return card
    }

    private func healthCard(_ health: CockpitMachineHealth) -> CockpitMapCard {
        infoCard(
            title: "Machine health",
            eyebrow: "LOCAL HARNESS",
            body: [
                health.mode.map { "mode \($0)" },
                health.freeRAMGB.map { String(format: "%.1f GB free", $0) },
                health.load1m.map { String(format: "load %.1f", $0) },
            ].compactMap { $0 }.joined(separator: "  "),
            foot: health.reasons?.joined(separator: ", "),
            tint: color(forHealth: health.light),
            badge: health.light.uppercased()
        )
    }

    private func infoCard(title: String, eyebrow: String, body: String?, foot: String?, tint: NSColor, badge: String) -> CockpitMapCard {
        let card = CockpitMapCard(style: .portfolio)
        card.configure(title: title, eyebrow: eyebrow, body: body, foot: foot, tint: tint, badge: badge, isSelected: false)
        return card
    }

    private func layoutContent() {
        let width = scrollView.contentSize.width
        guard width > 10 else { return }
        switch mode {
        case .pipeline, .system:
            layoutColumns(width: width)
        case .portfolio, .intelligence, .myCockpit:
            layoutGrid(width: width, minWidth: 286, height: 156)
        case .funnel, .moat, .runway, .changes:
            layoutRows(width: width, height: 156)
        case .knowledge:
            layoutRows(width: width, height: 320)
        case .models:
            layoutRows(width: width, height: 94)
        }
    }

    private func loadMapOverlaysForCurrentModeIfNeeded() {
        loadMachineHealthIfNeeded()
        loadKnowledgeGraphIfNeeded()
    }

    private func loadMachineHealthIfNeeded() {
        guard !machineHealthLoadStarted else { return }
        machineHealthLoadStarted = true
        Task { [weak self] in
            guard let self else { return }
            let loadedHealth = await endpointSource.loadMachineHealth()
            await MainActor.run {
                self.machineHealth = loadedHealth
                self.rebuild()
            }
        }
    }

    private func loadKnowledgeGraphIfNeeded() {
        guard mode == .knowledge else { return }
        guard !knowledgeGraphLoadStarted else { return }
        knowledgeGraphLoadStarted = true
        Task { [weak self] in
            guard let self else { return }
            let loadedGraph = await endpointSource.loadKnowledgeGraph()
            await MainActor.run {
                self.knowledgeGraph = loadedGraph
                guard self.mode == .knowledge else { return }
                self.rebuild()
            }
        }
    }

    private func loadProvenanceIfNeeded(_ vertical: String) {
        guard !vertical.isEmpty, provenanceByVertical[vertical] == nil, !loadingProvenance.contains(vertical) else { return }
        loadingProvenance.insert(vertical)
        Task { [weak self] in
            guard let self else { return }
            let provenance = await endpointSource.loadProvenance(vertical: vertical)
            await MainActor.run {
                self.loadingProvenance.remove(vertical)
                if let provenance {
                    self.provenanceByVertical[vertical] = provenance
                }
                self.syncDetail()
            }
        }
    }

    private func layoutColumns(width: CGFloat) {
        let gap: CGFloat = 12
        let columns = contentView.subviews
        let columnWidth = max(178, floor((width - CGFloat(max(0, columns.count - 1)) * gap) / CGFloat(max(1, columns.count))))
        var x: CGFloat = 0
        var maxHeight: CGFloat = 0
        for view in columns {
            let preferred = (view as? CockpitMapColumnView)?.preferredHeight(width: columnWidth) ?? 300
            view.frame = NSRect(x: x, y: 0, width: columnWidth, height: preferred)
            x += columnWidth + gap
            maxHeight = max(maxHeight, preferred)
        }
        contentView.frame = NSRect(x: 0, y: 0, width: width, height: max(scrollView.contentSize.height, maxHeight + 8))
    }

    private func layoutGrid(width: CGFloat, minWidth: CGFloat, height: CGFloat) {
        let gap: CGFloat = 12
        let count = max(1, Int(floor((width + gap) / (minWidth + gap))))
        let cardWidth = floor((width - CGFloat(count - 1) * gap) / CGFloat(count))
        for (idx, view) in contentView.subviews.enumerated() {
            let col = idx % count
            let row = idx / count
            view.frame = NSRect(x: CGFloat(col) * (cardWidth + gap), y: CGFloat(row) * (height + gap), width: cardWidth, height: height)
        }
        let rows = Int(ceil(Double(contentView.subviews.count) / Double(count)))
        contentView.frame = NSRect(x: 0, y: 0, width: width, height: max(scrollView.contentSize.height, CGFloat(rows) * (height + gap) + 8))
    }

    private func layoutRows(width: CGFloat, height: CGFloat) {
        let gap: CGFloat = 10
        var y: CGFloat = 0
        for view in contentView.subviews {
            let rowHeight = (view as? CockpitMapSizedSurface)?.preferredMapHeight
                ?? (view as? CockpitMapColumnView)?.preferredHeight(width: width)
                ?? height
            view.frame = NSRect(x: 0, y: y, width: width, height: rowHeight)
            y += rowHeight + gap
        }
        contentView.frame = NSRect(x: 0, y: 0, width: width, height: max(scrollView.contentSize.height, y + 8))
    }

    private func syncDetail() {
        if case .product(let vertical)? = selection {
            loadProvenanceIfNeeded(vertical)
        } else if case .model(let name)? = selection,
                  let vertical = state?.models.first(where: { $0.name == name })?.vertical {
            loadProvenanceIfNeeded(vertical)
        }
        detailPanel.render(selection.flatMap(selectedObject), state: state, provenanceByVertical: provenanceByVertical)
    }

    private func selectedObject(for selection: Selection) -> CockpitMapSelectedObject? {
        guard let state else { return nil }
        switch selection {
        case .product(let vertical):
            guard let product = state.product(vertical: vertical) else { return nil }
            let models = state.models.filter { $0.vertical == vertical || $0.powers == vertical }
            return .product(product, models)
        case .model(let name):
            guard let model = state.models.first(where: { $0.name == name }) else { return nil }
            return .model(model)
        case .node(let id):
            guard let node = state.systemNodes.first(where: { $0.id == id }) else { return nil }
            let edges = state.systemEdges.filter { $0.fromID == id || $0.toID == id }
            return .node(node, edges)
        case .integrity(let surface):
            guard let warning = state.integrityWarnings.first(where: { $0.surface == surface }) else { return nil }
            return .integrity(warning)
        case .alert(let id):
            guard let alert = state.alerts.first(where: { $0.id == id }) else { return nil }
            return .alert(alert)
        }
    }

    private func mapSubtitle(_ state: CockpitMapState) -> String {
        if let error = state.error {
            return "Map load error: \(error.message)"
        }
        let darkProducts = state.products.filter { $0.health.lowercased() == "dark" }.count
        let alertText = state.criticalAlertCount > 0 ? "  \(state.criticalAlertCount) critical" : ""
        return "\(state.products.count) products  \(state.models.count) models  \(darkProducts) dark\(alertText)  \(state.generatedAt ?? "no timestamp")"
    }

    private func configureSnapshotPopup(_ state: CockpitMapState) {
        snapshotPopup.isHidden = mode != .portfolio || state.snapshotTimestamps.isEmpty
        guard !snapshotPopup.isHidden else { return }
        let currentTitle = "Current"
        let currentItems = [currentTitle] + state.snapshotTimestamps.reversed()
        let selectedTitle = selectedSnapshot ?? currentTitle
        if snapshotPopup.itemTitles != currentItems {
            snapshotPopup.removeAllItems()
            snapshotPopup.addItems(withTitles: currentItems)
        }
        snapshotPopup.selectItem(withTitle: selectedTitle)
        snapshotPopup.toolTip = "Portfolio time travel from materialized map_history snapshots"
    }

    private func portfolioProducts(_ state: CockpitMapState) -> [CockpitMapProduct] {
        guard let selectedSnapshot else { return state.products }
        return state.productsSnapshot(timestamp: selectedSnapshot)
    }

    @objc private func snapshotChanged() {
        selectedSnapshot = snapshotPopup.titleOfSelectedItem == "Current" ? nil : snapshotPopup.titleOfSelectedItem
        rebuild()
    }

    private func bucketTitle(_ stage: String) -> String {
        switch stage {
        case "idea": return "Idea"
        case "built": return "Built"
        case "wired": return "Wired"
        case "live": return "Live"
        case "sold": return "Sold"
        default: return stage.capitalized
        }
    }

    private func laneTitle(_ layer: String) -> String {
        switch layer {
        case "source": return "Sources"
        case "pipeline": return "Pipelines"
        case "model": return "Models"
        case "surface": return "Surfaces"
        default: return layer.capitalized
        }
    }

    private func color(forHealth health: String) -> NSColor {
        switch health.lowercased() {
        case "green", "live": return CockpitTokens.Color.green
        case "yellow", "amber", "wired": return CockpitTokens.Color.amber
        case "red", "retired": return CockpitTokens.Color.red
        case "dark", "built": return CockpitTokens.Color.violet
        default: return CockpitTokens.Color.blue2
        }
    }

    private func color(forStatus status: String) -> NSColor {
        switch status.lowercased() {
        case "live", "wired": return CockpitTokens.Color.green
        case "dark": return CockpitTokens.Color.violet
        case "retired", "red": return CockpitTokens.Color.red
        default: return CockpitTokens.Color.amber
        }
    }

    private func color(forStage stage: String) -> NSColor {
        switch stage.lowercased() {
        case "live", "sold": return CockpitTokens.Color.green
        case "wired": return CockpitTokens.Color.blue2
        case "built": return CockpitTokens.Color.violet
        case "idea": return CockpitTokens.Color.amber
        default: return CockpitTokens.Color.muted
        }
    }

    private func color(forSeverity severity: String?) -> NSColor {
        switch (severity ?? "").lowercased() {
        case "alert", "critical", "high": return CockpitTokens.Color.red
        case "watch", "medium": return CockpitTokens.Color.amber
        default: return CockpitTokens.Color.blue2
        }
    }

    private func color(forAlertLevel level: String) -> NSColor {
        switch level.lowercased() {
        case "critical", "alert": return CockpitTokens.Color.red
        case "warn", "warning": return CockpitTokens.Color.amber
        default: return CockpitTokens.Color.blue2
        }
    }

    private func trustTint(_ trustScore: Int?) -> NSColor {
        guard let trustScore else { return CockpitTokens.Color.muted }
        if trustScore >= 80 { return CockpitTokens.Color.green }
        if trustScore >= 60 { return CockpitTokens.Color.amber }
        return CockpitTokens.Color.red
    }

    private func trustBand(_ trustScore: Int?) -> String {
        guard let trustScore else { return "UNKNOWN" }
        if trustScore >= 80 { return "GREEN" }
        if trustScore >= 60 { return "AMBER" }
        return "RED"
    }

    private func topSystemRisk(_ state: CockpitMapState) -> (node: CockpitMapSystemNode, reach: Int)? {
        let nodesByID = Dictionary(uniqueKeysWithValues: state.systemNodes.map { ($0.id, $0) })
        let wiredEdges = state.systemEdges.filter { $0.status.lowercased() == "wired" }
        let adjacency = Dictionary(grouping: wiredEdges, by: \.fromID).mapValues { $0.map(\.toID) }
        func surfaceReach(from id: String) -> Int {
            var seen: Set<String> = []
            var stack = adjacency[id] ?? []
            var surfaces: Set<String> = []
            while let next = stack.popLast() {
                guard !seen.contains(next) else { continue }
                seen.insert(next)
                if nodesByID[next]?.layer == "surface" {
                    surfaces.insert(next)
                }
                stack.append(contentsOf: adjacency[next] ?? [])
            }
            return surfaces.count
        }
        return state.systemNodes
            .filter { $0.layer != "surface" }
            .map { ($0, surfaceReach(from: $0.id)) }
            .filter { $0.1 > 0 }
            .max { lhs, rhs in lhs.1 < rhs.1 }
    }

    private func modelRank(_ status: String) -> Int {
        switch status.lowercased() {
        case "dark": return 0
        case "live": return 1
        case "retired": return 3
        default: return 2
        }
    }
}

private enum CockpitMapSelectedObject {
    case product(CockpitMapProduct, [CockpitMapModel])
    case model(CockpitMapModel)
    case node(CockpitMapSystemNode, [CockpitMapSystemEdge])
    case integrity(CockpitMapIntegrityWarning)
    case alert(CockpitMapAlert)
}

private protocol CockpitMapSizedSurface {
    var preferredMapHeight: CGFloat { get }
}

private extension CockpitMapProduct {
    var metricLine: String {
        [headlineMetric, metricValue].compactMap { value in
            let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            return trimmed.isEmpty ? nil : trimmed
        }.joined(separator: "  ")
    }
}

private final class CockpitMapFlippedView: NSView {
    override var isFlipped: Bool { true }
}

private final class CockpitMapColumnView: NSView {
    private let header = CockpitUI.label("", size: 11, weight: .bold, color: CockpitTokens.Color.faint)
    private let countLabel = CockpitUI.label("", size: 11, weight: .semibold, color: CockpitTokens.Color.blue2, align: .right)
    private var cards: [CockpitMapCard] = []

    init(title: String, count: Int) {
        super.init(frame: .zero)
        wantsLayer = true
        layer?.cornerRadius = 12
        layer?.backgroundColor = CockpitTokens.Color.panel.withAlphaComponent(0.36).cgColor
        layer?.borderColor = CockpitTokens.Color.line.withAlphaComponent(0.20).cgColor
        layer?.borderWidth = 1
        header.stringValue = title.uppercased()
        countLabel.stringValue = "\(count)"
        addSubview(header)
        addSubview(countLabel)
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    func addCard(_ card: CockpitMapCard) {
        cards.append(card)
        addSubview(card)
    }

    func preferredHeight(width: CGFloat) -> CGFloat {
        46 + CGFloat(cards.count) * 102 + CGFloat(max(0, cards.count - 1)) * 10 + 14
    }

    override func layout() {
        header.frame = NSRect(x: 14, y: 12, width: bounds.width - 70, height: 20)
        countLabel.frame = NSRect(x: bounds.width - 52, y: 12, width: 34, height: 20)
        var y: CGFloat = 44
        for card in cards {
            card.frame = NSRect(x: 10, y: y, width: bounds.width - 20, height: 92)
            y += 102
        }
    }
}

private final class CockpitMapCard: NSButton {
    enum Style {
        case compact
        case portfolio
        case model
        case node
    }

    var onPress: (() -> Void)?
    private let cardStyle: Style
    private var cardTitle = ""
    private var eyebrow = ""
    private var body = ""
    private var foot = ""
    private var badge = ""
    private var trailing = ""
    private var tint = CockpitTokens.Color.blue2
    private var selected = false

    init(style: Style) {
        self.cardStyle = style
        super.init(frame: .zero)
        isBordered = false
        title = ""
        target = self
        action = #selector(pressed)
        wantsLayer = true
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    func configure(title: String, eyebrow: String, body: String?, foot: String?, tint: NSColor, badge: String, trailing: String = "", isSelected: Bool) {
        self.cardTitle = title
        self.eyebrow = eyebrow
        self.body = body ?? ""
        self.foot = foot ?? ""
        self.tint = tint
        self.badge = badge
        self.trailing = trailing
        self.selected = isSelected
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        let rect = bounds.insetBy(dx: 0.5, dy: 0.5)
        let path = NSBezierPath(roundedRect: rect, xRadius: 10, yRadius: 10)
        let gradient = NSGradient(colors: [
            CockpitTokens.Color.panel2.withAlphaComponent(selected ? 0.96 : 0.70),
            NSColor.black.withAlphaComponent(0.10),
        ])
        gradient?.draw(in: path, angle: -90)
        (selected ? tint.withAlphaComponent(0.56) : CockpitTokens.Color.line.withAlphaComponent(0.22)).setStroke()
        path.lineWidth = selected ? 1.2 : 0.8
        path.stroke()

        tint.withAlphaComponent(selected ? 0.90 : 0.68).setFill()
        NSBezierPath(roundedRect: NSRect(x: rect.minX, y: rect.minY + 10, width: 3, height: max(20, rect.height - 20)), xRadius: 1.5, yRadius: 1.5).fill()

        drawText(eyebrow, x: 13, y: 10, width: rect.width - 26, size: 9.5, weight: .bold, color: tint.withAlphaComponent(0.94))
        drawText(cardTitle, x: 13, y: 28, width: rect.width - 26, size: cardStyle == .portfolio ? 13.5 : 12.5, weight: .bold, color: CockpitTokens.Color.text)
        if !body.isEmpty {
            drawText(body, x: 13, y: cardStyle == .portfolio ? 54 : 50, width: rect.width - 26, size: 11.2, weight: .semibold, color: CockpitTokens.Color.muted)
        }
        if !foot.isEmpty {
            drawText(foot, x: 13, y: cardStyle == .portfolio ? 78 : 70, width: rect.width - 26, size: 10.5, weight: .medium, color: CockpitTokens.Color.faint)
        }
        drawBadge()
        if !trailing.isEmpty {
            drawText(trailing, x: rect.width - 95, y: 10, width: 80, size: 10, weight: .bold, color: CockpitTokens.Color.muted, align: .right)
        }
    }

    private func drawBadge() {
        guard !badge.isEmpty else { return }
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 9, weight: .bold),
            .foregroundColor: CockpitTokens.Color.text,
        ]
        let text = NSString(string: badge)
        let size = text.size(withAttributes: attrs)
        let rect = NSRect(x: bounds.width - size.width - 22, y: bounds.height - 25, width: size.width + 12, height: 16)
        let path = NSBezierPath(roundedRect: rect, xRadius: 5, yRadius: 5)
        tint.withAlphaComponent(0.17).setFill()
        path.fill()
        tint.withAlphaComponent(0.34).setStroke()
        path.lineWidth = 0.8
        path.stroke()
        text.draw(in: rect.insetBy(dx: 6, dy: 2), withAttributes: attrs)
    }

    private func drawText(_ text: String, x: CGFloat, y: CGFloat, width: CGFloat, size: CGFloat, weight: NSFont.Weight, color: NSColor, align: NSTextAlignment = .left) {
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineBreakMode = .byTruncatingTail
        paragraph.alignment = align
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: size, weight: weight),
            .foregroundColor: color,
            .paragraphStyle: paragraph,
        ]
        NSString(string: text).draw(in: NSRect(x: x, y: y, width: width, height: size + 5), withAttributes: attrs)
    }

    @objc private func pressed() {
        onPress?()
    }
}

private final class CockpitMapIntegrityStrip: NSView {
    var onSelect: ((CockpitMapIntegrityWarning) -> Void)?
    private var warnings: [CockpitMapIntegrityWarning] = []
    private var buttons: [NSButton] = []
    private let title = CockpitUI.label("Integrity", size: 11, weight: .bold, color: CockpitTokens.Color.faint)

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.cornerRadius = 13
        layer?.backgroundColor = CockpitTokens.Color.panel.withAlphaComponent(0.36).cgColor
        layer?.borderColor = CockpitTokens.Color.red.withAlphaComponent(0.16).cgColor
        layer?.borderWidth = 1
        addSubview(title)
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    func configure(_ warnings: [CockpitMapIntegrityWarning]) {
        self.warnings = warnings
        buttons.forEach { $0.removeFromSuperview() }
        buttons = warnings.map { warning in
            let button = CockpitButton(title: "\(warning.surface): \(warning.servedValue ?? "?") -> \(warning.honestValue ?? "?")", target: self, action: #selector(pressed(_:)))
            button.font = .systemFont(ofSize: 11, weight: .bold)
            button.contentTintColor = CockpitTokens.Color.red
            button.toolTip = warning.note
            button.wantsLayer = true
            button.layer?.backgroundColor = CockpitTokens.Color.red.withAlphaComponent(0.060).cgColor
            button.layer?.borderColor = CockpitTokens.Color.red.withAlphaComponent(0.20).cgColor
            addSubview(button)
            return button
        }
        needsLayout = true
    }

    override func layout() {
        title.frame = NSRect(x: 14, y: 14, width: 70, height: 20)
        var x: CGFloat = 86
        for (idx, button) in buttons.enumerated() {
            button.tag = idx
            let width = min(max(180, button.intrinsicContentSize.width + 22), max(180, bounds.width - x - 12))
            button.frame = NSRect(x: x, y: 8, width: width, height: 32)
            x += width + 10
        }
    }

    @objc private func pressed(_ sender: NSButton) {
        guard warnings.indices.contains(sender.tag) else { return }
        onSelect?(warnings[sender.tag])
    }
}

private final class CockpitMapSummaryStrip: NSView {
    private var cards: [CockpitMapSummaryMetricView] = []

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.cornerRadius = 13
        layer?.backgroundColor = CockpitTokens.Color.panel.withAlphaComponent(0.28).cgColor
        layer?.borderColor = CockpitTokens.Color.line.withAlphaComponent(0.18).cgColor
        layer?.borderWidth = 1
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    func configure(_ summary: CockpitMapSummary) {
        cards.forEach { $0.removeFromSuperview() }
        let items: [(String, String, NSColor)] = [
            ("Sellable", "\(summary.sellableCount)", CockpitTokens.Color.green),
            ("Live", "\(summary.liveProductCount)", CockpitTokens.Color.blue2),
            ("Live not sold", "\(summary.liveNotSoldCount)", CockpitTokens.Color.amber),
            ("DARK", "\(summary.darkProductCount + summary.darkModelCount)", CockpitTokens.Color.violet),
            ("Integrity", "\(summary.integrityCount)", summary.integrityCount > 0 ? CockpitTokens.Color.red : CockpitTokens.Color.green),
            ("Trust", summary.trustScore.map(String.init) ?? "-", summary.trustScore.map { $0 >= 60 ? CockpitTokens.Color.amber : CockpitTokens.Color.red } ?? CockpitTokens.Color.muted),
        ]
        cards = items.map { item in
            let card = CockpitMapSummaryMetricView(label: item.0, value: item.1, tint: item.2)
            addSubview(card)
            return card
        }
        needsLayout = true
    }

    override func layout() {
        guard !cards.isEmpty else { return }
        let gap: CGFloat = 8
        let w = max(58, floor((bounds.width - 18 - CGFloat(cards.count - 1) * gap) / CGFloat(cards.count)))
        var x: CGFloat = 9
        for card in cards {
            card.frame = NSRect(x: x, y: 8, width: w, height: max(28, bounds.height - 16))
            x += w + gap
        }
    }
}

private final class CockpitMapSummaryMetricView: NSView {
    private let label: String
    private let value: String
    private let tint: NSColor

    init(label: String, value: String, tint: NSColor) {
        self.label = label
        self.value = value
        self.tint = tint
        super.init(frame: .zero)
        wantsLayer = true
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        let rect = bounds.insetBy(dx: 0.5, dy: 0.5)
        let path = NSBezierPath(roundedRect: rect, xRadius: 8, yRadius: 8)
        tint.withAlphaComponent(0.070).setFill()
        path.fill()
        tint.withAlphaComponent(0.20).setStroke()
        path.lineWidth = 0.7
        path.stroke()
        drawMetricText(value, y: 7, size: 14, weight: .bold, color: CockpitTokens.Color.text)
        drawMetricText(label.uppercased(), y: 27, size: 8.5, weight: .bold, color: CockpitTokens.Color.faint)
    }

    private func drawMetricText(_ text: String, y: CGFloat, size: CGFloat, weight: NSFont.Weight, color: NSColor) {
        let paragraph = NSMutableParagraphStyle()
        paragraph.alignment = .center
        paragraph.lineBreakMode = .byTruncatingTail
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: size, weight: weight),
            .foregroundColor: color,
            .paragraphStyle: paragraph,
        ]
        NSString(string: text).draw(in: NSRect(x: 3, y: y, width: max(0, bounds.width - 6), height: size + 5), withAttributes: attrs)
    }
}

private final class CockpitMapControlBoardView: NSView, CockpitMapSizedSurface {
    let preferredMapHeight: CGFloat = 112
    private let titleText: String
    private let state: CockpitMapState
    private let modeText: String

    init(title: String, state: CockpitMapState, mode: String) {
        self.titleText = title
        self.state = state
        self.modeText = mode
        super.init(frame: .zero)
        wantsLayer = true
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        drawMapPanel(bounds)
        drawMapText(titleText.uppercased(), rect: NSRect(x: 18, y: 14, width: 220, height: 16), size: 10, weight: .bold, color: CockpitTokens.Color.faint)
        drawPill("Products  \(state.products.count)", x: 18, y: 42, tint: CockpitTokens.Color.blue2, active: true)
        drawPill("Models  \(state.models.count)", x: 148, y: 42, tint: CockpitTokens.Color.blue2, active: true)
        drawPill("Facts  \(state.meta["n_facts"] ?? state.meta["facts"] ?? "-")", x: 268, y: 42, tint: CockpitTokens.Color.muted, active: false)
        drawPill("Focus: all", x: 388, y: 42, tint: CockpitTokens.Color.muted, active: false)
        drawPill("Saved views...", x: max(18, bounds.width - 262), y: 42, tint: CockpitTokens.Color.muted, active: false)
        drawMapText(modeText, rect: NSRect(x: 18, y: 78, width: bounds.width - 36, height: 18), size: 11.5, weight: .medium, color: CockpitTokens.Color.muted)
    }

    private func drawPill(_ text: String, x: CGFloat, y: CGFloat, tint: NSColor, active: Bool) {
        let rect = NSRect(x: x, y: y, width: min(112, max(88, CGFloat(text.count) * 7.0 + 24)), height: 28)
        let path = NSBezierPath(roundedRect: rect, xRadius: 8, yRadius: 8)
        tint.withAlphaComponent(active ? 0.13 : 0.050).setFill()
        path.fill()
        tint.withAlphaComponent(active ? 0.36 : 0.14).setStroke()
        path.lineWidth = 0.8
        path.stroke()
        drawMapText(text, rect: rect.insetBy(dx: 10, dy: 6), size: 10.8, weight: .semibold, color: active ? CockpitTokens.Color.text : CockpitTokens.Color.muted)
    }
}

private final class CockpitMapIntegrityGapBoardView: NSView, CockpitMapSizedSurface {
    let preferredMapHeight: CGFloat = 150
    private let warnings: [CockpitMapIntegrityWarning]

    init(warnings: [CockpitMapIntegrityWarning]) {
        self.warnings = warnings
        super.init(frame: .zero)
        wantsLayer = true
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        drawMapPanel(bounds)
        drawMapText(":: INTEGRITY GAPS", rect: NSRect(x: 18, y: 16, width: 220, height: 16), size: 10, weight: .bold, color: CockpitTokens.Color.faint)
        guard !warnings.isEmpty else {
            drawMapText("No materialized integrity warnings", rect: NSRect(x: 18, y: 52, width: bounds.width - 36, height: 18), size: 12, weight: .semibold, color: CockpitTokens.Color.muted)
            return
        }
        let gap: CGFloat = 12
        let cardWidth = max(220, floor((bounds.width - 36 - gap) / 2))
        for (idx, warning) in warnings.prefix(2).enumerated() {
            let rect = NSRect(x: 18 + CGFloat(idx) * (cardWidth + gap), y: 46, width: cardWidth, height: 82)
            let path = NSBezierPath(roundedRect: rect, xRadius: 10, yRadius: 10)
            CockpitTokens.Color.red.withAlphaComponent(0.10).setFill()
            path.fill()
            CockpitTokens.Color.red.withAlphaComponent(0.42).setStroke()
            path.lineWidth = 0.9
            path.stroke()
            drawMapText(warning.surface, rect: NSRect(x: rect.minX + 14, y: rect.minY + 12, width: rect.width - 28, height: 18), size: 12, weight: .bold, color: CockpitTokens.Color.text)
            drawMapText("SERVED  \(warning.servedValue ?? "-")  vs  HONEST  \(warning.honestValue ?? "-")", rect: NSRect(x: rect.minX + 14, y: rect.minY + 38, width: rect.width - 28, height: 16), size: 10.5, weight: .bold, color: CockpitTokens.Color.red)
            drawMapText(warning.note ?? "", rect: NSRect(x: rect.minX + 14, y: rect.minY + 58, width: rect.width - 28, height: 16), size: 10, weight: .medium, color: CockpitTokens.Color.muted)
        }
    }
}

private final class CockpitMapTrendChartView: NSView, CockpitMapSizedSurface {
    let preferredMapHeight: CGFloat = 190
    private let title: String
    private let rows: [CockpitMapHistoryRow]
    private let tint: NSColor

    init(title: String, rows: [CockpitMapHistoryRow], tint: NSColor) {
        self.title = title
        self.rows = rows
        self.tint = tint
        super.init(frame: .zero)
        wantsLayer = true
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        drawMapPanel(bounds)
        drawMapText(title.uppercased(), rect: NSRect(x: 18, y: 14, width: bounds.width - 36, height: 16), size: 10, weight: .bold, color: CockpitTokens.Color.faint)
        let values = rows.compactMap { row -> Double? in
            guard let value = row.metricValue else { return nil }
            return Double(value)
        }
        guard values.count >= 2 else {
            drawMapText("No trend rows", rect: NSRect(x: 18, y: 50, width: bounds.width - 36, height: 18), size: 12, weight: .semibold, color: CockpitTokens.Color.muted)
            return
        }
        let chart = bounds.insetBy(dx: 20, dy: 44)
        let minValue = min(values.min() ?? 0, 0)
        let maxValue = max(values.max() ?? 100, 100)
        let path = NSBezierPath()
        for (idx, value) in values.enumerated() {
            let x = chart.minX + CGFloat(idx) / CGFloat(values.count - 1) * chart.width
            let ratio = (value - minValue) / max(1, maxValue - minValue)
            let y = chart.maxY - CGFloat(ratio) * chart.height
            let point = NSPoint(x: x, y: y)
            idx == 0 ? path.move(to: point) : path.line(to: point)
        }
        tint.withAlphaComponent(0.95).setStroke()
        path.lineWidth = 2.2
        path.stroke()
        CockpitTokens.Color.line.withAlphaComponent(0.25).setStroke()
        NSBezierPath(rect: chart).stroke()
        if let last = values.last {
            drawMapText("\(Int(last.rounded())) / 100", rect: NSRect(x: 18, y: bounds.height - 34, width: bounds.width - 36, height: 18), size: 13, weight: .bold, color: tint)
        }
    }
}

private final class CockpitMapFunnelChartView: NSView, CockpitMapSizedSurface {
    let preferredMapHeight: CGFloat = 190
    private let stages: [CockpitMapFunnelStage]

    init(stages: [CockpitMapFunnelStage]) {
        self.stages = stages
        super.init(frame: .zero)
        wantsLayer = true
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        drawMapPanel(bounds)
        drawMapText("FUNNEL", rect: NSRect(x: 18, y: 14, width: bounds.width - 36, height: 16), size: 10, weight: .bold, color: CockpitTokens.Color.faint)
        let maxCount = max(1, stages.map(\.productCount).max() ?? 1)
        var y: CGFloat = 42
        for stage in stages {
            let labelW: CGFloat = 72
            drawMapText(stage.stage.uppercased(), rect: NSRect(x: 18, y: y, width: labelW, height: 16), size: 10.5, weight: .bold, color: CockpitTokens.Color.muted)
            let barX = 98.0
            let barW = max(8, (bounds.width - barX - 28) * CGFloat(stage.productCount) / CGFloat(maxCount))
            let rect = NSRect(x: barX, y: y + 2, width: barW, height: 12)
            let path = NSBezierPath(roundedRect: rect, xRadius: 6, yRadius: 6)
            CockpitTokens.Color.blue.withAlphaComponent(0.36).setFill()
            path.fill()
            drawMapText("\(stage.productCount)", rect: NSRect(x: barX + barW + 8, y: y - 1, width: 42, height: 16), size: 10.5, weight: .bold, color: CockpitTokens.Color.text)
            y += 23
        }
    }
}

private final class CockpitMapModelValueChartView: NSView, CockpitMapSizedSurface {
    let preferredMapHeight: CGFloat = 320
    private let models: [CockpitMapModel]

    init(models: [CockpitMapModel]) {
        self.models = models
        super.init(frame: .zero)
        wantsLayer = true
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        drawMapPanel(bounds)
        drawMapText("DARK VALUE BOARD", rect: NSRect(x: 18, y: 14, width: bounds.width - 36, height: 16), size: 10, weight: .bold, color: CockpitTokens.Color.faint)
        let plot = bounds.insetBy(dx: 36, dy: 34)
        CockpitTokens.Color.line.withAlphaComponent(0.24).setStroke()
        NSBezierPath(rect: plot).stroke()
        for model in models.prefix(80) {
            let value = CGFloat(model.valueScore ?? 1)
            let effort = effortValue(model.effort)
            let x = plot.minX + (effort - 1) / 2 * plot.width
            let y = plot.maxY - (value - 1) / 4 * plot.height
            let r: CGFloat = model.status.lowercased() == "dark" ? 5.5 : 4
            let dot = NSBezierPath(ovalIn: NSRect(x: x - r, y: y - r, width: r * 2, height: r * 2))
            (model.status.lowercased() == "dark" ? CockpitTokens.Color.violet : CockpitTokens.Color.green).withAlphaComponent(0.72).setFill()
            dot.fill()
        }
        drawMapText("effort S -> L", rect: NSRect(x: plot.minX, y: plot.maxY + 8, width: 120, height: 14), size: 9.5, weight: .medium, color: CockpitTokens.Color.faint)
        drawMapText("value", rect: NSRect(x: plot.maxX - 60, y: plot.minY - 18, width: 60, height: 14), size: 9.5, weight: .medium, color: CockpitTokens.Color.faint, align: .right)
    }

    private func effortValue(_ effort: String?) -> CGFloat {
        switch (effort ?? "M").uppercased() {
        case "S": return 1
        case "L": return 3
        default: return 2
        }
    }
}

private final class CockpitKnowledgeGraphView: NSView, CockpitMapSizedSurface {
    let preferredMapHeight: CGFloat = 430
    var onSelect: ((CockpitMapKnowledgeNode) -> Void)?
    private let graph: CockpitMapKnowledgeGraph

    init(graph: CockpitMapKnowledgeGraph) {
        self.graph = graph
        super.init(frame: .zero)
        wantsLayer = true
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        drawMapPanel(bounds)
        drawMapText("KNOWLEDGE GRAPH", rect: NSRect(x: 18, y: 14, width: bounds.width - 36, height: 16), size: 10, weight: .bold, color: CockpitTokens.Color.faint)
        let nodesByID = Dictionary(uniqueKeysWithValues: graph.nodes.map { ($0.id, $0) })
        let positions = graphPositions()
        let plot = bounds.insetBy(dx: 28, dy: 54)
        for (col, type) in ["product", "model", "fact"].enumerated() {
            let x = plot.minX + CGFloat(col) / 2 * plot.width
            drawMapText(type.uppercased(), rect: NSRect(x: x - 52, y: plot.minY - 22, width: 104, height: 14), size: 9.5, weight: .bold, color: CockpitTokens.Color.muted, align: .center)
        }
        for edge in graph.edges.prefix(160) {
            guard let a = positions[edge.source], let b = positions[edge.target] else { continue }
            let path = NSBezierPath()
            path.move(to: a)
            path.line(to: b)
            (edge.kind == "supersedes" ? CockpitTokens.Color.amber : CockpitTokens.Color.line2).withAlphaComponent(0.22).setStroke()
            path.lineWidth = 0.8
            path.stroke()
        }
        for (id, point) in positions {
            guard let node = nodesByID[id] else { continue }
            let color = graphColor(node)
            let r: CGFloat = node.type == "fact" ? 4.2 : 6
            color.withAlphaComponent(0.85).setFill()
            NSBezierPath(ovalIn: NSRect(x: point.x - r, y: point.y - r, width: r * 2, height: r * 2)).fill()
        }
        drawMapText("\(graph.nodes.count) nodes  \(graph.edges.count) edges", rect: NSRect(x: 18, y: bounds.height - 28, width: bounds.width - 36, height: 16), size: 11, weight: .semibold, color: CockpitTokens.Color.muted)
    }

    override func mouseDown(with event: NSEvent) {
        let point = convert(event.locationInWindow, from: nil)
        let nodesByID = Dictionary(uniqueKeysWithValues: graph.nodes.map { ($0.id, $0) })
        let hit = graphPositions().min { lhs, rhs in
            hypot(lhs.value.x - point.x, lhs.value.y - point.y) < hypot(rhs.value.x - point.x, rhs.value.y - point.y)
        }
        guard let hit, hypot(hit.value.x - point.x, hit.value.y - point.y) < 18, let node = nodesByID[hit.key] else { return }
        onSelect?(node)
    }

    private func graphPositions() -> [String: NSPoint] {
        let groups = ["product", "model", "fact"]
        let plot = bounds.insetBy(dx: 28, dy: 54)
        var positions: [String: NSPoint] = [:]
        for (col, type) in groups.enumerated() {
            let nodes = graph.nodes.filter { $0.type == type }.prefix(22)
            let x = plot.minX + CGFloat(col) / CGFloat(max(1, groups.count - 1)) * plot.width
            for (idx, node) in nodes.enumerated() {
                let y = plot.minY + CGFloat(idx + 1) / CGFloat(nodes.count + 1) * plot.height
                positions[node.id] = NSPoint(x: x, y: y)
            }
        }
        return positions
    }

    private func graphColor(_ node: CockpitMapKnowledgeNode) -> NSColor {
        switch node.type {
        case "product": return CockpitTokens.Color.green
        case "model": return node.status == "dark" ? CockpitTokens.Color.violet : CockpitTokens.Color.blue2
        case "fact": return CockpitTokens.Color.amber
        default: return CockpitTokens.Color.muted
        }
    }
}

private final class CockpitMapMoatMatrixView: NSView, CockpitMapSizedSurface {
    let preferredMapHeight: CGFloat = 240
    private let assets: [CockpitMapMoatAsset]

    init(assets: [CockpitMapMoatAsset]) {
        self.assets = assets
        super.init(frame: .zero)
        wantsLayer = true
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        drawMapPanel(bounds)
        drawMapText("MOAT MATRIX", rect: NSRect(x: 18, y: 14, width: 180, height: 16), size: 10, weight: .bold, color: CockpitTokens.Color.faint)
        let plot = bounds.insetBy(dx: 54, dy: 42)
        CockpitTokens.Color.line.withAlphaComponent(0.20).setStroke()
        NSBezierPath(rect: plot).stroke()
        drawMapText("strength", rect: NSRect(x: plot.maxX - 90, y: plot.maxY + 8, width: 90, height: 14), size: 9.5, weight: .medium, color: CockpitTokens.Color.faint, align: .right)
        drawMapText("value", rect: NSRect(x: plot.minX, y: plot.minY - 22, width: 80, height: 14), size: 9.5, weight: .medium, color: CockpitTokens.Color.faint)
        for asset in assets.prefix(80) {
            let strength = CGFloat(max(1, min(5, asset.strength ?? 2)))
            let value = CGFloat(max(1, min(5, asset.valueScore ?? 2)))
            let x = plot.minX + (strength - 1) / 4 * plot.width
            let y = plot.maxY - (value - 1) / 4 * plot.height
            let r: CGFloat = asset.isAtRisk ? 8 : 6
            let path = NSBezierPath(ovalIn: NSRect(x: x - r, y: y - r, width: r * 2, height: r * 2))
            (asset.isAtRisk ? CockpitTokens.Color.amber : CockpitTokens.Color.green).withAlphaComponent(0.55).setFill()
            path.fill()
            (asset.isAtRisk ? CockpitTokens.Color.amber : CockpitTokens.Color.green).withAlphaComponent(0.88).setStroke()
            path.lineWidth = asset.isAtRisk ? 1.8 : 0.9
            path.stroke()
        }
    }
}

private final class CockpitMapRunwayBoardView: NSView, CockpitMapSizedSurface {
    let preferredMapHeight: CGFloat = 190
    private let items: [CockpitMapRunwayItem]
    private let health: CockpitMachineHealth?

    init(items: [CockpitMapRunwayItem], health: CockpitMachineHealth?) {
        self.items = items
        self.health = health
        super.init(frame: .zero)
        wantsLayer = true
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        drawMapPanel(bounds)
        drawMapText("RUNWAY / LOCAL CAPACITY", rect: NSRect(x: 18, y: 14, width: 220, height: 16), size: 10, weight: .bold, color: CockpitTokens.Color.faint)
        let categories = Dictionary(grouping: items, by: \.category)
        let ordered = ["storage", "models", "compute"] + categories.keys.filter { !["storage", "models", "compute"].contains($0) }.sorted()
        let colWidth = max(120, (bounds.width - 48) / CGFloat(max(1, ordered.count)))
        for (idx, category) in ordered.enumerated() {
            let x = 18 + CGFloat(idx) * colWidth
            let count = categories[category]?.count ?? 0
            let active = count > 0
            drawMapText(category.uppercased(), rect: NSRect(x: x, y: 48, width: colWidth - 12, height: 16), size: 10, weight: .bold, color: CockpitTokens.Color.muted)
            let bar = NSRect(x: x, y: 76, width: colWidth - 18, height: 16)
            let path = NSBezierPath(roundedRect: bar, xRadius: 8, yRadius: 8)
            (active ? CockpitTokens.Color.blue2 : CockpitTokens.Color.line).withAlphaComponent(active ? 0.28 : 0.12).setFill()
            path.fill()
            drawMapText("\(count) items", rect: NSRect(x: x, y: 102, width: colWidth - 12, height: 16), size: 10.5, weight: .semibold, color: CockpitTokens.Color.text)
        }
        let healthText = [
            health?.mode.map { "mode \($0)" },
            health?.freeRAMGB.map { String(format: "%.1f GB free", $0) },
            health?.swapUsedGB.map { String(format: "%.1f GB swap", $0) },
        ].compactMap { $0 }.joined(separator: "  ")
        drawMapText(healthText.isEmpty ? "Machine health unavailable" : healthText, rect: NSRect(x: 18, y: bounds.height - 34, width: bounds.width - 36, height: 18), size: 11, weight: .semibold, color: CockpitTokens.Color.muted)
    }
}

private final class CockpitMapChangeTimelineView: NSView, CockpitMapSizedSurface {
    let preferredMapHeight: CGFloat = 220
    private let changes: [CockpitMapChange]
    private let alerts: [CockpitMapAlert]

    init(changes: [CockpitMapChange], alerts: [CockpitMapAlert]) {
        self.changes = changes
        self.alerts = alerts
        super.init(frame: .zero)
        wantsLayer = true
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        drawMapPanel(bounds)
        drawMapText("CHANGE TIMELINE", rect: NSRect(x: 18, y: 14, width: 200, height: 16), size: 10, weight: .bold, color: CockpitTokens.Color.faint)
        let rows = changes.prefix(9)
        let lineX: CGFloat = 34
        CockpitTokens.Color.line.withAlphaComponent(0.28).setStroke()
        let path = NSBezierPath()
        path.move(to: NSPoint(x: lineX, y: 48))
        path.line(to: NSPoint(x: lineX, y: bounds.height - 28))
        path.stroke()
        for (idx, change) in rows.enumerated() {
            let y = CGFloat(52 + idx * 17)
            colorForChange(change).withAlphaComponent(0.88).setFill()
            NSBezierPath(ovalIn: NSRect(x: lineX - 4, y: y - 4, width: 8, height: 8)).fill()
            drawMapText("\(change.entityKind) / \(change.entity): \(change.field) \(change.oldValue ?? "-") -> \(change.newValue ?? "-")", rect: NSRect(x: 52, y: y - 8, width: bounds.width - 70, height: 16), size: 10.5, weight: .medium, color: CockpitTokens.Color.muted)
        }
        if !alerts.isEmpty {
            drawMapText("\(alerts.count) live alerts", rect: NSRect(x: bounds.width - 150, y: 14, width: 132, height: 16), size: 10.5, weight: .bold, color: CockpitTokens.Color.red, align: .right)
        }
    }

    private func colorForChange(_ change: CockpitMapChange) -> NSColor {
        switch (change.severity ?? "").lowercased() {
        case "critical", "high": return CockpitTokens.Color.red
        case "medium", "watch": return CockpitTokens.Color.amber
        default: return CockpitTokens.Color.blue2
        }
    }
}

private func drawMapPanel(_ bounds: NSRect) {
    let rect = bounds.insetBy(dx: 0.5, dy: 0.5)
    let path = NSBezierPath(roundedRect: rect, xRadius: 10, yRadius: 10)
    NSGradient(colors: [
        CockpitTokens.Color.panel2.withAlphaComponent(0.72),
        NSColor.black.withAlphaComponent(0.08),
    ])?.draw(in: path, angle: -90)
    CockpitTokens.Color.line.withAlphaComponent(0.22).setStroke()
    path.lineWidth = 0.8
    path.stroke()
}

private func drawMapText(_ text: String, rect: NSRect, size: CGFloat, weight: NSFont.Weight, color: NSColor, align: NSTextAlignment = .left) {
    let paragraph = NSMutableParagraphStyle()
    paragraph.lineBreakMode = .byTruncatingTail
    paragraph.alignment = align
    NSString(string: text).draw(in: rect, withAttributes: [
        .font: NSFont.systemFont(ofSize: size, weight: weight),
        .foregroundColor: color,
        .paragraphStyle: paragraph,
    ])
}

private final class CockpitMapDetailPanel: NSView {
    var onOpen: ((String) -> Void)?
    private var openPath: String?
    private var noteTarget: (kind: String, entity: String)?
    private var copyFacts: String = ""
    private let title = CockpitUI.label("Select a map item", size: 13.5, weight: .bold, color: CockpitTokens.Color.text)
    private let bodyScroll = CockpitEdgeScrollView()
    private let body = NSTextField(wrappingLabelWithString: "")
    private let openButton = CockpitUI.button("Open Surface")
    private let noteButton = CockpitUI.button("Add Note")
    private let copyButton = CockpitUI.button("Copy Facts")

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.cornerRadius = 14
        layer?.backgroundColor = CockpitTokens.Color.panel.withAlphaComponent(0.70).cgColor
        layer?.borderColor = CockpitTokens.Color.line.withAlphaComponent(0.24).cgColor
        layer?.borderWidth = 1
        addSubview(title)
        body.font = .systemFont(ofSize: 11.5, weight: .medium)
        body.textColor = CockpitTokens.Color.muted
        body.drawsBackground = false
        body.isBordered = false
        body.isSelectable = true
        bodyScroll.hasVerticalScroller = true
        bodyScroll.hasHorizontalScroller = false
        bodyScroll.documentView = body
        CockpitScrollChrome.apply(to: bodyScroll)
        addSubview(bodyScroll)
        openButton.target = self
        openButton.action = #selector(openPressed)
        applyMapButtonChrome(openButton, active: true)
        addSubview(openButton)
        noteButton.target = self
        noteButton.action = #selector(notePressed)
        applyMapButtonChrome(noteButton, active: false, tint: CockpitTokens.Color.amber)
        addSubview(noteButton)
        copyButton.target = self
        copyButton.action = #selector(copyPressed)
        applyMapButtonChrome(copyButton, active: false, tint: CockpitTokens.Color.blue2)
        addSubview(copyButton)
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    func render(_ object: CockpitMapSelectedObject?, state: CockpitMapState?, provenanceByVertical: [String: CockpitMapProvenance]) {
        openPath = nil
        noteTarget = nil
        copyFacts = ""
        guard let object else {
            title.stringValue = "Select a map item"
            body.stringValue = "Click a product, model, system node, or integrity warning to inspect its source-backed map record."
            openButton.isHidden = true
            noteButton.isHidden = true
            copyButton.isHidden = true
            return
        }
        switch object {
        case .product(let product, let models):
            title.stringValue = product.displayName
            let integrity = state?.integrityWarnings.filter { $0.surface == product.vertical || $0.surface.localizedCaseInsensitiveContains(product.displayName) } ?? []
            let annotations = annotationsText(state?.annotations.filter { $0.entityKind == "product" && $0.entity == product.vertical } ?? [])
            let history = historyText(state?.history.filter { $0.entityKind == "product" && $0.entity == product.vertical } ?? [])
            let provenance = provenanceText(provenanceByVertical[product.vertical])
            copyFacts = [
                product.vertical,
                product.displayName,
                "stage=\(product.stage)",
                "health=\(product.health)",
                "metric=\(product.metricLine)",
                "readiness=\(product.buyerReadiness ?? "-")",
                provenance,
            ].joined(separator: "\n")
            body.stringValue = [
                "Stage: \(product.isSellable ? "sold" : product.stage)  Health: \(product.health)",
                "Metric: \(product.metricLine.isEmpty ? "-" : product.metricLine)",
                "Readiness: \(product.buyerReadiness ?? "-")  Open: \(product.openCount)  Running: \(product.runningCount)  DARK: \(product.darkCount)",
                "Path to sellable:\n" + pathToSellable(product, integrity: integrity),
                product.metricHonestNote ?? product.blurb ?? "",
                integrity.isEmpty ? "" : "Integrity:\n" + integrity.map { "\($0.severity ?? "flag"): \($0.note ?? $0.surface)" }.joined(separator: "\n"),
                models.isEmpty ? "Models: none mapped" : "Models: \(models.map(\.name).joined(separator: ", "))",
                provenance,
                history,
                annotations,
            ].filter { !$0.isEmpty }.joined(separator: "\n\n")
            openPath = product.surfaceURL
            noteTarget = ("product", product.vertical)
        case .model(let model):
            title.stringValue = model.name
            let annotations = annotationsText(state?.annotations.filter { $0.entityKind == "model" && $0.entity == model.name } ?? [])
            let history = historyText(state?.history.filter { $0.entityKind == "model" && $0.entity == model.name } ?? [])
            let provenance = model.vertical.map { provenanceText(provenanceByVertical[$0]) } ?? ""
            copyFacts = [
                model.name,
                "status=\(model.status)",
                "vertical=\(model.vertical ?? "-")",
                "metric=\([model.metric, model.metricValue].compactMap { $0 }.joined(separator: " "))",
                "value=\(model.valueScore.map(String.init) ?? "-")",
                "effort=\(model.effort ?? "-")",
                provenance,
            ].joined(separator: "\n")
            body.stringValue = [
                "Status: \(model.status)  Powers: \(model.powers ?? "-")",
                "Metric: \([model.metric, model.metricValue].compactMap { $0 }.joined(separator: " "))",
                "Value: \(model.valueScore.map(String.init) ?? "-")  Effort: \(model.effort ?? "-")",
                model.status.lowercased() == "dark" ? "Guided wiring:\n1. Claim the board item for \(model.vertical ?? "the owning vertical").\n2. Wire the model only after the generator exposes a materialized status column.\n3. Rebuild map.db and verify the row moves out of DARK." : "",
                model.ledgerRef ?? "",
                provenance,
                history,
                annotations,
            ].filter { !$0.isEmpty }.joined(separator: "\n\n")
            noteTarget = ("model", model.name)
        case .node(let node, let edges):
            title.stringValue = node.name
            copyFacts = [
                node.id,
                "layer=\(node.layer)",
                "status=\(node.status)",
                "edges=\(edges.count)",
            ].joined(separator: "\n")
            body.stringValue = [
                "Layer: \(node.layer)  Status: \(node.status)",
                "ID: \(node.id)",
                node.sizeNote ?? "",
                edges.isEmpty ? "Edges: none" : "Edges:\n" + edges.map { "\($0.fromID) -> \($0.toID) [\($0.status)]" }.joined(separator: "\n"),
            ].filter { !$0.isEmpty }.joined(separator: "\n\n")
            noteTarget = ("node", node.id)
        case .integrity(let warning):
            title.stringValue = warning.surface
            copyFacts = [
                warning.surface,
                "severity=\(warning.severity ?? "-")",
                "served=\(warning.servedValue ?? "-")",
                "honest=\(warning.honestValue ?? "-")",
                warning.note ?? "",
            ].joined(separator: "\n")
            body.stringValue = [
                "Severity: \(warning.severity ?? "-")",
                "Served: \(warning.servedValue ?? "-")",
                "Honest: \(warning.honestValue ?? "-")",
                warning.note ?? "",
            ].filter { !$0.isEmpty }.joined(separator: "\n\n")
            noteTarget = ("integrity", warning.surface)
        case .alert(let alert):
            title.stringValue = alert.title
            copyFacts = [
                alert.title,
                "level=\(alert.level)",
                "kind=\(alert.kind)",
                "cta=\([alert.ctaKind, alert.ctaTarget].compactMap { $0 }.joined(separator: " -> "))",
                alert.detail ?? "",
            ].joined(separator: "\n")
            body.stringValue = [
                "Level: \(alert.level)  Kind: \(alert.kind)",
                alert.detail ?? "",
                [alert.ctaKind, alert.ctaTarget].compactMap { $0 }.isEmpty ? "" : "CTA: \([alert.ctaKind, alert.ctaTarget].compactMap { $0 }.joined(separator: " -> "))",
            ].filter { !$0.isEmpty }.joined(separator: "\n\n")
            if alert.ctaKind == "product", let target = alert.ctaTarget {
                noteTarget = ("product", target)
            }
        }
        openButton.isHidden = openPath == nil
        noteButton.isHidden = noteTarget == nil
        copyButton.isHidden = copyFacts.isEmpty
        needsLayout = true
    }

    override func layout() {
        title.frame = NSRect(x: 18, y: 18, width: bounds.width - 36, height: 24)
        let visibleButtons = [openButton, copyButton, noteButton].filter { !$0.isHidden }
        let buttonY = bounds.height - 54
        if !visibleButtons.isEmpty {
            let gap: CGFloat = 8
            let width = floor((bounds.width - 36 - CGFloat(visibleButtons.count - 1) * gap) / CGFloat(visibleButtons.count))
            var x: CGFloat = 18
            for button in visibleButtons {
                button.frame = NSRect(x: x, y: buttonY, width: width, height: 34)
                x += width + gap
            }
        }
        let bodyBottom = visibleButtons.isEmpty ? bounds.height - 22 : bounds.height - 66
        bodyScroll.frame = NSRect(x: 18, y: 54, width: bounds.width - 36, height: max(80, bodyBottom - 54))
        let availableTextWidth = max(120, bodyScroll.contentSize.width - 8)
        let estimatedLines = max(5, body.stringValue.components(separatedBy: "\n").reduce(0) { partial, line in
            partial + max(1, Int(ceil(Double(max(1, line.count)) / Double(max(24, Int(availableTextWidth / 6.6))))))
        })
        body.frame = NSRect(x: 0, y: 0, width: availableTextWidth, height: CGFloat(estimatedLines) * 17 + 24)
    }

    @objc private func openPressed() {
        guard let openPath else { return }
        onOpen?(openPath)
    }

    @objc private func copyPressed() {
        guard !copyFacts.isEmpty else { return }
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(copyFacts, forType: .string)
    }

    @objc private func notePressed() {
        guard let noteTarget, let window else { return }
        let alert = NSAlert()
        alert.messageText = "Add operator note"
        alert.informativeText = "\(noteTarget.kind): \(noteTarget.entity)"
        alert.addButton(withTitle: "Add")
        alert.addButton(withTitle: "Cancel")
        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        input.placeholderString = "Note"
        alert.accessoryView = input
        alert.beginSheetModal(for: window) { response in
            guard response == .alertFirstButtonReturn else { return }
            let note = input.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !note.isEmpty else { return }
            Self.postAnnotation(kind: noteTarget.kind, entity: noteTarget.entity, note: note)
        }
    }

    private static func postAnnotation(kind: String, entity: String, note: String) {
        guard let url = URL(string: "\(HarnessEndpoint.base)/api/map/annotate") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "entity_kind": kind,
            "entity": entity,
            "note": note,
        ])
        URLSession.shared.dataTask(with: request).resume()
    }

    private func annotationsText(_ annotations: [CockpitMapAnnotation]) -> String {
        guard !annotations.isEmpty else { return "" }
        return "Operator notes:\n" + annotations.prefix(6).map { "\($0.timestamp): \($0.note)" }.joined(separator: "\n")
    }

    private func historyText(_ history: [CockpitMapHistoryRow]) -> String {
        guard !history.isEmpty else { return "" }
        return "Metric history:\n" + history.prefix(6).map { row in
            [row.timestamp, row.stage, row.health, row.metric, row.metricValue].compactMap { $0 }.joined(separator: "  ")
        }.joined(separator: "\n")
    }

    private func provenanceText(_ provenance: CockpitMapProvenance?) -> String {
        guard let provenance else { return "" }
        var sections: [String] = []
        if !provenance.facts.isEmpty {
            sections.append("Provenance:\n" + provenance.facts.prefix(8).map { fact in
                [
                    fact.status.map { "[\($0)]" },
                    fact.statement,
                    fact.value,
                    fact.unit,
                    fact.evidencePointer.map { "evidence: \($0)" },
                    fact.ownerLane.map { "owner: \($0)" },
                    fact.updatedAt,
                ].compactMap { $0 }.joined(separator: "  ")
            }.joined(separator: "\n"))
        }
        if let models = provenance.models, !models.isEmpty {
            sections.append("Provenance models:\n" + models.prefix(8).map {
                [$0.name, $0.status].compactMap { $0 }.joined(separator: "  ")
            }.joined(separator: "\n"))
        }
        if let commits = provenance.commits, !commits.isEmpty {
            sections.append("Commits:\n" + commits.prefix(5).map {
                [$0.sha, $0.ts, $0.message].compactMap { $0 }.joined(separator: "  ")
            }.joined(separator: "\n"))
        }
        return sections.joined(separator: "\n\n")
    }

    private func pathToSellable(_ product: CockpitMapProduct, integrity: [CockpitMapIntegrityWarning]) -> String {
        let built = ["built", "wired", "live"].contains(product.stage.lowercased()) || product.isSellable
        let wired = ["wired", "live"].contains(product.stage.lowercased()) || product.isSellable
        let served = product.surfaceURL?.isEmpty == false
        let gate = product.health.lowercased() != "red" && product.health.lowercased() != "dark"
        let noIntegrity = integrity.isEmpty
        let market = product.isSellable
        let items = [
            ("built", built),
            ("wired", wired),
            ("served", served),
            ("gate", gate),
            ("no integrity flag", noIntegrity),
            ("market validated", market),
        ]
        return items.map { "\($0.1 ? "OK" : "GAP")  \($0.0)" }.joined(separator: "\n")
    }
}

private func applyMapButtonChrome(_ button: NSButton?, active: Bool = false, tint: NSColor = CockpitTokens.Color.blue) {
    guard let button else { return }
    button.wantsLayer = true
    button.layer?.cornerRadius = 8
    button.layer?.backgroundColor = active
        ? tint.withAlphaComponent(0.040).cgColor
        : NSColor.white.withAlphaComponent(0.004).cgColor
    button.layer?.borderWidth = 0.5
    button.layer?.borderColor = (active ? tint : NSColor.white).withAlphaComponent(active ? 0.10 : 0.012).cgColor
    button.layer?.shadowColor = tint.cgColor
    button.layer?.shadowOpacity = active ? 0.18 : 0
    button.layer?.shadowRadius = active ? 10 : 0
    button.layer?.shadowOffset = .zero
}
