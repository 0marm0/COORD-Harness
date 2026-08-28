import AppKit

final class CockpitDiagnosticsView: NSView {
    var onClose: (() -> Void)?
    private let title = CockpitUI.label("Diagnostics", size: 14, weight: .bold, color: CockpitTokens.Color.text)
    private let summary = CockpitUI.label("", size: 11, weight: .medium, color: CockpitTokens.Color.muted)
    private let closeButton = CockpitUI.button("")
    private let scroll = CockpitEdgeScrollView()
    private let content = CockpitFlippedView()
    private var items: [NSView] = []

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.backgroundColor = CockpitTokens.Color.panel2.withAlphaComponent(0.90).cgColor
        layer?.borderColor = CockpitTokens.Color.line.withAlphaComponent(0.64).cgColor
        layer?.borderWidth = 1
        addSubview(title)
        addSubview(summary)
        closeButton.image = NSImage(systemSymbolName: "xmark", accessibilityDescription: "Close diagnostics")
        closeButton.imagePosition = .imageOnly
        closeButton.toolTip = "Close diagnostics"
        closeButton.target = self
        closeButton.action = #selector(closePressed)
        addSubview(closeButton)
        scroll.hasVerticalScroller = true
        scroll.documentView = content
        CockpitScrollChrome.apply(to: scroll)
        addSubview(scroll)
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    override func layout() {
        title.frame = NSRect(x: 16, y: 14, width: bounds.width - 58, height: 20)
        closeButton.frame = NSRect(x: bounds.width - 42, y: 12, width: 28, height: 28)
        summary.frame = NSRect(x: 16, y: 35, width: bounds.width - 58, height: 18)
        scroll.frame = NSRect(x: 0, y: 62, width: bounds.width, height: max(1, bounds.height - 62))
        layoutItems()
    }

    @objc private func closePressed() {
        onClose?()
    }

    func render(state: CockpitState, lastActionResult: NativeCockpitActionResult? = nil) {
        items.forEach { $0.removeFromSuperview() }
        items = []

        let actionCount = state.rows.reduce(0) { $0 + $1.actions.count }
        summary.stringValue = "seq \(state.writerSeq) | rows \(state.rows.count) | actions \(actionCount)"

        addSection("Read model")
        addCard(
            title: state.stale ? "Stale projection" : "Fresh projection",
            value: "schema \(state.schemaVersion)",
            detail: projectionDetail(state),
            tint: state.stale ? CockpitTokens.Color.amber : CockpitTokens.Color.green
        )
        if let error = state.error {
            addCard(
                title: "Load error",
                value: error.kind.rawValue,
                detail: error.message,
                tint: CockpitTokens.Color.red
            )
        }

        if let lastActionResult {
            addSection("Last action")
            addCard(
                title: lastActionResult.actionID.isEmpty ? "Native action" : lastActionResult.actionID,
                value: lastActionResult.cardValue,
                detail: lastActionResult.cardDetail,
                tint: lastActionResult.ok ? CockpitTokens.Color.green : CockpitTokens.Color.red
            )
        }

        if let inventory = state.capabilityInventory {
            addSection("Capabilities")
            addCard(
                title: "Authority",
                value: inventory.readOnly ? "read-only" : "mutable",
                detail: authorityDetail(inventory),
                tint: inventory.readOnly ? CockpitTokens.Color.green : CockpitTokens.Color.amber
            )
            addCard(
                title: "Token policy",
                value: inventory.defaultContextInjectionEnabled ? "context injection on" : "no default context injection",
                detail: tokenPolicyDetail(inventory),
                tint: inventory.defaultContextInjectionEnabled ? CockpitTokens.Color.amber : CockpitTokens.Color.green
            )
            for capability in inventory.capabilities.prefix(8) {
                addCard(
                    title: capability.label,
                    value: [capability.category, capability.status].compactMap { $0 }.joined(separator: " / "),
                    detail: capability.detail,
                    tint: tint(for: capability.status)
                )
            }
        }

        addSection("Sessions")
        if state.sessions.isEmpty {
            addCard(title: "No active sessions", value: "-", detail: "coord session table is empty", tint: CockpitTokens.Color.faint)
        } else {
            for session in state.sessions.prefix(8) {
                let age = session.heartbeatAgeSeconds.map { "heartbeat \(formatAge($0)) ago" }
                    ?? (session.isStale ? "heartbeat stale" : "heartbeat fresh")
                addCard(
                    title: session.label,
                    value: "\(session.actor) / \(session.status)",
                    detail: age,
                    tint: session.isStale ? CockpitTokens.Color.amber : CockpitTokens.ownerColor(session.actor)
                )
            }
        }

        addSection("Projection")
        if state.diagnostics.isEmpty {
            addCard(title: "No diagnostics", value: "ok", detail: "projection diagnostics table is empty", tint: CockpitTokens.Color.green)
        } else {
            for diagnostic in state.diagnostics.prefix(10) {
                addCard(
                    title: diagnostic.label,
                    value: diagnostic.status,
                    detail: diagnostic.detail ?? diagnostic.category,
                    tint: tint(for: diagnostic.status)
                )
            }
        }
        needsLayout = true
    }

