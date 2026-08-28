import Foundation
import SwiftUI

#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

struct CoordProviderManagementSnapshot: Decodable {
    let schema: String
    let catalog: [CoordManagedProvider]
    let profiles: [String: CoordManagedProfileCollection]
    let routingPolicy: CoordManagedRoutingPolicy
    let recommendation: CoordManagedRecommendation
    enum CodingKeys: String, CodingKey { case schema, catalog, profiles, recommendation; case routingPolicy = "routing_policy" }
}

struct CoordManagedProvider: Decodable, Identifiable {
    let id: String; let displayName: String; let accent: String; let authModes: [String]
    let defaultAuthMode: String; let capabilities: [String]; let enabled: Bool; let priority: Int
    enum CodingKeys: String, CodingKey { case id, accent, capabilities, enabled, priority; case displayName = "display_name"; case authModes = "auth_modes"; case defaultAuthMode = "default_auth_mode" }
}
struct CoordManagedProfileCollection: Decodable { let active: String; let profiles: [CoordManagedProfile] }
struct CoordManagedProfile: Decodable, Identifiable {
    let id: String; let label: String; let active: Bool; let authMode: String; let enabled: Bool; let credentialSet: Bool
    enum CodingKeys: String, CodingKey { case id, label, active, enabled; case authMode = "auth_mode"; case credentialSet = "credential_set" }
}
struct CoordManagedRoutingPolicy: Decodable {
    let version: Int; let mode: String; let minSessionRemaining: Int; let minWeeklyRemaining: Int
    let minRunwayMinutes: Int; let allowMeteredAPI: Bool; let preferSubscription: Bool; let preferLocal: Bool
    let requiredCapabilities: [String]
    enum CodingKeys: String, CodingKey { case version, mode; case minSessionRemaining = "min_session_remaining"; case minWeeklyRemaining = "min_weekly_remaining"; case minRunwayMinutes = "min_runway_minutes"; case allowMeteredAPI = "allow_metered_api"; case preferSubscription = "prefer_subscription"; case preferLocal = "prefer_local"; case requiredCapabilities = "required_capabilities" }
}
struct CoordManagedRecommendation: Decodable { let selected: CoordManagedCandidate?; let decision: String }
struct CoordManagedCandidate: Decodable {
    let provider: String; let displayName: String; let sessionRemaining: Double?; let weeklyRemaining: Double?; let runwayMinutes: Double?
    enum CodingKeys: String, CodingKey { case provider; case displayName = "display_name"; case sessionRemaining = "session_remaining"; case weeklyRemaining = "weekly_remaining"; case runwayMinutes = "runway_minutes" }
}

@MainActor final class CoordProviderManagementStore: ObservableObject {
    @Published private(set) var snapshot: CoordProviderManagementSnapshot?
    @Published private(set) var message = "Loading services…"
    @Published private(set) var busy = false
    private let baseURL: URL?
    init(baseURL: URL?) { self.baseURL = baseURL }
    func load() async { await send(nil) }
    func configure(_ provider: CoordManagedProvider) async { await send(["action":"provider_configure","provider_id":provider.id,"enabled":!provider.enabled,"priority":provider.priority]) }
    func select(provider: String, profileID: String) async { await send(["action":"account_select","provider_id":provider,"profile_id":profileID]) }
    func add(provider: CoordManagedProvider, label: String, auth: String, endpoint: String) async { await send(["action":"account_add","provider_id":provider.id,"label":label,"auth_mode":auth,"endpoint":endpoint.isEmpty ? NSNull() : endpoint]) }
    func credential(provider: String, profileID: String, value: String) async { await send(["action":"credential_set","provider_id":provider,"profile_id":profileID,"credential":value]) }
    func policy(mode: String, session: Int, weekly: Int, runway: Int, metered: Bool, subscription: Bool, local: Bool) async {
        await send(["action":"routing_policy_update","policy":["version":1,"mode":mode,"min_session_remaining":session,"min_weekly_remaining":weekly,"min_runway_minutes":runway,"allow_metered_api":metered,"prefer_subscription":subscription,"prefer_local":local,"required_capabilities":snapshot?.routingPolicy.requiredCapabilities ?? ["code","tools"]]])
    }
    private func send(_ document: [String: Any]?) async {
        guard !busy, let baseURL, var parts = URLComponents(url: baseURL, resolvingAgainstBaseURL: false), let host = parts.host, Self.loopback(host), parts.user == nil, parts.password == nil else { message = "Local COORD board is required."; return }
        parts.path = "/api/v1/provider-management"; parts.query = nil; parts.fragment = nil
        guard let url = parts.url else { message = "Local COORD board is required."; return }
        busy = true; defer { busy = false }
        do {
            var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 8)
            request.setValue("application/json", forHTTPHeaderField: "Accept")
            request.setValue("v1", forHTTPHeaderField: "X-Coord-Usage-Action")
            var origin = URLComponents(); origin.scheme = parts.scheme; origin.host = host; origin.port = parts.port
            request.setValue(origin.url?.absoluteString, forHTTPHeaderField: "Origin")
            if let document { request.httpMethod = "POST"; request.setValue("application/json", forHTTPHeaderField: "Content-Type"); request.httpBody = try JSONSerialization.data(withJSONObject: document) }
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode), data.count <= 262_144 else { message = "Provider setting was rejected."; return }
            let decoded = try JSONDecoder().decode(CoordProviderManagementSnapshot.self, from: data)
            guard decoded.schema == "coord.provider-management.v1" else { throw URLError(.cannotParseResponse) }
            snapshot = decoded; message = document == nil ? "Services are current." : "Saved."
        } catch { message = "Provider services are unavailable." }
    }
    private static func loopback(_ host: String) -> Bool { let value = host.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]")); if value == "localhost" || value == "::1" { return true }; let parts = value.split(separator: ".", omittingEmptySubsequences: false); return parts.count == 4 && Int(parts[0]) == 127 && parts.dropFirst().allSatisfy { (Int($0) ?? -1) >= 0 && (Int($0) ?? 256) <= 255 } }
}

