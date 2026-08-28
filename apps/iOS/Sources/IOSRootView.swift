import SwiftUI

struct IOSRootView: View {
    @ObservedObject var model: CockpitModel

    var body: some View {
        TabView {
            NavigationStack { IOSHomeView(model: model) }
                .tabItem { Label("Board", systemImage: "square.stack.3d.up") }
            NavigationStack { IOSAreaScreen(title: "Mesh", path: "/mesh", model: model) }
                .tabItem { Label("Mesh", systemImage: "point.3.connected.trianglepath.dotted") }
            NavigationStack { IOSAreaScreen(title: "Map", path: "/map", model: model) }
                .tabItem { Label("Map", systemImage: "square.grid.3x3") }
            NavigationStack { IOSAreaScreen(title: "Atlas", path: "/ops", model: model) }
                .tabItem { Label("Atlas", systemImage: "chart.dots.scatter") }
            NavigationStack { UsageDashboardView(model: model) }
                .tabItem { Label("Usage", systemImage: "gauge.with.dots.needle.33percent") }
            NavigationStack { IOSJobsView(model: model) }
                .tabItem { Label("Jobs", systemImage: "cpu") }
            NavigationStack { IOSSettingsView(model: model) }
                .tabItem { Label("Settings", systemImage: "slider.horizontal.3") }
        }
        .tint(Theme.accent)
        .preferredColorScheme(.dark)
    }
}

/// Every screen sits on the canvas and hides the system's own scroll background,
/// otherwise the list paints its default ground over the glow.
private struct CanvasScreen<Content: View>: View {
    @ViewBuilder var content: () -> Content

    var body: some View {
        ZStack {
            CoordCanvas()
            content()
        }
        .toolbarBackground(.hidden, for: .navigationBar)
        .scrollContentBackground(.hidden)
    }
}

private struct IOSHomeView: View {
    @ObservedObject var model: CockpitModel

    private var running: [NativeSnapshotV1.Row] {
        (model.snapshot?.rows ?? []).filter { Tone.forStatus($0.status) == .accent }
    }

    private var attention: [NativeSnapshotV1.Row] {
        (model.snapshot?.rows ?? []).filter { ["attention", "blocked", "failed"].contains($0.status.lowercased()) }
    }

    var body: some View {
        CanvasScreen {
            ScrollView {
                VStack(alignment: .leading, spacing: Space.xl) {
                    if let snapshot = model.snapshot {
                        if snapshot.stale || model.isShowingLastGood {
                            StaleNotice()
                        }
                        MetricGrid(summary: snapshot.summary)
                        RowSection(
                            title: "Running now",
                            empty: "Nothing holds a live claim on this board.",
                            rows: running
                        )
                        RowSection(
                            title: "Needs attention",
                            empty: "No row is blocked, failed, or flagged.",
                            rows: attention
                        )
                        Text("Read-only projection of NativeSnapshotV1. This client never writes to the board.")
                            .font(.caption2)
                            .foregroundStyle(Theme.faint)
                    } else {
                        EmptySnapshotView(message: "Set an endpoint in Settings, then pull to refresh.")
                            .frame(minHeight: 380)
                    }
                }
                .padding(Space.lg)
            }
        }
        .navigationTitle("")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .principal) { CoordWordmark(size: 15) }
        }
        .refreshable { await model.refresh() }
    }
}

private struct StaleNotice: View {
    var body: some View {
        HStack(spacing: Space.sm) {
            Image(systemName: "clock.badge.exclamationmark")
            Text("Showing stale or last-good data").font(.footnote)
        }
        .foregroundStyle(Theme.warn)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(Space.md)
        .panelSurface(cornerRadius: Theme.radiusSmall, tint: Theme.warnSoft)
    }
}

private struct MetricGrid: View {
    let summary: NativeSnapshotV1.Summary

    var body: some View {
        LazyVGrid(columns: [GridItem(.flexible(), spacing: Space.md), GridItem(.flexible(), spacing: Space.md)], spacing: Space.md) {
            IOSMetric(label: "Running", value: summary.running, tone: .accent)
            IOSMetric(label: "Attention", value: summary.attention, tone: .warn)
            IOSMetric(label: "Next", value: summary.next, tone: .neutral)
            IOSMetric(label: "Done", value: summary.done, tone: .pos)
        }
    }
}

private struct RowSection: View {
    let title: String
    let empty: String
    let rows: [NativeSnapshotV1.Row]

    var body: some View {
        VStack(alignment: .leading, spacing: Space.sm) {
            HStack(alignment: .firstTextBaseline) {
                Text(title).microLabel(Theme.muted)
                Spacer()
                if !rows.isEmpty {
                    Text("\(rows.count)")
                        .font(.system(size: 10, weight: .semibold).monospacedDigit())
                        .foregroundStyle(Theme.faint)
                }
            }
            if rows.isEmpty {
                Text(empty)
                    .font(.footnote)
                    .foregroundStyle(Theme.faint)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(Space.md)
                    .panelSurface(cornerRadius: Theme.radiusSmall)
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(rows.prefix(8).enumerated()), id: \.element.id) { index, row in
                        if index > 0 { Divider().overlay(Theme.hairlineColor) }
                        IOSRow(row: row).padding(.vertical, Space.md)
                    }
                }
                .padding(.horizontal, Space.md)
                .panelSurface(cornerRadius: Theme.radiusCard)
            }
        }
    }
}

