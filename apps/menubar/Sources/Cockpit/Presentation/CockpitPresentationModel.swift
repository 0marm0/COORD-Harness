import Foundation

enum CockpitScope: String, CaseIterable, Equatable, Codable {
    case now
    case attention
    case next
    case all

    var label: String {
        switch self {
        case .now: return "Now"
        case .attention: return "Attention"
        case .next: return "Next"
        case .all: return "All"
        }
    }
}

enum CockpitGroupMode: String, CaseIterable, Equatable, Codable {
    case smart
    case nested
    case agentSession = "agent_session"
    case domain
    case module
    case owner
    case status
    case none

    var label: String {
        switch self {
        case .smart: return "Grouped"
        case .nested: return "Nested"
        case .agentSession: return "Agent"
        case .domain: return "Domain"
        case .module: return "Module"
        case .owner: return "Owner"
        case .status: return "Status"
        case .none: return "None"
        }
    }
}

enum CockpitSortMode: String, CaseIterable, Equatable, Codable {
    case smart
    case progress
    case eta
    case priority
    case title
    case owner
    case module
    case status
    case domain
    case resource
    case id
    case note
    case why

    static let pickerCases: [CockpitSortMode] = [
        .smart, .progress, .eta, .priority, .title, .owner, .module,
        .status, .domain, .resource, .id, .note, .why,
    ]

    var label: String {
        switch self {
        case .smart: return "Ordered"
        case .progress: return "Progress"
        case .eta: return "ETA"
        case .priority: return "Priority"
        case .title: return "Title"
        case .owner: return "Owner"
        case .module: return "Module"
        case .status: return "Status"
        case .domain: return "Domain"
        case .resource: return "Resource"
        case .id: return "ID"
        case .note: return "Note"
        case .why: return "Why"
        }
    }

    func popupLabel(direction: CockpitSortDirection) -> String {
        guard self != .smart else { return label }
        return "\(label) \(direction.shortLabel)"
    }

    var defaultDirection: CockpitSortDirection {
        switch self {
        case .progress:
            return .descending
        default:
            return .ascending
        }
    }

    var headerColumnID: String? {
        switch self {
        case .smart: return nil
        case .progress: return "progress"
        case .eta: return "eta"
        case .priority: return "priority"
        case .title: return "work"
        case .owner: return "owner"
        case .module: return "module"
        case .status: return "state"
        case .domain: return "domain"
        case .resource: return "resource"
        case .id: return "id"
        case .note: return "note"
        case .why: return "why"
        }
    }

    init(columnID: String) {
        switch columnID {
        case "progress": self = .progress
        case "eta": self = .eta
        case "priority": self = .priority
        case "owner": self = .owner
        case "module": self = .module
        case "state": self = .status
        case "domain": self = .domain
        case "resource": self = .resource
        case "id": self = .id
        case "note": self = .note
        case "why": self = .why
        case "work": self = .title
        default: self = .smart
        }
    }
}

struct CockpitFilterChip: Equatable {
    var id: String
    var label: String
}

enum CockpitTableItem: Equatable {
    case group(CockpitRenderedGroup)
    case row(CockpitRow)
    case detail(CockpitRow)

    var stableID: String {
        switch self {
        case .group(let group): return "group:\(group.key)"
        case .row(let row): return "row:\(row.dedupKey)"
        case .detail(let row): return "detail:\(row.dedupKey)"
        }
    }

    var row: CockpitRow? {
        switch self {
        case .row(let row), .detail(let row): return row
        case .group: return nil
        }
    }
}

struct CockpitRenderedGroup: Equatable {
    var key: String
    var label: String
    var count: Int
    var isCollapsed: Bool
    var depth: Int = 0
}

struct CockpitPresentationModel: Equatable {
    var state: CockpitState
    var scope: CockpitScope = .now
    var groupMode: CockpitGroupMode = .smart
    var sortMode: CockpitSortMode = .smart
    var sortDirection: CockpitSortDirection = CockpitSortMode.smart.defaultDirection
    var owner: String = ""
    var module: String = ""
    var status: String = ""
    var query: String = ""
    var showSubline: Bool = false
    var showDiagnostics: Bool = false
    var collapsedGroupKeys: Set<String> = []
    var expandedRowKeys: Set<String> = []
    var columns: [CockpitColumn]

