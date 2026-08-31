import AppKit
import Foundation

enum LocalPowerSource: String, Equatable {
    case battery
    case adapter
}

enum LocalEnergyMode: Int, CaseIterable, Equatable {
    case automatic = 0
    case low = 1
    case high = 2

    var title: String {
        switch self {
        case .automatic: return "Automatic"
        case .low: return "Low Power"
        case .high: return "High Power"
        }
    }

    var symbolName: String {
        switch self {
        case .automatic: return "sparkles"
        case .low: return "battery.100percent"
        case .high: return "bolt.fill"
        }
    }
}

struct LocalBatterySnapshot: Equatable {
    let percent: Int?
    let source: String
    let detail: String
    let error: String?
    let chargeLimit: Int?
    let chargeLimitMutationAvailable: Bool
    let batteryModeRaw: Int?
    let adapterModeRaw: Int?
    let highPowerModeSupported: Bool
    let energyModeMutationAvailable: Bool

    init(
        percent: Int?,
        source: String,
        detail: String,
        error: String?,
        chargeLimit: Int? = nil,
        chargeLimitMutationAvailable: Bool = false,
        batteryModeRaw: Int? = nil,
        adapterModeRaw: Int? = nil,
        highPowerModeSupported: Bool = false,
        energyModeMutationAvailable: Bool = false
    ) {
        self.percent = percent
        self.source = source
        self.detail = detail
        self.error = error
        self.chargeLimit = chargeLimit
        self.chargeLimitMutationAvailable = chargeLimitMutationAvailable
        self.batteryModeRaw = batteryModeRaw
        self.adapterModeRaw = adapterModeRaw
        self.highPowerModeSupported = highPowerModeSupported
        self.energyModeMutationAvailable = energyModeMutationAvailable
    }

    /// Treat every exact value below 100 as a live charge lock. macOS may
    /// retain values such as 75% even though COORD only writes 80 or 100.
    var chargeLimitEnabled: Bool { chargeLimit.map { $0 < 100 } ?? false }
    var nextChargeLimit: Int { chargeLimitEnabled ? 100 : 80 }
    var adapterPowered: Bool { source == "Adapter" }
    var activePowerSource: LocalPowerSource? {
        if source == "Adapter" { return .adapter }
        if source == "Battery" { return .battery }
        return nil
    }
    var activeEnergyModeRaw: Int? {
        switch activePowerSource {
        case .battery: return batteryModeRaw
        case .adapter: return adapterModeRaw
        case nil: return nil
        }
    }

    static let unavailable = LocalBatterySnapshot(
        percent: nil,
        source: "Power source unavailable",
        detail: "Battery status unavailable",
        error: nil
    )
}

enum LocalEnergyModeParser {
    static func parse(_ text: String) -> (battery: Int, adapter: Int)? {
        var section: LocalPowerSource?
        var battery: Int?
        var adapter: Int?
        for raw in text.split(whereSeparator: \.isNewline) {
            let line = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            if line == "Battery Power:" { section = .battery; continue }
            if line == "AC Power:" { section = .adapter; continue }
            let fields = line.split(whereSeparator: \.isWhitespace)
            guard fields.count == 2, fields[0] == "powermode",
                  let value = Int(fields[1]), (0...2).contains(value) else { continue }
            if section == .battery { battery = value }
            if section == .adapter { adapter = value }
        }
        guard let battery, let adapter else { return nil }
        return (battery, adapter)
    }
}

enum LocalPowerFailure: Error, LocalizedError, Equatable {
    case command(String)

    var errorDescription: String? {
        switch self {
        case .command(let message): return message
        }
    }
}

enum LocalBatteryParser {
    static func parse(_ text: String) -> Result<LocalBatterySnapshot, LocalPowerFailure> {
        guard let percentIndex = text.firstIndex(of: "%"),
              let digits = text[..<percentIndex].split(whereSeparator: { !$0.isNumber }).last,
              let percent = Int(digits),
              (0...100).contains(percent)
        else {
            return .failure(.command("pmset did not return a valid battery percentage."))
        }

        let lower = text.lowercased()
        let source: String
        if lower.contains("ac power") {
            source = "Adapter"
        } else if lower.contains("battery power") {
            source = "Battery"
        } else {
            source = "Unknown source"
        }

        let detail: String
        if lower.contains("discharging") {
            detail = "Discharging"
        } else if lower.contains("finishing charge") {
            detail = "Finishing charge"
        } else if lower.contains("not charging") {
            detail = "Plugged in"
        } else if lower.contains("charged") {
            detail = "Charged"
        } else if lower.contains("charging") {
            detail = "Charging"
        } else {
            detail = "Charge state unknown"
        }
        return .success(LocalBatterySnapshot(
            percent: percent,
            source: source,
            detail: detail,
            error: nil
        ))
    }
}

enum LocalChargeLimitParser {
    static func parse(_ text: String) -> Int? {
        let values = text.split(whereSeparator: \.isNewline).compactMap { raw -> Int? in
            let line = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            guard line.hasPrefix("battlimit") || line.localizedCaseInsensitiveContains("charge limit") else {
                return nil
            }
            return line.split(whereSeparator: { !$0.isNumber }).compactMap { Int($0) }.last
        }.filter { (0...100).contains($0) }
        return values.count == 1 ? values[0] : nil
    }
}

