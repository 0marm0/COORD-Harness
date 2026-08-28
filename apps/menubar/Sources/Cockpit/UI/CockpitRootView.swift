import AppKit
import SwiftUI

protocol CockpitRootViewDelegate: AnyObject {
    func cockpitRootViewDidRequestRefresh(_ view: CockpitRootView)
    func cockpitRootViewDidRequestMapRefresh(_ view: CockpitRootView)
    func cockpitRootViewDidReleaseMapResources(_ view: CockpitRootView)
    func cockpitRootViewDidChangeLocalState(_ view: CockpitRootView)
    func cockpitRootView(_ view: CockpitRootView, perform action: String, row: CockpitRow?, payload: [String: Any])
    func cockpitRootView(_ view: CockpitRootView, open path: String)
}

private final class CockpitTopBarChromeView: NSView {
    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        drawGlass(in: bounds, radius: 0, strokeAlpha: 0.018, shadow: false)
        NSColor.white.withAlphaComponent(0.018).setFill()
        NSRect(x: 0, y: bounds.height - 1, width: bounds.width, height: 1).fill()
    }
}

private final class CockpitToolbarChromeView: NSView {
    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        drawGlass(in: bounds.insetBy(dx: 0.5, dy: 0.5), radius: 13, strokeAlpha: 0.018, shadow: true)
    }
}

private final class CockpitBoardChromeView: NSView {
    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        let rect = bounds.insetBy(dx: 0.5, dy: 0.5)
        let path = NSBezierPath(roundedRect: rect, xRadius: 14, yRadius: 14)
        NSGraphicsContext.saveGraphicsState()
        let shadow = NSShadow()
        shadow.shadowColor = NSColor.black.withAlphaComponent(0.26)
        shadow.shadowBlurRadius = 28
        shadow.shadowOffset = NSSize(width: 0, height: -8)
        shadow.set()
        CockpitTokens.Color.panel.withAlphaComponent(0.92).setFill()
        path.fill()
        NSGraphicsContext.restoreGraphicsState()

        CockpitTokens.Color.line.withAlphaComponent(0.34).setStroke()
        path.lineWidth = 1
        path.stroke()

        NSColor.white.withAlphaComponent(0.025).setFill()
        NSRect(x: rect.minX + 1, y: rect.minY + 1, width: max(0, rect.width - 2), height: 1).fill()
    }
}

private func drawGlass(in rect: NSRect, radius: CGFloat, strokeAlpha: CGFloat, shadow: Bool) {
    let path = radius > 0
        ? NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)
        : NSBezierPath(rect: rect)
    NSGraphicsContext.saveGraphicsState()
    if shadow {
        let drop = NSShadow()
        drop.shadowColor = NSColor.black.withAlphaComponent(0.24)
        drop.shadowBlurRadius = 28
        drop.shadowOffset = NSSize(width: 0, height: -10)
        drop.set()
    }
    let gradient = NSGradient(colors: [
        CockpitTokens.Color.panel.withAlphaComponent(0.34),
        NSColor.black.withAlphaComponent(0.16),
    ])
    gradient?.draw(in: path, angle: -90)
    NSGraphicsContext.restoreGraphicsState()

    NSColor.white.withAlphaComponent(strokeAlpha).setStroke()
    path.lineWidth = 0.5
    path.stroke()

    NSColor.white.withAlphaComponent(0.020).setFill()
    let topLine = NSRect(x: rect.minX + 1, y: rect.minY + 1, width: max(0, rect.width - 2), height: 1)
    topLine.fill()
}

private func applyQuietButtonChrome(_ button: NSButton?, active: Bool = false, tint: NSColor = CockpitTokens.Color.blue) {
    guard let button else { return }
    button.wantsLayer = true
    button.layer?.cornerRadius = 8
    button.layer?.backgroundColor = active
        ? tint.withAlphaComponent(0.030).cgColor
        : NSColor.white.withAlphaComponent(0.004).cgColor
    button.layer?.borderWidth = 0.5
    button.layer?.borderColor = (active ? tint : NSColor.white).withAlphaComponent(active ? 0.070 : 0.010).cgColor
    button.layer?.shadowColor = tint.cgColor
    button.layer?.shadowOpacity = active ? 0.20 : 0
    button.layer?.shadowRadius = active ? 9 : 0
    button.layer?.shadowOffset = .zero
}

private final class CockpitGlowButton: CockpitButton {
    var isGlowing = false { didSet { needsDisplay = true } }
    var glowTint = CockpitTokens.Color.glowBlue { didSet { needsDisplay = true } }
    var doubleClickAction: (() -> Void)?

    override func mouseDown(with event: NSEvent) {
        if event.clickCount > 1, let doubleClickAction {
            doubleClickAction()
            return
        }
        super.mouseDown(with: event)
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard isGlowing, bounds.width > 18, bounds.height > 8 else { return }
        let rect = NSRect(x: 7, y: bounds.height - 5, width: bounds.width - 14, height: 3)
        let line = NSBezierPath(roundedRect: rect, xRadius: 1.5, yRadius: 1.5)
        NSGraphicsContext.saveGraphicsState()
        let shadow = NSShadow()
        shadow.shadowColor = glowTint.withAlphaComponent(0.92)
        shadow.shadowBlurRadius = 13
        shadow.shadowOffset = .zero
        shadow.set()
        glowTint.withAlphaComponent(0.96).setFill()
        line.fill()
        NSGraphicsContext.restoreGraphicsState()
    }
}

final class CockpitRootView: NSView, NSTextFieldDelegate, NSSearchFieldDelegate {
    weak var delegate: CockpitRootViewDelegate?

    fileprivate enum Surface: String {
        case cockpit = "/cockpit"
        case attention = "/attention"
        case comms = "/comms"
        case dependencies = "/dependencies"
        case map = "/map"
        case mesh = "/mesh"
        case atlas = "/ops"
        case usage = "/usage"

        var label: String {
            switch self {
            case .cockpit: return "Jobs"
            case .attention: return "Attention"
            case .comms: return "Comms"
            case .dependencies: return "Dependencies"
            case .map: return "Product Map"
            case .mesh: return "Mesh"
            case .atlas: return "Atlas"
            case .usage: return "Usage"
            }
        }

        /// The Jobs table and Usage are native; every other destination shares
        /// the bounded board projection inside the Cockpit window.
        var isEmbeddedWeb: Bool {
            ![.cockpit, .usage].contains(self)
        }

        var embedPath: String {
            switch self {
            case .cockpit, .usage: return ""
            case .attention: return "/?embedded=1#v=attention"
            case .comms: return "/?embedded=1#v=comms"
            case .dependencies: return "/cockpit?native_map=1&embedded=1&lens=deps"
            case .map: return "/cockpit?native_map=1&embedded=1"
            case .mesh: return "/mesh?embedded=1"
            case .atlas: return "/ops?embedded=1"
            }
        }
    }

    fileprivate enum RightPanelMode {
        case none
        case diagnostics
        case inspector
    }

    private let topBar = CockpitTopBarChromeView()
    private let toolbar = CockpitToolbarChromeView()
    private let tableFrame = CockpitBoardChromeView()
    private let tableController = CockpitTableController()
    private var mapView: CockpitMapWebView?
    private var usageView: NSHostingView<InstalledUsageDashboardView>?
    private let usageStore: InstalledUsageStore
    private let usageManagedExternally: Bool
    private let diagnostics = CockpitDiagnosticsView()
    private let inspector = CockpitInspectorView()
    private let statusLabel = CockpitUI.label("", size: 11, weight: .medium, color: CockpitTokens.Color.muted)


    private let wordmark = CockpitUI.label(
        "COORD", size: 20, weight: .light, color: CockpitTokens.Color.text
    )
    private let subtitleLabel = CockpitUI.label("Ops", size: 12, weight: .semibold, color: CockpitTokens.Color.muted)
    private let statsLabel = CockpitUI.label("", size: 12, weight: .semibold, color: CockpitTokens.Color.text, align: .right)
    private let modeControl = CockpitSegmentedControl(items: ["L", "M", "F"])
    private let scopeControl = CockpitSegmentedControl(items: CockpitScope.allCases.map(\.label))
    private let groupPopup = NSPopUpButton()
    private let sortPopup = NSPopUpButton()
    private let ownerPopup = NSPopUpButton()
    private let modulePopup = NSPopUpButton()
    private let statusPopup = NSPopUpButton()
    private let viewsPopup = NSPopUpButton()
    private let searchField = NSSearchField()
    private let chipLabel = CockpitUI.label("", size: 11, weight: .medium, color: CockpitTokens.Color.muted)
    private let sessionStrip = CockpitSessionStripView()
    private let actionStatus = CockpitUI.label("", size: 11, weight: .medium, color: CockpitTokens.Color.muted, align: .right)
    private let glass = NSVisualEffectView()
    private let columnsPopover = NSPopover()
    private let commandsPopover = NSPopover()
    private let filtersPopover = NSPopover()
    private let contextPalette = NativeContextPaletteView()
    private let contextBridge = NativeContextBridgeClient()
    private var surfaceButtons: [NSButton] = []
    private var navigationMenus: [String: NSMenu] = [:]
    private var toolbarButtons: [NSButton] = []
    private var sublineButton: NSButton?
    private var diagnosticsButton: NSButton?
    private var inspectorButton: NSButton?
    private var columnsButton: NSButton?
    private var quickFiltersButton: NSButton?
    private var commandsButton: NSButton?
    private var searchButton: NSButton?
    private var resumeAllButton: NSButton?
    private var pauseAllButton: NSButton?
    private let compactPrimaryToolbarButtons: Set<String> = [
        "Search", "Commands",
    ]
    private let widePrimaryToolbarButtons: Set<String> = [
        "Search", "Commands",
    ]

    private var model: CockpitPresentationModel?
    private var mapState: CockpitMapState?
    private var activeSurface: Surface = .cockpit
    private var rightPanelMode: RightPanelMode = .none
    private var selectedRowKey: String?
    private var pendingColumns: [CockpitColumn]?
    private var pendingCollapsedGroupKeys: Set<String>?
    private var pendingExpandedRowKeys: Set<String>?
    private var pendingShowSubline: Bool?
    private var searchExpanded = false
    private var quickFiltersPinned = false
    private var pendingSnapshot: CockpitViewSnapshot?
    private var lastActionResult: NativeCockpitActionResult?
    private var contextSearchTask: Task<Void, Never>?
    private var contextReadTask: Task<Void, Never>?
    private var isUpdatingSortPopup = false
    private var cockpitAnimationsEnabled = true
    private let savedViewStore = CockpitSavedViewStore()
    private let previewGroupNone = ProcessInfo.processInfo.environment["COORD_COCKPIT_PREVIEW_GROUP_NONE"] == "1"
    private let previewSkipTableRender = ProcessInfo.processInfo.environment["COORD_COCKPIT_PREVIEW_SKIP_TABLE_RENDER"] == "1"
    private let previewSkipFilterPopups = ProcessInfo.processInfo.environment["COORD_COCKPIT_PREVIEW_SKIP_FILTER_POPUPS"] == "1"
    private let previewSkipSavedViews = ProcessInfo.processInfo.environment["COORD_COCKPIT_PREVIEW_SKIP_SAVED_VIEWS"] == "1"
    private let previewSkipSidePanels = ProcessInfo.processInfo.environment["COORD_COCKPIT_PREVIEW_SKIP_SIDE_PANELS"] == "1"
    private let previewSkipSessionStrip = ProcessInfo.processInfo.environment["COORD_COCKPIT_PREVIEW_SKIP_SESSION_STRIP"] == "1"
    private let previewOwnerFilter = ProcessInfo.processInfo.environment["COORD_COCKPIT_PREVIEW_OWNER_FILTER"] ?? ""
    private let previewExpandFirstRow = ProcessInfo.processInfo.environment["COORD_COCKPIT_PREVIEW_EXPAND_FIRST_ROW"] == "1"
    private var previewFiltersApplied = false
    private var previewExpansionApplied = false

    private var currentToolbarHeight: CGFloat {
        showsLowerToolbarRow ? CockpitTokens.toolbarExpandedHeight : CockpitTokens.toolbarCompactHeight
    }

    var isMapSurfaceActive: Bool {
        activeSurface.isEmbeddedWeb
    }

    private var showsLowerToolbarRow: Bool {
        model?.activeChips.isEmpty == false
    }

    override convenience init(frame frameRect: NSRect) {
        self.init(frame: frameRect, usageStore: InstalledUsageStore(), usageManagedExternally: false)
    }

    init(
        frame frameRect: NSRect,
        usageStore: InstalledUsageStore,
        usageManagedExternally: Bool = false
    ) {
        self.usageStore = usageStore
        self.usageManagedExternally = usageManagedExternally
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.backgroundColor = CockpitTokens.Color.bg.cgColor

        glass.material = .underWindowBackground
        glass.blendingMode = .behindWindow
        glass.state = .active
        glass.alphaValue = 0.055
        addSubview(glass)

        addSubview(topBar)
        addSubview(toolbar)
        addSubview(tableFrame)
        addSubview(tableController.scrollView)
        addSubview(diagnostics)
        addSubview(inspector)
        addSubview(contextPalette)
        diagnostics.isHidden = true
        inspector.isHidden = true
        contextPalette.isHidden = true
        diagnostics.onClose = { [weak self] in self?.setRightPanel(.none) }
        inspector.onClose = { [weak self] in self?.setRightPanel(.none) }
        contextPalette.onSearchRequested = { [weak self] query, mode in
            self?.scheduleContextSearch(query, mode: mode)
        }
        contextPalette.onReadRequested = { [weak self] hit in
            self?.readContextHit(hit)
        }
        contextPalette.onOpenRequested = { [weak self] hit in
            self?.openContextHit(hit)
        }
        contextPalette.onClose = { [weak self] in
            self?.contextSearchTask?.cancel()
            self?.contextReadTask?.cancel()
            self?.window?.makeFirstResponder(self)
        }
        buildTopBar()
        buildToolbar()
        sessionStrip.onClearChip = { [weak self] chipID in
            self?.clearFilterChip(id: chipID)
        }
        wireTable()
    }

    required init?(coder: NSCoder) { nil }

