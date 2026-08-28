import AppKit
import QuartzCore

final class CockpitTableController: NSObject, NSTableViewDataSource, NSTableViewDelegate {
    let tableView = NSTableView()
    let scrollView = CockpitEdgeScrollView()

    var onToggleGroup: ((String) -> Void)?
    var onToggleRow: ((String) -> Void)?
    var onAction: ((CockpitRowAction, CockpitRow) -> Void)?
    var onColumnResize: ((String, CGFloat) -> Void)?
    var onColumnMove: ((String, String) -> Void)?
    var onColumnOrderChange: (([String]) -> Void)?
    var onHeaderSort: ((String) -> Void)?
    var onSelection: ((CockpitRow?) -> Void)?

    private var model: CockpitPresentationModel?
    private var items: [CockpitTableItem] = []
    private var columns: [CockpitColumn] = []
    private var showSubline = false
    private var sortMode: CockpitSortMode = .smart
    private var sortDirection: CockpitSortDirection = CockpitSortMode.smart.defaultDirection
    private var lastViewportFitSignature = ""
    private var lastModelColumnWidthSignature = ""
    private var lastItemSignature = ""
    private var lastRenderDataSignature = ""
    private var isFittingColumns = false
    private var userSizedColumnIDs: Set<String> = []
    private var lastUserColumnResizeAt = Date.distantPast
    private var progressAnimationsEnabled = true
    private let previewColumnFilter: Set<String> = {
        let raw = ProcessInfo.processInfo.environment["COORD_COCKPIT_PREVIEW_COLUMN_FILTER"] ?? ""
        let ids = raw
            .split(separator: ",")
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() }
            .filter { !$0.isEmpty }
        return Set(ids)
    }()
    private let previewItemLimit: Int? = {
        let raw = ProcessInfo.processInfo.environment["COORD_COCKPIT_PREVIEW_ITEM_LIMIT"] ?? ""
        return Int(raw.trimmingCharacters(in: .whitespacesAndNewlines))
    }()

    override init() {
        super.init()
        tableView.delegate = self
        tableView.dataSource = self
        tableView.headerView = NSTableHeaderView()
        tableView.usesAlternatingRowBackgroundColors = false
        tableView.backgroundColor = .clear
        tableView.gridStyleMask = []
        tableView.gridColor = CockpitTokens.Color.line.withAlphaComponent(0.10)
        tableView.intercellSpacing = NSSize(width: 0, height: 0)
        tableView.rowSizeStyle = .custom
        tableView.headerView?.frame.size.height = 30
        tableView.columnAutoresizingStyle = .noColumnAutoresizing
        tableView.allowsColumnReordering = true
        tableView.allowsColumnResizing = true
        tableView.allowsMultipleSelection = false
        tableView.target = self
        tableView.doubleAction = #selector(doubleClicked)
        scrollView.documentView = tableView
        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = true
        CockpitScrollChrome.apply(to: scrollView)
        scrollView.scrollerStyle = .overlay

        NotificationCenter.default.addObserver(
            self,
            selector: #selector(columnDidResize(_:)),
            name: NSTableView.columnDidResizeNotification,
            object: tableView
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(columnDidMove(_:)),
            name: NSTableView.columnDidMoveNotification,
            object: tableView
        )
    }

    func render(_ model: CockpitPresentationModel) {
        let scrollOrigin = scrollView.contentView.bounds.origin
        let anchor = captureScrollAnchor()
        self.model = model
        let renderedItems = model.items
        if let previewItemLimit {
            self.items = Array(renderedItems.prefix(max(0, previewItemLimit)))
        } else {
            self.items = renderedItems
        }
        let itemSignature = items.map(\.stableID).joined(separator: "|")
        let dataSignature = renderDataSignature()
        let shouldFullReload = itemSignature != lastItemSignature
        let shouldPartialReload = dataSignature != lastRenderDataSignature
        let visibleColumns = model.visibleColumns
        self.sortMode = model.sortMode
        self.sortDirection = model.sortDirection
        if previewColumnFilter.isEmpty {
            self.columns = visibleColumns
        } else {
            self.columns = visibleColumns.filter { previewColumnFilter.contains($0.id.lowercased()) }
        }
        self.showSubline = model.showSubline
        rebuildColumnsIfNeeded()
        updateHorizontalScroller()
        if shouldFullReload {
            tableView.reloadData()
            lastItemSignature = itemSignature
            lastRenderDataSignature = dataSignature
        } else if shouldPartialReload {
            reloadVisibleRows()
            lastRenderDataSignature = dataSignature
        }
        restoreScrollAnchor(anchor, fallbackOrigin: scrollOrigin)
    }

    func updateHorizontalScroller() {
        fitColumnsToViewportIfNeeded()
        let columnWidth = tableView.tableColumns.reduce(CGFloat(0)) { $0 + $1.width }
        let gridWidth = intercolumnGridWidth
        let shouldScroll = columnWidth + gridWidth > scrollView.contentSize.width + 2
        scrollView.hasHorizontalScroller = shouldScroll
        if !shouldScroll, scrollView.contentView.bounds.origin.x != 0 {
            scrollView.contentView.scroll(to: NSPoint(x: 0, y: scrollView.contentView.bounds.origin.y))
            scrollView.reflectScrolledClipView(scrollView.contentView)
        }
    }

    func setProgressAnimationsEnabled(_ enabled: Bool) {
        guard progressAnimationsEnabled != enabled else { return }
        progressAnimationsEnabled = enabled
        applyProgressAnimationsEnabledToVisibleCells()
    }

    func numberOfRows(in tableView: NSTableView) -> Int {
        items.count
    }

    func selectRow(dedupKey: String, scrollIfNeeded: Bool = true) {
        guard let index = items.firstIndex(where: { $0.row?.dedupKey == dedupKey }) else { return }
        let originBeforeSelection = scrollView.contentView.bounds.origin
        tableView.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false)
        guard scrollIfNeeded else {
            restoreScrollOrigin(originBeforeSelection)
            return
        }
        let visibleRows = tableView.rows(in: scrollView.contentView.bounds)
        if !NSLocationInRange(index, visibleRows) {
            tableView.scrollRowToVisible(index)
        }
    }

    func revealPartiallyHiddenGroupHeader() {
        guard tableView.numberOfRows > 0 else { return }
        let origin = scrollView.contentView.bounds.origin
        let clampedY = clampRestoredGroupHeaderOrigin(origin.y)
        guard clampedY != origin.y else { return }
        restoreScrollOrigin(NSPoint(x: origin.x, y: clampedY))
    }

    func tableView(_ tableView: NSTableView, heightOfRow row: Int) -> CGFloat {
        guard row < items.count else { return CockpitTokens.rowHeight }
        switch items[row] {
        case .group: return CockpitTokens.groupHeight
        case .detail(let detailRow): return CockpitDetailCell.preferredHeight(row: detailRow, tableWidth: scrollView.contentSize.width)
        case .row: return CockpitTokens.rowHeight
        }
    }

    func tableView(_ tableView: NSTableView, isGroupRow row: Int) -> Bool {
        return false
    }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row rowIndex: Int) -> NSView? {
        guard rowIndex < items.count, let tableColumn else { return nil }
        let columnID = tableColumn.identifier.rawValue
        switch items[rowIndex] {
        case .group(let group):
            guard columnID == "work" else { return CockpitEmptyCell() }
            let cell = tableView.makeView(withIdentifier: NSUserInterfaceItemIdentifier("group"), owner: self) as? CockpitGroupCell ?? CockpitGroupCell()
            cell.identifier = NSUserInterfaceItemIdentifier("group")
            cell.configure(group: group)
            cell.onToggle = { [weak self] in
                self?.onToggleGroup?(group.key)
            }
            return cell
        case .detail(let row):
            guard columnID == "work" else { return CockpitEmptyCell() }
            let cell = tableView.makeView(withIdentifier: NSUserInterfaceItemIdentifier("detail"), owner: self) as? CockpitDetailCell ?? CockpitDetailCell()
            cell.identifier = NSUserInterfaceItemIdentifier("detail")
            cell.configure(row: row, rowIndex: rowIndex, target: self, action: #selector(actionSelected(_:)))
            return cell
        case .row(let row):
            if columnID == "progress" {
                let cell = tableView.makeView(withIdentifier: NSUserInterfaceItemIdentifier("progress"), owner: self) as? CockpitProgressCell ?? CockpitProgressCell()
                cell.identifier = NSUserInterfaceItemIdentifier("progress")
                cell.animationsEnabled = progressAnimationsEnabled
                cell.configure(row: row)
                return cell
            }
            if columnID == "control" {
                let cell = tableView.makeView(withIdentifier: NSUserInterfaceItemIdentifier("control"), owner: self) as? CockpitActionCell ?? CockpitActionCell()
                cell.identifier = NSUserInterfaceItemIdentifier("control")
                cell.configure(row: row, rowIndex: rowIndex, target: self, action: #selector(actionSelected(_:)))
                return cell
            }
            let cell = tableView.makeView(withIdentifier: NSUserInterfaceItemIdentifier("text-\(columnID)"), owner: self) as? CockpitTextCell ?? CockpitTextCell()
            cell.identifier = NSUserInterfaceItemIdentifier("text-\(columnID)")
            cell.configure(row: row, columnID: columnID, showSubline: showSubline)
            return cell
        }
    }

    func tableView(_ tableView: NSTableView, rowViewForRow row: Int) -> NSTableRowView? {
        let view = CockpitRowBackgroundView()
        guard row < items.count else { return view }
        switch items[row] {
        case .group(let group):
            view.kind = .group
            view.groupCount = group.count
            view.groupLabel = group.label
            view.groupKey = group.key
        case .detail(let row):
            view.kind = .detail
            view.status = row.status
        case .row(let row):
            view.kind = .work
            view.status = row.status
        }
        return view
    }

    func tableView(_ tableView: NSTableView, didClick tableColumn: NSTableColumn) {
        onHeaderSort?(tableColumn.identifier.rawValue)
    }

    func tableViewSelectionDidChange(_ notification: Notification) {
        let row = tableView.selectedRow
        guard row >= 0, row < items.count else {
            onSelection?(nil)
            return
        }
        onSelection?(items[row].row)
    }

    private func rebuildColumnsIfNeeded() {
        let existing = tableView.tableColumns.map { $0.identifier.rawValue }
        let desired = columns.map(\.id)
        let modelWidthSignature = columns
            .map { "\($0.id):\($0.width):\($0.minWidth)" }
            .joined(separator: "|")
        guard existing != desired else {
            let shouldApplyModelWidths = modelWidthSignature != lastModelColumnWidthSignature
            if shouldApplyModelWidths {
                lastModelColumnWidthSignature = modelWidthSignature
                lastViewportFitSignature = ""
            }
            for column in columns {
                if let tableColumn = tableView.tableColumns.first(where: { $0.identifier.rawValue == column.id }) {
                    if shouldApplyModelWidths && !shouldDeferModelWidthApply(for: column.id) {
                        tableColumn.width = CGFloat(column.width)
                    }
                    tableColumn.minWidth = CGFloat(column.minWidth)
                    configureHeaderCell(for: tableColumn, column: column)
                }
            }
            updateHorizontalScroller()
            return
        }
        for column in tableView.tableColumns {
            tableView.removeTableColumn(column)
        }
        lastModelColumnWidthSignature = modelWidthSignature
        lastViewportFitSignature = ""
        for column in columns {
            let tableColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(column.id))
            tableColumn.title = column.label
            configureHeaderCell(for: tableColumn, column: column)
            tableColumn.width = CGFloat(column.width)
            tableColumn.minWidth = CGFloat(column.minWidth)
            tableColumn.resizingMask = .userResizingMask
            tableView.addTableColumn(tableColumn)
        }
        updateHorizontalScroller()
    }

    private func configureHeaderCell(for tableColumn: NSTableColumn, column: CockpitColumn) {
        let direction = sortMode.headerColumnID == column.id ? sortDirection : nil
        tableColumn.headerCell = CockpitHeaderCell(title: column.label, alignment: column.alignment, sortDirection: direction)
    }

    private func fitColumnsToViewportIfNeeded() {
        let viewportWidth = scrollView.contentSize.width
        guard viewportWidth > 200, !tableView.tableColumns.isEmpty else { return }
        let signature = tableView.tableColumns.map(\.identifier.rawValue).joined(separator: "|") + ":\(Int(viewportWidth.rounded()))"
        guard signature != lastViewportFitSignature else { return }
        lastViewportFitSignature = signature
    }

    private func shouldDeferModelWidthApply(for columnID: String) -> Bool {
        userSizedColumnIDs.contains(columnID)
            && Date().timeIntervalSince(lastUserColumnResizeAt) < 0.65
    }

    private func restoreScrollOrigin(_ origin: NSPoint) {
        guard let documentView = scrollView.documentView else { return }
        let maxX = max(0, documentView.bounds.width - scrollView.contentSize.width)
        let maxY = max(0, documentView.bounds.height - scrollView.contentSize.height)
        let clamped = NSPoint(
            x: min(max(0, origin.x), maxX),
            y: min(max(0, origin.y), maxY)
        )
        guard scrollView.contentView.bounds.origin != clamped else { return }
        scrollView.contentView.scroll(to: clamped)
        scrollView.reflectScrolledClipView(scrollView.contentView)
    }

    private struct ScrollAnchor {
        var stableID: String?
        var offsetY: CGFloat
        var origin: NSPoint
    }

    private func captureScrollAnchor() -> ScrollAnchor {
        let origin = scrollView.contentView.bounds.origin
        let visibleRows = tableView.rows(in: scrollView.contentView.bounds)
        guard visibleRows.location != NSNotFound else {
            return ScrollAnchor(stableID: nil, offsetY: 0, origin: origin)
        }
        let index = min(max(0, visibleRows.location), max(0, items.count - 1))
        guard items.indices.contains(index) else {
            return ScrollAnchor(stableID: nil, offsetY: 0, origin: origin)
        }
        let rowRect = tableView.rect(ofRow: index)
        return ScrollAnchor(stableID: items[index].stableID, offsetY: origin.y - rowRect.minY, origin: origin)
    }

    private func restoreScrollAnchor(_ anchor: ScrollAnchor, fallbackOrigin: NSPoint) {
        tableView.layoutSubtreeIfNeeded()
        let targetY: CGFloat
        if let stableID = anchor.stableID,
           let index = items.firstIndex(where: { $0.stableID == stableID }) {
            targetY = tableView.rect(ofRow: index).minY + anchor.offsetY
        } else {
            targetY = fallbackOrigin.y
        }
        restoreScrollOrigin(NSPoint(x: fallbackOrigin.x, y: clampRestoredGroupHeaderOrigin(targetY)))
    }

    private func clampRestoredGroupHeaderOrigin(_ targetY: CGFloat) -> CGFloat {
        guard tableView.numberOfRows > 0 else { return targetY }
        let groupHeaderRevealPadding: CGFloat = 10
        for index in items.indices {
            guard index < tableView.numberOfRows else { continue }
            guard case .group = items[index] else { continue }
            let rect = tableView.rect(ofRow: index)
            if targetY > rect.minY && targetY <= rect.maxY + groupHeaderRevealPadding {
                return rect.minY
            }
        }
        return targetY
    }

    private func reloadVisibleRows() {
        let visibleRows = tableView.rows(in: scrollView.contentView.bounds)
        guard visibleRows.location != NSNotFound, tableView.numberOfRows > 0, tableView.numberOfColumns > 0 else {
            tableView.reloadData()
            return
        }
        let lower = max(0, visibleRows.location)
        let upper = min(tableView.numberOfRows, visibleRows.location + visibleRows.length + 1)
        guard lower < upper else { return }
        tableView.reloadData(
            forRowIndexes: IndexSet(integersIn: lower..<upper),
            columnIndexes: IndexSet(integersIn: 0..<tableView.numberOfColumns)
        )
    }

    private func applyProgressAnimationsEnabledToVisibleCells() {
        guard tableView.numberOfRows > 0, tableView.numberOfColumns > 0 else { return }
        let progressColumnIndexes = tableView.tableColumns.enumerated().compactMap { index, column in
            column.identifier.rawValue == "progress" ? index : nil
        }
        guard !progressColumnIndexes.isEmpty else { return }
        let visibleRows = tableView.rows(in: scrollView.contentView.bounds)
        guard visibleRows.location != NSNotFound else { return }
        let lower = max(0, visibleRows.location)
        let upper = min(tableView.numberOfRows, visibleRows.location + visibleRows.length + 1)
        guard lower < upper else { return }
        for rowIndex in lower..<upper {
            for columnIndex in progressColumnIndexes {
                let cell = tableView.view(atColumn: columnIndex, row: rowIndex, makeIfNecessary: false) as? CockpitProgressCell
                cell?.animationsEnabled = progressAnimationsEnabled
            }
        }
    }

    private func renderDataSignature() -> String {
        var signatures: [String] = []
        signatures.reserveCapacity(items.count)

        for item in items {
            switch item {
            case .group(let group):
                var fields: [String] = []
                fields.reserveCapacity(4)
                fields.append(item.stableID)
                fields.append(group.label)
                fields.append("\(group.count)")
                fields.append(group.isCollapsed ? "1" : "0")
                signatures.append(fields.joined(separator: "\u{1F}"))
            case .row(let row), .detail(let row):
                let formattedPercent: String
                if let percent = row.pct {
                    formattedPercent = String(format: "%.2f", percent)
                } else {
                    formattedPercent = ""
                }

                var fields: [String] = []
                fields.reserveCapacity(18)
                fields.append(item.stableID)
                fields.append(row.title)
                fields.append(row.status)
                fields.append(row.owner ?? "")
                fields.append(row.ownerGroup ?? "")
                fields.append(row.module ?? "")
                fields.append(row.moduleLabel ?? "")
                fields.append(row.domainLabel ?? "")
                fields.append(row.resourceClass ?? "")
                fields.append(row.pctDisplay ?? "")
                fields.append(formattedPercent)
                fields.append(row.etaText ?? "")
                fields.append(row.whyText ?? "")
                fields.append(row.noteText ?? "")
                fields.append(row.priority ?? "")
                fields.append(row.workID ?? "")
                fields.append(row.jobID ?? "")
                fields.append(showSubline ? "subline" : "compact")
                signatures.append(fields.joined(separator: "\u{1F}"))
            }
        }

        return signatures.joined(separator: "\u{1E}")
    }

    private var intercolumnGridWidth: CGFloat {
        tableView.gridStyleMask.contains(.solidVerticalGridLineMask)
            ? CGFloat(max(0, tableView.tableColumns.count - 1))
            : 0
    }

    @objc private func doubleClicked() {
        let row = tableView.clickedRow
        guard let interaction = CockpitTableInteractionResolver.doubleClick(items: items, rowIndex: row) else { return }
        switch interaction {
        case .toggleRow(let key):
            onToggleRow?(key)
        case .toggleGroup(let key):
            onToggleGroup?(key)
        case .rowAction:
            break
        }
    }

    @objc private func actionSelected(_ sender: NSMenuItem) {
        guard let selection = sender.representedObject as? CockpitActionSelection else { return }
        guard let interaction = CockpitTableInteractionResolver.rowAction(items: items, rowIndex: selection.rowIndex, actionID: selection.actionID) else { return }
        if case .rowAction(let rowAction, let row) = interaction {
            onAction?(rowAction, row)
        }
    }

    @objc private func columnDidResize(_ note: Notification) {
        guard !isFittingColumns else { return }
        guard let column = note.userInfo?["NSTableColumn"] as? NSTableColumn else { return }
        userSizedColumnIDs.insert(column.identifier.rawValue)
        lastUserColumnResizeAt = Date()
        updateHorizontalScroller()
        onColumnResize?(column.identifier.rawValue, column.width)
    }

    @objc private func columnDidMove(_ note: Notification) {
        guard let old = note.userInfo?["NSOldColumn"] as? Int,
              let new = note.userInfo?["NSNewColumn"] as? Int,
              old != new,
              new >= 0,
              new < tableView.tableColumns.count else { return }
        let moved = tableView.tableColumns[new].identifier.rawValue
        let beforeIndex = min(new + 1, tableView.tableColumns.count - 1)
        let before = tableView.tableColumns[beforeIndex].identifier.rawValue
        onColumnMove?(moved, before)
        onColumnOrderChange?(tableView.tableColumns.map { $0.identifier.rawValue })
    }
}