enum LocalNativeChargeLimitParser {
    static func parse(_ text: String) -> Int? {
        var latest: Int?
        for raw in text.split(whereSeparator: \.isNewline) {
            let line = String(raw)
            if line.localizedCaseInsensitiveContains("Charge limit was disabled") {
                latest = 100
                continue
            }
            for pattern in [
                #"MCL target:\s*([0-9]{1,3})%"#,
                #"Charge limit was set to:\s*([0-9]{1,3})"#,
            ] {
                guard let regex = try? NSRegularExpression(pattern: pattern),
                      let match = regex.firstMatch(
                        in: line,
                        range: NSRange(line.startIndex..., in: line)
                      ),
                      let range = Range(match.range(at: 1), in: line),
                      let value = Int(line[range]),
                      (0...100).contains(value) else { continue }
                latest = value
            }
        }
        return latest
    }
}

@MainActor
final class LocalPowerController {
    typealias BatteryReader = () -> Result<LocalBatterySnapshot, LocalPowerFailure>
    typealias CaffeineLauncher = () throws -> (Process, Pipe)
    typealias ChargeLimitWriter = (Int, Int) -> Result<LocalBatterySnapshot, LocalPowerFailure>
    typealias EnergyModeWriter = (
        LocalPowerSource, LocalEnergyMode, Int, Int
    ) -> Result<LocalBatterySnapshot, LocalPowerFailure>

    private let batteryReader: BatteryReader
    private let caffeineLauncher: CaffeineLauncher
    private let chargeLimitWriter: ChargeLimitWriter
    private let energyModeWriter: EnergyModeWriter

    private var caffeineProcess: Process?
    private var caffeineErrorPipe: Pipe?
    private var caffeineStopRequested = false
    private var batteryRefreshInFlight = false
    private(set) var chargeLimitMutationInFlight = false
    private(set) var energyModeMutationInFlight = false

    private(set) var battery = LocalBatterySnapshot.unavailable
    private(set) var caffeineActive = false
    var onChange: ((LocalBatterySnapshot, Bool) -> Void)?
    var onOutcome: ((Bool, String) -> Void)?

    init(
        batteryReader: @escaping BatteryReader = LocalPowerController.readBattery,
        caffeineLauncher: @escaping CaffeineLauncher = LocalPowerController.makeCaffeineProcess,
        chargeLimitWriter: @escaping ChargeLimitWriter = LocalPowerController.writeChargeLimit,
        energyModeWriter: @escaping EnergyModeWriter = LocalPowerController.writeEnergyMode
    ) {
        self.batteryReader = batteryReader
        self.caffeineLauncher = caffeineLauncher
        self.chargeLimitWriter = chargeLimitWriter
        self.energyModeWriter = energyModeWriter
    }

