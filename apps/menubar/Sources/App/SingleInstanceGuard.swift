import Foundation


enum SingleInstanceGuard {
    private static var fd: Int32 = -1


    @discardableResult
    static func acquire() -> Bool {
        let dir = (FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
                   ?? URL(fileURLWithPath: NSTemporaryDirectory()))
            .appendingPathComponent("io.coordharness.menubar.mac", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let path = dir.appendingPathComponent("instance.lock").path
        fd = open(path, O_CREAT | O_RDWR, 0o644)
        guard fd >= 0 else { return true }
        if flock(fd, LOCK_EX | LOCK_NB) != 0 { close(fd); fd = -1; return false }
        return true
    }
}
