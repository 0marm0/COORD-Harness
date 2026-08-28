import SwiftUI

extension Color {
    /// Construct a Color from a 0xRRGGBB literal.
    init(hex: UInt32, alpha: Double = 1.0) {
        let r = Double((hex >> 16) & 0xFF) / 255.0
        let g = Double((hex >> 8) & 0xFF) / 255.0
        let b = Double(hex & 0xFF) / 255.0
        self.init(.sRGB, red: r, green: g, blue: b, opacity: alpha)
    }
}

/// The one palette every native surface reads from. The web board and these
/// apps are two renderings of the same board, so they carry the same neutrals
/// and the same single accent rather than each inventing their own.
enum Theme {
    static let canvas   = Color(hex: 0x000000)
    static let bg       = Color(hex: 0x050608)
    static let surface  = Color(hex: 0x0E1013)
    static let surface2 = Color(hex: 0x16191E)
    static let border   = Color(hex: 0x23272E)
    static let hairlineColor = Color.white.opacity(0.08)
    static let glow     = Color(hex: 0x2E6BFF)

    static let textHi = Color(hex: 0xF4F6FA)
    static let text   = Color(hex: 0xE6E9EF)
    static let muted  = Color(hex: 0x8B939E)
    static let faint  = Color(hex: 0x565E6A)

    static let accent    = Color(hex: 0x7AA2FF)
    static let accentDim = Color(hex: 0x2A3A5E)

    static let pos  = Color(hex: 0x4ADE80)
    static let neg  = Color(hex: 0xF87171)
    static let warn = Color(hex: 0xFBBF24)

    static let accentSoft  = accent.opacity(0.14)
    static let posSoft     = pos.opacity(0.14)
    static let negSoft     = neg.opacity(0.14)
    static let warnSoft    = warn.opacity(0.14)
    static let surfaceSoft = Color.white.opacity(0.05)

    static let radiusCard: CGFloat  = 20
    static let radius: CGFloat      = 14
    static let radiusSmall: CGFloat = 9
    static let radiusPill: CGFloat  = 999
    static let hairline: CGFloat    = 1
}

enum Space {
    static let xxs: CGFloat = 2
    static let xs: CGFloat  = 4
    static let sm: CGFloat  = 8
    static let md: CGFloat  = 12
    static let lg: CGFloat  = 16
    static let xl: CGFloat  = 24
    static let xxl: CGFloat = 32
}

/// A derived status maps to one tone; nothing else picks a colour by hand, so a
/// status that gains a spelling gains it in exactly one place.
enum Tone: Equatable {
    case neutral, accent, pos, neg, warn

    var fg: Color {
        switch self {
        case .neutral: Theme.muted
        case .accent:  Theme.accent
        case .pos:     Theme.pos
        case .neg:     Theme.neg
        case .warn:    Theme.warn
        }
    }

    var soft: Color {
        switch self {
        case .neutral: Theme.surfaceSoft
        case .accent:  Theme.accentSoft
        case .pos:     Theme.posSoft
        case .neg:     Theme.negSoft
        case .warn:    Theme.warnSoft
        }
    }

    static func forStatus(_ status: String?) -> Tone {
        switch (status ?? "").lowercased() {
        case "running", "active", "live": .accent
        case "done", "complete", "completed", "ok": .pos
        case "blocked", "failed", "error", "stale": .neg
        case "queued", "planned", "pending", "waiting", "paused", "attention": .warn
        default: .neutral
        }
    }
}

extension Font {
    static let microLabel = Font.system(size: 10, weight: .semibold)
}

extension View {
    func microLabel(_ color: Color = Theme.faint) -> some View {
        font(.microLabel).tracking(0.6).foregroundStyle(color).textCase(.uppercase)
    }
}

/// Formatters shared by every native surface. Each refuses rather than guesses:
/// a value the board did not report prints an em dash, never a zero that would
/// read as a measurement.
enum Fmt {
    /// An ETA beyond this is a corrupt absolute timestamp stored as a duration,
    /// and must render as unknown rather than as a plausible enormous number.
    static let maxEtaSeconds: Double = 400 * 86_400

    static func eta(_ seconds: Double?) -> String {
        guard let seconds, seconds.isFinite, seconds > 0, seconds <= maxEtaSeconds else { return "—" }
        let total = Int(seconds.rounded())
        if total < 60 { return "\(total)s" }
        let minutes = total / 60
        if minutes < 60 { return "\(minutes)m" }
        let hours = minutes / 60
        let remainder = minutes % 60
        return remainder == 0 ? "\(hours)h" : "\(hours)h\(remainder)m"
    }

    static func pct(_ value: Double?) -> String {
        guard let value, value.isFinite else { return "—" }
        return "\(Int((value <= 1.0 ? value * 100 : value).rounded()))%"
    }

    static func fraction(_ value: Double?) -> Double {
        guard let value, value.isFinite else { return 0 }
        return min(max(value <= 1.0 ? value : value / 100.0, 0), 1)
    }

    static func age(_ date: Date?) -> String {
        guard let date else { return "—" }
        let seconds = Int(max(0, Date().timeIntervalSince(date)))
        if seconds < 3 { return "now" }
        if seconds < 60 { return "\(seconds)s" }
        if seconds < 3600 { return "\(seconds / 60)m" }
        return "\(seconds / 3600)h"
    }
}
