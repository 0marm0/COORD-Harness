import Foundation

struct CockpitViewState: Equatable {
    var state: CockpitState
    var owner: String = ""
    var module: String = ""
    var status: String = ""
    var query: String = ""

    var visibleRows: [CockpitRow] {
        state.rows.filter { row in
            matches(owner: owner, row: row)
                && matches(module: module, row: row)
                && matches(status: status, row: row)
                && matches(query: query, row: row)
        }
    }

    private func matches(owner expected: String, row: CockpitRow) -> Bool {
        let value = expected.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !value.isEmpty else { return true }
        return row.ownerGroup?.lowercased() == value || row.owner?.lowercased() == value
    }

    private func matches(module expected: String, row: CockpitRow) -> Bool {
        let value = expected.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !value.isEmpty else { return true }
        return row.module?.lowercased() == value || row.moduleLabel?.lowercased() == value
    }

    private func matches(status expected: String, row: CockpitRow) -> Bool {
        let value = expected.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !value.isEmpty else { return true }
        return row.status.lowercased().contains(value)
    }

    private func matches(query expected: String, row: CockpitRow) -> Bool {
        let value = expected.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !value.isEmpty else { return true }
        let haystack = [
            row.title,
            row.dedupKey,
            row.workID,
            row.parentID,
            row.jobID,
            row.owner,
            row.ownerGroup,
            row.module,
            row.moduleLabel,
            row.domainLabel,
            row.resourceClass,
            row.whyText,
            row.noteText,
            row.priority,
            row.rowKind,
            row.groupLabel,
            row.doneSignal,
            row.acceptanceSummary,
            row.contextPackRef,
        ]
        return haystack.compactMap { $0?.lowercased() }.joined(separator: " ").contains(value)
    }
}
