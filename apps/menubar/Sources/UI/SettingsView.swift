import AppKit

/// Fixed-height settings shell: navigation and actions stay pinned,
/// while only an unusually long selected category can scroll.
final class SettingsView: RowView, NSTextFieldDelegate {
    enum Page: String, CaseIterable {
        case general = "General"
        case display = "Display"
        case usage = "Usage"
        case power = "Power"
        case advanced = "Advanced"
    }

    var onSave: ((Config) -> Void)?
    var onChange: ((Config) -> Void)?
    var onClose: (() -> Void)?
    var onQuit: (() -> Void)?
    var onOpenProviderAccounts: (() -> Void)?
    var onOpenPower: (() -> Void)?
    var onOpenCockpit: (() -> Void)?

    private(set) var contentHeight: CGFloat
    private var cfg: Config
    private var isCompletingSave = false
    private var fields: [String: NSTextField] = [:]
    private var pages: [Page: RowView] = [:]
    private var pageHeights: [Page: CGFloat] = [:]
    private var pageButtons: [Page: NSButton] = [:]
    private let pageScroll = NSScrollView()

    private var stayOpen = NSButton()
    private var notifications = NSButton()
    private var showVitals = NSButton()
    private var telemetryEnabled = NSButton()
    private var telemetryPopover = NSButton()
    private var telemetryStatusItem = NSButton()
    private var batteryStatusItem = NSButton()
    private var telemetryCockpit = NSButton()
    private var telemetryCPU = NSButton()
    private var telemetryGPU = NSButton()
    private var telemetryRAM = NSButton()
    private var telemetryDisk = NSButton()
    private var telemetryProfilePopup = NSPopUpButton()
    private var telemetrySpacingPopup = NSPopUpButton()
    private var slowRing = NSButton()
    private var showInlineUsage = NSButton()
    private var launchLogin = NSButton()
    private var taskActions = NSButton()
    private var panelDetached = NSButton()
    private var panelAlwaysOnTop = NSButton()
    private var transportPopup = NSPopUpButton()
    private var statusItemPopup = NSPopUpButton()
    private var usageMetricPopup = NSPopUpButton()
    private var usageFillPopup = NSPopUpButton()
    private var usagePalettePopup = NSPopUpButton()
    private var showResetETA = NSButton()
    private var showRunoutETA = NSButton()
    private var warningMarkers = NSButton()
    private var showAttention = NSButton()
    private var showFollowup = NSButton()
    private var showLocalQueue = NSButton()

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

