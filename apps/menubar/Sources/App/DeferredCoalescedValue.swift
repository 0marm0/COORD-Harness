import Foundation

final class DeferredCoalescedValue<Value> {
    typealias Scheduler = (@escaping () -> Void) -> Void

    private let scheduler: Scheduler
    private var pendingValue: Value?
    private var pendingCommit: ((Value) -> Void)?
    private var isScheduled = false

    init(scheduler: @escaping Scheduler = { action in
        DispatchQueue.main.async { action() }
    }) {
        self.scheduler = scheduler
    }

    func enqueue(
        base: @autoclosure () -> Value,
        update: (inout Value) -> Void,
        commit: @escaping (Value) -> Void
    ) {
        dispatchPrecondition(condition: .onQueue(.main))
        var next = pendingValue ?? base()
        update(&next)
        pendingValue = next
        pendingCommit = commit
        guard !isScheduled else { return }
        isScheduled = true
        scheduler { [weak self] in self?.drain() }
    }

    func drain() {
        dispatchPrecondition(condition: .onQueue(.main))
        guard isScheduled else { return }
        isScheduled = false
        guard let value = pendingValue, let commit = pendingCommit else { return }
        pendingValue = nil
        pendingCommit = nil
        commit(value)
    }

    func cancel() {
        dispatchPrecondition(condition: .onQueue(.main))
        isScheduled = false
        pendingValue = nil
        pendingCommit = nil
    }
}
