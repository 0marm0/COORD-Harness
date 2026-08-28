import AppKit

final class CockpitLauncherAppDelegate: NSObject, NSApplicationDelegate {
    private lazy var cockpitWindow = CockpitWindowController()

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        openCockpitWindow()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag {
            openCockpitWindow()
        }
        return true
    }

    private func openCockpitWindow() {
        cockpitWindow.showWindow(nil)
    }
}
