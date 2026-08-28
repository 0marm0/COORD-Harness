import AppKit
import UserNotifications


final class Notifier {
    var enabled = true
    private var last: [String: String] = [:]
    private var lastPostedAt: [String: TimeInterval] = [:]
    private var authorized = false
    private let cooldownSecs: TimeInterval
    private let stateURL: URL

    init(cooldownSecs: TimeInterval = 6 * 3600, stateURL: URL? = nil) {
        self.cooldownSecs = cooldownSecs
        self.stateURL = stateURL ?? Notifier.defaultStateURL()
        loadState()
    }

    func prime() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { [weak self] ok, _ in
            self?.authorized = ok
        }
    }

    func check(_ state: MenubarState) {
        guard enabled else { return }
        let rows = state.workModel?.statusRows ?? []
        var current: [String: String] = [:]
        var names: [String: String] = [:]
        var byID: [String: Row] = [:]
        for r in rows {
            let id = stableID(r)
            guard !id.isEmpty else { continue }
            let status = normalizedStatus(r.status)
            current[id] = status
            names[id] = r.title
            byID[id] = r
        }
        let now = Date().timeIntervalSince1970
        for (id, status) in current {
            guard let prev = last[id], prev != status else { continue }
            guard shouldNotify(row: byID[id], id: id, status: status, previous: prev, now: now) else { continue }
            let name = names[id] ?? "Job"
            switch status {
            case "DONE":
                markPosted(id: id, status: status, now: now)
                post("Job Done", "\(name) completed")
            case "FAILED", "ERROR":
                markPosted(id: id, status: status, now: now)
                post("Job Failed", "\(name) failed — check dashboard")
            case "BLOCKED":
                markPosted(id: id, status: status, now: now)
                post("Job Blocked", "\(name) is blocked")
            default: break
            }
        }
        last = current
        saveState()
    }

    private func post(_ title: String, _ body: String) {
        guard authorized else { return }
        let c = UNMutableNotificationContent(); c.title = title; c.body = body
        let req = UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil)
        UNUserNotificationCenter.current().add(req)
    }

    private func shouldNotify(row: Row?, id: String, status: String, previous: String, now: TimeInterval) -> Bool {
        guard ["DONE", "FAILED", "ERROR", "BLOCKED"].contains(status) else { return false }
        if recentlyPosted(id: id, status: status, now: now) { return false }
        if status == "BLOCKED" {
            return isOperatorActionBlock(row)
        }
        return true
    }

    private func isOperatorActionBlock(_ row: Row?) -> Bool {
        row?.isOperatorActionBlock ?? false
    }

    private func stableID(_ row: Row) -> String {
        row.dedupKey ?? row.roadmapId ?? row.jobId ?? row.id ?? row.name ?? ""
    }

    private func normalizedStatus(_ raw: String?) -> String {
        (raw ?? "").trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
    }

    private func postKey(id: String, status: String) -> String { "\(id)|\(status)" }

    private func recentlyPosted(id: String, status: String, now: TimeInterval) -> Bool {
        guard let ts = lastPostedAt[postKey(id: id, status: status)] else { return false }
        return now - ts < cooldownSecs
    }

    private func markPosted(id: String, status: String, now: TimeInterval) {
        lastPostedAt[postKey(id: id, status: status)] = now
    }

    private static func defaultStateURL() -> URL {
        let dir = FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)
            .first ?? URL(fileURLWithPath: NSTemporaryDirectory())
        return dir.appendingPathComponent("COORDHARNESS", isDirectory: true)
            .appendingPathComponent("notification_state.json")
    }

    private struct StoredState: Codable {
        var last: [String: String]
        var lastPostedAt: [String: TimeInterval]
    }

    private func loadState() {
        guard let data = try? Data(contentsOf: stateURL),
              let stored = try? JSONDecoder().decode(StoredState.self, from: data) else { return }
        last = stored.last
        lastPostedAt = stored.lastPostedAt
    }

    private func saveState() {
        let stored = StoredState(last: last, lastPostedAt: lastPostedAt)
        guard let data = try? JSONEncoder().encode(stored) else { return }
        do {
            try FileManager.default.createDirectory(at: stateURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            try data.write(to: stateURL, options: .atomic)
        } catch {

        }
    }
}