    func refreshBattery() {
        guard !batteryRefreshInFlight else { return }
        batteryRefreshInFlight = true
        let reader = batteryReader
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let result = reader()
            DispatchQueue.main.async {
                guard let self else { return }
                self.batteryRefreshInFlight = false
                switch result {
                case .success(let snapshot):
                    self.battery = snapshot
                case .failure(let error):
                    let message = error.localizedDescription
                    self.battery = LocalBatterySnapshot(
                        percent: self.battery.percent,
                        source: self.battery.source,
                        detail: self.battery.detail,
                        error: message
                    )
                    self.onOutcome?(false, message)
                }
                self.publish()
            }
        }
    }

    func toggleChargeLimit(expected: Int, target: Int) {
        guard !chargeLimitMutationInFlight else {
            onOutcome?(false, "A charge-limit change is already pending.")
            return
        }
        guard battery.chargeLimitMutationAvailable,
              battery.chargeLimit == expected,
              target == (expected < 100 ? 100 : 80) else {
            onOutcome?(false, "Charge-limit readback changed or the installed COORD Shortcut is unavailable.")
            return
        }
        chargeLimitMutationInFlight = true
        let writer = chargeLimitWriter
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let result = writer(target, expected)
            DispatchQueue.main.async {
                guard let self else { return }
                self.chargeLimitMutationInFlight = false
                switch result {
                case .success(let snapshot):
                    self.battery = snapshot
                    self.publish()
                    self.onOutcome?(true, "Charge Limit · \(target)% verified")
                case .failure(let failure):
                    self.onOutcome?(false, failure.localizedDescription)
                    self.refreshBattery()
                }
            }
        }
    }

    func setEnergyMode(source: LocalPowerSource, mode: LocalEnergyMode, expected: Int) {
        guard !energyModeMutationInFlight else {
            onOutcome?(false, "An Energy Mode change is already pending.")
            return
        }
        let selected = source == .battery ? battery.batteryModeRaw : battery.adapterModeRaw
        let other = source == .battery ? battery.adapterModeRaw : battery.batteryModeRaw
        guard battery.energyModeMutationAvailable, selected == expected,
              let other, (0...2).contains(expected),
              mode != .high || battery.highPowerModeSupported else {
            onOutcome?(false, "Energy Mode readback changed or this mode is unavailable.")
            return
        }
        guard mode.rawValue != expected else { return }
        energyModeMutationInFlight = true
        let writer = energyModeWriter
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let result = writer(source, mode, expected, other)
            DispatchQueue.main.async {
                guard let self else { return }
                self.energyModeMutationInFlight = false
                switch result {
                case .success(let snapshot):
                    self.battery = snapshot
                    self.publish()
                    let sourceTitle = source == .battery ? "Battery" : "Adapter"
                    self.onOutcome?(true, "\(sourceTitle) · \(mode.title) verified")
                case .failure(let failure):
                    self.onOutcome?(false, failure.localizedDescription)
                    self.refreshBattery()
                }
            }
        }
    }

    func toggleCaffeine() {
        if let process = caffeineProcess, process.isRunning {
            caffeineStopRequested = true
            process.terminate()
            DispatchQueue.global(qos: .utility).async { [weak self] in
                process.waitUntilExit()
                DispatchQueue.main.async {
                    guard let self, self.caffeineProcess === process else { return }
                    self.caffeineProcess = nil
                    self.caffeineErrorPipe = nil
                    self.caffeineActive = false
                    self.caffeineStopRequested = false
                    self.publish()
                    self.onOutcome?(true, "COORD Caffeine off · process stopped")
                }
            }
            return
        }

        do {
            let (process, stderr) = try caffeineLauncher()
            try process.run()
            caffeineProcess = process
            caffeineErrorPipe = stderr
            caffeineStopRequested = false
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) { [weak self] in
                guard let self, self.caffeineProcess === process else { return }
                guard process.isRunning else {
                    let data = stderr.fileHandleForReading.readDataToEndOfFile()
                    let detail = String(data: data, encoding: .utf8)?
                        .trimmingCharacters(in: .whitespacesAndNewlines)
                    self.caffeineProcess = nil
                    self.caffeineErrorPipe = nil
                    self.caffeineActive = false
                    self.publish()
                    self.onOutcome?(false, detail?.isEmpty == false ? detail! : "caffeinate exited before the lease became active.")
                    return
                }
                self.caffeineActive = true
                self.publish()
                self.onOutcome?(true, "COORD Caffeine on · process verified running")
                process.terminationHandler = { [weak self] ended in
                    DispatchQueue.main.async {
                        guard let self, self.caffeineProcess === ended else { return }
                        let expected = self.caffeineStopRequested
                        self.caffeineProcess = nil
                        self.caffeineErrorPipe = nil
                        self.caffeineActive = false
                        self.caffeineStopRequested = false
                        self.publish()
                        if !expected {
                            self.onOutcome?(false, "COORD Caffeine stopped unexpectedly (status \(ended.terminationStatus)).")
                        }
                    }
                }
            }
        } catch {
            caffeineProcess = nil
            caffeineErrorPipe = nil
            caffeineActive = false
            publish()
            onOutcome?(false, "Could not start COORD Caffeine: \(error.localizedDescription)")
        }
    }

    func stopForTermination() {
        caffeineStopRequested = true
        caffeineProcess?.terminate()
        caffeineProcess = nil
        caffeineErrorPipe = nil
        caffeineActive = false
    }

    private func publish() {
        onChange?(battery, caffeineActive)
    }

    nonisolated private static func readBattery() -> Result<LocalBatterySnapshot, LocalPowerFailure> {
        let batteryResult = run(executable: "/usr/bin/pmset", arguments: ["-g", "batt"])
        guard batteryResult.exitCode == 0 else {
            return .failure(.command(batteryResult.output.isEmpty ? "pmset battery read failed." : batteryResult.output))
        }
        guard case .success(let base) = LocalBatteryParser.parse(batteryResult.output) else {
            return LocalBatteryParser.parse(batteryResult.output)
        }
        let limit = readChargeLimit()
        let shortcuts = run(executable: "/usr/bin/shortcuts", arguments: ["list"])
        let shortcutReady = shortcuts.exitCode == 0 && shortcuts.output
            .split(whereSeparator: \.isNewline)
            .contains { $0.trimmingCharacters(in: .whitespacesAndNewlines) == "COORD Set Battery Charge Limit" }
        let modesResult = run(executable: "/usr/bin/pmset", arguments: ["-g", "custom"])
        let modes = modesResult.exitCode == 0 ? LocalEnergyModeParser.parse(modesResult.output) : nil
        let capabilities = run(executable: "/usr/bin/pmset", arguments: ["-g", "cap"])
        let highPowerSupported = capabilities.exitCode == 0
            && capabilities.output.split(whereSeparator: \.isWhitespace).contains("highpowermode")
        return .success(LocalBatterySnapshot(
            percent: base.percent,
            source: base.source,
            detail: base.detail,
            error: base.error,
            chargeLimit: limit,
            chargeLimitMutationAvailable: limit != nil && shortcutReady,
            batteryModeRaw: modes?.battery,
            adapterModeRaw: modes?.adapter,
            highPowerModeSupported: highPowerSupported,
            energyModeMutationAvailable: modes != nil
        ))
    }

    nonisolated private static func writeEnergyMode(
        source: LocalPowerSource,
        mode: LocalEnergyMode,
        expected: Int,
        otherExpected: Int
    ) -> Result<LocalBatterySnapshot, LocalPowerFailure> {
        guard (0...2).contains(expected), (0...2).contains(otherExpected), mode.rawValue != expected else {
            return .failure(.command("Energy Mode request was not valid."))
        }
        let pre = run(executable: "/usr/bin/pmset", arguments: ["-g", "custom"])
        guard pre.exitCode == 0, let before = LocalEnergyModeParser.parse(pre.output) else {
            return .failure(.command("Current Energy Mode readback is unavailable."))
        }
        let selectedBefore = source == .battery ? before.battery : before.adapter
        let otherBefore = source == .battery ? before.adapter : before.battery
        guard selectedBefore == expected, otherBefore == otherExpected else {
            return .failure(.command("Energy Mode changed before confirmation. Refresh and try again."))
        }
        let flag = source == .battery ? "-b" : "-c"
        let command = "/usr/bin/pmset \(flag) powermode \(mode.rawValue)"
        let script = "do shell script \(String(reflecting: command)) with administrator privileges"
        let write = run(executable: "/usr/bin/osascript", arguments: ["-e", script])
        guard write.exitCode == 0 else {
            return .failure(.command(write.output.isEmpty ? "macOS rejected the Energy Mode change." : write.output))
        }
        for attempt in 0..<8 {
            if attempt > 0 { usleep(250_000) }
            let post = run(executable: "/usr/bin/pmset", arguments: ["-g", "custom"])
            guard post.exitCode == 0, let after = LocalEnergyModeParser.parse(post.output) else { continue }
            let selectedAfter = source == .battery ? after.battery : after.adapter
            let otherAfter = source == .battery ? after.adapter : after.battery
            if selectedAfter == mode.rawValue, otherAfter == otherExpected {
                return readBattery()
            }
        }
        return .failure(.command("The change may have applied, but exact Energy Mode readback did not verify it."))
    }

    nonisolated private static func writeChargeLimit(
        target: Int,
        expected: Int
    ) -> Result<LocalBatterySnapshot, LocalPowerFailure> {
        guard [80, 100].contains(target), (0...100).contains(expected), target != expected else {
            return .failure(.command("Charge Limit supports only verified 80% / 100% toggles."))
        }
        guard readChargeLimit() == expected else {
            return .failure(.command("Charge Limit changed before confirmation. Refresh and try again."))
        }
        let inputURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("org.coordharness.charge-limit.\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: inputURL) }
        do {
            try Data("\(target)".utf8).write(to: inputURL, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: inputURL.path)
        } catch {
            return .failure(.command("Could not prepare the charge-limit request."))
        }
        let write = run(
            executable: "/usr/bin/shortcuts",
            arguments: [
                "run", "COORD Set Battery Charge Limit",
                "--input-path", inputURL.path,
            ]
        )
        guard write.exitCode == 0 else {
            return .failure(.command(write.output.isEmpty ? "Apple Shortcuts rejected the Charge Limit change." : write.output))
        }
        var verified = false
        for attempt in 0..<8 {
            if attempt > 0 { usleep(250_000) }
            if readChargeLimit() == target {
                verified = true
                break
            }
        }
        guard verified else {
            return .failure(.command("The change may have applied, but exact Charge Limit readback did not verify it."))
        }
        return readBattery()
    }

    nonisolated private static func readChargeLimit() -> Int? {
        let native = run(
            executable: "/usr/bin/log",
            arguments: [
                "show", "--last", "5m", "--style", "compact", "--predicate",
                "process == \"PowerUIAgent\"",
            ]
        )
        if native.exitCode == 0, let value = LocalNativeChargeLimitParser.parse(native.output) {
            return value
        }
        let legacy = run(executable: "/usr/bin/pmset", arguments: ["-g", "battlimit"])
        guard legacy.exitCode == 0 else { return nil }
        return LocalChargeLimitParser.parse(legacy.output)
    }

    nonisolated private static func run(
        executable: String,
        arguments: [String]
    ) -> (exitCode: Int32, output: String) {
        let process = Process()
        let output = Pipe()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.standardOutput = output
        process.standardError = output
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return (78, error.localizedDescription)
        }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        let text = String(decoding: data, as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return (process.terminationStatus, text)
    }

    nonisolated private static func makeCaffeineProcess
() throws -> (Process, Pipe) {
        let process = Process()
        let failure = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/caffeinate")
        process.arguments = ["-dimsu"]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = failure
        return (process, failure)
    }
}

