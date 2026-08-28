import AppKit

final class CockpitWindowController: NSWindowController {
    private let source: CockpitReadModelSource
    private let refresher: CockpitProjectionRefresher
    private let fallbackSource: CockpitHTTPFallbackSource
    private let capabilitySource: CockpitCapabilityInventorySource
    private let mapSource: CockpitMapReadModelSource
    private let stateStore: CockpitWindowUIStateStore
    private let broker = NativeCockpitActionBroker()
    private let rootView: CockpitRootView
    private var timer: Timer?
    private var lastState: CockpitState?
    private var lastCapabilityInventory: CockpitCapabilityInventory?
    private var lastMapState: CockpitMapState?
    private var lastValidSQLiteAt = Date.distantPast
    private var lastProjectionRefresh = Date.distantPast
    private var lastCapabilityRefresh = Date.distantPast
    private var lastMapRefresh = Date.distantPast
    private var isReloading = false
    private var isMapReloading = false
    private var isRestoringWindowState = false
    private var stateSaveTimer: Timer?
    private let sqliteFallbackGraceSeconds: TimeInterval = 45
    private let previewIgnoreSavedState = ProcessInfo.processInfo.environment["COORD_COCKPIT_PREVIEW_IGNORE_SAVED_STATE"] == "1"

    init(
        source: CockpitReadModelSource = CockpitReadModelSource(),
        refresher: CockpitProjectionRefresher = CockpitProjectionRefresher(),
        fallbackSource: CockpitHTTPFallbackSource = CockpitHTTPFallbackSource(),
        capabilitySource: CockpitCapabilityInventorySource = CockpitCapabilityInventorySource(),
        mapSource: CockpitMapReadModelSource = CockpitMapReadModelSource(),
        stateStore: CockpitWindowUIStateStore = CockpitWindowUIStateStore(),
        usageStore: InstalledUsageStore = InstalledUsageStore(),
        usageManagedExternally: Bool = false
    ) {
        self.source = source
        self.refresher = refresher
        self.fallbackSource = fallbackSource
        self.capabilitySource = capabilitySource
        self.mapSource = mapSource
        self.stateStore = stateStore
        let initialSize = Self.initialWindowSize()
        self.rootView = CockpitRootView(
            frame: NSRect(origin: .zero, size: initialSize),
            usageStore: usageStore,
            usageManagedExternally: usageManagedExternally
        )
        let content = NSViewController()
        rootView.autoresizingMask = [.width, .height]
        content.view = rootView
        let window = NSWindow(
            contentRect: NSRect(origin: .zero, size: initialSize),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "COORD Cockpit"
        window.titleVisibility = .hidden
        window.minSize = CockpitTokens.windowMinSize
        window.contentViewController = content
        window.titlebarAppearsTransparent = true
        window.isReleasedWhenClosed = false
        super.init(window: window)
        window.delegate = self
        rootView.delegate = self
    }

    required init?(coder: NSCoder) { nil }

    private static func initialWindowSize() -> NSSize {
        if ProcessInfo.processInfo.environment["COORD_COCKPIT_PREVIEW_NARROW"] == "1" {
            return NSSize(width: 1120, height: 720)
        }
        return CockpitTokens.windowDefaultSize
    }

    override func showWindow(_ sender: Any?) {
        restoreWindowStateIfNeeded()
        super.showWindow(sender)
        window?.makeKeyAndOrderFront(sender)
        NSApp.activate(ignoringOtherApps: true)
        Task { @MainActor in
            await refreshProjectionThenReload(force: true)
            startTimer()
        }
    }

    override func close() {
        flushWindowStateSave()
        timer?.invalidate()
        timer = nil
        super.close()
    }

    func setDiagnosticsOpen(_ isOpen: Bool) {
        rootView.setDiagnosticsOpen(isOpen)
    }

    func setInspectorOpen(_ isOpen: Bool) {
        rootView.setInspectorOpen(isOpen)
    }

    func expandFirstVisibleRow() {
        rootView.expandFirstVisibleRow()
    }

    func showColumnsPanel() {
        rootView.showColumnsPanel()
    }

    func showCommandsMenu() {
        rootView.showCommandsMenu()
    }

    func showFirstUnsafeActionConfirmation() {
        rootView.showFirstUnsafeActionConfirmation()
    }

    private func startTimer() {
        timer?.invalidate()
        let refreshTimer = Timer.scheduledTimer(withTimeInterval: 1.5, repeats: true) { [weak self] _ in
            Task { @MainActor in
                await self?.refreshProjectionThenReload(force: false)
            }
        }
        refreshTimer.tolerance = 0.45
        timer = refreshTimer
    }

    private func refreshProjectionThenReload(force: Bool) async {
        guard !isReloading else { return }
        isReloading = true
        defer { isReloading = false }

        let shouldRefreshProjection = force || Date().timeIntervalSince(lastProjectionRefresh) > 3
        if shouldRefreshProjection {
            let refreshResult = await refresher.refresh()
            lastProjectionRefresh = Date()
            if case .success(.compactFallback) = refreshResult {
                if reloadFromSQLiteAfterCompactFallback() {
                    await reloadCapabilityInventoryIfNeeded(force: force)
                    return
                }
                await reloadFromHTTPFallback()
                await reloadCapabilityInventoryIfNeeded(force: force)
                return
            }
        }
        reloadFromSQLite(force: force)
        if lastState?.error != nil {
            await reloadFromHTTPFallback()
        }
        await reloadCapabilityInventoryIfNeeded(force: force)
        reloadMapIfNeeded(force: force)
    }

    private func reloadMapIfNeeded(force: Bool) {
        guard rootView.isMapSurfaceActive else { return }
        guard !isMapReloading else { return }
        guard force || Date().timeIntervalSince(lastMapRefresh) > 15 else { return }
        lastMapRefresh = Date()
        isMapReloading = true
        let mapSource = self.mapSource
        Task.detached(priority: .utility) { [mapSource] in
            let mapState = mapSource.load()
            await MainActor.run { [weak self] in
                guard let self else { return }
                self.isMapReloading = false
                guard self.rootView.isMapSurfaceActive else {
                    self.lastMapState = nil
                    return
                }
                self.lastMapState = mapState
                self.rootView.render(mapState: mapState)
            }
        }
    }

    private func reloadFromHTTPFallback() async {
        let state = await fallbackSource.load()
        if CockpitStatePreference.shouldKeepCurrentState(overFallback: state, current: lastState) {
            return
        }
        lastState = enrichedState(state)
        rootView.render(state: lastState ?? state)
    }

    private func reloadFromSQLiteAfterCompactFallback() -> Bool {
        let state = source.load()
        guard CockpitStatePreference.prefersNativeSQLite(state) else { return false }
        lastValidSQLiteAt = Date()
        lastState = enrichedState(state)
        rootView.render(state: lastState ?? state)
        return true
    }

    private func reloadFromSQLite(force: Bool) {
        let loadedAt = Date()
        let state: CockpitState
        if !force, let lastState, let changed = source.reloadIfChanged(since: lastState) {
            state = changed
        } else if force || lastState == nil {
            state = source.load()
        } else {
            return
        }
        if CockpitStatePreference.shouldKeepCurrentState(overSQLiteState: state, current: lastState),
           let lastState {
            let age = loadedAt.timeIntervalSince(lastValidSQLiteAt)
            let preserved = enrichedState(CockpitStatePreference.preservedCurrentState(
                lastState,
                overSQLiteState: state,
                lastGoodAge: age,
                graceSeconds: sqliteFallbackGraceSeconds
            ))
            self.lastState = preserved
            rootView.render(state: preserved)
            return
        }
        if CockpitStatePreference.prefersNativeSQLite(state) {
            lastValidSQLiteAt = loadedAt
        }
        lastState = enrichedState(state)
        rootView.render(state: lastState ?? state)
    }

    private func reloadCapabilityInventoryIfNeeded(force: Bool) async {
        guard force || Date().timeIntervalSince(lastCapabilityRefresh) > 30 else { return }
        lastCapabilityRefresh = Date()
        guard let inventory = await capabilitySource.load() else { return }
        lastCapabilityInventory = inventory
        guard let lastState else { return }
        let enriched = enrichedState(lastState)
        self.lastState = enriched
        rootView.render(state: enriched)
    }

    private func enrichedState(_ state: CockpitState) -> CockpitState {
        var copy = state
        copy.capabilityInventory = lastCapabilityInventory
        return copy
    }

    private func restoreWindowStateIfNeeded() {
        if previewIgnoreSavedState {
            if let window, !window.isVisible { window.center() }
            return
        }
        let state = stateStore.load()
        isRestoringWindowState = true
        defer { isRestoringWindowState = false }
        rootView.applyUIState(state)
        guard let window else { return }
        if let frame = state.frame?.nsRect, frame.intersectsAnyVisibleScreen {
            window.setFrame(frame, display: false)
        } else if !window.isVisible {
            window.center()
        }
    }

    private func saveWindowState() {
        guard !isRestoringWindowState else { return }
        stateSaveTimer?.invalidate()
        stateSaveTimer = Timer.scheduledTimer(withTimeInterval: 0.22, repeats: false) { [weak self] _ in
            self?.saveWindowStateNow()
        }
        stateSaveTimer?.tolerance = 0.08
    }

    private func flushWindowStateSave() {
        stateSaveTimer?.invalidate()
        stateSaveTimer = nil
        saveWindowStateNow()
    }

    private func saveWindowStateNow() {
        guard !isRestoringWindowState else { return }
        let frame = window.map { CockpitWindowFrame(rect: $0.frame) }
        stateStore.save(rootView.currentUIState(frame: frame))
    }
}

extension CockpitWindowController: CockpitRootViewDelegate {
    func cockpitRootViewDidRequestRefresh(_ view: CockpitRootView) {
        Task { @MainActor in
            await refreshProjectionThenReload(force: true)
            reloadMapIfNeeded(force: true)
        }
    }

