import AppKit

final class CockpitInspectorView: NSView {
    var onClose: (() -> Void)?
    private let title = CockpitUI.label("Inspector", size: 14, weight: .bold, color: CockpitTokens.Color.text)
    private let summary = CockpitUI.label("", size: 11, weight: .medium, color: CockpitTokens.Color.muted)
    private let closeButton = CockpitUI.button("")
    private let scroll = CockpitEdgeScrollView()
    private let content = CockpitInspectorContentView()
    private var items: [NSView] = []

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.backgroundColor = CockpitTokens.Color.panel2.withAlphaComponent(0.90).cgColor
        layer?.borderColor = CockpitTokens.Color.line.withAlphaComponent(0.64).cgColor
        layer?.borderWidth = 1
        addSubview(title)
        addSubview(summary)
        closeButton.image = NSImage(systemSymbolName: "xmark", accessibilityDescription: "Close inspector")
        closeButton.imagePosition = .imageOnly
        closeButton.toolTip = "Close inspector"
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

    func render(row: CockpitRow?, state: CockpitState) {
        items.forEach { $0.removeFromSuperview() }
        items = []

        guard let row else {
            summary.stringValue = "seq \(state.writerSeq) | select a work row"
            addCard(title: "No row selected", value: "-", detail: "Select a row or open Inspector again to focus the first visible row.", tint: CockpitTokens.Color.faint)
            needsLayout = true
            return
        }

        summary.stringValue = "\(row.ownerGroup ?? row.owner ?? "unowned") | \(row.status) | \(row.scope)"
        addCard(title: row.title, value: row.status.uppercased(), detail: row.workID ?? row.dedupKey, tint: CockpitTokens.statusColor(row.status))
        addSection("Context")
        addCard(title: "Why", value: row.priority ?? "-", detail: row.whyText ?? "-", tint: CockpitTokens.Color.blue2)
        addCard(title: "Note", value: row.rowKind ?? "-", detail: row.noteText ?? "-", tint: CockpitTokens.Color.muted)
        addSection("Progress")
        addCard(title: row.pctText, value: row.etaText ?? "-", detail: progressDetail(row), tint: CockpitTokens.Color.blue)
        addSection("Identity")
        addCard(title: row.moduleLabel ?? row.module ?? "-", value: row.resourceClass ?? "-", detail: "\(row.domainLabel ?? "-") | \(row.dedupKey)", tint: CockpitTokens.moduleColor(row.module ?? row.moduleLabel))
        addSection("Actions")
        if row.actions.isEmpty {
            addCard(title: "No row actions", value: "-", detail: "Projection did not publish actions for this row.", tint: CockpitTokens.Color.faint)
        } else {
            addCard(title: "\(row.actions.filter(\.isEnabled).count) enabled", value: "\(row.actions.count) total", detail: row.actions.map(\.label).joined(separator: ", "), tint: CockpitTokens.Color.green)
        }
        needsLayout = true
    }

    private func addSection(_ text: String) {
        let label = CockpitUI.label(text, size: 11, weight: .bold, color: CockpitTokens.Color.blue2)
        content.addSubview(label)
        items.append(label)
    }

    private func addCard(title: String, value: String, detail: String, tint: NSColor) {
        let card = CockpitInspectorCard(title: title, value: value, detail: detail, tint: tint)
        content.addSubview(card)
        items.append(card)
    }

    private func layoutItems() {
        let width = max(1, scroll.contentSize.width)
        var y: CGFloat = 12
        for item in items {
            if let card = item as? CockpitInspectorCard {
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

    private func progressDetail(_ row: CockpitRow) -> String {
        let progress = row.effectivePct.map { "\(Int($0.rounded()))% determinate" } ?? "indeterminate"
        return "\(progress) | ETA \(row.etaText ?? "-")"
    }
}

private final class CockpitInspectorCard: NSView {
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
        let detailLength = detailLabel.stringValue.count
        return detailLength > 120 ? 92 : 70
    }

    override func layout() {
        marker.frame = NSRect(x: 12, y: 14, width: 6, height: bounds.height - 28)
        titleLabel.frame = NSRect(x: 28, y: 12, width: max(80, bounds.width - 142), height: 18)
        valueLabel.frame = NSRect(x: bounds.width - 106, y: 12, width: 90, height: 18)
        detailLabel.frame = NSRect(x: 28, y: 34, width: max(80, bounds.width - 44), height: bounds.height - 42)
    }
}

private final class CockpitInspectorContentView: NSView {
    override var isFlipped: Bool { true }
}
