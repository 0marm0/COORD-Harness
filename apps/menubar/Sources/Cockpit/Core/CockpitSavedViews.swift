import Foundation

enum CockpitSortDirection: String, Codable, Equatable {
    case ascending
    case descending

    var shortLabel: String {
        switch self {
        case .ascending: return "Asc"
        case .descending: return "Desc"
        }
    }

    mutating func toggle() {
        self = self == .ascending ? .descending : .ascending
    }
}

struct CockpitViewSnapshot: Codable, Equatable {
    var scope: CockpitScope
    var groupMode: CockpitGroupMode
    var sortMode: CockpitSortMode
    var sortDirection: CockpitSortDirection
    var owner: String
    var module: String
    var status: String
    var query: String
    var columns: [CockpitColumn]?

    init(
        scope: CockpitScope,
        groupMode: CockpitGroupMode,
        sortMode: CockpitSortMode,
        sortDirection: CockpitSortDirection? = nil,
        owner: String,
        module: String,
        status: String,
        query: String,
        columns: [CockpitColumn]? = nil
    ) {
        self.scope = scope
        self.groupMode = groupMode
        self.sortMode = sortMode
        self.sortDirection = sortDirection ?? sortMode.defaultDirection
        self.owner = owner
        self.module = module
        self.status = status
        self.query = query
        self.columns = columns
    }

    private enum CodingKeys: String, CodingKey {
        case scope
        case groupMode
        case sortMode
        case sortDirection
        case owner
        case module
        case status
        case query
        case columns
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let sortMode = try container.decode(CockpitSortMode.self, forKey: .sortMode)
        self.scope = try container.decode(CockpitScope.self, forKey: .scope)
        self.groupMode = try container.decode(CockpitGroupMode.self, forKey: .groupMode)
        self.sortMode = sortMode
        self.sortDirection = try container.decodeIfPresent(CockpitSortDirection.self, forKey: .sortDirection) ?? sortMode.defaultDirection
        self.owner = try container.decode(String.self, forKey: .owner)
        self.module = try container.decode(String.self, forKey: .module)
        self.status = try container.decode(String.self, forKey: .status)
        self.query = try container.decode(String.self, forKey: .query)
        self.columns = try container.decodeIfPresent([CockpitColumn].self, forKey: .columns)
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(scope, forKey: .scope)
        try container.encode(groupMode, forKey: .groupMode)
        try container.encode(sortMode, forKey: .sortMode)
        try container.encode(sortDirection, forKey: .sortDirection)
        try container.encode(owner, forKey: .owner)
        try container.encode(module, forKey: .module)
        try container.encode(status, forKey: .status)
        try container.encode(query, forKey: .query)
        try container.encodeIfPresent(columns, forKey: .columns)
    }

    func matchesControls(of other: CockpitViewSnapshot) -> Bool {
        scope == other.scope
            && groupMode == other.groupMode
            && sortMode == other.sortMode
            && sortDirection == other.sortDirection
            && owner == other.owner
            && module == other.module
            && status == other.status
            && query == other.query
    }
}

struct CockpitSavedView: Codable, Equatable {
    var name: String
    var snapshot: CockpitViewSnapshot
}

enum CockpitSavedViews {
    static let builtIn: [CockpitSavedView] = [
        CockpitSavedView(name: "Now", snapshot: CockpitViewSnapshot(scope: .now, groupMode: .smart, sortMode: .smart, owner: "", module: "", status: "", query: "")),
        CockpitSavedView(name: "Local", snapshot: CockpitViewSnapshot(scope: .now, groupMode: .status, sortMode: .progress, owner: "", module: "", status: "running", query: "local")),
        CockpitSavedView(name: "Nested", snapshot: CockpitViewSnapshot(scope: .now, groupMode: .nested, sortMode: .smart, owner: "", module: "", status: "", query: "")),
        CockpitSavedView(name: "Claude", snapshot: CockpitViewSnapshot(scope: .now, groupMode: .smart, sortMode: .smart, owner: "claude", module: "", status: "", query: "")),
        CockpitSavedView(name: "Codex", snapshot: CockpitViewSnapshot(scope: .now, groupMode: .smart, sortMode: .smart, owner: "codex", module: "", status: "", query: "")),
        CockpitSavedView(name: "Backlog", snapshot: CockpitViewSnapshot(scope: .next, groupMode: .domain, sortMode: .eta, owner: "", module: "", status: "", query: "")),
    ]

    static func isBuiltIn(_ name: String) -> Bool {
        builtIn.contains { $0.name.caseInsensitiveCompare(name) == .orderedSame }
    }
}

final class CockpitSavedViewStore {
    private let defaults: UserDefaults
    private let key: String
    private let encoder = PropertyListEncoder()
    private let decoder = PropertyListDecoder()

    init(defaults: UserDefaults = .standard, key: String = "coordharness.nativeCockpit.savedViews.v1") {
        self.defaults = defaults
        self.key = key
        encoder.outputFormat = .binary
    }

    func allViews() -> [CockpitSavedView] {
        CockpitSavedViews.builtIn + customViews().filter { !CockpitSavedViews.isBuiltIn($0.name) }
    }

    func view(named name: String) -> CockpitSavedView? {
        allViews().first { $0.name.caseInsensitiveCompare(name) == .orderedSame }
    }

    func customViews() -> [CockpitSavedView] {
        guard let data = defaults.data(forKey: key),
              let views = try? decoder.decode([CockpitSavedView].self, from: data) else {
            return []
        }
        return views.sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
    }

    func save(name: String, snapshot: CockpitViewSnapshot) throws {
        let cleanName = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanName.isEmpty else { return }
        guard !CockpitSavedViews.isBuiltIn(cleanName) else { return }

        var custom = customViews().filter { $0.name.caseInsensitiveCompare(cleanName) != .orderedSame }
        custom.append(CockpitSavedView(name: cleanName, snapshot: snapshot))
        custom.sort { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
        let data = try encoder.encode(custom)
        defaults.set(data, forKey: key)
    }
}

enum CockpitNativePreferences {
    private static let quickFiltersPinnedKey = "coordharness.nativeCockpit.quickFiltersPinned.v1"

    static var quickFiltersPinned: Bool {
        get {
            guard UserDefaults.standard.object(forKey: quickFiltersPinnedKey) != nil else { return false }
            return UserDefaults.standard.bool(forKey: quickFiltersPinnedKey)
        }
        set {
            UserDefaults.standard.set(newValue, forKey: quickFiltersPinnedKey)
        }
    }
}
