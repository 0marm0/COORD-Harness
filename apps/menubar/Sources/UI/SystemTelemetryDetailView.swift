import AppKit

final class SystemTelemetryDetailView: NSView {
    static let preferredSize = NSSize(width: 560, height: 630)

    private var snapshot: SystemTelemetrySnapshot?
    private let config: Config
    private let onClose: () -> Void
    private let showsCloseControl: Bool

    override var isFlipped: Bool { true }

    init(snapshot: SystemTelemetrySnapshot?, config: Config, showsCloseControl: Bool = true, onClose: @escaping () -> Void) {
        self.snapshot = snapshot
        self.config = config
        self.showsCloseControl = showsCloseControl
        self.onClose = onClose
        super.init(frame: NSRect(origin: .zero, size: Self.preferredSize))
        wantsLayer = true
        layer?.backgroundColor = NSColor(calibratedWhite: 0.025, alpha: 0.36).cgColor
        rebuild()
    }

    func update(snapshot: SystemTelemetrySnapshot?) {
        self.snapshot = snapshot
        rebuild()
    }

    private func rebuild() {
        subviews.forEach { $0.removeFromSuperview() }
        let policy = SystemTelemetryDisplayPolicy(
            warningThreshold: config.systemTelemetryWarningThreshold,
            criticalThreshold: config.systemTelemetryCriticalThreshold
        )

        addLabel("System stats", frame: NSRect(x: 20, y: 18, width: 280, height: 24), size: 18, weight: .semibold, color: Tokens.Color.white.withAlphaComponent(0.96))

        if showsCloseControl {
            let close = NSButton(title: "Done", target: self, action: #selector(closeDetail))
            close.isBordered = false
            close.font = .systemFont(ofSize: 11, weight: .semibold)
            close.contentTintColor = Tokens.Color.progressBlueLt
            close.frame = NSRect(x: bounds.width - 64, y: 18, width: 48, height: 24)
            close.autoresizingMask = [.minXMargin]
            addSubview(close)
        }


        let cpu = metricCard(
            title: "CPU",
            value: SystemTelemetryDetailFormatter.percent(snapshot?.cpu.availablePercent),
            severity: policy.severity(for: snapshot?.cpu.availablePercent),
            source: snapshot?.cpu.source,
            rows: cpuRows(),
            error: snapshot?.cpu.error
        )
        cpu.frame = NSRect(x: 18, y: 68, width: 254, height: 260)
        addSubview(cpu)

        let gpu = metricCard(
            title: "GPU",
            value: SystemTelemetryDetailFormatter.percent(snapshot?.gpu.availablePercent),
            severity: policy.severity(for: snapshot?.gpu.availablePercent),
            source: snapshot?.gpu.source,
            rows: gpuRows(),
            error: snapshot?.gpu.error
        )
        gpu.frame = NSRect(x: 288, y: 68, width: 254, height: 260)
        addSubview(gpu)

        let memoryUsed = SystemTelemetryDetailFormatter.bytes(snapshot?.memory.usedBytes)
        let memoryTotal = SystemTelemetryDetailFormatter.bytes(snapshot?.memory.totalBytes)
        let memory = metricCard(
            title: "MEMORY",
            value: SystemTelemetryDetailFormatter.percent(snapshot?.memory.availablePercent),
            severity: policy.severity(for: snapshot?.memory.availablePercent),
            source: snapshot?.memory.source,
            rows: memoryRows(used: memoryUsed, total: memoryTotal),
            error: snapshot?.memory.error
        )
        memory.frame = NSRect(x: 18, y: 342, width: 254, height: 250)
        addSubview(memory)

        let userVisibleDisk = snapshot.flatMap { SystemTelemetryDiskCapacity.presentation(for: $0.disk) }
        let diskPercent = userVisibleDisk?.usedPercent ?? snapshot?.disk.availablePercent
        let disk = metricCard(
            title: "DISK",
            value: SystemTelemetryDetailFormatter.percent(diskPercent),
            severity: policy.severity(for: diskPercent),
            source: snapshot?.disk.source,
            rows: diskRows(),
            error: snapshot?.disk.error
        )
        disk.frame = NSRect(x: 288, y: 342, width: 254, height: 250)
        addSubview(disk)

        let cadence = snapshot?.cadence
        var footerParts = [snapshot?.freshness?.state.lowercased(), snapshot?.profile ?? cadence?.mode].compactMap { $0 }
        if let interval = cadence?.intervalSeconds {
            footerParts.append("sample \(SystemTelemetryDetailFormatter.interval(interval))")
        }
        if let age = snapshot?.freshness?.ageSeconds { footerParts.append(SystemTelemetryDetailFormatter.age(age)) }
        if let sequence = snapshot?.sequence { footerParts.append("sequence \(sequence)") }
        let footerText = footerParts.joined(separator: "  •  ")
        addLabel(footerText, frame: NSRect(x: 18, y: 606, width: 524, height: 16), size: 9.5, color: Tokens.Color.dimGray, align: .center)
    }

    private func metricCard(
        title: String,
        value: String,
        severity: SystemTelemetrySeverity,
        source: String?,
        rows: [(String, String)],
        error: String?
    ) -> NSView {
        let card = FlippedTelemetryCard()
        card.wantsLayer = true
        let tint = SystemTelemetryRow.color(for: severity)
        card.layer?.backgroundColor = NSColor(calibratedWhite: 0.07, alpha: 0.66).cgColor
        card.layer?.borderColor = tint.withAlphaComponent(0.28).cgColor
        card.layer?.borderWidth = 1
        card.layer?.cornerRadius = 13
        addLabel(title, to: card, frame: NSRect(x: 15, y: 13, width: 90, height: 13), size: 9, weight: .bold, color: Tokens.Color.sectionGray)
        let valueLabel = addLabel(value, to: card, frame: NSRect(x: 14, y: 30, width: 150, height: 32), size: 25, weight: .bold, color: tint)
        valueLabel.font = NSFont.monospacedDigitSystemFont(ofSize: 25, weight: .bold)

        if let source {
            let sourceText = "SOURCE  \(source.replacingOccurrences(of: "_", with: " ").uppercased())"
            addLabel(sourceText, to: card, frame: NSRect(x: 15, y: 64, width: 224, height: 12), size: 7.5, weight: .semibold, color: Tokens.Color.dimGray)
        }

        var y: CGFloat = 86
        for row in rows {
            addLabel(row.0, to: card, frame: NSRect(x: 15, y: y, width: 78, height: 14), size: 9.5, color: Tokens.Color.sectionGray)
            let detail = addLabel(row.1, to: card, frame: NSRect(x: 88, y: y, width: 121, height: 14), size: 9.5, weight: .medium, color: Tokens.Color.lightGray, align: .right)
            detail.font = .monospacedDigitSystemFont(ofSize: 9.5, weight: .medium)
            y += 24
        }
        if let error, !error.isEmpty {
            let errorLabel = addLabel(error, to: card, frame: NSRect(x: 15, y: y, width: 194, height: 13), size: 8.5, color: Tokens.Color.orange)
            errorLabel.lineBreakMode = .byTruncatingTail
        }
        return card
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


    private func cpuRows() -> [(String, String)] {
        guard let cpu = snapshot?.cpu else { return [] }
        var rows = [("Availability", cpu.availability.capitalized)]
        appendPercent("P-core load", cpu.pCoreUsagePercent, to: &rows)
        appendPercent("E-core load", cpu.eCoreUsagePercent, to: &rows)
        if let count = cpu.pCoreCount { rows.append(("P cores", "\(count)")) }
        if let count = cpu.eCoreCount { rows.append(("E cores", "\(count)")) }
        appendTemperature(cpu.temperatureC, to: &rows)
        return rows
    }

    private func gpuRows() -> [(String, String)] {
        guard let gpu = snapshot?.gpu else { return [] }
        var rows = [("Availability", gpu.availability.capitalized)]
        appendPercent("Renderer", gpu.rendererPercent, to: &rows)
        appendPercent("Tiler", gpu.tilerPercent, to: &rows)
        appendTemperature(gpu.temperatureC, to: &rows)
        appendPower("GPU power", gpu.powerW, to: &rows)
        appendPower("ANE power", gpu.anePowerW, to: &rows)
        return rows
    }

    private func memoryRows(used: String, total: String) -> [(String, String)] {
        guard let memory = snapshot?.memory else { return [] }
        var rows = [("Availability", memory.availability.capitalized)]
        if memory.usedBytes != nil || memory.totalBytes != nil {
            rows.append(("Used", memory.totalBytes == nil ? used : "\(used) / \(total)"))
        }
        if let free = memory.freeBytes { rows.append(("Free", SystemTelemetryDetailFormatter.bytes(free))) }
        if let swapUsed = memory.swapUsedBytes {
            let usedText = SystemTelemetryDetailFormatter.bytes(swapUsed)
            let swapText = memory.swapTotalBytes.map { "\(usedText) / \(SystemTelemetryDetailFormatter.bytes($0))" } ?? usedText
            rows.append(("Swap", swapText))
        }
        if let pressure = memory.pressure, !pressure.isEmpty { rows.append(("Pressure", pressure.capitalized)) }
        return rows
    }

    private func diskRows() -> [(String, String)] {
        guard let disk = snapshot?.disk else { return [] }
        var rows = [("Availability", disk.availability.capitalized)]
        if let used = disk.usedBytes {
            let usedText = SystemTelemetryDetailFormatter.bytes(used)
            let totalText = disk.totalBytes.map { "\(usedText) / \(SystemTelemetryDetailFormatter.bytes($0))" } ?? usedText
            rows.append(("Used", totalText))
        }
        if let free = disk.freeBytes { rows.append(("Free", SystemTelemetryDetailFormatter.bytes(free))) }
        if let read = disk.readBps { rows.append(("Read", SystemTelemetryDetailFormatter.rate(read))) }
        if let write = disk.writeBps { rows.append(("Write", SystemTelemetryDetailFormatter.rate(write))) }
        return rows
    }

    private func appendPercent(_ name: String, _ value: Double?, to rows: inout [(String, String)]) {
        guard value?.isFinite == true else { return }
        rows.append((name, SystemTelemetryDetailFormatter.percent(value)))
    }

    private func appendTemperature(_ value: Double?, to rows: inout [(String, String)]) {
        guard let value, value.isFinite else { return }
        rows.append(("Temperature", String(format: "%.1f °C", value)))
    }

    private func appendPower(_ name: String, _ value: Double?, to rows: inout [(String, String)]) {
        guard let value, value.isFinite else { return }
        rows.append((name, String(format: "%.1f W", value)))
    }


    @objc private func closeDetail() { onClose() }
    required init?(coder: NSCoder) { nil }
}


struct SystemTelemetryHistorySample: Equatable {
    let sequence: Int?
    let generatedAt: String?
    let cpuPercent: Double?
    let diskReadBps: Double?
    let diskWriteBps: Double?
}

final class SystemTelemetryHistoryBuffer {
    static let capacity = 30
    private(set) var samples: [SystemTelemetryHistorySample] = []

    func append(_ snapshot: SystemTelemetrySnapshot?) {
        guard let snapshot else { return }
        let sample = SystemTelemetryHistorySample(
            sequence: snapshot.sequence,
            generatedAt: snapshot.generatedAt,
            cpuPercent: snapshot.cpu.availablePercent,
            diskReadBps: snapshot.disk.readBps,
            diskWriteBps: snapshot.disk.writeBps
        )
        if let last = samples.last,
           (sample.sequence != nil && sample.sequence == last.sequence)
            || (sample.sequence == nil && sample.generatedAt != nil && sample.generatedAt == last.generatedAt) {
            samples[samples.count - 1] = sample
            return
        }
        samples.append(sample)
        if samples.count > Self.capacity { samples.removeFirst(samples.count - Self.capacity) }
    }
}

final class InlineSystemTelemetryDetailView: RowView {
    static let moduleHeight: CGFloat = 236
    private var snapshot: SystemTelemetrySnapshot?
    private let config: Config
    private var history: [SystemTelemetryHistorySample]

    init(snapshot: SystemTelemetrySnapshot?, config: Config, history: [SystemTelemetryHistorySample] = []) {
        self.snapshot = snapshot
        self.config = config
        self.history = history
        super.init(frame: NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: Self.moduleHeight + 4))
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor
        rebuild()
    }

    func update(snapshot: SystemTelemetrySnapshot?, history: [SystemTelemetryHistorySample]) {
        self.snapshot = snapshot
        self.history = history
        subviews.forEach { $0.removeFromSuperview() }
        rebuild()
    }

    private func rebuild() {
        let policy = SystemTelemetryDisplayPolicy(
            warningThreshold: config.systemTelemetryWarningThreshold,
            criticalThreshold: config.systemTelemetryCriticalThreshold
        )
        let module = InlineTelemetryModule(frame: NSRect(x: 12, y: 2, width: bounds.width - 24, height: Self.moduleHeight))
        module.wantsLayer = true
        // The outer popover already supplies COORD's dark glass. Keeping this
        // layer transparent avoids a second, differently tinted panel.
        module.layer?.backgroundColor = NSColor.clear.cgColor
        addSubview(module)

        addLabel("SYSTEM STATS", to: module, frame: NSRect(x: 14, y: 6, width: 120, height: 13), size: 8.5, weight: .semibold, color: Tokens.Color.sectionGray)
        let freshness = [snapshot?.freshness?.state.lowercased(), snapshot?.freshness?.ageSeconds.map(SystemTelemetryDetailFormatter.age)].compactMap { $0 }.joined(separator: "  ·  ")
        addLabel(freshness, to: module, frame: NSRect(x: 150, y: 6, width: module.bounds.width - 164, height: 13), size: 7.5, color: Tokens.Color.dimGray, align: .right)

        let cardGap: CGFloat = 6
        let cardInset: CGFloat = 10
        let cardWidth = (module.bounds.width - cardInset * 2 - cardGap * 3) / 4
        let diskPercent = snapshot.flatMap { SystemTelemetryDiskCapacity.presentation(for: $0.disk)?.usedPercent }
            ?? snapshot?.disk.availablePercent
        let models: [(String, Double?, [String])] = [
            ("CPU", snapshot?.cpu.availablePercent, cpuDetails()),
            ("GPU", snapshot?.gpu.availablePercent, gpuDetails()),
            ("RAM", snapshot?.memory.availablePercent, memoryDetails()),
            ("DISK", diskPercent, diskDetails()),
        ]
        for (index, model) in models.enumerated() {
            let frame = NSRect(x: cardInset + CGFloat(index) * (cardWidth + cardGap), y: 24, width: cardWidth, height: 94)
            module.addSubview(metricCard(
                title: model.0,
                percent: model.1,
                details: model.2,
                tint: SystemTelemetryRow.color(for: policy.severity(for: model.1)),
                frame: frame
            ))
        }

        module.addSubview(cpuHistoryCard(frame: NSRect(x: 10, y: 126, width: 157, height: 102)))
        module.addSubview(diskHistoryCard(frame: NSRect(x: 173, y: 126, width: 157, height: 102)))
        setAccessibilityElement(true)
        setAccessibilityRole(.group)
        setAccessibilityLabel("Expanded system stats with CPU, GPU, memory, disk, and recent activity")
    }

    private func metricCard(title: String, percent: Double?, details: [String], tint: NSColor, frame: NSRect) -> NSView {
        let card = InlineTelemetryCard(frame: frame)
        card.wantsLayer = true
        card.layer?.backgroundColor = NSColor.clear.cgColor

        addLabel(title, to: card, frame: NSRect(x: 5, y: 4, width: frame.width - 10, height: 11), size: 7.5, weight: .semibold, color: Tokens.Color.sectionGray, align: .center)
        let ringSize: CGFloat = 42
        let ringX = (frame.width - ringSize) / 2
        card.addSubview(TelemetryRingView(frame: NSRect(x: ringX, y: 16, width: ringSize, height: ringSize), percent: percent, tint: tint))
        let value = addLabel(SystemTelemetryDetailFormatter.percent(percent), to: card, frame: NSRect(x: 2, y: 28, width: frame.width - 4, height: 18), size: 10.5, weight: .semibold, color: tint, align: .center)
        value.font = .monospacedDigitSystemFont(ofSize: 11, weight: .semibold)
        if title == "CPU", let cpu = snapshot?.cpu {
            card.addSubview(TelemetryCoreLoadView(
                frame: NSRect(x: 4, y: 60, width: frame.width - 8, height: 23),
                pCore: cpu.pCoreUsagePercent,
                eCore: cpu.eCoreUsagePercent
            ))
            if details.count > 1 {
                addLabel(details[1], to: card, frame: NSRect(x: 4, y: 83, width: frame.width - 8, height: 9), size: 6.5, color: Tokens.Color.lightGray, align: .center)
            }
        } else {
            for (index, text) in details.prefix(2).enumerated() {
                let label = addLabel(text, to: card, frame: NSRect(x: 4, y: 62 + CGFloat(index) * 14, width: frame.width - 8, height: 11), size: 7, color: Tokens.Color.lightGray, align: .center)
                label.lineBreakMode = .byTruncatingMiddle
            }
        }
        return card
    }

    private func cpuHistoryCard(frame: NSRect) -> NSView {
        let card = historyCard(title: "CPU HISTORY", frame: frame)
        let samples = history.compactMap(\.cpuPercent)
        if samples.isEmpty {
            addLabel("Collecting samples", to: card, frame: NSRect(x: 10, y: 49, width: frame.width - 20, height: 13), size: 8, color: Tokens.Color.dimGray, align: .center)
        } else {
            card.addSubview(TelemetryBarHistoryView(frame: NSRect(x: 10, y: 28, width: frame.width - 20, height: 50), values: samples, tint: .systemBlue))
            addLabel("0", to: card, frame: NSRect(x: 10, y: 80, width: 30, height: 10), size: 6.5, color: Tokens.Color.dimGray)
            addLabel("NOW", to: card, frame: NSRect(x: frame.width - 40, y: 80, width: 30, height: 10), size: 6.5, color: Tokens.Color.dimGray, align: .right)
        }
        let current = snapshot?.cpu.availablePercent.map(SystemTelemetryDetailFormatter.percent) ?? "N/A"
        addLabel(current, to: card, frame: NSRect(x: frame.width - 48, y: 9, width: 38, height: 12), size: 8, weight: .medium, color: Tokens.Color.lightGray, align: .right)
        return card
    }

    private func diskHistoryCard(frame: NSRect) -> NSView {
        let card = historyCard(title: "DISK I/O", frame: frame)
        let hasRates = history.contains { $0.diskReadBps?.isFinite == true || $0.diskWriteBps?.isFinite == true }
        if hasRates {
            card.addSubview(TelemetryIOHistoryView(frame: NSRect(x: 10, y: 28, width: frame.width - 20, height: 50), samples: history))
        } else {
            addLabel("I/O history unavailable", to: card, frame: NSRect(x: 10, y: 49, width: frame.width - 20, height: 13), size: 8, color: Tokens.Color.dimGray, align: .center)
        }
        var legends: [String] = []
        if let read = snapshot?.disk.readBps, read.isFinite { legends.append("R " + SystemTelemetryDetailFormatter.rate(read)) }
        if let write = snapshot?.disk.writeBps, write.isFinite { legends.append("W " + SystemTelemetryDetailFormatter.rate(write)) }
        if !legends.isEmpty {
            addLabel(legends.joined(separator: "  "), to: card, frame: NSRect(x: 10, y: 82, width: frame.width - 20, height: 11), size: 6.8, color: Tokens.Color.lightGray, align: .center)
        }
        return card
    }

    private func historyCard(title: String, frame: NSRect) -> InlineTelemetryCard {
        let card = InlineTelemetryCard(frame: frame)
        card.wantsLayer = true
        card.layer?.backgroundColor = NSColor.clear.cgColor
        let separator = NSView(frame: NSRect(x: 0, y: 0, width: frame.width, height: 1))
        separator.wantsLayer = true
        separator.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.09).cgColor
        card.addSubview(separator)
        addLabel(title, to: card, frame: NSRect(x: 0, y: 7, width: 90, height: 12), size: 7.5, weight: .semibold, color: Tokens.Color.sectionGray)
        return card
    }

