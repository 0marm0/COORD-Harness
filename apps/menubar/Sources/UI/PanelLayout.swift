import AppKit


enum PanelLayout {

    static func totalHeight(state: MenubarState, config: Config) -> CGFloat {
        typealias L = Tokens.Layout
        var h: CGFloat = 0


        h += 44


        if state.healthSummary != nil { h += 18 }


        let running = state.workModel?.runningRows ?? []
        for r in running { h += (r.showsBar ? L.runningRowH : L.agentRowH) }
        let agents = state.normalizedAgentMilestones
        h += CGFloat(agents.count) * L.agentRowH


        let next = state.workModel?.nextRows ?? []
        let nextTotal = max(next.count, state.workModel?.summary?.next ?? 0)
        h += 26
        let shown = min(next.count, config.nextVisible)
        h += CGFloat(shown) * L.nextRowH
        if nextTotal > shown { h += L.nextRowH }


        let attn = state.workModel?.attentionRows ?? []
        if !attn.isEmpty {
            h += 24
            if !config.attentionCollapsed { h += CGFloat(attn.count) * L.attnRowH }
        }


        h += 32

        return min(h, L.maxPopoverHeight)
    }
}
