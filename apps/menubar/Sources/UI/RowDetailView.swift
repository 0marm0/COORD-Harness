import AppKit


final class RowDetailView: RowView {
    private let jobId: String
    private let taskTitle: String
    private let contextRef: String?

    init(_ row: Row, taskActionsEnabled: Bool) {
        self.jobId = row.jobId ?? row.id ?? row.roadmapId ?? ""
        self.taskTitle = row.title
        self.contextRef = row.contextPackRef
        super.init(frame: NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: 10))
        let textX = Tokens.Layout.expandTextX, textW = Tokens.Layout.popoverWidth - Tokens.Layout.expandTextX - 14
        var y: CGFloat = 2
        wantsLayer = true


        var shownIntro: String?
        if let s = (row.currentStep ?? row.step ?? row.detail),
           shouldShowIntro(s, row: row) {
            let f = wrapLabel(plain: s, size: 10.5, color: NSColor(white: 0.66, alpha: 0.92), width: textW)
            f.frame = NSRect(x: textX, y: y, width: textW, height: f.fittingSize.height); addSubview(f)
            y += f.fittingSize.height + 5
            shownIntro = s
        }

        func pair(_ label: String, _ value: String?) {
            guard let value = value, !value.isEmpty else { return }
            let f = pairLabel(label, value, width: textW)
            f.frame = NSRect(x: textX, y: y, width: textW, height: f.fittingSize.height); addSubview(f)
            y += f.fittingSize.height + 3
        }


        pair("Module", row.moduleLabel ?? row.module)
        if row.crossAgentHandoff == true {
            pair("Handoff", row.handoffLabel ?? [row.handoffFrom, row.handoffTo].compactMap { $0 }.joined(separator: " -> "))
        }
        if let deps = row.dependsOn, !deps.isEmpty { pair("Depends on", deps.joined(separator: ", ")) }
        if let note = mergedNote(row: row, context: "", shownIntro: shownIntro) {
            pair("Note", String(note.prefix(260)))
        }
        if let ds = row.doneSignal { pair("Done when", String(ds.prefix(200))) }
        pair("Acceptance", acceptanceText(row))
        if let ref = row.contextPackRef { pair("Context", String(ref.prefix(200))) }
        if let rubric = rubricText(row) { pair("Rubric", rubric) }
        if let budget = row.tokenBudget { pair("Token budget", "\(budget)") }
        pair("Heartbeat due", row.heartbeatDueAt)
        pair("Due", row.dueDate)
        if !row.showsBar { pair("Why next", row.nextRankReason) }
        let context = runProfile(row, includeStatusEta: !row.showsBar)
        if !context.isEmpty && !row.showsBar { pair("Run", context) }


        let meta = row.roadmapId ?? ""
        if !meta.isEmpty {
            let m = UI.label(meta, size: 9.5, color: NSColor(white: 0.46, alpha: 0.85))
            m.frame = NSRect(x: textX, y: y + 2, width: Tokens.Layout.popoverWidth - textX - 14, height: 13); addSubview(m)
            y += 18
        }