    func cockpitRootViewDidRequestMapRefresh(_ view: CockpitRootView) {
        reloadMapIfNeeded(force: true)
    }

    func cockpitRootViewDidReleaseMapResources(_ view: CockpitRootView) {
        lastMapState = nil
        lastMapRefresh = .distantPast
    }

    func cockpitRootViewDidChangeLocalState(_ view: CockpitRootView) {
        saveWindowState()
    }

    func cockpitRootView(_ view: CockpitRootView, perform action: String, row: CockpitRow?, payload: [String: Any]) {
        Task { @MainActor in
            view.setActionStatus("Sending \(action)...")
            let result = await broker.perform(action: action, row: row, payload: payload)
            switch result {
            case .success(let actionResult):
                view.setActionResult(actionResult)
                await refreshProjectionThenReload(force: true)
            case .failure(let error):
                view.setActionStatus(error.localizedDescription)
            }
        }
    }

    func cockpitRootView(_ view: CockpitRootView, open path: String) {
        if path.hasPrefix("http"), let url = URL(string: path) {
            NSWorkspace.shared.open(url)
        } else if let url = URL(string: "\(HarnessEndpoint.base)\(path)") {
            NSWorkspace.shared.open(url)
        }
    }
}

extension CockpitWindowController: NSWindowDelegate {
    func windowDidMove(_ notification: Notification) {
        saveWindowState()
    }

