import Foundation

enum NativeContextPaletteMode: String, CaseIterable, Codable, Equatable {
    case all
    case knowledge
    case facts
    case work
    case memory
    case files
    case done

    var id: String { rawValue }

    var label: String {
        switch self {
        case .all: return "All Context"
        case .knowledge: return "Knowledge"
        case .facts: return "Facts"
        case .work: return "Work"
        case .memory: return "Memory"
        case .files: return "Files"
        case .done: return "Done"
        }
    }

    var systemSymbolName: String {
        switch self {
        case .all: return "square.grid.2x2"
        case .knowledge: return "books.vertical"
        case .facts: return "checklist.checked"
        case .work: return "list.bullet.rectangle"
        case .memory: return "brain.head.profile"
        case .files: return "doc.text"
        case .done: return "checkmark.seal"
        }
    }
}

enum NativeContextPrimaryAction: String, Codable, Equatable {
    case readPointer
    case revealWork
    case openFile
    case openPointer
    case inspect
}

struct NativeContextSearchResponse: Decodable, Equatable {
    var ok: Bool
    var id: String?
    var command: String?
    var mode: NativeContextPaletteMode
    var query: String
    var profile: String?
    var workId: String?
    var hits: [NativeContextHit]?
    var groups: [NativeContextGroup]
    var sourceCounts: [String: Int]
    var providerResults: [NativeContextProviderResult]?
    var errors: [NativeContextBridgeError]?
    var truncated: Bool?
    var suggestions: [String]
    var intentCards: [NativeContextIntentCard]
    var facets: [NativeContextFacet]
    var answerCards: [NativeContextAnswerCard]
    var explorerSummary: NativeContextExplorerSummary
    var index: NativeContextIndexStats?
    var elapsedMs: Double?

    private enum CodingKeys: String, CodingKey {
        case ok
        case id
        case command
        case mode
        case query
        case profile
        case workId
        case hits
        case groups
        case sourceCounts
        case providerResults
        case errors
        case truncated
        case suggestions
        case intentCards
        case facets
        case answerCards
        case explorerSummary
        case index
        case elapsedMs
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        ok = try container.decode(Bool.self, forKey: .ok)
        id = try container.decodeIfPresent(String.self, forKey: .id)
        command = try container.decodeIfPresent(String.self, forKey: .command)
        mode = try container.decode(NativeContextPaletteMode.self, forKey: .mode)
        query = try container.decode(String.self, forKey: .query)
        profile = try container.decodeIfPresent(String.self, forKey: .profile)
        workId = try container.decodeIfPresent(String.self, forKey: .workId)
        hits = try container.decodeIfPresent([NativeContextHit].self, forKey: .hits)
        groups = try container.decode([NativeContextGroup].self, forKey: .groups)
        sourceCounts = try container.decode([String: Int].self, forKey: .sourceCounts)
        providerResults = try container.decodeIfPresent([NativeContextProviderResult].self, forKey: .providerResults)
        errors = try container.decodeIfPresent([NativeContextBridgeError].self, forKey: .errors)
        truncated = try container.decodeIfPresent(Bool.self, forKey: .truncated)
        suggestions = try container.decodeIfPresent([String].self, forKey: .suggestions) ?? []
        intentCards = try container.decodeIfPresent([NativeContextIntentCard].self, forKey: .intentCards) ?? []
        facets = try container.decodeIfPresent([NativeContextFacet].self, forKey: .facets) ?? []
        answerCards = try container.decodeIfPresent([NativeContextAnswerCard].self, forKey: .answerCards) ?? []
        explorerSummary = try container.decodeIfPresent(NativeContextExplorerSummary.self, forKey: .explorerSummary) ?? .empty
        index = try container.decodeIfPresent(NativeContextIndexStats.self, forKey: .index)
        elapsedMs = try container.decodeIfPresent(Double.self, forKey: .elapsedMs)
    }
}

struct NativeContextGroup: Decodable, Equatable {
    var id: String
    var label: String
    var accent: String
    var summary: String?
    var count: Int
    var items: [NativeContextHit]
}

struct NativeContextHit: Decodable, Equatable {
    var id: String
    var source: String
    var sourceLabel: String
    var group: String
    var groupLabel: String
    var kind: String
    var title: String
    var pointer: String?
    var snippet: String?
    var metadata: [String: NativeContextJSONValue]
    var accent: String
    var primaryAction: NativeContextPrimaryAction
    var previewLoaded: Bool
    var badges: [String]
}

struct NativeContextProviderResult: Decodable, Equatable {
    var source: String
    var returned: Int?
    var truncated: Bool?
    var error: String?
}

struct NativeContextIntentCard: Decodable, Equatable {
    var id: String
    var title: String
    var subtitle: String
    var query: String
    var mode: NativeContextPaletteMode
    var accent: String
    var count: Int
    var systemSymbolName: String
}

struct NativeContextFacet: Decodable, Equatable {
    var id: String
    var label: String
    var options: [NativeContextFacetOption]
}

