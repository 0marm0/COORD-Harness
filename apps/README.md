# Coord Cockpit native apps

SwiftUI preview clients for a local COORD board. The work, job, graph, and health
surfaces consume read models and retain a local last-good snapshot when the endpoint
is unavailable. Provider-account controls can issue POST requests to the board's fixed-loopback
adapter when an upstream is explicitly configured; they are not a standalone provider login
implementation and never write lifecycle state.

HTTPS is required for non-loopback hosts; plain HTTP is accepted only for `localhost`, the
IPv6 loopback address, or an address in the IPv4 loopback block. The server has no
authentication, so non-loopback use is not supported even over HTTPS.

## One-command macOS install

Requirements: macOS, Python 3.11 or newer, Xcode, and XcodeGen.

From a checkout, run:

```sh
apps/install.sh
```

The installer performs the complete local first run:

1. creates an isolated runtime at `~/Library/Application Support/COORD/venv` and
   installs a non-editable copy of the Python package, so the service does not depend
   on the source checkout;
2. creates or migrates `~/.coordharness/coord.db` without replacing existing rows;
3. persists the exact database and `http://127.0.0.1:7870` endpoint for GUI-launched apps;
4. installs and starts `~/Library/LaunchAgents/org.coordharness.board.plist`;
5. verifies `/healthz` and `/api/v1/snapshot` on port 7870; and
6. builds, ad-hoc signs, and installs `COORD.app` and `COORD Cockpit.app` in
   `~/Applications`.

Select an existing or differently located authority explicitly with
`apps/install.sh --db /absolute/path/to/coord.db`. `COORD_DB` is also accepted.
An explicit `COORD_DB` process environment wins over the persisted selection. There
is no compiled-checkout, current-directory, `COORD_HOME`, `COORD_PROJECT_ROOT`, legacy
variable, or private-project database fallback. With neither explicit source, the
native direct-DB reader reports that setup is required; it does not create a database.

The installed menu bar uses the board API as its primary read contract. The cockpit
loads the service projection first and currently retains an exact-path, read-only SQLite
reader for richer native projections, with an HTTP fallback and visible load diagnostics.
That transitional SQLite reader resolves the same database path persisted by the installer.

Repair is the same command: package code and app bundles are replaced, migrations are
applied in place, and the selected database is preserved. Successful completion prints
the exact endpoint, database, LaunchAgent, and app paths.

Uninstall the runtime, service, and exact app bundles with:

```sh
apps/uninstall.sh
```

Uninstall preserves the selected `coord.db`, `~/.coordharness`, and native preferences
by default. Back up and remove lifecycle data separately only when that data deletion is
intentional.

## Development builds

Requirements: macOS with Xcode 26.6 and XcodeGen.

Generate the project:

```sh
cd apps
xcodegen generate
```

Build and test macOS without signing:

```sh
xcodebuild -project CoordCockpit.xcodeproj -scheme CoordMenuBar -configuration Debug -destination 'platform=macOS' CODE_SIGNING_ALLOWED=NO build
xcodebuild -project CoordCockpit.xcodeproj -scheme CoordCockpitWindow -configuration Debug -destination 'platform=macOS' CODE_SIGNING_ALLOWED=NO build
xcodebuild -project CoordCockpit.xcodeproj -scheme CoordCockpitMac -configuration Debug -destination 'platform=macOS' CODE_SIGNING_ALLOWED=NO test
```

Build iOS for the simulator without signing:

```sh
xcodebuild -project CoordCockpit.xcodeproj -scheme CoordCockpitIOS -configuration Debug -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO build
```

Run from Xcode by opening `apps/CoordCockpit.xcodeproj` and selecting
`CoordMenuBar`, `CoordCockpitWindow`, or `CoordCockpitIOS`. The public macOS and
Python board default is `http://127.0.0.1:7870`.

Before building the apps, verify the backend independently:

```sh
COORD_DB=/absolute/path/to/coord.db coord-board --db "$COORD_DB" --host 127.0.0.1 --port 7870
curl --fail http://127.0.0.1:7870/healthz
curl --fail http://127.0.0.1:7870/api/v1/snapshot >/dev/null
```

The deterministic synthetic fixture is `Fixtures/snapshot-v1.json`. Bundle identifiers are `org.coordharness.cockpit.mac`, `org.coordharness.cockpit.ios`, and `org.coordharness.cockpit.tests`; no team or signing identity is configured.
