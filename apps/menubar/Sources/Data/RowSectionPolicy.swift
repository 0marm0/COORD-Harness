import Foundation

/// Which section of the panel a work row belongs to.
enum RowSection: String, Equatable {
    case running
    case attention
    case followup
    case next
    case status
}

/// Decide a row's section from the server's bucket, falling back to its status.
///
/// The board server already sorts every row into a bucket. The client used to
/// re-derive the section instead, and read the row's GROUP key where it meant
/// to read that bucket -- the group key being the row's epic, or since session
/// grouping, the chat that owns it. A group key never contains "running" or
/// "attention", so every bucket branch was dead and the section came entirely
/// from the status string, with anything the list did not name falling through
/// to `next`. On a live board that put sixty-nine PAUSED rows -- work a session
/// is holding -- under NEXT UP, beside a hundred and twenty-four rows nobody
/// had started.
///
/// So the bucket is asked first and the status is the fallback for sources that
/// send no bucket at all. PAUSED sits with RUNNING there: paused work is held
/// by a session and belongs beside the work it is holding.
///
/// This is deliberately a free function over plain values. It carries no
/// transport and no models, which is what lets the section rule be tested
/// without building the whole app.
func rowSection(
    bucket: String?,
    groupKey: String?,
    status: String?,
    paused: Bool?,
    live: Bool?
) -> RowSection {
    let scope = (bucket ?? groupKey ?? "").lowercased()
    let state = (status ?? "").uppercased()
    if scope.contains("follow") { return .followup }
    if scope.contains("attention") || ["BLOCKED", "FAILED", "STALLED"].contains(state) {
        return .attention
    }
    if scope.contains("running") || scope == "now" || state == "RUNNING" || state == "PAUSED"
        || paused == true || live == true {
        return .running
    }
    if ["DONE", "KILLED", "CANCELLED", "CANCELED"].contains(state) { return .status }
    return .next
}
