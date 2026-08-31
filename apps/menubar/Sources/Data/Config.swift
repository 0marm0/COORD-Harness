import Foundation


struct Config: Codable {

    var hotkey: Hotkey = .init()
    var refreshSecs: Double = 2
    var nextVisible: Int = 9
    var expandCount: Int = 10
    var glassMaterial: String = "under_window"
    var glassAlpha: Double = 0.0

    var notifications: Bool = true
    var attentionCollapsed: Bool = true
    var followupCollapsed: Bool = true
    var localQueueCollapsed: Bool = true
    var stayOpen: Bool = false
    var fetchTimeoutSecs: Double = 8


    var statusItemMode: String = UsageStatusMode.bars.rawValue
    var usageMetricMode: String = UsageMetricMode.auto.rawValue
    var usageSessionThreshold: Double = 50
    var usageShowResetETA: Bool = true
    var usageShowRunoutETA: Bool = true
    var usageBarsShowUsed: Bool = false
    var usageBarPalette: String = UsageBarPalette.colored.rawValue
    var usageWarningMarkersVisible: Bool = true
    var usageHistoryDays: Int = 30
    var usageWarningThreshold: Double = 20
    var showVitalsInPopover: Bool = false
    var systemTelemetryEnabled: Bool = true
    var systemTelemetryInPopover: Bool = true
    var systemTelemetryInStatusItem: Bool = true
    var batteryStatusItemEnabled: Bool = false
    var systemTelemetryStatusPreferenceVersion: Int = 1
    var systemTelemetryInCockpit: Bool = true
    var systemTelemetryProfile: String = "balanced"
    var systemTelemetryCompactSpacing: Bool = true
    var systemTelemetrySpacingPreferenceVersion: Int = 1
    var systemTelemetryShowCPU: Bool = true
    var systemTelemetryShowGPU: Bool = true
    var systemTelemetryShowRAM: Bool = true
    var systemTelemetryShowDisk: Bool = true
    var systemTelemetryWarningThreshold: Double = SystemTelemetryDisplayPolicy.defaultWarningThreshold
    var systemTelemetryCriticalThreshold: Double = SystemTelemetryDisplayPolicy.defaultCriticalThreshold
    var launchAtLogin: Bool = false
    var transport: String = "db"
    var slowRingTick: Bool = false
    var slowRingInterval: Double = 12
    var taskActionsEnabled: Bool = true
    var backgroundTickEnabled: Bool = false
    var panelDetached: Bool = false
    var panelAlwaysOnTop: Bool = false
    var usagePeekCollapsed: Bool = false

    struct Hotkey: Codable {
        var key: String = "comma"
        var mods: [String] = ["cmd"]
    }

    init() {}

