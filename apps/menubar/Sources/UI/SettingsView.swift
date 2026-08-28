import AppKit


final class SettingsView: RowView, NSTextFieldDelegate {

    var onSave: ((Config) -> Void)?
    var onChange: ((Config) -> Void)?
    var onClose: (() -> Void)?
    var onQuit: (() -> Void)?
    var onOpenProviderAccounts: (() -> Void)?

    private(set) var contentHeight: CGFloat = 0
    private var cfg: Config
    private var isCompletingSave = false
    private var fields: [String: NSTextField] = [:]
    private var stayOpen: NSButton!
    private var notifications: NSButton!
    private var showVitals: NSButton!
    private var telemetryEnabled: NSButton!
    private var telemetryPopover: NSButton!
    private var telemetryStatusItem: NSButton!
    private var telemetryCockpit: NSButton!
    private var telemetryCPU: NSButton!
    private var telemetryGPU: NSButton!
    private var telemetryRAM: NSButton!
    private var telemetryDisk: NSButton!
    private var telemetryProfilePopup: NSPopUpButton!
    private var telemetrySpacingPopup: NSPopUpButton!
    private var slowRing: NSButton!
    private var showInlineUsage: NSButton!
    private var launchLogin: NSButton!
    private var taskActions: NSButton!
    private var panelDetached: NSButton!
    private var panelAlwaysOnTop: NSButton!
    private var transportPopup: NSPopUpButton!
    private var statusItemPopup: NSPopUpButton!
    private var usageMetricPopup: NSPopUpButton!
    private var usageFillPopup: NSPopUpButton!
    private var usagePalettePopup: NSPopUpButton!
    private var showResetETA: NSButton!
    private var showRunoutETA: NSButton!
    private var warningMarkers: NSButton!
    private var showAttention: NSButton!
    private var showFollowup: NSButton!
    private var showLocalQueue: NSButton!
    private let statusChoices: [(title: String, mode: UsageStatusMode)] = [
        ("Quota bars + progress", .bars),
        ("Quota rings", .rings),
        ("Minimal status", .minimal),
    ]
    private let transportChoices: [(title: String, transport: SnapshotTransportKind)] = [
        ("Local database", .db),
        ("Legacy file compatibility", .filewatch),
        ("Local HTTP compatibility", .http),
    ]

    init(config: Config) {
        self.cfg = config
        super.init(frame: NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: 10))
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor
        var y: CGFloat = 12
        let L: CGFloat = 20, labelW: CGFloat = 150, fieldW: CGFloat = 150


        let title = UI.label("Settings", size: 13, weight: .bold, color: .white)
        title.frame = NSRect(x: L, y: y, width: 120, height: 18); addSubview(title)
        let close = NSButton(frame: NSRect(x: bounds.width - 28, y: y - 2, width: 20, height: 20))
        close.title = "✕"; close.isBordered = false; close.font = .systemFont(ofSize: 12)
        close.contentTintColor = Tokens.Color.dimGray; close.target = self; close.action = #selector(doClose); addSubview(close)
        y += 30