struct ProviderManagementView: View {
    @StateObject private var store: CoordProviderManagementStore
    @State private var mode = "advisory"
    @State private var session = 15
    @State private var weekly = 20
    @State private var runway = 60
    @State private var metered = false
    @State private var subscription = true
    @State private var local = false
    init(baseURL: URL?) { _store = StateObject(wrappedValue: CoordProviderManagementStore(baseURL: baseURL)) }
    var body: some View {
        ScrollView { VStack(alignment:.leading,spacing:12) {
            GroupBox("Intelligent routing") { VStack(alignment:.leading,spacing:9) {
                if let winner=store.snapshot?.recommendation.selected { Text("Recommended: \(winner.displayName)").font(.headline); Text([winner.sessionRemaining.map{"session \(Int($0))%"},winner.weeklyRemaining.map{"weekly \(Int($0))%"}].compactMap{$0}.joined(separator:" · ")).font(.caption).foregroundStyle(.secondary) } else { Text("No eligible route").font(.headline) }
                Picker("Mode",selection:$mode){Text("Advisory").tag("advisory");Text("Automatic selection").tag("automatic")}.pickerStyle(.segmented)
                HStack { Stepper("Session \(session)%",value:$session,in:0...100);Stepper("Weekly \(weekly)%",value:$weekly,in:0...100);Stepper("Runway \(runway)m",value:$runway,in:0...10080) }
                HStack { Toggle("Subscription",isOn:$subscription);Toggle("Local",isOn:$local);Toggle("Metered/API",isOn:$metered) }
                HStack { Text(store.message).font(.caption).foregroundStyle(.secondary);Spacer();Button("Save routing"){Task{await store.policy(mode:mode,session:session,weekly:weekly,runway:runway,metered:metered,subscription:subscription,local:local)}}.disabled(store.busy) }
            }.padding(4) }
            if let snapshot=store.snapshot { ForEach(snapshot.catalog) { provider in DisclosureGroup { CoordManagedProviderControls(store:store,provider:provider,profiles:snapshot.profiles[provider.id]) } label: { HStack { Circle().fill(Color(hex:provider.accent)).frame(width:9,height:9);VStack(alignment:.leading){Text(provider.displayName).font(.system(size:12,weight:.semibold));Text(provider.capabilities.joined(separator:" · ")).font(.caption2).foregroundStyle(.secondary)};Spacer();Button(provider.enabled ? "On":"Enable"){Task{await store.configure(provider)}}.buttonStyle(.bordered).controlSize(.small) } }.padding(9).background(.secondary.opacity(0.06),in:RoundedRectangle(cornerRadius:9)) } }
            Text("COORD stores no credentials or private endpoints. Credentials are write-only to the provider-owned macOS Keychain. Automatic selection never launches work by itself.").font(.caption).foregroundStyle(.secondary)
        }.padding(14) }.navigationTitle("Services & Routing").task{await store.load()}.onChange(of:store.snapshot?.routingPolicy.mode){_,_ in loadPolicy()}
    }
    private func loadPolicy(){guard let p=store.snapshot?.routingPolicy else{return};mode=p.mode;session=p.minSessionRemaining;weekly=p.minWeeklyRemaining;runway=p.minRunwayMinutes;metered=p.allowMeteredAPI;subscription=p.preferSubscription;local=p.preferLocal}
}

private struct CoordManagedProviderControls: View {
    @ObservedObject var store: CoordProviderManagementStore; let provider: CoordManagedProvider; let profiles: CoordManagedProfileCollection?
    @State private var label = ""
    @State private var auth = ""
    @State private var endpoint = ""
    @State private var credential = ""
    var active: CoordManagedProfile? { profiles?.profiles.first(where:{$0.id == profiles?.active}) }
    var body: some View { VStack(alignment:.leading,spacing:7) {
        if let profiles { Picker("Account",selection:Binding(get:{profiles.active},set:{id in Task{await store.select(provider:provider.id,profileID:id)}})){ForEach(profiles.profiles){Text($0.label).tag($0.id)}} }
        HStack { TextField("New account",text:$label);Picker("Auth",selection:$auth){ForEach(provider.authModes,id:\.self){Text($0).tag($0)}}.frame(maxWidth:130);TextField("Optional endpoint",text:$endpoint);Button("Add"){let name=label;label="";Task{await store.add(provider:provider,label:name,auth:auth.isEmpty ? provider.defaultAuthMode:auth,endpoint:endpoint)}}.disabled(label.isEmpty) }
        if let active,["api_key","gateway"].contains(active.authMode){HStack{SecureField(active.credentialSet ? "Replace Keychain credential":"Credential",text:$credential);Button("Save to Keychain"){let value=credential;credential="";Task{await store.credential(provider:provider.id,profileID:active.id,value:value)}}.disabled(credential.isEmpty)}}
    }.padding(.top,8).onAppear{if auth.isEmpty{auth=provider.defaultAuthMode}} }
}

private extension Color { init(hex:String){let text=hex.trimmingCharacters(in:CharacterSet(charactersIn:"#"));let raw=UInt64(text,radix:16) ?? 0x8B5CF6;self.init(red:Double((raw>>16)&255)/255,green:Double((raw>>8)&255)/255,blue:Double(raw&255)/255)} }