enum CoordBatteryStatusItemPresentation {
    static let width: CGFloat = 23
    static let height: CGFloat = 17

    static func tint(for snapshot: LocalBatterySnapshot) -> NSColor {
        snapshot.adapterPowered ? .systemGreen : .labelColor
    }

    static func glyph(for snapshot: LocalBatterySnapshot) -> String {
        guard snapshot.percent != nil else { return "minus" }
        if snapshot.adapterPowered,
           snapshot.chargeLimitEnabled,
           let percent = snapshot.percent,
           let limit = snapshot.chargeLimit,
           percent >= limit { return "pause.fill" }
        if snapshot.adapterPowered,
           snapshot.detail.localizedCaseInsensitiveContains("charging") { return "bolt.fill" }
        return snapshot.adapterPowered ? "powerplug.fill" : "arrow.down"
    }

    static func image(for snapshot: LocalBatterySnapshot) -> NSImage {
        let image = NSImage(size: NSSize(width: width, height: height), flipped: false) { _ in
            let body = NSRect(x: 0.25, y: 2, width: width - 2.5, height: 13)
            let terminal = NSRect(x: body.maxX + 0.25, y: body.midY - 2.25, width: 1.35, height: 5)
            let shell = NSBezierPath(roundedRect: body, xRadius: 4, yRadius: 4)
            NSColor.secondaryLabelColor.withAlphaComponent(0.42).setFill()
            shell.fill()
            NSBezierPath(roundedRect: terminal, xRadius: 0.6, yRadius: 0.6).fill()
            let fraction = CGFloat(min(100, max(0, snapshot.percent ?? 0))) / 100
            if fraction > 0 {
                NSGraphicsContext.saveGraphicsState()
                shell.addClip()
                tint(for: snapshot).withAlphaComponent(snapshot.adapterPowered ? 0.94 : 0.70).setFill()
                NSRect(x: body.minX, y: body.minY, width: body.width * fraction, height: body.height).fill()
                NSGraphicsContext.restoreGraphicsState()
            }
            NSColor.labelColor.withAlphaComponent(0.14).setStroke()
            shell.lineWidth = 0.5
            shell.stroke()

            let foreground = snapshot.adapterPowered ? NSColor.white : NSColor.textColor
            let paragraph = NSMutableParagraphStyle()
            paragraph.alignment = .center
            NSString(string: snapshot.percent.map(String.init) ?? "–").draw(
                in: NSRect(x: 0.5, y: 2.4, width: 15.3, height: 12.5),
                withAttributes: [
                    .font: NSFont.monospacedDigitSystemFont(
                        ofSize: (snapshot.percent.map(String.init) ?? "–").count > 2 ? 8.7 : 10.3,
                        weight: .bold
                    ),
                    .foregroundColor: foreground,
                    .paragraphStyle: paragraph,
                ]
            )
            let symbolConfig = NSImage.SymbolConfiguration(pointSize: 5.75, weight: .bold)
                .applying(NSImage.SymbolConfiguration(paletteColors: [foreground]))
            NSImage(systemSymbolName: glyph(for: snapshot), accessibilityDescription: nil)?
                .withSymbolConfiguration(symbolConfig)?
                .draw(in: NSRect(x: 16.1, y: 4.5, width: 4, height: 7.5))
            return true
        }
        image.isTemplate = false
        return image
    }
}

