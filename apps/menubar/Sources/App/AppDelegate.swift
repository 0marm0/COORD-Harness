import AppKit



final class AppDelegate: NSObject, NSApplicationDelegate {

    private var config = Config.load()
    private var source: SnapshotSource!
    private let usageStore = InstalledUsageStore()
    private lazy var telemetryStore: SystemTelemetryStore = MainActor.assumeIsolated { SystemTelemetryStore() }
    private lazy var statusItem = StatusItemController()
    private lazy var popover = PopoverController(config: config, usageStore: usageStore)
    private lazy var cockpitWindow = CockpitWindowController(
        usageStore: usageStore,
        usageManagedExternally: true
    )
    private var previewWindow: NSWindow?
    private lazy var hotkey = HotkeyManager()
    private let notifier = Notifier()
    private lazy var menuConfigCommitter = DeferredCoalescedValue<Config>()

    private var openTimer: Timer?
    private var slowTimer: Timer?
    private var usageTimer: Timer?
    private var telemetryTimer: Timer?
    private var lastState: MenubarState?
    private var snapshotRefreshInFlight = false
    private var snapshotRefreshPending = false
    private var refreshLogCount = 0
    private var lastRefreshLogKey = ""
    private var cockpitPreviewOnly: Bool {
        ProcessInfo.processInfo.environment["COORD_COCKPIT_PREVIEW_ONLY"] == "1"
    }
    private var cockpitPreviewEmptyWindow: Bool {
        ProcessInfo.processInfo.environment["COORD_COCKPIT_PREVIEW_EMPTY_WINDOW"] == "1"
    }
    private var cockpitPreviewRootOnly: Bool {
        ProcessInfo.processInfo.environment["COORD_COCKPIT_PREVIEW_ROOT_ONLY"] == "1"
    }
    private var cockpitPreviewRootEmptyRender: Bool {
        ProcessInfo.processInfo.environment["COORD_COCKPIT_PREVIEW_ROOT_EMPTY_RENDER"] == "1"
    }
    private var cockpitPreviewDiagnostics: Bool {
        ProcessInfo.processInfo.environment["COORD_COCKPIT_PREVIEW_DIAGNOSTICS"] == "1"
    }
    private var cockpitPreviewInspector: Bool {
        ProcessInfo.processInfo.environment["COORD_COCKPIT_PREVIEW_INSPECTOR"] == "1"
    }
    private var cockpitPreviewExpandFirst: Bool {
        ProcessInfo.processInfo.environment["COORD_COCKPIT_PREVIEW_EXPAND_FIRST"] == "1"
    }
    private var cockpitPreviewColumns: Bool {
        ProcessInfo.processInfo.environment["COORD_COCKPIT_PREVIEW_COLUMNS"] == "1"
    }
    private var cockpitPreviewCommands: Bool {
        ProcessInfo.processInfo.environment["COORD_COCKPIT_PREVIEW_COMMANDS"] == "1"
    }
    private var cockpitPreviewConfirm: Bool {
        ProcessInfo.processInfo.environment["COORD_COCKPIT_PREVIEW_CONFIRM"] == "1"
    }

