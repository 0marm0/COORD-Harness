import Foundation

enum CockpitKeyboardCommand: Equatable {
    case refresh
    case focusSearch
    case toggleInspector
    case toggleDiagnostics
    case openCommands
    case expandAll
    case collapseAll
    case reverseSort
    case clearSearchOrClosePanel
}

struct CockpitKeyboardShortcut: Equatable {
    var key: String
    var command: Bool = false
    var shift: Bool = false
    var option: Bool = false
    var control: Bool = false
}

enum CockpitKeyboardCommandResolver {
    static func resolve(_ shortcut: CockpitKeyboardShortcut) -> CockpitKeyboardCommand? {
        let key = shortcut.key.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if key == "escape" { return .clearSearchOrClosePanel }
        guard shortcut.command, !shortcut.option, !shortcut.control else { return nil }
        switch (key, shortcut.shift) {
        case ("r", false):
            return .refresh
        case ("f", false):
            return .focusSearch
        case ("i", false):
            return .toggleInspector
        case ("d", false):
            return .toggleDiagnostics
        case ("k", false):
            return .openCommands
        case ("e", false):
            return .expandAll
        case ("e", true):
            return .collapseAll
        case ("s", true):
            return .reverseSort
        default:
            return nil
        }
    }
}
