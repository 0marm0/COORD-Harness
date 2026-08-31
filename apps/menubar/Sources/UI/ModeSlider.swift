import AppKit


final class ModeSlider: NSView {

    enum Stop: Int, CaseIterable {
        case pause = 0, medium = 1, full = 2
        var name: String { ["pause", "medium", "full"][rawValue] }
        var letter: String { ["⏸", "M", "F"][rawValue] }
        init(_ mode: String) {
            switch mode.lowercased() {
            case "pause": self = .pause
            case "medium": self = .medium
            default: self = .full
            }
        }
    }

    var onSetMode: ((String) -> Void)?
    private var stop: Stop = .full
    private var paused = false
    private var dragIdx: Int?
    private var pendingName: String?
    private var pendingAt: Date?
    override var isFlipped: Bool { true }

    private var knobR: CGFloat { max(5, bounds.height/2 - 7) }

    func setLiveMode(_ mode: String, paused: Bool) {


        if let pn = pendingName, let t = pendingAt {
            if mode.lowercased() == pn { pendingName = nil; pendingAt = nil }
            else if Date().timeIntervalSince(t) < 20 {
                self.paused = false
                if let s = Stop(rawValue: Stop(pn).rawValue) { self.stop = s }
                needsDisplay = true; return
            } else { pendingName = nil; pendingAt = nil }
        }
        self.paused = paused
        self.stop = paused ? .pause : Stop(mode)
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        let midY = bounds.height/2


        let th: CGFloat = 8.0
        let track = NSBezierPath(roundedRect: NSRect(x: 2, y: midY - th/2, width: bounds.width - 4, height: th), xRadius: th/2, yRadius: th/2)
        Tokens.Color.gray(1, paused ? 0.06 : 0.10).set(); track.fill()


        let x0 = knobR + 2, x1 = bounds.width - knobR - 2

        let active = dragIdx ?? stop.rawValue
        for s in Stop.allCases where s.rawValue != active {
            let cx = stopX(s, x0: x0, x1: x1)
            let dot = NSBezierPath(ovalIn: NSRect(x: cx - 1.6, y: midY - 1.6, width: 3.2, height: 3.2))
            Tokens.Color.gray(1, 0.28).set(); dot.fill()
        }

        let cur = Stop(rawValue: active) ?? .full
        let cx = stopX(cur, x0: x0, x1: x1)
        let knob = NSBezierPath(ovalIn: NSRect(x: cx - knobR, y: midY - knobR, width: 2*knobR, height: 2*knobR))

        let tint = paused ? Tokens.Color.gray(0.62) : Tokens.modeTint(cur.name)
        Tokens.Color.gray(0.08, 0.55).set(); knob.fill()
        tint.set(); knob.lineWidth = 0.75; knob.stroke()

        let glyph = paused ? "⏸" : cur.letter
        let para = NSMutableParagraphStyle(); para.alignment = .center
        let s = NSAttributedString(string: glyph, attributes: [
            .font: NSFont.systemFont(ofSize: paused ? 9 : 8, weight: .regular),
            .foregroundColor: NSColor.white.withAlphaComponent(0.78), .paragraphStyle: para])
        let sz = s.size(); s.draw(at: NSPoint(x: cx - sz.width/2, y: midY - sz.height/2))
    }

    private func blendToWhite(_ c: NSColor, _ t: CGFloat) -> NSColor {
        c.blended(withFraction: t, of: .white) ?? c
    }
    private func stopX(_ s: Stop, x0: CGFloat, x1: CGFloat) -> CGFloat {
        x0 + (x1 - x0) * CGFloat(s.rawValue) / CGFloat(Stop.allCases.count - 1)
    }


    override func mouseDown(with e: NSEvent) { dragIdx = nearest(convert(e.locationInWindow, from: nil)); needsDisplay = true }
    override func mouseDragged(with e: NSEvent) { dragIdx = nearest(convert(e.locationInWindow, from: nil)); needsDisplay = true }
    override func mouseUp(with e: NSEvent) {
        let idx = nearest(convert(e.locationInWindow, from: nil))
        dragIdx = nil
        let s = Stop(rawValue: idx) ?? .full
        stop = s; paused = s == .pause; needsDisplay = true
        pendingName = s.name; pendingAt = Date()
        onSetMode?(s.name)
    }
    private func nearest(_ p: NSPoint) -> Int {
        let x0 = knobR, x1 = bounds.width - knobR
        let frac = max(0, min(1, (p.x - x0) / (x1 - x0)))
        return Int((frac * CGFloat(Stop.allCases.count - 1)).rounded())
    }
}
