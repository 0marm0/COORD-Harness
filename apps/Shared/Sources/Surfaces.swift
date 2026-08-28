import SwiftUI

/// The surface primitives. `glassEffect` arrived in the 26 SDKs; everything
/// below falls back to a material so the same call site renders on the
/// deployment targets this project actually declares.
extension View {
    func panelSurface(cornerRadius: CGFloat = Theme.radius, tint: Color? = nil) -> some View {
        let shape = RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
        return background(tint ?? Theme.surface.opacity(0.72), in: shape)
            .overlay(shape.strokeBorder(Theme.hairlineColor, lineWidth: Theme.hairline))
    }

    func pillSurface(tint: Color? = nil) -> some View {
        padding(.horizontal, Space.sm)
            .padding(.vertical, Space.xs + 1)
            .background(tint ?? Theme.surfaceSoft, in: Capsule())
            .overlay(Capsule().strokeBorder(Theme.hairlineColor, lineWidth: Theme.hairline))
    }
}

/// True black with a faint graphite top and a blue floor glow. The glow is the
/// only decorative gradient in the app; it is what keeps a mostly-empty board
/// from reading as a failed load.
struct CoordCanvas: View {
    var body: some View {
        ZStack {
            Theme.canvas.ignoresSafeArea()
            LinearGradient(
                colors: [Color.white.opacity(0.025), .clear, .clear],
                startPoint: .top,
                endPoint: .center
            )
            .ignoresSafeArea()
            RadialGradient(
                colors: [Theme.glow.opacity(0.18), .clear],
                center: .bottom,
                startRadius: 0,
                endRadius: 520
            )
            .ignoresSafeArea()
            .blendMode(.screen)
        }
    }
}

/// The wordmark is letter-spaced type, not an image, so it stays crisp at every
/// size and needs no asset to ship with the app.
struct CoordWordmark: View {
    var size: CGFloat = 20
    var body: some View {
        Text("COORD")
            .font(.system(size: size, weight: .light))
            .tracking(size * 0.34)
            .foregroundStyle(Theme.textHi)
            .accessibilityLabel("Coord")
    }
}

/// Which kind of thing holds a row. The lane is read from the owner label, the
/// same derivation the web board makes, so the two never disagree about who is
/// holding a row.
enum OwnerLane {
    case chat, code, accelerator, compute, operatorHuman, unassigned

    static func parse(_ owner: String?) -> OwnerLane {
        let raw = (owner ?? "").lowercased()
        guard !raw.isEmpty else { return .unassigned }
        let lane = raw.split(separator: ":").first.map(String.init) ?? raw
        let detail = raw.split(separator: ":").dropFirst().joined(separator: ":")
        switch lane.trimmingCharacters(in: .whitespaces) {
        case "claude": return .chat
        case "codex": return .code
        case "operator": return .operatorHuman
        case "local", "service":
            return detail.contains("gpu") || detail.contains("mlx") || detail.contains("metal")
                || detail.contains("accel") ? .accelerator : .compute
        default: return .unassigned
        }
    }

    var asset: String? {
        switch self {
        case .chat: "claude"
        case .code: "codex"
        case .accelerator: "accelerator"
        case .compute: "compute"
        case .operatorHuman, .unassigned: nil
        }
    }

    var glyph: String {
        switch self {
        case .chat: "bubble.left"
        case .code: "chevron.left.forwardslash.chevron.right"
        case .accelerator, .compute: "cpu"
        case .operatorHuman: "person"
        case .unassigned: "circle.dashed"
        }
    }

    var label: String {
        switch self {
        case .chat: "chat agent"
        case .code: "code agent"
        case .accelerator: "local accelerator"
        case .compute: "local compute"
        case .operatorHuman: "operator"
        case .unassigned: "unassigned"
        }
    }
}

/// The real mark when the asset is bundled, an SF glyph when it is not. A
/// missing asset degrades to a legible glyph rather than to blank space.
struct OwnerMark: View {
    let lane: OwnerLane
    var size: CGFloat = 15

    /// The marks ship as a folder reference, so a plain named lookup misses
    /// them; resolve the bundled file too before falling back to a glyph.
    private var bundled: Image? {
        guard let asset = lane.asset else { return nil }
        #if canImport(UIKit)
        if let image = UIImage(named: asset) { return Image(uiImage: image) }
        if let url = Bundle.main.url(forResource: asset, withExtension: "png", subdirectory: "Assets"),
           let data = try? Data(contentsOf: url),
           let image = UIImage(data: data) {
            return Image(uiImage: image)
        }
        #else
        if let image = NSImage(named: asset) { return Image(nsImage: image) }
        if let url = Bundle.main.url(forResource: asset, withExtension: "png", subdirectory: "Assets"),
           let image = NSImage(contentsOf: url) {
            return Image(nsImage: image)
        }
        #endif
        return nil
    }

    var body: some View {
        Group {
            if let bundled {
                bundled.resizable().scaledToFit()
            } else {
                Image(systemName: lane.glyph).resizable().scaledToFit().foregroundStyle(Theme.muted)
            }
        }
        .frame(width: size, height: size)
        .accessibilityLabel(lane.label)
    }
}

/// A thin determinate bar. It is drawn only where a fraction was reported;
/// there is deliberately no indeterminate variant, because a moving bar over an
/// unreported value invents progress.
struct ProgressBar: View {
    let fraction: Double
    var tone: Tone = .accent

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Theme.surfaceSoft)
                Capsule().fill(tone.fg).frame(width: max(0, geo.size.width * Fmt.fraction(fraction)))
            }
        }
        .frame(height: 4)
        .accessibilityLabel("Reported progress \(Fmt.pct(fraction))")
    }
}
