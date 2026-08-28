import AppKit


enum Art {
    static func image(_ name: String) -> NSImage? {
        if let i = NSImage(named: name) { return i }
        if let url = Bundle.main.url(forResource: name, withExtension: "png", subdirectory: "Assets") {
            return NSImage(contentsOf: url)
        }
        return nil
    }


    static let wordmark = image("wordmark")
    static let claude   = AgentMarks.owner(.claude, size: 18)
    static let codex    = AgentMarks.owner(.codex, size: 18)
    static let appleGPU = AgentMarks.owner(.local, size: 18, accelerated: true)
    static let appleOutline = AgentMarks.owner(.local, size: 18, accelerated: false)
}


enum UI {
    static func label(_ text: String, size: CGFloat, weight: NSFont.Weight = .regular,
                      color: NSColor, align: NSTextAlignment = .left) -> NSTextField {
        let f = NSTextField(labelWithString: text)
        f.font = .systemFont(ofSize: size, weight: weight)
        f.textColor = color; f.alignment = align
        f.lineBreakMode = .byTruncatingTail; f.cell?.usesSingleLineMode = true
        f.isBordered = false; f.drawsBackground = false; f.isEditable = false
        return f
    }

    static func ownerIcon(_ kind: OwnerKind, size: CGFloat) -> NSView {
        switch kind {
        case .claude, .codex, .mixed:


            return imageView(AgentMarks.owner(kind, size: size), size: size)
        case .operatorUser: return ringDot(Tokens.ownerTint(.operatorUser), size: size)
        case .local:  return ringDot(Tokens.ownerTint(.local), size: size)
        }
    }
    static func imageView(_ img: NSImage?, size: CGFloat, tint: NSColor? = nil) -> NSImageView {
        let v = NSImageView(frame: NSRect(x: 0, y: 0, width: size, height: size))
        v.image = img; v.imageScaling = .scaleProportionallyDown
        if let t = tint { img?.isTemplate = true; v.contentTintColor = t }
        return v
    }

    static func appleMark(size: CGFloat, filled: Bool, tint: NSColor = Tokens.Color.gpuApple) -> NSImageView {


        imageView(AgentMarks.owner(.local, size: size, accelerated: filled), size: size)
    }

    static func ringDot(_ color: NSColor, size: CGFloat) -> NSView {
        let s = min(size, 9.0)
        let v = NSView(frame: NSRect(x: (size-s)/2, y: (size-s)/2, width: s, height: s))
        v.wantsLayer = true
        v.layer?.borderColor = color.cgColor; v.layer?.borderWidth = 1.2; v.layer?.cornerRadius = s/2
        return v
    }
    static func filledDot(_ color: NSColor, size: CGFloat) -> NSView {
        let s = min(size, 10.0)
        let v = NSView(frame: NSRect(x: (size-s)/2, y: (size-s)/2, width: s, height: s))
        v.wantsLayer = true; v.layer?.backgroundColor = color.cgColor; v.layer?.cornerRadius = s/2
        return v
    }
}


class RowView: NSView {
    override var isFlipped: Bool { true }
    private var expandKey: String?


