import XCTest
@testable import CoordCockpitMac

@MainActor
final class StatusMenuActionQueueTests: XCTestCase {
    private struct Settings: Equatable {
        var mode = "bars"
        var stats = true
        var profile = "balanced"
    }

    func testChangesWaitForMenuDismissalSchedulerAndCommitOnce() {
        var scheduled: [@MainActor () -> Void] = []
        let coalescer = DeferredCoalescedValue<Settings> { scheduled.append($0) }
        var commits: [Settings] = []

        coalescer.enqueue(base: Settings(), update: { $0.mode = "minimal" }, commit: { commits.append($0) })
        coalescer.enqueue(base: Settings(), update: { $0.stats = false }, commit: { commits.append($0) })
        coalescer.enqueue(base: Settings(), update: { $0.profile = "live" }, commit: { commits.append($0) })

        XCTAssertTrue(commits.isEmpty)
        XCTAssertEqual(scheduled.count, 1)
        scheduled.removeFirst()()
        XCTAssertEqual(commits, [Settings(mode: "minimal", stats: false, profile: "live")])
    }

    func testRapidPreferenceStormSchedulesAndCommitsOnlyOnce() {
        var scheduled: [@MainActor () -> Void] = []
        let coalescer = DeferredCoalescedValue<Int> { scheduled.append($0) }
        var commits: [Int] = []

        for value in 0..<1_000 {
            coalescer.enqueue(base: -1, update: { $0 = value }, commit: { commits.append($0) })
        }

        XCTAssertEqual(scheduled.count, 1)
        XCTAssertTrue(commits.isEmpty)
        scheduled.removeFirst()()
        XCTAssertEqual(commits, [999])
    }

    func testDrainIsIdempotentAndCancelDropsPendingCommit() {
        var scheduled: [@MainActor () -> Void] = []
        let coalescer = DeferredCoalescedValue<Int> { scheduled.append($0) }
        var commits: [Int] = []
        coalescer.enqueue(base: 0, update: { $0 += 1 }, commit: { commits.append($0) })
        coalescer.drain()
        coalescer.drain()
        XCTAssertEqual(commits, [1])

        coalescer.enqueue(base: 1, update: { $0 += 1 }, commit: { commits.append($0) })
        coalescer.cancel()
        scheduled.forEach { $0() }
        XCTAssertEqual(commits, [1])
    }

    func testDoneAndApplyConfigUseSingleDiffedPaths() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        let settings = try String(contentsOf: root.appendingPathComponent("menubar/Sources/UI/SettingsView.swift"))
        let delegate = try String(contentsOf: root.appendingPathComponent("menubar/Sources/App/AppDelegate.swift"))

        let done = try XCTUnwrap(settings.components(separatedBy: "@objc private func doSave()").dropFirst().first?.components(separatedBy: "@objc private func controlChanged()").first)
        XCTAssertTrue(done.contains("isCompletingSave = true"))
        XCTAssertTrue(done.contains("makeFirstResponder(nil)"))
        XCTAssertEqual(done.components(separatedBy: "commitChanges()").count - 1, 1)
        XCTAssertEqual(done.components(separatedBy: "onSave?(cfg)").count - 1, 1)
        XCTAssertTrue(settings.contains("guard !isCompletingSave else { return }\n        commitChanges()\n        onChange?(cfg)"))
        XCTAssertEqual(settings.components(separatedBy: "cfg.save()").count - 1, 1)

        let apply = try XCTUnwrap(delegate.components(separatedBy: "func applyConfig(_ cfg: Config)").dropFirst().first?.components(separatedBy: "func applicationWillTerminate").first)
        for guardName in ["hotkeyChanged", "notificationsChanged", "loginItemChanged", "telemetryScheduleChanged", "sourceChanged", "slowTimerChanged", "openTimerChanged"] {
            XCTAssertTrue(apply.contains(guardName), "missing domain diff: \(guardName)")
        }
        XCTAssertTrue(apply.contains("if hotkeyChanged"))
        XCTAssertTrue(apply.contains("if loginItemChanged"))
        XCTAssertTrue(apply.contains("if telemetryScheduleChanged"))
        XCTAssertEqual(apply.components(separatedBy: "hotkey.register").count - 1, 1)
        XCTAssertEqual(apply.components(separatedBy: "LoginItemManager.apply").count - 1, 1)
        XCTAssertEqual(apply.components(separatedBy: "startSystemTelemetryRefresh()").count - 1, 1)
    }

    func testControllerAndTelemetryContractsAvoidMenuTrackingReentrancy() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        let controller = try String(contentsOf: root.appendingPathComponent("menubar/Sources/App/StatusItemController.swift"))
        let delegate = try String(contentsOf: root.appendingPathComponent("menubar/Sources/App/AppDelegate.swift"))
        let telemetry = try String(contentsOf: root.appendingPathComponent("Shared/Sources/SystemTelemetry.swift"))

        XCTAssertFalse(controller.contains("statusMode = mode.rawValue\n        onStatusModeChange"))
        XCTAssertTrue(controller.contains("func applyPreferences(_ config: Config)"))
        XCTAssertTrue(controller.contains("isBatchingPreferences"))
        XCTAssertFalse(controller.contains("(\"Light\", \"light\")"))
        XCTAssertTrue(controller.contains("case \"disk\": selected = systemTelemetryShowDisk"))
        XCTAssertTrue(controller.contains("default: return"))
        XCTAssertTrue(delegate.contains("enqueueMenuConfigChange"))
        XCTAssertTrue(delegate.contains("DeferredCoalescedValue<Config>"))
        XCTAssertTrue(telemetry.contains("@MainActor\nfinal class SystemTelemetryStore"))
    }
}
