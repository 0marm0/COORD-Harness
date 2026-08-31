import AppKit

/// Standalone Stats detail surface. Its narrow, vertically-scannable geometry and information
/// hierarchy is the canonical visual baseline for COORD's dense Stats dashboard.
enum SystemTelemetryStatsLayoutContract {
    static let preferredWidth: CGFloat = 276
    static let preferredHeight: CGFloat = 472
    static let minimumWidth: CGFloat = 268
    static let powerToolbarHeight: CGFloat = 24
    static let batteryWidth: CGFloat = 72
    static let batteryHeight: CGFloat = 16
    static let moduleHeight: CGFloat = 88
    static let ringDiameter: CGFloat = 56
    static let utilizationHeight: CGFloat = 60
    static let batteryModes = LocalEnergyMode.allCases
    static let moduleOrder = ["RAM", "GPU", "CPU", "DISK"]
}

final class PremiumSystemTelemetryDetailView: NSView {
    static let preferredSize = NSSize(width: SystemTelemetryStatsLayoutContract.preferredWidth, height: SystemTelemetryStatsLayoutContract.preferredHeight)

    private var snapshot: SystemTelemetrySnapshot?
    private let config: Config
    private var history: [SystemTelemetryHistorySample]
    private var battery: LocalBatterySnapshot
    private let onToggleChargeLimit: (Int, Int) -> Void
    private let onSetEnergyMode: (LocalPowerSource, LocalEnergyMode, Int) -> Void
    private let backgroundGlass = NSVisualEffectView()
    private let backgroundTint = NSView()

    override var isFlipped: Bool { true }

    init(
        snapshot: SystemTelemetrySnapshot?,
        config: Config,
        history: [SystemTelemetryHistorySample],
        battery: LocalBatterySnapshot = .unavailable,
        size: NSSize = preferredSize,
        onToggleChargeLimit: @escaping (Int, Int) -> Void = { _, _ in },
        onSetEnergyMode: @escaping (LocalPowerSource, LocalEnergyMode, Int) -> Void = { _, _, _ in }
    ) {
        self.snapshot = snapshot
        self.config = config
        self.history = history
        self.battery = battery
        self.onToggleChargeLimit = onToggleChargeLimit
        self.onSetEnergyMode = onSetEnergyMode
        super.init(frame: NSRect(origin: .zero, size: size))
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor
        backgroundGlass.frame = bounds
        backgroundGlass.autoresizingMask = [.width, .height]
        backgroundGlass.material = .hudWindow
        backgroundGlass.blendingMode = .behindWindow
        backgroundGlass.state = .active
        addSubview(backgroundGlass)
        backgroundTint.frame = bounds
        backgroundTint.autoresizingMask = [.width, .height]
        backgroundTint.wantsLayer = true
        backgroundTint.layer?.backgroundColor = NSColor(
            calibratedRed: 0.018, green: 0.021, blue: 0.026, alpha: 0.92
        ).cgColor
        addSubview(backgroundTint)
        rebuild()
    }

    func update(snapshot: SystemTelemetrySnapshot?, history: [SystemTelemetryHistorySample]) {
        self.snapshot = snapshot
        self.history = history
        rebuild()
    }

    func updatePower(battery: LocalBatterySnapshot) {
        self.battery = battery
        rebuild()
    }

    private func rebuild() {
        subviews
            .filter { $0 !== backgroundGlass && $0 !== backgroundTint }
            .forEach { $0.removeFromSuperview() }
        toolbar()
        metricModules()
        utilizationHistory()
        setAccessibilityElement(true)
        setAccessibilityRole(.group)
        setAccessibilityLabel("COORD system stats with four vertical modules and compact utilization history")
    }

