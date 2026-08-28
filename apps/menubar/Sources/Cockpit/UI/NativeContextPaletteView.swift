import AppKit

private class ContextPaletteContainer: NSView {
    override var isFlipped: Bool { true }
}

final class NativeContextPaletteView: NSView, NSSearchFieldDelegate {
    var onSearchRequested: ((String, NativeContextPaletteMode) -> Void)?
    var onReadRequested: ((NativeContextHit) -> Void)?
    var onOpenRequested: ((NativeContextHit) -> Void)?
    var onClose: (() -> Void)?

    private let backdrop = NSVisualEffectView()
    private let panel = ContextPaletteContainer()
    private let titleLabel = CockpitUI.label("Context Explorer", size: 13, weight: .bold, color: CockpitTokens.Color.text)
    private let subtitleLabel = CockpitUI.label("Browse work, evidence, facts, memory, files, and done history", size: 11, weight: .medium, color: CockpitTokens.Color.muted)
    private let closeButton = CockpitButton(title: "", target: nil, action: nil)
    private let searchField = NSSearchField()
    private let modeStrip = ContextPaletteContainer()
    private let suggestionsLabel = CockpitUI.label("GUIDED PATHS", size: 10, weight: .bold, color: CockpitTokens.Color.faint)
    private let suggestionsView = ContextPaletteContainer()
    private let explorerHeadlineLabel = CockpitUI.label("Choose a context path", size: 13.5, weight: .bold, color: CockpitTokens.Color.text)
    private let explorerSubheadLabel = CockpitUI.label("Typing narrows the view; clicking explores what the system already knows.", size: 11, weight: .medium, color: CockpitTokens.Color.muted)
    private let facetStrip = ContextPaletteContainer()
    private let resultScroll = NSScrollView()
    private let resultList = ContextPaletteContainer()
    private let previewPane = ContextPaletteContainer()
    private let previewSourceLabel = CockpitUI.label("", size: 10, weight: .bold, color: CockpitTokens.Color.faint)
    private let previewTitleLabel = CockpitUI.label("Ask a question or choose a suggestion", size: 17, weight: .bold, color: CockpitTokens.Color.text)
    private let previewPointerLabel = CockpitUI.label("", size: 10.5, weight: .medium, color: CockpitTokens.Color.muted)
    private let previewBodyScroll = NSScrollView()
    private let previewBody = NSTextView()
    private let statusLabel = CockpitUI.label("", size: 11, weight: .medium, color: CockpitTokens.Color.muted)
    private let copyPointerButton = CockpitButton(title: "Copy Pointer", target: nil, action: nil)
    private let openButton = CockpitButton(title: "Open", target: nil, action: nil)

    private var modeButtons: [NativeContextPaletteMode: NSButton] = [:]
    private var suggestionButtons: [NSButton] = []
    private var facetButtons: [NSButton] = []
    private var resultButtons: [NSButton] = []
    private var currentMode: NativeContextPaletteMode = .all
    private var currentHits: [String: NativeContextHit] = [:]
    private var currentAnswerCards: [String: NativeContextAnswerCard] = [:]
    private var selectedHitID: String?
    private var selectedAnswerCardID: String?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        isHidden = true
        build()
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    var isOpen: Bool { !isHidden }