@MainActor
final class CoordBatteryStatusItemController {
    var onRefresh: (() -> Void)?
    var onToggleChargeLimit: ((Int, Int) -> Void)?
    var onSetEnergyMode: ((LocalPowerSource, LocalEnergyMode, Int) -> Void)?

    private var statusItem: NSStatusItem?
    private let popover = NSPopover()
    private var snapshot = LocalBatterySnapshot.unavailable

    init() {
        popover.behavior = .transient
        popover.animates = false
        popover.appearance = NSAppearance(named: .vibrantDark)
    }

    func setEnabled(_ enabled: Bool) {
        if enabled {
            guard statusItem == nil else { return }
            let item = NSStatusBar.system.statusItem(
                withLength: CoordBatteryStatusItemPresentation.width
            )
            item.autosaveName = "org.coordharness.menubar.battery"
            if let button = item.button {
                button.target = self
                button.action = #selector(toggle)
                button.sendAction(on: [.leftMouseUp])
                button.imagePosition = .imageOnly
            }
            statusItem = item
            renderStatusItem()
        } else if let item = statusItem {
            popover.close()
            NSStatusBar.system.removeStatusItem(item)
            statusItem = nil
        }
    }

    func update(_ snapshot: LocalBatterySnapshot) {
        self.snapshot = snapshot
        renderStatusItem()
        if popover.isShown { installDetails() }
    }

    func shutdown() { setEnabled(false) }

    private func renderStatusItem() {
        guard let button = statusItem?.button else { return }
        statusItem?.length = CoordBatteryStatusItemPresentation.width
        let percent = snapshot.percent
        button.image = CoordBatteryStatusItemPresentation.image(for: snapshot)
        button.title = ""
        button.attributedTitle = NSAttributedString(string: "")
        button.toolTip = "Battery \(percent.map { "\($0)%" } ?? "unavailable") · \(snapshot.source) · \(snapshot.detail)"
        button.setAccessibilityLabel("Battery \(percent.map { "\($0) percent" } ?? "unavailable"). Open battery details")
    }