    func applicationWillFinishLaunching(_ note: Notification) {
        if cockpitPreviewOnly { return }

        if !SingleInstanceGuard.acquire() {
            NSLog("[coordharness-mac] another instance already running — terminating")
            NSApp.terminate(nil)
        }
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        MenubarLog.info("applicationDidFinishLaunching — creating status item")
        ProcessInfo.processInfo.disableAutomaticTermination("coordharness menu-bar stays resident")
        if cockpitPreviewOnly {
            NSApp.setActivationPolicy(.regular)
            if cockpitPreviewEmptyWindow {
                openPreviewEmptyWindow()
                return
            }
            if cockpitPreviewRootOnly {
                openPreviewRootWindow()
                return
            }
            openCockpitWindow()
            if cockpitPreviewDiagnostics {
                cockpitWindow.setDiagnosticsOpen(true)
            } else if cockpitPreviewInspector {
                cockpitWindow.setInspectorOpen(true)
            }
            if cockpitPreviewExpandFirst {
                DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { [weak self] in
                    self?.cockpitWindow.expandFirstVisibleRow()
                }
            }
            if cockpitPreviewColumns {
                DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { [weak self] in
                    self?.cockpitWindow.showColumnsPanel()
                }
            }
            if cockpitPreviewCommands {
                DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { [weak self] in
                    self?.cockpitWindow.showCommandsMenu()
                }
            }
            if cockpitPreviewConfirm {
                DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { [weak self] in
                    self?.cockpitWindow.showFirstUnsafeActionConfirmation()
                }
            }
            return
        }
        NSApp.setActivationPolicy(.accessory)

        statusItem.onClick = { [weak self] button in self?.togglePopover(button) }
        statusItem.onRefresh = { [weak self] in self?.refresh() }
        statusItem.onOpenSettings = { [weak self] in self?.openSettingsFromStatusMenu() }
        statusItem.onStatusModeChange = { [weak self] mode in
            self?.enqueueMenuConfigChange { $0.statusItemMode = mode.rawValue }
        }
        statusItem.onUsageMetricModeChange = { [weak self] mode in
            self?.enqueueMenuConfigChange { $0.usageMetricMode = mode.rawValue }
        }
        statusItem.onSystemTelemetryVisibilityChange = { [weak self] visible in
            self?.enqueueMenuConfigChange {
                $0.systemTelemetryEnabled = true
                $0.systemTelemetryInStatusItem = visible
                $0.systemTelemetryStatusPreferenceVersion = 1
            }
        }
        statusItem.onSystemTelemetryMetricChange = { [weak self] metric, visible in
            self?.enqueueMenuConfigChange {
                switch metric {
                case "cpu": $0.systemTelemetryShowCPU = visible
                case "gpu": $0.systemTelemetryShowGPU = visible
                case "ram": $0.systemTelemetryShowRAM = visible
                default: $0.systemTelemetryShowDisk = visible
                }
            }
        }
        statusItem.onSystemTelemetryProfileChange = { [weak self] profile in
            self?.enqueueMenuConfigChange { $0.systemTelemetryProfile = profile }
        }
        statusItem.applyPreferences(config)
        usageStore.onStateChange = { [weak self] state in
            self?.statusItem.updateUsage(state)
            self?.popover.updateUsage(state)
        }
        telemetryStore.onStateChange = { [weak self] snapshot in
            self?.statusItem.updateSystemTelemetry(snapshot)
            self?.popover.updateSystemTelemetry(snapshot)
        }
        popover.onWantsRefresh = { [weak self] in self?.refresh() }
        popover.onConfig = { [weak self] cfg in self?.applyConfig(cfg) }

        hotkey.register(config.hotkey) { [weak self] in self?.toggleFromHotkey() }

        notifier.enabled = config.notifications
        if config.notifications { notifier.prime() }
        LoginItemManager.apply(config.launchAtLogin)
        NSWorkspace.shared.notificationCenter.addObserver(
            self, selector: #selector(onWake), name: NSWorkspace.didWakeNotification, object: nil)

        rebuildSource()
        startUsageRefresh()
        startSystemTelemetryRefresh()
        refresh()
    }


    private func rebuildSource() {
        source?.stop()
        let transport = SnapshotTransportKind.resolve(config.transport)
        let s: SnapshotSource
        switch transport {
        case .db:
            s = NativeCockpitDBSource()
        case .filewatch:
            s = FileWatchSource()
        case .http:
            s = HTTPSource(timeout: config.fetchTimeoutSecs)
        }
        config.transport = transport.rawValue
        s.onChange = { [weak self] in self?.refresh() }
        source = s
        s.start()
        if transport == .filewatch { openTimer?.invalidate(); openTimer = nil }
        updateSlowTimer()
        MenubarLog.info("source rebuilt transport=\(config.transport) sourceType=\(type(of: s))")
    }


    private func togglePopover(_ button: NSStatusBarButton) {
        if popover.isShown { popover.close() } else { showPopover(button) }
    }
    private func toggleFromHotkey() {
        guard let button = statusItem.button else { return }
        togglePopover(button)
    }

    private func openSettingsFromStatusMenu() {
        guard let button = statusItem.button else { return }
        let transport = SnapshotTransportKind.resolve(config.transport)
        let cachedFallback = transport == .http ? SnapshotCache.load() : nil
        if let state = lastState ?? cachedFallback { popover.render(state) }
        popover.showSettings(relativeTo: button)
        if transport != .filewatch { startOpenTimer() }
        refresh()
    }

    private func showPopover(_ button: NSStatusBarButton) {
        let transport = SnapshotTransportKind.resolve(config.transport)
        let cachedFallback = (transport == .http) ? SnapshotCache.load() : nil
        if let s = lastState ?? cachedFallback { popover.render(s) }
        popover.show(relativeTo: button)
        if transport != .filewatch { startOpenTimer() }
        refresh()
    }


    private func startOpenTimer() {
        openTimer?.invalidate()


        let openInterval = min(config.refreshSecs, 1.0)
        openTimer = Timer.scheduledTimer(withTimeInterval: openInterval, repeats: true) {
            [weak self] _ in self?.refresh()
        }
    }

    func popoverDidClose() {
        openTimer?.invalidate(); openTimer = nil
    }

    func openCockpitWindow() {
        cockpitWindow.showWindow(nil)
    }

    private func openPreviewEmptyWindow() {
        let window = NSWindow(
            contentRect: NSRect(origin: .zero, size: CockpitTokens.windowDefaultSize),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        let view = NSView(frame: NSRect(origin: .zero, size: CockpitTokens.windowDefaultSize))
        view.wantsLayer = true
        view.layer?.backgroundColor = CockpitTokens.Color.bg.cgColor
        window.contentView = view
        window.title = "COORDHARNESS Cockpit Preview"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        previewWindow = window
    }

    private func openPreviewRootWindow() {
        let window = NSWindow(
            contentRect: NSRect(origin: .zero, size: CockpitTokens.windowDefaultSize),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        let view = CockpitRootView(frame: NSRect(origin: .zero, size: CockpitTokens.windowDefaultSize))
        view.autoresizingMask = [.width, .height]
        if cockpitPreviewRootEmptyRender {
            view.render(state: .error(CockpitLoadErrorState(kind: .transport, message: "Preview empty render")))
        }
        window.contentView = view
        window.title = "COORDHARNESS Cockpit Root Preview"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        previewWindow = window
    }

    @objc private func onWake() {
        refresh()
        Task { @MainActor in await usageStore.refresh() }
        if config.systemTelemetryEnabled {
            Task { @MainActor in await telemetryStore.refresh(baseURL: HarnessEndpoint.url("/")!) }
        }
    }


    func applyConfig(_ cfg: Config) {
        let previous = config
        let oldTransport = SnapshotTransportKind.resolve(previous.transport)
        var cfg = cfg
        let newTransport = SnapshotTransportKind.resolve(cfg.transport)
        cfg.transport = newTransport.rawValue

        let hotkeyChanged = cfg.hotkey.key != previous.hotkey.key || cfg.hotkey.mods != previous.hotkey.mods
        let notificationsChanged = cfg.notifications != previous.notifications
        let loginItemChanged = cfg.launchAtLogin != previous.launchAtLogin
        let telemetryScheduleChanged = cfg.systemTelemetryEnabled != previous.systemTelemetryEnabled
            || cfg.systemTelemetryProfile != previous.systemTelemetryProfile
        let sourceChanged = newTransport != oldTransport || cfg.fetchTimeoutSecs != previous.fetchTimeoutSecs
        let slowTimerChanged = cfg.slowRingTick != previous.slowRingTick
            || cfg.slowRingInterval != previous.slowRingInterval
        let openTimerChanged = cfg.refreshSecs != previous.refreshSecs

        MenubarLog.info("applyConfig transport=\(cfg.transport) source=\(sourceChanged) telemetry=\(telemetryScheduleChanged) hotkey=\(hotkeyChanged)")
        config = cfg
        if hotkeyChanged {
            hotkey.register(cfg.hotkey) { [weak self] in self?.toggleFromHotkey() }
        }
        if notificationsChanged {
            notifier.enabled = cfg.notifications
            if cfg.notifications { notifier.prime() }
        }
        if loginItemChanged {
            LoginItemManager.apply(cfg.launchAtLogin)
        }
        statusItem.applyPreferences(cfg)
        if telemetryScheduleChanged {
            startSystemTelemetryRefresh()
        }
        if sourceChanged {
            rebuildSource()
        } else if slowTimerChanged {
            updateSlowTimer()
        }
        if openTimer != nil, openTimerChanged {
            startOpenTimer()
        }
    }

    func applicationWillTerminate(_ note: Notification) {
        menuConfigCommitter.drain()
        openTimer?.invalidate(); openTimer = nil
        slowTimer?.invalidate(); slowTimer = nil
        usageTimer?.invalidate(); usageTimer = nil
        telemetryTimer?.invalidate(); telemetryTimer = nil
        source?.stop()
        hotkey.teardown()
    }

    private func enqueueMenuConfigChange(_ update: @escaping (inout Config) -> Void) {
        menuConfigCommitter.enqueue(base: config, update: update) { [weak self] next in
            guard let self else { return }
            next.save()
            self.applyConfig(next)
        }
    }

    private func startUsageRefresh() {
        usageTimer?.invalidate()
        Task { @MainActor in await usageStore.refresh() }
        let timer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            guard let self else { return }
            Task { @MainActor in await self.usageStore.refresh() }
        }
        timer.tolerance = 10
        usageTimer = timer
    }


    private func startSystemTelemetryRefresh() {
        telemetryTimer?.invalidate(); telemetryTimer = nil
        guard config.systemTelemetryEnabled else {
            statusItem.updateSystemTelemetry(nil)
            popover.updateSystemTelemetry(nil)
            return
        }
        let interval: TimeInterval = config.systemTelemetryProfile == "eco" ? 15 : (config.systemTelemetryProfile == "live" ? 2 : 5)
        Task { @MainActor in await telemetryStore.refresh(baseURL: HarnessEndpoint.url("/")!) }
        let timer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
            guard let self else { return }
            Task { @MainActor in await self.telemetryStore.refresh(baseURL: HarnessEndpoint.url("/")!) }
        }
        timer.tolerance = config.systemTelemetryProfile == "live" ? 0.2 : 1
        telemetryTimer = timer
    }


    private func updateSlowTimer() {
        slowTimer?.invalidate(); slowTimer = nil
        let transport = SnapshotTransportKind.resolve(config.transport)
        guard transport == .db || transport == .filewatch || config.slowRingTick else { return }
        let interval: TimeInterval
        switch transport {
        case .db:
            interval = max(5, config.slowRingInterval)
        case .filewatch:
            interval = 15
        case .http:
            interval = max(3, config.slowRingInterval)
        }
        slowTimer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) {
            [weak self] _ in self?.refresh()
        }
    }