final class CockpitRowBackgroundView: NSTableRowView {
    enum Kind {
        case work
        case group
        case detail
    }

    var kind: Kind = .work
    var status: String?
    var groupCount: Int?
    var groupLabel: String?
    var groupKey: String?

    override func drawBackground(in dirtyRect: NSRect) {
        guard !isSelected else {
            drawSelection(in: dirtyRect)
            return
        }
        switch kind {
        case .group:
            CockpitTokens.Color.panel2.withAlphaComponent(0.065).setFill()
        case .detail:
            CockpitTokens.Color.blue.withAlphaComponent(0.026).setFill()
        case .work:
            CockpitTokens.Color.panel.withAlphaComponent(0.006).setFill()
        }
        dirtyRect.fill()
        drawGroupLabel()
        drawGroupCount()
        drawStatusAccent()
    }

    override func drawSeparator(in dirtyRect: NSRect) {
        NSColor.white.withAlphaComponent(0.036).setFill()
        NSRect(x: 0, y: bounds.height - 1, width: bounds.width, height: 1).fill()
    }

    override func drawSelection(in dirtyRect: NSRect) {
        let highlight = NSBezierPath(roundedRect: bounds.insetBy(dx: 4, dy: 4), xRadius: 7, yRadius: 7)
        CockpitTokens.Color.blue.withAlphaComponent(0.052).setFill()
        highlight.fill()

        CockpitTokens.Color.selectionStroke.withAlphaComponent(0.22).setStroke()
        highlight.lineWidth = 1
        highlight.stroke()
        drawStatusAccent()
    }