    init(from decoder: Decoder) throws {
        let fallback = Config()
        let container = try decoder.container(keyedBy: CodingKeys.self)
        hotkey = try container.decodeIfPresent(Hotkey.self, forKey: .hotkey) ?? fallback.hotkey
        refreshSecs = try container.decodeIfPresent(Double.self, forKey: .refreshSecs) ?? fallback.refreshSecs
        nextVisible = try container.decodeIfPresent(Int.self, forKey: .nextVisible) ?? fallback.nextVisible
        expandCount = try container.decodeIfPresent(Int.self, forKey: .expandCount) ?? fallback.expandCount
        let storedGlassMaterial = try container.decodeIfPresent(String.self, forKey: .glassMaterial)
        let storedGlassAlpha = try container.decodeIfPresent(Double.self, forKey: .glassAlpha)
        // Migrate the former shipped defaults, which created Coord's gray double-material panel.
        // Preserve any genuinely customized combination.
        if storedGlassMaterial == "hud", storedGlassAlpha == 0.72 {
            glassMaterial = fallback.glassMaterial
            glassAlpha = fallback.glassAlpha
        } else {
            glassMaterial = storedGlassMaterial ?? fallback.glassMaterial
            glassAlpha = storedGlassAlpha ?? fallback.glassAlpha
        }
        notifications = try container.decodeIfPresent(Bool.self, forKey: .notifications) ?? fallback.notifications
        attentionCollapsed = try container.decodeIfPresent(Bool.self, forKey: .attentionCollapsed) ?? fallback.attentionCollapsed
        followupCollapsed = try container.decodeIfPresent(Bool.self, forKey: .followupCollapsed) ?? fallback.followupCollapsed
        localQueueCollapsed = try container.decodeIfPresent(Bool.self, forKey: .localQueueCollapsed) ?? fallback.localQueueCollapsed
        stayOpen = try container.decodeIfPresent(Bool.self, forKey: .stayOpen) ?? fallback.stayOpen
        fetchTimeoutSecs = try container.decodeIfPresent(Double.self, forKey: .fetchTimeoutSecs) ?? fallback.fetchTimeoutSecs
        statusItemMode = UsageStatusMode.resolve(
            try container.decodeIfPresent(String.self, forKey: .statusItemMode)
        ).rawValue
        usageMetricMode = UsageMetricMode.resolve(
            try container.decodeIfPresent(String.self, forKey: .usageMetricMode)
        ).rawValue
        usageSessionThreshold = min(100, max(0, try container.decodeIfPresent(Double.self, forKey: .usageSessionThreshold) ?? fallback.usageSessionThreshold))
        usageShowResetETA = try container.decodeIfPresent(Bool.self, forKey: .usageShowResetETA) ?? fallback.usageShowResetETA
        usageShowRunoutETA = try container.decodeIfPresent(Bool.self, forKey: .usageShowRunoutETA) ?? fallback.usageShowRunoutETA
        usageBarsShowUsed = try container.decodeIfPresent(Bool.self, forKey: .usageBarsShowUsed) ?? fallback.usageBarsShowUsed
        usageBarPalette = UsageBarPalette.resolve(try container.decodeIfPresent(String.self, forKey: .usageBarPalette)).rawValue
        usageWarningMarkersVisible = try container.decodeIfPresent(Bool.self, forKey: .usageWarningMarkersVisible) ?? fallback.usageWarningMarkersVisible
        usageHistoryDays = min(365, max(7, try container.decodeIfPresent(Int.self, forKey: .usageHistoryDays) ?? fallback.usageHistoryDays))
        usageWarningThreshold = min(100, max(0, try container.decodeIfPresent(Double.self, forKey: .usageWarningThreshold) ?? fallback.usageWarningThreshold))
        showVitalsInPopover = try container.decodeIfPresent(Bool.self, forKey: .showVitalsInPopover) ?? fallback.showVitalsInPopover
        systemTelemetryEnabled = try container.decodeIfPresent(Bool.self, forKey: .systemTelemetryEnabled) ?? fallback.systemTelemetryEnabled
        systemTelemetryInPopover = try container.decodeIfPresent(Bool.self, forKey: .systemTelemetryInPopover) ?? fallback.systemTelemetryInPopover
        let visibility = try MenuBarVisibilityPersistence(from: decoder)
        _ = try container.decodeIfPresent(Int.self, forKey: .systemTelemetryStatusPreferenceVersion)
        let storedTelemetryVisibility = visibility.systemTelemetryInStatusItem
        systemTelemetryStatusPreferenceVersion = 1
        // Absence gets the modern default. An explicitly stored false remains a user choice,
        // including configs written before the version marker existed.
        systemTelemetryInStatusItem = storedTelemetryVisibility ?? fallback.systemTelemetryInStatusItem
        batteryStatusItemEnabled = visibility.batteryStatusItemEnabled
            ?? fallback.batteryStatusItemEnabled
        systemTelemetryInCockpit = try container.decodeIfPresent(Bool.self, forKey: .systemTelemetryInCockpit) ?? fallback.systemTelemetryInCockpit
        let storedTelemetryProfile = try container.decodeIfPresent(String.self, forKey: .systemTelemetryProfile) ?? fallback.systemTelemetryProfile
        systemTelemetryProfile = ["eco", "balanced", "live"].contains(storedTelemetryProfile) ? storedTelemetryProfile : fallback.systemTelemetryProfile
        let storedTelemetrySpacingVersion = try container.decodeIfPresent(Int.self, forKey: .systemTelemetrySpacingPreferenceVersion)
        systemTelemetrySpacingPreferenceVersion = 1
        systemTelemetryCompactSpacing = storedTelemetrySpacingVersion == nil
            ? true
            : (try container.decodeIfPresent(Bool.self, forKey: .systemTelemetryCompactSpacing) ?? fallback.systemTelemetryCompactSpacing)
        systemTelemetryShowCPU = visibility.systemTelemetryShowCPU ?? fallback.systemTelemetryShowCPU
        systemTelemetryShowGPU = visibility.systemTelemetryShowGPU ?? fallback.systemTelemetryShowGPU
        systemTelemetryShowRAM = visibility.systemTelemetryShowRAM ?? fallback.systemTelemetryShowRAM
        systemTelemetryShowDisk = visibility.systemTelemetryShowDisk ?? fallback.systemTelemetryShowDisk
        let thresholds = SystemTelemetryDisplayPolicy(
            warningThreshold: try container.decodeIfPresent(Double.self, forKey: .systemTelemetryWarningThreshold) ?? fallback.systemTelemetryWarningThreshold,
            criticalThreshold: try container.decodeIfPresent(Double.self, forKey: .systemTelemetryCriticalThreshold) ?? fallback.systemTelemetryCriticalThreshold
        )
        systemTelemetryWarningThreshold = thresholds.warningThreshold
        systemTelemetryCriticalThreshold = thresholds.criticalThreshold
        launchAtLogin = try container.decodeIfPresent(Bool.self, forKey: .launchAtLogin) ?? fallback.launchAtLogin
        transport = try container.decodeIfPresent(String.self, forKey: .transport) ?? fallback.transport
        slowRingTick = try container.decodeIfPresent(Bool.self, forKey: .slowRingTick) ?? fallback.slowRingTick
        slowRingInterval = try container.decodeIfPresent(Double.self, forKey: .slowRingInterval) ?? fallback.slowRingInterval
        taskActionsEnabled = try container.decodeIfPresent(Bool.self, forKey: .taskActionsEnabled) ?? fallback.taskActionsEnabled
        backgroundTickEnabled = try container.decodeIfPresent(Bool.self, forKey: .backgroundTickEnabled) ?? fallback.backgroundTickEnabled
        panelDetached = try container.decodeIfPresent(Bool.self, forKey: .panelDetached) ?? fallback.panelDetached
        panelAlwaysOnTop = try container.decodeIfPresent(Bool.self, forKey: .panelAlwaysOnTop) ?? fallback.panelAlwaysOnTop
        usagePeekCollapsed = try container.decodeIfPresent(Bool.self, forKey: .usagePeekCollapsed) ?? fallback.usagePeekCollapsed
    }