    private func refresh() {
        Task { @MainActor [weak self] in
            self?.requestSnapshotRefresh()
        }
    }

    @MainActor
    private func requestSnapshotRefresh() {
        guard !snapshotRefreshInFlight else {
            snapshotRefreshPending = true
            return
        }
        guard let requestedSource = source else { return }
        snapshotRefreshInFlight = true
        Task.detached(priority: .utility) { [weak self, requestedSource] in
            let state = await requestedSource.current()
            guard let self else { return }
            await MainActor.run {
                self.finishSnapshotRefresh(state, requestedSource: requestedSource)
            }
        }
    }

    @MainActor
    private func finishSnapshotRefresh(_ state: MenubarState?, requestedSource: SnapshotSource) {
        snapshotRefreshInFlight = false
        if source === requestedSource, let state {
            lastState = state
            let summary = state.workModel?.summary
            refreshLogCount += 1
            let logKey = "\(config.transport)|\(String(describing: state.stale))|\(state.error ?? "nil")|\(state.source ?? "nil")|\(summary?.running ?? -1)|\(summary?.next ?? -1)"
            if refreshLogCount == 1 || refreshLogCount % 30 == 0 || logKey != lastRefreshLogKey {
                MenubarLog.info("render refresh count=\(refreshLogCount) transport=\(config.transport) shown=\(popover.isShown) stale=\(String(describing: state.stale)) error=\(state.error ?? "nil") source=\(state.source ?? "nil") running=\(summary?.running ?? -1) next=\(summary?.next ?? -1)")
                lastRefreshLogKey = logKey
            }
            statusItem.update(with: state)
            notifier.check(state)
            if popover.isShown { popover.render(state) }
        }
        if snapshotRefreshPending {
            snapshotRefreshPending = false
            requestSnapshotRefresh()
        }
    }
}
