import AppKit

enum PopoverClickPolicy {
    static func shouldCloseGlobalClick(location: NSPoint, anchorFrame: NSRect?, stayOpen: Bool) -> Bool {
        if stayOpen { return false }
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
