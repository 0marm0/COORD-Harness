import AppKit
import QuartzCore
import SwiftUI

enum PopoverNavigationDestination: Equatable {
    case main
    case settings
    case providerAccounts
    case usage
}

struct PopoverNavigationState {
    private(set) var destination: PopoverNavigationDestination = .main

    var isMain: Bool { destination == .main }

    mutating func showSettings() { destination = .settings }
    mutating func showProviderAccounts() { destination = .providerAccounts }
    mutating func showUsage() { destination = .usage }
    mutating func reset() { destination = .main }
}

final class PopoverController {

    var onWantsRefresh: (() -> Void)?
    var onConfig: ((Config) -> Void)?
    var isShown: Bool { popover.isShown || detachedWindow?.isVisible == true }

    private var config: Config
    private var navigation = PopoverNavigationState()
    private let popover = NSPopover()
    private let popoverViewController = NSViewController()
    private let glass = GlassBackground(frame: .zero)
    private var detachedWindow: NSWindow?
    private lazy var detachedWindowDelegate = DetachedPanelWindowDelegate { [weak self] in
        self?.detachedWindowDidClose()
    }


    private var clickMonitor: Any?
    private var content = FlippedView()
    private var boardScrollView: NSScrollView?
    private var pinnedFooter: FooterView?
    private var anchorScreenFrame: NSRect?
    private let stack: ContentStack
    private let usageStore: InstalledUsageStore
    private var renderedStateSignature: Data?

    init(config: Config, usageStore: InstalledUsageStore) {
        self.config = config
        self.stack = AppKitContentStack(config: config)
        self.usageStore = usageStore

        glass.configure(material: config.glassMaterial, alpha: config.glassAlpha)
        glass.autoresizingMask = [.width, .height]
        content.autoresizingMask = [.width]
        glass.addSubview(content)
        popoverViewController.view = glass

        popover.contentViewController = popoverViewController
        popover.behavior = .applicationDefined
        popover.animates = false
        popover.appearance = NSAppearance(named: .vibrantDark)
        popover.delegate = popoverDelegate
        stack.onAction = { [weak self] action in self?.perform(action) }

        stack.onRelayout = { [weak self] in
            guard let self, let state = self.lastState else { return }
            self.invalidateBoardRender()
            self.render(state)
        }
    }

    private var lastState: MenubarState?

    private func forceUsageRefresh() {
        Task { @MainActor [weak self] in
            guard let self else { return }
            await self.usageStore.refresh(force: true)
        }
    }


    func show(relativeTo button: NSStatusBarButton) {
        resetNavigationToMain(renderMain: true)
        stack.resetSystemTelemetryDisclosure()
        if let window = button.window {
            let inWindow = button.convert(button.bounds, to: nil)
            anchorScreenFrame = window.convertToScreen(inWindow)
        }
        forceUsageRefresh()
        if config.panelDetached {
            showDetachedWindow()
            return
        }
        attachGlassToPopover()
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        installClickMonitor()
    }

    func showSettings(relativeTo button: NSStatusBarButton) {
        if !isShown { show(relativeTo: button) }
        showSettings()
    }

    func close() {
        resetNavigationToMain(renderMain: false)
        stack.resetSystemTelemetryDisclosure()
        removeClickMonitor()
        popover.close()
        detachedWindow?.orderOut(nil)
        (NSApp.delegate as? AppDelegate)?.popoverDidClose()
    }