    private func installDetails() {
        let view = CoordBatteryDetailsView(snapshot: snapshot)
        view.onRefresh = { [weak self] in self?.onRefresh?() }
        view.onClose = { [weak self] in self?.popover.close() }
        view.onToggleChargeLimit = { [weak self] expected, target in
            self?.onToggleChargeLimit?(expected, target)
        }
        view.onSetEnergyMode = { [weak self] source, mode, expected in
            self?.onSetEnergyMode?(source, mode, expected)
        }
        let controller = NSViewController()
        controller.view = view
        popover.contentViewController = controller
        popover.contentSize = view.bounds.size
    }

    @objc private func toggle() {
        guard let button = statusItem?.button else { return }
        if popover.isShown {
            popover.close()
        } else {
            installDetails()
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        }
    }
}

struct CoordPowerControlsLayout: Equatable {
    static let height: CGFloat = 30
    static let caffeineFrame = NSRect(x: 0, y: 3, width: 24, height: 24)
    static let batteryFrame = NSRect(x: 24, y: 3, width: 30, height: 24)
    static let sliderFrame = NSRect(x: 56, y: 3, width: 58, height: 24)
    static let width: CGFloat = sliderFrame.maxX

    static var controlRects: [NSRect] { [caffeineFrame, batteryFrame, sliderFrame] }
}

final class CoordCaffeineButton: NSButton {
    var isActive = false { didSet { needsDisplay = true } }

    override func draw(_ dirtyRect: NSRect) {
        let alpha: CGFloat = isEnabled ? (isHighlighted ? 0.78 : 1) : 0.34
        (isActive ? NSColor(white: 0.94, alpha: alpha) : NSColor(white: 0.20, alpha: alpha)).setFill()
        NSBezierPath(ovalIn: NSRect(x: 6, y: 6, width: 12, height: 12)).fill()
        let score = NSBezierPath()
        if isActive {
            score.move(to: NSPoint(x: 12, y: 6.6))
            score.line(to: NSPoint(x: 12, y: 17.4))
        } else {
            score.move(to: NSPoint(x: 6.6, y: 12))
            score.line(to: NSPoint(x: 17.4, y: 12))
        }
        NSColor.black.withAlphaComponent(isActive ? 0.62 : 0.38).setStroke()
        score.lineWidth = 1.15
        score.stroke()
    }
}

final class CoordBatteryButton: NSButton {
    let snapshot: LocalBatterySnapshot

    init(frame: NSRect, snapshot: LocalBatterySnapshot) {
        self.snapshot = snapshot
        super.init(frame: frame)
        title = ""
        isBordered = false
        focusRingType = .none
        identifier = NSUserInterfaceItemIdentifier("coord.header.battery")
        let percent = snapshot.percent.map { "\($0)%" } ?? "—"
        let limitState = snapshot.chargeLimitEnabled ? "on" : "off"
        setAccessibilityLabel("Battery \(percent), 80 percent charge limit \(limitState)")
        setAccessibilityHelp(snapshot.chargeLimitMutationAvailable
            ? (snapshot.chargeLimitEnabled ? "Turn off 80% charge limit" : "Turn on 80% charge limit")
            : "Charge-limit control is unavailable until exact readback and the installed COORD Shortcut are ready")
        toolTip = snapshot.chargeLimitMutationAvailable
            ? (snapshot.chargeLimitEnabled ? "Turn off 80% charge limit" : "Turn on 80% charge limit")
            : "Charge-limit control unavailable"
    }

    override func draw(_ dirtyRect: NSRect) {
        let tint = Tokens.Color.lightGray.withAlphaComponent(isHighlighted ? 0.58 : 0.86)
        let body = NSRect(x: 6, y: 8, width: 16, height: 8)
        let terminal = NSRect(x: body.maxX + 0.6, y: body.midY - 1.5, width: 1.2, height: 3)
        tint.setStroke()
        let outline = NSBezierPath(roundedRect: body, xRadius: 2.4, yRadius: 2.4)
        outline.lineWidth = 0.75
        outline.stroke()
        tint.setFill()
        NSBezierPath(roundedRect: terminal, xRadius: 0.8, yRadius: 0.8).fill()
        if snapshot.chargeLimitEnabled {
            let pause = NSBezierPath()
            for x in [body.midX - 1.5, body.midX + 1.5] {
                pause.move(to: NSPoint(x: x, y: body.minY + 2.2))
                pause.line(to: NSPoint(x: x, y: body.maxY - 2.2))
            }
            pause.lineWidth = 0.75
            NSColor.white.withAlphaComponent(0.94).setStroke()
            pause.stroke()
        }
    }

    required init?(coder: NSCoder) { nil }
}

final class CoordPowerControlsView: NSView {
    var onToggleChargeLimit: ((Int, Int) -> Void)?
    var onToggleCaffeine: (() -> Void)?
    var onSetMode: ((String) -> Void)?

