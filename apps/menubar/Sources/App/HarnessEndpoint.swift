import Foundation

/// Where the native clients look for a board.
///
/// Every endpoint in this app resolves through here. They used to be spelled out
/// as literals pointing at a fixed port, which meant the app would silently
/// attach to whatever happened to be listening there -- including a different
/// system entirely. One place, one default, and an environment override.
enum HarnessEndpoint {
    static let defaultPort = 7870
    static let persistedBaseURLKey = "coordharness.baseURL"

    /// LaunchServices apps do not inherit shell variables. The installer writes
    /// the same explicit endpoint into each app's preference domain, while an
    /// environment override remains useful for development and tests.
    static let base = resolveBase(
        environment: ProcessInfo.processInfo.environment,
        persistedBaseURL: UserDefaults.standard.string(forKey: persistedBaseURLKey)
    )

    static func resolveBase(
        environment: [String: String],
        persistedBaseURL: String? = nil
    ) -> String {
        let candidate = nonempty(environment["COORD_BOARD_URL"])
            ?? nonempty(persistedBaseURL)
            ?? "http://127.0.0.1:\(defaultPort)"
        return candidate.hasSuffix("/") ? String(candidate.dropLast()) : candidate
    }

    static func url(_ path: String) -> URL? {
        URL(string: path.hasPrefix("/") ? base + path : base + "/" + path)
    }

    private static func nonempty(_ value: String?) -> String? {
        guard let value = value?.trimmingCharacters(in: .whitespacesAndNewlines),
              !value.isEmpty else { return nil }
        return value
    }
}
