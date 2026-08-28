import Foundation

enum CockpitTableInteraction: Equatable {
    case toggleGroup(String)
    case toggleRow(String)
    case rowAction(CockpitRowAction, CockpitRow)
}

enum CockpitTableInteractionResolver {
    static func doubleClick(items: [CockpitTableItem], rowIndex: Int) -> CockpitTableInteraction? {
        guard rowIndex >= 0, rowIndex < items.count else { return nil }
        switch items[rowIndex] {
        case .group(let group):
            return .toggleGroup(group.key)
        case .row(let row), .detail(let row):
            return .toggleRow(row.dedupKey)
        }
    }

    static func rowAction(items: [CockpitTableItem], rowIndex: Int, actionID: String) -> CockpitTableInteraction? {
        guard rowIndex >= 0, rowIndex < items.count else { return nil }
        let row: CockpitRow
        switch items[rowIndex] {
        case .row(let concrete), .detail(let concrete):
            row = concrete
        case .group:
            return nil
        }
        guard let action = row.actions.first(where: { $0.id == actionID }),
              action.isEnabled else { return nil }
        return .rowAction(action, row)
    }
}