    init(state: CockpitState) {
        self.state = state
        self.columns = Self.initialColumns(from: state.columns)
        self.collapsedGroupKeys = Set(state.groups.filter(\.isCollapsed).map(\.key))
    }

    var visibleColumns: [CockpitColumn] {
        return columns
            .sorted { $0.displayOrder < $1.displayOrder }
            .filter(\.isVisible)
    }

    var items: [CockpitTableItem] {
        let rows = sortedRows(filteredRows())
        if groupMode == .nested {
            return nestedItems(for: rows)
        }
        guard groupMode != .none else {
            return rows.flatMap(rowAndOptionalDetail)
        }

        let groups = orderedGroups(for: rows)
        var out: [CockpitTableItem] = []
        for group in groups {
            let bucket = rows.filter { groupKey(for: $0).key == group.key }
            guard !bucket.isEmpty else { continue }
            let rendered = CockpitRenderedGroup(
                key: group.key,
                label: group.label,
                count: bucket.count,
                isCollapsed: collapsedGroupKeys.contains(group.key),
                depth: 0
            )
            out.append(.group(rendered))
            if !rendered.isCollapsed {
                out.append(contentsOf: bucket.flatMap(rowAndOptionalDetail))
            }
        }
        return out
    }

    var activeChips: [CockpitFilterChip] {
        var chips: [CockpitFilterChip] = []
        if !ownerTrimmed.isEmpty { chips.append(CockpitFilterChip(id: "owner", label: "Owner: \(ownerTrimmed)")) }
        if !moduleTrimmed.isEmpty { chips.append(CockpitFilterChip(id: "module", label: "Module: \(moduleTrimmed)")) }
        if !statusTrimmed.isEmpty { chips.append(CockpitFilterChip(id: "status", label: "Status: \(statusTrimmed)")) }
        if !queryTrimmed.isEmpty { chips.append(CockpitFilterChip(id: "query", label: "Search: \(queryTrimmed)")) }
        return chips
    }

    func visibleJobControlIDs(actionID: String) -> [String] {
        var seen = Set<String>()
        var out: [String] = []
        for item in items {
            guard let row = item.row,
                  row.actions.contains(where: { $0.id == actionID && $0.isEnabled }),
                  let id = jobControlID(row),
                  !seen.contains(id) else { continue }
            seen.insert(id)
            out.append(id)
        }
        return out
    }

    mutating func clearFilterChip(id: String) {
        switch id.lowercased() {
        case "owner":
            owner = ""
        case "module":
            module = ""
        case "status":
            status = ""
        case "query", "search":
            query = ""
        default:
            break
        }
    }

    func column(id: String) -> CockpitColumn? {
        columns.first { $0.id == id }
    }

