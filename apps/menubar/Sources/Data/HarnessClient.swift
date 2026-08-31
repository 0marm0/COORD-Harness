import Foundation


protocol SnapshotSource: AnyObject {

    func current() async -> MenubarState?

    var onChange: (() -> Void)? { get set }
    func start()
    func stop()
}

enum SnapshotTransportKind: String, Equatable {
    case db
    case filewatch
    case http

    static func resolve(_ raw: String?) -> SnapshotTransportKind {
        switch (raw ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "filewatch", "file-watch", "snapshot":
            return .filewatch
        case "http", "api":
            return .http
        case "db", "coord", "coord-db", "native":
            return .db
        default:
            return .db
        }
    }
}


final class NativeCockpitDBSource: SnapshotSource {
    var onChange: (() -> Void)?

    private let readModel: CockpitReadModelLoading
    private let now: () -> Double
    private let fallbackGraceSeconds: Double
    private var lastGood: MenubarState?
    private var lastGoodAt: Double?

    init(
        readModel: CockpitReadModelLoading = CockpitReadModelSource(),
        now: @escaping () -> Double = { Date().timeIntervalSince1970 },
        fallbackGraceSeconds: Double = 45
    ) {
        self.readModel = readModel
        self.now = now
        self.fallbackGraceSeconds = fallbackGraceSeconds
        MenubarLog.info("NativeCockpitDBSource init database=\(CoordSQLite.defaultPath)")
    }

    func start() {}
    func stop() {}

    func current() async -> MenubarState? {
        let currentNow = now()
        let cockpit = readModel.load()
        let state = Self.menubarState(from: cockpit, now: currentNow)
        if state.error == nil {
            lastGood = state
            lastGoodAt = currentNow
            return state
        }
        if var good = lastGood {
            let age = max(0, currentNow - (lastGoodAt ?? good.ts ?? currentNow))
            if age <= fallbackGraceSeconds {
                good.stale = false
                good.refreshing = true
                good.error = nil
                good.ts = currentNow
            } else {
                good.stale = true
                good.refreshing = false
                good.error = state.error
            }
            return good
        }
        return state
    }

    static func menubarState(from cockpit: CockpitState, now: Double = Date().timeIntervalSince1970) -> MenubarState {
        if let error = cockpit.error {
            return MenubarState(
                schemaVersion: 2,
                version: 1,
                source: "coord-db",
                coordActive: true,
                coordCutover: true,
                coordNativeSource: "coord.db",
                stale: true,
                refreshing: cockpit.refreshing,
                error: "\(error.kind.rawValue): \(error.message)",
                ts: now,
                workModel: WorkModel(summary: Summary(open: 0, total: 0))
            )
        }

        let rows = cockpit.rows.map { Row(cockpit: $0) }
        let scopedRows = rows.filter { !isReferenceBucket($0) }
        let menuRows = scopedRows.isEmpty ? rows : scopedRows
        let runningRows = menuRows.filter { bucket(for: $0) == .running }
        let attentionRows = menuRows.filter { bucket(for: $0) == .attention }
        let followupRows = menuRows.filter { bucket(for: $0) == .followup }
        let nextRows = menuRows.filter { bucket(for: $0) == .next }
        let statusRows = menuRows.filter { bucket(for: $0) == .status }

        return MenubarState(
            schemaVersion: 2,
            version: 1,
            mode: cockpit.mode,
            liveMode: cockpit.liveMode,
            diagnostics: Diagnostics(
                agentSidecars: nil,
                agentMilestones: menuRows.filter { $0.isAgentCoordination }.count,
                jobSidecars: nil,
                roadmapItems: rows.count,
                unifiedRows: menuRows.count,
                sidecarParseErrors: 0,
                projectionTs: now
            ),
            source: "coord-db",
            coordActive: true,
            coordCutover: true,
            coordNativeSource: "coord.db",
            stale: cockpit.stale,
            refreshing: cockpit.refreshing,
            ts: now,
            workModel: WorkModel(
                summary: Summary(
                    running: cockpit.summary.running,
                    blocked: cockpit.summary.blocked,
                    doneToday: cockpit.summary.doneToday,
                    open: runningRows.count + attentionRows.count + followupRows.count + nextRows.count,
                    total: menuRows.count,
                    attention: cockpit.summary.attention,
                    next: cockpit.summary.next,
                    queueOpen: cockpit.summary.local,
                    queueBlocked: cockpit.summary.blocked
                ),
                runningRows: runningRows,
                attentionRows: attentionRows,
                followupRows: followupRows,
                nextRows: nextRows,
                queueActiveRows: [],
                queueBlockedRows: [],
                queueTerminalRows: [],
                statusRows: statusRows,
                localLanes: nil,
                agentMilestoneRows: runningRows.filter { $0.isAgentCoordination }
            )
        )
    }