    private func cpuDetails() -> [String] {
        guard let cpu = snapshot?.cpu else { return [] }
        var rows: [String] = []
        let loads = [("P", cpu.pCoreUsagePercent), ("E", cpu.eCoreUsagePercent)].compactMap { label, value in
            value?.isFinite == true ? "\(label) \(SystemTelemetryDetailFormatter.percent(value))" : nil
        }
        if !loads.isEmpty { rows.append(loads.joined(separator: " · ")) }
        var physical: [String] = []
        if let p = cpu.pCoreCount { physical.append("\(p)P") }
        if let e = cpu.eCoreCount { physical.append("\(e)E") }
        if let temp = cpu.temperatureC, temp.isFinite { physical.append(String(format: "%.0f°", temp)) }
        if !physical.isEmpty { rows.append(physical.joined(separator: " · ")) }
        return rows
    }

    private func gpuDetails() -> [String] {
        guard let gpu = snapshot?.gpu else { return [] }
        var rows: [String] = []
        let engines = [("R", gpu.rendererPercent), ("T", gpu.tilerPercent)].compactMap { label, value in
            value?.isFinite == true ? "\(label) \(SystemTelemetryDetailFormatter.percent(value))" : nil
        }
        if !engines.isEmpty { rows.append(engines.joined(separator: " · ")) }
        var physical: [String] = []
        if let power = gpu.powerW, power.isFinite { physical.append(String(format: "%.1f W", power)) }
        if let temp = gpu.temperatureC, temp.isFinite { physical.append(String(format: "%.0f°", temp)) }
        if !physical.isEmpty { rows.append(physical.joined(separator: " · ")) }
        return rows
    }