    private func installClickMonitor() {
        removeClickMonitor()
        guard !config.stayOpen else { return }
        clickMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown, .otherMouseDown]) { [weak self] _ in
            guard let self, self.popover.isShown else { return }
            guard PopoverClickPolicy.shouldCloseGlobalClick(
                location: NSEvent.mouseLocation,
                anchorFrame: self.anchorScreenFrame,
                stayOpen: self.config.stayOpen
            ) else { return }
            self.close()
        }
    }
    private func removeClickMonitor() {
        if let m = clickMonitor { NSEvent.removeMonitor(m); clickMonitor = nil }
    }


    func render(_ state: MenubarState) {
        lastState = state
        guard navigation.isMain else { return }
        let signature = stableRenderSignature(for: state)
        guard renderedStateSignature != signature else { return }
        swapIn { newContent in stack.build(state: state, into: newContent) }
        renderedStateSignature = signature
    }

    func updateSystemTelemetry(_ snapshot: SystemTelemetrySnapshot?) {
        stack.updateSystemTelemetry(snapshot)
    }

    func updateUsage(_ state: UsageDashboardState) {
        stack.updateUsage(state)
        guard isShown, navigation.isMain, let lastState else { return }
        invalidateBoardRender()
        render(lastState)
    }


    private func swapIn(_ build: (FlippedView) -> Void) {
        let previousOrigin = boardScrollView?.contentView.bounds.origin ?? .zero
        let previousAnchor = captureScrollAnchor(document: content, origin: previousOrigin)
        let newContent = FlippedView()
        newContent.autoresizingMask = [.width]
        build(newContent)
        let detachedVisible = detachedWindow?.isVisible == true
        let detachedSize = detachedVisible ? detachedWindow?.contentView?.bounds.size : nil
        let w = max(Tokens.Layout.popoverWidth, detachedSize?.width ?? Tokens.Layout.popoverWidth)
        let docH = max(1, stack.contentHeight)
        let footerH: CGFloat = 30
        let h = detachedVisible
            ? max(260, detachedSize?.height ?? min(docH + footerH, availablePopoverHeight()))
            : min(docH + footerH, availablePopoverHeight())
        let scrollH = max(1, h - footerH)
        newContent.frame = NSRect(x: 0, y: 0, width: w, height: docH)

        let scroll = NSScrollView(frame: NSRect(x: 0, y: footerH, width: w, height: scrollH))
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = false
        scroll.hasHorizontalScroller = false
        scroll.autohidesScrollers = true
        scroll.borderType = .noBorder
        scroll.documentView = newContent
        let maxScrollY = max(0, docH - scrollH)
        let anchorOrigin = restoredScrollOrigin(
            anchor: previousAnchor,
            document: newContent,
            fallback: previousOrigin
        )
        let restoredOrigin = headerSafeScrollOrigin(
            proposed: anchorOrigin,
            document: newContent,
            maxScrollY: maxScrollY
        )
        scroll.contentView.scroll(to: restoredOrigin)
        scroll.reflectScrolledClipView(scroll.contentView)

        let footer = FooterView { [weak self] in self?.perform($0) }
        footer.frame = NSRect(x: 0, y: 0, width: w, height: footerH)
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        glass.addSubview(scroll)
        glass.addSubview(footer)
        boardScrollView?.removeFromSuperview()
        pinnedFooter?.removeFromSuperview()
        content.removeFromSuperview()
        content = newContent
        boardScrollView = scroll
        pinnedFooter = footer
        if detachedVisible {
            glass.frame = NSRect(origin: .zero, size: NSSize(width: w, height: h))
            detachedWindow?.minSize = NSSize(width: Tokens.Layout.popoverWidth, height: 260)
        } else {
            popover.contentSize = NSSize(width: w, height: h)
        }
        CATransaction.commit()
    }

    private struct BoardScrollAnchor {
        let identifier: NSUserInterfaceItemIdentifier
        let offset: CGFloat
    }

    private func captureScrollAnchor(document: NSView, origin: NSPoint) -> BoardScrollAnchor? {
        guard origin.y > 0 else { return nil }
        for view in document.subviews where view.frame.maxY > origin.y {
            if let identifier = view.identifier {
                return BoardScrollAnchor(identifier: identifier, offset: origin.y - view.frame.minY)
            }
        }
        return nil
    }

    private func restoredScrollOrigin(
        anchor: BoardScrollAnchor?,
        document: NSView,
        fallback: NSPoint
    ) -> NSPoint {
        guard let anchor,
              let view = document.subviews.first(where: { $0.identifier == anchor.identifier })
        else { return fallback }
        return NSPoint(x: 0, y: view.frame.minY + anchor.offset)
    }

    private func stableRenderSignature(for state: MenubarState) -> Data? {
        var stable = state
        stable.ts = nil
        stable.diagnostics?.projectionTs = nil
        stable.healthSummary?.generatedAt = nil
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        return try? encoder.encode(stable)
    }

    private func invalidateBoardRender() {
        renderedStateSignature = nil
    }

    private func resetNavigationToMain(renderMain: Bool) {
        navigation.reset()
        invalidateBoardRender()
        if renderMain, let state = lastState { render(state) }
    }

    private func headerSafeScrollOrigin(proposed: NSPoint, document: NSView, maxScrollY: CGFloat) -> NSPoint {
        let clampedY = min(max(proposed.y, 0), maxScrollY)
        let headerRevealPadding: CGFloat = 10
        var safeY = clampedY
        for view in document.subviews where isSectionHeaderView(view) {
            let top = view.frame.minY
            let bottom = view.frame.maxY
            if clampedY > top && clampedY <= bottom + headerRevealPadding {
                safeY = min(safeY, max(0, top - 1))
            }
        }
        return NSPoint(x: 0, y: min(max(safeY, 0), maxScrollY))
    }

    private func isSectionHeaderView(_ view: NSView) -> Bool {
        view is SectionHeader || view is NextUpHeader || view is LaneHeader
    }


    private func showSettings() {
        navigation.showSettings()
        invalidateBoardRender()
        let v = SettingsView(config: config)
        v.onClose = { [weak self] in self?.exitSettings() }
        v.onChange = { [weak self] cfg in
            self?.applyConfig(cfg)
            self?.onConfig?(cfg)
        }
        v.onSave = { [weak self] cfg in
            self?.applyConfig(cfg)
            self?.onConfig?(cfg)
            self?.exitSettings()
        }
        v.onQuit = { NSApp.terminate(nil) }
        v.onOpenProviderAccounts = { [weak self] in self?.showProviderAccountsFromSettings() }
        let newContent = FlippedView(); newContent.autoresizingMask = [.width]
        newContent.addSubview(v)
        let w = Tokens.Layout.popoverWidth, h = v.contentHeight
        newContent.frame = NSRect(x: 0, y: 0, width: w, height: h)
        let viewportHeight = min(h, availablePopoverHeight())
        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: w, height: viewportHeight))
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = h > viewportHeight
        scroll.hasHorizontalScroller = false
        scroll.autohidesScrollers = true
        scroll.borderType = .noBorder
        scroll.documentView = newContent
        CATransaction.begin(); CATransaction.setDisableActions(true)
        glass.addSubview(scroll)
        boardScrollView?.removeFromSuperview(); boardScrollView = nil
        pinnedFooter?.removeFromSuperview(); pinnedFooter = nil
        content.removeFromSuperview(); content = newContent
        boardScrollView = scroll
        popover.contentSize = NSSize(width: w, height: viewportHeight)
        CATransaction.commit()
    }

    private func availablePopoverHeight() -> CGFloat {
        let fallback = Tokens.Layout.maxPopoverHeight
        guard let screen = NSScreen.main ?? NSApp.mainWindow?.screen else { return fallback }
        let visible = screen.visibleFrame
        let belowAnchor = anchorScreenFrame.map { max(0, $0.minY - visible.minY - 12) } ?? fallback
        let usable = belowAnchor > 360 ? belowAnchor : max(360, visible.height - 120)
        return min(fallback, usable)
    }
    private func exitSettings() {
        resetNavigationToMain(renderMain: true)
    }

    private func showProviderAccountsFromSettings() {
        navigation.showProviderAccounts()
        invalidateBoardRender()
        let detachedVisible = detachedWindow?.isVisible == true
        let detachedSize = detachedVisible ? detachedWindow?.contentView?.bounds.size : nil
        let width = detachedVisible ? max(500, detachedSize?.width ?? 520) : 500
        let height = detachedVisible
            ? max(420, detachedSize?.height ?? min(620, availablePopoverHeight()))
            : min(620, availablePopoverHeight())
        let newContent = FlippedView(frame: NSRect(x: 0, y: 0, width: width, height: height))
        let hosting = NSHostingView(
            rootView: UsageAccountSettingsView(
                baseURL: HarnessEndpoint.url("/"),
                onOpenCORDSettings: { [weak self] in self?.showSettings() },
                onDone: { [weak self] in self?.showSettings() }
            )
        )
        hosting.wantsLayer = true
        hosting.layer?.backgroundColor = NSColor.clear.cgColor
        hosting.frame = newContent.bounds
        hosting.autoresizingMask = [.width, .height]
        newContent.addSubview(hosting)

        CATransaction.begin(); CATransaction.setDisableActions(true)
        glass.addSubview(newContent)
        boardScrollView?.removeFromSuperview(); boardScrollView = nil
        pinnedFooter?.removeFromSuperview(); pinnedFooter = nil
        content.removeFromSuperview(); content = newContent
        if detachedVisible {
            glass.frame = NSRect(origin: .zero, size: NSSize(width: width, height: height))
        } else {
            popover.contentSize = NSSize(width: width, height: height)
        }
        CATransaction.commit()
    }

    private func showUsage() {
        navigation.showUsage()
        invalidateBoardRender()
        forceUsageRefresh()
        let detachedVisible = detachedWindow?.isVisible == true
        let detachedSize = detachedVisible ? detachedWindow?.contentView?.bounds.size : nil
        let width = max(500, detachedSize?.width ?? 500)
        let height = detachedVisible
            ? max(520, detachedSize?.height ?? 640)
            : min(680, availablePopoverHeight())
        let newContent = FlippedView(frame: NSRect(x: 0, y: 0, width: width, height: height))
        let hosting = NSHostingView(
            rootView: InstalledUsageDashboardView(
                compact: !detachedVisible,
                managesRefresh: false,
                onClose: { [weak self] in self?.exitUsage() },
                onOpenSettings: { [weak self] in self?.showSettingsFromUsage() },
                store: usageStore
            )
        )
        hosting.wantsLayer = true
        hosting.layer?.backgroundColor = NSColor.clear.cgColor
        hosting.frame = newContent.bounds
        hosting.autoresizingMask = [.width, .height]
        newContent.addSubview(hosting)

        CATransaction.begin(); CATransaction.setDisableActions(true)
        glass.addSubview(newContent)
        boardScrollView?.removeFromSuperview(); boardScrollView = nil
        pinnedFooter?.removeFromSuperview(); pinnedFooter = nil
        content.removeFromSuperview(); content = newContent
        if detachedVisible {
            glass.frame = NSRect(origin: .zero, size: NSSize(width: width, height: height))
        } else {
            popover.contentSize = NSSize(width: width, height: height)
        }
        CATransaction.commit()
    }

    private func exitUsage() {
        resetNavigationToMain(renderMain: true)
    }

    private func showSettingsFromUsage() {
        showSettings()
    }

    func applyConfig(_ cfg: Config) {
        let wasDetached = config.panelDetached
        invalidateBoardRender()
        config = cfg
        stack.updateConfig(cfg)
        glass.configure(material: cfg.glassMaterial, alpha: cfg.glassAlpha)
        detachedWindow?.level = MenuBarPanelWindowPolicy.level(alwaysOnTop: cfg.panelAlwaysOnTop)

        if popover.isShown { installClickMonitor() }
        if wasDetached != cfg.panelDetached, let state = lastState {
            resetNavigationToMain(renderMain: false)
            if cfg.panelDetached {
                popover.close()
                showDetachedWindow()
            } else {
                detachedWindow?.orderOut(nil)
                attachGlassToPopover()
            }
            render(state)
        }
    }

    private func perform(_ action: PanelAction) {
        switch action {
        case .setMode(let m):           HarnessControl.setMode(m)
        case .pauseAll(let ids):        HarnessControl.pauseAll(jobIds: ids)
        case .pauseResume(let id, let resume):
                                        HarnessControl.request(job: id, action: resume ? "resume" : "pause")
        case .kill(let id):             HarnessControl.request(job: id, action: "kill")
        case .refresh:                  forceUsageRefresh()
        case .openSettings:             showSettings()
        case .openUsage:                showUsage()
        case .toggleDetachedPanel:       toggleDetachedPanel()
        case .setUsagePeekCollapsed(let collapsed): setUsagePeekCollapsed(collapsed)
        case .openCockpit:              (NSApp.delegate as? AppDelegate)?.openCockpitWindow()
        case .openDashboard(let path):  NSWorkspace.shared.open(URL(string: "\(HarnessEndpoint.base)\(path)")!)
        case .taskAction(let job, let action, let assignee):
                                        HarnessControl.taskAction(job: job, action: action, assignee: assignee)
        case .handoff(let job, let to, let task, let contextRef):
                                        HarnessControl.handoff(job: job, to: to, task: task, contextRef: contextRef)
        case .capability(let job, let action, let contextRef):
                                        guard !CapabilityResultCache.isLoading(job: job, action: action) else { return }
                                        CapabilityResultCache.markLoading(job: job, action: action)
                                        if let s = lastState { render(s) }
                                        HarnessControl.capability(job: job, action: action, contextRef: contextRef) { [weak self] summary in
                                            DispatchQueue.main.async {
                                                guard let self else { return }
                                                if let s = self.lastState { self.render(s) }
                                                self.openHarnessCapability(job: job, action: action, resultId: summary.resultId, autoExecute: false)
                                            }
                                        }
        }

        onWantsRefresh?()
    }

    private func toggleDetachedPanel() {
        var next = config
        next.panelDetached.toggle()
        next.save()
        applyConfig(next)
        onConfig?(next)
    }

    private func setUsagePeekCollapsed(_ collapsed: Bool) {
        guard config.usagePeekCollapsed != collapsed else { return }
        var next = config
        next.usagePeekCollapsed = collapsed
        next.save()
        applyConfig(next)
        onConfig?(next)
        if let state = lastState { render(state) }
    }

    private func showDetachedWindow() {
        removeClickMonitor()
        popover.close()
        attachGlassToDetachedWindow()
        let window = ensureDetachedWindow()
        window.level = MenuBarPanelWindowPolicy.level(alwaysOnTop: config.panelAlwaysOnTop)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        if let state = lastState {
            render(state)
        }
    }

    private func ensureDetachedWindow() -> NSWindow {
        if let detachedWindow { return detachedWindow }
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: 620),
            styleMask: [.titled, .closable, .resizable, .utilityWindow],
            backing: .buffered,
            defer: false
        )
        window.title = "Coord Harness"
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: Tokens.Layout.popoverWidth, height: 260)
        window.collectionBehavior = [.moveToActiveSpace]
        window.delegate = detachedWindowDelegate
        detachedWindow = window
        return window
    }

    private func attachGlassToPopover() {
        if detachedWindow?.contentView === glass {
            detachedWindow?.contentView = nil
        }
        popoverViewController.view = glass
        popover.contentViewController = popoverViewController
    }

    private func attachGlassToDetachedWindow() {
        let window = ensureDetachedWindow()
        window.contentView = glass
        glass.frame = window.contentView?.bounds ?? NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: 620)
        glass.autoresizingMask = [.width, .height]
    }

    private func detachedWindowDidClose() {
        resetNavigationToMain(renderMain: false)
        stack.resetSystemTelemetryDisclosure()
        if config.panelDetached {
            var next = config
            next.panelDetached = false
            next.save()
            config = next
            onConfig?(next)
        }
        (NSApp.delegate as? AppDelegate)?.popoverDidClose()
    }

    private func openHarnessCapability(job: String, action: String, resultId: String? = nil, autoExecute: Bool = false) {
        guard var c = URLComponents(string: "\(HarnessEndpoint.base)/harness") else { return }
        var items = [
            URLQueryItem(name: "work", value: job),
            URLQueryItem(name: "cap", value: action),
        ]
        if let resultId, !resultId.isEmpty {
            items.append(URLQueryItem(name: "result_id", value: resultId))
        } else if autoExecute {
            items.append(URLQueryItem(name: "execute", value: "1"))
        } else {
            items.append(URLQueryItem(name: "execute", value: "0"))
        }
        c.queryItems = items
        if let url = c.url { NSWorkspace.shared.open(url) }
    }


    private lazy var popoverDelegate = PopoverDelegate { [weak self] in
        self?.resetNavigationToMain(renderMain: false)
        self?.stack.resetSystemTelemetryDisclosure()
        (NSApp.delegate as? AppDelegate)?.popoverDidClose()
    }
}


final class FlippedView: NSView { override var isFlipped: Bool { true } }

final class PopoverDelegate: NSObject, NSPopoverDelegate {
    private let onClose: () -> Void
    init(_ onClose: @escaping () -> Void) { self.onClose = onClose }
    func popoverDidClose(_ n: Notification) { onClose() }
}

final class DetachedPanelWindowDelegate: NSObject, NSWindowDelegate {
    private let onClose: () -> Void
    init(_ onClose: @escaping () -> Void) {
        self.onClose = onClose
    }
    func windowWillClose(_ notification: Notification) {
        onClose()
    }
}


/// The NSPopover owns the vibrant-dark glass. Keep this host transparent so COORD surfaces
/// render one continuous material instead of stacking a second effect view and gray border.
final class GlassBackground: NSView {
    func configure(material name: String, alpha: Double) {
        wantsLayer = true
        let a = CGFloat(max(0, min(1, alpha)))
        layer?.backgroundColor = (a < 0.01 ? NSColor.clear : NSColor(calibratedWhite: 0, alpha: a)).cgColor
    }
}
