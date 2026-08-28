import AppKit

final class SystemTelemetryRow: RowView {
    private static let metricWidth: CGFloat = 44
    private static let metricGap: CGFloat = 14
    private var valueLabels: [NSTextField] = []
    private var disclosureLabel: NSTextField!
    private var config: Config
    private var expanded: Bool

    static func metricFrames(metricCount: Int, panelWidth: CGFloat) -> [NSRect] {
        guard metricCount > 0 else { return [] }
        let count = CGFloat(metricCount)
        let contentWidth = panelWidth - Tokens.Layout.rowPadL - Tokens.Layout.rowPadR
        let groupWidth = count * metricWidth + CGFloat(max(0, metricCount - 1)) * metricGap
        let groupX = Tokens.Layout.rowPadL + max(0, (contentWidth - groupWidth) / 2)
        return (0..<metricCount).map { index in
            NSRect(
                x: groupX + CGFloat(index) * (metricWidth + metricGap),
                y: 0,
                width: metricWidth,
                height: 0
            )
        }
    }

    init(snapshot: SystemTelemetrySnapshot?, config: Config, expanded: Bool = false, onOpen: @escaping () -> Void = {}) {
        self.config = config
        self.expanded = expanded
        super.init(frame: NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: 30))
        toolTip = "Open system stats"

        let metrics = Self.metrics(snapshot)
        let metricFrames = Self.metricFrames(metricCount: metrics.count, panelWidth: bounds.width)
        for (metric, frame) in zip(metrics, metricFrames) {
            let name = UI.label(metric.0, size: 8.5, weight: .semibold, color: Tokens.Color.sectionGray, align: .center)
            name.frame = NSRect(x: frame.minX, y: 2, width: frame.width, height: 12)
            addSubview(name)
            let value = UI.label(
                "N/A",
                size: 11,
                weight: .bold,
                color: Tokens.Color.sectionGray,
                align: .center
            )
            value.font = .monospacedDigitSystemFont(ofSize: 11, weight: .bold)
            value.frame = NSRect(x: frame.minX, y: 14, width: frame.width, height: 14)
            addSubview(value)
            valueLabels.append(value)
        }
        disclosureLabel = UI.label(expanded ? "⌄" : "›", size: 17, weight: .medium, color: Tokens.Color.dimGray, align: .right)
        disclosureLabel.frame = NSRect(x: bounds.width - 25, y: 6, width: 13, height: 18)
        addSubview(disclosureLabel)
        let button = TelemetryOpenButton(frame: bounds, onOpen: onOpen)
        button.autoresizingMask = [.width, .height]
        addSubview(button)
        setAccessibilityElement(true)
        setAccessibilityRole(.button)
        update(snapshot: snapshot, config: config, expanded: expanded)
    }

    func update(snapshot: SystemTelemetrySnapshot?, config: Config? = nil, expanded: Bool? = nil) {
        if let config { self.config = config }
        if let expanded { self.expanded = expanded }
        let metrics = Self.metrics(snapshot)
        let policy = SystemTelemetryDisplayPolicy(
            warningThreshold: self.config.systemTelemetryWarningThreshold,
            criticalThreshold: self.config.systemTelemetryCriticalThreshold
        )
        for (valueLabel, metric) in zip(valueLabels, metrics) {
            valueLabel.stringValue = metric.1.map { "\(Int($0.rounded()))%" } ?? "N/A"
            valueLabel.textColor = Self.color(for: policy.severity(for: metric.1))
        }
        disclosureLabel.stringValue = self.expanded ? "⌄" : "›"
        setAccessibilityLabel(metrics.map { "\($0.0) \($0.1.map { "\(Int($0.rounded())) percent" } ?? "unavailable")" }.joined(separator: ", "))
        setAccessibilityHelp(self.expanded ? "Collapses inline CPU, GPU, memory, and disk statistics" : "Expands inline CPU, GPU, memory, and disk statistics")
    }

    private static func metrics(_ snapshot: SystemTelemetrySnapshot?) -> [(String, Double?)] {
        let userVisibleDisk = snapshot.flatMap { SystemTelemetryDiskCapacity.presentation(for: $0.disk) }
        return [
            ("CPU", snapshot?.cpu.availablePercent),
            ("GPU", snapshot?.gpu.availablePercent),
            ("RAM", snapshot?.memory.availablePercent),
            ("DISK", userVisibleDisk?.usedPercent ?? snapshot?.disk.availablePercent),
        ]
    }

    static func color(for severity: SystemTelemetrySeverity) -> NSColor {
        switch severity {
        case .unavailable: return Tokens.Color.sectionGray
        case .normal: return .systemBlue
        case .warning: return .systemOrange
        case .critical: return .systemRed
        }
    }

    required init?(coder: NSCoder) { nil }
}

private final class TelemetryOpenButton: NSButton {
    private let onOpen: () -> Void

    init(frame: NSRect, onOpen: @escaping () -> Void) {
        self.onOpen = onOpen
        super.init(frame: frame)
        title = ""
        isBordered = false
        isTransparent = true
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor
        target = self
        action = #selector(openStats)
        setAccessibilityLabel("Open system stats")
        setAccessibilityRole(.button)
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        addTrackingArea(NSTrackingArea(
            rect: bounds,
            options: [.mouseEnteredAndExited, .activeInKeyWindow],
            owner: self,
            userInfo: nil
        ))
    }

    override func mouseEntered(with event: NSEvent) {
        layer?.backgroundColor = NSColor.labelColor.withAlphaComponent(0.035).cgColor
    }

    override func mouseExited(with event: NSEvent) {
        layer?.backgroundColor = NSColor.clear.cgColor
    }

    @objc private func openStats() { onOpen() }
    required init?(coder: NSCoder) { nil }
}