    private func drawGroupCount() {
        guard kind == .group, let groupCount else { return }
        let paragraph = NSMutableParagraphStyle()
        paragraph.alignment = .right
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .semibold),
            .foregroundColor: CockpitTokens.Color.faint.withAlphaComponent(0.82),
            .paragraphStyle: paragraph,
        ]
        let rect = NSRect(x: max(0, bounds.width - 132), y: 14, width: 56, height: 18)
        NSAttributedString(string: "\(groupCount)", attributes: attrs).draw(in: rect)
    }

    private func drawGroupLabel() {
        guard kind == .group else { return }
        let title = (groupLabel ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty else { return }
        let paragraph = NSMutableParagraphStyle()
        paragraph.alignment = .left
        paragraph.lineBreakMode = .byTruncatingTail
        let shadow = NSShadow()
        let tint = groupTint()
        shadow.shadowColor = tint.withAlphaComponent(0.34)
        shadow.shadowBlurRadius = 13
        shadow.shadowOffset = .zero
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 12.25, weight: .semibold),
            .foregroundColor: tint.withAlphaComponent(0.96),
            .paragraphStyle: paragraph,
            .shadow: shadow,
        ]
        let rect = NSRect(x: 76, y: 15, width: max(120, bounds.width - 180), height: 17)
        NSAttributedString(string: title.uppercased(), attributes: attrs).draw(in: rect)
    }

    private func groupTint() -> NSColor {
        switch (groupKey ?? groupLabel ?? "").lowercased() {
        case "running", "live", "now": return CockpitTokens.Color.green
        case "attention", "blocked", "failed", "error", "stale": return CockpitTokens.Color.red
        case "next", "followup", "follow-up", "up-next", "queue", "queued": return CockpitTokens.Color.amber
        case "done", "complete", "completed": return CockpitTokens.Color.faint
        default: return CockpitTokens.Color.blue2
        }
    }

    private func drawStatusAccent() {
        guard kind == .work else { return }
        let accent = NSBezierPath(
            roundedRect: NSRect(x: 0, y: 6, width: 3, height: max(8, bounds.height - 12)),
            xRadius: 1.5,
            yRadius: 1.5
        )
        CockpitTokens.statusColor(status ?? "").withAlphaComponent(0.9).setFill()
        accent.fill()
    }
}

final class CockpitEmptyCell: NSView {
    override var isFlipped: Bool { true }
}

private final class CockpitHeaderCell: NSTableHeaderCell {
    private let headerAlignment: NSTextAlignment
    private let sortDirection: CockpitSortDirection?

    init(title: String, alignment: String?, sortDirection: CockpitSortDirection?) {
        self.headerAlignment = {
            switch alignment?.lowercased() {
            case "center": return .center
            case "trailing", "right": return .right
            default: return .left
            }
        }()
        self.sortDirection = sortDirection
        super.init(textCell: title)
    }

    required init(coder: NSCoder) {
        self.headerAlignment = .left
        self.sortDirection = nil
        super.init(coder: coder)
    }

