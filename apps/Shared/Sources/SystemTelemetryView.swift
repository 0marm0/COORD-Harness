import SwiftUI

struct SystemTelemetryStrip: View {
    let snapshot: SystemTelemetrySnapshot?
    var expanded = false
    var showDisk = true
    var embedded = false

    private var metrics: [(String, String, Double?, Color)] {
        let values: [(String, String, Double?, Color)]
        if let snapshot {
            values = [
                ("RAM", "memorychip", snapshot.memory.availablePercent, .blue),
                ("GPU", "display", snapshot.gpu.availablePercent, .purple),
                ("CPU", "cpu", snapshot.cpu.availablePercent, .cyan),
                ("DISK", "internaldrive", snapshot.disk.availablePercent, .red),
            ]
        } else {
            values = [
                ("RAM", "memorychip", nil, .blue),
                ("GPU", "display", nil, .purple),
                ("CPU", "cpu", nil, .cyan),
                ("DISK", "internaldrive", nil, .red),
            ]
        }
        return values.filter { showDisk || $0.0 != "DISK" }
    }

    var body: some View {
        Group {
            if expanded {
                expandedCockpitStrip
            } else {
                compactStrip
            }
        }
    }

    private var expandedCockpitStrip: some View {
        HStack(spacing: 28) {
            ForEach(metrics, id: \.0) { metric in
                if metric.0 == "RAM" {
                    memoryRing(SystemTelemetrySnapshot.MemoryRingComposition.make(snapshot?.memory))
                } else {
                    metricRing(metric)
                }
            }
        }
        .frame(maxWidth: 620)
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .frame(maxWidth: .infinity, minHeight: 68, maxHeight: 68)
        .background {
            if !embedded {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(.ultraThinMaterial)
            }
        }
    }

    private func metricRing(_ metric: (String, String, Double?, Color)) -> some View {
        VStack(spacing: 2) {
            ZStack {
                Circle().stroke(Color.primary.opacity(0.10), lineWidth: 4)
                Circle()
                    .trim(from: 0, to: CGFloat(min(100, max(0, metric.2 ?? 0)) / 100))
                    .stroke(metric.3, style: StrokeStyle(lineWidth: 4, lineCap: .round))
                    .rotationEffect(.degrees(-90))
                Text(metric.2.map { "\(Int($0.rounded()))" } ?? "–")
                    .font(.system(size: 10.5, weight: .semibold, design: .rounded))
                    .monospacedDigit()
            }
            .frame(width: 46, height: 46)
            Text(metric.0)
                .font(.system(size: 7.5, weight: .semibold))
                .foregroundStyle(metric.3)
        }
        .frame(width: 62)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(metric.0) \(metric.2.map { "\(Int($0.rounded())) percent" } ?? "unavailable")")
    }

    private func memoryRing(_ composition: SystemTelemetrySnapshot.MemoryRingComposition) -> some View {
        VStack(spacing: 2) {
            ZStack {
                Circle().stroke(Color.primary.opacity(0.10), lineWidth: 4)
                if composition.segments.isEmpty, composition.usesFallbackArc {
                    Circle()
                        .trim(from: 0, to: CGFloat(min(100, max(0, composition.centerUsedPercent ?? 0)) / 100))
                        .stroke(Color.blue, style: StrokeStyle(lineWidth: 4, lineCap: .round))
                        .rotationEffect(.degrees(-90))
                } else {
                    ForEach(Array(composition.segments.enumerated()), id: \.offset) { index, segment in
                        Circle()
                            .trim(
                                from: CGFloat(composition.segments.prefix(index).reduce(0) { $0 + $1.fraction }),
                                to: CGFloat(composition.segments.prefix(index + 1).reduce(0) { $0 + $1.fraction })
                            )
                            .stroke(
                                memorySegmentColor(segment.kind),
                                style: StrokeStyle(lineWidth: 4, lineCap: .butt)
                            )
                            .rotationEffect(.degrees(-90))
                    }
                }
                Text(composition.centerUsedPercent.map { "\(Int($0.rounded()))" } ?? "–")
                    .font(.system(size: 10.5, weight: .semibold, design: .rounded))
                    .monospacedDigit()
            }
            .frame(width: 46, height: 46)
            Text("RAM")
                .font(.system(size: 7.5, weight: .semibold))
                .foregroundStyle(Color.blue)
        }
        .frame(width: 62)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(
            "RAM \(composition.centerUsedPercent.map { "\(Int($0.rounded())) percent used" } ?? "unavailable"); App, Wired, Compressed, and Free"
        )
    }

    private func memorySegmentColor(
        _ kind: SystemTelemetrySnapshot.MemoryRingComposition.SegmentKind
    ) -> Color {
        switch kind {
        case .app: return .blue
        case .wired: return .orange
        case .compressed: return .pink
        case .free: return Color.primary.opacity(0.36)
        }
    }

    private var compactStrip: some View {
        HStack(spacing: 8) {
            ForEach(metrics, id: \.0) { metric in
                HStack(spacing: 4) {
                    Text(metric.0)
                        .font(.system(size: 9, weight: .semibold))
                        .foregroundStyle(.secondary)
                    Text(percent(metric.2))
                        .font(.system(size: 11, weight: .bold).monospacedDigit())
                        .foregroundStyle(metric.2 == nil ? Color.secondary : metric.3)
                }
            }
            if !embedded { Spacer(minLength: 0) }
            freshness
        }
        .fixedSize(horizontal: true, vertical: false)
        .layoutPriority(4)
        .padding(.horizontal, embedded ? 0 : 11)
        .frame(height: embedded ? 38 : 30)
        .background {
            if !embedded {
                RoundedRectangle(cornerRadius: 9, style: .continuous)
                    .fill(.ultraThinMaterial)
            }
        }
    }

    @ViewBuilder private var freshness: some View {
        if snapshot?.isStale == true {
            Image(systemName: "clock.badge.exclamationmark")
                .foregroundStyle(.orange)
                .help("Showing the last system telemetry snapshot")
        }
    }

    private func percent(_ value: Double?) -> String {
        value.map { "\(Int($0.rounded()))%" } ?? "N/A"
    }
}
