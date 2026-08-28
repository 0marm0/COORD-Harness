import AppKit
enum ProviderMenuMark {
    private static let sourceAssetDirectory = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .appendingPathComponent("brand/Assets", isDirectory: true)

    private static let cached: [String: NSImage] = Dictionary(
        uniqueKeysWithValues: ["claude", "codex"].compactMap { identity in
            load(identity).map { (identity, $0) }
        }
    )

    static func image(for identity: String) -> NSImage? {
        cached[identity.lowercased()]
    }

    private static func load(_ identity: String) -> NSImage? {
        for name in ["\(identity)-menu", identity] {
            let bundleURLs = [
                Bundle.main.url(forResource: name, withExtension: "png", subdirectory: "Assets"),
                Bundle.main.url(forResource: name, withExtension: "png"),
            ].compactMap { $0 }
            let sourceURL = sourceAssetDirectory.appendingPathComponent("\(name).png")
            for url in bundleURLs + [sourceURL] {
                guard let image = NSImage(contentsOf: url), isUsable(image) else { continue }
                image.isTemplate = false
                return image
            }
        }
        return nil
    }

    private static func isUsable(_ image: NSImage) -> Bool {
        guard let data = image.tiffRepresentation,
              let bitmap = NSBitmapImageRep(data: data),
              bitmap.pixelsWide > 0,
              bitmap.pixelsHigh > 0
        else { return false }
        var firstVisibleRGB: (Int, Int, Int)?
        var sawVisible = false
        var sawTransparent = false
        var sawVariation = false
        for y in 0..<bitmap.pixelsHigh {
            for x in 0..<bitmap.pixelsWide {
                guard let color = bitmap.colorAt(x: x, y: y)?.usingColorSpace(.deviceRGB) else { continue }
                if color.alphaComponent < 0.05 {
                    sawTransparent = true
                    continue
                }
                sawVisible = true
                let rgb = (
                    Int((color.redComponent * 255).rounded()),
                    Int((color.greenComponent * 255).rounded()),
                    Int((color.blueComponent * 255).rounded())
                )
                if let firstVisibleRGB {
                    sawVariation = sawVariation || firstVisibleRGB != rgb
                } else {
                    firstVisibleRGB = rgb
                }
            }
        }
        return sawVisible && (sawTransparent || sawVariation)
    }
}



enum RingRenderer {

    struct TelemetryPresentation {
        let label: String
        let value: String
        let severity: SystemTelemetrySeverity
    }

    static func statusImage(
        usage: UsageStatusPresentation,
        mode: UsageStatusMode,
        telemetry: [TelemetryPresentation] = [],
        compactTelemetrySpacing: Bool = true,
        palette: UsageBarPalette = .colored
    ) -> NSImage {
        let height = StatusItemImageLayout.height
        let baseWidth = CGFloat(UsageStatusLayout.baseWidth(mode: mode))
        let width = StatusItemImageLayout.imageWidth(
            baseWidth: baseWidth,
            moduleCount: telemetry.count,
            compact: compactTelemetrySpacing
        )
        let scale = Tokens.Layout.ringScale
        guard let rep = bitmap(Int((width * scale).rounded()), Int((height * scale).rounded())),
              let context = NSGraphicsContext(bitmapImageRep: rep)
        else { return NSImage(size: NSSize(width: width, height: height)) }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = context
        context.cgContext.scaleBy(x: scale, y: scale)

        switch mode {
        case .bars: drawQuotaBars(usage, width: baseWidth, height: height, palette: palette)
        case .rings: drawQuotaRings(usage, width: baseWidth, height: height, palette: palette)
        case .minimal: drawMinimal(usage, width: baseWidth, height: height)
        }
        drawTelemetry(
            telemetry,
            baseWidth: baseWidth,
            compactSpacing: compactTelemetrySpacing
        )

        NSGraphicsContext.restoreGraphicsState()
        let image = NSImage(size: NSSize(width: width, height: height))
        image.addRepresentation(rep)
        image.isTemplate = false
        return image
    }

