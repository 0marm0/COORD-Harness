import AppKit

/// Marks that say, at a glance, which kind of runner owns a row.
///
/// Every mark here is drawn, not loaded. That is deliberate. The board is read
/// at 9 to 17 points, where a scaled-down raster of a detailed logo turns to
/// mush, and where hairlines need to land on pixel boundaries to stay crisp.
/// Drawing also means the repository ships no image files for these, so there
/// is nothing to license, attribute, or accidentally redistribute.
///
/// These are generic role marks -- a chat agent, a code agent, a local
/// accelerator -- not any vendor's logo. If you would rather see real product
/// marks in your own build, drop `claude.png` / `codex.png` into the app's
/// `Assets` directory: every caller looks for a named asset first and only
/// falls back to these.
///
/// The palette matches `ownerBadgeColor`, so a row's mark and its owner tint
/// are the same colour rather than two hues that nearly agree.
enum AgentMarks {

    enum Palette {
        /// Warm orange. The chat lane.
        static let chat = NSColor(calibratedRed: 1.00, green: 0.42, blue: 0.20, alpha: 1)
        /// Violet-to-blue. The code lane.
        static let codeTop = NSColor(calibratedRed: 0.60, green: 0.55, blue: 0.95, alpha: 1)
        static let codeBottom = NSColor(calibratedRed: 0.27, green: 0.47, blue: 1.00, alpha: 1)
        /// Amber for an accelerator, green for plain compute.
        static let accelerated = NSColor(calibratedRed: 0.96, green: 0.77, blue: 0.32, alpha: 1)
        static let compute = NSColor(calibratedRed: 0.20, green: 0.78, blue: 0.35, alpha: 1)
        static let operatorTint = NSColor(calibratedRed: 0.96, green: 0.77, blue: 0.32, alpha: 1)
    }

    // MARK: - Drawing helper

    /// Draw into an image that redraws itself at whatever scale it is shown at.
    private static func mark(_ size: CGFloat, _ draw: @escaping (NSRect) -> Void) -> NSImage {
        let image = NSImage(size: NSSize(width: size, height: size), flipped: false) { rect in
            draw(rect)
            return true
        }
        image.isTemplate = false
        return image
    }

    // MARK: - Chat agent

    /// An eight-bit agent head: antennae, a visor with two eyes, four legs.
    ///
    /// Pixel art rather than a smooth glyph because a mark this small is only a
    /// handful of device pixels across, and a grid of whole squares survives
    /// that where curves do not.
    private static let headGrid = [
        "..#..#..",
        ".######.",
        "########",
        "#.####.#",
        "########",
        "########",
        ".######.",
        "#.#..#.#",
    ]

    static func chatAgent(size: CGFloat, tint: NSColor = Palette.chat) -> NSImage {
        mark(size) { rect in
            let columns = CGFloat(headGrid[0].count)
            let rows = CGFloat(headGrid.count)
            // Square cells, centred, snapped so edges land on pixel boundaries.
            let cell = (min(rect.width, rect.height) / max(columns, rows)).rounded(.down)
            guard cell >= 1 else { return }
            let originX = rect.minX + ((rect.width - cell * columns) / 2).rounded()
            let originY = rect.minY + ((rect.height - cell * rows) / 2).rounded()
            tint.setFill()
            for (rowIndex, row) in headGrid.enumerated() {
                for (columnIndex, character) in row.enumerated() where character == "#" {
                    // The grid reads top-down; AppKit's origin is bottom-left.
                    let flipped = rows - 1 - CGFloat(rowIndex)
                    NSRect(x: originX + CGFloat(columnIndex) * cell,
                           y: originY + flipped * cell,
                           width: cell, height: cell).fill()
                }
            }
        }
    }

    // MARK: - Code agent