    private func addSection(_ text: String) {
        let label = CockpitUI.label(text, size: 11, weight: .bold, color: CockpitTokens.Color.blue2)
        content.addSubview(label)
        items.append(label)
    }

    private func addCard(title: String, value: String, detail: String, tint: NSColor) {
        let card = CockpitDiagnosticCard(title: title, value: value, detail: detail, tint: tint)
        content.addSubview(card)
        items.append(card)
    }

    private func layoutItems() {
        let width = max(1, scroll.contentSize.width)
        var y: CGFloat = 12
        for item in items {
            if let card = item as? CockpitDiagnosticCard {
                let height = card.preferredHeight(width: width - 24)
                card.frame = NSRect(x: 12, y: y, width: width - 24, height: height)
                y += height + 10
            } else {
                item.frame = NSRect(x: 16, y: y, width: width - 32, height: 18)
                y += 24
            }
        }
        content.frame = NSRect(x: 0, y: 0, width: width, height: max(scroll.contentSize.height, y + 12))
    }

    private func tint(for status: String) -> NSColor {
        let value = status.lowercased()
        if value.contains("ok") || value.contains("fresh") { return CockpitTokens.Color.green }
        if value.contains("warn") || value.contains("stale") { return CockpitTokens.Color.amber }
        if value.contains("error") || value.contains("fail") { return CockpitTokens.Color.red }
        return CockpitTokens.Color.blue2
    }

    private func projectionDetail(_ state: CockpitState) -> String {
        let source = state.sourceVersion ?? "coord.db native_cockpit.v1"
        guard let raw = state.builtAt, let timestamp = Double(raw) else { return source }
        let age = max(0, Date().timeIntervalSince1970 - timestamp)
        return "built \(formatAge(age)) ago / \(source)"
    }

    private func authorityDetail(_ inventory: CockpitCapabilityInventory) -> String {
        let preferred = ["lifecycle", "projection_api", "local_job_telemetry", "context"]
        let parts = preferred.compactMap { key -> String? in
            guard let value = inventory.authority[key], !value.isEmpty else { return nil }
            return "\(key.replacingOccurrences(of: "_", with: " ")) \(value)"
        }
        if !parts.isEmpty { return parts.joined(separator: " | ") }
        return inventory.source ?? "capability inventory"
    }

    private func tokenPolicyDetail(_ inventory: CockpitCapabilityInventory) -> String {
        let trueKeys = inventory.tokenCostPolicy
            .filter(\.value)
            .map(\.key)
            .sorted()
            .map { $0.replacingOccurrences(of: "_", with: " ") }
        if trueKeys.isEmpty { return "No local helper reads count as model/API tokens" }
        return trueKeys.joined(separator: " | ")
    }

    private func formatAge(_ seconds: Double) -> String {
        let value = max(0, Int(seconds.rounded()))
        if value < 60 { return "\(value)s" }
        if value < 3_600 { return "\(value / 60)m \(value % 60)s" }
        return "\(value / 3_600)h \((value % 3_600) / 60)m"
    }
}

private final class CockpitDiagnosticCard: NSView {
    private let marker = NSView()
    private let titleLabel = CockpitUI.label("", size: 11.5, weight: .semibold, color: CockpitTokens.Color.text)
    private let valueLabel = CockpitUI.label("", size: 10.5, weight: .bold, color: CockpitTokens.Color.blue2, align: .right)
    private let detailLabel = CockpitUI.label("", size: 10.5, weight: .medium, color: CockpitTokens.Color.muted)
    private let tint: NSColor

    init(title: String, value: String, detail: String, tint: NSColor) {
        self.tint = tint
        super.init(frame: .zero)
        wantsLayer = true
        layer?.backgroundColor = CockpitTokens.Color.panel.withAlphaComponent(0.38).cgColor
        layer?.borderColor = CockpitTokens.Color.line2.withAlphaComponent(0.26).cgColor
        layer?.borderWidth = 1
        layer?.cornerRadius = 7
        marker.wantsLayer = true
        marker.layer?.backgroundColor = tint.cgColor
        marker.layer?.cornerRadius = 2.5
        addSubview(marker)
        addSubview(titleLabel)
        addSubview(valueLabel)
        addSubview(detailLabel)
        titleLabel.stringValue = title
        valueLabel.stringValue = value
        valueLabel.textColor = tint
        detailLabel.stringValue = detail
        detailLabel.lineBreakMode = .byWordWrapping
        detailLabel.cell?.usesSingleLineMode = false
        detailLabel.maximumNumberOfLines = 3
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    func preferredHeight(width: CGFloat) -> CGFloat {
        if detailLabel.stringValue.isEmpty { return 52 }
        return detailLabel.stringValue.count > 120 ? 92 : 70
    }

    override func layout() {
        marker.frame = NSRect(x: 12, y: 14, width: 6, height: bounds.height - 28)
        titleLabel.frame = NSRect(x: 28, y: 12, width: max(80, bounds.width - 142), height: 18)
        valueLabel.frame = NSRect(x: bounds.width - 106, y: 12, width: 90, height: 18)
        detailLabel.frame = NSRect(x: 28, y: 34, width: max(80, bounds.width - 44), height: 28)
    }
}

final class CockpitFlippedView: NSView {
    override var isFlipped: Bool { true }
}