    func makeExpandable(_ key: String?) {
        guard let key, expandKey == nil else { return }
        expandKey = key
        identifier = NSUserInterfaceItemIdentifier("coord-row:" + key)
        addGestureRecognizer(NSClickGestureRecognizer(target: self, action: #selector(_rowTapped)))
    }
    @objc private func _rowTapped() { if let k = expandKey { RowActions.expand?(k) } }
}


final class ProgressBarView: NSView {
    private var pct: Double?; private var tint = Tokens.Color.progressBlue; private var indeterminate = false
    func configure(pct: Double?, owner: OwnerKind, indeterminate: Bool) {
        self.pct = pct; self.indeterminate = indeterminate
        self.tint = Tokens.Color.progressBlue
        needsDisplay = true
    }
    override func draw(_ r: NSRect) {
        let h: CGFloat = 4, y = (bounds.height - h)/2
        let track = NSBezierPath(roundedRect: NSRect(x: 0, y: y, width: bounds.width, height: h), xRadius: 2, yRadius: 2)
        Tokens.Color.gray(1, 0.12).set(); track.fill()
        let frac: CGFloat = (indeterminate || pct == nil) ? 0.30 : CGFloat(max(0, min(100, pct!)))/100
        let alpha: CGFloat = (indeterminate || pct == nil) ? 0.45 : 0.96
        let fill = NSBezierPath(roundedRect: NSRect(x: 0, y: y, width: bounds.width*frac, height: h), xRadius: 2, yRadius: 2)
        tint.withAlphaComponent(alpha).set(); fill.fill()

    }
}


final class RunningLocalRow: RowView {

    private let pct: Double?
    private let indeterminate: Bool
    private let etaStr: String
    private let etaDim: Bool
    private let rateStr: String
    private let countsStr: String

    init(_ row: Row, showIcon: Bool = true) {
        let W = Tokens.Layout.popoverWidth
        typealias L = Tokens.Layout
        self.pct = row.showsIndeterminateBar ? nil : row.effectivePct
        self.indeterminate = row.showsIndeterminateBar
        let live = row.etaLive
        self.etaStr = live.isEmpty ? "" : (row.etaDerived == true ? "~" + live : live)
        self.etaDim = row.etaDerived == true
        self.rateStr = (row.rate.map { $0 > 0 ? String(format: "%.1f/s", $0) : "" }) ?? ""
        self.countsStr = (row.done != nil && row.total != nil && (row.done ?? 0) > 0 && (row.total ?? 0) > 0)
            ? "\(row.done!.formatted()) / \(row.total!.formatted())" : ""
        super.init(frame: NSRect(x: 0, y: 0, width: W, height: Tokens.Layout.runningRowH))
        toolTip = row.name ?? row.title
        wantsLayer = true


        if showIcon {
            let apple = UI.appleMark(size: L.leadIconW, filled: row.isGPU)
            apple.frame = NSRect(x: L.rowPadL, y: 8, width: L.leadIconW, height: L.leadIconW); addSubview(apple)


            let driver = AgentMarks.lane(from: row.ownerSessionLabel) ?? {
                let kind = row.iconOwnerKind
                return (kind == .claude || kind == .codex) ? kind : nil
            }()
            if let driver {
                let badge = UI.ownerIcon(driver, size: 9)
                badge.frame = NSRect(x: L.rowPadL - 8.5, y: 9.5, width: 9, height: 9); addSubview(badge)
            }
        }
        let title = UI.label(row.title, size: 12, weight: .bold, color: Tokens.Color.white.withAlphaComponent(0.92))
        title.frame = NSRect(x: L.titleX, y: 8, width: W - L.titleX - 38, height: 18); addSubview(title)


        let pause = NSButton(frame: NSRect(x: W - Tokens.Layout.rowPadR - 24, y: 22, width: 24, height: 22))
        pause.title = (row.paused == true) ? "▶" : "⏸"; pause.isBordered = false; pause.font = .systemFont(ofSize: 13)
        pause.contentTintColor = Tokens.Color.dimGray
        let jid = row.jobId ?? row.id; let resume = (row.paused == true)
        onPause = { if let jid = jid { RowActions.shared?( .pauseResume(jid, resume) ) } }
        pause.target = self; pause.action = #selector(doPause); addSubview(pause)
    }


    override func draw(_ dirty: NSRect) {
        typealias L = Tokens.Layout
        let by: CGFloat = 32, bh: CGFloat = 4
        let baseBx = L.titleX + 14
        let pctSlot: CGFloat = 28, etaSlot: CGFloat = 42, etaGap: CGFloat = 5
        let rightEdge = bounds.width - 66

        let pctFont = NSFont.boldSystemFont(ofSize: 9.5)
        var pctStr = ""
        if let p = pct, !indeterminate { pctStr = "\(Int(p.rounded()))%" }
        let pctAttr: [NSAttributedString.Key: Any] = [.font: pctFont, .foregroundColor: Tokens.Color.progressBlueLt.withAlphaComponent(0.96)]
        let etaAttr: [NSAttributedString.Key: Any] = [.font: pctFont, .foregroundColor: (etaDim ? Tokens.Color.dimGray : Tokens.Color.white.withAlphaComponent(0.90))]


        if !pctStr.isEmpty {
            let s = NSAttributedString(string: pctStr, attributes: pctAttr)
            s.draw(at: NSPoint(x: baseBx, y: by + bh/2 - s.size().height/2))
        }
        let bx = baseBx + pctSlot
        let barRight = rightEdge - etaSlot
        let bw = max(24, barRight - bx)


        let track = NSBezierPath(roundedRect: NSRect(x: bx, y: by, width: bw, height: bh), xRadius: bh/2, yRadius: bh/2)
        Tokens.Color.gray(0.5, 0.15).set(); track.fill()

        let frac: CGFloat = indeterminate ? 0.30 : CGFloat(max(0, min(100, pct ?? 0)))/100
        if frac > 0.002 {
            let fw = max(bh, bw * frac)
            let fill = NSBezierPath(roundedRect: NSRect(x: bx, y: by, width: fw, height: bh), xRadius: bh/2, yRadius: bh/2)


            NSGraphicsContext.saveGraphicsState()
            let glow = NSShadow(); glow.shadowOffset = .zero; glow.shadowBlurRadius = 6.5
            glow.shadowColor = Tokens.Color.glowBlue.withAlphaComponent(0.90); glow.set()
            Tokens.Color.progressBlue.withAlphaComponent(indeterminate ? 0.60 : 1).set(); fill.fill()
            NSGraphicsContext.restoreGraphicsState()
            if !indeterminate {
                let sheen = NSBezierPath(roundedRect: NSRect(x: bx, y: by, width: fw, height: bh * 0.5), xRadius: bh/2, yRadius: bh/2)
                Tokens.Color.progressBlueLt.withAlphaComponent(0.55).set(); sheen.fill()
            }
        }

        if !etaStr.isEmpty {
            let s = NSAttributedString(string: etaStr, attributes: etaAttr)
            s.draw(at: NSPoint(x: bx + bw + etaGap, y: by + bh/2 - s.size().height/2))
        }

        let subY = by + bh + 5, subInset: CGFloat = 6
        let subAttr: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 8.5), .foregroundColor: Tokens.Color.gray(0.62, 0.85)]
        let gap: CGFloat = 16
        var rateRun: NSAttributedString?
        var countRun: NSAttributedString?
        var rateX: CGFloat = bx + subInset
        var countX: CGFloat = bx + bw - subInset
        if !rateStr.isEmpty {
            let s = NSAttributedString(string: rateStr, attributes: subAttr)
            rateRun = s
            rateX = bx + bw * 0.36 - s.size().width / 2
        }
        if !countsStr.isEmpty {
            let s = NSAttributedString(string: countsStr, attributes: subAttr)
            countRun = s
            countX = bx + bw * 0.70 - s.size().width / 2
        }
        if let r = rateRun, let c = countRun {
            let rightLimit = bx + bw - subInset - c.size().width
            countX = min(max(countX, bx + subInset), rightLimit)
            if rateX + r.size().width + gap > countX {
                rateX = max(bx + subInset, countX - gap - r.size().width)
            }
        }
        if let r = rateRun {
            r.draw(at: NSPoint(x: max(bx + subInset, rateX), y: subY))
        }
        if let c = countRun {
            let rightLimit = bx + bw - subInset - c.size().width
            c.draw(at: NSPoint(x: min(max(bx + subInset, countX), rightLimit), y: subY))
        }
    }