    private enum Bucket {
        case running
        case attention
        case followup
        case next
        case status
    }

    private static func isReferenceBucket(_ row: Row) -> Bool {
        let section = (row.section ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let lane = (row.lane ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return section == "all" || lane == "all"
    }

    private static func bucket(for row: Row) -> Bucket {
        let scope = (row.section ?? row.lane ?? "").lowercased()
        let status = (row.status ?? "").uppercased()
        if scope.contains("follow") { return .followup }
        if scope.contains("attention") || ["BLOCKED", "FAILED", "STALLED"].contains(status) { return .attention }
        if scope.contains("running") || scope == "now" || status == "RUNNING" || row.live == true { return .running }
        if ["DONE", "KILLED", "CANCELLED", "CANCELED"].contains(status) { return .status }
        return .next
    }
}

extension Row {
    init(cockpit row: CockpitRow) {
        let normalizedStatus = row.status.uppercased()
        let isRunning = normalizedStatus == "RUNNING"
        let hasPct = row.pct != nil
        let progressKind = row.progressKind?.lowercased() ?? (hasPct ? "determinate" : "none")

        self.id = row.workID ?? row.jobID ?? row.dedupKey
        self.roadmapId = row.workID
        self.jobId = row.jobID
        self.dedupKey = row.dedupKey
        self.section = row.groupKey ?? row.scope
        self.lane = row.scope
        self.source = "coord-db"
        self.name = row.title
        self.display = row.title
        self.status = normalizedStatus
        self.paused = row.paused ?? (normalizedStatus == "PAUSED")
        self.live = row.live ?? isRunning
        self.stale = row.stale
        self.pct = row.pct
        self.pctDisplay = row.pctDisplay
        self.etaS = row.etaSeconds
        self.etaText = row.etaText
        self.etaDerived = row.etaDerived
        self.rate = row.rate
        self.rateUnit = nil
        self.done = row.done
        self.total = row.total
        self.indeterminate = progressKind == "indeterminate"
        self.loading = nil
        self.hasProgress = row.hasProgress ?? hasPct
        self.determinate = row.determinate ?? hasPct
        self.owner = row.owner
        self.owners = row.owner.map { [$0] }
        self.ownerGroup = row.ownerGroup
        self.ownerSessionId = row.ownerSessionID
        self.ownerSessionActor = row.ownerSessionActor
        self.ownerSessionLabel = row.ownerSessionLabel
        self.ownerExternalThreadId = row.ownerExternalThreadID
        self.ownerConversationTitle = row.ownerConversationTitle
        self.ownerWorktreeId = row.ownerWorktreeID
        self.crossAgentHandoff = nil
        self.handoffFrom = nil
        self.handoffTo = nil
        self.handoffLabel = nil
        self.kind = row.resourceClass
        self.rowKind = row.rowKind
        self.workKind = (row.rowKind ?? "").lowercased() == "agent" ? "agent_coordination" : nil
        self.platform = row.ownerGroup ?? row.owner
        self.hasProcess = nil
        self.progressKind = progressKind
        self.resourceClass = row.resourceClass
        self.module = row.module
        self.moduleLabel = row.moduleLabel
        self.domainLabel = row.domainLabel
        self.domainShortLabel = row.domainLabel
        self.priority = row.priority
        self.tier = nil
        self.epic = nil
        self.parent = row.parentID
        self.surface = row.rowKind
        self.detail = row.noteText
        self.note = row.noteText
        self.step = row.whyText
        self.currentStep = row.whyText
        self.nextRankReason = row.whyText
        self.whyNext = row.whyText
        self.pid = row.pid
        self.pgid = row.pgid
        self.cpu = nil
        self.ramMb = nil
        self.sidecarAgeS = row.sidecarAgeSeconds
        self.operatorState = nil
        self.operatorLastAction = nil
        self.visibility = nil
        self.blockedReasonClass = normalizedStatus == "BLOCKED" ? row.whyText : nil
        self.coordClaimStatus = normalizedStatus
        self.queuePosition = nil
        self.queueStatus = nil
        self.queueLaunchable = nil
        self.assignee = row.owner
        self.dependsOn = nil
        self.doneSignal = row.doneSignal
        self.doneSignalExists = nil
        self.acceptance = nil
        self.acceptanceJson = nil
        self.acceptanceSummary = row.acceptanceSummary
        self.contextPackRef = row.contextPackRef
        self.rubricState = nil
        self.rubricVerdict = nil
        self.tokenBudget = nil
        self.heartbeatDueAt = nil
        self.dueDate = nil
        self.mode = nil
        self.reqMode = nil
        self.modelLabel = nil
        self.nproc = nil
        self.nextRank = nil
    }
}


final class HTTPSource: SnapshotSource {
    var onChange: (() -> Void)?
    static let maxKnownSchema = 2

    private let url = URL(string: "\(HarnessEndpoint.base)/api/menubar")!
    private let session: URLSession
    private var lastGood: MenubarState?
    private var fetchCount = 0
    private var lastErrorText: String?

    init(timeout: TimeInterval = 8) {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.requestCachePolicy = .reloadIgnoringLocalCacheData
        cfg.timeoutIntervalForRequest = timeout
        cfg.httpMaximumConnectionsPerHost = 1
        cfg.httpShouldUsePipelining = false
        self.session = URLSession(configuration: cfg)
        self.lastGood = SnapshotCache.load()
        MenubarLog.info("HTTPSource init url=\(url.absoluteString) timeout=\(timeout) cacheLoaded=\(lastGood != nil)")
    }

    func start() {}
    func stop() {}

    func current() async -> MenubarState? {
        fetchCount += 1
        var req = URLRequest(url: url)
        req.setValue("close", forHTTPHeaderField: "Connection")
        do {
            let (data, response) = try await session.data(for: req)
            if let http = response as? HTTPURLResponse, http.statusCode != 200 {
                MenubarLog.info("HTTPSource fetch non-200 status=\(http.statusCode) bytes=\(data.count)")
                throw URLError(.badServerResponse)
            }
            let state = try Self.decode(data)
            lastGood = state
            SnapshotCache.save(data)
            lastErrorText = nil
            if fetchCount == 1 || fetchCount % 30 == 0 {
                let summary = state.workModel?.summary
                let staleText = state.stale.map(String.init) ?? "nil"
                MenubarLog.info("HTTPSource fetch ok count=\(fetchCount) bytes=\(data.count) stale=\(staleText) error=\(state.error ?? "nil") source=\(state.source ?? "nil") coordNative=\(state.coordNativeSource ?? "nil") running=\(summary?.running ?? -1) next=\(summary?.next ?? -1)")
            }
            return state
        } catch {

            let text = "\(type(of: error)): \(error)"
            if text != lastErrorText {
                MenubarLog.info("HTTPSource fetch failed count=\(fetchCount) error=\(text) lastGood=\(lastGood != nil)")
                lastErrorText = text
            }
            if var s = lastGood { s.stale = true; s.error = text; return s }
            return MenubarState(stale: true, error: text)
        }
    }

    static func decode(_ data: Data) throws -> MenubarState {
        let dec = JSONDecoder(); dec.keyDecodingStrategy = .convertFromSnakeCase
        var state = try dec.decode(MenubarState.self, from: data)
        if let v = state.schemaVersion, v > Self.maxKnownSchema { state.schemaAhead = true }
        return state
    }
}


final class FileWatchSource: SnapshotSource {
    var onChange: (() -> Void)?


    static let defaultPath = "\(NSHomeDirectory())/.coordharness/projection/menubar.snapshot.json"
    static let defaultLivenessPath = "\(NSHomeDirectory())/.coordharness/projection/menubar.liveness.json"

    private let path: String
    private let livenessPath: String
    private let staleAfter: TimeInterval
    private let queue = DispatchQueue(label: "io.coordharness.menubar.filewatch")
    private var fileSource: DispatchSourceFileSystemObject?
    private var dirSource: DispatchSourceFileSystemObject?
    private var fd: Int32 = -1
    private var watchedFileNumber: UInt64?
    private var lastGood: MenubarState?
    private var coalesce: DispatchWorkItem?

    init(
        path: String = FileWatchSource.defaultPath,
        livenessPath: String? = nil,
        staleAfter: TimeInterval = 30
    ) {
        self.path = path
        self.livenessPath = livenessPath ?? ((path as NSString).deletingLastPathComponent as NSString)
            .appendingPathComponent("menubar.liveness.json")
        self.staleAfter = staleAfter


        self.lastGood = Self.loadSnapshot(
            path: path, livenessPath: self.livenessPath, staleAfter: staleAfter
        ) ?? SnapshotCache.load()
        MenubarLog.info("FileWatchSource init path=\(path) liveness=\(self.livenessPath) staleAfter=\(staleAfter) initialLoaded=\(lastGood != nil)")
    }

    func start() { queue.async { [weak self] in self?.arm() } }
    func stop()  { queue.async { [weak self] in self?.disarm() } }


    func current() async -> MenubarState? {
        if let fresh = Self.loadSnapshot(path: path, livenessPath: livenessPath, staleAfter: staleAfter) {
            lastGood = fresh
            if path == Self.defaultPath,
               let data = try? Data(contentsOf: URL(fileURLWithPath: path)) {
                SnapshotCache.save(data)
            }
            return fresh
        }
        if let s = lastGood {
            let staleText = s.stale.map(String.init) ?? "nil"
            MenubarLog.info("FileWatchSource current using lastGood path=\(path) stale=\(staleText) error=\(s.error ?? "nil")")
            return Self.withLocalStaleness(s, now: Date().timeIntervalSince1970, staleAfter: staleAfter)
        }
        MenubarLog.info("FileWatchSource current missing snapshot path=\(path)")
        return MenubarState(stale: true, error: "snapshot not yet available")
    }


    static func decode(
        _ data: Data,
        now: Double,
        staleAfter: TimeInterval,
        livenessTs: Double? = nil
    ) -> MenubarState? {
        let dec = JSONDecoder(); dec.keyDecodingStrategy = .convertFromSnakeCase
        guard var s = try? dec.decode(MenubarState.self, from: data) else { return nil }
        s = withLocalStaleness(s, now: now, staleAfter: staleAfter, livenessTs: livenessTs)
        if let v = s.schemaVersion, v > HTTPSource.maxKnownSchema { s.schemaAhead = true }
        return s
    }

    static func withLocalStaleness(
        _ state: MenubarState,
        now: Double,
        staleAfter: TimeInterval,
        livenessTs: Double? = nil
    ) -> MenubarState {
        var s = state
        s.stale = (s.stale == true) || isStale(
            ts: livenessTs ?? s.ts, now: now, staleAfter: staleAfter
        )
        return s
    }


    static func isStale(ts: Double?, now: Double, staleAfter: TimeInterval) -> Bool {
        guard let ts else { return false }
        return now - ts > staleAfter
    }


    private func arm() {
        disarm()
        watchParentDir()
        let newFd = open(path, O_EVTONLY)
        guard newFd >= 0 else {
            MenubarLog.info("FileWatchSource arm waiting for snapshot path=\(path)")
            return
        }
        fd = newFd
        watchedFileNumber = Self.fileNumber(atPath: path)
        let src = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: newFd, eventMask: [.write, .extend, .delete, .rename, .revoke], queue: queue)
        src.setEventHandler { [weak self, weak src] in
            guard let self, let src else { return }
            let flags = src.data


            if !flags.isDisjoint(with: [.delete, .rename, .revoke]) {
                self.arm()
            } else {
                self.scheduleRead()
            }
        }


        src.setCancelHandler { close(newFd) }
        fileSource = src
        src.resume()
        scheduleRead()
    }

    private func disarm() {
        fileSource?.cancel(); fileSource = nil
        dirSource?.cancel();  dirSource = nil
        fd = -1
        watchedFileNumber = nil
    }


    private func scheduleRead() {
        coalesce?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.readAndEmit() }
        coalesce = work
        queue.asyncAfter(deadline: .now() + .milliseconds(30), execute: work)
    }

