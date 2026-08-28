import AppKit

enum CockpitTokens {
    static let windowMinSize = NSSize(width: 980, height: 620)
    static let windowDefaultSize = NSSize(width: 1480, height: 860)
    static let toolbarCompactHeight: CGFloat = 60
    static let toolbarExpandedHeight: CGFloat = 96
    static let toolbarHeight: CGFloat = toolbarExpandedHeight
    static let topbarHeight: CGFloat = 58
    static let rowHeight: CGFloat = 48
    static let groupHeight: CGFloat = 45
    static let detailHeight: CGFloat = 132
    static let diagnosticsWidth: CGFloat = 420

    enum Color {
        static let bg = NSColor(calibratedRed: 0.000, green: 0.000, blue: 0.000, alpha: 1)
        static let panel = NSColor(calibratedRed: 0.027, green: 0.031, blue: 0.043, alpha: 1)
        static let panel2 = NSColor(calibratedRed: 0.043, green: 0.051, blue: 0.071, alpha: 1)
        static let line = NSColor(calibratedRed: 0.18, green: 0.21, blue: 0.27, alpha: 1)
        static let line2 = NSColor(calibratedRed: 0.26, green: 0.30, blue: 0.38, alpha: 1)
        static let text = NSColor(calibratedRed: 0.96, green: 0.97, blue: 0.98, alpha: 1)
        static let muted = NSColor(calibratedRed: 0.65, green: 0.67, blue: 0.72, alpha: 1)
        static let faint = NSColor(calibratedRed: 0.44, green: 0.46, blue: 0.51, alpha: 1)
        static let blue = NSColor(calibratedRed: 0.37, green: 0.62, blue: 1.00, alpha: 1)
        static let blue2 = NSColor(calibratedRed: 0.57, green: 0.73, blue: 1.00, alpha: 1)
        static let glowBlue = NSColor(calibratedRed: 0.18, green: 0.45, blue: 0.96, alpha: 1)
        static let selectionFill = NSColor(calibratedRed: 0.050, green: 0.064, blue: 0.086, alpha: 1)
        static let selectionStroke = NSColor(calibratedRed: 0.37, green: 0.62, blue: 1.00, alpha: 1)
        static let green = NSColor(calibratedRed: 0.20, green: 0.78, blue: 0.35, alpha: 1)
        static let amber = NSColor(calibratedRed: 0.96, green: 0.77, blue: 0.32, alpha: 1)
        static let red = NSColor(calibratedRed: 0.97, green: 0.44, blue: 0.44, alpha: 1)
        static let violet = NSColor(calibratedRed: 0.62, green: 0.50, blue: 0.95, alpha: 1)
    }

    static func statusColor(_ status: String) -> NSColor {
        let s = status.lowercased()
        if s.contains("run") || s.contains("active") { return Color.green }
        if s.contains("block") || s.contains("fail") || s.contains("error") { return Color.red }
        if s.contains("done") || s.contains("archiv") { return Color.faint }
        return Color.amber
    }

    static func ownerColor(_ owner: String?) -> NSColor {
        switch (owner ?? "").lowercased() {
        case "claude": return NSColor(calibratedRed: 0.85, green: 0.55, blue: 0.30, alpha: 1)
        case "codex": return Color.blue2
        case "mixed": return Color.violet
        case "local", "operator": return Color.green
        default: return Color.faint
        }
    }

    static func moduleColor(_ module: String?) -> NSColor {
        let key = (module ?? "").lowercased()
        guard !key.isEmpty else { return Color.blue2 }
        let palette: [NSColor] = [
            Color.violet,
            NSColor(calibratedRed: 0.34, green: 0.82, blue: 0.92, alpha: 1),
            NSColor(calibratedRed: 0.46, green: 0.80, blue: 0.53, alpha: 1),
            NSColor(calibratedRed: 0.95, green: 0.62, blue: 0.34, alpha: 1),
            Color.blue2,
        ]
        var hash: UInt64 = 5381
        for byte in key.utf8 { hash = (hash &* 33) &+ UInt64(byte) }
        return palette[Int(hash % UInt64(palette.count))]
    }
}