private struct IOSJobsView: View {
    @ObservedObject var model: CockpitModel

    private var jobs: [NativeSnapshotV1.Row] {
        model.filteredRows.filter { $0.bucket.localizedCaseInsensitiveContains("job") }
    }

    var body: some View {
        CanvasScreen {
            if jobs.isEmpty {
                EmptySnapshotView(message: "No job rows are available.")
            } else {
                ScrollView {
                    VStack(spacing: 0) {
                        ForEach(Array(jobs.enumerated()), id: \.element.id) { index, row in
                            if index > 0 { Divider().overlay(Theme.hairlineColor) }
                            IOSRow(row: row).padding(.vertical, Space.md)
                        }
                    }
                    .padding(.horizontal, Space.md)
                    .panelSurface(cornerRadius: Theme.radiusCard)
                    .padding(Space.lg)
                }
                .searchable(text: $model.searchText, prompt: "Filter jobs")
            }
        }
        .navigationTitle("Jobs")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await model.refresh() }
    }
}

private struct IOSSettingsView: View {
    @ObservedObject var model: CockpitModel

    var body: some View {
        CanvasScreen {
            Form {
                Section {
                    LabeledContent("Base URL") {
                        TextField("https://host", text: $model.baseURLText)
                            .multilineTextAlignment(.trailing)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .keyboardType(.URL)
                    }
                    Button("Apply and refresh") { model.applyEndpoint() }
                        .tint(Theme.accent)
                } header: {
                    Text("Endpoint").microLabel(Theme.muted)
                } footer: {
                    Text("This client performs read-only GET requests to /api/v1/snapshot and /healthz.")
                }
                Section {
                    LabeledContent("State") {
                        switch model.health {
                        case .unknown:
                            Label("Unknown", systemImage: "questionmark.circle").foregroundStyle(Theme.muted)
                        case .healthy:
                            Label("Healthy", systemImage: "checkmark.circle.fill").foregroundStyle(Theme.pos)
                        case let .unavailable(message):
                            VStack(alignment: .trailing, spacing: 3) {
                                Label("Unavailable", systemImage: "xmark.circle.fill").foregroundStyle(Theme.neg)
                                Text(message)
                                    .font(.caption)
                                    .foregroundStyle(Theme.faint)
                                    .multilineTextAlignment(.trailing)
                            }
                            .accessibilityElement(children: .combine)
                        }
                    }
                    LabeledContent("Snapshot") { Text(model.snapshotStateLabel).foregroundStyle(Theme.muted) }
                    if let cacheIssue = model.cacheIssue {
                        LabeledContent("Cache") {
                            Text(cacheIssue)
                                .font(.caption)
                                .foregroundStyle(Theme.warn)
                                .multilineTextAlignment(.trailing)
                        }
                        .accessibilityElement(children: .combine)
                    }
                } header: {
                    Text("Connection").microLabel(Theme.muted)
                }
            }
        }
        .navigationTitle("Settings")
        .navigationBarTitleDisplayMode(.inline)
    }
}

private struct IOSMetric: View {
    let label: String
    let value: Int
    let tone: Tone

    var body: some View {
        VStack(alignment: .leading, spacing: Space.sm) {
            HStack(spacing: Space.xs + 2) {
                Circle().fill(tone.fg).frame(width: 6, height: 6)
                Text(label).microLabel(Theme.muted)
            }
            Text(value, format: .number)
                .font(.system(size: 30, weight: .light).monospacedDigit())
                .foregroundStyle(Theme.textHi)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, Space.lg)
        .padding(.vertical, Space.md + 2)
        .panelSurface(cornerRadius: Theme.radiusCard)
    }
}

private struct IOSRow: View {
    let row: NativeSnapshotV1.Row

    private var tone: Tone { Tone.forStatus(row.status) }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.sm) {
            HStack(alignment: .top, spacing: Space.sm) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(row.title)
                        .font(.system(size: 15, weight: .medium))
                        .foregroundStyle(Theme.textHi)
                        .lineLimit(2)
                    Text(row.id)
                        .font(.system(size: 11, weight: .regular).monospacedDigit())
                        .foregroundStyle(Theme.faint)
                }
                Spacer(minLength: Space.sm)
                Text(row.status.lowercased())
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(tone.fg)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 3)
                    .background(tone.soft, in: Capsule())
            }
            if !row.currentStep.isEmpty {
                Text(row.currentStep)
                    .font(.system(size: 13))
                    .foregroundStyle(Theme.muted)
                    .lineLimit(2)
            }
            HStack(spacing: Space.sm) {
                OwnerMark(lane: OwnerLane.parse(row.owner), size: 14)
                Text(row.owner.isEmpty ? "unassigned" : row.owner)
                    .font(.system(size: 11))
                    .foregroundStyle(Theme.faint)
                Spacer()
                if let progress = row.progressFraction {
                    Text(Fmt.pct(progress))
                        .font(.system(size: 11, weight: .medium).monospacedDigit())
                        .foregroundStyle(Theme.muted)
                }
            }
            if let progress = row.progressFraction {
                ProgressBar(fraction: progress, tone: tone)
            }
        }
    }
}
