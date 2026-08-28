import Foundation

enum CockpitRightPanelState: String, Codable, Equatable {
    case none
    case diagnostics
    case inspector
}

struct CockpitWindowFrame: Codable, Equatable {
    var x: Double
    var y: Double
    var width: Double
    var height: Double

    var isUsable: Bool {
        width >= 720 && height >= 480 && width.isFinite && height.isFinite && x.isFinite && y.isFinite
    }
}

struct CockpitWindowUIState: Codable, Equatable {
    var frame: CockpitWindowFrame?
    var rightPanel: CockpitRightPanelState
    var selectedRowKey: String?
    var quickFiltersPinned: Bool
    var viewSnapshot: CockpitViewSnapshot?
    var columns: [CockpitColumn]?
    var collapsedGroupKeys: [String]?
    var expandedRowKeys: [String]?
    var showSubline: Bool?

    static let defaultState = CockpitWindowUIState(
        frame: nil,
        rightPanel: .none,
        selectedRowKey: nil,
        quickFiltersPinned: false,
        viewSnapshot: nil,
        columns: nil,
        collapsedGroupKeys: nil,
        expandedRowKeys: nil,
        showSubline: nil
    )
}

final class CockpitWindowUIStateStore {
    private let defaults: UserDefaults
    private let key: String
    private let encoder = PropertyListEncoder()
    private let decoder = PropertyListDecoder()

    init(defaults: UserDefaults = .standard, key: String = "coordharness.nativeCockpit.windowState.v1") {
        self.defaults = defaults
        self.key = key
        encoder.outputFormat = .binary
    }

    func load() -> CockpitWindowUIState {
        guard let data = defaults.data(forKey: key),
              let state = try? decoder.decode(CockpitWindowUIState.self, from: data) else {
            return .defaultState
        }
        return state.frame?.isUsable == false
            ? CockpitWindowUIState(
                frame: nil,
                rightPanel: state.rightPanel,
                selectedRowKey: state.selectedRowKey,
                quickFiltersPinned: state.quickFiltersPinned,
                viewSnapshot: state.viewSnapshot,
                columns: state.columns,
                collapsedGroupKeys: state.collapsedGroupKeys,
                expandedRowKeys: state.expandedRowKeys,
                showSubline: state.showSubline
            )
            : state
    }

    func save(_ state: CockpitWindowUIState) {
        guard let data = try? encoder.encode(state) else { return }
        defaults.set(data, forKey: key)
    }
}
