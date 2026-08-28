import Foundation

struct CockpitCapabilityInventory: Decodable, Equatable {
    var schemaVersion: Int
    var mode: String
    var generatedAt: Double?
    var source: String?
    var readOnly: Bool
    var actionsEnabled: Bool
    var defaultContextInjectionEnabled: Bool
    var authority: [String: String]
    var tokenCostPolicy: [String: Bool]
    var capabilities: [CockpitCapabilityRow]

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case mode
        case generatedAt = "generated_at"
        case source
        case readOnly = "read_only"
        case actionsEnabled = "actions_enabled"
        case defaultContextInjectionEnabled = "default_context_injection_enabled"
        case authority
        case tokenCostPolicy = "token_cost_policy"
        case capabilities
        case rows
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.schemaVersion = try container.decodeIfPresent(Int.self, forKey: .schemaVersion) ?? 1
        self.mode = try container.decodeIfPresent(String.self, forKey: .mode) ?? "capability_inventory"
        self.generatedAt = try container.decodeIfPresent(Double.self, forKey: .generatedAt)
        self.source = try container.decodeIfPresent(String.self, forKey: .source)
        self.readOnly = try container.decodeIfPresent(Bool.self, forKey: .readOnly) ?? true
        self.actionsEnabled = try container.decodeIfPresent(Bool.self, forKey: .actionsEnabled) ?? false
        self.defaultContextInjectionEnabled = try container.decodeIfPresent(Bool.self, forKey: .defaultContextInjectionEnabled) ?? false
        self.authority = try container.decodeIfPresent([String: String].self, forKey: .authority) ?? [:]
        self.tokenCostPolicy = try container.decodeIfPresent([String: Bool].self, forKey: .tokenCostPolicy) ?? [:]
        self.capabilities = try container.decodeIfPresent([CockpitCapabilityRow].self, forKey: .capabilities)
            ?? container.decodeIfPresent([CockpitCapabilityRow].self, forKey: .rows)
            ?? []
    }
}

struct CockpitCapabilityRow: Decodable, Equatable {
    var id: String
    var label: String
    var category: String?
    var plane: String?
    var status: String
    var canonicalOwner: String?
    var surface: String?
    var automatic: Bool
    var residentProcess: Bool
    var modelTokens: Bool
    var chatContextInjected: Bool
    var duplicates: String?
    var operatorAction: String?

    private enum CodingKeys: String, CodingKey {
        case id
        case label
        case category
        case plane
        case status
        case canonicalOwner = "canonical_owner"
        case surface
        case automatic
        case residentProcess = "resident_process"
        case modelTokens = "model_tokens"
        case chatContextInjected = "chat_context_injected"
        case duplicates
        case operatorAction = "operator_action"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.id = try container.decodeIfPresent(String.self, forKey: .id) ?? ""
        self.label = try container.decodeIfPresent(String.self, forKey: .label) ?? id
        self.category = try container.decodeIfPresent(String.self, forKey: .category)
        self.plane = try container.decodeIfPresent(String.self, forKey: .plane)
        self.status = try container.decodeIfPresent(String.self, forKey: .status) ?? "unknown"
        self.canonicalOwner = try container.decodeIfPresent(String.self, forKey: .canonicalOwner)
        self.surface = try container.decodeIfPresent(String.self, forKey: .surface)
        self.automatic = try container.decodeIfPresent(Bool.self, forKey: .automatic) ?? false
        self.residentProcess = try container.decodeIfPresent(Bool.self, forKey: .residentProcess) ?? false
        self.modelTokens = try container.decodeIfPresent(Bool.self, forKey: .modelTokens) ?? false
        self.chatContextInjected = try container.decodeIfPresent(Bool.self, forKey: .chatContextInjected) ?? false
        self.duplicates = try container.decodeIfPresent(String.self, forKey: .duplicates)
        self.operatorAction = try container.decodeIfPresent(String.self, forKey: .operatorAction)
    }

    var detail: String {
        [
            canonicalOwner.map { "owner \($0)" },
            surface,
            operatorAction,
        ].compactMap { $0 }.joined(separator: " | ")
    }
}
