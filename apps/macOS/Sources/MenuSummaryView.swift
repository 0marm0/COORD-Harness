import SwiftUI

struct MenuSummaryView: View {
    @ObservedObject var model: CockpitModel
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: DesignTokens.sectionSpacing) {
            HStack {
                Label("Coord Cockpit", systemImage: "gauge.with.dots.needle.67percent")
                    .font(.headline)
                Spacer()
                HealthLabel(health: model.health)
            }

            if let snapshot = model.snapshot {
                HStack {
                    summaryItem(snapshot.summary.running, "Running")
                    summaryItem(snapshot.summary.attention, "Attention")
                    summaryItem(snapshot.summary.next, "Next")
                    summaryItem(snapshot.summary.done, "Done")
                }
                if snapshot.stale || model.isShowingLastGood {
                    Label("Showing last-good or stale data", systemImage: "clock.badge.exclamationmark")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }
                Divider()
                ForEach(snapshot.rows.prefix(4)) { row in
                    HStack {
                        Text(row.title).lineLimit(1)
                        Spacer()
                        StatusBadge(status: row.status)
                    }
                }
            } else {
                EmptySnapshotView(message: "Open the Cockpit to configure an endpoint.")
                    .frame(height: 150)
            }

            Divider()
            HStack {
                Button("Open Cockpit") { openWindow(id: "cockpit") }
                Spacer()
                Button {
                    Task { await model.refresh() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .disabled(model.isRefreshing)
            }
        }
        .padding()
        .frame(width: 390)
        .task { model.startPolling() }
    }

    private func summaryItem(_ value: Int, _ label: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value, format: .number).font(.title3.bold()).monospacedDigit()
            Text(label).font(.caption).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