    private static func drawQuotaBars(_ usage: UsageStatusPresentation, width: CGFloat, height: CGFloat, palette: UsageBarPalette) {
        for (index, identity) in ["claude", "codex"].enumerated() {
            let rowCenter = index == 0 ? CGFloat(16) : CGFloat(6)
            let selection = usage.selections.first { $0.provider.lowercased() == identity }
            drawProviderMark(
                identity,
                in: NSRect(
                    x: UsageStatusLayout.providerIconX,
                    y: rowCenter - UsageStatusLayout.providerIconSize / 2,
                    width: UsageStatusLayout.providerIconSize,
                    height: UsageStatusLayout.providerIconSize
                )
            )
            let quotaColor = usageColor(identity: identity, palette: palette)
            drawTinyBar(
                usage.displayPercent(selection?.window),
                x: UsageStatusLayout.quotaBarX,
                y: rowCenter - 1.5,
                width: UsageStatusLayout.quotaBarWidth,
                color: quotaColor,
                warningMarkerPercent: usage.warningMarkerPercent
            )
            drawText(
                quotaPercent(usage.displayPercent(selection?.window)),
                in: NSRect(x: UsageStatusLayout.quotaPercentX, y: rowCenter - 5, width: UsageStatusLayout.percentWidth, height: 10),
                size: 8.2,
                color: quotaColor,
                alignment: .left,
                monospacedDigits: true
            )
        }
    }

    private static func drawQuotaRings(_ usage: UsageStatusPresentation, width: CGFloat, height: CGFloat, palette: UsageBarPalette) {
        for (index, identity) in ["claude", "codex"].enumerated() {
            let x = CGFloat(1 + index * 24)
            let selection = usage.selections.first { $0.provider.lowercased() == identity }
            drawProviderMark(identity, in: NSRect(x: x, y: 5.75, width: UsageStatusLayout.providerIconSize, height: UsageStatusLayout.providerIconSize))
            drawMiniRing(usage.displayPercent(selection?.window), x: x + 12, y: 6, color: usageColor(identity: identity, palette: palette))
        }
    }

    private static func usageColor(identity: String, palette: UsageBarPalette) -> NSColor {
        palette == .neutral ? NSColor.labelColor.withAlphaComponent(0.82) : providerColor(identity)
    }

    private static func drawMinimal(_ usage: UsageStatusPresentation, width: CGFloat, height: CGFloat) {
        drawText("CORD", in: NSRect(x: 1, y: 4, width: width - 7, height: 12), size: 7.5, color: .labelColor)
        drawFreshness(usage, x: width - 3, y: 9, diameter: 3)
    }

    private static func drawTelemetry(
        _ telemetry: [TelemetryPresentation],
        baseWidth: CGFloat,
        compactSpacing: Bool
    ) {
        for (index, metric) in telemetry.enumerated() {
            let module = StatusItemImageLayout.telemetryFrame(
                index: index,
                baseWidth: baseWidth,
                compact: compactSpacing
            )
            drawText(
                metric.label,
                in: StatusItemImageLayout.telemetryLabelFrame(in: module),
                size: 7,
                color: NSColor.labelColor.withAlphaComponent(0.72),
                alignment: .center
            )
            drawText(
                metric.value,
                in: StatusItemImageLayout.telemetryValueFrame(in: module),
                size: 10.5,
                color: telemetryColor(for: metric.severity),
                alignment: .center,
                monospacedDigits: true
            )
        }
    }

    private static func telemetryColor(for severity: SystemTelemetrySeverity) -> NSColor {
        switch severity {
        case .unavailable: return .secondaryLabelColor
        case .normal: return .systemBlue
        case .warning: return .systemOrange
        case .critical: return .systemRed
        }
    }

    private static func drawTinyBar(_ remaining: Double?, x: CGFloat, y: CGFloat, width: CGFloat, color: NSColor, warningMarkerPercent: Double? = nil) {
        let track = NSBezierPath(roundedRect: NSRect(x: x, y: y, width: width, height: 3), xRadius: 1.5, yRadius: 1.5)
        NSColor.labelColor.withAlphaComponent(0.25).setFill()
        track.fill()
        guard let remaining else { return }
        let fillWidth = max(1, width * CGFloat(min(max(remaining, 0), 100)) / 100)
        let fill = NSBezierPath(roundedRect: NSRect(x: x, y: y, width: fillWidth, height: 3), xRadius: 1.5, yRadius: 1.5)
        color.setFill()
        fill.fill()
        if let warningMarkerPercent {
            let markerX = x + width * CGFloat(min(100, max(0, warningMarkerPercent))) / 100
            NSColor.systemRed.withAlphaComponent(0.95).setStroke()
            let marker = NSBezierPath()
            marker.move(to: NSPoint(x: markerX, y: y - 1))
            marker.line(to: NSPoint(x: markerX, y: y + 4))
            marker.lineWidth = 0.7
            marker.stroke()
        }
    }