    private func toolbar() {
        let batteryX = (bounds.width - SystemTelemetryStatsLayoutContract.batteryWidth) / 2
        let batteryControl = PremiumStatsBatteryButton(
            frame: NSRect(x: batteryX, y: 6, width: SystemTelemetryStatsLayoutContract.batteryWidth,
                          height: SystemTelemetryStatsLayoutContract.batteryHeight),
            snapshot: battery
        )
        batteryControl.onToggle = { [weak self] expected, target in self?.onToggleChargeLimit(expected, target) }
        addSubview(batteryControl)

        let modeWidth: CGFloat = 18
        let modeSpacing: CGFloat = 8
        let selectorWidth = CGFloat(SystemTelemetryStatsLayoutContract.batteryModes.count) * modeWidth
            + CGFloat(SystemTelemetryStatsLayoutContract.batteryModes.count - 1) * modeSpacing
        var modeX = bounds.width - selectorWidth - 8
        for mode in SystemTelemetryStatsLayoutContract.batteryModes {
            let button = PremiumStatsEnergyModeButton(
                frame: NSRect(x: modeX, y: 4, width: modeWidth, height: 18),
                mode: mode,
                selected: battery.activeEnergyModeRaw == mode.rawValue,
                enabled: battery.energyModeMutationAvailable
                    && (mode != .high || battery.highPowerModeSupported)
            )
            button.onSelect = { [weak self] selected in
                guard let self, let source = self.battery.activePowerSource,
                      let expected = self.battery.activeEnergyModeRaw else { return }
                self.onSetEnergyMode(source, selected, expected)
            }
            addSubview(button)
            modeX += modeWidth + modeSpacing
        }
    }