    init(
        frame: NSRect,
        battery: LocalBatterySnapshot,
        caffeineActive: Bool,
        mode: String
    ) {
        super.init(frame: frame)
        let caffeine = CoordCaffeineButton(frame: CoordPowerControlsLayout.caffeineFrame)
        caffeine.title = ""
        caffeine.isBordered = false
        caffeine.focusRingType = .none
        caffeine.isActive = caffeineActive
        caffeine.identifier = NSUserInterfaceItemIdentifier("coord.header.caffeine")
        caffeine.setAccessibilityLabel(caffeineActive ? "Caffeine on" : "Caffeine off")
        caffeine.toolTip = caffeineActive ? "Turn off COORD Caffeine" : "Turn on COORD Caffeine"
        caffeine.target = self
        caffeine.action = #selector(toggleCaffeine)
        addSubview(caffeine)

        let batteryButton = CoordBatteryButton(
            frame: CoordPowerControlsLayout.batteryFrame,
            snapshot: battery
        )
        batteryButton.target = self
        batteryButton.action = #selector(toggleChargeLimit)
        batteryButton.isEnabled = battery.chargeLimitMutationAvailable
        addSubview(batteryButton)

        let slider = ModeSlider(frame: CoordPowerControlsLayout.sliderFrame)
        slider.identifier = NSUserInterfaceItemIdentifier("coord.header.mode-control")
        slider.setLiveMode(mode, paused: mode == "pause")
        slider.onSetMode = { [weak self] in self?.onSetMode?($0) }
        addSubview(slider)
    }

    @objc private func toggleChargeLimit() {
        guard let button = subviews.first(where: {
            $0.identifier?.rawValue == "coord.header.battery"
        }) as? CoordBatteryButton,
        let expected = button.snapshot.chargeLimit else { return }
        onToggleChargeLimit?(expected, button.snapshot.nextChargeLimit)
    }
    @objc private func toggleCaffeine() { onToggleCaffeine?() }
    required init?(coder: NSCoder) { nil }
}

final class CoordBatteryMeterView: NSView {
    let snapshot: LocalBatterySnapshot

    init(frame: NSRect, snapshot: LocalBatterySnapshot) {
        self.snapshot = snapshot
        super.init(frame: frame)
        setAccessibilityElement(true)
        setAccessibilityLabel("Battery \(snapshot.percent.map { "\($0) percent" } ?? "unavailable")")
    }

    override func draw(_ dirtyRect: NSRect) {
        let body = NSRect(x: 0.5, y: 3, width: bounds.width - 5, height: bounds.height - 6)
        let shell = NSBezierPath(roundedRect: body, xRadius: 5, yRadius: 5)
        NSColor.labelColor.withAlphaComponent(0.14).setFill()
        shell.fill()
        if let percent = snapshot.percent {
            NSGraphicsContext.saveGraphicsState()
            shell.addClip()
            NSColor.systemGreen.withAlphaComponent(0.80).setFill()
            NSRect(x: body.minX, y: body.minY, width: body.width * CGFloat(percent) / 100, height: body.height).fill()
            NSGraphicsContext.restoreGraphicsState()
        }
        NSColor.labelColor.withAlphaComponent(0.42).setStroke()
        shell.lineWidth = 0.75
        shell.stroke()
        NSColor.labelColor.withAlphaComponent(0.34).setFill()
        NSBezierPath(roundedRect: NSRect(x: body.maxX + 1.2, y: body.midY - 2.5, width: 2, height: 5), xRadius: 1, yRadius: 1).fill()
        let text = snapshot.percent.map { "\($0)%" } ?? "—%"
        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .bold),
            .foregroundColor: NSColor.white.withAlphaComponent(0.95),
        ]
        let size = text.size(withAttributes: attributes)
        text.draw(at: NSPoint(x: body.midX - size.width / 2, y: body.midY - size.height / 2), withAttributes: attributes)
    }

    required init?(coder: NSCoder) { nil }
}

final class CoordBatteryDetailsView: NSView {
    var onRefresh: (() -> Void)?
    var onClose: (() -> Void)?
    var onToggleChargeLimit: ((Int, Int) -> Void)?
    var onSetEnergyMode: ((LocalPowerSource, LocalEnergyMode, Int) -> Void)?

    private let snapshot: LocalBatterySnapshot
    private var modeBindings: [(button: NSButton, source: LocalPowerSource, mode: LocalEnergyMode)] = []

