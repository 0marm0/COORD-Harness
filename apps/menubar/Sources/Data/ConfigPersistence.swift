import Foundation

enum ConfigPersistence {
    static func write(
        _ data: Data,
        to url: URL,
        fileManager: FileManager = .default
    ) throws {
        try fileManager.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try data.write(to: url, options: .atomic)
    }
}

/// Acronym-safe top-level config fields. JSONDecoder.convertFromSnakeCase maps
/// *_cpu / *_gpu / *_ram to Cpu / Gpu / Ram, so explicit CodingKeys are required.
struct MenuBarVisibilityPersistence: Codable, Equatable {
    var batteryStatusItemEnabled: Bool?
    var systemTelemetryInStatusItem: Bool?
    var systemTelemetryShowCPU: Bool?
    var systemTelemetryShowGPU: Bool?
    var systemTelemetryShowRAM: Bool?
    var systemTelemetryShowDisk: Bool?

    private enum CodingKeys: String, CodingKey {
        case batteryStatusItemEnabled
        case systemTelemetryInStatusItem
        case systemTelemetryShowCPU = "systemTelemetryShowCpu"
        case systemTelemetryShowGPU = "systemTelemetryShowGpu"
        case systemTelemetryShowRAM = "systemTelemetryShowRam"
        case systemTelemetryShowDisk
    }
}
