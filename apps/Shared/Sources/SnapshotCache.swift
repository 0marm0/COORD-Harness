import Foundation

protocol SnapshotCaching: Sendable {
    func load() throws -> NativeSnapshotV1?
    func save(_ snapshot: NativeSnapshotV1) throws
}

enum SnapshotCacheError: LocalizedError, Equatable {
    case corrupt
    case loadFailed
    case saveFailed

    var errorDescription: String? {
        switch self {
        case .corrupt: "The last-good snapshot is corrupt and was not loaded."
        case .loadFailed: "The last-good snapshot could not be loaded."
        case .saveFailed: "Live data is available, but it could not be saved for offline use."
        }
    }
}

struct SnapshotCache: SnapshotCaching, Sendable {
    private let fileURL: URL

    init(fileManager: FileManager = .default) {
        let base = fileManager.urls(for: .cachesDirectory, in: .userDomainMask).first
            ?? fileManager.temporaryDirectory
        fileURL = base
            .appending(path: "org.coordharness.cockpit", directoryHint: .isDirectory)
            .appending(path: "snapshot-v1.json")
    }

    init(fileURL: URL) {
        self.fileURL = fileURL
    }

    func load() throws -> NativeSnapshotV1? {
        guard FileManager.default.fileExists(atPath: fileURL.path) else { return nil }
        do {
            let data = try Data(contentsOf: fileURL)
            do {
                return try SnapshotCoding.decoder().decode(NativeSnapshotV1.self, from: data).validated()
            } catch {
                throw SnapshotCacheError.corrupt
            }
        } catch let error as SnapshotCacheError {
            throw error
        } catch {
            throw SnapshotCacheError.loadFailed
        }
    }

    func save(_ snapshot: NativeSnapshotV1) throws {
        do {
            let directory = fileURL.deletingLastPathComponent()
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let data = try SnapshotCoding.encoder().encode(snapshot)
            try data.write(to: fileURL, options: .atomic)
        } catch {
            throw SnapshotCacheError.saveFailed
        }
    }
}