    private var onPause: (() -> Void)?
    @objc private func doPause() { onPause?() }
    required init?(coder: NSCoder) { nil }
}


final class RunningAgentRow: RowView {
    init(_ row: Row, showIcon: Bool = true) {
        super.init(frame: NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: Tokens.Layout.agentRowH))
        toolTip = (row.name ?? row.title) + ((row.currentStep ?? row.step).map { "\n\n" + $0 } ?? "")
        typealias L = Tokens.Layout
        if showIcon {
            let icon = UI.ownerIcon(row.iconOwnerKind, size: L.leadIconW)
            icon.frame = NSRect(x: L.rowPadL, y: 4, width: L.leadIconW, height: L.leadIconW); addSubview(icon)
        }


        let title = UI.label(row.title, size: 11.5, color: Tokens.Color.gray(0.74, 0.88))
        title.frame = NSRect(x: L.titleX, y: 4, width: bounds.width - L.titleX - L.rowPadR, height: 16); addSubview(title)

    }
    required init?(coder: NSCoder) { nil }
}


final class NextUpRow: RowView {
    init(_ row: Row, showIcon: Bool = true) {
        super.init(frame: NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: Tokens.Layout.nextRowH))
        toolTip = row.nextRankReason ?? row.name ?? row.title
        typealias L = Tokens.Layout
        let localIconY: CGFloat = 3.5
        if showIcon {
            let icon: NSView = row.isGPU ? UI.appleMark(size: 12, filled: false) : UI.ownerIcon(row.iconOwnerKind, size: L.leadIconW)
            icon.frame = NSRect(x: L.rowPadL, y: row.isGPU ? localIconY : 8, width: L.leadIconW, height: L.leadIconW); addSubview(icon)
        }
        let eta = UI.label(row.etaDisplay, size: 10, color: Tokens.Color.white.withAlphaComponent(0.88), align: .right)
        eta.frame = NSRect(x: bounds.width - L.rowPadR - 44, y: 7, width: 44, height: 14)
        if row.etaDerived == true { eta.textColor = Tokens.Color.dimGray }
        addSubview(eta)