    private func metricModules() {
        let inset: CGFloat = 10
        let surfaceY: CGFloat = 34
        let width = bounds.width - inset * 2
        let composition = SystemTelemetrySnapshot.MemoryRingComposition.make(snapshot?.memory)
        let diskPercent = snapshot.flatMap { SystemTelemetryDiskCapacity.presentation(for: $0.disk)?.usedPercent }
            ?? snapshot?.disk.availablePercent
        let modules: [(String, Double?, NSColor, [(String, String)])] = [
            ("RAM", composition.centerUsedPercent, .systemBlue, [
                ("App", composition.appBytes.map(SystemTelemetryDetailFormatter.bytes) ?? "-"),
                ("Wired", composition.wiredBytes.map(SystemTelemetryDetailFormatter.bytes) ?? "-"),
                ("Compressed", composition.compressedBytes.map(SystemTelemetryDetailFormatter.bytes) ?? "-"),
                ("Free", composition.physicalFreeBytes.map(SystemTelemetryDetailFormatter.bytes) ?? "-"),
            ]),
            ("GPU", snapshot?.gpu.availablePercent, .systemPurple, [
                ("Usage", snapshot?.gpu.availablePercent.map(SystemTelemetryDetailFormatter.percent) ?? "-"),
                ("Render", snapshot?.gpu.rendererPercent.map(SystemTelemetryDetailFormatter.percent) ?? "-"),
                ("Temp", snapshot?.gpu.temperatureC.map { String(format: "%.0f °C", $0) } ?? snapshot?.gpu.powerW.map { String(format: "%.1f W", $0) } ?? "-"),
            ]),
            ("CPU", snapshot?.cpu.availablePercent, .systemCyan, [
                ("Usage", snapshot?.cpu.availablePercent.map(SystemTelemetryDetailFormatter.percent) ?? "-"),
                ("P / E", "\(snapshot?.cpu.pCoreUsagePercent.map(SystemTelemetryDetailFormatter.percent) ?? "-") / \(snapshot?.cpu.eCoreUsagePercent.map(SystemTelemetryDetailFormatter.percent) ?? "-")"),
                ("Temp", snapshot?.cpu.temperatureC.map { String(format: "%.0f °C", $0) } ?? "-"),
            ]),
            ("DISK", diskPercent, diskTint, [
                ("Used", snapshot.flatMap { SystemTelemetryDiskCapacity.presentation(for: $0.disk) }.map { SystemTelemetryDetailFormatter.bytes($0.usedBytes) } ?? "-"),
                ("Free", snapshot.flatMap { SystemTelemetryDiskCapacity.presentation(for: $0.disk) }.map { SystemTelemetryDetailFormatter.bytes($0.freeBytes) } ?? "-"),
                ("Read", snapshot?.disk.readBps.map(SystemTelemetryDetailFormatter.rate) ?? "-"),
                ("Write", snapshot?.disk.writeBps.map(SystemTelemetryDetailFormatter.rate) ?? "-"),
            ]),
        ]

        let surface = PremiumTelemetrySurface(
            frame: NSRect(x: inset, y: surfaceY, width: width,
                          height: SystemTelemetryStatsLayoutContract.moduleHeight * 4)
        )
        addSubview(surface)

        for (index, metric) in modules.enumerated() {
            let y = CGFloat(index) * SystemTelemetryStatsLayoutContract.moduleHeight
            addLabel(metric.0, to: surface,
                     frame: NSRect(x: 16, y: y + 4, width: 74, height: 12),
                     size: 10.5, weight: .bold, color: metric.2, align: .left)
            if metric.0 == "RAM" {
                surface.addSubview(PremiumSegmentedMemoryRing(
                    frame: NSRect(x: 18, y: y + 22, width: SystemTelemetryStatsLayoutContract.ringDiameter,
                                  height: SystemTelemetryStatsLayoutContract.ringDiameter),
                    composition: composition
                ))
            } else {
                surface.addSubview(PremiumTelemetryRing(
                    frame: NSRect(x: 18, y: y + 22, width: SystemTelemetryStatsLayoutContract.ringDiameter,
                                  height: SystemTelemetryStatsLayoutContract.ringDiameter),
                    percent: metric.1, tint: metric.2, lineWidth: 6
                ))
            }
            let value = metric.1.map { String(format: "%.0f%%", $0) } ?? "–"
            let center = addLabel(value, to: surface,
                                  frame: NSRect(x: 18, y: y + 41, width: SystemTelemetryStatsLayoutContract.ringDiameter, height: 15),
                                  size: 14, weight: .medium,
                                  color: Tokens.Color.white.withAlphaComponent(0.96), align: .center)
            center.font = .monospacedDigitSystemFont(ofSize: 14, weight: .medium)
            for (row, fact) in metric.3.enumerated() {
                let color: NSColor? = metric.0 == "RAM"
                    ? [
                        NSColor.systemBlue,
                        NSColor(calibratedRed: 1.00, green: 0.56, blue: 0.20, alpha: 1),
                        NSColor(calibratedRed: 1.00, green: 0.22, blue: 0.40, alpha: 1),
                        NSColor.white.withAlphaComponent(0.32),
                    ][row]
                    : nil
                addVerticalFact(fact.0, fact.1, color: color, to: surface,
                                y: y + 24 + CGFloat(row) * 13, width: width)
            }
            if index < modules.count - 1 {
                let separator = NSView(frame: NSRect(x: 10, y: y + SystemTelemetryStatsLayoutContract.moduleHeight - 0.5,
                                                     width: width - 20, height: 0.5))
                separator.wantsLayer = true
                separator.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.07).cgColor
                surface.addSubview(separator)
            }
        }
    }

    private func addVerticalFact(
        _ label: String, _ value: String, color: NSColor?,
        to surface: NSView, y: CGFloat, width: CGFloat
    ) {
        let dotX: CGFloat = 106
        if let color {
            let dot = NSView(frame: NSRect(x: dotX, y: y + 3, width: 5, height: 5))
            dot.wantsLayer = true
            dot.layer?.backgroundColor = color.cgColor
            dot.layer?.cornerRadius = 1
            surface.addSubview(dot)
        }
        addLabel(label, to: surface,
                 frame: NSRect(x: color == nil ? dotX : dotX + 9, y: y, width: 48, height: 11),
                 size: 9.5, color: Tokens.Color.sectionGray)
        let valueLabel = addLabel(value, to: surface,
                                  frame: NSRect(x: 160, y: y, width: width - 178, height: 12),
                                  size: 9.5, weight: .semibold, color: Tokens.Color.lightGray, align: .right)
        valueLabel.font = .monospacedDigitSystemFont(ofSize: 9.5, weight: .semibold)
    }

    private func utilizationHistory() {
        let panelY: CGFloat = 394
        let panelHeight = min(SystemTelemetryStatsLayoutContract.utilizationHeight, bounds.height - panelY - 8)
        let panel = PremiumTelemetrySurface(
            frame: NSRect(x: 10, y: panelY, width: bounds.width - 20, height: panelHeight)
        )
        addSubview(panel)
        addLabel("UTILIZATION", to: panel, frame: NSRect(x: 8, y: 4, width: 90, height: 9),
                 size: 8.5, weight: .semibold, color: Tokens.Color.dimGray)
        if history.count >= 2 {
            panel.addSubview(PremiumUtilizationHistoryView(
                frame: NSRect(x: 8, y: 17, width: panel.bounds.width - 16, height: panel.bounds.height - 22),
                samples: history, diskTint: diskTint
            ))
        } else {
            addLabel("Collecting history", to: panel,
                     frame: NSRect(x: 0, y: panel.bounds.midY - 6, width: panel.bounds.width, height: 12),
                     size: 8, weight: .medium, color: Tokens.Color.dimGray, align: .center)
        }
    }

    private var cpuFacts: [(String, String)] {
        [
            ("P / E", "\(snapshot?.cpu.pCoreUsagePercent.map(SystemTelemetryDetailFormatter.percent) ?? "-") / \(snapshot?.cpu.eCoreUsagePercent.map(SystemTelemetryDetailFormatter.percent) ?? "-")"),
            ("Temp", snapshot?.cpu.temperatureC.map { String(format: "%.0f °C", $0) } ?? "-"),
        ]
    }

    private var gpuFacts: [(String, String)] {
        [
            ("Render", snapshot?.gpu.rendererPercent.map(SystemTelemetryDetailFormatter.percent) ?? "-"),
            ("Temp", snapshot?.gpu.temperatureC.map { String(format: "%.0f °C", $0) } ?? snapshot?.gpu.powerW.map { String(format: "%.1f W", $0) } ?? "-"),
        ]
    }

    private var memoryFacts: [(String, String)] {
        [
            ("Free", snapshot?.memory.freeBytes.map(SystemTelemetryDetailFormatter.bytes) ?? "-"),
            ("Swap", snapshot?.memory.swapUsedBytes.map(SystemTelemetryDetailFormatter.bytes) ?? "-"),
        ]
    }

    private var diskFacts: [(String, String)] {
        let capacity = snapshot.flatMap { SystemTelemetryDiskCapacity.presentation(for: $0.disk) }
        let read = snapshot?.disk.readBps.map(SystemTelemetryDetailFormatter.rate) ?? "-"
        let write = snapshot?.disk.writeBps.map(SystemTelemetryDetailFormatter.rate) ?? "-"
        return [
            ("Free", capacity.map { SystemTelemetryDetailFormatter.bytes($0.freeBytes) } ?? "-"),
            ("Read", read),
            ("Write", write),
        ]
    }

    private var diskTint: NSColor {
        let used = snapshot.flatMap { SystemTelemetryDiskCapacity.presentation(for: $0.disk)?.usedPercent }
            ?? snapshot?.disk.availablePercent
        guard let used else { return Tokens.Color.lightGray.withAlphaComponent(0.42) }
        if used >= config.systemTelemetryCriticalThreshold { return NSColor(red: 1.00, green: 0.28, blue: 0.31, alpha: 1) }
        if used >= config.systemTelemetryWarningThreshold { return Tokens.Color.statsWarningOrange }
        return NSColor(red: 0.25, green: 0.82, blue: 0.50, alpha: 1)
    }

    private func addInlineFact(_ label: String, _ value: String, to panel: NSView, y: CGFloat) {
        addLabel(label, to: panel, frame: NSRect(x: 9, y: y, width: 48, height: 11), size: 8.5, color: Tokens.Color.sectionGray)
        let valueLabel = addLabel(value, to: panel, frame: NSRect(x: 54, y: y, width: panel.bounds.width - 63, height: 11), size: 8.5, weight: .semibold, color: Tokens.Color.lightGray, align: .right)
        valueLabel.font = .monospacedDigitSystemFont(ofSize: 8.5, weight: .semibold)
    }

    private func addLegend(_ entries: [(String, NSColor)], to panel: NSView) {
        var x = panel.bounds.width - CGFloat(entries.count) * 48 - 8
        for entry in entries {
            let dot = NSView(frame: NSRect(x: x, y: 11, width: 5, height: 5))
            dot.wantsLayer = true
            dot.layer?.backgroundColor = entry.1.cgColor
            dot.layer?.cornerRadius = 2.5
            panel.addSubview(dot)
            addLabel(entry.0, to: panel, frame: NSRect(x: x + 8, y: 8, width: 34, height: 11), size: 7.5, color: Tokens.Color.dimGray)
            x += 48
        }
    }

    @discardableResult
    private func addLabel(
        _ text: String,
        to parent: NSView? = nil,
        frame: NSRect,
        size: CGFloat,
        weight: NSFont.Weight = .regular,
        color: NSColor,
        align: NSTextAlignment = .left
    ) -> NSTextField {
        let label = UI.label(text, size: size, weight: weight, color: color, align: align)
        label.frame = frame
        (parent ?? self).addSubview(label)
        return label
    }

    required init?(coder: NSCoder) { nil }
}