    init(config: Config, height: CGFloat = 660) {
        cfg = config
        contentHeight = max(420, height)
        super.init(frame: NSRect(
            x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: contentHeight
        ))
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor

        let left: CGFloat = 16
        let width = Tokens.Layout.popoverWidth
        let title = UI.label("Settings", size: 17, weight: .bold, color: .white)
        title.frame = NSRect(x: left, y: 12, width: 180, height: 22)
        addSubview(title)
        let subtitle = UI.label(
            "Focused controls · changes apply instantly",
            size: 9.5,
            color: Tokens.Color.dimGray
        )
        subtitle.frame = NSRect(x: left, y: 34, width: 260, height: 14)
        addSubview(subtitle)

        let close = NSButton(frame: NSRect(x: width - 34, y: 12, width: 22, height: 22))
        close.title = "✕"
        close.isBordered = false
        close.font = .systemFont(ofSize: 12, weight: .medium)
        close.contentTintColor = Tokens.Color.dimGray
        close.target = self
        close.action = #selector(doClose)
        close.setAccessibilityLabel("Close Settings")
        addSubview(close)

        let navY: CGFloat = 56
        let buttonWidth = (width - 2 * left) / CGFloat(Page.allCases.count)
        for (index, page) in Page.allCases.enumerated() {
            let button = NSButton(frame: NSRect(
                x: left + CGFloat(index) * buttonWidth,
                y: navY,
                width: buttonWidth,
                height: 28
            ))
            button.title = page.rawValue
            button.isBordered = false
            button.font = .systemFont(ofSize: 10, weight: .semibold)
            button.tag = index
            button.target = self
            button.action = #selector(selectPage(_:))
            button.setAccessibilityLabel("\(page.rawValue) Settings")
            addSubview(button)
            pageButtons[page] = button
        }

        buildPages()

        let pageY: CGFloat = 92
        let bottomHeight: CGFloat = 52
        pageScroll.frame = NSRect(
            x: 0, y: pageY, width: width, height: contentHeight - pageY - bottomHeight
        )
        pageScroll.autoresizingMask = [.width, .height]
        pageScroll.drawsBackground = false
        pageScroll.hasHorizontalScroller = false
        pageScroll.autohidesScrollers = true
        pageScroll.borderType = .noBorder
        addSubview(pageScroll)

        let separator = NSView(frame: NSRect(
            x: left, y: contentHeight - bottomHeight, width: width - 2 * left, height: 1
        ))
        separator.wantsLayer = true
        separator.layer?.backgroundColor = Tokens.Color.gray(1, 0.10).cgColor
        separator.autoresizingMask = [.width, .minYMargin]
        addSubview(separator)

        let quit = NSButton(frame: NSRect(x: left, y: contentHeight - 41, width: 118, height: 25))
        quit.title = "Quit COORD"
        quit.isBordered = false
        quit.alignment = .left
        quit.contentTintColor = Tokens.Color.red
        quit.font = .systemFont(ofSize: 10.5, weight: .medium)
        quit.target = self
        quit.action = #selector(doQuit)
        quit.autoresizingMask = [.maxXMargin, .minYMargin]
        addSubview(quit)

        let credit = UI.label("Omar Motala 2026", size: 9, color: Tokens.Color.dimGray)
        credit.alignment = .center
        credit.frame = NSRect(x: 132, y: contentHeight - 36, width: width - 264, height: 16)
        credit.autoresizingMask = [.minXMargin, .maxXMargin, .minYMargin]
        addSubview(credit)

        let done = NSButton(frame: NSRect(
            x: width - 96, y: contentHeight - 42, width: 80, height: 27
        ))
        done.title = "Done"
        done.bezelStyle = .rounded
        done.font = .systemFont(ofSize: 11, weight: .semibold)
        done.target = self
        done.action = #selector(doSave)
        done.autoresizingMask = [.minXMargin, .minYMargin]
        addSubview(done)

        show(page: .general)
    }

    private func buildPages() {
        buildGeneralPage()
        buildDisplayPage()
        buildUsagePage()
        buildPowerPage()
        buildAdvancedPage()
        for popup in [telemetryProfilePopup, telemetrySpacingPopup, statusItemPopup,
                      usageMetricPopup, usageFillPopup, usagePalettePopup, transportPopup] {
            popup.target = self
            popup.action = #selector(controlChanged)
        }
    }

