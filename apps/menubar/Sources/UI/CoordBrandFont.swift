#if canImport(AppKit)
import AppKit

/// The wordmark face, resolved the same way the web board resolves it.
///
/// The desktop app drew COORD in letter-spaced system font while the board
/// drew it in the brand face, so one product showed two logos. Both surfaces
/// now ask for the SAME locally installed family and fall back identically.
///
/// The face is deliberately NOT bundled. `docs`/`.gitignore` already record
/// the rule for the web side: font licences do not permit redistribution, so
/// the repository ships no font file and asks the machine for one it may
/// already have. An operator who has licensed the face installs it once and
/// both the board and this app pick it up; everyone else gets the fallback,
/// and the letterspacing is what makes the mark read as a mark either way.
enum CoordBrandFont {
    /// Names are tried in the same order as the `local(...)` list in app.css.
    private static let candidates = [
        "TWK Lausanne Pan 200",
        "TWKLausannePan-200",
        "Lausanne 200",
        "Lausanne Thin",
        "Lausanne",
    ]

    static func wordmark(size: CGFloat) -> NSFont {
        for name in candidates {
            if let font = NSFont(name: name, size: size) { return font }
        }
        return NSFont.systemFont(ofSize: size, weight: .light)
    }
}
#endif