    private func build() {
        backdrop.material = .hudWindow
        backdrop.blendingMode = .withinWindow
        backdrop.state = .active
        backdrop.alphaValue = 0.58
        addSubview(backdrop)

        panel.wantsLayer = true
        panel.layer?.cornerRadius = 16
        panel.layer?.backgroundColor = CockpitTokens.Color.panel.withAlphaComponent(0.97).cgColor
        panel.layer?.borderColor = CockpitTokens.Color.blue.withAlphaComponent(0.20).cgColor
        panel.layer?.borderWidth = 1
        panel.layer?.shadowColor = NSColor.black.cgColor
        panel.layer?.shadowOpacity = 0.42
        panel.layer?.shadowRadius = 48
        panel.layer?.shadowOffset = NSSize(width: 0, height: -18)
        addSubview(panel)

        panel.addSubview(titleLabel)
        panel.addSubview(subtitleLabel)
        closeButton.image = NSImage(systemSymbolName: "xmark", accessibilityDescription: "Close")
        closeButton.title = ""
        closeButton.target = self
        closeButton.action = #selector(closePressed)
        closeButton.toolTip = "Close"
        closeButton.layer?.backgroundColor = CockpitTokens.Color.panel2.withAlphaComponent(0.44).cgColor
        panel.addSubview(closeButton)

        searchField.delegate = self
        searchField.placeholderString = "Search, or choose a context path"
        searchField.font = .systemFont(ofSize: 15, weight: .semibold)
        searchField.isBordered = false
        searchField.wantsLayer = true
        searchField.layer?.backgroundColor = CockpitTokens.Color.panel2.withAlphaComponent(0.72).cgColor
        searchField.layer?.borderColor = CockpitTokens.Color.blue.withAlphaComponent(0.20).cgColor
        searchField.layer?.borderWidth = 1
        searchField.layer?.cornerRadius = 12
        panel.addSubview(searchField)

        panel.addSubview(modeStrip)
        for mode in NativeContextPaletteMode.allCases {
            let button = CockpitButton(title: mode.label, target: self, action: #selector(modePressed(_:)))
            button.identifier = NSUserInterfaceItemIdentifier(mode.rawValue)
            button.font = .systemFont(ofSize: 11.5, weight: .bold)
            button.image = NSImage(systemSymbolName: mode.systemSymbolName, accessibilityDescription: mode.label)
            button.imagePosition = .imageLeading
            modeStrip.addSubview(button)
            modeButtons[mode] = button
        }

        panel.addSubview(suggestionsLabel)
        panel.addSubview(suggestionsView)
        panel.addSubview(explorerHeadlineLabel)
        panel.addSubview(explorerSubheadLabel)
        panel.addSubview(facetStrip)

        resultScroll.drawsBackground = false
        resultScroll.hasVerticalScroller = true
        resultScroll.scrollerStyle = .overlay
        resultScroll.borderType = .noBorder
        resultScroll.documentView = resultList
        CockpitScrollChrome.apply(to: resultScroll)
        panel.addSubview(resultScroll)

        previewPane.wantsLayer = true
        previewPane.layer?.cornerRadius = 13
        previewPane.layer?.backgroundColor = CockpitTokens.Color.panel2.withAlphaComponent(0.96).cgColor
        previewPane.layer?.borderColor = CockpitTokens.Color.line.withAlphaComponent(0.18).cgColor
        previewPane.layer?.borderWidth = 1
        panel.addSubview(previewPane)
        previewPane.addSubview(previewSourceLabel)
        previewPane.addSubview(previewTitleLabel)
        previewPane.addSubview(previewPointerLabel)

        previewBody.isEditable = false
        previewBody.isSelectable = true
        previewBody.drawsBackground = false
        previewBody.textColor = CockpitTokens.Color.text.withAlphaComponent(0.84)
        previewBody.font = .systemFont(ofSize: 12.4, weight: .medium)
        previewBody.textContainerInset = NSSize(width: 12, height: 12)
        previewBodyScroll.drawsBackground = false
        previewBodyScroll.hasVerticalScroller = true
        previewBodyScroll.scrollerStyle = .overlay
        previewBodyScroll.borderType = .noBorder
        previewBodyScroll.documentView = previewBody
        CockpitScrollChrome.apply(to: previewBodyScroll)
        previewPane.addSubview(previewBodyScroll)

        copyPointerButton.target = self
        copyPointerButton.action = #selector(copyPointerPressed)
        openButton.target = self
        openButton.action = #selector(openPressed)
        previewPane.addSubview(copyPointerButton)
        previewPane.addSubview(openButton)
        panel.addSubview(statusLabel)
        updateModeButtons()
        renderSuggestions(["current native cockpit work", "what did we already finish", "model ledger status", "open blockers", "recent memory proposals"])
    }

    override func layout() {
        super.layout()
        backdrop.frame = bounds
        let width = min(bounds.width - 80, CGFloat(1120))
        let height = min(bounds.height - 84, CGFloat(690))
        panel.frame = NSRect(x: (bounds.width - width) / 2, y: max(34, (bounds.height - height) / 2), width: width, height: height)

        titleLabel.frame = NSRect(x: 24, y: 18, width: 180, height: 22)
        subtitleLabel.frame = NSRect(x: 24, y: 40, width: width - 84, height: 18)
        closeButton.frame = NSRect(x: width - 46, y: 18, width: 28, height: 28)
        searchField.frame = NSRect(x: 20, y: 68, width: width - 40, height: 40)
        modeStrip.frame = NSRect(x: 20, y: 118, width: width - 40, height: 42)
        layoutModeButtons()

        suggestionsLabel.frame = NSRect(x: 24, y: 172, width: 140, height: 14)
        suggestionsView.frame = NSRect(x: 20, y: 192, width: 268, height: height - 216)
        layoutSuggestionButtons()

        let resultsX: CGFloat = 310
        let previewW = min(CGFloat(390), max(330, width * 0.36))
        let resultsW = max(300, width - resultsX - previewW - 32)
        explorerHeadlineLabel.frame = NSRect(x: resultsX, y: 172, width: resultsW, height: 20)
        explorerSubheadLabel.frame = NSRect(x: resultsX, y: 194, width: resultsW, height: 18)
        facetStrip.frame = NSRect(x: resultsX, y: 220, width: resultsW, height: 34)
        layoutFacetButtons()
        resultScroll.frame = NSRect(x: resultsX, y: 266, width: resultsW, height: height - 306)
        previewPane.frame = NSRect(x: width - previewW - 20, y: 172, width: previewW, height: height - 212)
        layoutPreview()
        layoutResultButtons(width: resultsW)
    }

    func show(initialQuery: String? = nil, mode: NativeContextPaletteMode = .all) {
        isHidden = false
        alphaValue = 1
        currentMode = mode
        updateModeButtons()
        if let initialQuery, !initialQuery.isEmpty {
            searchField.stringValue = initialQuery
        }
        searchField.currentEditor()?.selectAll(nil)
        window?.makeFirstResponder(searchField)
        statusLabel.stringValue = "Choose a suggestion or type a query."
    }

    func hide() {
        isHidden = true
        onClose?()
    }

    func setLoading(query: String) {
        statusLabel.stringValue = query.isEmpty ? "Loading context..." : "Searching \"\(query)\"..."
    }

    func render(response: NativeContextSearchResponse) {
        currentMode = response.mode
        updateModeButtons()
        let cardCount = response.answerCards.count
        statusLabel.stringValue = response.truncated == true
            ? "\(cardCount) cards shown, more available"
            : "\(cardCount) context cards"
        explorerHeadlineLabel.stringValue = response.explorerSummary.headline.isEmpty ? "\(cardCount) useful context cards" : response.explorerSummary.headline
        explorerSubheadLabel.stringValue = response.explorerSummary.subhead.isEmpty
            ? "Work, facts, and evidence are grouped for browsing; typing only narrows the view."
            : response.explorerSummary.subhead
        renderIntentCards(response.intentCards, fallbackSuggestions: response.suggestions)
        renderFacets(response.facets)
        renderAnswerCards(response.answerCards, groups: response.groups)
        if let first = response.answerCards.first {
            selectAnswerCard(first)
        } else if let first = response.groups.flatMap(\.items).first {
            selectHit(first)
        } else {
            selectedHitID = nil
            selectedAnswerCardID = nil
            previewSourceLabel.stringValue = "NO MATCH"
            previewTitleLabel.stringValue = "No context matched"
            previewPointerLabel.stringValue = ""
            previewBody.string = "Try a broader phrase, or use one of the suggested context paths."
        }
    }

    func renderError(_ message: String) {
        statusLabel.stringValue = message
        previewSourceLabel.stringValue = "ERROR"
        previewTitleLabel.stringValue = "Context search failed"
        previewPointerLabel.stringValue = ""
        previewBody.string = "What happened\n\(message)\n\nTry a broader query, switch back to All Context, or choose one of the suggested paths on the left."
    }

    func renderRead(_ response: NativeContextReadResponse) {
        guard response.pointer == selectedHit?.pointer else { return }
        if !response.detailCard.title.isEmpty {
            previewTitleLabel.stringValue = response.detailCard.title
            previewPointerLabel.stringValue = response.detailCard.sourcePath.isEmpty ? response.pointer : response.detailCard.sourcePath
            previewBody.string = Self.detailText(response.detailCard)
            return
        }
        let body = response.read["body"]?.stringValue ?? response.read["content"]?.stringValue
        if let body, !body.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            previewBody.string = Self.humanReadableBody(body)
            return
        }
        let metadata = response.read["metadata"]?.objectValue
        let docExists = metadata?["doc_exists"]?.boolValue ?? response.read["exists"]?.boolValue
        let reason = metadata?["reason"]?.stringValue
        if docExists == true {
            previewBody.string = "The source document exists, but this exact section anchor is stale or no longer resolves.\n\nUse Open to jump to the document, or search the title from the left side to find the current section."
        } else {
            previewBody.string = "This pointer could not be expanded.\n\nReason: \(reason ?? "No readable body was returned.")\n\nUse Copy Pointer if you want to inspect it manually."
        }
    }

