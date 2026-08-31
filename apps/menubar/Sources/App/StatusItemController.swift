import AppKit


final class StatusItemController {

    var onClick: ((NSStatusBarButton) -> Void)?
    var onRefresh: (() -> Void)?
    var onOpenSettings: (() -> Void)?
    var onStatusModeChange: ((UsageStatusMode) -> Void)?
    var onUsageMetricModeChange: ((UsageMetricMode) -> Void)?
    var onSystemTelemetryVisibilityChange: ((Bool) -> Void)?
    var onBatteryStatusItemVisibilityChange: ((Bool) -> Void)?
    var onSystemTelemetryMetricChange: ((String, Bool) -> Void)?
    var onSystemTelemetryProfileChange: ((String) -> Void)?
    var systemTelemetryProfile = "balanced"
    var systemTelemetryCompactSpacing = true { didSet { requestRender() } }
    var statusMode: String = UsageStatusMode.bars.rawValue { didSet { requestRender() } }
    var usageMetricMode: String = UsageMetricMode.auto.rawValue { didSet { requestRender() } }
    var usageSessionThreshold: Double = 50 { didSet { requestRender() } }
    var usageBarsShowUsed: Bool = false { didSet { requestRender() } }
    var usageBarPalette: String = UsageBarPalette.colored.rawValue { didSet { requestRender() } }
    var usageWarningMarkersVisible: Bool = true { didSet { requestRender() } }
    var usageWarningThreshold: Double = 20 { didSet { requestRender() } }
    var showSystemTelemetry = false { didSet { requestRender() } }
    var batteryStatusItemVisible = false
    var systemTelemetryShowCPU = true { didSet { requestRender() } }
    var systemTelemetryShowGPU = true { didSet { requestRender() } }
    var systemTelemetryShowRAM = true { didSet { requestRender() } }
    var systemTelemetryShowDisk = false { didSet { requestRender() } }
    var systemTelemetryWarningThreshold = SystemTelemetryDisplayPolicy.defaultWarningThreshold { didSet { requestRender() } }
    var systemTelemetryCriticalThreshold = SystemTelemetryDisplayPolicy.defaultCriticalThreshold { didSet { requestRender() } }
    var button: NSStatusBarButton? { item.button }

    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private var lastState: MenubarState?
    private var usageState = UsageDashboardState()
    private var systemTelemetry: SystemTelemetrySnapshot?
    private var isBatchingPreferences = false

