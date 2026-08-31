import AppKit

enum PopoverClickPolicy {
    // Accessibility-driven status-item presses can deliver their synthetic
    // global mouse event about one second after AXPress opens the popover.
    // Keep that delayed opening event from being mistaken for an outside click.
    static let openingClickGrace: TimeInterval = 3.0

    static func shouldCloseGlobalClick(
        location: NSPoint,
        anchorFrame: NSRect?,
        stayOpen: Bool,
        openedAt: TimeInterval? = nil,
        now: TimeInterval = ProcessInfo.processInfo.systemUptime
    ) -> Bool {
        if stayOpen { return false }
        if let openedAt, now - openedAt < openingClickGrace { return false }
        if let anchorFrame, anchorFrame.insetBy(dx: -6, dy: -8).contains(location) {
            return false
        }
        return true
    }
}

enum MenuBarPanelWindowPolicy {
    static func level(alwaysOnTop: Bool) -> NSWindow.Level {
        alwaysOnTop ? .floating : .normal
    }
}