    private func jobControlID(_ row: CockpitRow) -> String? {
        let job = (row.jobID ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if !job.isEmpty { return job }
        let work = (row.workID ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return work.isEmpty ? nil : work
    }

    mutating func replaceState(_ newState: CockpitState) {
        state = newState
        if columns.isEmpty {
            columns = Self.initialColumns(from: newState.columns)
        } else {
            mergeNewColumns(from: Self.initialColumns(from: newState.columns))
        }
        let valid = Set(newState.rows.map(\.dedupKey))
        expandedRowKeys = expandedRowKeys.intersection(valid)
    }

    mutating func toggleGroup(key: String) {
        if collapsedGroupKeys.contains(key) { collapsedGroupKeys.remove(key) }
        else { collapsedGroupKeys.insert(key) }
    }

    mutating func toggleRow(dedupKey: String) {
        if expandedRowKeys.contains(dedupKey) { expandedRowKeys.remove(dedupKey) }
        else { expandedRowKeys.insert(dedupKey) }
    }

    mutating func resizeColumn(id: String, width: Int) {
        guard let index = columns.firstIndex(where: { $0.id == id }) else { return }
        let minWidth = max(1, columns[index].minWidth)
        columns[index].width = min(1_800, max(minWidth, width))
    }

    mutating func moveColumn(id: String, before targetID: String) {
        let ordered = columns.sorted { $0.displayOrder < $1.displayOrder }
        guard let from = ordered.firstIndex(where: { $0.id == id }),
              let to = ordered.firstIndex(where: { $0.id == targetID }),
              from != to else { return }
        var changed = ordered
        let moved = changed.remove(at: from)
        changed.insert(moved, at: from < to ? max(0, to - 1) : to)
        for idx in changed.indices {
            changed[idx].displayOrder = (idx + 1) * 10
        }
        columns = changed
    }

    mutating func moveColumnUp(id: String) {
        moveColumn(id: id, by: -1)
    }

    mutating func moveColumnDown(id: String) {
        moveColumn(id: id, by: 1)
    }

    private mutating func moveColumn(id: String, by delta: Int) {
        let ordered = columns.sorted { $0.displayOrder < $1.displayOrder }
        guard let from = ordered.firstIndex(where: { $0.id == id }) else { return }
        let to = from + delta
        guard ordered.indices.contains(to) else { return }
        var changed = ordered
        changed.swapAt(from, to)
        for idx in changed.indices {
            changed[idx].displayOrder = (idx + 1) * 10
        }
        columns = changed
    }

    mutating func reorderVisibleColumns(_ visibleIDs: [String]) {
        let existing = Dictionary(uniqueKeysWithValues: columns.map { ($0.id, $0) })
        let visibleSet = Set(visibleIDs)
        var changed: [CockpitColumn] = []
        for id in visibleIDs where existing[id]?.isVisible == true {
            if let column = existing[id] {
                changed.append(column)
            }
        }
        changed.append(contentsOf: columns
            .sorted { $0.displayOrder < $1.displayOrder }
            .filter { !visibleSet.contains($0.id) })
        for idx in changed.indices {
            changed[idx].displayOrder = (idx + 1) * 10
        }
        columns = changed
    }

    mutating func applyColumnState(_ persisted: [CockpitColumn]) {
        guard !persisted.isEmpty else { return }
        let knownIDs = Self.nativeDefaultIDs
        let persistedKnown = persisted
            .filter { knownIDs.contains($0.id) }
            .map(normalizedPersistedColumn)
        let persistedIDs = Set(persistedKnown.map(\.id))
        let canonical = persistedKnown + columns
            .sorted { $0.displayOrder < $1.displayOrder }
            .filter { knownIDs.contains($0.id) && !persistedIDs.contains($0.id) }
            .map(normalizedCurrentColumn)
        let extras = extraColumns(current: columns, persisted: persisted)
        columns = canonical + extras
    }

    mutating func setColumnVisible(id: String, visible: Bool) {
        guard let index = columns.firstIndex(where: { $0.id == id }) else { return }
        columns[index].isVisible = visible
    }

    mutating func resetColumns() {
        columns = CockpitColumn.webDefaults
    }

    mutating func showAllColumns() {
        for idx in columns.indices {
            columns[idx].isVisible = true
        }
    }

    mutating func restoreDefaultColumnVisibility() {
        let defaults = Dictionary(uniqueKeysWithValues: CockpitColumn.webDefaults.map { ($0.id, $0.isVisible) })
        for idx in columns.indices {
            if let isVisible = defaults[columns[idx].id] {
                columns[idx].isVisible = isVisible
            }
        }
    }

    mutating func resetColumnWidthsToDefaults() {
        let defaults = Dictionary(uniqueKeysWithValues: CockpitColumn.webDefaults.map { ($0.id, $0) })
        for idx in columns.indices {
            if let column = defaults[columns[idx].id] {
                columns[idx].width = column.width
                columns[idx].minWidth = column.minWidth
            }
        }
    }

    private mutating func mergeNewColumns(from incoming: [CockpitColumn]) {
        let existingIDs = Set(columns.map(\.id))
        var merged = columns.map(normalizedCurrentColumn)
        for column in incoming where !existingIDs.contains(column.id) {
            var next = normalizedExtraColumn(column)
            next.displayOrder = ((merged.map(\.displayOrder).max() ?? 0) / 10 + 1) * 10
            merged.append(next)
        }
        columns = merged
    }

    private func normalizedPersistedColumn(_ persisted: CockpitColumn) -> CockpitColumn {
        let defaults = Dictionary(uniqueKeysWithValues: CockpitColumn.webDefaults.map { ($0.id, $0) })
        let current = columns.first { $0.id == persisted.id }
        var next = defaults[persisted.id] ?? current ?? persisted
        next.displayOrder = migratedDisplayOrder(for: persisted, base: next)
        next.isVisible = migratedVisibility(for: persisted, base: next)
        next.width = migratedWidth(for: persisted, base: next)
        return next
    }

    private func normalizedCurrentColumn(_ column: CockpitColumn) -> CockpitColumn {
        let defaults = Dictionary(uniqueKeysWithValues: CockpitColumn.webDefaults.map { ($0.id, $0) })
        guard var next = defaults[column.id] else { return normalizedExtraColumn(column) }
        next.displayOrder = column.displayOrder
        next.isVisible = column.isVisible
        next.width = max(next.minWidth, column.width)
        return next
    }

    private static let nativeDefaultIDs = Set(CockpitColumn.webDefaults.map(\.id))
    private static let deprecatedProjectionColumnIDs: Set<String> = ["display", "status", "current_step"]

    private static func initialColumns(from incoming: [CockpitColumn]) -> [CockpitColumn] {
        var out = CockpitColumn.webDefaults
        for column in incoming where nativeDefaultIDs.contains(column.id) {
            guard let index = out.firstIndex(where: { $0.id == column.id }) else { continue }
            out[index].isVisible = column.isVisible
            out[index].width = min(1_800, max(out[index].minWidth, column.width))
        }
        for column in incoming.sorted(by: { $0.displayOrder < $1.displayOrder }) where !nativeDefaultIDs.contains(column.id) {
            var extra = column
            if deprecatedProjectionColumnIDs.contains(extra.id) {
                extra.isVisible = false
            }
            extra.displayOrder = ((out.map(\.displayOrder).max() ?? 0) / 10 + 1) * 10
            out.append(extra)
        }
        return out
    }

    private func extraColumns(current: [CockpitColumn], persisted: [CockpitColumn]) -> [CockpitColumn] {
        var byID: [String: CockpitColumn] = [:]
        for column in current + persisted where !Self.nativeDefaultIDs.contains(column.id) {
            byID[column.id] = normalizedExtraColumn(column)
        }
        var extras = byID.values.sorted { lhs, rhs in
            if lhs.displayOrder != rhs.displayOrder { return lhs.displayOrder < rhs.displayOrder }
            return lhs.id < rhs.id
        }
        let baseOrder = (CockpitColumn.webDefaults.map(\.displayOrder).max() ?? 0) / 10 + 1
        for idx in extras.indices {
            extras[idx].displayOrder = (baseOrder + idx) * 10
        }
        return extras
    }

    private func normalizedExtraColumn(_ column: CockpitColumn) -> CockpitColumn {
        var next = column
        next.minWidth = max(40, next.minWidth)
        next.width = min(1_800, max(next.minWidth, next.width))
        if Self.deprecatedProjectionColumnIDs.contains(next.id) {
            next.isVisible = false
        }
        return next
    }

    private func migratedWidth(for persisted: CockpitColumn, base: CockpitColumn) -> Int {
        let legacyDefaultWidths: [String: Set<Int>] = [
            "state": [8, 14, 18],
            "owner": [26, 32],
        ]
        if legacyDefaultWidths[persisted.id]?.contains(persisted.width) == true {
            return base.width
        }
        return min(1_800, max(base.minWidth, persisted.width))
    }

    private func migratedVisibility(for persisted: CockpitColumn, base: CockpitColumn) -> Bool {
        let legacyDefaultVisibility: [String: Bool] = [
            "why": true,
            "resource": false,
            "id": false,
        ]
        if legacyDefaultVisibility[persisted.id] == persisted.isVisible {
            return base.isVisible
        }
        return persisted.isVisible
    }

    private func migratedDisplayOrder(for persisted: CockpitColumn, base: CockpitColumn) -> Int {
        let legacyDefaultOrder: [String: Int] = [
            "why": 70,
            "note": 80,
            "priority": 90,
            "control": 100,
            "domain": 110,
            "resource": 120,
            "id": 130,
        ]
        if legacyDefaultOrder[persisted.id] == persisted.displayOrder {
            return base.displayOrder
        }
        return persisted.displayOrder
    }

    mutating func toggleSubline() {
        showSubline.toggle()
    }

    mutating func setSortMode(_ mode: CockpitSortMode) {
        sortMode = mode
        sortDirection = mode.defaultDirection
    }

    mutating func setSortModeFromHeader(columnID: String) {
        let nextMode = CockpitSortMode(columnID: columnID)
        if nextMode == sortMode {
            sortDirection.toggle()
        } else {
            setSortMode(nextMode)
        }
    }

    var viewSnapshot: CockpitViewSnapshot {
        CockpitViewSnapshot(
            scope: scope,
            groupMode: groupMode,
            sortMode: sortMode,
            sortDirection: sortDirection,
            owner: owner,
            module: module,
            status: status,
            query: query,
            columns: columns
        )
    }

    mutating func applyView(_ snapshot: CockpitViewSnapshot) {
        scope = snapshot.scope
        groupMode = snapshot.groupMode
        sortMode = snapshot.sortMode
        sortDirection = snapshot.sortDirection
        owner = snapshot.owner
        module = snapshot.module
        status = snapshot.status
        query = snapshot.query
        if let columns = snapshot.columns {
            applyColumnState(columns)
        }
        collapsedGroupKeys = []
        expandedRowKeys = []
    }

    private func filteredRows() -> [CockpitRow] {
        var viewState = CockpitViewState(state: state)
        viewState.owner = owner
        viewState.module = module
        viewState.status = status
        viewState.query = query
        return viewState.visibleRows.filter(matchesScope)
    }

    private func matchesScope(_ row: CockpitRow) -> Bool {
        let rowScope = row.scope.lowercased()
        let rowStatus = row.status.lowercased()
        switch scope {
        case .all:
            return true
        case .now:
            return ["now", "running", "live", "next", "queue", "queued", "followup", "follow-up", "local"].contains(rowScope)
                || ["running", "queued", "ready", "launchable"].contains(rowStatus)
        case .attention:
            return rowScope == "attention" || ["attention", "blocked", "failed", "error", "stale"].contains(rowStatus)
        case .next:
            return ["next", "queue", "queued", "followup", "follow-up", "local"].contains(rowScope)
                || ["queued", "ready", "launchable"].contains(rowStatus)
        }
    }

    private func sortedRows(_ rows: [CockpitRow]) -> [CockpitRow] {
        rows.sorted { lhs, rhs in
            switch sortMode {
            case .smart:
                return smartLess(lhs, rhs)
            case .progress:
                return optionalDoubleLess(lhs.effectivePct, rhs.effectivePct) ?? smartLess(lhs, rhs)
            case .eta:
                return optionalDoubleLess(lhs.etaSeconds, rhs.etaSeconds) ?? smartLess(lhs, rhs)
            case .priority:
                return directedIntLess(priorityRank(lhs.priority), priorityRank(rhs.priority)) ?? smartLess(lhs, rhs)
            case .title:
                return stringLess(lhs.title, rhs.title) ?? smartLess(lhs, rhs)
            case .owner:
                return stringLess(nonEmpty(lhs.ownerGroup, lhs.owner), nonEmpty(rhs.ownerGroup, rhs.owner)) ?? smartLess(lhs, rhs)
            case .module:
                return stringLess(nonEmpty(lhs.moduleLabel, lhs.module), nonEmpty(rhs.moduleLabel, rhs.module)) ?? smartLess(lhs, rhs)
            case .status:
                return stringLess(lhs.status, rhs.status) ?? smartLess(lhs, rhs)
            case .domain:
                return stringLess(lhs.domainLabel ?? "", rhs.domainLabel ?? "") ?? smartLess(lhs, rhs)
            case .resource:
                return stringLess(lhs.resourceClass ?? "", rhs.resourceClass ?? "") ?? smartLess(lhs, rhs)
            case .id:
                return stringLess(nonEmpty(lhs.workID, lhs.jobID, lhs.dedupKey), nonEmpty(rhs.workID, rhs.jobID, rhs.dedupKey)) ?? smartLess(lhs, rhs)
            case .note:
                return stringLess(lhs.noteText ?? "", rhs.noteText ?? "") ?? smartLess(lhs, rhs)
            case .why:
                return stringLess(lhs.whyText ?? "", rhs.whyText ?? "") ?? smartLess(lhs, rhs)
            }
        }
    }

    private func directedIntLess(_ lhs: Int, _ rhs: Int) -> Bool? {
        guard lhs != rhs else { return nil }
        return sortDirection == .ascending ? lhs < rhs : lhs > rhs
    }

    private func optionalDoubleLess(_ lhs: Double?, _ rhs: Double?) -> Bool? {
        switch (lhs, rhs) {
        case (nil, nil):
            return nil
        case (nil, _?):
            return false
        case (_?, nil):
            return true
        case let (left?, right?):
            guard left != right else { return nil }
            return sortDirection == .ascending ? left < right : left > right
        }
    }

    private func stringLess(_ lhs: String, _ rhs: String) -> Bool? {
        let left = lhs.trimmingCharacters(in: .whitespacesAndNewlines)
        let right = rhs.trimmingCharacters(in: .whitespacesAndNewlines)
        switch (left.isEmpty, right.isEmpty) {
        case (true, true):
            return nil
        case (true, false):
            return false
        case (false, true):
            return true
        case (false, false):
            let cmp = left.localizedCaseInsensitiveCompare(right)
            guard cmp != .orderedSame else { return nil }
            return sortDirection == .ascending ? cmp == .orderedAscending : cmp == .orderedDescending
        }
    }

    private func smartLess(_ lhs: CockpitRow, _ rhs: CockpitRow) -> Bool {
        let leftRank = smartBucketRank(lhs)
        let rightRank = smartBucketRank(rhs)
        if leftRank != rightRank { return leftRank < rightRank }
        if lhs.displayOrder != rhs.displayOrder { return lhs.displayOrder < rhs.displayOrder }
        return lhs.dedupKey < rhs.dedupKey
    }

    private func smartBucketRank(_ row: CockpitRow) -> Int {
        let status = row.status.lowercased()
        let scope = row.scope.lowercased()
        let group = (row.groupKey ?? "").lowercased()
        if row.live == true || status == "running" || ["running", "live"].contains(scope) || ["running", "live"].contains(group) {
            return 0
        }
        if ["attention", "blocked", "failed", "error", "stale"].contains(status)
            || ["attention", "blocked", "failed", "error", "stale"].contains(scope)
            || ["attention", "blocked", "failed", "error", "stale"].contains(group) {
            return 1
        }
        if ["queued", "ready", "launchable"].contains(status)
            || ["next", "queue", "queued", "followup", "follow-up", "local", "up-next"].contains(scope)
            || ["next", "queue", "queued", "followup", "follow-up", "local", "up-next"].contains(group) {
            return 2
        }
        if ["done", "complete", "completed"].contains(status) || scope == "done" || group == "done" {
            return 4
        }
        return 3
    }

    private func priorityRank(_ priority: String?) -> Int {
        let raw = (priority ?? "").lowercased()
        if raw.contains("p0") || raw.contains("critical") { return 0 }
        if raw.contains("p1") || raw.contains("high") { return 1 }
        if raw.contains("p2") || raw.contains("medium") { return 2 }
        if raw.contains("p3") || raw.contains("low") { return 3 }
        return 9
    }

    private func orderedGroups(for rows: [CockpitRow]) -> [CockpitRenderedGroup] {
        var seen: Set<String> = []
        var groups: [CockpitRenderedGroup] = []
        let serverGroups = Dictionary(uniqueKeysWithValues: state.groups.map { ($0.key, $0) })
        for row in rows {
            let key = groupKey(for: row)
            guard !seen.contains(key.key) else { continue }
            seen.insert(key.key)
            let server = serverGroups[key.key]
            let label = presentationGroupLabel(key: key.key, label: server?.label ?? key.label)
            groups.append(CockpitRenderedGroup(
                key: key.key,
                label: label,
                count: server?.count ?? rows.filter { groupKey(for: $0).key == key.key }.count,
                isCollapsed: collapsedGroupKeys.contains(key.key),
                depth: 0
            ))
        }
        return groups
    }

    private func nestedItems(for rows: [CockpitRow]) -> [CockpitTableItem] {
        let rowsByIdentity = Dictionary(grouping: rows, by: rowIdentity)
        let visibleParentIDs = Set(rowsByIdentity.keys)
        let childrenByParent = Dictionary(grouping: rows.filter { !parentID($0).isEmpty }, by: parentID)
        var emitted: Set<String> = []
        var out: [CockpitTableItem] = []

        for row in rows {
            if emitted.contains(row.dedupKey) { continue }
            let id = rowIdentity(row)
            let parent = parentID(row)

            if !parent.isEmpty, visibleParentIDs.contains(parent) {
                continue
            }

            let children = childrenByParent[id] ?? []
            if !children.isEmpty {
                appendNestedGroup(label: row.title, keySeed: id, parent: row, children: children, emitted: &emitted, out: &out)
                continue
            }

            if !parent.isEmpty {
                let orphanChildren = childrenByParent[parent] ?? [row]
                appendOrphanNestedGroup(parentID: parent, children: orphanChildren, emitted: &emitted, out: &out)
                continue
            }

            var standalone = row
            standalone.hierarchyDepth = 0
            out.append(contentsOf: rowAndOptionalDetail(standalone))
            emitted.insert(row.dedupKey)
        }
        return out
    }

    private func appendNestedGroup(
        label: String,
        keySeed: String,
        parent: CockpitRow,
        children: [CockpitRow],
        emitted: inout Set<String>,
        out: inout [CockpitTableItem]
    ) {
        let key = "nested:\(normalizeGroupKey(keySeed, fallback: parent.dedupKey))"
        let rendered = CockpitRenderedGroup(
            key: key,
            label: label,
            count: children.count + 1,
            isCollapsed: collapsedGroupKeys.contains(key),
            depth: 0
        )
        out.append(.group(rendered))
        guard !rendered.isCollapsed else {
            emitted.insert(parent.dedupKey)
            children.forEach { emitted.insert($0.dedupKey) }
            return
        }
        var parentRow = parent
        parentRow.hierarchyDepth = 0
        out.append(contentsOf: rowAndOptionalDetail(parentRow))
        emitted.insert(parent.dedupKey)
        for child in children.sorted(by: smartLess) {
            var childRow = child
            childRow.hierarchyDepth = 1
            out.append(contentsOf: rowAndOptionalDetail(childRow))
            emitted.insert(child.dedupKey)
        }
    }

    private func appendOrphanNestedGroup(
        parentID: String,
        children: [CockpitRow],
        emitted: inout Set<String>,
        out: inout [CockpitTableItem]
    ) {
        let key = "nested-orphan:\(normalizeGroupKey(parentID, fallback: "parent"))"
        let rendered = CockpitRenderedGroup(
            key: key,
            label: parentID,
            count: children.count,
            isCollapsed: collapsedGroupKeys.contains(key),
            depth: 0
        )
        out.append(.group(rendered))
        guard !rendered.isCollapsed else {
            children.forEach { emitted.insert($0.dedupKey) }
            return
        }
        for child in children.sorted(by: smartLess) {
            var childRow = child
            childRow.hierarchyDepth = 1
            out.append(contentsOf: rowAndOptionalDetail(childRow))
            emitted.insert(child.dedupKey)
        }
    }

    private func groupKey(for row: CockpitRow) -> (key: String, label: String) {
        switch groupMode {
        case .smart, .nested:
            return (
                normalizeGroupKey(row.groupKey ?? row.scope, fallback: "ungrouped"),
                nonEmpty(row.groupLabel, row.scope.capitalized, "Ungrouped")
            )
        case .agentSession:
            // Prefer the projection's resolved key: only the server can bridge
            // one chat's two identities and roll a subagent up under the chat
            // that spawned it. The client-side derivation below is the fallback
            // for a projection that predates session_group_key.
            if let resolved = row.sessionGroupKey, !resolved.isEmpty {
                return (
                    normalizeGroupKey(resolved, fallback: "unowned"),
                    nonEmpty(row.sessionGroupLabel, row.ownerSessionLabel, row.ownerConversationTitle, row.ownerGroup, row.owner, "Unowned")
                )
            }
            let sessionKey = nonEmpty(row.ownerSessionID, row.ownerExternalThreadID, row.ownerWorktreeID, row.ownerGroup, row.owner, "unowned")
            return (
                "agent-session:\(normalizeGroupKey(sessionKey, fallback: "unowned"))",
                nonEmpty(row.ownerSessionLabel, row.ownerConversationTitle, row.ownerSessionActor, row.ownerGroup, row.owner, "Unowned")
            )
        case .domain:
            return (normalizeGroupKey(row.domainLabel, fallback: "no-domain"), nonEmpty(row.domainLabel, "No domain"))
        case .module:
            return (normalizeGroupKey(row.module ?? row.moduleLabel, fallback: "no-module"), nonEmpty(row.moduleLabel, row.module, "No module"))
        case .owner:
            return (normalizeGroupKey(row.ownerGroup ?? row.owner, fallback: "unowned"), nonEmpty(row.ownerGroup, row.owner, "Unowned"))
        case .status:
            return (normalizeGroupKey(row.status, fallback: "unknown"), row.status.capitalized)
        case .none:
            return ("all", "All")
        }
    }

    private func presentationGroupLabel(key: String, label: String) -> String {
        let normalized = normalizeGroupKey(key, fallback: "")
        if ["running", "live", "now"].contains(normalized) {
            return "Running"
        }
        if ["attention", "blocked", "failed", "error", "stale"].contains(normalized) {
            return "Attention"
        }
        if ["followup", "follow-up", "next", "up-next"].contains(normalized) {
            return "Up Next"
        }
        return nonEmpty(label, normalized.capitalized, "Ungrouped")
    }

    private func rowAndOptionalDetail(_ row: CockpitRow) -> [CockpitTableItem] {
        if expandedRowKeys.contains(row.dedupKey) { return [.row(row), .detail(row)] }
        return [.row(row)]
    }

    private func rowIdentity(_ row: CockpitRow) -> String {
        nonEmpty(row.workID, row.jobID, row.dedupKey)
    }

    private func parentID(_ row: CockpitRow) -> String {
        nonEmpty(row.parentID)
    }

    private func normalizeGroupKey(_ raw: String?, fallback: String) -> String {
        let value = nonEmpty(raw, fallback).lowercased()
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_"))
        return String(value.unicodeScalars.map { allowed.contains($0) ? Character($0) : "-" })
            .replacingOccurrences(of: "--", with: "-")
            .trimmingCharacters(in: CharacterSet(charactersIn: "-"))
    }

    private func nonEmpty(_ values: String?...) -> String {
        for value in values {
            let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            if !trimmed.isEmpty { return trimmed }
        }
        return ""
    }

    private var ownerTrimmed: String { owner.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var moduleTrimmed: String { module.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var statusTrimmed: String { status.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var queryTrimmed: String { query.trimmingCharacters(in: .whitespacesAndNewlines) }
}