private final class PremiumStatsBatteryButton: NSButton {
    let snapshot: LocalBatterySnapshot
    var onToggle: ((Int, Int) -> Void)?

    init(frame: NSRect, snapshot: LocalBatterySnapshot) {
        self.snapshot = snapshot
        super.init(frame: frame)
        title = ""
        isBordered = false
        focusRingType = .none
        isEnabled = snapshot.chargeLimitMutationAvailable
        identifier = NSUserInterfaceItemIdentifier("coord.stats.battery-limit")
        target = self
        action = #selector(toggle)
        let limitState = snapshot.chargeLimitEnabled ? "on" : "off"
        let percent = snapshot.percent.map { "\($0) percent" } ?? "unavailable"
        setAccessibilityLabel("Battery \(percent), 80 percent charge limit \(limitState)")
        setAccessibilityHelp(
            snapshot.chargeLimitMutationAvailable
                ? (snapshot.chargeLimitEnabled ? "Turn off 80% charge limit" : "Turn on 80% charge limit")
                : "Charge-limit control unavailable until exact readback is available"
        )
        toolTip = accessibilityHelp()
    }

    override func draw(_ dirtyRect: NSRect) {
        let bodyRect = NSRect(x: 0.5, y: 1, width: bounds.width - 6, height: bounds.height - 2)
        let shell = NSBezierPath(roundedRect: bodyRect, xRadius: 4, yRadius: 4)
        NSColor.white.withAlphaComponent(0.07).setFill()
        shell.fill()
        if let percent = snapshot.percent {
            NSGraphicsContext.saveGraphicsState()
            shell.addClip()
            NSColor.systemGreen.withAlphaComponent(snapshot.adapterPowered ? 0.72 : 0.32).setFill()
            let inset = bodyRect.insetBy(dx: 2, dy: 2)
            NSRect(
                x: inset.minX, y: inset.minY,
                width: inset.width * CGFloat(min(100, max(0, percent))) / 100,
                height: inset.height
            ).fill()
            NSGraphicsContext.restoreGraphicsState()
        }
        shell.lineWidth = 1
        NSColor.systemGreen.withAlphaComponent(0.82).setStroke()
        shell.stroke()
        let terminal = NSBezierPath(roundedRect: NSRect(x: bodyRect.maxX + 1, y: bounds.midY - 3, width: 3, height: 6), xRadius: 1, yRadius: 1)
        NSColor.systemGreen.withAlphaComponent(0.82).setFill()
        terminal.fill()

        let label = snapshot.percent.map { "\($0)%" } ?? "-%"
        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 7.5, weight: .semibold),
            .foregroundColor: NSColor.white.withAlphaComponent(0.96),
        ]
        let size = label.size(withAttributes: attributes)
        label.draw(
            at: NSPoint(x: bodyRect.midX - size.width / 2 - (snapshot.chargeLimitEnabled ? 4 : 0),
                        y: bodyRect.midY - size.height / 2),
            withAttributes: attributes
        )
        if snapshot.chargeLimitEnabled {
            let pause = NSBezierPath()
            for x in [bodyRect.midX + 9, bodyRect.midX + 12] {
                pause.move(to: NSPoint(x: x, y: bodyRect.midY - 3))
                pause.line(to: NSPoint(x: x, y: bodyRect.midY + 3))
            }
            pause.lineWidth = 0.75
            NSColor.white.withAlphaComponent(0.96).setStroke()
            pause.stroke()
        }
    }

    @objc private func toggle() {
        guard let expected = snapshot.chargeLimit, snapshot.chargeLimitMutationAvailable else { return }
        onToggle?(expected, snapshot.nextChargeLimit)
    }

    required init?(coder: NSCoder) { nil }
}

