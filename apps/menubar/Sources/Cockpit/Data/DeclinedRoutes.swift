import Foundation

/// Routes the server has said it does not serve, remembered for the process.
///
/// Three of this client's probes answer 404 against the coordination board, and
/// each is a deliberate decision rather than a gap: serving them would fold
/// absent counts into zeros and present a read-only board as a control surface.
/// The client asked anyway, on a 1.5-second timer, forever — 71 probes of one
/// route in a single short session, 489 in a longer one.
///
/// A 404 or a 501 is a durable statement about a route, so it is believed the
/// first time. Every other failure still retries, because a board that is
/// merely down will come back, and a client that gave up on a restart would be
/// the worse bug.
///
/// The same URL is polled by more than one source, which is why this is shared
/// state rather than a flag on either of them: latching one left the other
/// asking.
enum DeclinedRoutes {
    private static let lock = NSLock()
    private static var declined: [String: Int] = [:]

    /// Status codes that describe the route itself rather than this attempt.
    static func isDurableDecline(_ status: Int) -> Bool {
        status == 404 || status == 501
    }

    static func record(_ url: URL, status: Int) {
        guard isDurableDecline(status) else { return }
        lock.lock()
        defer { lock.unlock() }
        declined[key(url)] = status
    }

    /// The status the server declined with, or nil if it never has.
    static func declinedStatus(_ url: URL) -> Int? {
        lock.lock()
        defer { lock.unlock() }
        return declined[key(url)]
    }

    /// Test seam: a fresh process has asked nothing yet.
    static func reset() {
        lock.lock()
        defer { lock.unlock() }
        declined.removeAll()
    }

    /// Path only. The same route on the same client is one decision, and the
    /// query string on these probes is a fixed profile rather than a variable.
    private static func key(_ url: URL) -> String {
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            return url.absoluteString
        }
        return "\(parts.host ?? ""):\(parts.port ?? -1)\(parts.path)"
    }
}
