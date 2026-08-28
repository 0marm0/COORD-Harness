import AppKit
import Carbon.HIToolbox


final class HotkeyManager {

    private var hotKeyRef: EventHotKeyRef?
    private var handlerRef: EventHandlerRef?
    private var onTrigger: (() -> Void)?
    private let signature: OSType = 0x6C69746E

    private static let keycodes: [String: UInt32] = [
        "comma": UInt32(kVK_ANSI_Comma), "period": UInt32(kVK_ANSI_Period),
        "space": UInt32(kVK_Space), "h": UInt32(kVK_ANSI_H), "j": UInt32(kVK_ANSI_J),
        "l": UInt32(kVK_ANSI_L), "m": UInt32(kVK_ANSI_M),
    ]

    func register(_ hk: Config.Hotkey, onTrigger: @escaping () -> Void) {
        self.onTrigger = onTrigger
        unregister()
        installHandlerIfNeeded()
        let keycode = Self.keycodes[hk.key] ?? UInt32(kVK_ANSI_Comma)
        var mask: UInt32 = 0
        for m in hk.mods {
            switch m.lowercased() {
            case "cmd", "command":  mask |= UInt32(cmdKey)
            case "opt", "option":   mask |= UInt32(optionKey)
            case "ctrl", "control": mask |= UInt32(controlKey)
            case "shift":           mask |= UInt32(shiftKey)
            default: break
            }
        }
        let id = EventHotKeyID(signature: signature, id: 1)
        RegisterEventHotKey(keycode, mask, id, GetApplicationEventTarget(), 0, &hotKeyRef)
    }


    func unregister() {
        if let r = hotKeyRef { UnregisterEventHotKey(r); hotKeyRef = nil }
    }


    func teardown() {
        unregister()
        if let h = handlerRef { RemoveEventHandler(h); handlerRef = nil }
    }

    private func installHandlerIfNeeded() {
        guard handlerRef == nil else { return }
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        let selfPtr = Unmanaged.passUnretained(self).toOpaque()
        InstallEventHandler(GetApplicationEventTarget(), { _, event, ctx -> OSStatus in
            guard let ctx = ctx else { return noErr }
            let mgr = Unmanaged<HotkeyManager>.fromOpaque(ctx).takeUnretainedValue()
            var hkID = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                              nil, MemoryLayout<EventHotKeyID>.size, nil, &hkID)
            DispatchQueue.main.async { mgr.onTrigger?() }
            return noErr
        }, 1, &spec, selfPtr, &handlerRef)
    }

    deinit { teardown() }
}