    /// A terminal tile: gradient ground, a prompt chevron, a caret rule.
    static func codeAgent(size: CGFloat) -> NSImage {
        mark(size) { rect in
            let box = rect.insetBy(dx: rect.width * 0.06, dy: rect.height * 0.06)
            let radius = box.width * 0.30
            let tile = NSBezierPath(roundedRect: box, xRadius: radius, yRadius: radius)
            NSGradient(starting: Palette.codeTop, ending: Palette.codeBottom)?
                .draw(in: tile, angle: -90)

            let stroke = max(1, (box.width * 0.10).rounded())
            NSColor.white.setStroke()

            // The chevron sits left of centre, the rule right of it: `>_`.
            let chevron = NSBezierPath()
            chevron.lineWidth = stroke
            chevron.lineCapStyle = .round
            chevron.lineJoinStyle = .round
            chevron.move(to: NSPoint(x: box.minX + box.width * 0.28, y: box.minY + box.height * 0.68))
            chevron.line(to: NSPoint(x: box.minX + box.width * 0.46, y: box.midY + box.height * 0.03))
            chevron.line(to: NSPoint(x: box.minX + box.width * 0.28, y: box.minY + box.height * 0.30))
            chevron.stroke()

            let rule = NSBezierPath()
            rule.lineWidth = stroke
            rule.lineCapStyle = .round
            rule.move(to: NSPoint(x: box.minX + box.width * 0.56, y: box.minY + box.height * 0.32))
            rule.line(to: NSPoint(x: box.minX + box.width * 0.76, y: box.minY + box.height * 0.32))
            rule.stroke()
        }
    }

    // MARK: - Local runner

    /// A packaged die with pins: the mark for work running on this machine.
    ///
    /// Filled when the job holds the accelerator, outlined when it is ordinary
    /// compute -- the same distinction the panel draws, so a glance at the
    /// column tells you what is contended.
    static func silicon(size: CGFloat, accelerated: Bool) -> NSImage {
        mark(size) { rect in
            let tint = accelerated ? Palette.accelerated : Palette.compute
            let body = rect.insetBy(dx: rect.width * 0.22, dy: rect.height * 0.22)
            let line = max(1, (rect.width * 0.07).rounded())

            // Pins, three to a side, before the package so they tuck under it.
            tint.setStroke()
            let pins = NSBezierPath()
            pins.lineWidth = line
            pins.lineCapStyle = .round
            for step in 0..<3 {
                let offset = CGFloat(step + 1) / 4
                let x = body.minX + body.width * offset
                let y = body.minY + body.height * offset
                pins.move(to: NSPoint(x: x, y: body.maxY)); pins.line(to: NSPoint(x: x, y: rect.maxY - line / 2))
                pins.move(to: NSPoint(x: x, y: body.minY)); pins.line(to: NSPoint(x: x, y: rect.minY + line / 2))
                pins.move(to: NSPoint(x: body.minX, y: y)); pins.line(to: NSPoint(x: rect.minX + line / 2, y: y))
                pins.move(to: NSPoint(x: body.maxX, y: y)); pins.line(to: NSPoint(x: rect.maxX - line / 2, y: y))
            }
            pins.stroke()

            let radius = body.width * 0.22
            let package = NSBezierPath(roundedRect: body, xRadius: radius, yRadius: radius)
            package.lineWidth = line
            if accelerated {
                tint.setFill()
                package.fill()
                // Knock the core out of the fill so the mark keeps its structure.
                let core = body.insetBy(dx: body.width * 0.28, dy: body.height * 0.28)
                NSGraphicsContext.current?.compositingOperation = .destinationOut
                NSBezierPath(roundedRect: core, xRadius: core.width * 0.28, yRadius: core.width * 0.28).fill()
                NSGraphicsContext.current?.compositingOperation = .sourceOver
            } else {
                package.stroke()
                let core = body.insetBy(dx: body.width * 0.28, dy: body.height * 0.28)
                let inner = NSBezierPath(roundedRect: core, xRadius: core.width * 0.28, yRadius: core.width * 0.28)
                inner.lineWidth = line
                inner.stroke()
            }
        }
    }

    // MARK: - Operator

    /// A person at the controls: work a human took rather than delegated.
    static func operatorMark(size: CGFloat, tint: NSColor = Palette.operatorTint) -> NSImage {
        mark(size) { rect in
            tint.setFill()
            let head = NSRect(x: rect.midX - rect.width * 0.17, y: rect.minY + rect.height * 0.52,
                              width: rect.width * 0.34, height: rect.width * 0.34)
            NSBezierPath(ovalIn: head).fill()
            let shoulders = NSRect(x: rect.minX + rect.width * 0.16, y: rect.minY + rect.height * 0.14,
                                   width: rect.width * 0.68, height: rect.height * 0.34)
            NSBezierPath(roundedRect: shoulders,
                         xRadius: shoulders.width * 0.42, yRadius: shoulders.width * 0.42).fill()
            // Trim the lower corners so the bust reads as shoulders, not a pill.
            NSGraphicsContext.current?.compositingOperation = .destinationOut
            NSBezierPath(rect: NSRect(x: rect.minX, y: rect.minY,
                                      width: rect.width, height: rect.height * 0.14)).fill()
            NSGraphicsContext.current?.compositingOperation = .sourceOver
        }
    }