    private static func drawMiniRing(_ remaining: Double?, x: CGFloat, y: CGFloat, color: NSColor) {
        let rect = NSRect(x: x, y: y, width: 10, height: 10)
        let track = NSBezierPath(ovalIn: rect)
        track.lineWidth = 1.4
        NSColor.secondaryLabelColor.withAlphaComponent(0.25).setStroke()
        track.stroke()
        guard let remaining else { return }
        let value = min(max(remaining, 0), 100)
        let arc = NSBezierPath()
        arc.appendArc(
            withCenter: NSPoint(x: rect.midX, y: rect.midY),
            radius: 4.3,
            startAngle: 90,
            endAngle: 90 - 360 * CGFloat(value) / 100,
            clockwise: true
        )
        arc.lineWidth = 1.4
        arc.lineCapStyle = .round
        color.setStroke()
        arc.stroke()
    }

    private static func drawProviderMark(_ identity: String, in rect: NSRect) {
        guard let image = ProviderMenuMark.image(for: identity), image.size.width > 0, image.size.height > 0 else {
            drawText(identity == "claude" ? "C" : "X", in: rect, size: 6, color: providerColor(identity), alignment: .center)
            return
        }
        let scale = min(rect.width / image.size.width, rect.height / image.size.height)
        let size = NSSize(width: image.size.width * scale, height: image.size.height * scale)
        let target = NSRect(
            x: rect.midX - size.width / 2,
            y: rect.midY - size.height / 2,
            width: size.width,
            height: size.height
        )
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current?.imageInterpolation = .high
        image.draw(in: target, from: .zero, operation: .sourceOver, fraction: 1)
        NSGraphicsContext.current?.compositingOperation = .sourceAtop
        providerColor(identity).setFill()
        NSBezierPath(rect: target).fill()
        NSGraphicsContext.restoreGraphicsState()
    }

    private static func quotaPercent(_ remaining: Double?) -> String {
        guard let remaining else { return "—" }
        return "\(Int(min(max(remaining, 0), 100).rounded()))%"
    }

    private static func drawFreshness(
        _ usage: UsageStatusPresentation,
        x: CGFloat,
        y: CGFloat,
        diameter: CGFloat = 4
    ) {
        let color: NSColor = usage.unavailable ? .systemRed : (usage.stale ? .systemOrange : .systemGreen)
        color.setFill()
        NSBezierPath(ovalIn: NSRect(x: x, y: y, width: diameter, height: diameter)).fill()
    }

    private static func providerColor(_ identity: String) -> NSColor {
        identity == "claude"
            ? NSColor(calibratedRed: 0.96, green: 0.50, blue: 0.32, alpha: 1)
            : NSColor(calibratedRed: 0.58, green: 0.40, blue: 0.96, alpha: 1)
    }

    private static func drawText(
        _ text: String,
        in rect: NSRect,
        size: CGFloat,
        color: NSColor,
        alignment: NSTextAlignment = .left,
        monospacedDigits: Bool = false
    ) {
        let paragraph = NSMutableParagraphStyle()
        paragraph.alignment = alignment
        paragraph.lineBreakMode = .byTruncatingTail
        NSAttributedString(string: text, attributes: [
            .font: monospacedDigits
                ? NSFont.monospacedDigitSystemFont(ofSize: size, weight: .semibold)
                : NSFont.systemFont(ofSize: size, weight: .medium),
            .foregroundColor: color,
            .paragraphStyle: paragraph,
        ]).draw(in: rect)
    }