    private func memoryDetails() -> [String] {
        guard let memory = snapshot?.memory else { return [] }
        var rows: [String] = []
        if let free = memory.freeBytes { rows.append("Free " + SystemTelemetryDetailFormatter.bytes(free)) }
        if let swap = memory.swapUsedBytes { rows.append("Swap " + SystemTelemetryDetailFormatter.bytes(swap)) }
        return rows
    }

    private func diskDetails() -> [String] {
        guard let disk = snapshot?.disk else { return [] }
        let capacity = SystemTelemetryDiskCapacity.presentation(for: disk)
        var rows: [String] = []
        if let free = capacity?.freeBytes { rows.append("Free " + SystemTelemetryDetailFormatter.bytes(free)) }
        if let total = capacity?.totalBytes { rows.append("of " + SystemTelemetryDetailFormatter.bytes(total)) }
        return rows
    }

    @discardableResult
    private func addLabel(
        _ text: String,
        to parent: NSView,
        frame: NSRect,
        size: CGFloat,
        weight: NSFont.Weight = .regular,
        color: NSColor,
        align: NSTextAlignment = .left
    ) -> NSTextField {
        let label = UI.label(text, size: size, weight: weight, color: color, align: align)
        label.frame = frame
        parent.addSubview(label)
        return label
    }

