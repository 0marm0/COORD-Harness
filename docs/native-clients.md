# Native clients

Status: **Preview** in [`feature-status.json`](feature-status.json). The clients are clean-room, read-only views by default in this branch — `CoordMenuBar` has one opt-in write path, see [Data contract](#data-contract) below; there is no app-store, signing, or public distribution claim.

## Scope

| Client | Target | User surface |
|---|---|---|
| `CoordCockpitMac` | macOS 14+ | Desktop window plus `MenuBarExtra` |
| `CoordCockpitIOS` | iOS 17+ | Home, Jobs, and Settings |
| `CoordCockpitTests` | Host test target | Shared model, transport, cache, and view-model checks |

The source project is generated from [`../apps/project.yml`](../apps/project.yml) with XcodeGen:

```bash
cd apps
xcodegen generate
```

This creates `apps/CoordCockpit.xcodeproj`. The exact unsigned build/test commands live in [`../apps/README.md`](../apps/README.md) and pass `CODE_SIGNING_ALLOWED=NO`, so CI does not require signing secrets.

## Data contract

The four native targets split into two families, and they do not read the same way.

**`CoordCockpitIOS` and `CoordCockpitMac` — HTTP only.** They use `URLSession` for `GET` requests to:

- `/api/v1/snapshot`
- `/healthz`

The base URL is configurable and may use HTTP or HTTPS. Neither target links any SQLite source, so
the snapshot endpoint is the only way they see a board.

**`CoordMenuBar` and `CoordCockpitWindow` — read-only SQLite, with HTTP as the fallback.** They open
the database named by `COORD_DB` directly, with `SQLITE_OPEN_READONLY` and `PRAGMA query_only=ON`, and
fall back to the HTTP snapshot when that path is unavailable. What they read is not the lifecycle
tables but a pre-materialised `native_cockpit_*` projection, gated on a schema version the app pins —
so a projection migration does require an app release for these two. Do not expect the snapshot
contract to insulate them.

Both families keep a last-good local snapshot so the UI can explain temporary unavailability without
inventing live state.

None of the four open `coord.db` for anything but read-only, query-only SQLite access, and
none implement lifecycle actions, process controls, signing team IDs, or privileged
entitlements, or an alternate status resolver.

**One exception, opt-in and off by default:** `CoordMenuBar` (only) can POST an operator
reassignment to the board server's `/api/native/action` route, through
[`NativeOperatorTokenSource.swift`](../apps/menubar/Sources/Cockpit/Core/NativeOperatorTokenSource.swift)
and
[`NativeCockpitActionBroker.swift`](../apps/menubar/Sources/Cockpit/UI/NativeCockpitActionBroker.swift).
This requires the server operator to set `COORD_NATIVE_OPERATOR_WRITES=1` and have a private,
uid-owned operator-token file present; the request carries that token as a bearer credential.
`CoordCockpitWindow`, `CoordCockpitIOS`, and `CoordCockpitMac` implement no such path. See
[`threat-model.md` §2.8](threat-model.md#28-the-read-only-projection--survives-with-two-precise-caveats)
for the full gating.

The bundle identifiers are clean-room placeholders under `org.coordharness.cockpit.*`; no developer team identifier is stored.

## Build posture

The branch was regenerated with XcodeGen 2.45.4 and validated with Xcode 26.6: the unsigned macOS suite passed 17 tests with zero failures, and the unsigned iOS Simulator build succeeded. Treat these as branch-local receipts, not a broad toolchain guarantee; CI re-runs unsigned builds.

## Verified synthetic captures

Both clients below consume the same fixed-clock fictional board as the browser screenshots.

<p align="center">
  <img src="assets/screens/macos-cockpit.png" alt="COORD Cockpit window on the synthetic board." width="100%">
</p>

<p align="center">
  <img src="assets/screens/macos-panel.png" alt="COORD menu-bar panel on the synthetic board, one row expanded." width="380">
</p>

<p align="center">
  <img src="assets/screens/ios-home.png" alt="iOS Home view with summary metrics, fictional work rows, and three read-only tabs." width="320">
</p>

The PNGs are exact-window or simulator captures; hashes, dimensions, methods, device details, and the synthetic source clock live in [`assets/provenance.json`](assets/provenance.json).

<p align="center">
  <img src="assets/projection-topology.svg" alt="The iOS and macOS window clients read the versioned snapshot and health endpoints over HTTP, while the menu bar and Cockpit window read coord.db directly, read-only." width="100%">
</p>

See [web board](web-board.md) for the producer and [compatibility](compatibility.md) for versioning expectations.