    private var selectedHit: NativeContextHit? {
        guard let selectedHitID else { return nil }
        return currentHits[selectedHitID]
    }

    private func renderIntentCards(_ cards: [NativeContextIntentCard], fallbackSuggestions: [String]) {
        for button in suggestionButtons { button.removeFromSuperview() }
        suggestionButtons.removeAll()
        if cards.isEmpty {
            for suggestion in fallbackSuggestions.prefix(8) {
                let button = ContextIntentCardButton(suggestion: suggestion, target: self, action: #selector(suggestionPressed(_:)))
                suggestionsView.addSubview(button)
                suggestionButtons.append(button)
            }
        } else {
            for card in cards.prefix(8) {
                let button = ContextIntentCardButton(card: card, target: self, action: #selector(intentPressed(_:)))
                suggestionsView.addSubview(button)
                suggestionButtons.append(button)
            }
        }
        needsLayout = true
    }

    private func renderFacets(_ facets: [NativeContextFacet]) {
        for button in facetButtons { button.removeFromSuperview() }
        facetButtons.removeAll()
        for facet in facets.prefix(3) {
            for option in facet.options.prefix(8) {
                let button = ContextFacetButton(facet: facet, option: option, target: self, action: #selector(facetPressed(_:)))
                facetStrip.addSubview(button)
                facetButtons.append(button)
            }
        }
        needsLayout = true
    }

    private func renderAnswerCards(_ cards: [NativeContextAnswerCard], groups: [NativeContextGroup]) {
        for button in resultButtons { button.removeFromSuperview() }
        resultButtons.removeAll()
        currentHits.removeAll()
        currentAnswerCards.removeAll()
        for group in groups {
            for item in group.items {
                currentHits[item.id] = item
            }
        }
        let displayCards = cards.isEmpty ? fallbackCards(from: groups) : cards
        for card in displayCards {
            currentAnswerCards[card.id] = card
            let button = ContextAnswerCardButton(card: card, target: self, action: #selector(answerCardPressed(_:)))
            resultList.addSubview(button)
            resultButtons.append(button)
        }
        resultList.frame = NSRect(x: 0, y: 0, width: resultScroll.bounds.width, height: max(resultScroll.bounds.height, CGFloat(resultButtons.count) * 96 + 8))
        needsLayout = true
    }

    private func fallbackCards(from groups: [NativeContextGroup]) -> [NativeContextAnswerCard] {
        groups.flatMap(\.items).map { hit in
            NativeContextAnswerCard(
                id: hit.id,
                hitId: hit.id,
                title: hit.title,
                summary: Self.cleanedSnippet(hit.snippet ?? hit.pointer ?? ""),
                sourceLabel: hit.sourceLabel,
                group: hit.group,
                displayType: hit.group,
                accent: hit.accent,
                pointer: hit.pointer,
                primaryAction: hit.primaryAction,
                badges: hit.badges,
                whyItMatters: Self.whyFallbackMatters(hit),
                relationshipHints: ["1 source in \(hit.groupLabel)"]
            )
        }
    }

    private func renderSuggestions(_ suggestions: [String]) {
        for button in suggestionButtons { button.removeFromSuperview() }
        suggestionButtons.removeAll()
        for suggestion in suggestions.prefix(8) {
            let button = ContextIntentCardButton(suggestion: suggestion, target: self, action: #selector(suggestionPressed(_:)))
            suggestionsView.addSubview(button)
            suggestionButtons.append(button)
        }
        needsLayout = true
    }

    private func renderGroups(_ groups: [NativeContextGroup]) {
        for button in resultButtons { button.removeFromSuperview() }
        resultButtons.removeAll()
        currentHits.removeAll()
        var y: CGFloat = 0
        for group in groups {
            let header = ContextPaletteHeaderButton(title: "\(group.label.uppercased())  \(group.count)")
            header.isEnabled = false
            resultList.addSubview(header)
            resultButtons.append(header)
            y += 30
            for item in group.items {
                currentHits[item.id] = item
                let button = ContextPaletteResultButton(hit: item, target: self, action: #selector(resultPressed(_:)))
                resultList.addSubview(button)
                resultButtons.append(button)
                y += 78
            }
            y += 10
        }
        resultList.frame = NSRect(x: 0, y: 0, width: resultScroll.bounds.width, height: max(resultScroll.bounds.height, y + 8))
        needsLayout = true
    }

    private func selectHit(_ hit: NativeContextHit) {
        selectedHitID = hit.id
        selectedAnswerCardID = nil
        updateSelectedResultChrome()
        previewSourceLabel.stringValue = "\(hit.sourceLabel.uppercased()) · \(hit.kind.replacingOccurrences(of: "_", with: " ").uppercased())"
        previewTitleLabel.stringValue = hit.title
        previewPointerLabel.stringValue = hit.pointer ?? ""
        previewBody.string = Self.previewSummary(for: hit)
        if hit.primaryAction == .readPointer,
           let pointer = hit.pointer,
           pointer.hasPrefix("memory://") || pointer.hasPrefix("kfts://") {
            onReadRequested?(hit)
        }
    }

    private func selectAnswerCard(_ card: NativeContextAnswerCard) {
        selectedAnswerCardID = card.id
        selectedHitID = card.hitId
        updateSelectedResultChrome()
        previewSourceLabel.stringValue = "\(card.sourceLabel.uppercased()) · \(card.displayType.uppercased())"
        previewTitleLabel.stringValue = card.title
        previewPointerLabel.stringValue = card.pointer ?? ""
        previewBody.string = Self.previewSummary(for: card)
        if let hit = currentHits[card.hitId],
           hit.primaryAction == .readPointer,
           let pointer = hit.pointer,
           pointer.hasPrefix("memory://") || pointer.hasPrefix("kfts://") {
            onReadRequested?(hit)
        }
    }

    private static func previewSummary(for hit: NativeContextHit) -> String {
        let snippet = cleanedSnippet(hit.snippet ?? "")
        let badges = hit.badges.filter { !$0.isEmpty }.joined(separator: "  ·  ")
        let action: String
        switch hit.primaryAction {
        case .readPointer:
            action = "This item can expand into a bounded source excerpt."
        case .revealWork:
            action = "Open reveals the matching work row in the cockpit."
        case .openFile:
            action = "Open jumps directly to the source file."
        case .openPointer, .inspect:
            action = "Copy the pointer or open the linked source."
        }
        return [
            "Why this is here",
            snippet.isEmpty ? "Matched this query through the context index." : snippet,
            "",
            badges.isEmpty ? nil : "Signals\n\(badges)",
            "",
            "What you can do\n\(action)",
        ].compactMap { $0 }.joined(separator: "\n")
    }

    private static func previewSummary(for card: NativeContextAnswerCard) -> String {
        let badges = card.badges.filter { !$0.isEmpty }.joined(separator: "  ·  ")
        let related = card.relationshipHints.filter { !$0.isEmpty }.joined(separator: "  ·  ")
        return [
            card.summary,
            "",
            "Why this matters",
            card.whyItMatters,
            "",
            badges.isEmpty ? nil : "Signals\n\(badges)",
            related.isEmpty ? nil : "Related\n\(related)",
        ].compactMap { $0 }.joined(separator: "\n")
    }

    private static func detailText(_ card: NativeContextDetailCard) -> String {
        var chunks: [String] = []
        if !card.summary.isEmpty {
            chunks.append(card.summary)
        }
        for section in card.sections where !section.items.isEmpty {
            chunks.append(section.title + "\n" + section.items.map { "• \($0)" }.joined(separator: "\n"))
        }
        if !card.timeline.isEmpty {
            chunks.append("Timeline\n" + card.timeline.map { "\($0.label): \($0.value)" }.joined(separator: "\n"))
        }
        if !card.related.isEmpty {
            chunks.append("Related\n" + card.related.map { item in
                let summary = item.summary.isEmpty ? item.pointer : item.summary
                return "• \(item.title) · \(item.relation)\n  \(summary)"
            }.joined(separator: "\n"))
        }
        return chunks.joined(separator: "\n\n")
    }

    private static func humanReadableBody(_ raw: String) -> String {
        raw
            .replacingOccurrences(of: #"(?m)^#{1,6}\s*"#, with: "", options: .regularExpression)
            .replacingOccurrences(of: #"(?m)^\s*[-*]\s+"#, with: "• ", options: .regularExpression)
            .replacingOccurrences(of: "`", with: "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func cleanedSnippet(_ raw: String) -> String {
        raw
            .replacingOccurrences(of: "matched terms:", with: "Matched")
            .replacingOccurrences(of: "; title/display match", with: " · title match")
            .replacingOccurrences(of: "; artifact/context path match", with: " · context path")
            .replacingOccurrences(of: "; artifact/context path mat...", with: " · context path")
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func whyFallbackMatters(_ hit: NativeContextHit) -> String {
        switch hit.group {
        case "work": return "This is coordination work you may need to inspect or resume."
        case "facts": return "This is accepted evidence behind an operational claim."
        case "knowledge": return "This is source-backed context for the selected topic."
        case "memory": return "This is accepted or proposed memory that can shape future work."
        case "done": return "This is completed work that can prevent duplicate effort."
        default: return "This is related context from the local knowledge surfaces."
        }
    }

    private func updateSelectedResultChrome() {
        for button in resultButtons {
            if let result = button as? ContextPaletteResultButton {
                result.isSelected = result.hit.id == selectedHitID
            } else if let result = button as? ContextAnswerCardButton {
                result.isSelected = result.card.id == selectedAnswerCardID
            }
        }
    }

    private func layoutModeButtons() {
        let gap: CGFloat = 8
        var x: CGFloat = 0
        for mode in NativeContextPaletteMode.allCases {
            guard let button = modeButtons[mode] else { continue }
            let width = min(max(button.intrinsicContentSize.width + 18, 82), 132)
            button.frame = NSRect(x: x, y: 2, width: width, height: 34)
            x += width + gap
        }
    }

    private func layoutSuggestionButtons() {
        var y: CGFloat = 0
        for button in suggestionButtons {
            button.frame = NSRect(x: 0, y: y, width: suggestionsView.bounds.width, height: 70)
            y += 80
        }
    }

    private func layoutFacetButtons() {
        var x: CGFloat = 0
        for button in facetButtons {
            let width = min(max(button.intrinsicContentSize.width + 24, 74), 142)
            button.frame = NSRect(x: x, y: 1, width: width, height: 28)
            x += width + 8
        }
    }

    private func layoutResultButtons(width: CGFloat) {
        var y: CGFloat = 0
        for button in resultButtons {
            if button is ContextPaletteHeaderButton {
                button.frame = NSRect(x: 0, y: y, width: width - 8, height: 24)
                y += 30
            } else if button is ContextAnswerCardButton {
                button.frame = NSRect(x: 0, y: y, width: width - 8, height: 88)
                y += 98
            } else {
                button.frame = NSRect(x: 0, y: y, width: width - 8, height: 70)
                y += 78
            }
        }
        resultList.frame = NSRect(x: 0, y: 0, width: width, height: max(resultScroll.bounds.height, y + 8))
    }

    private func layoutPreview() {
        let w = previewPane.bounds.width
        let h = previewPane.bounds.height
        previewSourceLabel.frame = NSRect(x: 18, y: 16, width: w - 36, height: 14)
        previewTitleLabel.frame = NSRect(x: 18, y: 38, width: w - 36, height: 44)
        previewPointerLabel.frame = NSRect(x: 18, y: 86, width: w - 36, height: 34)
        previewBodyScroll.frame = NSRect(x: 14, y: 132, width: w - 28, height: max(140, h - 190))
        copyPointerButton.frame = NSRect(x: 16, y: h - 44, width: 132, height: 28)
        openButton.frame = NSRect(x: 156, y: h - 44, width: 90, height: 28)
    }

    private func updateModeButtons() {
        for (mode, button) in modeButtons {
            let active = mode == currentMode
            button.contentTintColor = active ? CockpitTokens.Color.text : CockpitTokens.Color.muted
            button.layer?.backgroundColor = active
                ? CockpitTokens.Color.blue.withAlphaComponent(0.13).cgColor
                : CockpitTokens.Color.panel2.withAlphaComponent(0.26).cgColor
            button.layer?.borderColor = (active ? CockpitTokens.Color.blue : CockpitTokens.Color.line)
                .withAlphaComponent(active ? 0.34 : 0.09).cgColor
            button.layer?.shadowColor = CockpitTokens.Color.blue.cgColor
            button.layer?.shadowOpacity = active ? 0.24 : 0
            button.layer?.shadowRadius = active ? 12 : 0
        }
    }

    @objc private func modePressed(_ sender: NSButton) {
        guard let id = sender.identifier?.rawValue,
              let mode = NativeContextPaletteMode(rawValue: id) else { return }
        currentMode = mode
        updateModeButtons()
        onSearchRequested?(searchField.stringValue, currentMode)
    }

    @objc private func suggestionPressed(_ sender: NSButton) {
        let suggestion = sender.identifier?.rawValue ?? sender.title
        searchField.stringValue = suggestion
        onSearchRequested?(suggestion, currentMode)
    }

    @objc private func intentPressed(_ sender: NSButton) {
        guard let id = sender.identifier?.rawValue,
              let button = sender as? ContextIntentCardButton,
              let card = button.card else { return }
        _ = id
        currentMode = card.mode
        searchField.stringValue = card.query
        updateModeButtons()
        onSearchRequested?(card.query, card.mode)
    }

    @objc private func facetPressed(_ sender: NSButton) {
        guard let id = sender.identifier?.rawValue,
              let mode = NativeContextPaletteMode(rawValue: id) else { return }
        currentMode = mode
        updateModeButtons()
        onSearchRequested?(searchField.stringValue, currentMode)
    }

    @objc private func resultPressed(_ sender: NSButton) {
        guard let id = sender.identifier?.rawValue, let hit = currentHits[id] else { return }
        selectHit(hit)
    }

    @objc private func answerCardPressed(_ sender: NSButton) {
        guard let id = sender.identifier?.rawValue, let card = currentAnswerCards[id] else { return }
        selectAnswerCard(card)
    }

    @objc private func copyPointerPressed() {
        guard let pointer = selectedHit?.pointer, !pointer.isEmpty else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(pointer, forType: .string)
        statusLabel.stringValue = "Copied pointer"
    }

    @objc private func openPressed() {
        guard let selectedHit else { return }
        onOpenRequested?(selectedHit)
    }

    @objc private func closePressed() {
        hide()
    }

    func controlTextDidChange(_ obj: Notification) {
        onSearchRequested?(searchField.stringValue, currentMode)
    }
}

private final class ContextPaletteHeaderButton: NSButton {
    init(title: String) {
        super.init(frame: .zero)
        self.title = title
        isBordered = false
        alignment = .left
        font = .systemFont(ofSize: 10, weight: .bold)
        contentTintColor = CockpitTokens.Color.faint
    }

    required init?(coder: NSCoder) { nil }
}

private final class ContextIntentCardButton: NSButton {
    let card: NativeContextIntentCard?
    private let suggestion: String?

    init(card: NativeContextIntentCard, target: AnyObject?, action: Selector?) {
        self.card = card
        self.suggestion = nil
        super.init(frame: .zero)
        identifier = NSUserInterfaceItemIdentifier(card.id)
        self.target = target
        self.action = action
        title = ""
        isBordered = false
        wantsLayer = true
        toolTip = card.subtitle
    }

    init(suggestion: String, target: AnyObject?, action: Selector?) {
        self.card = nil
        self.suggestion = suggestion
        super.init(frame: .zero)
        identifier = NSUserInterfaceItemIdentifier(suggestion)
        self.target = target
        self.action = action
        title = ""
        isBordered = false
        wantsLayer = true
        toolTip = suggestion
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        let rect = bounds.insetBy(dx: 1, dy: 1)
        let path = NSBezierPath(roundedRect: rect, xRadius: 12, yRadius: 12)
        let accent = contextColor(for: card?.accent ?? "blue")
        CockpitTokens.Color.panel2.withAlphaComponent(0.42).setFill()
        path.fill()
        accent.withAlphaComponent(0.20).setStroke()
        path.lineWidth = 1
        path.stroke()
        accent.withAlphaComponent(0.65).setFill()
        NSBezierPath(roundedRect: NSRect(x: 1, y: 10, width: 3, height: bounds.height - 20), xRadius: 1.5, yRadius: 1.5).fill()

        let title = card?.title ?? suggestion ?? ""
        let subtitle = card?.subtitle ?? "Search this context path"
        let count = card.map { "\($0.count)" } ?? ""
        draw(text: title, rect: NSRect(x: 14, y: 12, width: bounds.width - 46, height: 18), size: 12.8, weight: .bold, color: CockpitTokens.Color.text)
        draw(text: subtitle, rect: NSRect(x: 14, y: 34, width: bounds.width - 28, height: 28), size: 10.8, weight: .medium, color: CockpitTokens.Color.muted)
        if !count.isEmpty {
            draw(text: count, rect: NSRect(x: bounds.width - 34, y: 12, width: 20, height: 18), size: 11, weight: .bold, color: accent)
        }
    }

    private func draw(text: String, rect: NSRect, size: CGFloat, weight: NSFont.Weight, color: NSColor) {
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineBreakMode = .byTruncatingTail
        paragraph.maximumLineHeight = size + 3
        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: size, weight: weight),
            .foregroundColor: color,
            .paragraphStyle: paragraph,
        ]
        NSString(string: text).draw(in: rect, withAttributes: attributes)
    }
}

private final class ContextFacetButton: CockpitButton {
    init(facet: NativeContextFacet, option: NativeContextFacetOption, target: AnyObject?, action: Selector?) {
        super.init(frame: .zero)
        title = "\(option.label)  \(option.count)"
        self.target = target
        self.action = action
        identifier = NSUserInterfaceItemIdentifier(option.id)
        font = .systemFont(ofSize: 11.5, weight: .bold)
        contentTintColor = option.active ? CockpitTokens.Color.text : CockpitTokens.Color.muted
        layer?.backgroundColor = option.active
            ? CockpitTokens.Color.glowBlue.withAlphaComponent(0.16).cgColor
            : CockpitTokens.Color.panel2.withAlphaComponent(0.42).cgColor
        layer?.borderColor = (option.active ? CockpitTokens.Color.glowBlue : CockpitTokens.Color.line)
            .withAlphaComponent(option.active ? 0.38 : 0.13).cgColor
        toolTip = "\(facet.label): \(option.label)"
    }

    required init?(coder: NSCoder) { nil }
}

private final class ContextAnswerCardButton: NSButton {
    let card: NativeContextAnswerCard
    var isSelected = false { didSet { needsDisplay = true } }

    init(card: NativeContextAnswerCard, target: AnyObject?, action: Selector?) {
        self.card = card
        super.init(frame: .zero)
        identifier = NSUserInterfaceItemIdentifier(card.id)
        self.target = target
        self.action = action
        title = ""
        isBordered = false
        wantsLayer = true
        toolTip = card.pointer
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        let rect = bounds.insetBy(dx: 1, dy: 1)
        let path = NSBezierPath(roundedRect: rect, xRadius: 12, yRadius: 12)
        let accent = contextColor(for: card.accent)
        (isSelected ? accent.withAlphaComponent(0.16) : CockpitTokens.Color.panel2.withAlphaComponent(0.30)).setFill()
        path.fill()
        (isSelected ? accent.withAlphaComponent(0.52) : CockpitTokens.Color.line.withAlphaComponent(0.12)).setStroke()
        path.lineWidth = 1
        path.stroke()
        if isSelected {
            accent.withAlphaComponent(0.94).setFill()
            NSBezierPath(roundedRect: NSRect(x: 1, y: 10, width: 3, height: bounds.height - 20), xRadius: 1.5, yRadius: 1.5).fill()
        }

        draw(text: card.sourceLabel.uppercased(), rect: NSRect(x: 16, y: 10, width: 150, height: 13), size: 9.5, weight: .bold, color: accent.withAlphaComponent(0.92))
        let badge = card.badges.first ?? card.displayType
        draw(text: badge.uppercased(), rect: NSRect(x: bounds.width - 112, y: 10, width: 96, height: 13), size: 9.5, weight: .bold, color: CockpitTokens.Color.faint, align: .right)
        draw(text: card.title, rect: NSRect(x: 16, y: 28, width: bounds.width - 32, height: 19), size: 13.6, weight: .bold, color: CockpitTokens.Color.text)
        draw(text: card.summary, rect: NSRect(x: 16, y: 51, width: bounds.width - 32, height: 17), size: 11.4, weight: .medium, color: CockpitTokens.Color.muted)
        draw(text: card.whyItMatters, rect: NSRect(x: 16, y: 69, width: bounds.width - 32, height: 15), size: 10.5, weight: .medium, color: CockpitTokens.Color.faint)
    }

    private func draw(text: String, rect: NSRect, size: CGFloat, weight: NSFont.Weight, color: NSColor, align: NSTextAlignment = .left) {
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineBreakMode = .byTruncatingTail
        paragraph.alignment = align
        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: size, weight: weight),
            .foregroundColor: color,
            .paragraphStyle: paragraph,
        ]
        NSString(string: text).draw(in: rect, withAttributes: attributes)
    }
}

private final class ContextPaletteResultButton: NSButton {
    let hit: NativeContextHit
    var isSelected = false { didSet { needsDisplay = true } }

    init(hit: NativeContextHit, target: AnyObject?, action: Selector?) {
        self.hit = hit
        super.init(frame: .zero)
        identifier = NSUserInterfaceItemIdentifier(hit.id)
        self.target = target
        self.action = action
        isBordered = false
        title = ""
        wantsLayer = true
        toolTip = hit.pointer
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        let rect = bounds.insetBy(dx: 1, dy: 1)
        let path = NSBezierPath(roundedRect: rect, xRadius: 11, yRadius: 11)
        let accent = color(for: hit.accent)
        (isSelected ? accent.withAlphaComponent(0.18) : CockpitTokens.Color.panel2.withAlphaComponent(0.28)).setFill()
        path.fill()
        (isSelected ? accent.withAlphaComponent(0.45) : CockpitTokens.Color.line.withAlphaComponent(0.10)).setStroke()
        path.lineWidth = 1
        path.stroke()
        if isSelected {
            accent.withAlphaComponent(0.90).setFill()
            NSBezierPath(roundedRect: NSRect(x: 1, y: 9, width: 3, height: bounds.height - 18), xRadius: 1.5, yRadius: 1.5).fill()
        }

        draw(text: hit.sourceLabel.uppercased(), rect: NSRect(x: 14, y: 8, width: bounds.width - 28, height: 12), size: 9.5, weight: .bold, color: CockpitTokens.Color.faint)
        draw(text: hit.title, rect: NSRect(x: 14, y: 24, width: bounds.width - 28, height: 18), size: 13.2, weight: .bold, color: CockpitTokens.Color.text)
        draw(text: clean(hit.snippet ?? hit.pointer ?? ""), rect: NSRect(x: 14, y: 45, width: bounds.width - 28, height: 16), size: 11.2, weight: .medium, color: CockpitTokens.Color.muted)
    }

    private func draw(text: String, rect: NSRect, size: CGFloat, weight: NSFont.Weight, color: NSColor) {
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineBreakMode = .byTruncatingTail
        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: size, weight: weight),
            .foregroundColor: color,
            .paragraphStyle: paragraph,
        ]
        NSString(string: text).draw(in: rect, withAttributes: attributes)
    }

    private func color(for accent: String) -> NSColor {
        switch accent {
        case "green": return CockpitTokens.Color.green
        case "amber": return CockpitTokens.Color.amber
        case "purple": return NSColor(calibratedRed: 0.58, green: 0.46, blue: 1.00, alpha: 1)
        case "slate", "gray": return CockpitTokens.Color.muted
        default: return CockpitTokens.Color.blue
        }
    }

    private func clean(_ raw: String) -> String {
        raw
            .replacingOccurrences(of: "matched terms:", with: "Matched")
            .replacingOccurrences(of: "; title/display match", with: " · title")
            .replacingOccurrences(of: "; artifact/context path match", with: " · path")
    }
}

private func contextColor(for accent: String) -> NSColor {
    switch accent {
    case "green": return CockpitTokens.Color.green
    case "amber": return CockpitTokens.Color.amber
    case "purple": return CockpitTokens.Color.violet
    case "red": return CockpitTokens.Color.red
    case "slate", "gray": return CockpitTokens.Color.muted
    default: return CockpitTokens.Color.glowBlue
    }
}