    required init?(coder: NSCoder) { nil }
}

private final class InlineTelemetryModule: NSView {
    override var isFlipped: Bool { true }
}

private final class InlineTelemetryCard: NSView {
    override var isFlipped: Bool { true }
}

private final class TelemetryCoreLoadView: NSView {
    override var isFlipped: Bool { true }

    init(frame: NSRect, pCore: Double?, eCore: Double?) {
        super.init(frame: frame)
        addRow(label: "P", value: pCore, y: 0, tint: .systemBlue)
        addRow(label: "E", value: eCore, y: 13, tint: .systemTeal)
    }

    private func addRow(label: String, value: Double?, y: CGFloat, tint: NSColor) {
        let name = UI.label(label, size: 6.5, weight: .medium, color: Tokens.Color.sectionGray)
        name.frame = NSRect(x: 0, y: y, width: 8, height: 9)
        addSubview(name)
        let track = NSView(frame: NSRect(x: 10, y: y + 2, width: bounds.width - 10, height: 4))
        track.wantsLayer = true
        track.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.08).cgColor
        track.layer?.cornerRadius = 2
        addSubview(track)
        guard let value, value.isFinite else { return }
        let fill = NSView(frame: NSRect(x: 0, y: 0, width: track.bounds.width * CGFloat(min(100, max(0, value))) / 100, height: 4))
        fill.wantsLayer = true
        fill.layer?.backgroundColor = tint.withAlphaComponent(0.82).cgColor
        fill.layer?.cornerRadius = 2
        track.addSubview(fill)
    }

    required init?(coder: NSCoder) { nil }
}