    static let path = ProcessInfo.processInfo.environment["COORD_MENUBAR_CONFIG"]
        ?? "\(NSHomeDirectory())/.coordharness/menubar_panel_config.json"

    static func load() -> Config {
        let url = URL(fileURLWithPath: path)
        guard let data = try? Data(contentsOf: url) else {
            MenubarLog.info("config load failed path=\(path); using defaults transport=db")
            return Config()
        }
        let storedObject = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        let persistTelemetryStatusMigration =
            storedObject?["system_telemetry_status_preference_version"] == nil
            && storedObject?["systemTelemetryStatusPreferenceVersion"] == nil
        let persistTelemetrySpacingMigration =
            storedObject?["system_telemetry_spacing_preference_version"] == nil
            && storedObject?["systemTelemetrySpacingPreferenceVersion"] == nil
        let dec = JSONDecoder(); dec.keyDecodingStrategy = .convertFromSnakeCase

        do {
            var config = try dec.decode(Config.self, from: data)
            config.transport = SnapshotTransportKind.resolve(config.transport).rawValue
            if persistTelemetryStatusMigration || persistTelemetrySpacingMigration {
                // The decoder promotes shipped unversioned telemetry defaults.
                // Persist their markers now so this is a real one-time migration,
                // not an in-memory exception every launch.
                config.save()
            }
            MenubarLog.info("config loaded path=\(path) transport=\(config.transport) timeout=\(config.fetchTimeoutSecs) slowRing=\(config.slowRingTick)")
            return config
        } catch {
            MenubarLog.info("config decode failed path=\(path) bytes=\(data.count) error=\(type(of: error)): \(error); using defaults")
            return Config()
        }
    }

    func save() {
        let enc = JSONEncoder()
        enc.keyEncodingStrategy = .convertToSnakeCase
        enc.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let data = try? enc.encode(self) else { return }
        let url = URL(fileURLWithPath: Self.path)
        do {
            try ConfigPersistence.write(data, to: url)
        } catch {
            MenubarLog.info("config save failed path=\(Self.path) error=\(type(of: error)): \(error)")
        }
    }
}
