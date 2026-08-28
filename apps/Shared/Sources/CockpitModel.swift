import Combine
import Foundation

@MainActor
final class CockpitModel: ObservableObject {
    static let defaultBaseURL = "http://127.0.0.1:7870"
    static let persistedBaseURLKey = "coordharness.baseURL"

    enum Health: Equatable {
        case unknown
        case healthy
        case unavailable(String)
    }

    @Published private(set) var snapshot: NativeSnapshotV1?
    @Published private(set) var health: Health = .unknown
    @Published private(set) var isRefreshing = false
    @Published private(set) var isShowingLastGood = false
    @Published private(set) var cacheIssue: String?
    @Published private(set) var usageState = UsageDashboardState()
    @Published private(set) var systemTelemetry: SystemTelemetrySnapshot?
    @Published var baseURLText: String
    @Published var searchText = ""
    @Published var selectedStatus = "All"

    private let client: any SnapshotFetching
    private let cache: any SnapshotCaching
    private let defaults: UserDefaults
    private let usageLastGoodGrace: TimeInterval
    private var pollingTask: Task<Void, Never>?

    init(
        client: any SnapshotFetching = SnapshotClient(),
        cache: any SnapshotCaching = SnapshotCache(),
        defaults: UserDefaults = .standard,
        usageLastGoodGrace: TimeInterval = 120
    ) {
        self.client = client
        self.cache = cache
        self.defaults = defaults
        self.usageLastGoodGrace = usageLastGoodGrace
        baseURLText = Self.resolveBaseURL(
            environment: ProcessInfo.processInfo.environment,
            persistedBaseURL: defaults.string(forKey: Self.persistedBaseURLKey)
        )
        do {
            snapshot = try cache.load()
            isShowingLastGood = snapshot != nil
        } catch {
            snapshot = nil
            cacheIssue = error.localizedDescription
        }
    }

    deinit {
        pollingTask?.cancel()
    }

    var baseURL: URL? {
        URL(string: baseURLText.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    var filteredRows: [NativeSnapshotV1.Row] {
        guard let rows = snapshot?.rows else { return [] }
        return rows.filter { row in
            let statusMatches = selectedStatus == "All" || row.status == selectedStatus
            let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
            let searchMatches = query.isEmpty || [row.title, row.owner, row.module, row.group, row.currentStep]
                .contains { $0.localizedCaseInsensitiveContains(query) }
            return statusMatches && searchMatches
        }
    }

    var statuses: [String] {
        ["All"] + Array(Set(snapshot?.rows.map(\.status) ?? [])).sorted()
    }

    var snapshotStateLabel: String {
        guard let snapshot else { return "Unavailable" }
        if snapshot.stale { return "Stale" }
        if isShowingLastGood { return "Last good" }
        return "Current"
    }

    func applyEndpoint() {
        defaults.set(baseURLText.trimmingCharacters(in: .whitespacesAndNewlines), forKey: Self.persistedBaseURLKey)
        Task { await refresh() }
    }

    static func resolveBaseURL(environment: [String: String], persistedBaseURL: String?) -> String {
        let raw = nonempty(environment["COORD_BOARD_URL"])
            ?? nonempty(persistedBaseURL)
            ?? defaultBaseURL
        return raw.hasSuffix("/") ? String(raw.dropLast()) : raw
    }

    private static func nonempty(_ value: String?) -> String? {
        guard let value = value?.trimmingCharacters(in: .whitespacesAndNewlines),
              !value.isEmpty else { return nil }
        return value
    }

    func startPolling(interval: Duration = .seconds(15)) {
        guard pollingTask == nil else { return }
        pollingTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(for: interval)
            }
        }
    }

    func stopPolling() {
        pollingTask?.cancel()
        pollingTask = nil
    }

    func refresh() async {
        guard !isRefreshing else { return }
        guard let baseURL else {
            health = .unavailable(SnapshotError.invalidEndpoint.localizedDescription)
            return
        }
        isRefreshing = true
        defer { isRefreshing = false }

        var snapshotFailure: Error?
        do {
            let result = try await client.fetchSnapshot(baseURL: baseURL)
            snapshot = result
            isShowingLastGood = false
            do {
                try cache.save(result)
                cacheIssue = nil
            } catch {
                cacheIssue = SnapshotCacheError.saveFailed.localizedDescription
            }
        } catch {
            snapshotFailure = error
            isShowingLastGood = snapshot != nil
        }

        do {
            var usage = try await client.fetchUsage(baseURL: baseURL)
            var warmingRetries = 0
            while usage.providers.isEmpty,
                  usage.refresh?.state == "warming",
                  warmingRetries < 3 {
                warmingRetries += 1
                try await Task.sleep(for: .milliseconds(1_250))
                usage = try await client.fetchUsage(baseURL: baseURL)
            }
            usageState = .accepting(
                usage,
                preserving: usageState,
                at: Date(),
                grace: usageLastGoodGrace
            )
        } catch {
            usageState = .preserving(
                usageState,
                error: error,
                at: Date(),
                grace: usageLastGoodGrace
            )
        }

        if let telemetryClient = client as? any SystemTelemetryFetching {
            do {
                systemTelemetry = try await telemetryClient.fetchSystemTelemetry(baseURL: baseURL)
            } catch {
                // Preserve the last known telemetry snapshot. Unsupported metrics remain N/A.
            }
        }

        do {
            try await client.fetchHealth(baseURL: baseURL)
            if let snapshotFailure {
                health = .unavailable(snapshotFailure.localizedDescription)
            } else {
                health = .healthy
            }
        } catch {
            let detail = snapshotFailure?.localizedDescription ?? error.localizedDescription
            health = .unavailable(detail)
        }
    }
}