    override var isFlipped: Bool { true }
    override var acceptsFirstResponder: Bool { true }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        window?.makeFirstResponder(self)
    }

    override func keyDown(with event: NSEvent) {
        guard let command = CockpitKeyboardCommandResolver.resolve(CockpitKeyboardShortcut(event: event)) else {
            super.keyDown(with: event)
            return
        }
        performKeyboardCommand(command)
    }

    override func layout() {
        super.layout()
        glass.frame = bounds
        let cockpitVisible = activeSurface == .cockpit
        let usageVisible = activeSurface == .usage
        let panelW: CGFloat = cockpitVisible && rightPanelMode != .none ? CockpitTokens.diagnosticsWidth : 0
        topBar.frame = NSRect(x: 0, y: 0, width: bounds.width, height: CockpitTokens.topbarHeight)
        let toolbarHeight = currentToolbarHeight
        let toolbarChromeHeight: CGFloat = showsLowerToolbarRow ? 80 : 44
        toolbar.isHidden = !cockpitVisible
        tableFrame.isHidden = !cockpitVisible
        tableController.scrollView.isHidden = !cockpitVisible
        tableController.setProgressAnimationsEnabled(cockpitAnimationsEnabled && cockpitVisible)
        if let mapView {
            mapView.isHidden = !activeSurface.isEmbeddedWeb
            mapView.frame = mapFrame
        }
        if let usageView {
            usageView.isHidden = !usageVisible
            usageView.frame = mapFrame
        }
        toolbar.frame = NSRect(
            x: 34,
            y: CockpitTokens.topbarHeight + 8,
            width: max(CGFloat(120), bounds.width - 52),
            height: toolbarChromeHeight
        )
        let tableFrameRect = NSRect(
            x: 34,
            y: CockpitTokens.topbarHeight + toolbarHeight,
            width: max(120, bounds.width - panelW - 52),
            height: max(120, bounds.height - CockpitTokens.topbarHeight - toolbarHeight - 16)
        )
        tableFrame.frame = tableFrameRect
        tableController.scrollView.frame = tableFrameRect.insetBy(dx: 1, dy: 1)
        let panelFrame = NSRect(
            x: bounds.width - panelW,
            y: CockpitTokens.topbarHeight,
            width: panelW,
            height: bounds.height - CockpitTokens.topbarHeight
        )
        diagnostics.frame = panelFrame
        inspector.frame = panelFrame
        contextPalette.frame = bounds
        tableController.updateHorizontalScroller()
        layoutTopBar()
        if cockpitVisible {
            layoutToolbar()
        }
    }

    func render(state: CockpitState) {
        var appliedPendingColumns = false
        if var current = model {
            current.replaceState(state)
            model = current
        } else {
            model = CockpitPresentationModel(state: state)
        }
        if let snapshot = pendingSnapshot {
            model?.applyView(snapshot)
            pendingSnapshot = nil
        }
        if let columns = pendingColumns {
            model?.applyColumnState(columns)
            pendingColumns = nil
            appliedPendingColumns = true
        }
        applyPendingOutlineState()
        if previewGroupNone {
            model?.groupMode = .none
        }
        applyPreviewFiltersIfNeeded()
        applyPreviewExpansionIfNeeded()
        synchronizeControls()
        redrawFromModel()
        if appliedPendingColumns {
            delegate?.cockpitRootViewDidChangeLocalState(self)
        }
    }

    func render(mapState: CockpitMapState) {
        self.mapState = mapState
        switch CockpitMapLifecycle.mapStateRefreshAction(isMapVisible: activeSurface == .map) {
        case .render:
            ensureMapView().render(mapState)
        case .storeOnly, .deactivate, .unloadNow:
            break
        }
    }

    func releaseMapResources() {
        if CockpitMapLifecycle.windowCloseAction() == .unloadNow {
            unloadMapResources()
        }
    }

    func currentUIState(frame: CockpitWindowFrame? = nil) -> CockpitWindowUIState {
        CockpitWindowUIState(
            frame: frame,
            rightPanel: panelState,
            selectedRowKey: selectedRowKey,
            quickFiltersPinned: quickFiltersPinned,
            viewSnapshot: model?.viewSnapshot ?? pendingSnapshot,
            columns: model?.columns ?? pendingColumns,
            collapsedGroupKeys: model?.collapsedGroupKeys.sorted() ?? pendingCollapsedGroupKeys?.sorted(),
            expandedRowKeys: model?.expandedRowKeys.sorted() ?? pendingExpandedRowKeys?.sorted(),
            showSubline: model?.showSubline ?? pendingShowSubline
        )
    }

    func applyUIState(_ state: CockpitWindowUIState) {
        quickFiltersPinned = state.quickFiltersPinned
        CockpitNativePreferences.quickFiltersPinned = state.quickFiltersPinned
        selectedRowKey = state.selectedRowKey?.trimmingCharacters(in: .whitespacesAndNewlines)
        if selectedRowKey?.isEmpty == true { selectedRowKey = nil }
        pendingSnapshot = state.viewSnapshot
        pendingColumns = state.columns
        pendingCollapsedGroupKeys = state.collapsedGroupKeys.map(Set.init)
        pendingExpandedRowKeys = state.expandedRowKeys.map(Set.init)
        pendingShowSubline = state.showSubline
        setRightPanel(RightPanelMode(state.rightPanel))
        if let snapshot = state.viewSnapshot, var model {
            model.applyView(snapshot)
            self.model = model
            pendingSnapshot = nil
        }
        if let columns = state.columns, var model {
            model.applyColumnState(columns)
            self.model = model
            pendingColumns = nil
        }
        applyPendingOutlineState()
        synchronizeControls()
        redrawFromModel()
    }

    func setActionStatus(_ text: String) {
        actionStatus.stringValue = text
    }

    func setCockpitAnimationsEnabled(_ enabled: Bool) {
        guard cockpitAnimationsEnabled != enabled else { return }
        cockpitAnimationsEnabled = enabled
        tableController.setProgressAnimationsEnabled(enabled && activeSurface == .cockpit)
    }

    func setActionResult(_ result: NativeCockpitActionResult) {
        lastActionResult = result
        actionStatus.stringValue = result.statusLine
        if rightPanelMode == .diagnostics, let model {
            diagnostics.render(state: model.state, lastActionResult: result)
        }
    }

    func setDiagnosticsOpen(_ isOpen: Bool) {
        setRightPanel(isOpen ? .diagnostics : .none)
    }

    func setInspectorOpen(_ isOpen: Bool) {
        if isOpen { selectFirstVisibleRowIfNeeded() }
        setRightPanel(isOpen ? .inspector : .none)
        redrawFromModel()
    }

    func expandFirstVisibleRow() {
        guard var model else { return }
        guard let row = model.items.compactMap(\.row).first else { return }
        selectedRowKey = row.dedupKey
        if !model.expandedRowKeys.contains(row.dedupKey) {
            model.toggleRow(dedupKey: row.dedupKey)
        }
        self.model = model
        tableController.selectRow(dedupKey: row.dedupKey)
        redrawFromModel()
    }

    func showColumnsPanel() {
        guard let columnsButton else { return }
        columnsPressed(columnsButton)
    }

    func showCommandsMenu() {
        guard let commandsButton else { return }
        commandsPressed(commandsButton)
    }

    func showFirstUnsafeActionConfirmation() {
        guard let model else { return }
        for row in model.items.compactMap(\.row) {
            if let action = row.actions.first(where: { $0.isEnabled && $0.requiresConfirmation }) {
                selectedRowKey = row.dedupKey
                tableController.selectRow(dedupKey: row.dedupKey)
                performRowAction(action, row: row)
                return
            }
        }
    }

    private func buildTopBar() {
        topBar.wantsLayer = true
        topBar.layer?.backgroundColor = NSColor.clear.cgColor
        wordmark.attributedStringValue = NSAttributedString(
            string: "COORD",
            attributes: [
                .font: CoordBrandFont.wordmark(size: 20),
                .foregroundColor: NSColor.white.withAlphaComponent(0.94),
                .kern: 5.4,
            ]
        )
        topBar.addSubview(wordmark)
        topBar.addSubview(subtitleLabel)
        subtitleLabel.isHidden = true
        topBar.addSubview(statsLabel)
        topBar.addSubview(modeControl)

        let navigation = [
            ("Jobs", "jobs"),
            ("Comms", "comms"),
            ("Atlas", "atlas"),
            ("More", "more"),
        ]
        for (title, identifier) in navigation {
            let button = CockpitUI.navButton(title, active: identifier == "jobs")
            button.identifier = NSUserInterfaceItemIdentifier(identifier)
            button.target = self
            button.action = #selector(navigationPressed(_:))
            topBar.addSubview(button)
            surfaceButtons.append(button)
        }
        navigationMenus["atlas"] = makeNavigationMenu([
            ("Explorer", "context:explorer"),
            ("Startup", "context:startup"),
            ("Operations", Surface.atlas.rawValue),
        ])
        navigationMenus["more"] = makeNavigationMenu([
            ("Attention", Surface.attention.rawValue),
            ("Usage", Surface.usage.rawValue),
            ("", "separator"),
            ("Comms", Surface.comms.rawValue),
            ("Dependencies", Surface.dependencies.rawValue),
            ("", "separator"),
            ("Explorer", "context:explorer"),
            ("Startup", "context:startup"),
            ("Operations", Surface.atlas.rawValue),
            ("", "separator"),
            ("Mesh", Surface.mesh.rawValue),
            ("Product Map", Surface.map.rawValue),
        ])

        modeControl.onSelection = { [weak self] _ in self?.modeChanged() }
    }

    private func makeNavigationMenu(_ entries: [(String, String)]) -> NSMenu {
        let menu = NSMenu()
        menu.autoenablesItems = false
        for (title, destination) in entries {
            if destination == "separator" {
                menu.addItem(.separator())
                continue
            }
            let item = NSMenuItem(title: title, action: #selector(navigationMenuItemPressed(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = destination
            item.isEnabled = true
            menu.addItem(item)
        }
        return menu
    }

    @objc private func navigationPressed(_ sender: NSButton) {
        guard let identifier = sender.identifier?.rawValue else { return }
        if identifier == "jobs" || identifier == "comms" {
            openSurface(path: identifier == "jobs" ? Surface.cockpit.rawValue : Surface.comms.rawValue)
            return
        }
        guard let menu = navigationMenus[identifier] else { return }
        menu.popUp(positioning: nil, at: NSPoint(x: 0, y: sender.bounds.maxY + 3), in: sender)
    }

    @objc private func navigationMenuItemPressed(_ sender: NSMenuItem) {
        guard let destination = sender.representedObject as? String else { return }
        switch destination {
        case "context:explorer":
            showContextPalette()
        case "context:startup":
            showContextPalette(initialQuery: "startup orientation")
        default:
            openSurface(path: destination)
        }
    }

    private func buildToolbar() {
        toolbar.wantsLayer = true
        tableFrame.wantsLayer = true
        tableFrame.layer?.masksToBounds = true
        tableController.scrollView.wantsLayer = true
        tableController.scrollView.layer?.cornerRadius = 12
        tableController.scrollView.layer?.masksToBounds = true

        scopeControl.onSelection = { [weak self] _ in self?.scopeChanged() }
        toolbar.addSubview(scopeControl)

        configurePopup(groupPopup, items: CockpitGroupMode.allCases.map { ($0.label, $0.rawValue) }, action: #selector(groupChanged))
        rebuildSortPopup(for: nil)
        configurePopup(ownerPopup, items: [("Owner", "")], action: #selector(ownerChanged))
        configurePopup(modulePopup, items: [("Module", "")], action: #selector(moduleChanged))
        configurePopup(statusPopup, items: [("Status", "")], action: #selector(statusChanged))
        configurePopup(viewsPopup, items: [("View", "")], action: #selector(viewChanged))
        toolbar.addSubview(groupPopup)
        toolbar.addSubview(sortPopup)
        toolbar.addSubview(ownerPopup)
        toolbar.addSubview(modulePopup)
        toolbar.addSubview(statusPopup)
        toolbar.addSubview(viewsPopup)

        searchField.placeholderString = "Search"
        searchField.target = self
        searchField.action = #selector(searchChanged)
        searchField.delegate = self
        toolbar.addSubview(searchField)

        let toolbarCommands: [(String, String, Selector)] = [
            ("Refresh", "arrow.clockwise", #selector(refreshPressed)),
            ("Search", "magnifyingglass", #selector(searchPressed)),
            ("Inspector", "sidebar.right", #selector(inspectorPressed)),
            ("Diagnostics", "waveform.path.ecg", #selector(diagnosticsPressed)),
            ("Expand", "arrow.down.right.and.arrow.up.left", #selector(expandPressed)),
            ("Collapse", "arrow.up.left.and.arrow.down.right", #selector(collapsePressed)),
            ("Columns", "rectangle.split.3x3", #selector(columnsPressed(_:))),
            ("Commands", "ellipsis.circle", #selector(commandsPressed(_:))),
            ("Subline", "text.alignleft", #selector(sublinePressed)),
            ("View / filters", "line.3.horizontal.decrease.circle", #selector(quickFiltersPressed(_:))),
            ("Save view", "bookmark", #selector(saveViewPressed)),
            ("Reset", "arrow.counterclockwise", #selector(resetPressed)),
            ("Resume all", "play.circle", #selector(resumeAllPressed)),
            ("Pause all", "pause.circle", #selector(pauseAllPressed)),
        ]
        for (title, symbol, action) in toolbarCommands {
            let button = CockpitGlowButton(title: title, target: nil, action: nil)
            if let image = NSImage(systemSymbolName: symbol, accessibilityDescription: title) {
                button.title = ""
                button.image = image
                button.imagePosition = .imageOnly
            } else {
                button.title = String(title.prefix(1))
            }
            button.identifier = NSUserInterfaceItemIdentifier(title)
            button.toolTip = title
            button.target = self
            button.action = action
            applyQuietButtonChrome(button)
            if title == "Pause all" {
                button.contentTintColor = CockpitTokens.Color.amber
                applyQuietButtonChrome(button, active: true, tint: CockpitTokens.Color.amber)
            } else if title == "Resume all" {
                button.contentTintColor = CockpitTokens.Color.green
                applyQuietButtonChrome(button, active: true, tint: CockpitTokens.Color.green)
            }
            if title == "Subline" {
                sublineButton = button
            } else if title == "Diagnostics" {
                diagnosticsButton = button
            } else if title == "Inspector" {
                inspectorButton = button
            } else if title == "Columns" {
                columnsButton = button
            } else if title == "View / filters" {
                quickFiltersButton = button
                button.doubleClickAction = { [weak self] in self?.toggleInlineFilters() }
            } else if title == "Commands" {
                commandsButton = button
            } else if title == "Search" {
                searchButton = button
            } else if title == "Resume all" {
                resumeAllButton = button
            } else if title == "Pause all" {
                pauseAllButton = button
            }
            if title == "Resume all" || title == "Pause all" {
                topBar.addSubview(button)
            } else {
                toolbar.addSubview(button)
                toolbarButtons.append(button)
            }
        }
        toolbar.addSubview(chipLabel)
        toolbar.addSubview(sessionStrip)
        toolbar.addSubview(statusLabel)
        toolbar.addSubview(actionStatus)
    }

    private func configurePopup(_ popup: NSPopUpButton, items: [(String, String)], action: Selector) {
        popup.removeAllItems()
        for item in items {
            popup.addItem(withTitle: item.0)
            popup.lastItem?.representedObject = item.1
        }
        popup.target = self
        popup.action = action
        popup.controlSize = .regular
        popup.font = .systemFont(ofSize: 12, weight: .medium)
        popup.isBordered = false
        popup.wantsLayer = true
        popup.layer?.backgroundColor = CockpitTokens.Color.panel2.withAlphaComponent(0.34).cgColor
        popup.layer?.borderColor = NSColor.white.withAlphaComponent(0.024).cgColor
        popup.layer?.borderWidth = 0.5
        popup.layer?.cornerRadius = 10
        popup.layer?.shadowColor = NSColor.black.cgColor
        popup.layer?.shadowOpacity = 0.18
        popup.layer?.shadowRadius = 12
        popup.layer?.shadowOffset = CGSize(width: 0, height: -5)
    }

    private func rebuildSortPopup(for model: CockpitPresentationModel?) {
        isUpdatingSortPopup = true
        defer { isUpdatingSortPopup = false }
        let selectedMode = model?.sortMode
        let direction = model?.sortDirection ?? CockpitSortMode.smart.defaultDirection
        let items = CockpitSortMode.pickerCases.map { mode in
            let title = mode == selectedMode ? mode.popupLabel(direction: direction) : mode.label
            return (title, mode.rawValue)
        }
        configurePopup(sortPopup, items: items, action: #selector(sortChanged))
        if let selectedMode,
           let item = sortPopup.itemArray.first(where: { ($0.representedObject as? String) == selectedMode.rawValue }) {
            sortPopup.select(item)
            sortPopup.toolTip = selectedMode == .smart
                ? "Sort: \(selectedMode.label)"
                : "Sort: \(selectedMode.label), \(direction.shortLabel)"
        } else {
            sortPopup.selectItem(at: 0)
            sortPopup.toolTip = "Sort"
        }
    }

    private func wireTable() {
        tableController.onToggleGroup = { [weak self] key in
            guard var model = self?.model else { return }
            model.toggleGroup(key: key)
            self?.model = model
            self?.redrawFromModel()
            if let self {
                self.delegate?.cockpitRootViewDidChangeLocalState(self)
            }
        }
        tableController.onToggleRow = { [weak self] key in
            guard var model = self?.model else { return }
            model.toggleRow(dedupKey: key)
            self?.model = model
            self?.redrawFromModel()
            if let self {
                self.delegate?.cockpitRootViewDidChangeLocalState(self)
            }
        }
        tableController.onAction = { [weak self] action, row in
            guard let self else { return }
            self.performRowAction(action, row: row)
        }
        tableController.onColumnResize = { [weak self] id, width in
            guard var model = self?.model else { return }
            model.resizeColumn(id: id, width: Int(width))
            self?.model = model
            if let self {
                self.delegate?.cockpitRootViewDidChangeLocalState(self)
            }
        }
        tableController.onColumnMove = { [weak self] moved, before in
            guard var model = self?.model else { return }
            model.moveColumn(id: moved, before: before)
            self?.model = model
        }
        tableController.onColumnOrderChange = { [weak self] orderedIDs in
            guard var model = self?.model else { return }
            model.reorderVisibleColumns(orderedIDs)
            self?.model = model
            if let self {
                self.delegate?.cockpitRootViewDidChangeLocalState(self)
            }
        }
        tableController.onHeaderSort = { [weak self] columnID in
            guard var model = self?.model else { return }
            model.setSortModeFromHeader(columnID: columnID)
            self?.model = model
            self?.synchronizeControls()
            self?.redrawFromModel()
            if let self {
                self.delegate?.cockpitRootViewDidChangeLocalState(self)
            }
        }
        tableController.onSelection = { [weak self] row in
            guard let self else { return }
            let nextKey = row?.dedupKey
            guard nextKey != self.selectedRowKey else { return }
            self.selectedRowKey = nextKey
            if self.rightPanelMode == .inspector {
                self.redrawFromModel()
            }
            self.delegate?.cockpitRootViewDidChangeLocalState(self)
        }
    }

    private func layoutTopBar() {


        wordmark.frame = NSRect(x: 72, y: 16, width: 136, height: 30)
        subtitleLabel.frame = .zero
        var navWidths: [CGFloat] = []
        for button in surfaceButtons {
            let width = min(102, button.intrinsicContentSize.width + 18)
            navWidths.append(width)
        }
        let layout = CockpitTopBarLayout.compute(
            width: bounds.width,
            surfaceWidths: navWidths,
            showsResume: resumeAllButton?.isHidden == false && resumeAllButton?.isEnabled == true,
            showsPause: pauseAllButton?.isHidden == false && pauseAllButton?.isEnabled == true
        )
        if let pause = layout.pause {
            pauseAllButton?.frame = NSRect(x: pause.minX, y: pause.minY, width: pause.width, height: pause.height)
        }
        if let resume = layout.resume {
            resumeAllButton?.frame = NSRect(x: resume.minX, y: resume.minY, width: resume.width, height: resume.height)
        }
        statsLabel.frame = NSRect(x: layout.stats.minX, y: layout.stats.minY, width: layout.stats.width, height: layout.stats.height)
        modeControl.frame = NSRect(x: layout.mode.minX, y: layout.mode.minY, width: layout.mode.width, height: layout.mode.height)
        for button in surfaceButtons { button.isHidden = true }
        for (offset, buttonIndex) in layout.surfaceIndices.enumerated() {
            guard surfaceButtons.indices.contains(buttonIndex), layout.surfaces.indices.contains(offset) else { continue }
            let button = surfaceButtons[buttonIndex]
            let frame = layout.surfaces[offset]
            button.isHidden = false
            button.frame = NSRect(x: frame.minX, y: frame.minY, width: frame.width, height: frame.height)
        }
    }

    private func layoutToolbar() {
        let showLowerRow = showsLowerToolbarRow
        let controlHeight: CGFloat = 36
        let y1: CGFloat = showLowerRow ? 8 : 4
        let toolbarWidth = toolbar.bounds.width
        let compact = bounds.width < 1180
        let scopeWidth: CGFloat = compact ? 260 : 284
        let groupWidth: CGFloat = compact ? 108 : 124
        let sortWidth: CGFloat = compact ? 96 : 116
        let buttonGap: CGFloat = compact ? 4 : 7
        var leadingX: CGFloat = 9
        scopeControl.frame = NSRect(x: leadingX, y: y1, width: scopeWidth, height: controlHeight)
        leadingX += scopeWidth + (compact ? 8 : 14)
        groupPopup.frame = NSRect(x: leadingX, y: y1, width: groupWidth, height: controlHeight)
        leadingX += groupWidth + (compact ? 6 : 8)
        sortPopup.frame = NSRect(x: leadingX, y: y1, width: sortWidth, height: controlHeight)
        leadingX += sortWidth + (compact ? 8 : 12)
        let filterButtonWidth: CGFloat = compact ? 32 : 36
        quickFiltersButton?.isHidden = false
        quickFiltersButton?.frame = NSRect(x: leadingX, y: y1, width: filterButtonWidth, height: controlHeight)
        leadingX += filterButtonWidth + (compact ? 6 : 8)
        let inlinePopupWidth: CGFloat = compact ? 90 : 104
        if quickFiltersPinned {
            ownerPopup.isHidden = false
            modulePopup.isHidden = false
            statusPopup.isHidden = false
            viewsPopup.isHidden = false
            ownerPopup.frame = NSRect(x: leadingX, y: y1, width: inlinePopupWidth, height: controlHeight)
            leadingX += inlinePopupWidth + (compact ? 6 : 8)
            modulePopup.frame = NSRect(x: leadingX, y: y1, width: inlinePopupWidth, height: controlHeight)
            leadingX += inlinePopupWidth + (compact ? 6 : 8)
            statusPopup.frame = NSRect(x: leadingX, y: y1, width: inlinePopupWidth, height: controlHeight)
            leadingX += inlinePopupWidth + (compact ? 6 : 8)
            viewsPopup.frame = NSRect(x: leadingX, y: y1, width: inlinePopupWidth, height: controlHeight)
            leadingX += inlinePopupWidth + (compact ? 8 : 12)
        } else {
            ownerPopup.isHidden = !quickFiltersPinned
            modulePopup.isHidden = !quickFiltersPinned
            statusPopup.isHidden = !quickFiltersPinned
            viewsPopup.isHidden = !quickFiltersPinned
        }
        let primaryToolbarButtons = compact ? compactPrimaryToolbarButtons : widePrimaryToolbarButtons
        let visibleButtons = toolbarButtons.filter { primaryToolbarButtons.contains(toolbarButtonID($0)) }
        let buttonClusterWidth = visibleButtons.reduce(CGFloat(0)) { $0 + toolbarButtonWidth($1, compact: compact) }
            + CGFloat(max(0, visibleButtons.count - 1)) * buttonGap
        let buttonStart = max(leadingX + (compact ? 104 : 148) + 10, toolbarWidth - 9 - buttonClusterWidth)
        let searchOpen = searchExpanded || !searchField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        let reservedSearchWidth: CGFloat = searchOpen ? (compact ? 156 : 220) : 0
        let availableSearch = max(compact ? 104 : 148, buttonStart - 10 - leadingX)
        let searchWidth = min(availableSearch, reservedSearchWidth)
        searchField.isHidden = !searchOpen
        searchField.frame = searchOpen
            ? NSRect(x: leadingX, y: y1, width: searchWidth, height: controlHeight)
            : NSRect(x: leadingX, y: y1, width: 0, height: controlHeight)
        if searchOpen {
            leadingX += searchWidth + 10
        }
        var x: CGFloat = buttonStart
        for button in toolbarButtons {
            if let quickFiltersButton, button === quickFiltersButton {
                continue
            }
            guard primaryToolbarButtons.contains(toolbarButtonID(button)) else {
                button.isHidden = true
                continue
            }
            let width = toolbarButtonWidth(button, compact: compact)
            button.isHidden = false
            button.frame = NSRect(x: x, y: y1, width: width, height: controlHeight)
            x += width + buttonGap
        }

        let statusX = max(620, toolbarWidth - 360)
        let stripX: CGFloat = 9
        sessionStrip.frame = NSRect(x: stripX, y: 48, width: max(32, statusX - stripX - 12), height: 28)
        chipLabel.frame = NSRect(x: stripX, y: 48, width: max(32, statusX - stripX - 12), height: 28)
        chipLabel.isHidden = true
        sessionStrip.isHidden = !showLowerRow
        statusLabel.frame = NSRect(x: statusX, y: 48, width: 160, height: 28)
        actionStatus.frame = NSRect(x: toolbarWidth - 190, y: 48, width: 170, height: 28)
        statusLabel.isHidden = !showLowerRow
        actionStatus.isHidden = !showLowerRow
    }

    private func toolbarButtonWidth(_ button: NSButton, compact: Bool) -> CGFloat {
        if !button.title.isEmpty {
            return min(compact ? 92 : 112, max(compact ? 66 : 72, button.intrinsicContentSize.width + 16))
        }
        return compact ? 28 : 32
    }

    private func toolbarButtonID(_ button: NSButton) -> String {
        button.identifier?.rawValue ?? button.toolTip ?? ""
    }

    private func synchronizeControls() {
        guard let model else { return }
        scopeControl.selectedIndex = CockpitScope.allCases.firstIndex(of: model.scope) ?? 0
        groupPopup.selectItem(withTitle: model.groupMode.label)
        rebuildSortPopup(for: model)
        searchField.stringValue = model.query
        selectMode(model.state.mode ?? model.state.liveMode)
        if !previewSkipFilterPopups {
            updateFilterPopups(model.state)
        }
        if !previewSkipSavedViews {
            updateSavedViewsPopup()
        }
    }

    private func redrawFromModel() {
        guard let model else { return }
        if let selectedRowKey, model.state.row(dedupKey: selectedRowKey) == nil {
            self.selectedRowKey = nil
        }
        if rightPanelMode == .inspector {
            selectFirstVisibleRowIfNeeded()
        }
        if !previewSkipTableRender {
            tableController.render(model)
        }
        if let selectedRowKey, !previewSkipTableRender {
            tableController.selectRow(dedupKey: selectedRowKey, scrollIfNeeded: false)
        }
        if !previewSkipSidePanels {
            renderVisibleSidePanel()
        }
        let s = model.state.summary
        statsLabel.attributedStringValue = statsString(s)
        statusLabel.stringValue = "seq \(model.state.writerSeq)  rows \(model.state.rows.count)"
        chipLabel.stringValue = model.activeChips.map(\.label).joined(separator: "   ")
        if !previewSkipSessionStrip {
            sessionStrip.configure(sessions: model.state.sessions, chips: model.activeChips)
        }
        updateSublineButton(model.showSubline)
        updateQuickFiltersButton()
        updatePanelButtons()
        updateBulkButtons(model)
        updateSurfaceButtons()
        needsLayout = true
    }

    private func selectedRow(in model: CockpitPresentationModel) -> CockpitRow? {
        guard let selectedRowKey else { return nil }
        return model.state.row(dedupKey: selectedRowKey)
    }

    private func renderVisibleSidePanel() {
        guard let model else { return }
        switch rightPanelMode {
        case .diagnostics:
            diagnostics.render(state: model.state, lastActionResult: lastActionResult)
        case .inspector:
            inspector.render(row: selectedRow(in: model), state: model.state)
        case .none:
            break
        }
    }

    private func applyPreviewFiltersIfNeeded() {
        guard !previewFiltersApplied else { return }
        let owner = previewOwnerFilter.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !owner.isEmpty, var model else { return }
        model.owner = owner
        self.model = model
        previewFiltersApplied = true
    }

    private func applyPreviewExpansionIfNeeded() {
        guard previewExpandFirstRow, !previewExpansionApplied, var model else { return }
        if let row = model.items.compactMap(\.row).first {
            model.expandedRowKeys.insert(row.dedupKey)
            self.model = model
        }
        previewExpansionApplied = true
    }

    private func applyPendingOutlineState() {
        guard var model else { return }
        var changed = false
        if let collapsed = pendingCollapsedGroupKeys {
            model.collapsedGroupKeys = collapsed
            pendingCollapsedGroupKeys = nil
            changed = true
        }
        if let expanded = pendingExpandedRowKeys {
            model.expandedRowKeys = expanded
            pendingExpandedRowKeys = nil
            changed = true
        }
        if let showSubline = pendingShowSubline {
            model.showSubline = showSubline
            pendingShowSubline = nil
            changed = true
        }
        if changed {
            self.model = model
        }
    }

    private func statsString(_ summary: CockpitSummary) -> NSAttributedString {
        let out = NSMutableAttributedString()
        let labels: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 11.5, weight: .semibold),
            .foregroundColor: CockpitTokens.Color.muted,
        ]
        func append(_ label: String, value: Int, color: NSColor) {
            if out.length > 0 {
                out.append(NSAttributedString(string: "  ", attributes: labels))
            }
            out.append(NSAttributedString(string: "\(value)", attributes: [
                .font: NSFont.monospacedDigitSystemFont(ofSize: 11.5, weight: .bold),
                .foregroundColor: color,
            ]))
            out.append(NSAttributedString(string: " \(label)", attributes: labels))
        }
        append("running", value: summary.running, color: CockpitTokens.Color.green)
        append("blocked", value: summary.attention, color: CockpitTokens.Color.red)
        append("next", value: summary.next, color: CockpitTokens.Color.blue2)
        if summary.local > 0 {
            append("local", value: summary.local, color: CockpitTokens.Color.text.withAlphaComponent(0.92))
        }
        return out
    }

    private func selectFirstVisibleRowIfNeeded() {
        guard selectedRowKey == nil, let model else { return }
        guard let row = model.items.compactMap(\.row).first else { return }
        selectedRowKey = row.dedupKey
        tableController.selectRow(dedupKey: row.dedupKey)
    }

    private func updateFilterPopups(_ state: CockpitState) {
        let owner = ownerPopup.selectedItem?.representedObject as? String ?? ""
        let module = modulePopup.selectedItem?.representedObject as? String ?? ""
        let status = statusPopup.selectedItem?.representedObject as? String ?? ""
        setFilterPopup(ownerPopup, title: "Owner", kind: .owner, selected: owner, state: state)
        setFilterPopup(modulePopup, title: "Module", kind: .module, selected: module, state: state)
        setFilterPopup(statusPopup, title: "Status", kind: .status, selected: status, state: state)
    }

    private func setFilterPopup(_ popup: NSPopUpButton, title: String, kind: CockpitFilterKind, selected: String, state: CockpitState) {
        popup.removeAllItems()
        popup.addItem(withTitle: title)
        popup.lastItem?.representedObject = ""
        for option in state.filterOptions.filter({ $0.kind == kind }).prefix(30) {
            popup.addItem(withTitle: "\(option.label) (\(option.count))")
            popup.lastItem?.representedObject = option.value
        }
        if let item = popup.itemArray.first(where: { ($0.representedObject as? String) == selected }) {
            popup.select(item)
        }
    }

    private func updateSavedViewsPopup() {
        guard let model else { return }
        let selected = matchingSavedViewName(for: model.viewSnapshot) ?? ""
        viewsPopup.removeAllItems()
        viewsPopup.addItem(withTitle: "View")
        viewsPopup.lastItem?.representedObject = ""
        for view in savedViewStore.allViews() {
            viewsPopup.addItem(withTitle: "View: \(view.name)")
            viewsPopup.lastItem?.representedObject = view.name
        }
        if !selected.isEmpty, let item = viewsPopup.itemArray.first(where: { ($0.representedObject as? String) == selected }) {
            viewsPopup.select(item)
        } else {
            viewsPopup.selectItem(at: 0)
        }
    }

    private func matchingSavedViewName(for snapshot: CockpitViewSnapshot) -> String? {
        savedViewStore.allViews().first { $0.snapshot == snapshot }?.name
            ?? savedViewStore.allViews().first { $0.snapshot.matchesControls(of: snapshot) }?.name
    }

    private func selectMode(_ mode: String?) {
        switch (mode ?? "").lowercased() {
        case "light": modeControl.selectedIndex = 0
        case "medium": modeControl.selectedIndex = 1
        case "full": modeControl.selectedIndex = 2
        default: modeControl.selectedIndex = -1
        }
    }

    @objc private func scopeChanged() {
        guard var model, scopeControl.selectedIndex >= 0 else { return }
        model.scope = CockpitScope.allCases[scopeControl.selectedIndex]
        self.model = model
        redrawFromModel()
        delegate?.cockpitRootViewDidChangeLocalState(self)
    }

    @objc private func groupChanged() {
        guard var model, let raw = groupPopup.selectedItem?.representedObject as? String else { return }
        model.groupMode = CockpitGroupMode(rawValue: raw) ?? .smart
        self.model = model
        redrawFromModel()
        delegate?.cockpitRootViewDidChangeLocalState(self)
    }

    @objc private func sortChanged() {
        guard !isUpdatingSortPopup else { return }
        guard var model, let raw = sortPopup.selectedItem?.representedObject as? String else { return }
        model.setSortMode(CockpitSortMode(rawValue: raw) ?? .smart)
        self.model = model
        synchronizeControls()
        redrawFromModel()
        delegate?.cockpitRootViewDidChangeLocalState(self)
    }

    private func performKeyboardCommand(_ command: CockpitKeyboardCommand) {
        switch command {
        case .refresh:
            refreshPressed()
        case .focusSearch:
            searchExpanded = true
            needsLayout = true
            window?.makeFirstResponder(searchField)
            searchField.currentEditor()?.selectAll(nil)
        case .toggleInspector:
            inspectorPressed()
        case .toggleDiagnostics:
            diagnosticsPressed()
        case .openCommands:
            showCommandsMenu()
        case .expandAll:
            expandPressed()
        case .collapseAll:
            collapsePressed()
        case .reverseSort:
            reverseSortPressed()
        case .clearSearchOrClosePanel:
            clearSearchOrClosePanel()
        }
    }

    private func reverseSortPressed() {
        guard var model, model.sortMode != .smart else { return }
        model.sortDirection.toggle()
        self.model = model
        synchronizeControls()
        redrawFromModel()
        delegate?.cockpitRootViewDidChangeLocalState(self)
    }

    @objc private func reverseSortMenuPressed() {
        reverseSortPressed()
    }

    private func clearSearchOrClosePanel() {
        if contextPalette.isOpen {
            contextPalette.hide()
            return
        }
        if !searchField.stringValue.isEmpty {
            searchField.stringValue = ""
            searchChanged()
            return
        }
        if searchExpanded {
            searchExpanded = false
            needsLayout = true
            window?.makeFirstResponder(self)
            return
        }
        if rightPanelMode != .none {
            setRightPanel(.none)
            return
        }
        window?.makeFirstResponder(self)
    }

    @objc private func ownerChanged() { applyFilter(ownerPopup, keyPath: \.owner) }
    @objc private func moduleChanged() { applyFilter(modulePopup, keyPath: \.module) }
    @objc private func statusChanged() { applyFilter(statusPopup, keyPath: \.status) }

    @objc private func viewChanged() {
        guard var model, let name = viewsPopup.selectedItem?.representedObject as? String, !name.isEmpty else { return }
        guard let view = savedViewStore.view(named: name) else { return }
        model.applyView(view.snapshot)
        self.model = model
        synchronizeControls()
        redrawFromModel()
        delegate?.cockpitRootViewDidChangeLocalState(self)
    }

    private func applyFilter(_ popup: NSPopUpButton, keyPath: WritableKeyPath<CockpitPresentationModel, String>) {
        guard var model else { return }
        model[keyPath: keyPath] = popup.selectedItem?.representedObject as? String ?? ""
        self.model = model
        redrawFromModel()
        delegate?.cockpitRootViewDidChangeLocalState(self)
    }

    private func applyFilterValue(kind: CockpitFilterKind, value: String) {
        guard var model else { return }
        switch kind {
        case .owner:
            model.owner = value
        case .module:
            model.module = value
        case .status:
            model.status = value
        default:
            return
        }
        self.model = model
        synchronizeControls()
        redrawFromModel()
        delegate?.cockpitRootViewDidChangeLocalState(self)
    }

    @objc private func searchChanged() {
        guard var model else { return }
        model.query = searchField.stringValue
        self.model = model
        redrawFromModel()
        delegate?.cockpitRootViewDidChangeLocalState(self)
    }

    private func clearFilterChip(id: String) {
        guard var model else { return }
        model.clearFilterChip(id: id)
        self.model = model
        synchronizeControls()
        redrawFromModel()
        delegate?.cockpitRootViewDidChangeLocalState(self)
    }

    func controlTextDidChange(_ obj: Notification) {
        searchChanged()
    }

    @objc private func modeChanged() {
        let modes = ["light", "medium", "full"]
        guard modeControl.selectedIndex >= 0 else { return }
        delegate?.cockpitRootView(self, perform: "mode.set", row: nil, payload: ["mode": modes[modeControl.selectedIndex]])
    }

    @objc private func refreshPressed() { delegate?.cockpitRootViewDidRequestRefresh(self) }

    @objc private func searchPressed() {
        searchExpanded.toggle()
        if searchExpanded {
            needsLayout = true
            window?.makeFirstResponder(searchField)
            searchField.currentEditor()?.selectAll(nil)
        } else if searchField.stringValue.isEmpty {
            window?.makeFirstResponder(self)
            needsLayout = true
        } else {
            searchField.stringValue = ""
            searchChanged()
        }
    }

    @objc private func inspectorPressed() {
        if rightPanelMode == .inspector {
            setRightPanel(.none)
            return
        }
        selectFirstVisibleRowIfNeeded()
        setRightPanel(.inspector)
    }

    @objc private func diagnosticsPressed() {
        setRightPanel(rightPanelMode == .diagnostics ? .none : .diagnostics)
    }

    private func setRightPanel(_ mode: RightPanelMode) {
        rightPanelMode = mode
        diagnostics.isHidden = mode != .diagnostics
        inspector.isHidden = mode != .inspector
        if !previewSkipSidePanels {
            renderVisibleSidePanel()
        }
        updatePanelButtons()
        needsLayout = true
        delegate?.cockpitRootViewDidChangeLocalState(self)
    }

    private func updatePanelButtons() {
        updateToggleButton(diagnosticsButton, enabled: rightPanelMode == .diagnostics)
        updateToggleButton(inspectorButton, enabled: rightPanelMode == .inspector)
    }

    private func updateToggleButton(_ button: NSButton?, enabled: Bool) {
        button?.contentTintColor = enabled ? CockpitTokens.Color.text : CockpitTokens.Color.muted
        applyQuietButtonChrome(button, active: enabled)
        (button as? CockpitGlowButton)?.isGlowing = enabled
    }

    @objc private func columnsPressed(_ sender: NSButton) {
        guard let model else { return }
        let panel = CockpitColumnsPanel(
            columns: model.columns,
            onToggle: { [weak self] id, visible in
                guard var model = self?.model else { return }
                model.setColumnVisible(id: id, visible: visible)
                self?.model = model
                self?.redrawFromModel()
                if let self {
                    self.delegate?.cockpitRootViewDidChangeLocalState(self)
                }
            },
            onShowAll: { [weak self] in
                guard var model = self?.model else { return }
                model.showAllColumns()
                self?.model = model
                self?.redrawFromModel()
                if let self {
                    self.delegate?.cockpitRootViewDidChangeLocalState(self)
                }
            },
            onDefaults: { [weak self] in
                guard var model = self?.model else { return }
                model.restoreDefaultColumnVisibility()
                self?.model = model
                self?.redrawFromModel()
                if let self {
                    self.delegate?.cockpitRootViewDidChangeLocalState(self)
                }
            },
            onFit: { [weak self] in
                guard var model = self?.model else { return }
                model.resetColumnWidthsToDefaults()
                self?.model = model
                self?.redrawFromModel()
                if let self {
                    self.delegate?.cockpitRootViewDidChangeLocalState(self)
                }
            },
            onReset: { [weak self] in
                guard var model = self?.model else { return }
                model.resetColumns()
                self?.model = model
                self?.redrawFromModel()
                if let self {
                    self.delegate?.cockpitRootViewDidChangeLocalState(self)
                }
            },
            onWidth: { [weak self] id, width in
                guard var model = self?.model else { return }
                model.resizeColumn(id: id, width: width)
                self?.model = model
                self?.redrawFromModel()
                if let self {
                    self.delegate?.cockpitRootViewDidChangeLocalState(self)
                }
            },
            onMove: { [weak self] id, delta in
                guard var model = self?.model else { return }
                if delta < 0 {
                    model.moveColumnUp(id: id)
                } else {
                    model.moveColumnDown(id: id)
                }
                self?.model = model
                self?.redrawFromModel()
                if let self {
                    self.delegate?.cockpitRootViewDidChangeLocalState(self)
                }
            }
        )
        let controller = NSViewController()
        controller.view = panel
        columnsPopover.contentViewController = controller
        columnsPopover.behavior = .transient
        columnsPopover.animates = true
        columnsPopover.show(relativeTo: sender.bounds, of: sender, preferredEdge: .maxY)
    }

    @objc private func sublinePressed() {
        guard var model else { return }
        model.toggleSubline()
        self.model = model
        redrawFromModel()
        delegate?.cockpitRootViewDidChangeLocalState(self)
    }

    @objc private func commandsPressed(_ sender: NSButton) {
        let row = selectedActionRow()
        let claude = rowActionState(row, id: "task.assign.claude")
        let codex = rowActionState(row, id: "task.assign.codex")
        let unassign = rowActionState(row, id: "task.unassign")
        let claim = rowActionState(row, id: "claim.create")
        let handoff = rowActionState(row, id: "handoff.create")
        let capability = rowActionState(row, id: "capability.run")
        let canResume = model?.visibleJobControlIDs(actionID: "jobs.resume").isEmpty == false
        let canPause = model?.visibleJobControlIDs(actionID: "jobs.pause").isEmpty == false
        let rowItems = [
            CockpitCommandPanelItem(id: "row.assignClaude", title: "Claude", symbol: nil, enabled: claude.enabled, tint: NSColor(calibratedRed: 1.00, green: 0.36, blue: 0.20, alpha: 1), toolTip: claude.toolTip),
            CockpitCommandPanelItem(id: "row.assignCodex", title: "Codex", symbol: nil, enabled: codex.enabled, tint: CockpitTokens.Color.blue2, toolTip: codex.toolTip),
            CockpitCommandPanelItem(id: "row.unassign", title: "Unassign", symbol: "person.crop.circle.badge.xmark", enabled: unassign.enabled, toolTip: unassign.toolTip),
            CockpitCommandPanelItem(id: "row.claim", title: "Claim", symbol: "checkmark.seal", enabled: claim.enabled, toolTip: claim.toolTip),
            CockpitCommandPanelItem(id: "row.handoff", title: "Handoff", symbol: "arrowshape.turn.up.right", enabled: handoff.enabled, toolTip: handoff.toolTip),
            CockpitCommandPanelItem(id: "row.capability", title: "Run", symbol: "bolt", enabled: capability.enabled, toolTip: capability.toolTip),
        ]
        let enabledRowItems = rowItems.filter(\.enabled)
        var sections: [CockpitCommandPanelSection] = [
            CockpitCommandPanelSection(title: "View", items: [
                CockpitCommandPanelItem(id: "view.refresh", title: "Refresh", symbol: "arrow.clockwise", enabled: true, toolTip: "Refresh projection"),
                CockpitCommandPanelItem(id: "view.inspector", title: rightPanelMode == .inspector ? "Close Inspector" : "Inspector", symbol: "sidebar.right", enabled: true, toolTip: "Toggle row inspector"),
                CockpitCommandPanelItem(id: "view.diagnostics", title: rightPanelMode == .diagnostics ? "Close Diagnostics" : "Diagnostics", symbol: "waveform.path.ecg", enabled: true, toolTip: "Toggle diagnostics"),
                CockpitCommandPanelItem(id: "view.filters", title: quickFiltersPinned ? "Hide Filters" : "Pin Filters", symbol: "line.3.horizontal.decrease.circle", enabled: true, toolTip: "Toggle quick filters"),
            ]),
            CockpitCommandPanelSection(title: "Layout", items: [
                CockpitCommandPanelItem(id: "layout.sublines", title: model?.showSubline == true ? "Hide Sublines" : "Sublines", symbol: "text.alignleft", enabled: true, toolTip: "Toggle row sublines"),
                CockpitCommandPanelItem(id: "layout.expand", title: "Expand", symbol: "arrow.down.right.and.arrow.up.left", enabled: true, toolTip: "Expand visible groups"),
                CockpitCommandPanelItem(id: "layout.collapse", title: "Collapse", symbol: "arrow.up.left.and.arrow.down.right", enabled: true, toolTip: "Collapse visible groups"),
                CockpitCommandPanelItem(id: "layout.reverseSort", title: "Reverse Sort", symbol: "arrow.up.arrow.down", enabled: model?.sortMode != .smart, toolTip: model?.sortMode == .smart ? "Choose a column sort first" : "Reverse current sort"),
                CockpitCommandPanelItem(id: "view.save", title: "Save View", symbol: "bookmark", enabled: true, toolTip: "Save current view"),
                CockpitCommandPanelItem(id: "view.reset", title: "Reset", symbol: "arrow.counterclockwise", enabled: true, toolTip: "Reset filters and layout"),
            ]),
        ]
        if !enabledRowItems.isEmpty {
            sections.append(CockpitCommandPanelSection(title: row?.title ?? "Row actions", items: enabledRowItems))
        }
        sections.append(
            CockpitCommandPanelSection(title: "Jobs and Boards", items: [
                CockpitCommandPanelItem(id: "jobs.resumeAll", title: "Resume Visible", symbol: "play.circle", enabled: canResume, tint: CockpitTokens.Color.green, toolTip: canResume ? "Resume visible eligible jobs" : "No visible resumable jobs"),
                CockpitCommandPanelItem(id: "jobs.pauseAll", title: "Pause Visible", symbol: "pause.circle", enabled: canPause, tint: CockpitTokens.Color.amber, toolTip: canPause ? "Pause visible eligible jobs" : "No visible pausable jobs"),
                CockpitCommandPanelItem(id: "open.jobs", title: "Jobs", symbol: "tablecells", enabled: true, toolTip: "Open jobs board"),
                CockpitCommandPanelItem(id: "open.rich", title: "Console", symbol: "terminal", enabled: true, toolTip: "Open rich console"),
                CockpitCommandPanelItem(id: "daemon.restart", title: "Restart Daemon", symbol: "power", enabled: true, tint: CockpitTokens.Color.amber, toolTip: "Restart io.coordharness.ops.web"),
            ])
        )
        let panel = CockpitCommandsPanel(sections: sections) { [weak self] id in
            self?.commandsPopover.performClose(nil)
            self?.performCommandPanelAction(id)
        }
        let controller = NSViewController()
        controller.view = panel
        commandsPopover.contentViewController = controller
        commandsPopover.behavior = .transient
        commandsPopover.animates = true
        commandsPopover.show(relativeTo: sender.bounds, of: sender, preferredEdge: .maxY)
    }

    private func showContextPalette(initialQuery requestedQuery: String? = nil) {
        let seed = searchField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let initialQuery = requestedQuery ?? (seed.isEmpty ? "current native cockpit work" : seed)
        contextPalette.show(initialQuery: initialQuery, mode: .all)
        scheduleContextSearch(initialQuery, mode: .all, immediate: true)
    }

    private func scheduleContextSearch(
        _ query: String,
        mode: NativeContextPaletteMode,
        immediate: Bool = false
    ) {
        contextSearchTask?.cancel()
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            contextPalette.setLoading(query: "")
            return
        }
        contextPalette.setLoading(query: trimmed)
        contextSearchTask = Task { [weak self] in
            if !immediate {
                try? await Task.sleep(nanoseconds: 240_000_000)
            }
            guard !Task.isCancelled, let self else { return }
            do {
                let response = try await self.contextBridge.search(query: trimmed, mode: mode, limit: 18)
                guard !Task.isCancelled else { return }
                await MainActor.run {
                    self.contextPalette.render(response: response)
                }
            } catch {
                guard !Task.isCancelled else { return }
                await MainActor.run {
                    self.contextPalette.renderError(error.localizedDescription)
                }
            }
        }
    }

    private func readContextHit(_ hit: NativeContextHit) {
        guard let pointer = hit.pointer, !pointer.isEmpty else { return }
        contextReadTask?.cancel()
        contextReadTask = Task { [weak self] in
            guard let self else { return }
            do {
                let response = try await self.contextBridge.read(pointer: pointer, maxBytes: 12_000)
                guard !Task.isCancelled else { return }
                await MainActor.run {
                    self.contextPalette.renderRead(response)
                }
            } catch {
                guard !Task.isCancelled else { return }
                await MainActor.run {
                    self.contextPalette.renderError(error.localizedDescription)
                }
            }
        }
    }

    private func openContextHit(_ hit: NativeContextHit) {
        if revealWorkHit(hit) {
            contextPalette.hide()
            return
        }
        if let pointer = hit.pointer, revealWorkPointer(pointer) {
            contextPalette.hide()
            return
        }
        if let pointer = hit.pointer, openLocalPointer(pointer) {
            return
        }
        if let pointer = hit.pointer, pointer.hasPrefix("http"), let url = URL(string: pointer) {
            NSWorkspace.shared.open(url)
            return
        }
        if let pointer = hit.pointer {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(pointer, forType: .string)
            contextPalette.renderError("Copied pointer because no direct opener is registered: \(pointer)")
        }
    }

    private func revealWorkHit(_ hit: NativeContextHit) -> Bool {
        guard hit.source == "board" || hit.source == "board_history" else { return false }
        let id = hit.metadata["id"]?.stringValue
            ?? hit.metadata["work_id"]?.stringValue
            ?? hit.metadata["job_id"]?.stringValue
        guard let id, !id.isEmpty else { return false }
        return revealWorkID(id)
    }

    private func revealWorkPointer(_ pointer: String) -> Bool {
        guard pointer.hasPrefix("coord://work/") else { return false }
        let workID = String(pointer.dropFirst("coord://work/".count))
        return revealWorkID(workID)
    }

    private func revealWorkID(_ workID: String) -> Bool {
        guard let row = model?.items.compactMap(\.row).first(where: { row in
            row.workID == workID || row.jobID == workID || row.dedupKey == workID
        }) else { return false }
        selectedRowKey = row.dedupKey
        tableController.selectRow(dedupKey: row.dedupKey)
        if rightPanelMode == .none {
            setRightPanel(.inspector)
        } else {
            redrawFromModel()
        }
        return true
    }

    private func openLocalPointer(_ pointer: String) -> Bool {
        var raw = pointer
        if raw.hasPrefix("memory://") {
            raw = String(raw.dropFirst("memory://".count))
        } else if raw.hasPrefix("kfts://") {
            raw = String(raw.dropFirst("kfts://".count))
        }
        if let hash = raw.firstIndex(of: "#") {
            raw = String(raw[..<hash])
        }
        guard !raw.isEmpty, !raw.hasPrefix("coord://") else { return false }
        let repoRoot = URL(fileURLWithPath: ProcessInfo.processInfo.environment["COORD_PROJECT_ROOT"]
            ?? FileManager.default.currentDirectoryPath)
        let url: URL
        if raw.hasPrefix("/") {
            url = URL(fileURLWithPath: raw)
        } else {
            url = repoRoot.appendingPathComponent(raw)
        }
        guard FileManager.default.fileExists(atPath: url.path) else { return false }
        NSWorkspace.shared.open(url)
        return true
    }

    private func performCommandPanelAction(_ id: String) {
        switch id {
        case "view.refresh": refreshPressed()
        case "view.inspector": inspectorPressed()
        case "view.diagnostics": diagnosticsPressed()
        case "view.filters":
            if let quickFiltersButton {
                showFiltersPanel(quickFiltersButton)
            }
        case "layout.sublines": sublinePressed()
        case "layout.expand": expandPressed()
        case "layout.collapse": collapsePressed()
        case "layout.reverseSort": reverseSortMenuPressed()
        case "view.save": saveViewPressed()
        case "view.reset": resetPressed()
        case "row.assignClaude": assignClaudePressed()
        case "row.assignCodex": assignCodexPressed()
        case "row.unassign": unassignSelectedPressed()
        case "row.claim": claimSelectedPressed()
        case "row.handoff": handoffSelectedPressed()
        case "row.capability": capabilitySelectedPressed()
        case "jobs.resumeAll": resumeAllPressed()
        case "jobs.pauseAll": pauseAllPressed()
        case "open.backlog": openBacklogPressed()
        case "open.jobs": openJobsPressed()
        case "open.rich": openRichPressed()
        case "daemon.restart": restartDaemonPressed()
        default: break
        }
    }

    @objc private func expandPressed() {
        guard var model else { return }
        for item in model.items {
            if case .group(let group) = item, group.isCollapsed { model.toggleGroup(key: group.key) }
        }
        self.model = model
        redrawFromModel()
        delegate?.cockpitRootViewDidChangeLocalState(self)
    }

    @objc private func collapsePressed() {
        guard var model else { return }
        for item in model.items {
            if case .group(let group) = item, !group.isCollapsed { model.toggleGroup(key: group.key) }
        }
        self.model = model
        redrawFromModel()
        delegate?.cockpitRootViewDidChangeLocalState(self)
    }

    @objc private func resetPressed() {
        guard var model else { return }
        model.scope = .now
        model.groupMode = .smart
        model.setSortMode(.smart)
        model.owner = ""
        model.module = ""
        model.status = ""
        model.query = ""
        model.showSubline = false
        model.collapsedGroupKeys = []
        model.expandedRowKeys = []
        model.resetColumns()
        self.model = model
        synchronizeControls()
        redrawFromModel()
        delegate?.cockpitRootViewDidChangeLocalState(self)
    }

    @objc private func quickFiltersPressed(_ sender: Any) {
        let clickCount = NSApp.currentEvent?.clickCount ?? 1
        if clickCount > 1 {
            toggleInlineFilters()
            return
        }
        let anchor = (sender as? NSButton) ?? quickFiltersButton ?? commandsButton
        guard let anchor else { return }
        showFiltersPanel(anchor)
    }

    private func toggleInlineFilters() {
        quickFiltersPinned.toggle()
        CockpitNativePreferences.quickFiltersPinned = quickFiltersPinned
        updateQuickFiltersButton()
        needsLayout = true
        delegate?.cockpitRootViewDidChangeLocalState(self)
    }

    private func showFiltersPanel(_ anchor: NSButton) {
        guard let model else { return }
        let panel = CockpitFiltersPanel(
            viewOptions: savedViewOptions(selected: matchingSavedViewName(for: model.viewSnapshot)),
            groupOptions: groupOptions(),
            sortOptions: sortOptions(for: model),
            ownerOptions: filterOptions(kind: .owner, title: "Owner", state: model.state),
            moduleOptions: filterOptions(kind: .module, title: "Module", state: model.state),
            statusOptions: filterOptions(kind: .status, title: "Status", state: model.state),
            selectedView: matchingSavedViewName(for: model.viewSnapshot) ?? "",
            selectedGroup: model.groupMode.rawValue,
            selectedSort: model.sortMode.rawValue,
            selectedOwner: model.owner,
            selectedModule: model.module,
            selectedStatus: model.status,
            onView: { [weak self] name in
                self?.applySavedView(named: name)
            },
            onGroup: { [weak self] raw in
                self?.applyGroupMode(raw)
            },
            onSort: { [weak self] raw in
                self?.applySortMode(raw)
            },
            onFilter: { [weak self] kind, value in
                self?.applyFilterValue(kind: kind, value: value)
            },
            onColumns: { [weak self, weak anchor] in
                guard let self, let anchor else { return }
                self.filtersPopover.performClose(nil)
                self.columnsPressed(anchor)
            },
            onDiagnostics: { [weak self] in
                self?.filtersPopover.performClose(nil)
                self?.diagnosticsPressed()
            },
            onReset: { [weak self] in
                self?.filtersPopover.performClose(nil)
                self?.resetPressed()
            }
        )
        let controller = NSViewController()
        controller.view = panel
        filtersPopover.contentViewController = controller
        filtersPopover.behavior = .transient
        filtersPopover.animates = true
        filtersPopover.show(relativeTo: anchor.bounds, of: anchor, preferredEdge: .maxY)
    }

    private func savedViewOptions(selected: String?) -> [(String, String)] {
        var options: [(String, String)] = [("View", "")]
        options.append(contentsOf: savedViewStore.allViews().map { ("View: \($0.name)", $0.name) })
        if let selected, !selected.isEmpty, !options.contains(where: { $0.1 == selected }) {
            options.append(("View: \(selected)", selected))
        }
        return options
    }

    private func groupOptions() -> [(String, String)] {
        CockpitGroupMode.allCases.map { ($0.label, $0.rawValue) }
    }

    private func sortOptions(for model: CockpitPresentationModel) -> [(String, String)] {
        CockpitSortMode.pickerCases.map { mode in
            let title = mode == model.sortMode ? mode.popupLabel(direction: model.sortDirection) : mode.label
            return (title, mode.rawValue)
        }
    }

    private func applyGroupMode(_ raw: String) {
        guard var model else { return }
        model.groupMode = CockpitGroupMode(rawValue: raw) ?? .smart
        self.model = model
        synchronizeControls()
        redrawFromModel()
        delegate?.cockpitRootViewDidChangeLocalState(self)
    }

    private func applySortMode(_ raw: String) {
        guard var model else { return }
        model.setSortMode(CockpitSortMode(rawValue: raw) ?? .smart)
        self.model = model
        synchronizeControls()
        redrawFromModel()
        delegate?.cockpitRootViewDidChangeLocalState(self)
    }

    private func applySavedView(named name: String) {
        guard !name.isEmpty, var model, let view = savedViewStore.view(named: name) else { return }
        model.applyView(view.snapshot)
        self.model = model
        synchronizeControls()
        redrawFromModel()
        delegate?.cockpitRootViewDidChangeLocalState(self)
    }

    private func filterOptions(kind: CockpitFilterKind, title: String, state: CockpitState) -> [(String, String)] {
        var options: [(String, String)] = [(title, "")]
        options.append(contentsOf: state.filterOptions
            .filter { $0.kind == kind }
            .prefix(24)
            .map { ("\($0.label)  \($0.count)", $0.value) })
        return options
    }

    @objc private func saveViewPressed() {
        guard let model else { return }
        let alert = NSAlert()
        alert.messageText = "Save view"
        alert.informativeText = "Name this view."
        alert.alertStyle = .informational
        alert.addButton(withTitle: "Save")
        alert.addButton(withTitle: "Cancel")
        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 260, height: 26))
        input.placeholderString = "View name"
        alert.accessoryView = input

        let persist = { [weak self] in
            guard let self else { return }
            do {
                try self.savedViewStore.save(name: input.stringValue, snapshot: model.viewSnapshot)
                self.updateSavedViewsPopup()
                if let name = self.matchingSavedViewName(for: model.viewSnapshot) {
                    if let item = self.viewsPopup.itemArray.first(where: { ($0.representedObject as? String) == name }) {
                        self.viewsPopup.select(item)
                    }
                }
                self.actionStatus.stringValue = "Saved view"
            } catch {
                self.actionStatus.stringValue = "Could not save view"
            }
        }

        if let window {
            alert.beginSheetModal(for: window) { response in
                if response == .alertFirstButtonReturn { persist() }
            }
        } else if alert.runModal() == .alertFirstButtonReturn {
            persist()
        }
    }

    @objc private func pauseAllPressed() {
        sendBulkJobControl(action: "jobs.pause_all", rowActionID: "jobs.pause", emptyMessage: "No visible pausable jobs")
    }

    @objc private func resumeAllPressed() {
        sendBulkJobControl(action: "jobs.resume_all", rowActionID: "jobs.resume", emptyMessage: "No visible resumable jobs")
    }

    private func sendBulkJobControl(action: String, rowActionID: String, emptyMessage: String) {
        guard let model else { return }
        let jobs = model.visibleJobControlIDs(actionID: rowActionID)
        guard !jobs.isEmpty else {
            actionStatus.stringValue = emptyMessage
            return
        }
        delegate?.cockpitRootView(self, perform: action, row: nil, payload: ["jobs": jobs])
    }

    private func performRowAction(_ action: CockpitRowAction, row: CockpitRow) {
        switch action.id {
        case "claim.create":
            promptClaim(row: row)
            return
        case "handoff.create":
            promptHandoff(row: row)
            return
        case "capability.run":
            promptCapability(row: row)
            return
        default:
            break
        }
        guard action.requiresConfirmation else {
            delegate?.cockpitRootView(self, perform: action.id, row: row, payload: [:])
            return
        }

        let alert = NSAlert()
        alert.messageText = "Confirm \(action.label)"
        alert.informativeText = "This will run \(action.id) for \(row.title)."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Confirm")
        alert.addButton(withTitle: "Cancel")

        let send = { [weak self] in
            guard let self else { return }
            self.delegate?.cockpitRootView(self, perform: action.id, row: row, payload: ["confirmed": true])
        }
        if let window {
            alert.beginSheetModal(for: window) { response in
                if response == .alertFirstButtonReturn { send() }
                else { self.actionStatus.stringValue = "Cancelled \(action.label)" }
            }
        } else if alert.runModal() == .alertFirstButtonReturn {
            send()
        } else {
            actionStatus.stringValue = "Cancelled \(action.label)"
        }
    }

    @objc private func assignClaudePressed() {
        performSelectedRowAction(id: "task.assign.claude")
    }

    @objc private func assignCodexPressed() {
        performSelectedRowAction(id: "task.assign.codex")
    }

    @objc private func unassignSelectedPressed() {
        performSelectedRowAction(id: "task.unassign")
    }

    @objc private func claimSelectedPressed() {
        performSelectedRowAction(id: "claim.create")
    }

    private func promptClaim(row: CockpitRow) {
        promptSingleLine(
            title: "Claim selected",
            message: "Claim step",
            placeholder: "Step",
            defaultValue: "native cockpit claim",
            confirmTitle: "Claim"
        ) { [weak self] step in
            guard let self else { return }
            self.delegate?.cockpitRootView(self, perform: "claim.create", row: row, payload: ["step": step])
        }
    }

    @objc private func handoffSelectedPressed() {
        performSelectedRowAction(id: "handoff.create")
    }

    @objc private func capabilitySelectedPressed() {
        performSelectedRowAction(id: "capability.run")
    }

    @objc private func restartDaemonPressed() {
        let alert = NSAlert()
        alert.messageText = "Restart ops daemon"
        alert.informativeText = "This restarts io.coordharness.ops.web."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Restart")
        alert.addButton(withTitle: "Cancel")
        let send = { [weak self] in
            guard let self else { return }
            self.delegate?.cockpitRootView(self, perform: "daemon.restart", row: nil, payload: ["confirmed": true])
        }
        if let window {
            alert.beginSheetModal(for: window) { response in
                if response == .alertFirstButtonReturn { send() }
            }
        } else if alert.runModal() == .alertFirstButtonReturn {
            send()
        }
    }

    private func selectedActionRow() -> CockpitRow? {
        guard let model, let row = selectedRow(in: model), workID(row) != nil else { return nil }
        return row
    }

    private func performSelectedRowAction(id: String) {
        guard let row = selectedActionRow() else {
            actionStatus.stringValue = "Select a work row"
            return
        }
        guard let action = enabledRowAction(row, id: id) else {
            actionStatus.stringValue = unavailableActionMessage(row: row, id: id)
            return
        }
        performRowAction(action, row: row)
    }

    private func enabledRowAction(_ row: CockpitRow?, id: String) -> CockpitRowAction? {
        row?.actions.first { $0.id == id && $0.isEnabled }
    }

    private func rowActionState(_ row: CockpitRow?, id: String) -> (enabled: Bool, toolTip: String?) {
        guard let row else { return (false, "Select a work row") }
        guard let action = row.actions.first(where: { $0.id == id }) else {
            return (false, "Action unavailable for this row")
        }
        if action.isEnabled { return (true, action.label) }
        return (false, action.disabledReason?.isEmpty == false ? action.disabledReason : "Action disabled for this row")
    }

    private func unavailableActionMessage(row: CockpitRow, id: String) -> String {
        guard let action = row.actions.first(where: { $0.id == id }) else {
            return "Action unavailable"
        }
        if let reason = action.disabledReason, !reason.isEmpty {
            return reason
        }
        return "Action disabled"
    }

    private func workID(_ row: CockpitRow) -> String? {
        let value = (row.workID ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return value.isEmpty ? nil : value
    }

    private func jobControlID(_ row: CockpitRow) -> String? {
        let job = (row.jobID ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if !job.isEmpty { return job }
        return workID(row)
    }

    private func promptSingleLine(
        title: String,
        message: String,
        placeholder: String,
        defaultValue: String,
        confirmTitle: String,
        completion: @escaping (String) -> Void
    ) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.alertStyle = .informational
        alert.addButton(withTitle: confirmTitle)
        alert.addButton(withTitle: "Cancel")
        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 300, height: 26))
        input.placeholderString = placeholder
        input.stringValue = defaultValue
        alert.accessoryView = input
        let send = {
            let value = input.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            if !value.isEmpty { completion(value) }
        }
        if let window {
            alert.beginSheetModal(for: window) { response in
                if response == .alertFirstButtonReturn { send() }
            }
        } else if alert.runModal() == .alertFirstButtonReturn {
            send()
        }
    }

    private func promptHandoff(row: CockpitRow) {
        let alert = NSAlert()
        alert.messageText = "Handoff selected"
        alert.informativeText = workID(row) ?? row.title
        alert.alertStyle = .informational
        alert.addButton(withTitle: "Create")
        alert.addButton(withTitle: "Cancel")

        let content = NSView(frame: NSRect(x: 0, y: 0, width: 340, height: 116))
        let lane = NSPopUpButton(frame: NSRect(x: 0, y: 88, width: 140, height: 26))
        lane.addItems(withTitles: ["claude", "codex"])
        let title = NSTextField(frame: NSRect(x: 0, y: 50, width: 340, height: 26))
        title.placeholderString = "Task"
        title.stringValue = row.title
        let acceptance = NSTextField(frame: NSRect(x: 0, y: 12, width: 340, height: 26))
        acceptance.placeholderString = "Acceptance"
        acceptance.stringValue = "Done signal exists and verification passes"
        content.addSubview(lane)
        content.addSubview(title)
        content.addSubview(acceptance)
        alert.accessoryView = content

        let send = { [weak self] in
            guard let self else { return }
            let task = title.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            let accept = acceptance.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !task.isEmpty, !accept.isEmpty else { return }
            self.delegate?.cockpitRootView(
                self,
                perform: "handoff.create",
                row: row,
                payload: [
                    "to": lane.titleOfSelectedItem ?? "claude",
                    "task": task,
                    "acceptance": accept,
                    "refs": [self.workID(row) ?? row.dedupKey],
                    "why": "native cockpit handoff",
                ]
            )
        }
        if let window {
            alert.beginSheetModal(for: window) { response in
                if response == .alertFirstButtonReturn { send() }
            }
        } else if alert.runModal() == .alertFirstButtonReturn {
            send()
        }
    }

    private func promptCapability(row: CockpitRow) {
        let alert = NSAlert()
        alert.messageText = "Run capability"
        alert.informativeText = workID(row) ?? row.title
        alert.alertStyle = .informational
        alert.addButton(withTitle: "Run")
        alert.addButton(withTitle: "Cancel")

        let content = NSView(frame: NSRect(x: 0, y: 0, width: 300, height: 72))
        let capability = NSPopUpButton(frame: NSRect(x: 0, y: 44, width: 180, height: 26))
        capability.addItems(withTitles: ["context", "loop_doctor", "verify", "open_proof", "deerflow", "token_ledger", "handoff_packet"])
        let contextRef = NSTextField(frame: NSRect(x: 0, y: 6, width: 300, height: 26))
        contextRef.placeholderString = "Context ref"
        contextRef.stringValue = workID(row).map { "coord://work/\($0)" } ?? ""
        content.addSubview(capability)
        content.addSubview(contextRef)
        alert.accessoryView = content

        let send = { [weak self] in
            guard let self else { return }
            self.delegate?.cockpitRootView(
                self,
                perform: "capability.run",
                row: row,
                payload: [
                    "capability": capability.titleOfSelectedItem ?? "context",
                    "context_ref": contextRef.stringValue.trimmingCharacters(in: .whitespacesAndNewlines),
                ]
            )
        }
        if let window {
            alert.beginSheetModal(for: window) { response in
                if response == .alertFirstButtonReturn { send() }
            }
        } else if alert.runModal() == .alertFirstButtonReturn {
            send()
        }
    }

    private func openSurface(path: String) {
        let wasMapVisible = activeSurface.isEmbeddedWeb
        if let surface = Surface(rawValue: path), surface.isEmbeddedWeb {
            activeSurface = surface
            setRightPanel(.none)
            updateSurfaceButtons()
            let view = ensureMapView()
            // Label before path: assigning the path starts the load, and a
            // failure has to be able to name the surface that failed.
            view.surfaceLabel = surface.label
            view.surfacePath = surface.embedPath
            if surface == .map, let mapState {
                view.render(mapState)
            } else if surface == .map {
                delegate?.cockpitRootViewDidRequestMapRefresh(self)
            } else {
                view.activate()
            }
            needsLayout = true
            return
        }
        if path == Surface.usage.rawValue {
            activeSurface = .usage
            setRightPanel(.none)
            applyMapLifecycle(CockpitMapLifecycle.surfaceChangeAction(wasMapVisible: wasMapVisible, isMapVisible: false))
            _ = ensureUsageView()
            Task { @MainActor [weak self] in
                guard let self else { return }
                await self.usageStore.refresh(force: true)
            }
            updateSurfaceButtons()
            needsLayout = true
            return
        }

        if path == Surface.cockpit.rawValue {
            activeSurface = .cockpit
            applyMapLifecycle(CockpitMapLifecycle.surfaceChangeAction(wasMapVisible: wasMapVisible, isMapVisible: false))
            updateSurfaceButtons()
            needsLayout = true
            return
        }
        delegate?.cockpitRootView(self, open: path)
    }

    private func applyMapLifecycle(_ action: CockpitMapLifecycleAction) {
        switch action {
        case .storeOnly:
            break
        case .render:
            ensureMapView().activate()
        case .deactivate(let delay):
            mapView?.deactivate(unloadAfter: delay)
        case .unloadNow:
            unloadMapResources()
        }
    }

    private var mapFrame: NSRect {
        NSRect(
            x: 0,
            y: CockpitTokens.topbarHeight,
            width: bounds.width,
            height: max(120, bounds.height - CockpitTokens.topbarHeight)
        )
    }

    private func ensureUsageView() -> NSHostingView<InstalledUsageDashboardView> {
        if let usageView { return usageView }
        let view = NSHostingView(rootView: InstalledUsageDashboardView(
            managesRefresh: !usageManagedExternally,
            store: usageStore
        ))
        view.frame = mapFrame
        view.isHidden = activeSurface != .usage
        usageView = view
        addSubview(view, positioned: .below, relativeTo: diagnostics)
        needsLayout = true
        return view
    }

    private func ensureMapView() -> CockpitMapWebView {
        if let mapView { return mapView }
        let view = CockpitMapWebView()
        view.frame = mapFrame
        view.isHidden = !activeSurface.isEmbeddedWeb
        mapView = view
        addSubview(view, positioned: .below, relativeTo: diagnostics)
        needsLayout = true
        return view
    }

    private func unloadMapResources() {
        mapView?.unloadNow()
        mapView?.removeFromSuperview()
        mapView = nil
        mapState = nil
        delegate?.cockpitRootViewDidReleaseMapResources(self)
    }

    @objc private func openJobsPressed() {
        delegate?.cockpitRootView(self, open: "/jobs")
    }

    @objc private func openBacklogPressed() {
        delegate?.cockpitRootView(self, open: "/roadmap")
    }

    @objc private func openRichPressed() {
        delegate?.cockpitRootView(self, open: "\(HarnessEndpoint.base)")
    }

    private func updateSublineButton(_ enabled: Bool) {
        sublineButton?.contentTintColor = enabled ? CockpitTokens.Color.text : CockpitTokens.Color.muted
        applyQuietButtonChrome(sublineButton, active: enabled)
        (sublineButton as? CockpitGlowButton)?.isGlowing = enabled
        sublineButton?.toolTip = enabled ? "Hide row sublines" : "Show row sublines"
    }

    private func updateQuickFiltersButton() {
        quickFiltersButton?.contentTintColor = quickFiltersPinned ? CockpitTokens.Color.text : CockpitTokens.Color.muted
        applyQuietButtonChrome(quickFiltersButton, active: quickFiltersPinned)
        (quickFiltersButton as? CockpitGlowButton)?.isGlowing = quickFiltersPinned
        quickFiltersButton?.toolTip = quickFiltersPinned ? "Hide quick filters" : "Show quick filters"
    }

    private func updateBulkButtons(_ model: CockpitPresentationModel) {
        updateBulkButton(
            resumeAllButton,
            enabled: !model.visibleJobControlIDs(actionID: "jobs.resume").isEmpty,
            color: CockpitTokens.Color.green,
            enabledTip: "Resume visible eligible jobs",
            disabledTip: "No visible resumable jobs"
        )
        updateBulkButton(
            pauseAllButton,
            enabled: !model.visibleJobControlIDs(actionID: "jobs.pause").isEmpty,
            color: CockpitTokens.Color.amber,
            enabledTip: "Pause visible eligible jobs",
            disabledTip: "No visible pausable jobs"
        )
    }

    private func updateBulkButton(_ button: NSButton?, enabled: Bool, color: NSColor, enabledTip: String, disabledTip: String) {
        button?.isEnabled = enabled
        button?.isHidden = !enabled
        button?.contentTintColor = enabled ? color : CockpitTokens.Color.faint.withAlphaComponent(0.55)
        applyQuietButtonChrome(button, active: enabled, tint: color)
        (button as? CockpitGlowButton)?.isGlowing = enabled
        (button as? CockpitGlowButton)?.glowTint = color
        button?.toolTip = enabled ? enabledTip : disabledTip
    }

    private func updateSurfaceButtons() {
        let commsSurfaces: Set<Surface> = [.comms, .dependencies]
        let moreSurfaces: Set<Surface> = [.attention, .usage, .mesh, .map]
        for button in surfaceButtons {
            let identifier = button.identifier?.rawValue ?? ""
            let active: Bool
            switch identifier {
            case "jobs": active = activeSurface == .cockpit
            case "comms": active = commsSurfaces.contains(activeSurface)
            case "atlas": active = activeSurface == .atlas
            case "more": active = moreSurfaces.contains(activeSurface)
            default: active = false
            }
            (button as? CockpitNavButton)?.setActive(active)
            button.contentTintColor = active ? CockpitTokens.Color.text : CockpitTokens.Color.muted
            button.font = .systemFont(ofSize: 12, weight: active ? .bold : .semibold)
        }
    }
}

extension CockpitRootView: CockpitMapViewDelegate {
    func cockpitMapView(_ view: CockpitMapView, open path: String) {
        delegate?.cockpitRootView(self, open: path)
    }
}

private extension CockpitKeyboardShortcut {
    init(event: NSEvent) {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        let rawKey = event.charactersIgnoringModifiers ?? event.characters ?? ""
        let normalized = rawKey == "\u{1b}" ? "escape" : rawKey
        self.init(
            key: normalized,
            command: flags.contains(.command),
            shift: flags.contains(.shift),
            option: flags.contains(.option),
            control: flags.contains(.control)
        )
    }
}

private extension CockpitRootView {
    var panelState: CockpitRightPanelState {
        switch rightPanelMode {
        case .none: return .none
        case .diagnostics: return .diagnostics
        case .inspector: return .inspector
        }
    }
}

private extension CockpitRootView.RightPanelMode {
    init(_ state: CockpitRightPanelState) {
        switch state {
        case .none: self = .none
        case .diagnostics: self = .diagnostics
        case .inspector: self = .inspector
        }
    }
}

final class CockpitSessionStripView: NSView {
    private var pills: [CockpitSessionPill] = []
    private let overflowPill = CockpitSessionPill(text: "+0", color: CockpitTokens.Color.faint)
    var onClearChip: ((String) -> Void)?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        overflowPill.isHidden = true
        addSubview(overflowPill)
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    func configure(sessions: [CockpitSession], chips: [CockpitFilterChip]) {
        pills.forEach { $0.removeFromSuperview() }
        pills = []

        for chip in chips.prefix(3) {
            addPill(text: chip.label, color: CockpitTokens.Color.selectionStroke, chipID: chip.id)
        }
        if !chips.isEmpty && !sessions.isEmpty {
            addPill(text: "|", color: CockpitTokens.Color.faint, isDivider: true)
        }
        var seenSessionLabels = Set<String>()
        for session in sessions {
            let label = uniqueSessionLabel(session, used: &seenSessionLabels)
            guard !label.isEmpty else { continue }
            addPill(text: label, color: session.isStale ? CockpitTokens.Color.amber : CockpitTokens.ownerColor(session.actor))
            if seenSessionLabels.count >= 6 { break }
        }
        if pills.isEmpty {
            addPill(text: "No live sessions", color: CockpitTokens.Color.faint)
        }
        needsLayout = true
    }

    override func layout() {
        var x: CGFloat = 0
        let maxX = bounds.width
        let overflowWidth: CGFloat = 38
        var visiblePills: [CockpitSessionPill] = []
        for (index, pill) in pills.enumerated() {
            let width = pill.preferredWidth
            let remaining = pills.count - index - 1
            let reserve = remaining > 0 ? overflowWidth + 5 : 0
            if x + width + reserve > maxX {
                pill.isHidden = true
            } else {
                pill.isHidden = false
                pill.frame = NSRect(x: x, y: 3, width: width, height: 24)
                visiblePills.append(pill)
                x += width + 5
            }
        }
        var hiddenCount = pills.count - visiblePills.count
        if hiddenCount > 0 {
            while x + overflowWidth > maxX, let last = visiblePills.popLast() {
                last.isHidden = true
                x = last.frame.minX
                hiddenCount = pills.count - visiblePills.count
            }
            overflowPill.updateText("+\(hiddenCount)")
            overflowPill.frame = NSRect(x: x, y: 3, width: overflowWidth, height: 24)
            overflowPill.isHidden = maxX < overflowWidth
        } else {
            overflowPill.isHidden = true
        }
    }

    private func addPill(text: String, color: NSColor, isDivider: Bool = false, chipID: String? = nil) {
        let pill = CockpitSessionPill(text: text, color: color, isDivider: isDivider, chipID: chipID)
        pill.onClear = { [weak self] chipID in
            self?.onClearChip?(chipID)
        }
        addSubview(pill)
        pills.append(pill)
    }

    private func uniqueSessionLabel(_ session: CockpitSession, used: inout Set<String>) -> String {
        var label = session.label.trimmingCharacters(in: .whitespacesAndNewlines)
        if label.isEmpty {
            label = sessionShortLabel(session)
        }
        var candidate = label
        if used.contains(candidate) {
            let fallback = sessionShortLabel(session, minCharacters: 10)
            candidate = fallback == candidate ? "\(fallback) #\(used.count + 1)" : fallback
        }
        if used.contains(candidate) {
            candidate = "\(candidate) #\(used.count + 1)"
        }
        used.insert(candidate)
        return candidate
    }

    private func sessionShortLabel(_ session: CockpitSession, minCharacters: Int = 8) -> String {
        let actor = session.actor.isEmpty ? "agent" : session.actor
        let tail = session.id.split(separator: ":").last.map(String.init) ?? session.id
        let short = tail.count > minCharacters ? String(tail.prefix(minCharacters)) : tail
        return "\(actor) \(short)"
    }
}

private final class CockpitSessionPill: NSView {
    private var text: String
    private let color: NSColor
    private let isDivider: Bool
    private let chipID: String?
    private(set) var preferredWidth: CGFloat
    var onClear: ((String) -> Void)?

    init(text: String, color: NSColor, isDivider: Bool = false, chipID: String? = nil) {
        self.text = text
        self.color = color
        self.isDivider = isDivider
        self.chipID = chipID
        self.preferredWidth = CockpitSessionPill.width(for: text, isDivider: isDivider, isChip: chipID != nil)
        super.init(frame: .zero)
        wantsLayer = true
        layer?.cornerRadius = 7
        layer?.backgroundColor = isDivider
            ? NSColor.clear.cgColor
            : (chipID == nil ? color.withAlphaComponent(0.055) : CockpitTokens.Color.selectionFill.withAlphaComponent(0.44)).cgColor
        layer?.borderColor = isDivider
            ? NSColor.clear.cgColor
            : (chipID == nil ? color.withAlphaComponent(0.16) : CockpitTokens.Color.selectionStroke.withAlphaComponent(0.16)).cgColor
        layer?.borderWidth = isDivider ? 0 : 1
        toolTip = text
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    override func resetCursorRects() {
        super.resetCursorRects()
        if chipID != nil {
            addCursorRect(bounds, cursor: .pointingHand)
        }
    }

    override func mouseDown(with event: NSEvent) {
        guard let chipID else {
            super.mouseDown(with: event)
            return
        }
        onClear?(chipID)
    }

    func updateText(_ text: String) {
        self.text = text
        self.preferredWidth = CockpitSessionPill.width(for: text, isDivider: isDivider, isChip: chipID != nil)
        toolTip = text
        needsDisplay = true
    }

    private static func width(for text: String, isDivider: Bool, isChip: Bool) -> CGFloat {
        isDivider ? 10 : min(isChip ? 124 : 116, max(46, CGFloat(text.count * 7 + (isChip ? 36 : 22))))
    }

    override func draw(_ dirtyRect: NSRect) {
        let paragraph = NSMutableParagraphStyle()
        paragraph.alignment = chipID == nil ? .center : .left
        paragraph.lineBreakMode = .byTruncatingTail
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 10.5, weight: .semibold),
            .foregroundColor: isDivider ? CockpitTokens.Color.faint : CockpitTokens.Color.text,
            .paragraphStyle: paragraph,
        ]
        let inset: NSRect
        if isDivider {
            inset = NSRect(x: 0, y: 3, width: bounds.width, height: bounds.height - 6)
        } else if chipID == nil {
            inset = bounds.insetBy(dx: 8, dy: 3)
        } else {
            inset = NSRect(x: 9, y: 3, width: max(8, bounds.width - 27), height: bounds.height - 6)
        }
        NSAttributedString(string: text, attributes: attrs).draw(in: inset)
        if chipID != nil {
            let xAttrs: [NSAttributedString.Key: Any] = [
                .font: NSFont.systemFont(ofSize: 11, weight: .bold),
                .foregroundColor: CockpitTokens.Color.muted,
                .paragraphStyle: {
                    let p = NSMutableParagraphStyle()
                    p.alignment = .center
                    return p
                }(),
            ]
            NSAttributedString(string: "x", attributes: xAttrs).draw(in: NSRect(x: bounds.width - 20, y: 3, width: 12, height: bounds.height - 6))
        }
    }
}

final class CockpitFiltersPanel: NSView {
    private let title = CockpitUI.label("Filters", size: 12, weight: .bold, color: CockpitTokens.Color.text)
    private let hint = CockpitUI.label("Saved views, row filters, and columns", size: 10.5, weight: .medium, color: CockpitTokens.Color.muted)
    private let viewPopup = NSPopUpButton()
    private let groupPopup = NSPopUpButton()
    private let sortPopup = NSPopUpButton()
    private let ownerPopup = NSPopUpButton()
    private let modulePopup = NSPopUpButton()
    private let statusPopup = NSPopUpButton()
    private let columnsButton = CockpitUI.button("Columns / Reorder")
    private let diagnosticsButton = CockpitUI.button("Diagnostics")
    private let resetButton = CockpitUI.button("Reset")
    private let onView: (String) -> Void
    private let onGroup: (String) -> Void
    private let onSort: (String) -> Void
    private let onFilter: (CockpitFilterKind, String) -> Void
    private let onColumns: () -> Void
    private let onDiagnostics: () -> Void
    private let onReset: () -> Void

    init(
        viewOptions: [(String, String)],
        groupOptions: [(String, String)],
        sortOptions: [(String, String)],
        ownerOptions: [(String, String)],
        moduleOptions: [(String, String)],
        statusOptions: [(String, String)],
        selectedView: String,
        selectedGroup: String,
        selectedSort: String,
        selectedOwner: String,
        selectedModule: String,
        selectedStatus: String,
        onView: @escaping (String) -> Void,
        onGroup: @escaping (String) -> Void,
        onSort: @escaping (String) -> Void,
        onFilter: @escaping (CockpitFilterKind, String) -> Void,
        onColumns: @escaping () -> Void,
        onDiagnostics: @escaping () -> Void,
        onReset: @escaping () -> Void
    ) {
        self.onView = onView
        self.onGroup = onGroup
        self.onSort = onSort
        self.onFilter = onFilter
        self.onColumns = onColumns
        self.onDiagnostics = onDiagnostics
        self.onReset = onReset
        super.init(frame: NSRect(x: 0, y: 0, width: 372, height: 330))
        wantsLayer = true
        layer?.backgroundColor = CockpitTokens.Color.panel2.withAlphaComponent(0.96).cgColor
        layer?.borderColor = CockpitTokens.Color.line.withAlphaComponent(0.22).cgColor
        layer?.borderWidth = 1
        layer?.cornerRadius = 14
        addSubview(title)
        addSubview(hint)
        configure(viewPopup, role: "view", options: viewOptions, selected: selectedView)
        configure(groupPopup, role: "group", options: groupOptions, selected: selectedGroup)
        configure(sortPopup, role: "sort", options: sortOptions, selected: selectedSort)
        configure(ownerPopup, role: CockpitFilterKind.owner.rawValue, options: ownerOptions, selected: selectedOwner)
        configure(modulePopup, role: CockpitFilterKind.module.rawValue, options: moduleOptions, selected: selectedModule)
        configure(statusPopup, role: CockpitFilterKind.status.rawValue, options: statusOptions, selected: selectedStatus)
        for popup in [viewPopup, groupPopup, sortPopup, ownerPopup, modulePopup, statusPopup] {
            addSubview(popup)
        }
        columnsButton.image = NSImage(systemSymbolName: "rectangle.split.3x3", accessibilityDescription: "Columns")
        columnsButton.imagePosition = .imageLeading
        columnsButton.target = self
        columnsButton.action = #selector(columnsPressed)
        diagnosticsButton.image = NSImage(systemSymbolName: "waveform.path.ecg", accessibilityDescription: "Diagnostics")
        diagnosticsButton.imagePosition = .imageLeading
        diagnosticsButton.target = self
        diagnosticsButton.action = #selector(diagnosticsPressed)
        resetButton.image = NSImage(systemSymbolName: "arrow.counterclockwise", accessibilityDescription: "Reset")
        resetButton.imagePosition = .imageLeading
        resetButton.target = self
        resetButton.action = #selector(resetPressed)
        addSubview(columnsButton)
        addSubview(diagnosticsButton)
        addSubview(resetButton)
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    override func layout() {
        title.frame = NSRect(x: 16, y: 14, width: bounds.width - 32, height: 18)
        hint.frame = NSRect(x: 16, y: 34, width: bounds.width - 32, height: 16)
        viewPopup.frame = NSRect(x: 16, y: 62, width: bounds.width - 32, height: 30)
        groupPopup.frame = NSRect(x: 16, y: 99, width: bounds.width - 32, height: 30)
        sortPopup.frame = NSRect(x: 16, y: 136, width: bounds.width - 32, height: 30)
        ownerPopup.frame = NSRect(x: 16, y: 173, width: bounds.width - 32, height: 30)
        modulePopup.frame = NSRect(x: 16, y: 210, width: bounds.width - 32, height: 30)
        statusPopup.frame = NSRect(x: 16, y: 247, width: bounds.width - 32, height: 30)
        columnsButton.frame = NSRect(x: 16, y: bounds.height - 38, width: 150, height: 28)
        diagnosticsButton.frame = NSRect(x: 174, y: bounds.height - 38, width: 112, height: 28)
        resetButton.frame = NSRect(x: bounds.width - 82, y: bounds.height - 38, width: 66, height: 28)
    }

    private func configure(_ popup: NSPopUpButton, role: String, options: [(String, String)], selected: String) {
        popup.removeAllItems()
        for option in options {
            popup.addItem(withTitle: option.0)
            popup.lastItem?.representedObject = option.1
        }
        popup.identifier = NSUserInterfaceItemIdentifier(role)
        popup.target = self
        switch role {
        case "view":
            popup.action = #selector(viewChanged(_:))
        case "group":
            popup.action = #selector(groupChanged(_:))
        case "sort":
            popup.action = #selector(sortChanged(_:))
        default:
            popup.action = #selector(filterChanged(_:))
        }
        popup.font = .systemFont(ofSize: 12, weight: .medium)
        popup.controlSize = .regular
        popup.isBordered = false
        popup.wantsLayer = true
        popup.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.010).cgColor
        popup.layer?.borderColor = NSColor.white.withAlphaComponent(0.020).cgColor
        popup.layer?.borderWidth = 0.5
        popup.layer?.cornerRadius = 9
        if let item = popup.itemArray.first(where: { ($0.representedObject as? String) == selected }) {
            popup.select(item)
        } else {
            popup.selectItem(at: 0)
        }
    }

    @objc private func filterChanged(_ sender: NSPopUpButton) {
        let kind = CockpitFilterKind(raw: sender.identifier?.rawValue)
        let value = sender.selectedItem?.representedObject as? String ?? ""
        onFilter(kind, value)
    }

    @objc private func viewChanged(_ sender: NSPopUpButton) {
        let value = sender.selectedItem?.representedObject as? String ?? ""
        onView(value)
    }

    @objc private func groupChanged(_ sender: NSPopUpButton) {
        let value = sender.selectedItem?.representedObject as? String ?? ""
        onGroup(value)
    }

    @objc private func sortChanged(_ sender: NSPopUpButton) {
        let value = sender.selectedItem?.representedObject as? String ?? ""
        onSort(value)
    }

    @objc private func columnsPressed() {
        onColumns()
    }

    @objc private func diagnosticsPressed() {
        onDiagnostics()
    }

    @objc private func resetPressed() {
        onReset()
    }
}

final class CockpitColumnsPanel: NSView, NSSearchFieldDelegate {
    private let title = CockpitUI.label("Columns", size: 12, weight: .bold, color: CockpitTokens.Color.text)
    private let hint = CockpitUI.label("Work stays visible; use controls or drag headers", size: 10.5, weight: .medium, color: CockpitTokens.Color.muted)
    private let search = NSSearchField()
    private let scroll = CockpitEdgeScrollView()
    private let list = CockpitFlippedView()
    private let showAllButton = CockpitUI.button("Show all")
    private let defaultsButton = CockpitUI.button("Defaults")
    private let fitButton = CockpitUI.button("Widths")
    private let resetButton = CockpitUI.button("Reset")
    private var columns: [CockpitColumn]
    private var checks: [NSButton] = []
    private var widthFields: [NSTextField] = []
    private var moveUpButtons: [NSButton] = []
    private var moveDownButtons: [NSButton] = []
    private var onToggle: (String, Bool) -> Void
    private var onShowAll: () -> Void
    private var onDefaults: () -> Void
    private var onFit: () -> Void
    private var onReset: () -> Void
    private var onWidth: (String, Int) -> Void
    private var onMove: (String, Int) -> Void

    init(
        columns: [CockpitColumn],
        onToggle: @escaping (String, Bool) -> Void,
        onShowAll: @escaping () -> Void,
        onDefaults: @escaping () -> Void,
        onFit: @escaping () -> Void,
        onReset: @escaping () -> Void,
        onWidth: @escaping (String, Int) -> Void,
        onMove: @escaping (String, Int) -> Void
    ) {
        self.columns = columns.sorted(by: { $0.displayOrder < $1.displayOrder })
        self.onToggle = onToggle
        self.onShowAll = onShowAll
        self.onDefaults = onDefaults
        self.onFit = onFit
        self.onReset = onReset
        self.onWidth = onWidth
        self.onMove = onMove
        super.init(frame: NSRect(x: 0, y: 0, width: 352, height: 526))
        wantsLayer = true
        layer?.backgroundColor = CockpitTokens.Color.panel2.withAlphaComponent(0.98).cgColor
        addSubview(title)
        addSubview(hint)
        search.placeholderString = "Find columns"
        search.focusRingType = .none
        search.target = self
        search.action = #selector(searchChanged)
        search.delegate = self
        addSubview(search)
        scroll.hasVerticalScroller = true
        scroll.documentView = list
        CockpitScrollChrome.apply(to: scroll)
        addSubview(scroll)
        for button in [showAllButton, defaultsButton, fitButton, resetButton] {
            addSubview(button)
        }
        showAllButton.target = self
        showAllButton.action = #selector(showAllPressed)
        defaultsButton.target = self
        defaultsButton.action = #selector(defaultsPressed)
        fitButton.target = self
        fitButton.action = #selector(fitPressed)
        fitButton.toolTip = "Restore default column widths"
        resetButton.target = self
        resetButton.action = #selector(resetPressed)
        rebuildChecks()
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    override func layout() {
        title.frame = NSRect(x: 16, y: 14, width: bounds.width - 32, height: 18)
        hint.frame = NSRect(x: 16, y: 34, width: bounds.width - 32, height: 16)
        search.frame = NSRect(x: 16, y: 58, width: bounds.width - 32, height: 28)
        let buttonY = bounds.height - 42
        let buttonGap: CGFloat = 6
        let buttonWidth = floor((bounds.width - 32 - buttonGap * 3) / 4)
        showAllButton.frame = NSRect(x: 16, y: buttonY, width: buttonWidth, height: 28)
        defaultsButton.frame = NSRect(x: showAllButton.frame.maxX + buttonGap, y: buttonY, width: buttonWidth, height: 28)
        fitButton.frame = NSRect(x: defaultsButton.frame.maxX + buttonGap, y: buttonY, width: buttonWidth, height: 28)
        resetButton.frame = NSRect(x: fitButton.frame.maxX + buttonGap, y: buttonY, width: buttonWidth, height: 28)
        scroll.frame = NSRect(x: 0, y: 96, width: bounds.width, height: max(80, buttonY - 104))
        layoutChecks()
    }

    private func rebuildChecks() {
        checks.forEach { $0.removeFromSuperview() }
        widthFields.forEach { $0.removeFromSuperview() }
        moveUpButtons.forEach { $0.removeFromSuperview() }
        moveDownButtons.forEach { $0.removeFromSuperview() }
        checks = []
        widthFields = []
        moveUpButtons = []
        moveDownButtons = []
        for column in filteredColumns() {
            let label = column.label.isEmpty ? column.id.capitalized : column.label
            let check = NSButton(checkboxWithTitle: label, target: self, action: #selector(toggled(_:)))
            check.identifier = NSUserInterfaceItemIdentifier(column.id)
            check.state = column.isVisible ? .on : .off
            check.isEnabled = column.id != "work"
            check.font = .systemFont(ofSize: 12, weight: .medium)
            check.contentTintColor = CockpitTokens.Color.text
            check.toolTip = "\(column.id) | width \(column.width)"
            list.addSubview(check)
            checks.append(check)

            let widthField = columnWidthField(for: column)
            list.addSubview(widthField)
            widthFields.append(widthField)

            let up = moveButton(symbol: "chevron.left", id: column.id, title: "Move \(label) left", action: #selector(moveUpPressed(_:)))
            let down = moveButton(symbol: "chevron.right", id: column.id, title: "Move \(label) right", action: #selector(moveDownPressed(_:)))
            up.isEnabled = columns.first?.id != column.id
            down.isEnabled = columns.last?.id != column.id
            list.addSubview(up)
            list.addSubview(down)
            moveUpButtons.append(up)
            moveDownButtons.append(down)
        }
        layoutChecks()
    }

    private func layoutChecks() {
        let width = max(1, scroll.contentSize.width)
        var y: CGFloat = 8
        for idx in checks.indices {
            let check = checks[idx]
            check.frame = NSRect(x: 16, y: y, width: max(80, width - 156), height: 26)
            if widthFields.indices.contains(idx) {
                widthFields[idx].frame = NSRect(x: width - 132, y: y + 2, width: 56, height: 22)
            }
            if moveUpButtons.indices.contains(idx) {
                moveUpButtons[idx].frame = NSRect(x: width - 66, y: y + 2, width: 22, height: 22)
            }
            if moveDownButtons.indices.contains(idx) {
                moveDownButtons[idx].frame = NSRect(x: width - 38, y: y + 2, width: 22, height: 22)
            }
            y += 28
        }
        list.frame = NSRect(x: 0, y: 0, width: width, height: max(scroll.contentSize.height, y + 8))
    }

    private func columnWidthField(for column: CockpitColumn) -> NSTextField {
        let field = NSTextField(string: "\(column.width)")
        field.identifier = NSUserInterfaceItemIdentifier(column.id)
        field.target = self
        field.action = #selector(widthChanged(_:))
        field.font = .monospacedDigitSystemFont(ofSize: 11, weight: .semibold)
        field.alignment = .right
        field.focusRingType = .none
        field.isBordered = false
        field.drawsBackground = false
        field.wantsLayer = true
        field.layer?.backgroundColor = CockpitTokens.Color.panel.withAlphaComponent(0.34).cgColor
        field.layer?.borderColor = CockpitTokens.Color.line.withAlphaComponent(0.22).cgColor
        field.layer?.borderWidth = 1
        field.layer?.cornerRadius = 6
        field.textColor = CockpitTokens.Color.muted
        field.toolTip = "Column width in pixels (\(column.minWidth)-1800)"
        return field
    }

    private func moveButton(symbol: String, id: String, title: String, action: Selector) -> NSButton {
        let button = NSButton()
        button.identifier = NSUserInterfaceItemIdentifier(id)
        button.target = self
        button.action = action
        button.title = ""
        button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: title)
        button.imagePosition = .imageOnly
        button.isBordered = false
        button.toolTip = title
        button.contentTintColor = CockpitTokens.Color.muted
        button.wantsLayer = true
        button.layer?.cornerRadius = 5
        button.layer?.backgroundColor = CockpitTokens.Color.panel.withAlphaComponent(0.24).cgColor
        button.layer?.borderColor = CockpitTokens.Color.line2.withAlphaComponent(0.18).cgColor
        button.layer?.borderWidth = 1
        return button
    }

    private func filteredColumns() -> [CockpitColumn] {
        let query = search.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !query.isEmpty else { return columns }
        return columns.filter { column in
            column.id.lowercased().contains(query) || column.label.lowercased().contains(query)
        }
    }

    @objc private func toggled(_ sender: NSButton) {
        guard let id = sender.identifier?.rawValue else { return }
        if let idx = columns.firstIndex(where: { $0.id == id }) {
            columns[idx].isVisible = sender.state == .on
        }
        onToggle(id, sender.state == .on)
    }

    @objc private func moveUpPressed(_ sender: NSButton) {
        moveColumn(sender, delta: -1)
    }

    @objc private func moveDownPressed(_ sender: NSButton) {
        moveColumn(sender, delta: 1)
    }

    private func moveColumn(_ sender: NSButton, delta: Int) {
        guard let id = sender.identifier?.rawValue,
              let from = columns.firstIndex(where: { $0.id == id }) else { return }
        let to = from + delta
        guard columns.indices.contains(to) else { return }
        columns.swapAt(from, to)
        for idx in columns.indices {
            columns[idx].displayOrder = (idx + 1) * 10
        }
        rebuildChecks()
        onMove(id, delta)
    }

    @objc private func widthChanged(_ sender: NSTextField) {
        guard let id = sender.identifier?.rawValue,
              let idx = columns.firstIndex(where: { $0.id == id }) else { return }
        let minWidth = columns[idx].minWidth
        let typed = Int(sender.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)) ?? columns[idx].width
        let next = min(1_800, max(minWidth, typed))
        columns[idx].width = next
        sender.stringValue = "\(next)"
        onWidth(id, next)
    }

    @objc private func searchChanged() {
        rebuildChecks()
    }

    func controlTextDidChange(_ obj: Notification) {
        rebuildChecks()
    }

    @objc private func showAllPressed() {
        for idx in columns.indices {
            columns[idx].isVisible = true
        }
        rebuildChecks()
        onShowAll()
    }

    @objc private func defaultsPressed() {
        let defaults = Dictionary(uniqueKeysWithValues: CockpitColumn.webDefaults.map { ($0.id, $0.isVisible) })
        for idx in columns.indices {
            if let visible = defaults[columns[idx].id] {
                columns[idx].isVisible = visible
            }
        }
        rebuildChecks()
        onDefaults()
    }

    @objc private func fitPressed() {
        onFit()
    }

    @objc private func resetPressed() {
        columns = CockpitColumn.webDefaults
        rebuildChecks()
        onReset()
    }
}

struct CockpitCommandPanelItem {
    var id: String
    var title: String
    var symbol: String?
    var enabled: Bool
    var tint: NSColor?
    var toolTip: String?

    init(id: String, title: String, symbol: String?, enabled: Bool, tint: NSColor? = nil, toolTip: String? = nil) {
        self.id = id
        self.title = title
        self.symbol = symbol
        self.enabled = enabled
        self.tint = tint
        self.toolTip = toolTip
    }
}

struct CockpitCommandPanelSection {
    var title: String
    var items: [CockpitCommandPanelItem]
}

final class CockpitCommandsPanel: NSView {
    private let sections: [CockpitCommandPanelSection]
    private let onSelect: (String) -> Void
    private var labels: [NSTextField] = []
    private var buttons: [NSButton] = []

    init(sections: [CockpitCommandPanelSection], onSelect: @escaping (String) -> Void) {
        self.sections = sections
        self.onSelect = onSelect
        super.init(frame: NSRect(x: 0, y: 0, width: 430, height: 438))
        wantsLayer = true
        layer?.backgroundColor = CockpitTokens.Color.panel2.withAlphaComponent(0.90).cgColor
        layer?.borderColor = CockpitTokens.Color.line.withAlphaComponent(0.22).cgColor
        layer?.borderWidth = 1
        layer?.cornerRadius = 14
        build()
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    private func build() {
        for section in sections {
            let label = CockpitUI.label(section.title, size: 10, weight: .bold, color: CockpitTokens.Color.faint)
            label.stringValue = section.title.uppercased()
            addSubview(label)
            labels.append(label)
            for item in section.items {
                let button = commandButton(item)
                addSubview(button)
                buttons.append(button)
            }
        }
    }

    private func commandButton(_ item: CockpitCommandPanelItem) -> NSButton {
        let button = CockpitButton(title: item.title, target: self, action: #selector(commandPressed(_:)))
        button.identifier = NSUserInterfaceItemIdentifier(item.id)
        button.font = .systemFont(ofSize: 11.5, weight: .semibold)
        button.contentTintColor = item.enabled
            ? (item.tint ?? CockpitTokens.Color.text.withAlphaComponent(0.94))
            : CockpitTokens.Color.faint.withAlphaComponent(0.55)
        button.isEnabled = item.enabled
        button.toolTip = item.toolTip
        button.alignment = .center
        button.imagePosition = .imageLeading
        if let symbol = item.symbol {
            button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: item.title)
        }
        button.layer?.backgroundColor = CockpitTokens.Color.panel.withAlphaComponent(item.enabled ? 0.075 : 0.032).cgColor
        button.layer?.borderColor = (item.tint ?? CockpitTokens.Color.line).withAlphaComponent(item.enabled ? 0.11 : 0.055).cgColor
        return button
    }

    override func layout() {
        let columns = 3
        let left: CGFloat = 14
        let gap: CGFloat = 8
        let buttonHeight: CGFloat = 27
        let buttonWidth = floor((bounds.width - left * 2 - gap * CGFloat(columns - 1)) / CGFloat(columns))
        var y: CGFloat = 14
        var labelIndex = 0
        var buttonIndex = 0
        for section in sections {
            labels[labelIndex].frame = NSRect(x: left, y: y, width: bounds.width - left * 2, height: 14)
            labelIndex += 1
            y += 19
            for idx in section.items.indices {
                let col = idx % columns
                let row = idx / columns
                let x = left + CGFloat(col) * (buttonWidth + gap)
                buttons[buttonIndex].frame = NSRect(x: x, y: y + CGFloat(row) * (buttonHeight + gap), width: buttonWidth, height: buttonHeight)
                buttonIndex += 1
            }
            let rows = max(1, Int(ceil(Double(section.items.count) / Double(columns))))
            y += CGFloat(rows) * (buttonHeight + gap) + 10
        }
    }

    @objc private func commandPressed(_ sender: NSButton) {
        guard let id = sender.identifier?.rawValue else { return }
        onSelect(id)
    }
}

enum CockpitUI {
    static func label(_ text: String, size: CGFloat, weight: NSFont.Weight = .regular, color: NSColor, align: NSTextAlignment = .left) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = .systemFont(ofSize: size, weight: weight)
        label.textColor = color
        label.alignment = align
        label.lineBreakMode = .byTruncatingTail
        label.cell?.usesSingleLineMode = true
        label.isBordered = false
        label.drawsBackground = false
        return label
    }

    static func button(_ title: String) -> NSButton {
        let button = CockpitButton(title: title, target: nil, action: nil)
        button.isBordered = false
        button.controlSize = .regular
        button.font = .systemFont(ofSize: 12, weight: .semibold)
        button.contentTintColor = CockpitTokens.Color.text
        return button
    }

    static func navButton(_ title: String, active: Bool) -> NSButton {
        let button = CockpitNavButton(title: title, active: active)
        button.isBordered = false
        button.controlSize = .regular
        button.font = .systemFont(ofSize: 12, weight: active ? .bold : .semibold)
        button.contentTintColor = active ? CockpitTokens.Color.text : CockpitTokens.Color.muted
        return button
    }

    static func configurePill(_ view: NSView, color: NSColor, alpha: CGFloat = 0.16, radius: CGFloat = 7) {
        view.wantsLayer = true
        view.layer?.backgroundColor = color.withAlphaComponent(alpha).cgColor
        view.layer?.borderColor = color.withAlphaComponent(0.32).cgColor
        view.layer?.borderWidth = 1
        view.layer?.cornerRadius = radius
    }
}

enum CockpitAssets {
    static func image(_ name: String) -> NSImage? {
        if let image = NSImage(named: name) { return image }
        if let url = Bundle.main.url(forResource: name, withExtension: "png", subdirectory: "Assets") {
            return NSImage(contentsOf: url)
        }
        return nil
    }
}

class CockpitButton: NSButton {
    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        commonInit()
    }

    convenience init(title: String, target: Any?, action: Selector?) {
        self.init(frame: .zero)
        self.title = title
        self.target = target as AnyObject?
        self.action = action
        commonInit()
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        commonInit()
    }

    private func commonInit() {
        isBordered = false
        wantsLayer = true
        layer?.cornerRadius = 8
        layer?.backgroundColor = NSColor.white.withAlphaComponent(0.004).cgColor
        layer?.borderWidth = 0.5
        layer?.borderColor = NSColor.white.withAlphaComponent(0.010).cgColor
        layer?.shadowColor = CockpitTokens.Color.blue.cgColor
        layer?.shadowOpacity = 0
        layer?.shadowRadius = 0
        layer?.shadowOffset = .zero
    }
}

final class CockpitNavButton: CockpitButton {
    private let activeLine = CALayer()
    private var isActive: Bool

    init(title: String, active: Bool) {
        self.isActive = active
        super.init(frame: .zero)
        self.title = title
        installActiveLine()
        updateActiveChrome()
    }

    required init?(coder: NSCoder) {
        self.isActive = false
        super.init(coder: coder)
        installActiveLine()
        updateActiveChrome()
    }

    func setActive(_ active: Bool) {
        guard active != isActive else { return }
        isActive = active
        updateActiveChrome()
        needsDisplay = true
    }

    override func layout() {
        super.layout()
        activeLine.frame = CGRect(x: 8, y: max(1, bounds.height - 5), width: max(0, bounds.width - 16), height: 3)
        activeLine.cornerRadius = 1.5
    }

    private func installActiveLine() {
        activeLine.backgroundColor = CockpitTokens.Color.glowBlue.withAlphaComponent(0.98).cgColor
        activeLine.shadowColor = CockpitTokens.Color.glowBlue.cgColor
        activeLine.shadowOpacity = 0.98
        activeLine.shadowRadius = 14
        activeLine.shadowOffset = .zero
        layer?.addSublayer(activeLine)
    }

    private func updateActiveChrome() {
        layer?.backgroundColor = isActive ? NSColor.white.withAlphaComponent(0.012).cgColor : NSColor.clear.cgColor
        layer?.borderColor = NSColor.clear.cgColor
        layer?.shadowOpacity = isActive ? 0.10 : 0
        activeLine.opacity = isActive ? 1 : 0
    }
}

final class CockpitSegmentedControl: NSView {
    var onSelection: ((Int) -> Void)?
    private let items: [String]
    private var buttons: [NSButton] = []
    var selectedIndex: Int = -1 {
        didSet {
            updateButtons()
            needsDisplay = true
        }
    }

    init(items: [String]) {
        self.items = items
        super.init(frame: .zero)
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor
        layer?.borderColor = NSColor.clear.cgColor
        layer?.borderWidth = 0
        layer?.cornerRadius = 10
        for (idx, item) in items.enumerated() {
            let button = NSButton(title: item, target: self, action: #selector(clicked(_:)))
            button.tag = idx
            button.isBordered = false
            button.font = .systemFont(ofSize: 12, weight: .semibold)
            button.contentTintColor = CockpitTokens.Color.muted
            button.wantsLayer = true
            addSubview(button)
            buttons.append(button)
        }
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    override func layout() {
        layer?.cornerRadius = min(8, bounds.height / 2)
        guard !buttons.isEmpty else { return }
        let w = bounds.width / CGFloat(buttons.count)
        for (idx, button) in buttons.enumerated() {
            button.frame = NSRect(x: CGFloat(idx) * w + 1, y: 1, width: max(1, w - 2), height: max(1, bounds.height - 2))
            button.layer?.cornerRadius = 6
        }
        updateButtons()
    }

    override func draw(_ dirtyRect: NSRect) {
        let bgRect = bounds.insetBy(dx: 0.5, dy: 0.5)
        drawGlass(in: bgRect, radius: min(11, bgRect.height / 2), strokeAlpha: 0.025, shadow: false)
        guard selectedIndex >= 0, selectedIndex < buttons.count, !buttons.isEmpty else { return }
        let segmentWidth = bounds.width / CGFloat(buttons.count)
        let rect = NSRect(
            x: CGFloat(selectedIndex) * segmentWidth + 2,
            y: 2,
            width: max(1, segmentWidth - 4),
            height: max(1, bounds.height - 4)
        )
        let selected = NSBezierPath(roundedRect: rect, xRadius: 7, yRadius: 7)
        let selectedGradient = NSGradient(colors: [
            NSColor.white.withAlphaComponent(0.040),
            CockpitTokens.Color.glowBlue.withAlphaComponent(0.045),
            NSColor.black.withAlphaComponent(0.030),
        ])
        selectedGradient?.draw(in: selected, angle: -90)

        NSGraphicsContext.saveGraphicsState()
        let shadow = NSShadow()
        shadow.shadowColor = CockpitTokens.Color.glowBlue.withAlphaComponent(0.96)
        shadow.shadowBlurRadius = 15
        shadow.shadowOffset = .zero
        shadow.set()
        let line = NSBezierPath(roundedRect: NSRect(x: rect.minX + 7, y: rect.maxY - 4, width: max(0, rect.width - 14), height: 3), xRadius: 1.5, yRadius: 1.5)
        CockpitTokens.Color.glowBlue.withAlphaComponent(0.98).setFill()
        line.fill()
        NSGraphicsContext.restoreGraphicsState()
    }

    @objc private func clicked(_ sender: NSButton) {
        selectedIndex = sender.tag
        onSelection?(sender.tag)
    }

    private func updateButtons() {
        for button in buttons {
            let selected = button.tag == selectedIndex
            button.contentTintColor = selected ? CockpitTokens.Color.text : CockpitTokens.Color.muted
            button.layer?.backgroundColor = NSColor.clear.cgColor
            button.layer?.borderWidth = 0
            button.layer?.borderColor = NSColor.clear.cgColor
            button.layer?.shadowColor = CockpitTokens.Color.selectionStroke.cgColor
            button.layer?.shadowOpacity = 0
            button.layer?.shadowRadius = 0
            button.layer?.shadowOffset = .zero
        }
    }
}