    // MARK: - Composition

    /// A hardware mark with the driving agent tucked into its corner.
    ///
    /// A local job has two facts worth showing at once: what it is using, and
    /// who set it running. Two columns would be wasteful for something this
    /// small, so the agent rides the hardware mark as a badge -- the same
    /// arrangement the running-job rows use in the panel.
    static func localRunner(size: CGFloat, accelerated: Bool, driver: OwnerKind?) -> NSImage {
        // Through the named lookup, so the badge rides whatever hardware mark
        // the build actually uses rather than always the drawn one.
        let base = owner(.local, size: size, accelerated: accelerated)
            ?? silicon(size: size, accelerated: accelerated)
        guard let driver, let badge = badgeImage(for: driver, size: (size * 0.46).rounded()) else {
            return base
        }
        let composed = NSImage(size: NSSize(width: size, height: size), flipped: false) { rect in
            base.draw(in: rect)
            let side = badge.size.width
            let corner = NSRect(x: rect.maxX - side, y: rect.minY, width: side, height: side)
            // Punch a hole first so the badge reads against the pins behind it.
            NSGraphicsContext.current?.compositingOperation = .destinationOut
            NSBezierPath(ovalIn: corner.insetBy(dx: -side * 0.14, dy: -side * 0.14)).fill()
            NSGraphicsContext.current?.compositingOperation = .sourceOver
            badge.draw(in: corner)
            return true
        }
        return composed
    }

    /// The lane named by a session identifier, or nil if it names none.
    ///
    /// A local job is owned by the runner but driven by an agent, and the board
    /// records the driver as a session label. `nil` rather than a default: an
    /// unbadged mark says "not recorded", where a guessed badge would assert a
    /// lane the board never claimed.
    static func lane(from session: String?) -> OwnerKind? {
        let head = (session ?? "").split(separator: ":").first.map(String.init) ?? ""
        switch head.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "claude": return .claude
        case "codex": return .codex
        case "mixed": return .mixed
        default: return nil
        }
    }

    private static func badgeImage(for kind: OwnerKind, size: CGFloat) -> NSImage? {
        switch kind {
        // Through the same lookup as the owner column, so a substituted mark
        // shows up on a badged local row too rather than only in one place.
        case .claude, .codex, .mixed: return owner(kind, size: size)
        case .local, .operatorUser: return nil
        }
    }

    // MARK: - Named lookup

    /// A square image of exactly `size`, with the source drawn aspect-fit.
    ///
    /// `NSImage(contentsOf:)` reports the file's pixel dimensions as its size,
    /// so a 256px asset answers 256 when asked how big it is. Callers that size
    /// a badge or a cell from `image.size` then work in the wrong units -- a
    /// 15pt badge drawn 256pt wide. Normalising here means every mark this type
    /// returns is the size that was asked for, whatever produced it.
    private static func fit(_ image: NSImage, in size: CGFloat) -> NSImage {
        let source = image.size
        guard source.width > 0, source.height > 0 else { return image }
        let scale = min(size / source.width, size / source.height)
        let drawn = NSSize(width: source.width * scale, height: source.height * scale)
        return NSImage(size: NSSize(width: size, height: size), flipped: false) { rect in
            image.draw(in: NSRect(x: rect.midX - drawn.width / 2,
                                  y: rect.midY - drawn.height / 2,
                                  width: drawn.width, height: drawn.height))
            return true
        }
    }

    /// The mark for an owner, preferring a bundled asset when one is present.
    ///
    /// Asset first, drawn second. That ordering is what lets a build drop in
    /// real product marks without touching this file.
    static func owner(_ kind: OwnerKind, size: CGFloat, accelerated: Bool = false) -> NSImage? {
        func named(_ names: String...) -> NSImage? {
            for name in names {
                if let image = NSImage(named: name) { return fit(image, in: size) }
                if let url = Bundle.main.url(forResource: name, withExtension: "png", subdirectory: "Assets"),
                   let image = NSImage(contentsOf: url) {
                    return fit(image, in: size)
                }
            }
            return nil
        }
        switch kind {
        case .claude:
            return named("claude") ?? chatAgent(size: size)
        case .codex:
            return named("codex", "openai") ?? codeAgent(size: size)
        case .mixed:
            return named("mixed") ?? chatAgent(size: size)
        case .local:
            return named(accelerated ? "accelerator" : "compute")
                ?? silicon(size: size, accelerated: accelerated)
        case .operatorUser:
            return named("operator") ?? operatorMark(size: size)
        }
    }
}
