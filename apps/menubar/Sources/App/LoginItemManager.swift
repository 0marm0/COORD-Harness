import Foundation
import ServiceManagement


enum LoginItemManager {
    static var isEnabled: Bool {
        if #available(macOS 13.0, *) { return SMAppService.mainApp.status == .enabled }
        return false
    }


    @discardableResult
    static func apply(_ enabled: Bool) -> Bool {
        guard #available(macOS 13.0, *) else { return false }
        do {
            if enabled {
                if SMAppService.mainApp.status != .enabled { try SMAppService.mainApp.register() }
            } else {
                if SMAppService.mainApp.status == .enabled { try SMAppService.mainApp.unregister() }
            }
            return SMAppService.mainApp.status != .requiresApproval
        } catch {
            NSLog("[coordharness-mac] login-item \(enabled ? "register" : "unregister") failed: \(error)")
            return false
        }
    }
}