        func section(_ name: String) {
            let l = UI.label(name.uppercased(), size: 9.5, weight: .semibold, color: Tokens.Color.sectionGray)
            l.frame = NSRect(x: L, y: y, width: bounds.width - 2*L, height: 14)
            addSubview(l)
            y += 20
        }
        func field(_ label: String, _ key: String, _ value: String) {
            let l = UI.label(label, size: 11, color: Tokens.Color.lightGray)
            l.frame = NSRect(x: L, y: y + 2, width: labelW, height: 16)
            addSubview(l)
            let f = NSTextField(string: value)
            f.frame = NSRect(x: L + labelW, y: y, width: fieldW, height: 20)
            f.font = .systemFont(ofSize: 11)
            f.drawsBackground = false
            f.delegate = self
            f.target = self
            f.action = #selector(controlChanged)
            fields[key] = f
            addSubview(f)
            y += 24
        }
        func check(_ label: String, _ on: Bool) -> NSButton {
            let b = NSButton(checkboxWithTitle: label, target: self, action: #selector(controlChanged))
            b.frame = NSRect(x: L, y: y, width: bounds.width - 2*L, height: 18)
            b.state = on ? .on : .off
            (b.cell as? NSButtonCell)?.font = .systemFont(ofSize: 11)
            addSubview(b)
            y += 22
            return b
        }

        section("Provider services")
        let accounts = NSButton(frame: NSRect(x: L, y: y, width: bounds.width - 2*L, height: 26))
        accounts.title = "Accounts · Services · Routing…"
        accounts.bezelStyle = .rounded
        accounts.image = NSImage(systemSymbolName: "person.crop.circle.badge.checkmark", accessibilityDescription: nil)
        accounts.imagePosition = .imageLeading
        accounts.target = self
        accounts.action = #selector(doOpenProviderAccounts)
        accounts.setAccessibilityLabel("Provider accounts, services, and intelligent routing")
        accounts.setAccessibilityHelp("Opens sign-in, multi-account, service, Keychain, and routing controls.")
        addSubview(accounts)
        y += 34

        section("System stats · Menu bar")
        telemetryEnabled = check("System stats enabled", cfg.systemTelemetryEnabled)
        telemetryStatusItem = check("Always show stats beside Usage in the menu bar", cfg.systemTelemetryInStatusItem)
        telemetryCockpit = check("Show stats in the cockpit", cfg.systemTelemetryInCockpit)
        telemetryCPU = check("CPU", cfg.systemTelemetryShowCPU)
        telemetryGPU = check("GPU", cfg.systemTelemetryShowGPU)
        telemetryRAM = check("RAM", cfg.systemTelemetryShowRAM)
        telemetryDisk = check("Disk", cfg.systemTelemetryShowDisk)
        field("Warning threshold (%)", "telemetry_warning", String(Int(cfg.systemTelemetryWarningThreshold)))
        field("Critical threshold (%)", "telemetry_critical", String(Int(cfg.systemTelemetryCriticalThreshold)))
        let telemetryProfileLabel = UI.label("Sampling profile", size: 11, color: Tokens.Color.lightGray)
        telemetryProfileLabel.frame = NSRect(x: L, y: y + 2, width: labelW, height: 16); addSubview(telemetryProfileLabel)
        telemetryProfilePopup = NSPopUpButton(frame: NSRect(x: L + labelW, y: y - 2, width: fieldW, height: 24))
        telemetryProfilePopup.addItems(withTitles: ["Eco", "Balanced", "Live"])
        telemetryProfilePopup.selectItem(withTitle: cfg.systemTelemetryProfile.capitalized)
        telemetryProfilePopup.toolTip = "Eco minimizes wakeups; Live refreshes only when an enabled surface is visible."
        addSubview(telemetryProfilePopup); y += 28
        let telemetrySpacingLabel = UI.label("Menu-bar stats spacing", size: 11, color: Tokens.Color.lightGray)
        telemetrySpacingLabel.frame = NSRect(x: L, y: y + 2, width: labelW, height: 16); addSubview(telemetrySpacingLabel)
        telemetrySpacingPopup = NSPopUpButton(frame: NSRect(x: L + labelW, y: y - 2, width: fieldW, height: 24))
        telemetrySpacingPopup.addItems(withTitles: ["Compact", "Comfortable"])
        telemetrySpacingPopup.selectItem(at: cfg.systemTelemetryCompactSpacing ? 0 : 1)
        telemetrySpacingPopup.toolTip = "Controls spacing between persistent menu-bar CPU, GPU, RAM, and Disk values"
        addSubview(telemetrySpacingPopup); y += 30

        section("Usage display")
        showInlineUsage = check("Show inline Usage details", !cfg.usagePeekCollapsed)
        showInlineUsage.setAccessibilityHelp("Shows or hides the compact provider Usage rows in the main panel.")
        let modeLabel = UI.label("Menu-bar usage", size: 11, color: Tokens.Color.lightGray)
        modeLabel.frame = NSRect(x: L, y: y + 2, width: labelW, height: 16); addSubview(modeLabel)
        statusItemPopup = NSPopUpButton(frame: NSRect(x: L + labelW, y: y - 2, width: fieldW, height: 24))
        statusItemPopup.addItems(withTitles: statusChoices.map(\.title))
        let currentMode = UsageStatusMode.resolve(cfg.statusItemMode)
        statusItemPopup.selectItem(at: statusChoices.firstIndex { $0.mode == currentMode } ?? 0)
        addSubview(statusItemPopup); y += 28
        let metricLabel = UI.label("Quota window", size: 11, color: Tokens.Color.lightGray)
        metricLabel.frame = NSRect(x: L, y: y + 2, width: labelW, height: 16); addSubview(metricLabel)
        usageMetricPopup = NSPopUpButton(frame: NSRect(x: L + labelW, y: y - 2, width: fieldW, height: 24))
        usageMetricPopup.addItems(withTitles: ["Auto", "Weekly", "Session"])
        usageMetricPopup.selectItem(at: UsageMetricMode.allCases.firstIndex(of: UsageMetricMode.resolve(cfg.usageMetricMode)) ?? 0)
        usageMetricPopup.toolTip = "Auto shows Weekly unless an available Session quota is below the threshold"
        addSubview(usageMetricPopup); y += 28
        let fillLabel = UI.label("Bar meaning", size: 11, color: Tokens.Color.lightGray)
        fillLabel.frame = NSRect(x: L, y: y + 2, width: labelW, height: 16); addSubview(fillLabel)
        usageFillPopup = NSPopUpButton(frame: NSRect(x: L + labelW, y: y - 2, width: fieldW, height: 24))
        usageFillPopup.addItems(withTitles: ["Remaining", "Used"])
        usageFillPopup.selectItem(at: cfg.usageBarsShowUsed ? 1 : 0)
        usageFillPopup.toolTip = "Choose whether quota bars and percentages show capacity remaining or already used"
        addSubview(usageFillPopup); y += 28
        let paletteLabel = UI.label("Bar palette", size: 11, color: Tokens.Color.lightGray)
        paletteLabel.frame = NSRect(x: L, y: y + 2, width: labelW, height: 16); addSubview(paletteLabel)
        usagePalettePopup = NSPopUpButton(frame: NSRect(x: L + labelW, y: y - 2, width: fieldW, height: 24))
        usagePalettePopup.addItems(withTitles: ["Provider colors", "Neutral white / gray"])
        usagePalettePopup.selectItem(at: UsageBarPalette.resolve(cfg.usageBarPalette) == .colored ? 0 : 1)
        usagePalettePopup.toolTip = "Provider logos stay colored in either palette"
        addSubview(usagePalettePopup); y += 28
        field("Auto session threshold", "usage_threshold", String(Int(cfg.usageSessionThreshold)))
        field("History horizon (days)", "usage_history_days", String(cfg.usageHistoryDays))
        field("Low quota warning (%)", "usage_warning", String(Int(cfg.usageWarningThreshold)))
        showResetETA = check("Show reset ETA", cfg.usageShowResetETA)
        showRunoutETA = check("Show run-out ETA", cfg.usageShowRunoutETA)
        warningMarkers = check("Show low-quota marker on bars", cfg.usageWarningMarkersVisible)
        slowRing = check("Keep quota progress live while panel is closed", cfg.slowRingTick)
        field("Dark glass opacity", "glass_alpha", String(format: "%.2f", cfg.glassAlpha))

        section("Work display")
        field("Next jobs visible", "next_visible", String(cfg.nextVisible))
        field("Expand next count", "expand_count", String(cfg.expandCount))
        showVitals = check("Show work vitals in panel", cfg.showVitalsInPopover)
        taskActions = check("Show row controls", cfg.taskActionsEnabled)
        notifications = check("Notify on done, failed, or blocked work", cfg.notifications)
        showAttention = check("Show Needs Attention by default", !cfg.attentionCollapsed)
        showFollowup = check("Show Follow-up by default", !cfg.followupCollapsed)
        showLocalQueue = check("Show local queue by default", !cfg.localQueueCollapsed)
        stayOpen = check("Keep panel open on click-away", cfg.stayOpen)
        panelDetached = check("Pop out as resizable window", cfg.panelDetached)
        panelAlwaysOnTop = check("Keep pop-out window above other windows", cfg.panelAlwaysOnTop)

        section("Refresh & history")
        field("Panel refresh interval (s)", "refresh", String(Int(cfg.refreshSecs)))
        field("Closed-panel refresh (s)", "slow_interval", String(Int(cfg.slowRingInterval)))
        field("Request timeout (s)", "fetch_timeout", String(Int(cfg.fetchTimeoutSecs)))

        section("Data / Compatibility")
        let sourceLabel = UI.label("Work data source", size: 11, color: Tokens.Color.lightGray)
        sourceLabel.frame = NSRect(x: L, y: y + 2, width: labelW, height: 16); addSubview(sourceLabel)
        transportPopup = NSPopUpButton(frame: NSRect(x: L + labelW, y: y - 2, width: fieldW, height: 24))
        transportPopup.addItems(withTitles: transportChoices.map(\.title))
        let currentTransport = SnapshotTransportKind.resolve(cfg.transport)
        transportPopup.selectItem(at: transportChoices.firstIndex { $0.transport == currentTransport } ?? 0)
        addSubview(transportPopup); y += 28
        launchLogin = check("Launch COORD at login", cfg.launchAtLogin)
        field("Panel shortcut key", "hk_key", cfg.hotkey.key)
        field("Shortcut modifiers", "hk_mods", cfg.hotkey.mods.joined(separator: ","))
        let hint = UI.label("Example: comma with cmd, or cmd,shift", size: 9.5, color: Tokens.Color.dimGray)
        hint.frame = NSRect(x: L, y: y, width: bounds.width - 2*L, height: 14); addSubview(hint); y += 18

        let save = NSButton(frame: NSRect(x: L, y: y, width: bounds.width - 2*L, height: 26))
        for popup in [telemetryProfilePopup, telemetrySpacingPopup, statusItemPopup, usageMetricPopup, usageFillPopup, usagePalettePopup, transportPopup].compactMap({ $0 }) {
            popup.target = self
            popup.action = #selector(controlChanged)
        }

        save.title = "Done"; save.bezelStyle = .rounded; save.target = self; save.action = #selector(doSave); addSubview(save)
        y += 34


        let sep = NSView(frame: NSRect(x: L, y: y, width: bounds.width - 2*L, height: 1)); sep.wantsLayer = true
        sep.layer?.backgroundColor = Tokens.Color.gray(1, 0.12).cgColor; addSubview(sep); y += 10

        let quit = NSButton(frame: NSRect(x: L, y: y, width: bounds.width - 2*L, height: 24))
        quit.title = "Quit COORD"; quit.isBordered = false; quit.contentTintColor = Tokens.Color.red
        quit.font = .systemFont(ofSize: 11, weight: .medium); quit.target = self; quit.action = #selector(doQuit); addSubview(quit)
        y += 36

        contentHeight = y
        frame = NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: y)
    }

    @objc private func doClose() { window?.makeFirstResponder(nil); onClose?() }
    @objc private func doOpenProviderAccounts() { onOpenProviderAccounts?() }
    @objc private func doQuit() { onQuit?() }
    @objc private func doSave() {
        guard !isCompletingSave else { return }
        isCompletingSave = true
        defer { isCompletingSave = false }
        // Resigning the field synchronously delivers controlTextDidEndEditing.
        // Suppress that intermediate live-change path, then read its final value once.
        window?.makeFirstResponder(nil)
        commitChanges()
        onSave?(cfg)
    }
    @objc private func controlChanged() {
        guard !isCompletingSave else { return }
        commitChanges()
        onChange?(cfg)
    }
    func controlTextDidEndEditing(_ notification: Notification) { controlChanged() }
    private func commitChanges() {
        func s(_ k: String) -> String { fields[k]?.stringValue ?? "" }
        cfg.hotkey.key = s("hk_key").lowercased().isEmpty ? "comma" : s("hk_key").lowercased()
        cfg.hotkey.mods = s("hk_mods").split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        cfg.refreshSecs = max(1, Double(s("refresh")) ?? cfg.refreshSecs)
        cfg.nextVisible = max(1, Int(s("next_visible")) ?? cfg.nextVisible)
        cfg.expandCount = max(1, Int(s("expand_count")) ?? cfg.expandCount)
        cfg.glassAlpha = min(1, max(0, Double(s("glass_alpha")) ?? cfg.glassAlpha))
        cfg.slowRingInterval = max(3, Double(s("slow_interval")) ?? cfg.slowRingInterval)
        cfg.fetchTimeoutSecs = max(1, Double(s("fetch_timeout")) ?? cfg.fetchTimeoutSecs)
        cfg.stayOpen = stayOpen.state == .on
        cfg.notifications = notifications.state == .on
        cfg.showVitalsInPopover = showVitals.state == .on
        cfg.systemTelemetryEnabled = telemetryEnabled.state == .on
        cfg.systemTelemetryInStatusItem = telemetryStatusItem.state == .on
        cfg.systemTelemetryInCockpit = telemetryCockpit.state == .on
        cfg.systemTelemetryShowCPU = telemetryCPU.state == .on
        cfg.systemTelemetryShowGPU = telemetryGPU.state == .on
        cfg.systemTelemetryShowRAM = telemetryRAM.state == .on
        let systemThresholds = SystemTelemetryDisplayPolicy(
            warningThreshold: Double(s("telemetry_warning")) ?? cfg.systemTelemetryWarningThreshold,
            criticalThreshold: Double(s("telemetry_critical")) ?? cfg.systemTelemetryCriticalThreshold
        )
        cfg.systemTelemetryWarningThreshold = systemThresholds.warningThreshold
        cfg.systemTelemetryCriticalThreshold = systemThresholds.criticalThreshold

        cfg.systemTelemetryShowDisk = telemetryDisk.state == .on
        cfg.systemTelemetryProfile = (telemetryProfilePopup.titleOfSelectedItem ?? "Balanced").lowercased()
        cfg.systemTelemetryCompactSpacing = telemetrySpacingPopup.indexOfSelectedItem == 0
        cfg.slowRingTick = slowRing.state == .on
        cfg.launchAtLogin = launchLogin.state == .on
        cfg.taskActionsEnabled = taskActions.state == .on
        cfg.panelDetached = panelDetached.state == .on
        cfg.panelAlwaysOnTop = panelAlwaysOnTop.state == .on
        cfg.usagePeekCollapsed = showInlineUsage.state != .on
        cfg.attentionCollapsed = showAttention.state != .on
        cfg.followupCollapsed = showFollowup.state != .on
        cfg.localQueueCollapsed = showLocalQueue.state != .on
        cfg.transport = transportChoices[min(max(transportPopup.indexOfSelectedItem, 0), transportChoices.count - 1)].transport.rawValue
        cfg.statusItemMode = statusChoices[min(max(statusItemPopup.indexOfSelectedItem, 0), statusChoices.count - 1)].mode.rawValue
        cfg.usageMetricMode = UsageMetricMode.allCases[min(max(usageMetricPopup.indexOfSelectedItem, 0), UsageMetricMode.allCases.count - 1)].rawValue
        cfg.usageBarsShowUsed = usageFillPopup.indexOfSelectedItem == 1
        cfg.usageBarPalette = usagePalettePopup.indexOfSelectedItem == 0
            ? UsageBarPalette.colored.rawValue
            : UsageBarPalette.neutral.rawValue
        cfg.usageSessionThreshold = min(100, max(0, Double(s("usage_threshold")) ?? cfg.usageSessionThreshold))
        cfg.usageHistoryDays = min(365, max(7, Int(s("usage_history_days")) ?? cfg.usageHistoryDays))
        cfg.usageWarningThreshold = min(100, max(0, Double(s("usage_warning")) ?? cfg.usageWarningThreshold))
        cfg.usageShowResetETA = showResetETA.state == .on
        cfg.usageShowRunoutETA = showRunoutETA.state == .on
        cfg.usageWarningMarkersVisible = warningMarkers.state == .on
        cfg.save()
    }
    required init?(coder: NSCoder) { nil }
}