    private func makePage() -> RowView {
        let page = RowView(frame: NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: 1))
        page.autoresizingMask = [.width]
        return page
    }

    private func finish(_ page: Page, view: RowView, y: CGFloat) {
        let height = max(y + 14, pageScroll.bounds.height)
        view.frame = NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: height)
        pages[page] = view
        pageHeights[page] = height
    }

    private func section(_ title: String, in page: RowView, y: inout CGFloat) {
        if y > 18 {
            let divider = NSView(frame: NSRect(x: 16, y: y, width: bounds.width - 32, height: 1))
            divider.wantsLayer = true
            divider.layer?.backgroundColor = Tokens.Color.gray(1, 0.08).cgColor
            page.addSubview(divider)
            y += 12
        }
        let label = UI.label(title, size: 10, weight: .bold, color: Tokens.Color.sectionGray)
        label.frame = NSRect(x: 16, y: y, width: bounds.width - 32, height: 16)
        page.addSubview(label)
        y += 22
    }

    @discardableResult
    private func check(
        _ title: String, _ enabled: Bool, in page: RowView, y: inout CGFloat
    ) -> NSButton {
        let button = NSButton(checkboxWithTitle: title, target: self, action: #selector(controlChanged))
        button.frame = NSRect(x: 18, y: y, width: bounds.width - 36, height: 19)
        button.state = enabled ? .on : .off
        (button.cell as? NSButtonCell)?.font = .systemFont(ofSize: 10.5)
        page.addSubview(button)
        y += 25
        return button
    }

    private func field(
        _ title: String, _ key: String, _ value: String, in page: RowView, y: inout CGFloat
    ) {
        let label = UI.label(title, size: 10.5, color: Tokens.Color.lightGray)
        label.frame = NSRect(x: 18, y: y + 3, width: 180, height: 16)
        page.addSubview(label)
        let input = NSTextField(string: value)
        input.frame = NSRect(x: 204, y: y, width: bounds.width - 222, height: 21)
        input.font = .systemFont(ofSize: 10.5)
        input.delegate = self
        input.target = self
        input.action = #selector(controlChanged)
        fields[key] = input
        page.addSubview(input)
        y += 28
    }

    private func popup(
        _ title: String,
        items: [String],
        values: [String],
        selected: String,
        identifier: String? = nil,
        in page: RowView,
        y: inout CGFloat
    ) -> NSPopUpButton {
        let label = UI.label(title, size: 10.5, color: Tokens.Color.lightGray)
        label.frame = NSRect(x: 18, y: y + 3, width: 180, height: 16)
        page.addSubview(label)
        let menu = NSPopUpButton(frame: NSRect(x: 204, y: y - 2, width: bounds.width - 222, height: 24))
        menu.addItems(withTitles: items)
        for (item, value) in zip(menu.itemArray, values) { item.representedObject = value }
        menu.selectItem(at: max(0, values.firstIndex(of: selected) ?? 0))
        if let identifier { menu.identifier = NSUserInterfaceItemIdentifier(identifier) }
        page.addSubview(menu)
        y += 28
        return menu
    }

    private func note(_ text: String, in page: RowView, y: inout CGFloat) {
        let label = UI.label(text, size: 9.5, color: Tokens.Color.dimGray)
        label.frame = NSRect(x: 18, y: y, width: bounds.width - 36, height: 28)
        label.cell?.wraps = true
        label.cell?.usesSingleLineMode = false
        label.lineBreakMode = .byWordWrapping
        page.addSubview(label)
        y += 34
    }

    private func buildGeneralPage() {
        let page = makePage()
        var y: CGFloat = 12
        section("Accounts & routing", in: page, y: &y)
        let accounts = NSButton(frame: NSRect(x: 18, y: y, width: bounds.width - 36, height: 29))
        accounts.title = "Accounts · Services · Routing…"
        accounts.bezelStyle = .rounded
        accounts.font = .systemFont(ofSize: 10.5, weight: .semibold)
        accounts.image = NSImage(systemSymbolName: "person.crop.circle.badge.checkmark", accessibilityDescription: nil)
        accounts.imagePosition = .imageLeading
        accounts.target = self
        accounts.action = #selector(doOpenProviderAccounts)
        accounts.setAccessibilityLabel("Provider accounts, services, and intelligent routing")
        accounts.setAccessibilityHelp("Opens multi-account, service, Keychain, and routing controls.")
        page.addSubview(accounts)
        y += 38

        let cockpit = NSButton(frame: NSRect(x: 18, y: y, width: bounds.width - 36, height: 29))
        cockpit.title = "Open Full Cockpit Window…"
        cockpit.bezelStyle = .rounded
        cockpit.font = .systemFont(ofSize: 10.5, weight: .semibold)
        cockpit.target = self
        cockpit.action = #selector(doOpenCockpit)
        cockpit.toolTip = "Open COORD’s full-size searchable cockpit window"
        page.addSubview(cockpit)
        y += 38

        section("Hotkey", in: page, y: &y)
        field("Key", "hk_key", cfg.hotkey.key, in: page, y: &y)
        field("Modifiers (csv)", "hk_mods", cfg.hotkey.mods.joined(separator: ","), in: page, y: &y)

        section("Work display", in: page, y: &y)
        field("Next jobs visible", "next_visible", String(cfg.nextVisible), in: page, y: &y)
        field("Expand next count", "expand_count", String(cfg.expandCount), in: page, y: &y)
        showAttention = check("Show Needs Attention by default", !cfg.attentionCollapsed, in: page, y: &y)
        showFollowup = check("Show Follow-up by default", !cfg.followupCollapsed, in: page, y: &y)
        showLocalQueue = check("Show local queue by default", !cfg.localQueueCollapsed, in: page, y: &y)

        section("Behavior", in: page, y: &y)
        stayOpen = check("Keep panel open on click-away", cfg.stayOpen, in: page, y: &y)
        notifications = check("Job notifications", cfg.notifications, in: page, y: &y)
        launchLogin = check("Launch COORD at login", cfg.launchAtLogin, in: page, y: &y)
        taskActions = check("Show row controls", cfg.taskActionsEnabled, in: page, y: &y)
        panelDetached = check("Always open main panel as a resizable window", cfg.panelDetached, in: page, y: &y)
        panelAlwaysOnTop = check("Keep pop-out above other windows", cfg.panelAlwaysOnTop, in: page, y: &y)
        finish(.general, view: page, y: y)
    }

    private func buildDisplayPage() {
        let page = makePage()
        var y: CGFloat = 12
        section("System stats · Menu bar", in: page, y: &y)
        telemetryEnabled = check("System stats enabled", cfg.systemTelemetryEnabled, in: page, y: &y)
        telemetryStatusItem = check("Show Stats as a separate menu-bar item", cfg.systemTelemetryInStatusItem, in: page, y: &y)
        telemetryPopover = check("Show stats in the main panel", cfg.systemTelemetryInPopover, in: page, y: &y)
        telemetryCockpit = check("Show stats in the cockpit", cfg.systemTelemetryInCockpit, in: page, y: &y)
        telemetryCPU = check("CPU", cfg.systemTelemetryShowCPU, in: page, y: &y)
        telemetryGPU = check("GPU", cfg.systemTelemetryShowGPU, in: page, y: &y)
        telemetryRAM = check("RAM", cfg.systemTelemetryShowRAM, in: page, y: &y)
        telemetryDisk = check("Disk", cfg.systemTelemetryShowDisk, in: page, y: &y)
        telemetryProfilePopup = popup(
            "Display refresh", items: ["Eco", "Balanced", "Live"], values: ["eco", "balanced", "live"],
            selected: cfg.systemTelemetryProfile, identifier: "coord.settings.telemetry-profile", in: page, y: &y
        )
        telemetrySpacingPopup = popup(
            "Menu-bar stats spacing", items: ["Compact", "Comfortable"], values: ["compact", "comfortable"],
            selected: cfg.systemTelemetryCompactSpacing ? "compact" : "comfortable",
            identifier: "coord.settings.telemetry-spacing", in: page, y: &y
        )
        field("Warning threshold (%)", "telemetry_warning", String(Int(cfg.systemTelemetryWarningThreshold)), in: page, y: &y)
        field("Critical threshold (%)", "telemetry_critical", String(Int(cfg.systemTelemetryCriticalThreshold)), in: page, y: &y)

        section("Panel appearance", in: page, y: &y)
        field("Glass opacity (0–1)", "glass_alpha", String(format: "%.2f", cfg.glassAlpha), in: page, y: &y)
        finish(.display, view: page, y: y)
    }

    private func buildUsagePage() {
        let page = makePage()
        var y: CGFloat = 12
        section("Usage display", in: page, y: &y)
        showInlineUsage = check("Show inline Usage details", !cfg.usagePeekCollapsed, in: page, y: &y)
        statusItemPopup = popup(
            "Menu-bar usage", items: statusChoices.map(\.title), values: statusChoices.map { $0.mode.rawValue },
            selected: UsageStatusMode.resolve(cfg.statusItemMode).rawValue,
            identifier: "coord.settings.status-mode", in: page, y: &y
        )
        usageMetricPopup = popup(
            "Quota window", items: ["Auto", "Weekly", "Session"], values: UsageMetricMode.allCases.map(\.rawValue),
            selected: UsageMetricMode.resolve(cfg.usageMetricMode).rawValue,
            identifier: "coord.settings.usage-window", in: page, y: &y
        )
        usageFillPopup = popup(
            "Bar meaning", items: ["Remaining", "Used"], values: ["remaining", "used"],
            selected: cfg.usageBarsShowUsed ? "used" : "remaining",
            identifier: "coord.settings.usage-fill", in: page, y: &y
        )
        usagePalettePopup = popup(
            "Bar palette", items: ["Provider colors", "Neutral white / gray"],
            values: [UsageBarPalette.colored.rawValue, UsageBarPalette.neutral.rawValue],
            selected: UsageBarPalette.resolve(cfg.usageBarPalette).rawValue,
            identifier: "coord.settings.usage-palette", in: page, y: &y
        )
        field("Auto session threshold", "usage_threshold", String(Int(cfg.usageSessionThreshold)), in: page, y: &y)
        field("History horizon (days)", "usage_history_days", String(cfg.usageHistoryDays), in: page, y: &y)
        field("Low quota warning (%)", "usage_warning", String(Int(cfg.usageWarningThreshold)), in: page, y: &y)
        showResetETA = check("Show reset ETA", cfg.usageShowResetETA, in: page, y: &y)
        showRunoutETA = check("Show run-out ETA", cfg.usageShowRunoutETA, in: page, y: &y)
        warningMarkers = check("Show low-quota marker on bars", cfg.usageWarningMarkersVisible, in: page, y: &y)
        slowRing = check("Keep quota progress live while panel is closed", cfg.slowRingTick, in: page, y: &y)
        finish(.usage, view: page, y: y)
    }

    private func buildPowerPage() {
        let page = makePage()
        var y: CGFloat = 12
        section("Menu bar & dropdown", in: page, y: &y)
        batteryStatusItem = check("Show Battery as a separate menu-bar item", cfg.batteryStatusItemEnabled, in: page, y: &y)
        note("The separate item is compact and opens the same verified Battery & Performance controls used by the main COORD panel.", in: page, y: &y)

        section("Battery & performance", in: page, y: &y)
        let power = NSButton(frame: NSRect(x: 18, y: y, width: bounds.width - 36, height: 30))
        power.title = "Battery & Power Status…"
        power.bezelStyle = .rounded
        power.font = .systemFont(ofSize: 10.5, weight: .semibold)
        power.target = self
        power.action = #selector(doOpenPower)
        power.toolTip = "Battery status, native charge limit, and per-source Energy Mode controls."
        page.addSubview(power)
        y += 39
        note("Charge Limit uses Apple’s native Manual Charge Limit state and verifies the exact target after every change. Energy Mode remains source-specific.", in: page, y: &y)
        finish(.power, view: page, y: y)
    }

    private func buildAdvancedPage() {
        let page = makePage()
        var y: CGFloat = 12
        section("Refresh & history", in: page, y: &y)
        field("Panel refresh interval (s)", "refresh", String(Int(cfg.refreshSecs)), in: page, y: &y)
        field("Closed-panel refresh (s)", "slow_interval", String(Int(cfg.slowRingInterval)), in: page, y: &y)
        field("Request timeout (s)", "fetch_timeout", String(Int(cfg.fetchTimeoutSecs)), in: page, y: &y)
        showVitals = check("Show work vitals in panel", cfg.showVitalsInPopover, in: page, y: &y)

        section("Data & Compatibility", in: page, y: &y)
        transportPopup = popup(
            "Work data source", items: transportChoices.map(\.title), values: transportChoices.map { $0.transport.rawValue },
            selected: SnapshotTransportKind.resolve(cfg.transport).rawValue,
            identifier: "coord.settings.transport", in: page, y: &y
        )
        note("Local database is canonical. Filewatch and HTTP remain explicit legacy compatibility transports.", in: page, y: &y)
        finish(.advanced, view: page, y: y)
    }

    private func show(page: Page) {
        guard let view = pages[page] else { return }
        pageScroll.documentView = view
        pageScroll.hasVerticalScroller = (pageHeights[page] ?? 0) > pageScroll.bounds.height
        pageScroll.contentView.scroll(to: .zero)
        pageScroll.reflectScrolledClipView(pageScroll.contentView)
        for (candidate, button) in pageButtons {
            let active = candidate == page
            button.contentTintColor = active ? .white : Tokens.Color.dimGray
            button.wantsLayer = true
            button.layer?.backgroundColor = active
                ? Tokens.Color.progressBlue.withAlphaComponent(0.20).cgColor
                : NSColor.clear.cgColor
            button.layer?.cornerRadius = 7
        }
    }

    @objc private func selectPage(_ sender: NSButton) {
        window?.makeFirstResponder(nil)
        guard Page.allCases.indices.contains(sender.tag) else { return }
        show(page: Page.allCases[sender.tag])
    }

    @objc private func doClose() { window?.makeFirstResponder(nil); onClose?() }
    @objc private func doOpenProviderAccounts() { onOpenProviderAccounts?() }
    @objc private func doOpenPower() { onOpenPower?() }
    @objc private func doOpenCockpit() { onOpenCockpit?() }
    @objc private func doQuit() { onQuit?() }

    @objc private func doSave() {
        guard !isCompletingSave else { return }
        isCompletingSave = true
        defer { isCompletingSave = false }
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
        func s(_ key: String) -> String { fields[key]?.stringValue ?? "" }
        func represented(_ popup: NSPopUpButton, fallback: String) -> String {
            popup.selectedItem?.representedObject as? String ?? fallback
        }
        cfg.hotkey.key = s("hk_key").lowercased().isEmpty ? "comma" : s("hk_key").lowercased()
        cfg.hotkey.mods = s("hk_mods").split(separator: ",").map {
            $0.trimmingCharacters(in: .whitespaces)
        }.filter { !$0.isEmpty }
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
        cfg.systemTelemetryInPopover = telemetryPopover.state == .on
        cfg.batteryStatusItemEnabled = batteryStatusItem.state == .on
        cfg.systemTelemetryInCockpit = telemetryCockpit.state == .on
        cfg.systemTelemetryShowCPU = telemetryCPU.state == .on
        cfg.systemTelemetryShowGPU = telemetryGPU.state == .on
        cfg.systemTelemetryShowRAM = telemetryRAM.state == .on
        cfg.systemTelemetryShowDisk = telemetryDisk.state == .on
        let thresholds = SystemTelemetryDisplayPolicy(
            warningThreshold: Double(s("telemetry_warning")) ?? cfg.systemTelemetryWarningThreshold,
            criticalThreshold: Double(s("telemetry_critical")) ?? cfg.systemTelemetryCriticalThreshold
        )
        cfg.systemTelemetryWarningThreshold = thresholds.warningThreshold
        cfg.systemTelemetryCriticalThreshold = thresholds.criticalThreshold
        cfg.systemTelemetryProfile = represented(telemetryProfilePopup, fallback: cfg.systemTelemetryProfile)
        cfg.systemTelemetryCompactSpacing = represented(telemetrySpacingPopup, fallback: cfg.systemTelemetryCompactSpacing ? "compact" : "comfortable") == "compact"
        cfg.slowRingTick = slowRing.state == .on
        cfg.launchAtLogin = launchLogin.state == .on
        cfg.taskActionsEnabled = taskActions.state == .on
        cfg.panelDetached = panelDetached.state == .on
        cfg.panelAlwaysOnTop = panelAlwaysOnTop.state == .on
        cfg.usagePeekCollapsed = showInlineUsage.state != .on
        cfg.attentionCollapsed = showAttention.state != .on
        cfg.followupCollapsed = showFollowup.state != .on
        cfg.localQueueCollapsed = showLocalQueue.state != .on
        cfg.transport = represented(transportPopup, fallback: cfg.transport)
        cfg.statusItemMode = represented(statusItemPopup, fallback: cfg.statusItemMode)
        cfg.usageMetricMode = represented(usageMetricPopup, fallback: cfg.usageMetricMode)
        cfg.usageBarsShowUsed = represented(
            usageFillPopup,
            fallback: cfg.usageBarsShowUsed ? "used" : "remaining"
        ) == "used"
        cfg.usageBarPalette = represented(usagePalettePopup, fallback: cfg.usageBarPalette)
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