struct NativeContextFacetOption: Decodable, Equatable {
    var id: String
    var label: String
    var count: Int
    var active: Bool
}

struct NativeContextAnswerCard: Decodable, Equatable {
    var id: String
    var hitId: String
    var title: String
    var summary: String
    var sourceLabel: String
    var group: String
    var displayType: String
    var accent: String
    var pointer: String?
    var primaryAction: NativeContextPrimaryAction
    var badges: [String]
    var whyItMatters: String
    var relationshipHints: [String]
}

struct NativeContextExplorerSummary: Decodable, Equatable {
    var headline: String
    var subhead: String

    static let empty = NativeContextExplorerSummary(headline: "", subhead: "")
}

struct NativeContextBridgeError: Decodable, Equatable {
    var source: String?
    var error: String
}

struct NativeContextIndexStats: Decodable, Equatable {
    var available: Bool?
    var documents: Int
    var sourceFileCount: Int?
    var indexedSourcePathCount: Int?
    var stale: Bool
    var staleReasons: [String]
    var freshnessBasis: String?
    var updatedAt: NativeContextJSONValue?
    var refreshCommand: String?
}

struct NativeContextReadResponse: Decodable, Equatable {
    var ok: Bool
    var id: String?
    var command: String?
    var pointer: String
    var read: [String: NativeContextJSONValue]
    var detailCard: NativeContextDetailCard

    private enum CodingKeys: String, CodingKey {
        case ok
        case id
        case command
        case pointer
        case read
        case detailCard
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        ok = try container.decode(Bool.self, forKey: .ok)
        id = try container.decodeIfPresent(String.self, forKey: .id)
        command = try container.decodeIfPresent(String.self, forKey: .command)
        pointer = try container.decode(String.self, forKey: .pointer)
        read = try container.decode([String: NativeContextJSONValue].self, forKey: .read)
        detailCard = try container.decodeIfPresent(NativeContextDetailCard.self, forKey: .detailCard) ?? .empty
    }
}

struct NativeContextDetailCard: Decodable, Equatable {
    var title: String
    var sourcePath: String
    var summary: String
    var sections: [NativeContextDetailSection]
    var timeline: [NativeContextDetailTimelineItem]
    var related: [NativeContextRelatedItem]

    static let empty = NativeContextDetailCard(title: "", sourcePath: "", summary: "", sections: [], timeline: [], related: [])
}

struct NativeContextDetailSection: Decodable, Equatable {
    var title: String
    var items: [String]
}

struct NativeContextDetailTimelineItem: Decodable, Equatable {
    var label: String
    var value: String
}

struct NativeContextRelatedItem: Decodable, Equatable {
    var title: String
    var relation: String
    var pointer: String
    var summary: String
}

enum NativeContextJSONValue: Decodable, Equatable {
    case string(String)
    case int(Int)
    case double(Double)
    case bool(Bool)
    case object([String: NativeContextJSONValue])
    case array([NativeContextJSONValue])
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Int.self) {
            self = .int(value)
        } else if let value = try? container.decode(Double.self) {
            self = .double(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([String: NativeContextJSONValue].self) {
            self = .object(value)
        } else if let value = try? container.decode([NativeContextJSONValue].self) {
            self = .array(value)
        } else {
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "Unsupported JSON value")
        }
    }

    var stringValue: String? {
        switch self {
        case .string(let value): return value
        case .int(let value): return String(value)
        case .double(let value): return String(value)
        case .bool(let value): return String(value)
        case .object, .array, .null: return nil
        }
    }

    var intValue: Int? {
        switch self {
        case .int(let value): return value
        case .double(let value): return Int(value)
        case .string(let value): return Int(value)
        case .bool, .object, .array, .null: return nil
        }
    }

    var boolValue: Bool? {
        switch self {
        case .bool(let value): return value
        case .string(let value):
            if value.lowercased() == "true" { return true }
            if value.lowercased() == "false" { return false }
            return nil
        case .int(let value): return value != 0
        case .double(let value): return value != 0
        case .object, .array, .null: return nil
        }
    }

    var objectValue: [String: NativeContextJSONValue]? {
        if case .object(let value) = self { return value }
        return nil
    }
}

enum NativeContextPaletteLifecycleEvent: Equatable {
    case appLaunch
    case paletteOpened
    case paletteClosed
    case idleTimerFired
}

enum NativeContextPaletteLifecycleAction: Equatable {
    case stayUnloaded
    case startBridge
    case scheduleUnload(after: Int)
    case unloadNow
}

enum NativeContextPaletteLifecycle {
    static let defaultIdleUnloadDelay = 90

    static func action(for event: NativeContextPaletteLifecycleEvent) -> NativeContextPaletteLifecycleAction {
        switch event {
        case .appLaunch:
            return .stayUnloaded
        case .paletteOpened:
            return .startBridge
        case .paletteClosed:
            return .scheduleUnload(after: defaultIdleUnloadDelay)
        case .idleTimerFired:
            return .unloadNow
        }
    }
}
