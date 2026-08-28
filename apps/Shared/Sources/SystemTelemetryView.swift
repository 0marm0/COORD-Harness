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
                ("CPU", "cpu", snapshot.cpu.availablePercent, .blue),
                ("GPU", "display", snapshot.gpu.availablePercent, .orange),
                ("RAM", "memorychip", snapshot.memory.availablePercent, .red),
                ("DISK", "internaldrive", snapshot.disk.availablePercent, .cyan),
            ]
        } else {
            values = [("CPU", "cpu", nil, .blue), ("GPU", "display", nil, .orange),
                      ("RAM", "memorychip", nil, .red), ("DISK", "internaldrive", nil, .cyan)]
        }
        return values.filter { showDisk || $0.0 != "DISK" }
    }

    var body: some View {
        if expanded {
            VStack(alignment: .leading, spacing: 9) {
                HStack {
                    Label("SYSTEM", systemImage: "chart.bar.fill")
                        .font(.system(size: 10, weight: .bold))
                        .tracking(1)
                    Spacer()
                    freshness
                }
                ForEach(metrics, id: \.0) { metric in
                    HStack(spacing: 9) {
                        Label(metric.0, systemImage: metric.1)
                            .font(.caption.weight(.semibold))
                            .frame(width: 66, alignment: .leading)
                        ProgressView(value: metric.2 ?? 0, total: 100)
                            .tint(metric.2 == nil ? Color.secondary.opacity(0.28) : metric.3)
                        Text(percent(metric.2))
                            .font(.caption.weight(.bold).monospacedDigit())
                            .foregroundStyle(metric.2 == nil ? Color.secondary : metric.3)
                            .frame(width: 38, alignment: .trailing)
                    }
                }
                if let snapshot {
                    HStack(spacing: 14) {
                        Text("Swap \(bytes(snapshot.memory.swapUsedBytes))")
                        if showDisk {
                            Text("Disk free \(bytes(snapshot.disk.freeBytes))")
                            Text("R \(rate(snapshot.disk.readBps)) · W \(rate(snapshot.disk.writeBps))")
                        }
                    }
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
                }
            }
            .padding(embedded ? 0 : 11)
            .background {
                if !embedded {
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .fill(.ultraThinMaterial)
                }
            }
        } else {
            HStack(spacing: 12) {
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
            .padding(.horizontal, embedded ? 0 : 11)
            .frame(height: embedded ? 38 : 30)
            .background {
                if !embedded {
                    RoundedRectangle(cornerRadius: 9, style: .continuous)
                        .fill(.ultraThinMaterial)
                }
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

    private func bytes(_ value: Int64?) -> String {
        guard let value else { return "N/A" }
        return ByteCountFormatter.string(fromByteCount: value, countStyle: .memory)
    }

    private func rate(_ value: Double?) -> String {
        guard let value, value.isFinite else { return "N/A" }
        return ByteCountFormatter.string(fromByteCount: Int64(max(0, value)), countStyle: .file) + "/s"
    }
}