    private func readAndEmit() {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)) else { return }
        let livenessTs = Self.boundLivenessTimestamp(snapshotPath: path, livenessPath: livenessPath)
        guard let s = Self.decode(
            data,
            now: Date().timeIntervalSince1970,
            staleAfter: staleAfter,
            livenessTs: livenessTs
        ) else {
            MenubarLog.info("FileWatchSource decode failed path=\(path) bytes=\(data.count)")
            return
        }
        lastGood = s
        if path == Self.defaultPath { SnapshotCache.save(data) }
        onChange?()
    }

    private static func loadSnapshot(
        path: String,
        livenessPath: String,
        staleAfter: TimeInterval
    ) -> MenubarState? {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)) else { return nil }
        return decode(
            data,
            now: Date().timeIntervalSince1970,
            staleAfter: staleAfter,
            livenessTs: boundLivenessTimestamp(snapshotPath: path, livenessPath: livenessPath)
        )
    }

    private struct LivenessReceipt: Decodable {
        let schemaVersion: Int?
        let ts: Double?
        let snapshotInode: UInt64?
        let snapshotSizeBytes: UInt64?
    }

    private static func fileNumber(atPath path: String) -> UInt64? {
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: path),
              let value = attrs[.systemFileNumber] as? NSNumber else { return nil }
        return value.uint64Value
    }

    private static func boundLivenessTimestamp(snapshotPath: String, livenessPath: String) -> Double? {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: livenessPath)) else { return nil }
        let dec = JSONDecoder(); dec.keyDecodingStrategy = .convertFromSnakeCase
        guard let receipt = try? dec.decode(LivenessReceipt.self, from: data),
              receipt.schemaVersion == 1,
              let ts = receipt.ts,
              let receiptInode = receipt.snapshotInode,
              let receiptSize = receipt.snapshotSizeBytes,
              let attrs = try? FileManager.default.attributesOfItem(atPath: snapshotPath),
              let inode = attrs[.systemFileNumber] as? NSNumber,
              let size = attrs[.size] as? NSNumber,
              inode.uint64Value == receiptInode,
              size.uint64Value == receiptSize else { return nil }
        return ts
    }


    private func watchParentDir() {
        let dir = (path as NSString).deletingLastPathComponent
        let dfd = open(dir, O_EVTONLY)
        guard dfd >= 0 else {
            queue.asyncAfter(deadline: .now() + 1) { [weak self] in self?.arm() }; return
        }
        let dsrc = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: dfd, eventMask: [.write], queue: queue)
        dsrc.setEventHandler { [weak self] in
            guard let self else { return }
            let currentFileNumber = Self.fileNumber(atPath: self.path)
            if currentFileNumber != nil && currentFileNumber != self.watchedFileNumber {
                self.arm()
            }
        }
        dsrc.setCancelHandler { close(dfd) }
        dirSource = dsrc
        dsrc.resume()
    }
}