    static func combinedImage(pct: Double?, etaText: String?) -> NSImage {
        typealias L = Tokens.Layout
        let ringPt = 18.0, fontPt = L.statusFont, gap = L.statusGap, pad = L.statusPad, H = L.statusImgH


        let pretty = etaText ?? ""
        let etaFont = NSFont.systemFont(ofSize: fontPt)
        let etaAttrs: [NSAttributedString.Key: Any] = [.font: etaFont, .foregroundColor: NSColor.white]
        let etaStr = pretty.isEmpty ? nil : NSAttributedString(string: pretty, attributes: etaAttrs)
        let textW = etaStr?.size().width ?? 0

        let W = pad + textW + (etaStr != nil ? gap : 0) + ringPt + pad
        let scale = L.ringScale
        guard let rep = bitmap(Int((W*scale).rounded()), Int((H*scale).rounded())),
              let ctx = NSGraphicsContext(bitmapImageRep: rep) else { return NSImage(size: .init(width: W, height: H)) }
        NSGraphicsContext.saveGraphicsState(); NSGraphicsContext.current = ctx
        ctx.cgContext.scaleBy(x: scale, y: scale)


        if let etaStr = etaStr {
            let ty = (H - etaStr.size().height) / 2
            etaStr.draw(at: NSPoint(x: pad, y: ty))
        }

        let ringX = pad + textW + (etaStr != nil ? gap : 0)
        drawRing(into: ctx, pct: pct, originX: ringX, originY: (H - ringPt) / 2, size: ringPt)

        NSGraphicsContext.restoreGraphicsState()
        let img = NSImage(size: NSSize(width: W, height: H)); img.addRepresentation(rep); img.isTemplate = false
        return img
    }


    static func ringImage(pct: Double?, size: CGFloat = Tokens.Layout.ringS) -> NSImage {
        let scale = Tokens.Layout.ringScale
        guard let rep = bitmap(Int((size*scale).rounded()), Int((size*scale).rounded())),
              let ctx = NSGraphicsContext(bitmapImageRep: rep) else { return NSImage(size: .init(width: size, height: size)) }
        NSGraphicsContext.saveGraphicsState(); NSGraphicsContext.current = ctx
        ctx.cgContext.scaleBy(x: scale, y: scale)
        drawRing(into: ctx, pct: pct, originX: 0, originY: 0, size: size)
        NSGraphicsContext.restoreGraphicsState()
        let img = NSImage(size: NSSize(width: size, height: size)); img.addRepresentation(rep); img.isTemplate = false
        return img
    }


    private static func drawRing(into ctx: NSGraphicsContext, pct: Double?, originX: CGFloat, originY: CGFloat, size: CGFloat) {
        let lw = Tokens.Layout.ringLW
        let cx = originX + size/2, cy = originY + size/2
        let r = size/2 - lw/2 - 0.6

        let track = NSBezierPath(ovalIn: NSRect(x: cx - r, y: cy - r, width: 2*r, height: 2*r))
        track.lineWidth = lw; Tokens.Color.gray(0.55, 0.25).set(); track.stroke()

        guard let pct = pct else { return }
        let frac = max(0, min(100, pct))
        let arc = NSBezierPath()
        arc.appendArc(withCenter: NSPoint(x: cx, y: cy), radius: r,
                      startAngle: 90, endAngle: 90 - 360 * CGFloat(frac)/100, clockwise: true)
        arc.lineWidth = lw; arc.lineCapStyle = .round

        ctx.saveGraphicsState()
        let glow = NSShadow(); glow.shadowOffset = .zero; glow.shadowBlurRadius = 2.6
        glow.shadowColor = Tokens.Color.glowBlue.withAlphaComponent(0.95); glow.set()
        Tokens.Color.progressBlue.blended(withFraction: 0.18, of: Tokens.Color.progressBlueLt)?.set() ?? Tokens.Color.progressBlue.set()
        arc.stroke()
        ctx.restoreGraphicsState()


        let num = String(Int(frac.rounded()))
        let inner = 2 * (r - lw/2)
        let fsize = min((inner * 0.92) / CGFloat(max(1, num.count)) / 0.70, inner * 1.06)
        let para = NSMutableParagraphStyle(); para.alignment = .center
        let str = NSAttributedString(string: num, attributes: [
            .font: NSFont.systemFont(ofSize: fsize, weight: .medium),
            .foregroundColor: NSColor.white, .paragraphStyle: para])
        let sz = str.size()
        str.draw(at: NSPoint(x: cx - sz.width/2, y: cy - sz.height/2))
    }

    private static func bitmap(_ w: Int, _ h: Int) -> NSBitmapImageRep? {
        NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: w, pixelsHigh: h, bitsPerSample: 8,
                         samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                         colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)
    }


    static func fmtETA(_ secs: Double) -> String? { ETAFormat.fmtETA(secs) }
}