    init(snapshot: LocalBatterySnapshot) {
        self.snapshot = snapshot
        super.init(frame: NSRect(x: 0, y: 0, width: 244, height: snapshot.error == nil ? 132 : 150))
        let meter = CoordBatteryMeterView(
            frame: NSRect(x: 12, y: 72, width: 164, height: 28),
            snapshot: snapshot
        )
        meter.identifier = NSUserInterfaceItemIdentifier("coord.battery.meter")
        addSubview(meter)

        let chargeAction = NSButton(frame: meter.frame)
        chargeAction.title = ""
        chargeAction.isBordered = false
        chargeAction.focusRingType = .none
        chargeAction.identifier = NSUserInterfaceItemIdentifier("coord.battery.charge-limit")
        chargeAction.target = self
        chargeAction.action = #selector(chargeLimitPressed)
        chargeAction.isEnabled = snapshot.chargeLimitMutationAvailable && snapshot.chargeLimit != nil
        chargeAction.toolTip = snapshot.chargeLimitEnabled
            ? "Turn off the current charge limit"
            : "Turn on the 80% charge limit"
        addSubview(chargeAction)

        let refresh = iconButton("arrow.clockwise", action: #selector(refreshPressed), label: "Refresh battery")
        refresh.identifier = NSUserInterfaceItemIdentifier("coord.battery.refresh")
        refresh.frame = NSRect(x: 184, y: 78, width: 16, height: 16)
        addSubview(refresh)

        let close = iconButton("xmark", action: #selector(closePressed), label: "Close battery details")
        close.identifier = NSUserInterfaceItemIdentifier("coord.battery.close")
        close.frame = NSRect(x: 216, y: 78, width: 16, height: 16)
        addSubview(close)

        let detail = NSTextField(labelWithString: "\(snapshot.source) · \(snapshot.detail)")
        detail.font = .systemFont(ofSize: 9.5, weight: .medium)
        detail.textColor = Tokens.Color.lightGray
        detail.frame = NSRect(x: 12, y: 108, width: 220, height: 16)
        addSubview(detail)

        addModeRow(source: .battery, rawValue: snapshot.batteryModeRaw, y: 38)
        addModeRow(source: .adapter, rawValue: snapshot.adapterModeRaw, y: 7)

        if let error = snapshot.error {
            let failure = NSTextField(labelWithString: error)
            failure.identifier = NSUserInterfaceItemIdentifier("coord.battery.failure")
            failure.font = .systemFont(ofSize: 9, weight: .medium)
            failure.textColor = .systemOrange
            failure.lineBreakMode = .byTruncatingTail
            failure.toolTip = error
            failure.frame = NSRect(x: 12, y: 128, width: 220, height: 16)
            addSubview(failure)
        }
    }

    private func addModeRow(source: LocalPowerSource, rawValue: Int?, y: CGFloat) {
        let sourceTitle = source == .battery ? "BATTERY" : "ADAPTER"
        let symbol = source == .battery ? "battery.100percent" : "powerplug.fill"
        let icon = NSImageView(frame: NSRect(x: 16, y: y + 4, width: 16, height: 16))
        icon.image = NSImage(systemSymbolName: symbol, accessibilityDescription: sourceTitle)
        icon.contentTintColor = Tokens.Color.lightGray
        addSubview(icon)
        let label = NSTextField(labelWithString: sourceTitle)
        label.font = .systemFont(ofSize: 9.5, weight: .semibold)
        label.textColor = Tokens.Color.lightGray
        label.frame = NSRect(x: 40, y: y + 3, width: 64, height: 18)
        addSubview(label)

        for (index, mode) in LocalEnergyMode.allCases.enumerated() {
            let button = iconButton(
                mode.symbolName,
                action: #selector(modePressed(_:)),
                label: "Set \(sourceTitle) Energy Mode to \(mode.title)"
            )
            button.identifier = NSUserInterfaceItemIdentifier("coord.battery.\(source.rawValue).mode.\(mode.rawValue)")
            button.frame = NSRect(x: 148 + CGFloat(index) * 28, y: y + 2, width: 20, height: 20)
            button.focusRingType = .none
            let selected = rawValue == mode.rawValue
            button.contentTintColor = selected ? .white : Tokens.Color.dimGray.withAlphaComponent(0.58)
            button.isEnabled = snapshot.energyModeMutationAvailable
                && rawValue != nil
                && (mode != .high || snapshot.highPowerModeSupported)
            button.setAccessibilityValue(selected ? "Selected" : "Not selected")
            button.toolTip = button.isEnabled
                ? "\(sourceTitle) · \(mode.title)"
                : "Energy Mode is unavailable until exact readback and authorization are ready"
            addSubview(button)
            modeBindings.append((button, source, mode))
        }
    }

    private func iconButton(_ symbol: String, action: Selector, label: String) -> NSButton {
        let button = NSButton(frame: .zero)
        button.title = ""
        button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: label)
        button.imagePosition = .imageOnly
        button.isBordered = false
        button.contentTintColor = Tokens.Color.dimGray
        button.target = self
        button.action = action
        button.setAccessibilityLabel(label)
        return button
    }

    @objc private func refreshPressed() { onRefresh?() }
    @objc private func closePressed() { onClose?() }
    @objc private func chargeLimitPressed() {
        guard let expected = snapshot.chargeLimit, snapshot.chargeLimitMutationAvailable else { return }
        onToggleChargeLimit?(expected, snapshot.nextChargeLimit)
    }
    @objc private func modePressed(_ sender: NSButton) {
        guard let binding = modeBindings.first(where: { $0.button === sender }) else { return }
        let expected = binding.source == .battery ? snapshot.batteryModeRaw : snapshot.adapterModeRaw
        guard snapshot.energyModeMutationAvailable,
              let expected,
              expected != binding.mode.rawValue,
              binding.mode != .high || snapshot.highPowerModeSupported else { return }
        onSetEnergyMode?(binding.source, binding.mode, expected)
    }
    required init?(coder: NSCoder) { nil }
}