        let title = UI.label(row.title, size: 11.5, color: Tokens.Color.gray(0.74, 0.88))
        title.frame = NSRect(x: L.titleX, y: 5, width: bounds.width - L.titleX - L.rowPadR - 50, height: 16); addSubview(title)
    }
    required init?(coder: NSCoder) { nil }
}


final class AttentionRow: RowView {

    init(_ row: Row, suppressDot: Bool = false, inset: CGFloat = 0) {
        super.init(frame: NSRect(x: inset, y: 0, width: Tokens.Layout.popoverWidth - inset, height: Tokens.Layout.attnRowH))
        toolTip = row.name ?? row.title
        typealias L = Tokens.Layout


        if !suppressDot {
            let dot = UI.ringDot(Tokens.Color.gray(0.62), size: 9)
            dot.setFrameOrigin(NSPoint(x: L.rowPadL, y: 9)); addSubview(dot)
        }
        if row.isGPU {
            let g = UI.appleMark(size: 11, filled: true); g.frame = NSRect(x: bounds.width - L.rowPadR - 11, y: 8, width: 11, height: 11); addSubview(g)
        }
        let title = UI.label(row.title, size: 11, color: Tokens.Color.gray(0.75))
        title.frame = NSRect(x: L.titleX, y: 5, width: bounds.width - L.titleX - L.rowPadR - 16, height: 16); addSubview(title)
    }
    required init?(coder: NSCoder) { nil }
}