        if taskActionsEnabled && !jobId.isEmpty {
            let cur = row.iconOwnerKind

            let aL = UI.label("Send:", size: 10, color: NSColor(white: 0.55, alpha: 0.9))
            aL.frame = NSRect(x: textX, y: y, width: 116, height: 16); addSubview(aL)
            addSubview(chip("Claude", Art.claude, x: 133, y: y - 1, current: cur == .claude, action: #selector(handoffClaude)))
            addSubview(chip("Codex", Art.codex, x: 205, y: y - 1, current: cur == .codex, action: #selector(handoffCodex)))
            y += 22

            let moveL = UI.label("Move:", size: 10, color: NSColor(white: 0.55, alpha: 0.9))
            moveL.frame = NSRect(x: textX, y: y, width: 54, height: 16); addSubview(moveL)
            addSubview(textButton("Next up", x: 133, y: y - 1, w: 58, action: #selector(doNextup)))
            addSubview(textButton("Follow-up", x: 200, y: y - 1, w: 72, action: #selector(doFollowup)))
            addSubview(textButton("Later", x: 281, y: y - 1, w: 42, action: #selector(doDefer)))
            addSubview(textButton("Hide", x: 330, y: y - 1, w: 36, action: #selector(doClear)))
            y += 20

            let inspectL = UI.label("Inspect:", size: 10, color: NSColor(white: 0.55, alpha: 0.9))
            inspectL.frame = NSRect(x: textX, y: y, width: 54, height: 16); addSubview(inspectL)
            addSubview(textButton("Open Harness", x: 133, y: y - 1, w: 92, action: #selector(openHarness)))
            y += 20

            if let cached = CapabilityResultCache.latest(job: jobId) {
                let prefix = cached.ok ? cached.label : "\(cached.label) failed"
                pair("Tool result", "\(prefix): \(cached.statusText)")
            }
        }

        y += 8


        frame = NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: y)
    }

    private func shouldShowIntro(_ raw: String, row: Row) -> Bool {
        let s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty else { return false }
        let lower = s.lowercased()
        if row.title.lowercased().contains(String(lower.prefix(40))) { return false }
        if let note = row.note?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased(), note == lower {
            return false
        }
        if row.showsBar && lower.contains("embedding") && row.total != nil {
            return false
        }
        return true
    }

    private func mergedNote(row r: Row, context: String, shownIntro: String?) -> String? {
        var parts: [String] = []
        if !context.isEmpty { parts.append(context) }
        if let note = r.note?.trimmingCharacters(in: .whitespacesAndNewlines), !note.isEmpty {
            let intro = shownIntro?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            if intro == nil || intro != note.lowercased() {
                parts.append(note)
            }
        }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    private func acceptanceText(_ row: Row) -> String? {
        for value in [row.acceptanceSummary, row.acceptance, row.acceptanceJson] {
            if let text = value?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty, text != "[]" {
                return String(text.prefix(220))
            }
        }
        return nil
    }

    private func rubricText(_ row: Row) -> String? {
        let parts = [row.rubricState, row.rubricVerdict].compactMap {
            $0?.trimmingCharacters(in: .whitespacesAndNewlines)
        }.filter { !$0.isEmpty }
        return parts.isEmpty ? nil : parts.joined(separator: " / ")
    }


    private func runProfile(_ r: Row, includeStatusEta: Bool = true) -> String {
        var parts: [String] = []
        if includeStatusEta, let s = r.status?.uppercased(), !["QUEUED", "NEXT", "OPEN"].contains(s) { parts.append(s) }
        let eta = r.etaDisplay; if includeStatusEta && !eta.isEmpty { parts.append("ETA " + eta) }
        if let p = r.priority?.trimmingCharacters(in: .whitespaces), !p.isEmpty, p != "6", p != "6.0", p.uppercased() != "P6" {
            parts.append(p.uppercased().hasPrefix("P") ? p.uppercased() : "P" + p)
        }
        if let lane = (r.resourceClass ?? r.lane)?.uppercased(), !["CPU", "NONE", ""].contains(lane) { parts.append(lane) }
        if let m = r.modelLabel, !m.isEmpty { parts.append(m) }
        return parts.joined(separator: " · ")
    }

    private func wrapLabel(plain: String, size: CGFloat, color: NSColor, width: CGFloat) -> NSTextField {
        let f = NSTextField(wrappingLabelWithString: plain)
        f.font = .systemFont(ofSize: size); f.textColor = color
        f.isBordered = false; f.drawsBackground = false; f.isEditable = false
        f.preferredMaxLayoutWidth = width; f.frame.size.width = width; f.layoutSubtreeIfNeeded()
        return f
    }
    private func pairLabel(_ label: String, _ value: String, width: CGFloat) -> NSTextField {
        let s = NSMutableAttributedString()
        s.append(NSAttributedString(string: label + "   ", attributes: [
            .font: NSFont.boldSystemFont(ofSize: 10), .foregroundColor: NSColor(white: 0.58, alpha: 0.95)]))
        s.append(NSAttributedString(string: value, attributes: [
            .font: NSFont.systemFont(ofSize: 10), .foregroundColor: NSColor(white: 0.78, alpha: 0.95)]))
        let f = NSTextField(labelWithAttributedString: s)
        f.lineBreakMode = .byWordWrapping; f.maximumNumberOfLines = 0
        f.preferredMaxLayoutWidth = width; f.frame.size.width = width; f.layoutSubtreeIfNeeded()
        f.toolTip = value
        return f
    }

    private func chip(_ title: String, _ img: NSImage?, x: CGFloat, y: CGFloat, current: Bool, action: Selector) -> NSButton {
        let b = NSButton(frame: NSRect(x: x, y: y, width: 62, height: 18))
        b.title = title; b.isBordered = false; b.alignment = .left
        b.font = current ? .boldSystemFont(ofSize: 10.5) : .systemFont(ofSize: 10.5)
        b.contentTintColor = current ? NSColor(white: 0.95, alpha: 1) : NSColor(white: 0.5, alpha: 0.8)
        if let icon = icon11(img) { b.image = icon; b.imagePosition = .imageLeft }
        b.target = self; b.action = action
        return b
    }
    private func textButton(_ title: String, x: CGFloat, y: CGFloat, w: CGFloat, action: Selector) -> NSButton {
        let b = NSButton(frame: NSRect(x: x, y: y, width: w, height: 18))
        b.title = title; b.isBordered = false; b.alignment = .left; b.font = .systemFont(ofSize: 10.5)
        b.contentTintColor = NSColor(white: 0.64, alpha: 0.88); b.target = self; b.action = action
        return b
    }
    private func icon11(_ img: NSImage?) -> NSImage? {
        guard let img else { return nil }
        let c = NSImage(size: NSSize(width: 11, height: 11))
        c.lockFocus(); img.draw(in: NSRect(x: 0, y: 0, width: 11, height: 11)); c.unlockFocus()
        return c
    }

    @objc private func doFollowup()   { emit("followup", nil) }
    @objc private func doClear()      { emit("clear", nil) }
    @objc private func doNextup()     { emit("nextup", nil) }
    @objc private func doDefer()      { emit("defer", nil) }
    @objc private func handoffClaude(){ emitHandoff("claude") }
    @objc private func handoffCodex() { emitHandoff("codex") }
    @objc private func openHarness()  { emitOpenHarness() }
    private func emit(_ action: String, _ assignee: String?) {
        RowActions.shared?(.taskAction(job: jobId, action: action, assignee: assignee))
    }
    private func emitHandoff(_ to: String) {
        RowActions.shared?(.handoff(job: jobId, to: to, task: taskTitle, contextRef: contextRef))
    }
    private func emitOpenHarness() {
        var components = URLComponents()
        components.path = "/harness"
        components.queryItems = [
            URLQueryItem(name: "work", value: jobId),
            URLQueryItem(name: "execute", value: "0"),
        ]
        RowActions.shared?(.openDashboard(components.string ?? "/harness"))
    }
    required init?(coder: NSCoder) { nil }
}
