import AppKit


enum Tokens {


    enum Layout {
        static let popoverWidth: CGFloat   = 404
        static let maxPopoverHeight: CGFloat = 820

        static let runningRowH: CGFloat = 46
        static let nextRowH: CGFloat    = 24
        static let attnRowH: CGFloat    = 28
        static let agentRowH: CGFloat   = 22

        static let rowPadL: CGFloat   = 31
        static let rowPadR: CGFloat   = 29
        static let topPad: CGFloat    = 8
        static let belowHeaderGap: CGFloat = 10
        static let sectionGap: CGFloat = 14
        static let groupGap: CGFloat   = 10
        static let serverToAgentGap: CGFloat = 2
        static let leadIconW: CGFloat = 16
        static let titleX: CGFloat    = rowPadL + leadIconW + 5
        static let expandTextX: CGFloat = titleX + 14

        static let hdrInset: CGFloat   = 3
        static let hdrNumGap: CGFloat  = 11
        static let hdrChvGap: CGFloat  = 6

        static let agentIconPt: CGFloat = 17
        // The COORD wordmark asset is 4096 x 927. Keep its true aspect ratio;
        // the former 296/88 ratio belonged to a different product asset.
        // The compact panel needs enough vertical mass for the raster mark to read as
        // a wordmark rather than a hairline, without crowding the control cluster.
        static let headerHeight: CGFloat = 44
        static let wordmarkH: CGFloat   = 28
        static let wordmarkW: CGFloat   = wordmarkH * (4096.0/927.0)
        static let headerControlsWidth: CGFloat = CoordPowerControlsLayout.width
        static let headerControlsHeight: CGFloat = CoordPowerControlsLayout.height

        static let footerHeight: CGFloat = 34
        static let footerToolRailWidth: CGFloat = 84
        static var telemetryRailWidth: CGFloat { popoverWidth - footerToolRailWidth }


        static let ringS: CGFloat      = 19.5
        static let ringLW: CGFloat     = 1.5
        static let ringScale: CGFloat  = 3.0
        static let statusImgH: CGFloat = 20
        static let statusFont: CGFloat = 11
        static let statusGap: CGFloat  = 3
        static let statusPad: CGFloat  = 1
    }


    enum Color {
        static func rgb(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat, _ a: CGFloat = 1) -> NSColor {
            NSColor(calibratedRed: r, green: g, blue: b, alpha: a)
        }
        static func gray(_ w: CGFloat, _ a: CGFloat = 1) -> NSColor {
            NSColor(calibratedWhite: w, alpha: a)
        }

        static let green          = rgb(0.204, 0.780, 0.349)
        static let greenDark      = rgb(0.137, 0.549, 0.243)


        static let progressBlue   = rgb(0.11, 0.30, 0.88)
        static let progressBlueLt = rgb(0.38, 0.62, 1.0)
        static let glowBlue       = rgb(0.20, 0.44, 1.0)
        static let red            = rgb(0.90, 0.27, 0.25)
        static let orange         = rgb(0.95, 0.62, 0.07)
        // Provider branding and resource severity are separate palette roles.
        // Claude keeps its warmer brand orange; warning is darker/redder between blue and critical red.
        static let claudeOrange = rgb(0.95, 0.47, 0.24)
        static let statsWarningOrange = rgb(0.90, 0.36, 0.16)
        static let yellow         = rgb(0.95, 0.82, 0.10)
        static let white          = NSColor.white
        static let lightGray      = rgb(0.85, 0.85, 0.87)
        static let dimGray        = rgb(0.55, 0.58, 0.62)
        static let sectionGray    = rgb(0.50, 0.52, 0.55)


        static let modeGreen  = green
        static let modeOrange = rgb(0.760, 0.439, 0.110)
        static let modeRed    = rgb(0.698, 0.227, 0.180)


        static let gpuApple   = rgb(0.86, 0.87, 0.92)
    }


    static func ownerTint(_ k: OwnerKind) -> NSColor {
        switch k {
        case .claude:   return Color.claudeOrange
        case .codex:    return Color.rgb(0.36, 0.62, 0.92)
        case .mixed:    return Color.rgb(0.66, 0.55, 0.90)
        case .local:    return Color.rgb(0.55, 0.58, 0.62)
        case .operatorUser: return Color.rgb(0.42, 0.80, 0.58)
        }
    }

    static func modeTint(_ mode: String) -> NSColor {
        switch mode.lowercased() {
        case "light":  return Color.modeGreen
        case "medium": return Color.modeOrange
        case "full":   return Color.modeRed
        default:       return Color.gray(0.56)
        }
    }
}
