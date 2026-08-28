import SwiftUI
import WebKit

private enum CockpitSection: String, CaseIterable, Identifiable {
    case overview = "Overview"
    case work = "Work"
    case jobs = "Jobs"
    case graph = "Graph"
    case activity = "Activity & Health"
    case comms = "Comms"
    case usage = "Usage"

    var id: Self { self }
    var symbol: String {
        switch self {
        case .overview: "rectangle.3.group"
        case .work: "list.bullet.rectangle"
        case .jobs: "gearshape.2"
        case .graph: "point.3.connected.trianglepath.dotted"
        case .activity: "waveform.path.ecg"
        case .comms: "bubble.left.and.bubble.right"
        case .usage: "gauge.with.dots.needle.33percent"
        }
    }
}

struct MacCockpitView: View {
    @ObservedObject var model: CockpitModel
    @State private var selection: CockpitSection? = .overview
    @StateObject private var commsSurface = MacCommsSurfaceStore()

    var body: some View {
        NavigationSplitView {
            List(CockpitSection.allCases, selection: $selection) { section in
                Label(section.rawValue, systemImage: section.symbol)
            }
            .navigationTitle("Coord Cockpit")
        } detail: {
            Group {
                switch selection ?? .overview {
                case .overview: OverviewView(model: model, onOpenUsage: { selection = .usage })
                case .work: WorkListView(model: model, jobsOnly: false)
                case .jobs: WorkListView(model: model, jobsOnly: true)
                case .graph: GraphSummaryView(model: model)
                case .activity: ActivityHealthView(model: model)
                case .comms: MacCommsView(store: commsSurface, baseURLText: model.baseURLText)
                case .usage: UsageDashboardView(model: model)
                }
            }
            .toolbar {
                ToolbarItem {
                    Button {
                        Task { await model.refresh() }
                    } label: {
                        Label("Refresh", systemImage: "arrow.clockwise")
                    }
                    .disabled(model.isRefreshing)
                }
            }
        }
    }
}

/// The standalone macOS Cockpit mounts the same complete web Comms surface as
/// the menu-bar Cockpit. The retained store keeps one WKWebView alive while an
/// operator moves between native sections, so snapshot polling does not reset
/// scroll position or rebuild the Fleet/Comms/Pulse workspace.
final class MacCommsSurfaceStore: ObservableObject {
    let webView: WKWebView
    private var requestedURL: URL?

    init() {
        let configuration = WKWebViewConfiguration()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        configuration.websiteDataStore = .default()
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.allowsBackForwardNavigationGestures = false
        webView.setValue(false, forKey: "drawsBackground")
    }

    func loadIfNeeded(baseURLText: String) {
        guard let target = MacCommsView.targetURL(baseURLText: baseURLText), target != requestedURL else {
            return
        }
        requestedURL = target
        webView.load(URLRequest(
            url: target,
            cachePolicy: .reloadIgnoringLocalAndRemoteCacheData,
            timeoutInterval: 45
        ))
    }
}

struct MacCommsView: NSViewRepresentable {
    static let unifiedRoute = "/?embedded=1#v=comms"

    let store: MacCommsSurfaceStore
    let baseURLText: String

    func makeNSView(context: Context) -> WKWebView {
        store.webView
    }

    func updateNSView(_ webView: WKWebView, context: Context) {
        store.loadIfNeeded(baseURLText: baseURLText)
    }

    static func targetURL(baseURLText: String) -> URL? {
        let trimmed = baseURLText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let baseURL = URL(string: trimmed),
              let scheme = baseURL.scheme?.lowercased(),
              ["http", "https"].contains(scheme),
              baseURL.host != nil else {
            return nil
        }
        return URL(string: unifiedRoute, relativeTo: baseURL)?.absoluteURL
    }
}

private struct OverviewView: View {
    @ObservedObject var model: CockpitModel
    let onOpenUsage: () -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: DesignTokens.sectionSpacing) {
                PageHeader(title: "Overview", model: model)
                UsageCompactBoardStrip(
                    state: model.usageState,
                    systemTelemetry: model.systemTelemetry,
                    showSystemTelemetry: UserDefaults.standard.object(
                        forKey: "coord.cockpit.system-telemetry-visible"
                    ) as? Bool ?? true,
                    onOpenDetails: onOpenUsage
                )
                if let snapshot = model.snapshot {
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 140))], spacing: 12) {
                        MetricCard(label: "Running", value: snapshot.summary.running, symbol: "play.circle")
                        MetricCard(label: "Attention", value: snapshot.summary.attention, symbol: "exclamationmark.triangle")
                        MetricCard(label: "Next", value: snapshot.summary.next, symbol: "arrow.right.circle")
                        MetricCard(label: "Done", value: snapshot.summary.done, symbol: "checkmark.circle")
                        MetricCard(label: "Total", value: snapshot.summary.total, symbol: "sum")
                    }
                    GroupBox("Current work") {
                        VStack(spacing: 0) {
                            ForEach(snapshot.rows.prefix(6)) { row in
                                CompactRow(row: row)
                                if row.id != snapshot.rows.prefix(6).last?.id { Divider() }
                            }
                        }
                    }
                } else {
                    EmptySnapshotView(message: "Check the endpoint, then refresh.")
                        .frame(maxWidth: .infinity, minHeight: 300)
                }
            }
            .padding(DesignTokens.pagePadding)
        }
        .navigationTitle("Overview")
    }
}