    func windowDidResize(_ notification: Notification) {
        saveWindowState()
    }

    func windowDidBecomeKey(_ notification: Notification) {
        rootView.setCockpitAnimationsEnabled(true)
    }

    func windowDidResignKey(_ notification: Notification) {
        rootView.setCockpitAnimationsEnabled(false)
    }

    func windowDidMiniaturize(_ notification: Notification) {
        rootView.setCockpitAnimationsEnabled(false)
    }

    func windowDidDeminiaturize(_ notification: Notification) {
        rootView.setCockpitAnimationsEnabled(true)
    }

    func windowWillClose(_ notification: Notification) {
        flushWindowStateSave()
        rootView.setCockpitAnimationsEnabled(false)
        rootView.releaseMapResources()
        timer?.invalidate()
        timer = nil
        stateSaveTimer?.invalidate()
        stateSaveTimer = nil
    }
}

private extension CockpitWindowFrame {
    init(rect: NSRect) {
        self.init(
            x: Double(rect.origin.x),
            y: Double(rect.origin.y),
            width: Double(rect.size.width),
            height: Double(rect.size.height)
        )
    }

    var nsRect: NSRect {
        NSRect(x: x, y: y, width: width, height: height)
    }
}

private extension NSRect {
    var intersectsAnyVisibleScreen: Bool {
        guard width >= CockpitTokens.windowMinSize.width, height >= CockpitTokens.windowMinSize.height else { return false }
        return NSScreen.screens.contains { $0.visibleFrame.intersects(self) }
    }
}
