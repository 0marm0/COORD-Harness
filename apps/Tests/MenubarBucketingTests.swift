import Foundation
import XCTest
@testable import CoordCockpitMac

/// Sections must come from the server's bucket, not be re-derived from status.
///
/// On a live board a hundred and ninety-three rows sat under NEXT UP. A hundred
/// and twenty-four were genuinely queued; the other sixty-nine were PAUSED --
/// work a session is holding, listed as work nobody had started. The cause was
/// that the section rule read the row's GROUP key where it meant to read the
/// server's bucket. A group key is an epic, or since session grouping the chat
/// that owns the row; it never contains "running" or "attention", so every
/// bucket branch was dead and the section came entirely from the status string,
/// with anything unnamed -- PAUSED included -- falling through to `next`.
final class MenubarBucketingTests: XCTestCase {

    func testPausedWorkIsHeldBesideRunningNotQueuedAsNext() {
        XCTAssertEqual(
            rowSection(
                bucket: "running", groupKey: "EPIC-CONTROL-PLANE",
                status: "PAUSED", paused: true, live: nil
            ),
            .running
        )
        XCTAssertEqual(
            rowSection(
                bucket: "next", groupKey: "EPIC-CONTROL-PLANE",
                status: "QUEUED", paused: nil, live: nil
            ),
            .next
        )
    }

    func testPausedIsHeldEvenWhenNoBucketIsSent() {
        // The status fallback must agree with the bucket rule, or a source that
        // sends no bucket re-creates the defect on its own.
        XCTAssertEqual(
            rowSection(bucket: nil, groupKey: nil, status: "PAUSED", paused: nil, live: nil),
            .running
        )
    }

    func testTheServersBucketDecidesTheSectionEvenWhenTheStatusIsUnknown() {
        // A status the client has never heard of must still land where the
        // server put it. Re-deriving from the status string is what sent
        // everything unrecognised to NEXT UP.
        for (bucket, expected) in [
            ("running", RowSection.running),
            ("attention", RowSection.attention),
            ("followup", RowSection.followup),
        ] {
            XCTAssertEqual(
                rowSection(
                    bucket: bucket, groupKey: "EPIC-CONTROL-PLANE",
                    status: "REHYDRATING", paused: nil, live: nil
                ),
                expected,
                "bucket \(bucket) was overridden by an unknown status"
            )
        }
    }

    func testAGroupKeyIsNeverReadAsASectionNameWhenABucketIsPresent() {
        // A row grouped under a chat whose label happens to contain a section
        // word must be filed by its bucket, not by that coincidence.
        XCTAssertEqual(
            rowSection(
                bucket: "next", groupKey: "claude:running-repairs",
                status: "QUEUED", paused: nil, live: nil
            ),
            .next
        )
    }

    func testTheGroupKeyIsStillTheFallbackWhenNoBucketIsSent() {
        // Older sources put the bucket in the group slot; dropping that
        // fallback would silently refile every one of their rows.
        XCTAssertEqual(
            rowSection(bucket: nil, groupKey: "attention", status: "QUEUED", paused: nil, live: nil),
            .attention
        )
    }
}
