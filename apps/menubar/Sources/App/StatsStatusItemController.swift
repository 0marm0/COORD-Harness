import AppKit

final class StatsStatusItemController {
    var onClick: ((NSStatusBarButton) -> Void)?

    private var item: NSStatusItem?
    private var snapshot: SystemTelemetrySnapshot?
    private var config = Config()

    func setEnabled(_ enabled: Bool) {
        if enabled {
            guard item == nil else { return }
            let next = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
            next.autosaveName = "org.coordharness.menubar.stats"
            next.isVisible = true
            if let button = next.button {
                button.imagePosition = .imageOnly
                button.target = self
                button.action = #selector(clicked)
            }
            item = next
            render()
        } else {
            shutdown()
        }
    }

    func update(snapshot: SystemTelemetrySnapshot?, config: Config) {
        self.snapshot = snapshot
        self.config = config
        render()
    }

    func shutdown() {
        if let item { NSStatusBar.system.removeStatusItem(item) }
        item = nil
    }

    @objc private func clicked() {
        guard let button = item?.button else { return }
        onClick?(button)
    }

    private func render() {
        guard let button = item?.button else { return }
        let presentation = CoordStatsStatusItemPresentation.make(snapshot: snapshot, config: config)
        let compact = config.systemTelemetryCompactSpacing
        var image: NSImage?
        button.effectiveAppearance.performAsCurrentDrawingAppearance {
            image = CoordStatsStatusItemRenderer.image(presentation, compact: compact)
        }
        item?.length = CoordStatsStatusItemRenderer.width(
            itemCount: presentation.metrics.count,
            compact: compact
        )
        button.image = image
        button.title = ""
        button.attributedTitle = NSAttributedString()
        let values = presentation.metrics.map { "\($0.label) \($0.value)" }.joined(separator: ", ")
        let label = values.isEmpty ? "System stats unavailable" : "System stats: \(values)"
        button.toolTip = "\(label). Open Stats."
        button.setAccessibilityLabel(label)
        button.setAccessibilityHelp("Open COORD System stats.")
    }
}

struct CoordStatsStatusItemPresentation {
    struct Metric {
        let label: String
        let value: String
        let severity: SystemTelemetrySeverity
    }

    let metrics: [Metric]

    static func make(snapshot: SystemTelemetrySnapshot?, config: Config) -> Self {
        let stale = snapshot?.isStale != false
        let diskPercent = snapshot.flatMap {
            SystemTelemetryDiskCapacity.presentation(for: $0.disk)?.usedPercent
        } ?? snapshot?.disk.availablePercent
        let candidates: [(String, Bool, Double?)] = [
            ("GPU", config.systemTelemetryShowGPU, snapshot?.gpu.availablePercent),
            ("RAM", config.systemTelemetryShowRAM, snapshot?.memory.availablePercent),
            ("CPU", config.systemTelemetryShowCPU, snapshot?.cpu.availablePercent),
            ("DSK", config.systemTelemetryShowDisk, diskPercent),
        ]
        let policy = SystemTelemetryDisplayPolicy(
            warningThreshold: config.systemTelemetryWarningThreshold,
            criticalThreshold: config.systemTelemetryCriticalThreshold
        )
        return Self(metrics: candidates.compactMap { label, enabled, percent in
            guard enabled else { return nil }
            return Metric(
                label: label,
                value: stale ? "N/A" : percent.map { "\(Int($0.rounded()))" } ?? "N/A",
                severity: stale ? .unavailable : policy.severity(for: percent)
            )
        })
    }
}

enum CoordStatsStatusItemRenderer {
    static let height: CGFloat = StatusItemImageLayout.height
    static let horizontalPadding: CGFloat = 1

    static func moduleWidth(compact: Bool) -> CGFloat { compact ? 30 : 34 }
    static func spacing(compact: Bool) -> CGFloat { compact ? 1 : 3 }

    static func width(itemCount: Int, compact: Bool = true) -> CGFloat {
        let count = max(1, itemCount)
        let moduleWidth = moduleWidth(compact: compact)
        let spacing = spacing(compact: compact)
        return horizontalPadding * 2
            + CGFloat(count) * moduleWidth
            + CGFloat(max(0, count - 1)) * spacing
    }

    static func image(
        _ presentation: CoordStatsStatusItemPresentation,
        compact: Bool = true
    ) -> NSImage {
        let moduleWidth = moduleWidth(compact: compact)
        let spacing = spacing(compact: compact)
        let width = width(itemCount: presentation.metrics.count, compact: compact)
        let image = NSImage(size: NSSize(width: width, height: height), flipped: false) { _ in
            if presentation.metrics.isEmpty {
                draw(
                    "Stats -",
                    in: NSRect(x: 0, y: 4, width: width, height: 14),
                    size: 9,
                    color: .secondaryLabelColor
                )
                return true
            }
            for (index, metric) in presentation.metrics.enumerated() {
                let x = horizontalPadding + CGFloat(index) * (moduleWidth + spacing)
                draw(
                    metric.label,
                    in: NSRect(x: x, y: 13, width: moduleWidth, height: 9),
                    size: 7,
                    color: NSColor.labelColor.withAlphaComponent(0.72)
                )
                draw(
                    metric.value,
                    in: NSRect(x: x, y: 0, width: moduleWidth, height: 14),
                    size: 12,
                    weight: .regular,
                    color: color(for: metric.severity)
                )
            }
            return true
        }
        image.isTemplate = false
        image.accessibilityDescription = "System stats"
        return image
    }

    private static func color(for severity: SystemTelemetrySeverity) -> NSColor {
        switch severity {
        case .unavailable: return .secondaryLabelColor
        case .normal: return .systemBlue
        case .warning: return Tokens.Color.statsWarningOrange
        case .critical: return .systemRed
        }
    }

    private static func draw(
        _ text: String,
        in rect: NSRect,
        size: CGFloat,
        weight: NSFont.Weight = .medium,
        color: NSColor
    ) {
        let paragraph = NSMutableParagraphStyle()
        paragraph.alignment = .center
        NSAttributedString(string: text, attributes: [
            .font: NSFont.monospacedDigitSystemFont(ofSize: size, weight: weight),
            .foregroundColor: color,
            .paragraphStyle: paragraph,
        ]).draw(in: rect)
    }
}
