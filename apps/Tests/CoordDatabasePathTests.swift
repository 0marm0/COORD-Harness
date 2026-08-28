import XCTest
@testable import CoordCockpitMac

final class CoordDatabasePathTests: XCTestCase {
    func testExplicitDatabaseWinsOverPersistedSelection() {
        XCTAssertEqual(
            CoordDatabasePath.resolve(
                environment: ["COORD_DB": "/authority/coord.db"],
                persistedPath: "/persisted/coord.db",
                homeDirectory: "/home/operator"
            ),
            "/authority/coord.db"
        )
    }

    func testPersistedSelectionSupportsLaunchServicesAndExpandsHome() {
        XCTAssertEqual(
            CoordDatabasePath.resolve(
                environment: [:],
                persistedPath: "~/.coordharness/coord.db",
                homeDirectory: "/home/operator"
            ),
            "/home/operator/.coordharness/coord.db"
        )
    }

    func testLegacyAndAmbientPathsDoNotBecomeDatabaseAuthority() {
        XCTAssertNil(
            CoordDatabasePath.resolve(
                environment: [
                    "COORD_COORD_DB": "/legacy/coord.db",
                    "COORD_HOME": "/state",
                    "COORD_PROJECT_ROOT": "/private/checkout",
                ],
                persistedPath: nil,
                homeDirectory: "/home/operator"
            )
        )
    }

    func testMissingSelectionIsSetupRequiredInsteadOfHomeOrCheckoutFallback() {
        XCTAssertNil(
            CoordDatabasePath.resolve(
                environment: [:],
                persistedPath: "  ",
                homeDirectory: "/home/operator"
            )
        )

        XCTAssertThrowsError(try CoordSQLite.openReadOnly(path: "")) { error in
            let message = String(describing: error)
            XCTAssertTrue(message.contains("COORD setup is required"))
            XCTAssertTrue(message.contains("apps/install.sh"))
        }
    }

    func testSelectedMissingDatabaseHasActionableDiagnostic() {
        let missing = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
            .appendingPathComponent("coord.db")

        XCTAssertThrowsError(try CoordSQLite.openReadOnly(path: missing.path)) { error in
            let message = String(describing: error)
            XCTAssertTrue(message.contains("Verify the database selected during COORD setup"))
            XCTAssertTrue(message.contains(missing.path))
        }
    }
}
