import Foundation

enum CockpitMapLifecycleAction: Equatable {
    case storeOnly
    case render
    case deactivate(unloadAfter: TimeInterval)
    case unloadNow
}

enum CockpitMapLifecycle {
    static let defaultIdleUnloadDelay: TimeInterval = 60

    static func mapStateRefreshAction(isMapVisible: Bool) -> CockpitMapLifecycleAction {
        isMapVisible ? .render : .storeOnly
    }

    static func surfaceChangeAction(
        wasMapVisible: Bool,
        isMapVisible: Bool,
        idleUnloadDelay: TimeInterval = defaultIdleUnloadDelay
    ) -> CockpitMapLifecycleAction {
        if isMapVisible { return .render }
        if wasMapVisible { return .unloadNow }
        return .storeOnly
    }

    static func windowCloseAction() -> CockpitMapLifecycleAction {
        .unloadNow
    }
}
