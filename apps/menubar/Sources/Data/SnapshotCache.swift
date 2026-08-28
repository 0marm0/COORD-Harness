import Foundation


enum SnapshotCache {
    static let url: URL = {
        let base = (FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
                    ?? URL(fileURLWithPath: NSTemporaryDirectory()))
            .appendingPathComponent("io.coordharness.menubar.mac", isDirectory: true)
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        return base.appendingPathComponent("last_menubar.json")
    }()


    static func save(_ data: Data) {
        let tmp = url.appendingPathExtension("tmp")
        guard (try? data.write(to: tmp, options: .atomic)) != nil else { return }
        try? FileManager.default.removeItem(at: url)
        _ = try? FileManager.default.moveItem(at: tmp, to: url)
    }


    static func load() -> MenubarState? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        let dec = JSONDecoder(); dec.keyDecodingStrategy = .convertFromSnakeCase
        guard var s = try? dec.decode(MenubarState.self, from: data) else { return nil }
        s.stale = true
        return s
    }
}