    override func draw(withFrame cellFrame: NSRect, in controlView: NSView) {
        NSColor.white.withAlphaComponent(0.018).setFill()
        cellFrame.fill()

        CockpitTokens.Color.line.withAlphaComponent(0.11).setFill()
        NSRect(x: cellFrame.minX, y: cellFrame.maxY - 1, width: cellFrame.width, height: 1).fill()

        if let sortDirection {
            drawSortGlyph(direction: sortDirection, frame: cellFrame)
        }
        guard !stringValue.isEmpty else { return }
        let paragraph = NSMutableParagraphStyle()
        paragraph.alignment = headerAlignment
        paragraph.lineBreakMode = .byTruncatingTail
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedSystemFont(ofSize: 9.5, weight: .bold),
            .foregroundColor: CockpitTokens.Color.faint.withAlphaComponent(0.78),
            .paragraphStyle: paragraph,
        ]
        let insetX: CGFloat = headerAlignment == .center ? 4 : 10
        let textRect = NSRect(
            x: cellFrame.minX + insetX,
            y: cellFrame.midY - 6,
            width: max(1, cellFrame.width - insetX * 2 - (sortDirection == nil ? 0 : 12)),
            height: 14
        )
        NSAttributedString(string: stringValue.uppercased(), attributes: attrs).draw(in: textRect)
    }

    private func drawSortGlyph(direction: CockpitSortDirection, frame: NSRect) {
        let glyph = NSBezierPath()
        let midX = frame.maxX - 10
        let midY = frame.midY
        if direction == .ascending {
            glyph.move(to: NSPoint(x: midX - 4, y: midY + 2))
            glyph.line(to: NSPoint(x: midX, y: midY - 3))
            glyph.line(to: NSPoint(x: midX + 4, y: midY + 2))
        } else {
            glyph.move(to: NSPoint(x: midX - 4, y: midY - 2))
            glyph.line(to: NSPoint(x: midX, y: midY + 3))
            glyph.line(to: NSPoint(x: midX + 4, y: midY - 2))
        }
        glyph.close()
        CockpitTokens.Color.blue2.withAlphaComponent(0.86).setFill()
        glyph.fill()
    }
}

final class CockpitGroupCell: NSView {
    var onToggle: (() -> Void)?
    private let disclosure = CockpitDisclosureGlyph()
    private let label = CockpitUI.label("", size: 15.5, weight: .heavy, color: CockpitTokens.Color.blue2)
    private var depth: Int = 0

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        addSubview(label)
        label.isHidden = true
        let glow = NSShadow()
        glow.shadowColor = CockpitTokens.Color.blue.withAlphaComponent(0.48)
        glow.shadowBlurRadius = 16
        glow.shadowOffset = .zero
        label.shadow = glow
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    func configure(group: CockpitRenderedGroup) {
        depth = group.depth
        disclosure.isCollapsed = group.isCollapsed
        label.stringValue = ""
        toolTip = group.isCollapsed ? "Expand \(group.label)" : "Collapse \(group.label)"
    }

    override func mouseDown(with event: NSEvent) {
        onToggle?()
    }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.white.withAlphaComponent(0.012).setFill()
        dirtyRect.fill()
    }

    override func layout() {
        let inset = CGFloat(depth) * 18
        disclosure.frame = .zero
        label.frame = NSRect(x: 24 + inset, y: 9, width: 0, height: 0)
    }
}

private final class CockpitDisclosureGlyph: NSView {
    var isCollapsed = false {
        didSet { needsDisplay = true }
    }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        let path = NSBezierPath()
        if isCollapsed {
            path.move(to: NSPoint(x: 2, y: 1))
            path.line(to: NSPoint(x: bounds.width - 2, y: bounds.midY))
            path.line(to: NSPoint(x: 2, y: bounds.height - 1))
        } else {
            path.move(to: NSPoint(x: 1, y: 2))
            path.line(to: NSPoint(x: bounds.midX, y: bounds.height - 2))
            path.line(to: NSPoint(x: bounds.width - 1, y: 2))
        }
        path.lineWidth = 1.8
        path.lineCapStyle = .round
        path.lineJoinStyle = .round
        CockpitTokens.Color.muted.withAlphaComponent(0.9).setStroke()
        path.stroke()
    }
}

final class CockpitTextCell: NSView {
    private let icon = NSImageView()
    private let primary = CockpitUI.label("", size: 12, weight: .regular, color: CockpitTokens.Color.text)
    private let secondary = CockpitUI.label("", size: 10, weight: .regular, color: CockpitTokens.Color.muted)
    private let pill = NSView()
    private var activeColumnID = ""
    private var hierarchyDepth = 0

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        addSubview(pill)
        addSubview(icon)
        addSubview(primary)
        addSubview(secondary)
        icon.imageScaling = .scaleProportionallyDown
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    func configure(row: CockpitRow, columnID: String, showSubline: Bool = false) {
        activeColumnID = columnID
        hierarchyDepth = row.hierarchyDepth
        pill.isHidden = true
        icon.isHidden = true
        icon.image = nil
        secondary.isHidden = true
        primary.alignment = .left
        primary.font = .systemFont(ofSize: 12, weight: .regular)
        primary.textColor = CockpitTokens.Color.text
        secondary.textColor = CockpitTokens.Color.muted
        switch columnID {
        case "state":
            primary.stringValue = ""
            primary.textColor = .clear
            primary.alignment = .center
        case "owner":
            primary.stringValue = ""
            pill.isHidden = true
            icon.isHidden = false
            icon.image = ownerImage(row)
            icon.alphaValue = 0.95
            icon.toolTip = ownerTooltip(row)
            primary.alignment = .center
        case "work":
            primary.stringValue = row.title
            primary.font = .systemFont(ofSize: 13.25, weight: .semibold)
            primary.textColor = NSColor(calibratedRed: 0.933, green: 0.949, blue: 0.980, alpha: 1)
            secondary.isHidden = !showSubline
            secondary.stringValue = [row.domainLabel, row.workID].compactMap { $0 }.joined(separator: "  ")
        case "module":
            let badge = moduleBadge(row)
            primary.stringValue = badge
            primary.font = .systemFont(ofSize: badge.count > 9 ? 6.6 : 7.2, weight: .heavy)
            primary.textColor = CockpitTokens.Color.text
            primary.alignment = .center
            pill.isHidden = false
            CockpitUI.configurePill(pill, color: CockpitTokens.moduleColor(row.module ?? row.moduleLabel), alpha: 0.10, radius: 4)
            let subline = moduleSubline(row)
            secondary.isHidden = subline.isEmpty
            secondary.stringValue = subline
        case "eta":
            primary.stringValue = meaningful(row.etaText) ?? ""
            primary.alignment = .right
        case "why":
            primary.stringValue = meaningful(row.whyText) ?? ""
            primary.font = .systemFont(ofSize: 11, weight: .regular)
            primary.textColor = CockpitTokens.Color.muted.withAlphaComponent(0.82)
        case "note":
            primary.stringValue = meaningful(row.noteText) ?? ""
            primary.font = .systemFont(ofSize: 11, weight: .regular)
            primary.textColor = CockpitTokens.Color.muted.withAlphaComponent(0.82)
        case "priority":
            primary.stringValue = priorityLabel(row.priority)
            primary.font = .systemFont(ofSize: 12.5, weight: .semibold)
            primary.textColor = priorityColor(row.priority)
            primary.alignment = .center
        case "domain":
            primary.stringValue = row.domainLabel ?? "-"
        case "resource":
            primary.stringValue = row.resourceClass ?? "-"
            primary.font = .systemFont(ofSize: 10.5, weight: .regular)
            primary.textColor = CockpitTokens.Color.muted.withAlphaComponent(0.66)
        case "id":
            primary.stringValue = row.workID ?? row.dedupKey
            primary.font = .systemFont(ofSize: 10.5, weight: .regular)
            primary.textColor = CockpitTokens.Color.muted.withAlphaComponent(0.62)
        default:
            primary.stringValue = "-"
        }
        toolTip = primary.stringValue
        needsLayout = true
    }

