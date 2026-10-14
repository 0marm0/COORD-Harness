import Foundation

/// A visible standalone window owns one usage refresh loop, independently of
/// slower board projection reads. Embedded menu windows use their existing owner.
@MainActor
final class UsageWindowRefreshLifecycle {
    private let interval: TimeInterval
    private let refresh: @MainActor @Sendable () async -> Void
    private let sleep: @Sendable (TimeInterval) async throws -> Void
    private var task: Task<Void, Never>?

    init(
        interval: TimeInterval = 60,
        refresh: @escaping @MainActor @Sendable () async -> Void,
        sleep: @escaping @Sendable (TimeInterval) async throws -> Void = { delay in
            try await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
        }
    ) {
        self.interval = interval.isFinite ? max(0.01, interval) : 60
        self.refresh = refresh
        self.sleep = sleep
    }

    func start() {
        guard task == nil else { return }
        let refresh = self.refresh, sleep = self.sleep, interval = self.interval
        task = Task {
            await refresh()
            while !Task.isCancelled {
                do { try await sleep(interval) } catch { return }
                guard !Task.isCancelled else { return }
                await refresh()
            }
        }
    }

    func stop() {
        task?.cancel()
        task = nil
    }

    deinit { task?.cancel() }
}
