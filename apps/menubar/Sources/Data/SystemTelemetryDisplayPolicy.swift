import Foundation

/// Pure geometry shared by the persistent status renderer and deterministic
/// tests. Compact values match COORD.s compact image-only status-item layout.
enum StatusItemImageLayout {
    static let height: CGFloat = 22
    static let compactTelemetryLeadingGap: CGFloat = 8
    static let regularTelemetryLeadingGap: CGFloat = 14
    static let compactTelemetryModuleWidth: CGFloat = 30
    static let regularTelemetryModuleWidth: CGFloat = 34
    static let compactTelemetryModuleSpacing: CGFloat = 4
    static let regularTelemetryModuleSpacing: CGFloat = 8

    static func telemetryLeadingGap(compact: Bool) -> CGFloat {
        compact ? compactTelemetryLeadingGap : regularTelemetryLeadingGap
    }

    static func telemetryModuleWidth(compact: Bool) -> CGFloat {
        compact ? compactTelemetryModuleWidth : regularTelemetryModuleWidth
    }

    static func telemetryModuleSpacing(compact: Bool) -> CGFloat {
        compact ? compactTelemetryModuleSpacing : regularTelemetryModuleSpacing
    }

    static func telemetryTotalWidth(moduleCount: Int, compact: Bool) -> CGFloat {
        guard moduleCount > 0 else { return 0 }
        return telemetryLeadingGap(compact: compact)
            + CGFloat(moduleCount) * telemetryModuleWidth(compact: compact)
            + CGFloat(moduleCount - 1) * telemetryModuleSpacing(compact: compact)
    }

    static func imageWidth(baseWidth: CGFloat, moduleCount: Int, compact: Bool) -> CGFloat {
        baseWidth + telemetryTotalWidth(moduleCount: moduleCount, compact: compact)
    }

    static func telemetryFrame(
        index: Int,
        baseWidth: CGFloat,
        compact: Bool
    ) -> CGRect {
        let moduleWidth = telemetryModuleWidth(compact: compact)
        let x = baseWidth
            + telemetryLeadingGap(compact: compact)
            + CGFloat(index) * (moduleWidth + telemetryModuleSpacing(compact: compact))
        return CGRect(x: x, y: 1, width: moduleWidth, height: height - 2)
    }

    static func telemetryLabelFrame(in module: CGRect) -> CGRect {
        CGRect(x: module.minX, y: 12, width: module.width, height: 8)
    }

    static func telemetryValueFrame(in module: CGRect) -> CGRect {
        CGRect(x: module.minX, y: 1, width: module.width, height: 12)
    }
}

enum SystemTelemetrySeverity: Equatable {
    case unavailable
    case normal
    case warning
    case critical
}

struct SystemTelemetryDisplayPolicy {
    static let defaultWarningThreshold = 70.0
    static let defaultCriticalThreshold = 90.0

    let warningThreshold: Double
    let criticalThreshold: Double

    init(warningThreshold: Double, criticalThreshold: Double) {
        let warning = min(100, max(0, warningThreshold))
        let critical = min(100, max(warning, criticalThreshold))
        self.warningThreshold = warning
        self.criticalThreshold = critical
    }

    func severity(for usagePercent: Double?) -> SystemTelemetrySeverity {
        guard let usagePercent, usagePercent.isFinite else { return .unavailable }
        let value = min(100, max(0, usagePercent))
        if value >= criticalThreshold { return .critical }
        if value >= warningThreshold { return .warning }
        return .normal
    }
}

enum SystemTelemetryDetailFormatter {
    static func percent(_ value: Double?) -> String {
        guard let value, value.isFinite else { return "N/A" }
        return "\(Int(min(100, max(0, value)).rounded()))%"
    }

    static func bytes(_ value: Int64?) -> String {
        guard let value, value >= 0 else { return "—" }
        return byteValue(Double(value))
    }

    static func rate(_ value: Double?) -> String {
        guard let value, value.isFinite, value >= 0 else { return "—" }
        return "\(byteValue(value))/s"
    }

    static func age(_ value: Double?) -> String {
        guard let value, value.isFinite, value >= 0 else { return "age unavailable" }
        if value < 10 { return String(format: "%.1fs ago", value) }
        if value < 60 { return "\(Int(value.rounded()))s ago" }
        return "\(Int((value / 60).rounded()))m ago"
    }

    static func interval(_ value: Double?) -> String {
        guard let value, value.isFinite, value > 0 else { return "on demand" }
        if value < 10 { return String(format: "%.1fs", value) }
        return "\(Int(value.rounded()))s"
    }

    private static func byteValue(_ raw: Double) -> String {
        let units = ["B", "KB", "MB", "GB", "TB", "PB"]
        var value = raw
        var unit = 0
        while value >= 1024, unit < units.count - 1 {
            value /= 1024
            unit += 1
        }
        if unit == 0 { return "\(Int(value.rounded())) \(units[unit])" }
        let precision = value < 10 ? 1 : 0
        return String(format: "%.*f %@", precision, value, units[unit])
    }
}