private struct WorkListView: View {
    @ObservedObject var model: CockpitModel
    let jobsOnly: Bool

    private var rows: [NativeSnapshotV1.Row] {
        jobsOnly ? model.filteredRows.filter { $0.bucket.localizedCaseInsensitiveContains("job") } : model.filteredRows
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                TextField("Filter work", text: $model.searchText)
                    .textFieldStyle(.roundedBorder)
                Picker("Status", selection: $model.selectedStatus) {
                    ForEach(model.statuses, id: \.self) { Text($0).tag($0) }
                }
                .frame(width: 180)
            }
            .padding()
            Divider()
            if rows.isEmpty {
                EmptySnapshotView(message: jobsOnly ? "No job rows match the current filter." : "No work rows match the current filter.")
            } else {
                List(rows) { row in DetailedRow(row: row) }
            }
        }
        .navigationTitle(jobsOnly ? "Jobs" : "Work")
    }
}

private struct GraphSummaryView: View {
    @ObservedObject var model: CockpitModel

    private var buckets: [(String, Int)] {
        let grouped = Dictionary(grouping: model.snapshot?.rows ?? [], by: \.bucket)
        return grouped.map { ($0.key, $0.value.count) }.sorted { $0.0 < $1.0 }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: DesignTokens.sectionSpacing) {
                Text("Work by bucket").font(.title2.bold())
                if buckets.isEmpty {
                    EmptySnapshotView(message: "Bucket relationships appear when a snapshot is available.")
                } else {
                    ForEach(buckets, id: \.0) { bucket, count in
                        HStack {
                            Image(systemName: "circle.grid.cross")
                            Text(bucket.isEmpty ? "Unbucketed" : bucket)
                            Spacer()
                            Text(count, format: .number).monospacedDigit().foregroundStyle(.secondary)
                        }
                        .padding()
                        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: DesignTokens.cornerRadius))
                    }
                }
            }
            .padding(DesignTokens.pagePadding)
        }
        .navigationTitle("Graph")
    }
}

private struct ActivityHealthView: View {
    @ObservedObject var model: CockpitModel

    var body: some View {
        Form {
            Section("Connection") {
                TextField("Base URL", text: $model.baseURLText)
                Button("Apply endpoint") { model.applyEndpoint() }
                LabeledContent("Health") { HealthLabel(health: model.health) }
            }
            Section("Snapshot") {
                LabeledContent("Source", value: model.snapshot?.source ?? "Unavailable")
                LabeledContent("Generated") {
                    if let date = model.snapshot?.generatedAt {
                        Text(date, format: .dateTime.year().month().day().hour().minute().second())
                    } else { Text("Unavailable") }
                }
                LabeledContent("Freshness", value: model.snapshotStateLabel)
                LabeledContent("Sessions", value: String(model.snapshot?.sessions?.filter(\.live).count ?? 0))
                if let cacheIssue = model.cacheIssue {
                    LabeledContent("Cache") {
                        Text(cacheIssue).foregroundStyle(.orange)
                    }
                }
            }
        }
        .formStyle(.grouped)
        .navigationTitle("Activity & Health")
    }
}

private struct PageHeader: View {
    let title: String
    @ObservedObject var model: CockpitModel

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(title).font(.largeTitle.bold())
            Spacer()
            HealthLabel(health: model.health)
            if model.isShowingLastGood { StatusBadge(status: "Last good") }
            if model.snapshot?.stale == true { StatusBadge(status: "Stale") }
        }
    }
}

private struct MetricCard: View {
    let label: String
    let value: Int
    let symbol: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(label, systemImage: symbol).foregroundStyle(.secondary)
            Text(value, format: .number).font(.system(size: 28, weight: .bold, design: .rounded)).monospacedDigit()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: DesignTokens.cornerRadius))
    }
}

private struct CompactRow: View {
    let row: NativeSnapshotV1.Row

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: row.stale ? "clock.badge.exclamationmark" : "circle.fill")
                .foregroundStyle(row.stale ? .orange : DesignTokens.statusColor(for: row.status))
                .imageScale(.small)
            VStack(alignment: .leading) {
                Text(row.title).lineLimit(1)
                Text(row.currentStep).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer()
            StatusBadge(status: row.status)
        }
        .padding(.vertical, 9)
    }
}

private struct DetailedRow: View {
    let row: NativeSnapshotV1.Row

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text(row.title).font(.headline)
                Spacer()
                StatusBadge(status: row.status)
            }
            Text(row.currentStep).foregroundStyle(.secondary).lineLimit(2)
            HStack(spacing: 14) {
                Label(row.owner, systemImage: "person")
                Label(row.module, systemImage: "shippingbox")
                Label(row.group, systemImage: "square.stack.3d.up")
                Spacer()
                if let progress = row.progressFraction {
                    ProgressView(value: progress).frame(width: 100)
                    Text(progress, format: .percent.precision(.fractionLength(0))).monospacedDigit()
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
        .padding(.vertical, 5)
    }
}

struct HealthLabel: View {
    let health: CockpitModel.Health

    var body: some View {
        switch health {
        case .unknown:
            Label("Unknown", systemImage: "questionmark.circle").foregroundStyle(.secondary)
        case .healthy:
            Label("Healthy", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
        case let .unavailable(message):
            Label("Unavailable", systemImage: "xmark.circle.fill")
                .foregroundStyle(.red)
                .help(message)
        }
    }
}