private final class PremiumStatsEnergyModeButton: NSButton {
    let mode: LocalEnergyMode
    var onSelect: ((LocalEnergyMode) -> Void)?

    init(frame: NSRect, mode: LocalEnergyMode, selected: Bool, enabled: Bool) {
        self.mode = mode
        super.init(frame: frame)
        title = ""
        image = NSImage(systemSymbolName: mode.symbolName, accessibilityDescription: mode.title)
        imagePosition = .imageOnly
        isBordered = false
        isEnabled = enabled
        focusRingType = .none
        font = .systemFont(ofSize: 8.5, weight: .semibold)
        contentTintColor = selected ? .white : Tokens.Color.lightGray.withAlphaComponent(0.32)
        identifier = NSUserInterfaceItemIdentifier("coord.stats.energy-mode.\(mode.rawValue)")
        target = self
        action = #selector(selectMode)
        setAccessibilityLabel("Set \(mode.title) Energy Mode")
        setAccessibilityValue(selected ? "Selected" : "Not selected")
        toolTip = "\(mode.title) Energy Mode"
    }

    @objc private func selectMode() { onSelect?(mode) }

    required init?(coder: NSCoder) { nil }
}

private final class PremiumTelemetrySurface: NSView {
    override var isFlipped: Bool { true }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor
    }

    required init?(coder: NSCoder) { nil }
}

