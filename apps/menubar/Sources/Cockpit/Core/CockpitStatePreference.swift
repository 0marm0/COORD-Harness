import Foundation

enum CockpitStatePreference {
    static func prefersNativeSQLite(_ state: CockpitState) -> Bool {
        state.error == nil
            && state.schemaVersion > 0
            && state.writerSeq > 0
            && !state.rows.isEmpty
    }

    static func shouldKeepCurrentState(overFallback fallback: CockpitState, current: CockpitState?) -> Bool {
        guard let current, prefersNativeSQLite(current) else { return false }
        return fallback.error == nil && fallback.rows.isEmpty
    }

    static func shouldKeepCurrentState(overSQLiteState state: CockpitState, current: CockpitState?) -> Bool {
        guard let current, prefersNativeSQLite(current) else { return false }
        if state.error != nil { return true }
        return state.error == nil
            && state.stale
            && state.rows.isEmpty
            && state.schemaVersion > 0
            && state.writerSeq > 0
    }

    static func shouldKeepCurrentState(overSQLiteError state: CockpitState, current: CockpitState?) -> Bool {
        shouldKeepCurrentState(overSQLiteState: state, current: current)
    }

    static func preservedCurrentState(
        _ current: CockpitState,
        overSQLiteState state: CockpitState,
        lastGoodAge: TimeInterval,
        graceSeconds: TimeInterval
    ) -> CockpitState {
        var preserved = current
        if state.error != nil && lastGoodAge <= graceSeconds {
            preserved.stale = false
            preserved.refreshing = true
            preserved.error = nil
        } else {
            preserved.stale = true
            preserved.refreshing = false
            preserved.error = state.error
        }
        return preserved
    }
}