    override func layout() {
        let iconSize: CGFloat = activeColumnID == "owner" ? min(17, max(12, bounds.width - 2)) : 16
        if activeColumnID == "module" {
            let availableWidth = max(38, bounds.width - 12)
            let labelWidth = min(max(40, primary.intrinsicContentSize.width + 16), availableWidth)
            let pillY: CGFloat = secondary.isHidden ? 16 : 8
            pill.frame = NSRect(x: 6, y: pillY, width: labelWidth, height: 16)
            primary.frame = NSRect(x: 6, y: pillY + 2, width: labelWidth, height: 13)
            secondary.frame = NSRect(x: 6, y: 25, width: max(1, bounds.width - 12), height: 14)
        } else {
            pill.frame = NSRect(x: max(4, (bounds.width - 22) / 2), y: 13, width: 22, height: 22)
            let iconX = activeColumnID == "owner"
                ? max(0, min(2, bounds.width - iconSize))
                : max(4, (bounds.width - iconSize) / 2)
            icon.frame = NSRect(x: iconX, y: 14, width: iconSize, height: iconSize)
            if activeColumnID == "state" || activeColumnID == "priority" {
                primary.frame = NSRect(x: 0, y: 14, width: max(1, bounds.width), height: 18)
            } else {
                let depthInset = activeColumnID == "work" ? CGFloat(hierarchyDepth) * 18 : 0
                primary.frame = NSRect(x: 8 + depthInset, y: secondary.isHidden ? 13 : 5, width: max(1, bounds.width - 16 - depthInset), height: 19)
                secondary.frame = NSRect(x: 8 + depthInset, y: 24, width: max(1, bounds.width - 16 - depthInset), height: 14)
                return
            }
            secondary.frame = NSRect(x: 8, y: 24, width: max(1, bounds.width - 16), height: 14)
        }
    }