private final class PremiumTelemetryPanel: NSView {
    private let accent: NSColor
    override var isFlipped: Bool { true }

    init(frame: NSRect, accent: NSColor) {
        self.accent = accent
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = NSColor.white.withAlphaComponent(0.045).cgColor
        layer?.borderColor = accent.withAlphaComponent(0.22).cgColor
        layer?.borderWidth = 0.75
        layer?.cornerRadius = 10
    }

    required init?(coder: NSCoder) { nil }
}

private final class PremiumTelemetryRing: NSView {
    private let percent: Double?
    private let tint: NSColor
    private let lineWidth: CGFloat

    init(frame: NSRect, percent: Double?, tint: NSColor, lineWidth: CGFloat = 6) {
        self.percent = percent
        self.tint = tint
        self.lineWidth = lineWidth
        super.init(frame: frame)
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let rect = bounds.insetBy(dx: lineWidth, dy: lineWidth)
        let track = NSBezierPath(ovalIn: rect)
        track.lineWidth = lineWidth
        NSColor.white.withAlphaComponent(0.10).setStroke()
        track.stroke()
        guard let percent, percent.isFinite else { return }
        let progress = NSBezierPath()
        progress.appendArc(
            withCenter: NSPoint(x: bounds.midX, y: bounds.midY),
            radius: min(rect.width, rect.height) / 2,
            startAngle: 90,
            endAngle: 90 - CGFloat(min(100, max(0, percent))) * 3.6,
            clockwise: true
        )
        progress.lineWidth = lineWidth
        progress.lineCapStyle = .round
        tint.setStroke()
        progress.stroke()
    }

    required init?(coder: NSCoder) { nil }
}