struct CapabilityResultSummary {
    let job: String
    let action: String
    let label: String
    let ok: Bool
    let statusText: String
    let resultId: String?
    let updatedAt: Date
}

enum CapabilityResultCache {
    private static let lock = NSLock()
    private static var latestByJob: [String: CapabilityResultSummary] = [:]
    private static var loadingKeys: Set<String> = []
    private static let ttl: TimeInterval = 900

    static func markLoading(job: String, action: String) {
        let summary = CapabilityResultSummary(
            job: job,
            action: action,
            label: actionLabel(action),
            ok: true,
            statusText: "running...",
            resultId: nil,
            updatedAt: Date()
        )
        lock.lock()
        loadingKeys.insert(cacheKey(job: job, action: action))
        latestByJob[job] = summary
        lock.unlock()
    }

    static func isLoading(job: String, action: String) -> Bool {
        lock.lock()
        let loading = loadingKeys.contains(cacheKey(job: job, action: action))
        lock.unlock()
        return loading
    }

    static func store(_ summary: CapabilityResultSummary) {
        lock.lock()
        latestByJob[summary.job] = summary
        loadingKeys.remove(cacheKey(job: summary.job, action: summary.action))
        lock.unlock()
    }

    static func latest(job: String) -> CapabilityResultSummary? {
        lock.lock()
        let value = latestByJob[job]
        if let value, Date().timeIntervalSince(value.updatedAt) > ttl {
            latestByJob.removeValue(forKey: job)
            lock.unlock()
            return nil
        }
        lock.unlock()
        return value
    }