final class SectionHeader: RowView {
    var onToggle: (() -> Void)?
    init(label: String, count: Int?, collapsed: Bool, ownerIcon kind: OwnerKind? = nil,
         appleIcon: Bool = false, appleIconPair: Bool = false, iconOnly: Bool = false,
         font: CGFloat = 10.5, countText: String? = nil) {
        super.init(frame: NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: 23))
        typealias L = Tokens.Layout
        var x = L.rowPadL - 8
        if appleIconPair || appleIcon {
            let a = UI.appleMark(size: 16, filled: false); a.frame = NSRect(x: L.rowPadL, y: 4, width: 16, height: 16); addSubview(a); x = L.titleX
        } else if let k = kind {
            let ic = UI.ownerIcon(k, size: 16); ic.frame = NSRect(x: L.rowPadL, y: 5, width: 16, height: 16); addSubview(ic); x = L.titleX
        }
        var labW: CGFloat = 0
        if !iconOnly {
            let lab = UI.label(label.uppercased(), size: font, weight: .semibold, color: Tokens.Color.sectionGray)
            lab.frame = NSRect(x: x, y: 7, width: 180, height: 14); addSubview(lab)
            labW = min(lab.attributedStringValue.size().width, 180)
        }
        if let ct = countText ?? count.map({ "\($0)" }) {


            let cnt = UI.label(ct, size: max(8.5, font - 2.5), weight: .light, color: Tokens.Color.gray(0.42, 0.95))

            let countX = iconOnly ? x : x + labW + L.hdrNumGap
            cnt.frame = NSRect(x: countX, y: 8.25, width: 80, height: 13); addSubview(cnt)
        }

        let btn = NSButton(frame: bounds); btn.isBordered = false; btn.title = ""; btn.target = self
        btn.action = #selector(tap); btn.autoresizingMask = [.width]; addSubview(btn)
    }
    @objc private func tap() { onToggle?() }
    required init?(coder: NSCoder) { nil }
}


final class LaneHeader: RowView {
    init(_ label: String, count: Int) {
        super.init(frame: NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: 18))
        let l = UI.label("\(label.uppercased())  \(count)", size: 8.8, weight: .semibold, color: Tokens.Color.sectionGray.withAlphaComponent(0.88))
        l.frame = NSRect(x: Tokens.Layout.titleX, y: 3, width: 200, height: 12); addSubview(l)
    }
    required init?(coder: NSCoder) { nil }
}


final class InitiativeRow: RowView {
    init(label: String, counts: HierarchyCounts?, height: CGFloat = Tokens.Layout.nextRowH) {
        super.init(frame: NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: height))
        let l = UI.label(label, size: 10.5, color: Tokens.Color.gray(0.82, 0.95))
        l.frame = NSRect(x: Tokens.Layout.titleX, y: (height - 14) / 2, width: 210, height: 14); addSubview(l)
        let total = counts?.total ?? 0
        var bits: [String] = []
        if let running = counts?.running, running > 0 { bits.append("\(running) running") }
        if let next = counts?.next, next > 0 { bits.append("\(next) next") }
        if let attention = counts?.attention, attention > 0 { bits.append("\(attention) attn") }
        let countText = bits.isEmpty ? "\(total)" : "\(total) · " + bits.joined(separator: " · ")
        let c = UI.label(countText, size: 9.5, color: Tokens.Color.dimGray.withAlphaComponent(0.85))
        c.frame = NSRect(x: Tokens.Layout.popoverWidth - 190, y: (height - 13) / 2, width: 176, height: 13)
        c.alignment = .right; addSubview(c)
    }
    required init?(coder: NSCoder) { nil }
}

final class PlaceholderRow: RowView {
    init(_ text: String, height: CGFloat = Tokens.Layout.nextRowH) {
        super.init(frame: NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: height))
        let l = UI.label(text, size: 10.5, color: Tokens.Color.dimGray.withAlphaComponent(0.8))
        l.frame = NSRect(x: Tokens.Layout.titleX, y: (height-14)/2, width: 260, height: 14); addSubview(l)
    }
    required init?(coder: NSCoder) { nil }
}