private final class TelemetryRingView: NSView {
    private let percent: Double?
    private let tint: NSColor

    init(frame: NSRect, percent: Double?, tint: NSColor) {
        self.percent = percent
        self.tint = tint
        super.init(frame: frame)
        wantsLayer = true
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let lineWidth: CGFloat = 4.5
        let rect = bounds.insetBy(dx: lineWidth, dy: lineWidth)
        let track = NSBezierPath(ovalIn: rect)
        track.lineWidth = lineWidth
        NSColor.white.withAlphaComponent(0.09).setStroke()
        track.stroke()
        guard let percent, percent.isFinite else { return }
        let center = NSPoint(x: bounds.midX, y: bounds.midY)
        let radius = min(rect.width, rect.height) / 2
        let progress = NSBezierPath()
        progress.appendArc(withCenter: center, radius: radius, startAngle: 90, endAngle: 90 - CGFloat(min(100, max(0, percent))) * 3.6, clockwise: true)
        progress.lineWidth = lineWidth
        progress.lineCapStyle = .round
        tint.setStroke()
        progress.stroke()
    }

    required init?(coder: NSCoder) { nil }
}

private final class TelemetryBarHistoryView: NSView {
    private let values: [Double]
    private let tint: NSColor

    init(frame: NSRect, values: [Double], tint: NSColor) {
        self.values = Array(values.suffix(SystemTelemetryHistoryBuffer.capacity))
        self.tint = tint
        super.init(frame: frame)
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let baseline = NSBezierPath()
        baseline.move(to: NSPoint(x: 0, y: 0.5))
        baseline.line(to: NSPoint(x: bounds.width, y: 0.5))
        NSColor.white.withAlphaComponent(0.08).setStroke()
        baseline.stroke()
        guard !values.isEmpty else { return }
        let slot = bounds.width / CGFloat(max(values.count, 12))
        let width = max(2, slot - 2)
        for (index, raw) in values.enumerated() {
            let fraction = CGFloat(min(100, max(0, raw))) / 100
            let height = max(2, bounds.height * fraction)
            let rect = NSRect(x: CGFloat(index) * slot + 1, y: 1, width: width, height: height - 1)
            let path = NSBezierPath(roundedRect: rect, xRadius: 1.5, yRadius: 1.5)
            tint.withAlphaComponent(0.32 + 0.62 * fraction).setFill()
            path.fill()
        }
    }