    private static func actionLabel(_ action: String) -> String {
        switch action {
        case "context": return "Context"
        case "verify": return "Verify"
        case "open_proof": return "Proof"
        case "loop_doctor": return "Loop"
        case "deerflow": return "Workflow"
        case "token_ledger": return "Tokens"
        case "handoff_packet": return "Packet"
        default: return action
        }
    }

    private static func cacheKey(job: String, action: String) -> String {
        "\(job)\u{1f}\(action)"
    }
}

struct HarnessControlOutcome: Equatable {
    let ok: Bool
    let message: String
}

enum HarnessControl {
    private static let base = "\(HarnessEndpoint.base)"
    private static let session: URLSession = {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 3
        cfg.httpMaximumConnectionsPerHost = 2
        return URLSession(configuration: cfg)
    }()


    static func setMode(_ mode: String, done: ((HarnessControlOutcome) -> Void)? = nil) {
        guard var c = URLComponents(string: "\(base)/api/mode") else {
            done?(.init(ok: false, message: "Invalid mode endpoint.")); return
        }
        c.queryItems = [URLQueryItem(name: "set", value: mode)]
        guard let url = c.url else { done?(.init(ok: false, message: "Invalid mode URL.")); return }
        postOutcome(url, success: "Performance mode · \(mode) verified", done: done)
    }