    private func meaningful(_ value: String?) -> String? {
        let text = (value ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, text != "-", text != "—" else { return nil }
        return text
    }

    private func priorityLabel(_ value: String?) -> String {
        let text = meaningful(value) ?? ""
        if let rank = Int(text) {
            return "P\(rank)"
        }
        if text.count > 3, text.lowercased().hasPrefix("p") {
            return String(text.prefix(3)).uppercased()
        }
        return text.uppercased()
    }

    private func priorityColor(_ value: String?) -> NSColor {
        let text = (value ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if let rank = Int(text) {
            if rank <= 0 { return CockpitTokens.Color.red }
            if rank <= 2 { return CockpitTokens.Color.amber }
            if rank <= 4 { return CockpitTokens.Color.blue2 }
            return CockpitTokens.Color.muted
        }
        if text.contains("p0") || text.contains("critical") { return CockpitTokens.Color.red }
        if text.contains("p1") || text.contains("high") { return CockpitTokens.Color.amber }
        if text.contains("p2") || text.contains("medium") { return CockpitTokens.Color.blue2 }
        return CockpitTokens.Color.muted
    }

    private func moduleBadge(_ row: CockpitRow) -> String {
        let raw = meaningful(row.moduleLabel) ?? meaningful(row.module) ?? meaningful(row.domainLabel) ?? ""
        let normalized = raw
            .replacingOccurrences(of: "_", with: " ")
            .replacingOccurrences(of: "-", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !normalized.isEmpty else { return "" }
        return shortModuleLabel(normalized).uppercased()
    }

    private func shortModuleLabel(_ value: String) -> String {
        let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleaned = text
            .replacingOccurrences(of: "Other / Unsorted", with: "")
            .replacingOccurrences(of: "Other", with: "", options: .caseInsensitive)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return String(cleaned.prefix(16))
    }

    private func moduleSubline(_ row: CockpitRow) -> String {
        let label = meaningful(row.moduleLabel)
        let domain = meaningful(row.domainLabel)
        if let domain,
           domain.caseInsensitiveCompare(label ?? "") != .orderedSame,
           domain.caseInsensitiveCompare(moduleBadge(row)) != .orderedSame {
            return shortDomainLabel(domain)
        }
        return ""
    }

    private func shortDomainLabel(_ value: String) -> String {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return String(trimmed.prefix(28))
    }

    private func ownerBadgeColor(_ row: CockpitRow) -> NSColor {
        switch ownerKey(row) {
        case "claude":
            return NSColor(calibratedRed: 1.00, green: 0.42, blue: 0.20, alpha: 1)
        case "codex", "mixed":
            return CockpitTokens.Color.blue2
        case "local":
            if isGPU(row) { return CockpitTokens.Color.amber }
            return CockpitTokens.Color.green
        case "operator":
            return CockpitTokens.Color.amber
        default:
            return CockpitTokens.Color.faint
        }
    }

    private func ownerImage(_ row: CockpitRow) -> NSImage? {
        let size: CGFloat = 17
        switch ownerKey(row) {
        case "claude":
            return AgentMarks.owner(.claude, size: size)
        case "codex":
            return AgentMarks.owner(.codex, size: size)
        case "mixed":
            return AgentMarks.owner(.mixed, size: size)
        case "local":


            return AgentMarks.localRunner(size: size, accelerated: isGPU(row), driver: driverKind(row))
        case "operator":
            return AgentMarks.owner(.operatorUser, size: size)
        default:
            return nil
        }
    }


    private func driverKind(_ row: CockpitRow) -> OwnerKind? {
        let raw = row.ownerSessionLabel ?? row.ownerSessionActor ?? row.owner ?? ""
        let lane = raw.split(separator: ":").first.map(String.init) ?? ""
        switch lane.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "claude": return .claude
        case "codex": return .codex
        case "mixed": return .mixed
        default: return nil
        }
    }

    private func ownerTooltip(_ row: CockpitRow) -> String {
        let owner = ownerKey(row)
        let resource = row.resourceClass?.trimmingCharacters(in: .whitespacesAndNewlines)
        if let resource, !resource.isEmpty {
            return "\(owner) | \(resource)"
        }
        return owner
    }

    private func ownerKey(_ row: CockpitRow) -> String {
        let raw = (row.ownerGroup ?? row.owner ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if !raw.isEmpty { return raw }
        let resource = (row.resourceClass ?? "").lowercased()
        let kind = (row.rowKind ?? "").lowercased()
        if resource.contains("gpu") || resource.contains("cpu") || kind.contains("local") {
            return "local"
        }
        return "unowned"
    }

    private func isGPU(_ row: CockpitRow) -> Bool {
        let haystack = [
            row.resourceClass,
            row.rowKind,
            row.workID,
            row.jobID,
            row.title,
        ]
        .compactMap { $0?.lowercased() }
        .joined(separator: " ")
        return ["gpu", "mlx", "metal", "mps", "local_gpu"].contains { haystack.contains($0) }
    }
}

final class CockpitProgressCell: NSView {
    private enum ProgressColor {
        static let fillStart = NSColor(calibratedRed: 59.0 / 255.0, green: 120.0 / 255.0, blue: 1.0, alpha: 1)
        static let fillMid = NSColor(calibratedRed: 95.0 / 255.0, green: 152.0 / 255.0, blue: 1.0, alpha: 1)
        static let fillEnd = NSColor(calibratedRed: 167.0 / 255.0, green: 202.0 / 255.0, blue: 1.0, alpha: 1)
        static let sweepMid = NSColor(calibratedRed: 91.0 / 255.0, green: 151.0 / 255.0, blue: 1.0, alpha: 1)
        static let sweepEnd = NSColor(calibratedRed: 173.0 / 255.0, green: 205.0 / 255.0, blue: 1.0, alpha: 1)
    }

    private let label = CockpitUI.label("", size: 11.5, weight: .semibold, color: CockpitTokens.Color.muted, align: .right)
    private let sweepContainer = CALayer()
    private let sweepLayer = CAGradientLayer()
    private var row: CockpitRow?
    private var sweepSignature = ""
    var animationsEnabled = true {
        didSet {
            if oldValue != animationsEnabled {
                updateSweepLayer()
            }
        }
    }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.isGeometryFlipped = true
        addSubview(label)
        sweepContainer.masksToBounds = true
        sweepContainer.cornerRadius = 3.5
        sweepContainer.isHidden = true
        sweepLayer.startPoint = CGPoint(x: 0, y: 0.5)
        sweepLayer.endPoint = CGPoint(x: 1, y: 0.5)
        sweepLayer.colors = [
            ProgressColor.fillStart.withAlphaComponent(0.00).cgColor,
            ProgressColor.sweepMid.withAlphaComponent(0.46).cgColor,
            ProgressColor.sweepEnd.withAlphaComponent(0.92).cgColor,
            NSColor.white.withAlphaComponent(0.28).cgColor,
            ProgressColor.fillStart.withAlphaComponent(0.00).cgColor,
        ]
        sweepLayer.locations = [0, 0.30, 0.52, 0.64, 1]
        sweepLayer.shadowColor = CockpitTokens.Color.blue.cgColor
        sweepLayer.shadowOpacity = 0.55
        sweepLayer.shadowRadius = 12
        sweepLayer.shadowOffset = .zero
        sweepContainer.addSublayer(sweepLayer)
        ensureSweepLayerAttached()
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        if window == nil {
            stopSweepAnimation()
        } else {
            updateSweepLayer()
        }
    }

    func configure(row: CockpitRow) {
        self.row = row
        label.stringValue = visibleProgressText(row)
        label.isHidden = label.stringValue.isEmpty
        updateSweepLayer()
        needsDisplay = true
    }

    override func layout() {
        label.frame = NSRect(x: 6, y: 13, width: 54, height: 18)
        updateSweepLayer()
    }

    override func draw(_ dirtyRect: NSRect) {
        guard let row else { return }
        let hasLabel = !visibleProgressText(row).isEmpty
        let x: CGFloat = hasLabel ? 66 : 8
        let y: CGFloat = 18
        let width = max(20, bounds.width - x - 12)
        let trackRect = NSRect(x: x, y: y, width: width, height: 7)
        let track = NSBezierPath(roundedRect: trackRect, xRadius: 3.5, yRadius: 3.5)
        let isLive = row.status.lowercased().contains("run") || row.status.lowercased().contains("active")
        (isLive ? CockpitTokens.Color.blue.withAlphaComponent(0.075) : NSColor.white.withAlphaComponent(0.055)).setFill()
        track.fill()
        CockpitTokens.Color.blue.withAlphaComponent(isLive ? 0.115 : 0.0).setStroke()
        track.lineWidth = isLive ? 0.75 : 0
        if isLive { track.stroke() }
        let pct = row.effectivePct
        NSGraphicsContext.saveGraphicsState()
        track.addClip()
        if let pct {
            drawDeterminateProgress(row: row, x: x, y: y, width: width, height: 7, fraction: CGFloat(pct / 100))
        } else if shouldDrawProgressTrack(row) {
            drawIndeterminateProgress(row: row, x: x, y: y, width: width)
        }
        NSGraphicsContext.restoreGraphicsState()
    }

    private func drawDeterminateProgress(row: CockpitRow, x: CGFloat, y: CGFloat, width: CGFloat, height: CGFloat, fraction: CGFloat) {
        let frac = max(0.02, min(1, fraction))
        let fillRect = NSRect(x: x, y: y, width: max(5, width * frac), height: height)
        let fill = NSBezierPath(roundedRect: fillRect, xRadius: height / 2, yRadius: height / 2)
        NSGraphicsContext.saveGraphicsState()
        let shadow = NSShadow()
        let active = row.status.lowercased().contains("run") || row.status.lowercased().contains("active")
        shadow.shadowColor = CockpitTokens.Color.blue.withAlphaComponent(active ? 0.58 : 0.20)
        shadow.shadowBlurRadius = active ? 14 : 5
        shadow.shadowOffset = .zero
        shadow.set()
        let gradient = NSGradient(colors: [
            ProgressColor.fillStart.withAlphaComponent(active ? 0.76 : 0.30),
            ProgressColor.fillMid.withAlphaComponent(active ? 0.92 : 0.38),
            ProgressColor.fillEnd.withAlphaComponent(active ? 0.82 : 0.30),
        ])
        gradient?.draw(in: fill, angle: 0)
        NSGraphicsContext.restoreGraphicsState()

    }

    private func drawIndeterminateProgress(row: CockpitRow, x: CGFloat, y: CGFloat, width: CGFloat) {
        let active = row.status.lowercased().contains("run") || row.status.lowercased().contains("active")
        guard !active else { return }
        let segmentWidth = max(42, min(82, width * 0.42))
        let segmentX = x + (width - segmentWidth) * 0.52
        let rect = NSRect(x: segmentX, y: y, width: segmentWidth, height: 7)
        let segment = NSBezierPath(roundedRect: rect, xRadius: 3.5, yRadius: 3.5)
        NSGraphicsContext.saveGraphicsState()
        let shadow = NSShadow()
        shadow.shadowColor = CockpitTokens.Color.blue.withAlphaComponent(active ? 0.56 : 0.18)
        shadow.shadowBlurRadius = active ? 14 : 5
        shadow.shadowOffset = .zero
        shadow.set()
        let gradient = NSGradient(colors: [
            ProgressColor.fillStart.withAlphaComponent(0.00),
            ProgressColor.fillStart.withAlphaComponent(active ? 0.34 : 0.12),
            NSColor(calibratedRed: 152.0 / 255.0, green: 194.0 / 255.0, blue: 1.0, alpha: 1).withAlphaComponent(active ? 0.74 : 0.22),
            NSColor.white.withAlphaComponent(active ? 0.18 : 0.06),
            ProgressColor.fillStart.withAlphaComponent(0.00),
        ])
        gradient?.draw(in: segment, angle: 0)
        NSGraphicsContext.restoreGraphicsState()
    }

    private func updateSweepLayer() {
        ensureSweepLayerAttached()
        guard animationsEnabled, let row, bounds.width > 0, bounds.height > 0 else {
            stopSweepAnimation()
            return
        }
        let status = row.status.lowercased()
        let isLive = status.contains("run") || status.contains("active")
        guard isLive, shouldDrawProgressTrack(row) else {
            stopSweepAnimation()
            return
        }

        let hasLabel = !visibleProgressText(row).isEmpty
        let x: CGFloat = hasLabel ? 66 : 8
        let y: CGFloat = 18
        let height: CGFloat = 7
        let width = max(20, bounds.width - x - 12)
        let hostWidth: CGFloat
        if let pct = row.effectivePct {
            let fraction = max(0.02, min(1, CGFloat(pct / 100)))
            hostWidth = max(5, width * fraction)
        } else {
            hostWidth = width
        }
        guard hostWidth > 24 else {
            stopSweepAnimation()
            return
        }

        sweepContainer.isHidden = false
        sweepContainer.frame = CGRect(x: x, y: y, width: hostWidth, height: height)
        sweepContainer.cornerRadius = height / 2
        let sweepWidth = max(42, min(86, hostWidth * 0.44))
        let nextSignature = "\(row.dedupKey):\(Int(hostWidth.rounded())):\(Int(sweepWidth.rounded()))"
        if nextSignature != sweepSignature {
            sweepLayer.removeAnimation(forKey: "progressSweep")
            sweepSignature = nextSignature
        }
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        sweepLayer.bounds = CGRect(x: 0, y: 0, width: sweepWidth, height: height)
        sweepLayer.cornerRadius = height / 2
        sweepLayer.position = CGPoint(x: -sweepWidth / 2, y: height / 2)
        CATransaction.commit()

        if sweepLayer.animation(forKey: "progressSweep") == nil {
            let animation = CABasicAnimation(keyPath: "position.x")
            animation.fromValue = -sweepWidth / 2
            animation.toValue = hostWidth + sweepWidth / 2
            animation.duration = 1.55
            animation.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
            animation.repeatCount = .infinity
            animation.isRemovedOnCompletion = false
            sweepLayer.add(animation, forKey: "progressSweep")
        }
    }

    private func stopSweepAnimation() {
        sweepContainer.isHidden = true
        sweepLayer.removeAnimation(forKey: "progressSweep")
        sweepSignature = ""
    }

    private func ensureSweepLayerAttached() {
        wantsLayer = true
        guard let layer else { return }
        layer.isGeometryFlipped = true
        if sweepContainer.superlayer !== layer {
            sweepContainer.removeFromSuperlayer()
            layer.addSublayer(sweepContainer)
        }
    }

    private func visibleProgressText(_ row: CockpitRow) -> String {
        if let pct = row.effectivePct {
            return "\(Int(pct.rounded()))%"
        }
        return meaningful(row.pctDisplay) ?? ""
    }

    private func meaningful(_ value: String?) -> String? {
        let text = (value ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, text != "-", text != "—" else { return nil }
        return text
    }

    private func shouldDrawProgressTrack(_ row: CockpitRow) -> Bool {
        if row.effectivePct != nil { return true }
        let status = row.status.lowercased()
        return status.contains("run") || status.contains("active") || status.contains("launch")
    }
}

private final class CockpitActionSelection: NSObject {
    let rowIndex: Int
    let actionID: String

    init(rowIndex: Int, actionID: String) {
        self.rowIndex = rowIndex
        self.actionID = actionID
    }
}

final class CockpitActionCell: NSView {
    private let button = CockpitActionGlyph()
    private let actionMenu = NSMenu()
    private var hasEnabledActions = false
    private var primarySelection: CockpitActionSelection?
    private weak var actionTarget: AnyObject?
    private var actionSelector: Selector?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        button.activationHandler = { [weak self] in
            self?.activatePrimaryOrMenu()
        }
        button.menuHandler = { [weak self] in
            self?.showMenu()
        }
        addSubview(button)
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    func configure(row: CockpitRow, rowIndex: Int, target: AnyObject, action: Selector) {
        actionMenu.removeAllItems()
        let actions = row.actions.sorted(by: { $0.displayOrder < $1.displayOrder })
        actionTarget = target
        actionSelector = action
        primarySelection = nil
        button.kind = .menu
        if let primary = actions.first(where: { ["jobs.pause", "jobs.resume"].contains($0.id) && $0.isEnabled }) {
            primarySelection = CockpitActionSelection(rowIndex: rowIndex, actionID: primary.id)
            button.kind = primary.id == "jobs.resume" ? .resume : .pause
        }
        for item in actions {
            let menuItem = NSMenuItem(title: item.label, action: action, keyEquivalent: "")
            menuItem.target = target
            menuItem.representedObject = CockpitActionSelection(rowIndex: rowIndex, actionID: item.id)
            menuItem.isEnabled = item.isEnabled
            if let reason = item.disabledReason, !reason.isEmpty {
                menuItem.toolTip = reason
            }
            actionMenu.addItem(menuItem)
        }
        hasEnabledActions = row.actions.contains(where: \.isEnabled)
        if let primarySelection,
           let primary = actions.first(where: { $0.id == primarySelection.actionID }) {
            button.toolTip = "\(primary.label) (right-click for row actions)"
            button.accessibilityTitle = primary.label
            button.accessibilityHelpText = "Press to \(primary.label.lowercased()); right-click for row actions"
        } else {
            button.toolTip = row.actions.isEmpty ? "No row actions" : "Row actions"
            button.accessibilityTitle = row.actions.isEmpty ? "No row actions" : "Row actions"
            button.accessibilityHelpText = row.actions.isEmpty ? "No actions are available for this row" : "Press to open row actions"
        }
        button.isEnabled = hasEnabledActions
    }

    override func layout() {
        button.frame = NSRect(x: max(2, (bounds.width - 28) / 2), y: 11, width: 28, height: 24)
    }

    override func mouseDown(with event: NSEvent) {
        guard hasEnabledActions, !actionMenu.items.isEmpty else { return }
        if event.modifierFlags.contains(.control) {
            showMenu()
            return
        }
        activatePrimaryOrMenu()
    }

    override func rightMouseDown(with event: NSEvent) {
        guard hasEnabledActions, !actionMenu.items.isEmpty else { return }
        showMenu()
    }

    private func showMenu() {
        actionMenu.popUp(positioning: nil, at: NSPoint(x: button.frame.minX, y: button.frame.maxY + 4), in: self)
    }

    fileprivate func activatePrimaryOrMenu() {
        guard hasEnabledActions, !actionMenu.items.isEmpty else { return }
        if let primarySelection, let actionSelector {
            let item = NSMenuItem(title: "", action: actionSelector, keyEquivalent: "")
            item.representedObject = primarySelection
            _ = NSApp.sendAction(actionSelector, to: actionTarget, from: item)
            return
        }
        showMenu()
    }
}

private enum CockpitActionGlyphKind {
    case menu
    case pause
    case resume
}

private final class CockpitActionGlyph: NSView {
    var activationHandler: (() -> Void)?
    var menuHandler: (() -> Void)?
    var accessibilityTitle = "Row actions" {
        didSet { setAccessibilityLabel(accessibilityTitle) }
    }
    var accessibilityHelpText = "Press to open row actions" {
        didSet { setAccessibilityHelp(accessibilityHelpText) }
    }
    var isEnabled = false {
        didSet { needsDisplay = true }
    }
    var kind: CockpitActionGlyphKind = .menu {
        didSet { needsDisplay = true }
    }
    private var isHovered = false {
        didSet { needsDisplay = true }
    }
    private var isPressed = false {
        didSet { needsDisplay = true }
    }
    private var trackingArea: NSTrackingArea?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        setAccessibilityElement(true)
        setAccessibilityRole(.button)
        setAccessibilityLabel(accessibilityTitle)
        setAccessibilityHelp(accessibilityHelpText)
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }
    override var acceptsFirstResponder: Bool { isEnabled }

    override func updateTrackingAreas() {
        if let trackingArea {
            removeTrackingArea(trackingArea)
        }
        let area = NSTrackingArea(rect: bounds, options: [.mouseEnteredAndExited, .activeAlways], owner: self, userInfo: nil)
        addTrackingArea(area)
        trackingArea = area
        super.updateTrackingAreas()
    }

    override func mouseEntered(with event: NSEvent) {
        isHovered = true
    }

    override func mouseExited(with event: NSEvent) {
        isHovered = false
        isPressed = false
    }

    override func mouseDown(with event: NSEvent) {
        guard isEnabled else { return }
        isPressed = true
        if event.modifierFlags.contains(.control) {
            menuHandler?()
        } else {
            activationHandler?()
        }
        isPressed = false
    }

    override func rightMouseDown(with event: NSEvent) {
        guard isEnabled else { return }
        menuHandler?()
    }

    override func keyDown(with event: NSEvent) {
        guard isEnabled else { return }
        let key = event.charactersIgnoringModifiers ?? event.characters ?? ""
        if key == " " || key == "\r" || key == "\n" {
            activationHandler?()
        } else {
            super.keyDown(with: event)
        }
    }

    override func accessibilityPerformPress() -> Bool {
        guard isEnabled else { return false }
        activationHandler?()
        return true
    }

    override func draw(_ dirtyRect: NSRect) {
        let color = isEnabled ? CockpitTokens.Color.text : CockpitTokens.Color.muted.withAlphaComponent(0.42)
        let rect = bounds.insetBy(dx: 3.5, dy: 4)
        let border = NSBezierPath(roundedRect: rect, xRadius: 7, yRadius: 7)
        let fillAlpha: CGFloat = isPressed ? 0.13 : (isHovered ? 0.075 : (isEnabled ? 0.022 : 0.012))
        CockpitTokens.Color.panel2.withAlphaComponent(fillAlpha).setFill()
        border.fill()
        CockpitTokens.Color.line.withAlphaComponent(isEnabled ? (isHovered ? 0.72 : 0.42) : 0.20).setStroke()
        border.lineWidth = 1
        border.stroke()

        color.withAlphaComponent(isEnabled ? 0.72 : 0.24).setFill()
        switch kind {
        case .menu:
            let y = bounds.midY - 1.5
            for x in [bounds.midX - 6, bounds.midX, bounds.midX + 6] {
                NSBezierPath(ovalIn: NSRect(x: x - 1.6, y: y, width: 3.2, height: 3.2)).fill()
            }
        case .pause:
            NSBezierPath(roundedRect: NSRect(x: bounds.midX - 5, y: bounds.midY - 5, width: 3, height: 10), xRadius: 1, yRadius: 1).fill()
            NSBezierPath(roundedRect: NSRect(x: bounds.midX + 2, y: bounds.midY - 5, width: 3, height: 10), xRadius: 1, yRadius: 1).fill()
        case .resume:
            let path = NSBezierPath()
            path.move(to: NSPoint(x: bounds.midX - 4, y: bounds.midY - 6))
            path.line(to: NSPoint(x: bounds.midX - 4, y: bounds.midY + 6))
            path.line(to: NSPoint(x: bounds.midX + 7, y: bounds.midY))
            path.close()
            path.fill()
        }
    }
}

final class CockpitDetailCell: NSView {
    private enum DetailLayout {
        static let textX: CGFloat = 10
        static let textY: CGFloat = 8
        static let rowRightPadding: CGFloat = 20
        static let labelIndent: CGFloat = 58
        static let actionHeight: CGFloat = 42
        static let verticalPadding: CGFloat = 18
    }

    private let text = NSTextView()
    private var actionButtons: [NSButton] = []
    private var rowIndex = -1
    private weak var actionTarget: AnyObject?
    private var actionSelector: Selector?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        text.isEditable = false
        text.isSelectable = true
        text.drawsBackground = false
        text.textColor = CockpitTokens.Color.muted
        text.font = .systemFont(ofSize: 11, weight: .regular)
        text.textContainerInset = NSSize(width: 10, height: 7)
        text.textContainer?.lineFragmentPadding = 0
        text.textContainer?.widthTracksTextView = true
        text.isHorizontallyResizable = false
        text.isVerticallyResizable = false
        addSubview(text)
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    func configure(row: CockpitRow, rowIndex: Int, target: AnyObject, action: Selector) {
        self.rowIndex = rowIndex
        self.actionTarget = target
        self.actionSelector = action
        actionButtons.forEach { $0.removeFromSuperview() }
        actionButtons = []
        for item in inlineActions(row.actions) {
            let button = CockpitButton(title: shortActionLabel(item), target: self, action: #selector(detailActionPressed(_:)))
            button.identifier = NSUserInterfaceItemIdentifier(item.id)
            button.toolTip = item.label
            button.font = .systemFont(ofSize: 10.5, weight: .semibold)
            let tint = actionTint(item)
            button.contentTintColor = tint.withAlphaComponent(0.90)
            button.layer?.backgroundColor = tint.withAlphaComponent(0.026).cgColor
            button.layer?.borderColor = tint.withAlphaComponent(0.10).cgColor
            addSubview(button)
            actionButtons.append(button)
        }
        text.textStorage?.setAttributedString(Self.detailText(row))
        text.toolTip = fullDetailText(row)
    }

    override func layout() {
        let hasActions = !actionButtons.isEmpty
        let textWidth = expandedTextWidth()
        text.frame = NSRect(
            x: DetailLayout.textX,
            y: DetailLayout.textY,
            width: textWidth,
            height: max(1, bounds.height - (hasActions ? DetailLayout.actionHeight : 16))
        )
        text.textContainer?.containerSize = NSSize(width: textWidth - text.textContainerInset.width * 2, height: CGFloat.greatestFiniteMagnitude)
        var x: CGFloat = DetailLayout.textX
        let y = max(8, bounds.height - 31)
        for button in actionButtons {
            let width = min(92, max(46, button.intrinsicContentSize.width + 16))
            button.frame = NSRect(x: x, y: y, width: width, height: 23)
            x += width + 6
            button.isHidden = x > bounds.width - 10
        }
    }

    static func preferredHeight(row: CockpitRow, tableWidth: CGFloat) -> CGFloat {
        let width = detailTextWidth(tableWidth: tableWidth)
        let textHeight = ceil(detailText(row).boundingRect(
            with: NSSize(width: width - 20, height: CGFloat.greatestFiniteMagnitude),
            options: [.usesLineFragmentOrigin, .usesFontLeading]
        ).height)
        let actionHeight: CGFloat = inlineActionIDs(row.actions).isEmpty ? 0 : DetailLayout.actionHeight
        return max(CockpitTokens.detailHeight, textHeight + DetailLayout.verticalPadding + actionHeight)
    }

    @objc private func detailActionPressed(_ sender: NSButton) {
        guard let actionID = sender.identifier?.rawValue,
              let actionSelector else { return }
        let item = NSMenuItem(title: sender.title, action: actionSelector, keyEquivalent: "")
        item.representedObject = CockpitActionSelection(rowIndex: rowIndex, actionID: actionID)
        _ = NSApp.sendAction(actionSelector, to: actionTarget, from: item)
    }

    private func expandedTextWidth() -> CGFloat {
        guard let tableView = enclosingTableView() else {
            return max(1, bounds.width - 20)
        }
        let origin = convert(NSPoint(x: 0, y: 0), to: tableView)
        return max(bounds.width - 20, tableView.bounds.width - origin.x - DetailLayout.rowRightPadding)
    }

    private func enclosingTableView() -> NSTableView? {
        var view: NSView? = self
        while let current = view {
            if let table = current as? NSTableView { return table }
            view = current.superview
        }
        return nil
    }

    private func inlineActions(_ actions: [CockpitRowAction]) -> [CockpitRowAction] {
        let ids = Self.inlineActionIDs(actions)
        return ids.compactMap { id in actions.first(where: { $0.id == id }) }
    }

    private static func inlineActionIDs(_ actions: [CockpitRowAction]) -> [String] {
        let enabled = actions.filter { $0.isEnabled && $0.id != "jobs.kill" }
        let preferred = [
            "task.assign.claude",
            "task.assign.codex",
            "task.unassign",
            "task.nextup",
            "task.followup",
            "task.defer",
            "task.clear",
            "jobs.start_declared",
            "handoff.create",
            "claim.create",
            "capability.run",
        ]
        var out: [String] = []
        for id in preferred {
            if enabled.contains(where: { $0.id == id }) {
                out.append(id)
            }
        }
        for action in enabled.sorted(by: { $0.displayOrder < $1.displayOrder }) where !out.contains(action.id) {
            out.append(action.id)
        }
        return Array(out.prefix(8))
    }

    private func shortActionLabel(_ action: CockpitRowAction) -> String {
        switch action.id {
        case "task.assign.claude": return "Claude"
        case "task.assign.codex": return "Codex"
        case "task.unassign": return "Unassign"
        case "task.nextup": return "Next"
        case "task.followup": return "Follow"
        case "task.defer": return "Later"
        case "task.clear": return "Hide"
        case "jobs.start_declared": return "Start"
        case "handoff.create": return "Handoff"
        case "claim.create": return "Claim"
        case "capability.run": return "Run"
        default: return action.label
        }
    }

    private func actionTint(_ action: CockpitRowAction) -> NSColor {
        switch action.id {
        case "task.assign.claude":
            return NSColor(calibratedRed: 1.00, green: 0.42, blue: 0.20, alpha: 1)
        case "task.assign.codex":
            return CockpitTokens.Color.blue2
        case "jobs.pause", "task.defer":
            return CockpitTokens.Color.amber
        case "jobs.resume", "jobs.start_declared", "task.nextup":
            return CockpitTokens.Color.green
        case "task.clear", "task.unassign":
            return CockpitTokens.Color.muted
        default:
            return CockpitTokens.Color.text
        }
    }

    private static func detailText(_ row: CockpitRow) -> NSAttributedString {
        let out = NSMutableAttributedString()
        appendDetailLine(out, label: "Why", value: meaningful(row.whyText) ?? "-")
        appendDetailLine(out, label: "Note", value: meaningful(row.noteText) ?? "-")
        appendDetailLine(out, label: "Meta", value: compactMeta(row))
        appendDetailLine(out, label: "Key", value: compactKey(row), isLast: true)
        return out
    }

    private static func appendDetailLine(_ out: NSMutableAttributedString, label: String, value: String, isLast: Bool = false) {
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineBreakMode = .byWordWrapping
        paragraph.tabStops = [NSTextTab(textAlignment: .left, location: DetailLayout.labelIndent)]
        paragraph.defaultTabInterval = DetailLayout.labelIndent
        paragraph.headIndent = DetailLayout.labelIndent
        paragraph.paragraphSpacing = 2
        let labelAttrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedSystemFont(ofSize: 10, weight: .bold),
            .foregroundColor: CockpitTokens.Color.faint.withAlphaComponent(0.86),
            .paragraphStyle: paragraph,
        ]
        let valueAttrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 11, weight: .medium),
            .foregroundColor: CockpitTokens.Color.muted.withAlphaComponent(0.95),
            .paragraphStyle: paragraph,
        ]
        out.append(NSAttributedString(string: "\(label.uppercased())\t", attributes: labelAttrs))
        out.append(NSAttributedString(string: value, attributes: valueAttrs))
        if !isLast {
            out.append(NSAttributedString(string: "\n", attributes: valueAttrs))
        }
    }