    required init?(coder: NSCoder) { nil }
}

private final class TelemetryIOHistoryView: NSView {
    private let samples: [SystemTelemetryHistorySample]

    init(frame: NSRect, samples: [SystemTelemetryHistorySample]) {
        self.samples = Array(samples.suffix(SystemTelemetryHistoryBuffer.capacity))
        super.init(frame: frame)
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let maximum = samples.flatMap { [$0.diskReadBps ?? 0, $0.diskWriteBps ?? 0] }.filter(\.isFinite).max() ?? 0
        guard maximum > 0 else { return }
        drawLine(values: samples.map(\.diskReadBps), maximum: maximum, color: .systemCyan)
        drawLine(values: samples.map(\.diskWriteBps), maximum: maximum, color: .systemPurple)
    }

    private func drawLine(values: [Double?], maximum: Double, color: NSColor) {
        guard values.count > 1 else { return }
        let path = NSBezierPath()
        var started = false
        for (index, value) in values.enumerated() {
            guard let value, value.isFinite else { continue }
            let x = CGFloat(index) / CGFloat(max(1, values.count - 1)) * bounds.width
            let y = CGFloat(max(0, value) / maximum) * max(1, bounds.height - 2) + 1
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

final class TransientSystemTelemetryPopover {
    private let popover = NSPopover()
    private weak var detailView: SystemTelemetryDetailView?

    init() {
        popover.behavior = .transient
        popover.animates = false
        popover.appearance = NSAppearance(named: .vibrantDark)
    }

    var isShown: Bool { popover.isShown }

    func toggle(
        relativeTo button: NSStatusBarButton,
        snapshot: SystemTelemetrySnapshot?,
        config: Config
    ) {
        if popover.isShown {
            popover.close()
            return
        }
        let preferred = SystemTelemetryDetailView.preferredSize
        let detail = SystemTelemetryDetailView(
            snapshot: snapshot,
            config: config,
            showsCloseControl: false,
            onClose: { [weak self] in self?.popover.close() }
        )
        let controller = NSViewController()
        controller.view = detail
        popover.contentViewController = controller
        popover.contentSize = preferred
        detailView = detail
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
    }

    func update(snapshot: SystemTelemetrySnapshot?) {
        detailView?.update(snapshot: snapshot)
    }

    func close() {
        popover.close()
    }
}
private final class FlippedTelemetryCard: NSView {
    override var isFlipped: Bool { true }
}
