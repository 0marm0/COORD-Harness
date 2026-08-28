import AppKit

let app = NSApplication.shared
let delegate = CockpitLauncherAppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