    private static func detailTextWidth(tableWidth: CGFloat) -> CGFloat {
        max(320, tableWidth - 140)
    }

    private static func compactMeta(_ row: CockpitRow) -> String {
        [
            row.status,
            row.scope,
            meaningful(row.rowKind),
            meaningful(row.moduleLabel) ?? meaningful(row.module),
            meaningful(row.domainLabel),
            meaningful(row.resourceClass),
        ]
        .compactMap { $0 }
        .joined(separator: "  /  ")
    }

    private static func compactKey(_ row: CockpitRow) -> String {
        let work = meaningful(row.workID) ?? "-"
        return "\(work)  /  \(row.dedupKey)"
    }

    private func fullDetailText(_ row: CockpitRow) -> String {
        [
            "Why: \(Self.meaningful(row.whyText) ?? "-")",
            "Note: \(Self.meaningful(row.noteText) ?? "-")",
            "Status: \(row.status)",
            "Scope: \(row.scope)",
            "Kind: \(Self.meaningful(row.rowKind) ?? "-")",
            "Module: \(Self.meaningful(row.moduleLabel) ?? Self.meaningful(row.module) ?? "-")",
            "Domain: \(Self.meaningful(row.domainLabel) ?? "-")",
            "Resource: \(Self.meaningful(row.resourceClass) ?? "-")",
            "ID: \(Self.meaningful(row.workID) ?? "-")",
            "Dedup: \(row.dedupKey)",
        ].joined(separator: "\n")
    }

    private static func meaningful(_ value: String?) -> String? {
        let text = (value ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, text != "-", text != "—" else { return nil }
        return text
    }
}