    static func request(job: String, action: String) {
        let crid = UUID().uuidString
        guard var c = URLComponents(string: "\(base)/api/request") else { return }
        c.queryItems = [URLQueryItem(name: "job", value: job),
                        URLQueryItem(name: "action", value: action),
                        URLQueryItem(name: "crid", value: crid)]
        guard let u = c.url else { return }
        post(u)
    }


    static func pauseAll(jobIds: [String], done: ((HarnessControlOutcome) -> Void)? = nil) {
        guard var c = URLComponents(string: "\(base)/api/bulk_control") else {
            done?(.init(ok: false, message: "Invalid pause endpoint.")); return
        }
        c.queryItems = [URLQueryItem(name: "action", value: "pause")]
        guard let url = c.url else { done?(.init(ok: false, message: "Invalid pause URL.")); return }
        postOutcome(url, success: "All eligible work paused · verified", done: done)
    }

    static func resumeAll(jobIds: [String]) {
        guard var c = URLComponents(string: "\(base)/api/bulk_control") else { return }
        c.queryItems = [URLQueryItem(name: "action", value: "resume")]
        if let u = c.url { post(u) }
    }


    static func taskAction(job: String, action: String, assignee: String? = nil) {
        guard var c = URLComponents(string: "\(base)/api/task_action") else { return }
        var items = [URLQueryItem(name: "job", value: job), URLQueryItem(name: "action", value: action)]
        if let a = assignee { items.append(URLQueryItem(name: "assignee", value: a)) }
        c.queryItems = items
        if let u = c.url { post(u) }
    }


    static func capability(job: String, action: String, contextRef: String?, done: ((CapabilityResultSummary) -> Void)? = nil) {
        guard var c = URLComponents(string: "\(base)/api/capability") else { return }
        var items = [
            URLQueryItem(name: "job", value: job),
            URLQueryItem(name: "action", value: action),
            URLQueryItem(name: "execute", value: "1"),
        ]
        if let contextRef, !contextRef.isEmpty {
            items.append(URLQueryItem(name: "context_ref", value: contextRef))
        }
        c.queryItems = items
        guard let u = c.url else { return }
        postData(u) { data, ok in
            let summary = capabilitySummary(job: job, action: action, data: data, transportOK: ok)
            CapabilityResultCache.store(summary)
            done?(summary)
        }
    }

    static func handoff(job: String, to ownerLane: String, task: String, contextRef: String?) {
        guard let u = URL(string: "\(base)/api/handoff") else { return }
        var refs = [String]()
        if let contextRef, !contextRef.isEmpty { refs.append(contextRef) }
        if refs.isEmpty { refs.append("coord://work/\(job)") }
        let body: [String: Any] = [
            "work_id": job,
            "to": ownerLane,
            "task": task.isEmpty ? "Continue \(job)" : task,
            "why": "Operator handoff from the native menu bar.",
            "acceptance": "Receiver has enough context to continue or return one targeted blocker.",
            "refs": refs,
            "constraints": ["Use coord.db lifecycle helpers; do not hand-edit projection JSON."],
            "title": "handoff to \(ownerLane): \(job)",
        ]
        postJSON(u, body: body)
    }