private final class PremiumSegmentedMemoryRing: NSView {
    private let composition: SystemTelemetrySnapshot.MemoryRingComposition

    init(frame: NSRect, composition: SystemTelemetrySnapshot.MemoryRingComposition) {
        self.composition = composition
        super.init(frame: frame)
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let lineWidth: CGFloat = 5
        let rect = bounds.insetBy(dx: lineWidth, dy: lineWidth)
        let track = NSBezierPath(ovalIn: rect)
        track.lineWidth = lineWidth
        NSColor.white.withAlphaComponent(0.10).setStroke()
        track.stroke()

        if composition.segments.isEmpty {
            guard composition.usesFallbackArc, let percent = composition.centerUsedPercent else { return }
            drawArc(from: 0, fraction: min(100, max(0, percent)) / 100, color: .systemBlue, lineWidth: lineWidth)
            return
        }

        var start = 0.0
        for segment in composition.segments {
            let color: NSColor
            switch segment.kind {
            case .app: color = .systemBlue
            case .wired: color = NSColor(calibratedRed: 1.00, green: 0.56, blue: 0.20, alpha: 1)
            case .compressed: color = NSColor(calibratedRed: 1.00, green: 0.22, blue: 0.40, alpha: 1)
            case .free: color = NSColor.white.withAlphaComponent(0.32)
            }
            drawArc(from: start, fraction: segment.fraction, color: color, lineWidth: lineWidth)
            start += segment.fraction
        }
    }

    private func drawArc(from start: Double, fraction: Double, color: NSColor, lineWidth: CGFloat) {
        guard fraction > 0 else { return }
        let radius = min(bounds.width, bounds.height) / 2 - lineWidth
        let arc = NSBezierPath()
        arc.appendArc(
            withCenter: NSPoint(x: bounds.midX, y: bounds.midY),
            radius: radius,
            startAngle: 90 - CGFloat(start) * 360,
            endAngle: 90 - CGFloat(start + fraction) * 360,
            clockwise: true
        )
        arc.lineWidth = lineWidth
        arc.lineCapStyle = .butt
        color.setStroke()
        arc.stroke()
    }

    required init?(coder: NSCoder) { nil }
}

private final class PremiumUtilizationHistoryView: NSView {
    private let samples: [SystemTelemetryHistorySample]
    private let diskTint: NSColor

    init(frame: NSRect, samples: [SystemTelemetryHistorySample], diskTint: NSColor) {
        self.samples = Array(samples.suffix(SystemTelemetryHistoryBuffer.capacity))
        self.diskTint = diskTint
        super.init(frame: frame)
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        for fraction in [CGFloat(0), 0.5, 1] {
            let y = fraction * bounds.height
            let grid = NSBezierPath()
            grid.move(to: NSPoint(x: 0, y: y))
            grid.line(to: NSPoint(x: bounds.width, y: y))
            grid.lineWidth = 0.5
            NSColor.white.withAlphaComponent(0.08).setStroke()
            grid.stroke()
        }
        drawLine(samples.map(\.cpuPercent), color: .systemCyan)
        drawLine(samples.map(\.gpuPercent), color: .systemPurple)
        drawLine(samples.map(\.memoryPercent), color: .systemBlue)
        drawLine(samples.map(\.diskPercent), color: diskTint)
    }

    private func drawLine(_ values: [Double?], color: NSColor) {
        guard values.count > 1 else { return }
        let path = NSBezierPath()
        var started = false
        for (index, raw) in values.enumerated() {
            guard let raw, raw.isFinite else { continue }
            let x = CGFloat(index) / CGFloat(max(1, values.count - 1)) * bounds.width
            let y = CGFloat(min(100, max(0, raw))) / 100 * bounds.height
            if started { path.line(to: NSPoint(x: x, y: y)) }
            else { path.move(to: NSPoint(x: x, y: y)); started = true }
        }
        path.lineWidth = 1.8
        path.lineJoinStyle = .round
        path.lineCapStyle = .round
        color.withAlphaComponent(0.9).setStroke()
        path.stroke()
    }

    required init?(coder: NSCoder) { nil }
}