    init() {
        item.autosaveName = "org.coordharness.menubar.primary"
        item.isVisible = true
        if let b = item.button {
            b.imagePosition = .imageOnly
            b.image = RingRenderer.statusImage(
                usage: .make(from: usageState),
                mode: .bars,
                palette: .colored
            )
            b.target = self; b.action = #selector(clicked)
            b.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
    }

    @objc private func clicked() {
        let event = NSApp.currentEvent
        if event?.type == .rightMouseUp {
            showMenu()
            return
        }
        guard let button = item.button else { return }
        onClick?(button)
    }

    func update(with state: MenubarState) {
        lastState = state
        render()
    }

    func updateSystemTelemetry(_ snapshot: SystemTelemetrySnapshot?) {
        systemTelemetry = snapshot
        render()
    }

    func updateUsage(_ state: UsageDashboardState) {
        usageState = state
        render()
    }

    func applyPreferences(_ config: Config) {
        isBatchingPreferences = true
        statusMode = config.statusItemMode
        usageMetricMode = config.usageMetricMode
        usageSessionThreshold = config.usageSessionThreshold
        usageBarsShowUsed = config.usageBarsShowUsed
        usageBarPalette = config.usageBarPalette
        usageWarningMarkersVisible = config.usageWarningMarkersVisible
        usageWarningThreshold = config.usageWarningThreshold
        showSystemTelemetry = config.systemTelemetryEnabled && config.systemTelemetryInStatusItem
        batteryStatusItemVisible = config.batteryStatusItemEnabled
        systemTelemetryShowCPU = config.systemTelemetryShowCPU
        systemTelemetryShowGPU = config.systemTelemetryShowGPU
        systemTelemetryShowRAM = config.systemTelemetryShowRAM
        systemTelemetryShowDisk = config.systemTelemetryShowDisk
        systemTelemetryWarningThreshold = config.systemTelemetryWarningThreshold
        systemTelemetryCriticalThreshold = config.systemTelemetryCriticalThreshold
        systemTelemetryProfile = config.systemTelemetryProfile
        systemTelemetryCompactSpacing = config.systemTelemetryCompactSpacing
        isBatchingPreferences = false
        render()
    }

    private func requestRender() {
        guard !isBatchingPreferences else { return }
        render()
    }

    private func render() {
        guard let button = item.button else { return }
        let usage = UsageStatusPresentation.make(
            from: usageState,
            metricMode: usageMetricMode,
            sessionThreshold: usageSessionThreshold,
            showUsed: usageBarsShowUsed,
            warningMarkersVisible: usageWarningMarkersVisible,
            warningThreshold: usageWarningThreshold
        )
        // Stats own an independently dockable status item. Keep the primary item
        // quota-only so enabling Stats never paints the same metrics twice.
        let telemetry: [RingRenderer.TelemetryPresentation] = []
        button.imagePosition = .imageOnly
        button.title = ""
        button.attributedTitle = NSAttributedString()
        var resolvedImage: NSImage?
        button.effectiveAppearance.performAsCurrentDrawingAppearance {
            resolvedImage = RingRenderer.statusImage(
                usage: usage,
                mode: UsageStatusMode.resolve(statusMode),
                telemetry: telemetry,
                compactTelemetrySpacing: systemTelemetryCompactSpacing,
                palette: UsageBarPalette.resolve(usageBarPalette)
            )
        }
        button.image = resolvedImage
        item.length = resolvedImage?.size.width ?? NSStatusItem.variableLength
        let workWarning = lastState?.hasProjectionWarning == true ? "Work projection stale. " : ""
        let accessibility = workWarning + usage.accessibilityLabel
        button.toolTip = accessibility
        button.setAccessibilityLabel(accessibility)
        button.setAccessibilityHelp("Open COORD tasks and usage.")
    }

    private func systemTelemetryPresentations() -> [RingRenderer.TelemetryPresentation] {
        let snapshot = systemTelemetry
        let stale = snapshot?.isStale != false
        let diskPercent = snapshot.flatMap { SystemTelemetryDiskCapacity.presentation(for: $0.disk)?.usedPercent }
            ?? snapshot?.disk.availablePercent
        let values: [(String, Double?)] = [
            ("GPU", systemTelemetryShowGPU ? snapshot?.gpu.availablePercent : nil),
            ("RAM", systemTelemetryShowRAM ? snapshot?.memory.availablePercent : nil),
            ("CPU", systemTelemetryShowCPU ? snapshot?.cpu.availablePercent : nil),
            ("DSK", systemTelemetryShowDisk ? diskPercent : nil),
        ]
        let policy = SystemTelemetryDisplayPolicy(
            warningThreshold: systemTelemetryWarningThreshold,
            criticalThreshold: systemTelemetryCriticalThreshold
        )
        return values.compactMap { label, percent in
            guard metricEnabled(label) else { return nil }
            let value = stale ? "N/A" : percent.map { "\(Int($0.rounded()))" } ?? "N/A"
            return RingRenderer.TelemetryPresentation(
                label: label,
                value: value,
                severity: stale ? .unavailable : policy.severity(for: percent)
            )
        }
    }

    private func metricEnabled(_ label: String) -> Bool {
        switch label {
        case "CPU": systemTelemetryShowCPU
        case "GPU": systemTelemetryShowGPU
        case "RAM": systemTelemetryShowRAM
        case "DSK": systemTelemetryShowDisk
        default: false
        }
    }

    private func systemTelemetryAccessibilityLabel() -> String {
        guard let snapshot = systemTelemetry, !snapshot.isStale else { return "System stats unavailable." }
        var values: [String] = []
        if systemTelemetryShowCPU { values.append("CPU " + telemetryPercent(snapshot.cpu.availablePercent)) }
        if systemTelemetryShowGPU { values.append("GPU " + telemetryPercent(snapshot.gpu.availablePercent)) }
        if systemTelemetryShowRAM { values.append("RAM " + telemetryPercent(snapshot.memory.availablePercent)) }
        if systemTelemetryShowDisk {
            let diskPercent = SystemTelemetryDiskCapacity.presentation(for: snapshot.disk)?.usedPercent ?? snapshot.disk.availablePercent
            values.append("disk " + telemetryPercent(diskPercent))
        }
        return "System stats: " + values.joined(separator: ", ") + "."
    }

    private func telemetryPercent(_ value: Double?) -> String {
        value.map { "\(Int($0.rounded()))%" } ?? "N/A"
    }

    private func showMenu() {
        guard let b = item.button else { return }
        let menu = NSMenu()
        let liveIds = (lastState?.workModel?.runningRows ?? []).filter { $0.live == true }.compactMap { $0.jobId ?? $0.id }
        let pausedIds = (lastState?.workModel?.runningRows ?? []).filter { $0.paused == true }.compactMap { $0.jobId ?? $0.id }
        menu.addItem(makeItem("Pause all running (\(liveIds.count))", #selector(menuPauseAll), enabled: !liveIds.isEmpty))
        menu.addItem(makeItem("Resume paused (\(pausedIds.count))", #selector(menuResumeAll), enabled: !pausedIds.isEmpty))
        menu.addItem(makeItem("Refresh", #selector(menuRefresh)))
        menu.addItem(.separator())

        let modeItem = NSMenuItem(title: "Mode", action: nil, keyEquivalent: "")
        let modeMenu = NSMenu()
        let pauseMode = makeItem("Pause", #selector(menuPauseAll), enabled: !liveIds.isEmpty)
        pauseMode.identifier = NSUserInterfaceItemIdentifier("coord.menu.mode.pause")
        pauseMode.representedObject = "pause"
        pauseMode.state = lastState?.displayMode == "pause" ? .on : .off
        modeMenu.addItem(pauseMode)
        for (label, key) in [("Medium", "medium"), ("Full", "full")] {
            let mi = makeItem(label, #selector(menuMode(_:)))
            mi.identifier = NSUserInterfaceItemIdentifier("coord.menu.mode." + key)
            mi.representedObject = key
            if lastState?.displayMode == key { mi.state = .on }
            modeMenu.addItem(mi)
        }
        modeItem.submenu = modeMenu
        menu.addItem(modeItem)
        let statusItem = NSMenuItem(title: "Status display", action: nil, keyEquivalent: "")
        let statusMenu = NSMenu()
        for (label, mode) in [("Bars", UsageStatusMode.bars), ("Rings", .rings), ("Minimal", .minimal)] {
            let option = makeItem(label, #selector(menuStatusMode(_:)))
            option.representedObject = mode.rawValue
            option.state = UsageStatusMode.resolve(statusMode) == mode ? .on : .off
            statusMenu.addItem(option)
        }
        statusItem.submenu = statusMenu
        menu.addItem(statusItem)
        let quotaItem = NSMenuItem(title: "Quota window", action: nil, keyEquivalent: "")
        let quotaMenu = NSMenu()
        for mode in UsageMetricMode.allCases {
            let label = mode == .auto ? "Auto (weekly → session)" : mode.rawValue.capitalized
            let option = makeItem(label, #selector(menuUsageMetricMode(_:)))
            option.representedObject = mode.rawValue
            option.state = UsageMetricMode.resolve(usageMetricMode) == mode ? .on : .off
            quotaMenu.addItem(option)
        }
        quotaItem.submenu = quotaMenu
        menu.addItem(quotaItem)

        let batteryItem = NSMenuItem(title: "Battery", action: nil, keyEquivalent: "")
        let batteryMenu = NSMenu()
        let batteryVisible = makeItem("Show separate menu-bar item", #selector(menuBatteryStatusItemVisibility(_:)))
        batteryVisible.state = batteryStatusItemVisible ? .on : .off
        batteryMenu.addItem(batteryVisible)
        batteryItem.submenu = batteryMenu
        menu.addItem(batteryItem)
        let statsItem = NSMenuItem(title: "System stats", action: nil, keyEquivalent: "")
        let statsMenu = NSMenu()
        let visible = makeItem("Show separate menu-bar item", #selector(menuSystemTelemetryVisibility(_:)))
        visible.state = showSystemTelemetry ? .on : .off
        statsMenu.addItem(visible)
        statsMenu.addItem(.separator())
        for (label, key, selected) in [("CPU", "cpu", systemTelemetryShowCPU), ("GPU", "gpu", systemTelemetryShowGPU), ("RAM", "ram", systemTelemetryShowRAM), ("Disk", "disk", systemTelemetryShowDisk)] {
            let option = makeItem(label, #selector(menuSystemTelemetryMetric(_:)))
            option.representedObject = key; option.state = selected ? .on : .off
            statsMenu.addItem(option)
        }
        statsMenu.addItem(.separator())
        for profile in ["eco", "balanced", "live"] {
            let option = makeItem(profile.capitalized, #selector(menuSystemTelemetryProfile(_:)))
            option.representedObject = profile; option.state = systemTelemetryProfile == profile ? .on : .off
            statsMenu.addItem(option)
        }
        statsItem.submenu = statsMenu
        menu.addItem(statsItem)
        menu.addItem(.separator())
        menu.addItem(makeItem("Settings…", #selector(menuOpenSettings)))
        menu.addItem(makeItem("Open Full Cockpit Window…", #selector(menuOpenCockpit)))
        menu.addItem(.separator())
        menu.addItem(makeItem("Quit COORD", #selector(menuQuit)))

        menu.popUp(positioning: nil, at: NSPoint(x: 0, y: b.bounds.height + 4), in: b)
    }

    private func makeItem(_ title: String, _ action: Selector, enabled: Bool = true) -> NSMenuItem {
        let mi = NSMenuItem(title: title, action: action, keyEquivalent: "")
        mi.target = self; mi.isEnabled = enabled
        return mi
    }

    @objc private func menuPauseAll() {
        let ids = (lastState?.workModel?.runningRows ?? []).filter { $0.live == true }.compactMap { $0.jobId ?? $0.id }
        afterMenuDismissal { [weak self] in
            HarnessControl.pauseAll(jobIds: ids)
            self?.onRefresh?()
        }
    }
    @objc private func menuResumeAll() {
        let ids = (lastState?.workModel?.runningRows ?? []).filter { $0.paused == true }.compactMap { $0.jobId ?? $0.id }
        afterMenuDismissal { [weak self] in
            HarnessControl.resumeAll(jobIds: ids)
            self?.onRefresh?()
        }
    }
    @objc private func menuRefresh() { afterMenuDismissal { [weak self] in self?.onRefresh?() } }
    @objc private func menuBatteryStatusItemVisibility(_ sender: NSMenuItem) {
        onBatteryStatusItemVisibilityChange?(!batteryStatusItemVisible)
    }
    @objc private func menuSystemTelemetryVisibility(_ sender: NSMenuItem) {
        onSystemTelemetryVisibilityChange?(!showSystemTelemetry)
    }
    @objc private func menuSystemTelemetryMetric(_ sender: NSMenuItem) {
        guard let key = sender.representedObject as? String else { return }
        let selected: Bool
        switch key {
        case "cpu": selected = systemTelemetryShowCPU
        case "gpu": selected = systemTelemetryShowGPU
        case "ram": selected = systemTelemetryShowRAM
        case "disk": selected = systemTelemetryShowDisk
        default: return
        }
        onSystemTelemetryMetricChange?(key, !selected)
    }
    @objc private func menuSystemTelemetryProfile(_ sender: NSMenuItem) {
        guard let profile = sender.representedObject as? String else { return }
        onSystemTelemetryProfileChange?(profile)
    }
    @objc private func menuMode(_ sender: NSMenuItem) {
        guard let key = sender.representedObject as? String else { return }
        afterMenuDismissal { [weak self] in
            HarnessControl.setMode(key)
            self?.onRefresh?()
        }
    }
    @objc private func menuStatusMode(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String else { return }
        let mode = UsageStatusMode.resolve(raw)
        onStatusModeChange?(mode)
    }
    @objc private func menuUsageMetricMode(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String else { return }
        let mode = UsageMetricMode.resolve(raw)
        usageMetricMode = mode.rawValue
        onUsageMetricModeChange?(mode)
    }
    @objc private func menuOpenSettings() {
        afterMenuDismissal { [weak self] in self?.onOpenSettings?() }
    }
    @objc private func menuOpenCockpit() {
        afterMenuDismissal {
            Task { @MainActor in
                (NSApp.delegate as? AppDelegate)?.openCockpitWindow()
            }
        }
    }
    @objc private func menuQuit() {
        afterMenuDismissal { NSApp.terminate(nil) }
    }

    private func afterMenuDismissal(_ action: @escaping () -> Void) {
        DispatchQueue.main.async(execute: action)
    }
}
