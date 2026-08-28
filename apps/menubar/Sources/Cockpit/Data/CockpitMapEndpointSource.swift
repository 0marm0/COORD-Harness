import Foundation

enum CockpitMapEndpointDecoder {
    static func decodeKnowledgeGraph(_ data: Data) throws -> CockpitMapKnowledgeGraph {
        try decoder.decode(CockpitMapKnowledgeGraph.self, from: data)
    }

    static func decodeMachineHealth(_ data: Data) throws -> CockpitMachineHealth {
        try decoder.decode(CockpitMachineHealth.self, from: data)
    }

    static func decodeProvenance(_ data: Data) throws -> CockpitMapProvenance {
        try decoder.decode(CockpitMapProvenance.self, from: data)
    }

    private static var decoder: JSONDecoder {
        JSONDecoder()
    }
}

final class CockpitMapEndpointSource {
    private let baseURL: URL
    private let session: URLSession

    init(
        baseURL: URL = URL(string: "\(HarnessEndpoint.base)")!,
        session: URLSession = .shared
    ) {
        self.baseURL = baseURL
        self.session = session
    }

    func loadKnowledgeGraph() async -> CockpitMapKnowledgeGraph? {
        await load(path: "/api/map/graph", decode: CockpitMapEndpointDecoder.decodeKnowledgeGraph)
    }

    func loadMachineHealth() async -> CockpitMachineHealth? {
        await load(path: "/api/health", decode: CockpitMapEndpointDecoder.decodeMachineHealth)
    }

    func loadProvenance(vertical: String) async -> CockpitMapProvenance? {
        var components = URLComponents(url: baseURL.appendingPathComponent("/api/map/provenance"), resolvingAgainstBaseURL: false)
        components?.queryItems = [URLQueryItem(name: "vertical", value: vertical)]
        guard let url = components?.url else { return nil }
        return await load(url: url, decode: CockpitMapEndpointDecoder.decodeProvenance)
    }

    private func load<T>(path: String, decode: (Data) throws -> T) async -> T? {
        await load(url: baseURL.appendingPathComponent(path), decode: decode)
    }

    private func load<T>(url: URL, decode: (Data) throws -> T) async -> T? {
        do {
            let (data, response) = try await session.data(from: url)
            if let http = response as? HTTPURLResponse, http.statusCode != 200 {
                return nil
            }
            return try decode(data)
        } catch {
            return nil
        }
    }
}
