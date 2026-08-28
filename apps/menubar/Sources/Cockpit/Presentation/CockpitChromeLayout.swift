import CoreGraphics

struct CockpitTopBarLayout {
    var surfaceIndices: [Int]
    var surfaces: [CGRect]
    var stats: CGRect
    var mode: CGRect
    var resume: CGRect?
    var pause: CGRect?

    static func compute(width: CGFloat, surfaceWidths: [CGFloat], showsResume: Bool, showsPause: Bool) -> CockpitTopBarLayout {
        let rightMargin: CGFloat = 18
        let buttonWidth: CGFloat = 36
        let buttonY: CGFloat = 13
        let buttonHeight: CGFloat = 32
        let buttonGap: CGFloat = 7
        let minSurfaceX: CGFloat = 214

        var right = width - rightMargin
        var pause: CGRect?
        var resume: CGRect?

        if showsPause {
            pause = CGRect(x: right - buttonWidth, y: buttonY, width: buttonWidth, height: buttonHeight)
            right = (pause?.minX ?? right) - buttonGap
        }
        if showsResume {
            resume = CGRect(x: right - buttonWidth, y: buttonY, width: buttonWidth, height: buttonHeight)
            right = (resume?.minX ?? right) - 12
        }

        let mode = CGRect(x: right - 112, y: buttonY, width: 112, height: buttonHeight)
        right = mode.minX - 4

        let statsWidth: CGFloat = width < 1_180 ? 238 : 300
        let stats = CGRect(x: right - statsWidth, y: buttonY + 7, width: statsWidth, height: 18)
        right = stats.minX - 44

        let navGap: CGFloat = 6
        let availableWidth = max(0, right - minSurfaceX)
        var surfaceIndices = Array(surfaceWidths.indices)
        func totalWidth(_ indices: [Int]) -> CGFloat {
            indices.reduce(CGFloat(0)) { $0 + surfaceWidths[$1] }
                + CGFloat(max(0, indices.count - 1)) * navGap
        }

        // Jobs and More are the compact-width safety rail. More repeats every
        // route family, so no destination disappears when the window narrows.
        if totalWidth(surfaceIndices) > availableWidth, surfaceWidths.count > 1 {
            surfaceIndices = [0, surfaceWidths.count - 1]
            for index in surfaceWidths.indices.dropFirst().dropLast() {
                let candidate = Array(surfaceIndices.dropLast()) + [index, surfaceWidths.count - 1]
                if totalWidth(candidate) <= availableWidth {
                    surfaceIndices = candidate
                }
            }
        }
        if totalWidth(surfaceIndices) > availableWidth {
            surfaceIndices = surfaceWidths.isEmpty ? [] : [surfaceWidths.count - 1]
        }

        var surfaces: [CGRect] = []
        var x = minSurfaceX
        for index in surfaceIndices {
            let surfaceWidth = surfaceWidths[index]
            surfaces.append(CGRect(x: x, y: buttonY, width: surfaceWidth, height: buttonHeight))
            x += surfaceWidth + navGap
        }

        return CockpitTopBarLayout(
            surfaceIndices: surfaceIndices,
            surfaces: surfaces,
            stats: stats,
            mode: mode,
            resume: resume,
            pause: pause
        )
    }
}