final class HealthStrip: RowView {
    init(state: MenubarState, showVitals: Bool) {
        super.init(frame: NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: 18))
        typealias L = Tokens.Layout
        let liveGovernor = state.govLive?.alive == true
        let offline = !liveGovernor && state.governor == "offline"
        let stale = state.hasProjectionWarning
        let errs = state.diagnostics?.sidecarParseErrors ?? 0
        let color: NSColor = offline ? Tokens.Color.red : (stale || errs > 0 ? Tokens.Color.orange : Tokens.Color.green)
        let dot = UI.filledDot(color, size: 8); dot.frame = NSRect(x: L.rowPadL, y: 6, width: 8, height: 8); addSubview(dot)
        var text = offline ? "governor offline" : (stale ? "stale — last-good shown" : "governor online")
        if errs > 0 { text += " · \(errs) sidecar err" }
        let l = UI.label(text, size: 9.5, color: Tokens.Color.dimGray)
        l.frame = NSRect(x: L.titleX, y: 4, width: 180, height: 12); addSubview(l)
        if showVitals, let gl = state.govLive {
            var v = ""
            if let free = gl.freeGb { v += String(format: "RAM %.0fg free", free) }
            if let m = gl.mode ?? state.liveMode { v += (v.isEmpty ? "" : " · ") + m }
            if !v.isEmpty {
                let vl = UI.label(v, size: 9.5, color: Tokens.Color.dimGray, align: .right)
                vl.frame = NSRect(x: bounds.width - 168, y: 4, width: 156, height: 12); addSubview(vl)
            }
        }
    }
    required init?(coder: NSCoder) { nil }
}


final class CoordHealthRow: RowView {
    init(_ h: HealthSummary) {
        super.init(frame: NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: 18))
        typealias L = Tokens.Layout
        let color = h.isWarn ? Tokens.Color.orange : Tokens.Color.dimGray
        let l = UI.label(h.stripText, size: 9.5, color: color)
        l.frame = NSRect(x: L.rowPadL, y: 4, width: bounds.width - L.rowPadL - L.rowPadR, height: 12)
        addSubview(l)
    }
    required init?(coder: NSCoder) { nil }
}


final class EmptyStateRow: RowView {
    init(_ text: String) {
        super.init(frame: NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: 52))
        let l = UI.label(text, size: 12, color: Tokens.Color.dimGray, align: .center)
        l.frame = NSRect(x: 12, y: 18, width: Tokens.Layout.popoverWidth - 24, height: 16); addSubview(l)
    }
    required init?(coder: NSCoder) { nil }
}


final class NextUpHeader: RowView {
    var onToggle: (() -> Void)?


    init(shown: Int, total: Int, collapsed: Bool) {
        super.init(frame: NSRect(x: 0, y: 0, width: Tokens.Layout.popoverWidth, height: 23))
        let lab = UI.label("NEXT UP", size: 9, weight: .bold, color: Tokens.Color.sectionGray)
        lab.frame = NSRect(x: Tokens.Layout.rowPadL - 8, y: 7, width: 70, height: 14); addSubview(lab)

        let cnt = UI.label("\(shown) / \(total)", size: 8.5, weight: .light, color: Tokens.Color.gray(0.42, 0.95))

        cnt.frame = NSRect(x: Tokens.Layout.rowPadL + 50, y: 8.5, width: 90, height: 13); addSubview(cnt)

        let btn = NSButton(frame: bounds); btn.isBordered = false; btn.title = ""; btn.target = self
        btn.action = #selector(tap); btn.autoresizingMask = [.width]; addSubview(btn)
    }
    @objc private func tap() { onToggle?() }
    required init?(coder: NSCoder) { nil }
}


enum RowActions {
    static var shared: ((PanelAction) -> Void)?
    static var expand: ((String) -> Void)?
}

extension NSView { @discardableResult func then(_ f: (NSView) -> Void) -> NSView { f(self); return self } }