    private static func postOutcome(
        _ url: URL,
        success: String,
        done: ((HarnessControlOutcome) -> Void)?
    ) {
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.httpBody = Data()
        request.setValue("swift-\(UUID().uuidString)", forHTTPHeaderField: "X-Request-Id")
        session.dataTask(with: request) { data, response, error in
            if let error {
                done?(.init(ok: false, message: error.localizedDescription))
                return
            }
            guard let http = response as? HTTPURLResponse else {
                done?(.init(ok: false, message: "Control endpoint returned no HTTP response."))
                return
            }
            let object = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            let reason = object?["reason"] as? String
                ?? object?["error"] as? String
                ?? object?["message"] as? String
            guard (200..<300).contains(http.statusCode) else {
                done?(.init(ok: false, message: reason ?? "Control failed with HTTP \(http.statusCode)."))
                return
            }
            if object?["ok"] as? Bool == false {
                done?(.init(ok: false, message: reason ?? "Control endpoint refused the action."))
                return
            }
            done?(.init(ok: true, message: reason?.isEmpty == false ? reason! : success))
        }.resume()
    }

    private static func post(_ url: URL, _ done: ((Bool) -> Void)? = nil) {
        var r = URLRequest(url: url); r.httpMethod = "POST"; r.httpBody = Data()
        r.setValue("swift-\(UUID().uuidString)", forHTTPHeaderField: "X-Request-Id")
        session.dataTask(with: r) { _, resp, _ in
            let ok = (resp as? HTTPURLResponse).map { (200..<300).contains($0.statusCode) } ?? false
            done?(ok)
        }.resume()
    }

    private static func postData(_ url: URL, _ done: ((Data?, Bool) -> Void)? = nil) {
        var r = URLRequest(url: url); r.httpMethod = "POST"; r.httpBody = Data()
        r.timeoutInterval = 45
        r.setValue("swift-\(UUID().uuidString)", forHTTPHeaderField: "X-Request-Id")
        session.dataTask(with: r) { data, resp, _ in
            let ok = (resp as? HTTPURLResponse).map { (200..<300).contains($0.statusCode) } ?? false
            done?(data, ok)
        }.resume()
    }

    private static func capabilitySummary(job: String, action: String, data: Data?, transportOK: Bool) -> CapabilityResultSummary {
        guard transportOK, let data,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return CapabilityResultSummary(
                job: job,
                action: action,
                label: action,
                ok: false,
                statusText: "request failed",
                resultId: nil,
                updatedAt: Date()
            )
        }
        let ok = (obj["ok"] as? Bool) ?? false
        let label = (obj["label"] as? String) ?? action
        let resultId = obj["result_id"] as? String
        var status = (obj["reason"] as? String) ?? ""
        if status.isEmpty, let results = obj["results"] as? [[String: Any]], let first = results.first {
            status = (first["summary"] as? String) ?? ""
            if status.isEmpty {
                status = (first["label"] as? String) ?? ""
            }
        }
        if status.isEmpty, let warnings = obj["warnings"] as? [String], let first = warnings.first {
            status = first
        }
        if status.isEmpty {
            status = ok ? "ready in Harness" : "capability failed"
        }
        return CapabilityResultSummary(
            job: job,
            action: action,
            label: label,
            ok: ok,
            statusText: String(status.prefix(120)),
            resultId: resultId,
            updatedAt: Date()
        )
    }

    private static func postJSON(_ url: URL, body: [String: Any], _ done: ((Bool) -> Void)? = nil) {
        var r = URLRequest(url: url); r.httpMethod = "POST"
        r.httpBody = try? JSONSerialization.data(withJSONObject: body, options: [])
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        r.setValue("swift-\(UUID().uuidString)", forHTTPHeaderField: "X-Request-Id")
        session.dataTask(with: r) { _, resp, _ in
            let ok = (resp as? HTTPURLResponse).map { (200..<300).contains($0.statusCode) } ?? false
            done?(ok)
        }.resume()
    }
}
