import SwiftUI

#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

enum InstalledUsageFetchError: LocalizedError, Equatable {
    case loopbackRequired
    case invalidResponse
    case httpStatus(Int)

    var errorDescription: String? {
        switch self {
        case .loopbackRequired: "Usage is available only from the loopback CORD board."
        case .invalidResponse: "The usage service returned an invalid response."
        case let .httpStatus(status): "The usage service returned HTTP \(status)."
        }
    }
}

protocol InstalledUsageFetching: Sendable {
    func fetch() async throws -> UsageIntelligenceSnapshot
}

struct InstalledUsageClient: InstalledUsageFetching, Sendable {
    let session: URLSession
    let timeout: TimeInterval

    init(session: URLSession = .shared, timeout: TimeInterval = 5) {
        self.session = session
        self.timeout = timeout
    }

    func endpoint() throws -> URL {
        guard let url = HarnessEndpoint.url("/api/v1/usage-dashboard"),
              let host = url.host,
              Self.isLoopback(host),
              ["http", "https"].contains(url.scheme?.lowercased() ?? "") else {
            throw InstalledUsageFetchError.loopbackRequired
        }
        return url
    }

    func fetch() async throws -> UsageIntelligenceSnapshot {
        var request = URLRequest(
            url: try endpoint(),
            cachePolicy: .reloadIgnoringLocalCacheData,
            timeoutInterval: timeout
        )
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw InstalledUsageFetchError.invalidResponse }
        guard (200..<300).contains(http.statusCode) else {
            throw InstalledUsageFetchError.httpStatus(http.statusCode)
        }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return try decoder.decode(UsageIntelligenceSnapshot.self, from: data)
    }

    private static func isLoopback(_ host: String) -> Bool {
        let normalized = host.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        if normalized == "localhost" || normalized == "::1" { return true }
        let octets = normalized.split(separator: ".", omittingEmptySubsequences: false)
        return octets.count == 4 && Int(octets[0]) == 127 && octets.dropFirst().allSatisfy {
            guard let value = Int($0) else { return false }
            return (0...255).contains(value)
        }
    }
}

final class InstalledUsageStore: ObservableObject {
    @Published private(set) var state = UsageDashboardState()
    var onStateChange: ((UsageDashboardState) -> Void)?

    private let client: any InstalledUsageFetching
    private let lastGoodGrace: TimeInterval
    private let warmingRetryDelay: TimeInterval
    private let staleRefreshingRetryDelay: TimeInterval
    private let transientRetryLimit: Int
    private let sleep: @Sendable (TimeInterval) async throws -> Void
    private var isLoading = false
    private var pendingForcedRefresh = false

    init(
        client: any InstalledUsageFetching = InstalledUsageClient(),
        lastGoodGrace: TimeInterval = 120,
        warmingRetryDelay: TimeInterval = 1.25,
        staleRefreshingRetryDelay: TimeInterval = 0.35,
        transientRetryLimit: Int = 3,
        sleep: @escaping @Sendable (TimeInterval) async throws -> Void = { delay in
            guard delay > 0 else { return }
            try await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
        }
    ) {
        self.client = client
        self.lastGoodGrace = lastGoodGrace
        self.warmingRetryDelay = max(0, warmingRetryDelay)
        self.staleRefreshingRetryDelay = max(0, staleRefreshingRetryDelay)
        self.transientRetryLimit = max(0, transientRetryLimit)
        self.sleep = sleep
    }

    @MainActor
    func refresh(force: Bool = false) async {
        if isLoading {
            if force { pendingForcedRefresh = true }
            return
        }
        isLoading = true
        if state.snapshot != nil { state.refreshing = true }
        defer {
            isLoading = false
            pendingForcedRefresh = false
        }
        repeat {
            pendingForcedRefresh = false
            do {
                let snapshot = try await UsageTransientRefreshRetry.resolve(
                    warmingRetryDelay: warmingRetryDelay,
                    staleRefreshingRetryDelay: staleRefreshingRetryDelay,
                    retryLimit: transientRetryLimit,
                    load: { [client] in try await client.fetch() },
                    sleep: sleep
                )
                publish(.accepting(
                    snapshot,
                    preserving: state,
                    at: Date(),
                    grace: lastGoodGrace
                ))
            } catch {
                publish(.preserving(state, error: error, at: Date(), grace: lastGoodGrace))
            }
        } while pendingForcedRefresh
    }

    @MainActor
    private func publish(_ next: UsageDashboardState) {
        state = next
        onStateChange?(next)
    }
}

struct InstalledUsageDashboardView: View {
    @ObservedObject private var store: InstalledUsageStore
    @State private var showingAccounts = false
    let compact: Bool
    let managesRefresh: Bool
    let onClose: (() -> Void)?
    let onOpenSettings: (() -> Void)?

    init(
        compact: Bool = false,
        managesRefresh: Bool = true,
        onClose: (() -> Void)? = nil,
        onOpenSettings: (() -> Void)? = nil,
        store: InstalledUsageStore = InstalledUsageStore()
    ) {
        self.store = store
        self.compact = compact
        self.managesRefresh = managesRefresh
        self.onClose = onClose
        self.onOpenSettings = onOpenSettings
    }

    var body: some View {
        UsageDashboardContent(
            state: store.state,
            forceCompact: compact,
            usesDenseRoute: true,
            onClose: onClose,
            onOpenSettings: { showingAccounts = true },
            onRefresh: { Task { await store.refresh(force: true) } }
        )
        .sheet(isPresented: $showingAccounts) {
            UsageAccountSettingsView(
                baseURL: HarnessEndpoint.url("/"),
                onOpenCORDSettings: onOpenSettings
            )
        }
        .task {
            guard managesRefresh else { return }
            await store.refresh(force: true)
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 60_000_000_000)
                guard !Task.isCancelled else { return }
                await store.refresh()
            }
        }
    }
}
