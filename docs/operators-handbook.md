# Operator's handbook

Running the harness day to day: one command to bring the whole thing up, what each
surface shows, how to drive every operation from MCP with the CLI as its twin, and
what to do when something that used to work stops.

[Getting started](getting-started.md) is the first-run guide — install, seed, claim one
row. This page assumes that has already happened at least once and is about the
recurring day.

Every claim below names a module, a script line, a test, or a command whose output is
reproduced verbatim. Where a control exists in the UI but has nothing behind it in this
repository, this page says so rather than describing the intent.

---

## 1. One command

```bash
./scripts/demo.sh              # seed if needed, then serve the web board
./scripts/demo.sh --native     # also build and launch the two macOS clients
./scripts/demo.sh --reset      # discard var/demo and seed a fresh board
```

[`scripts/demo.sh`](../scripts/demo.sh) is the whole harness against a synthetic board.
It is the fastest way to see every surface at once and the only path that wires the
native clients to a board deliberately rather than by default.

### What it seeds

On first run, or after `--reset`, it creates `var/demo/` and runs
[`coordharness.demo`](../src/coordharness/demo.py) against it. The seeder prints its own
counts; from a fresh seed in a scratch project:

```text
seeding demo board at …/proj/.coordharness/coord.db
  sessions     8
  work_items   37
  claims       10
  events       5
  job sidecars 10
```

The estate is fictional and deterministic under `SOURCE_DATE_EPOCH`
([`demo.py`](../src/coordharness/demo.py), `_synthetic_clock`). Rows are `UI-*`, `ML-*`,
`PLT-*`, `SRCH-*`, `OPS-*`, `TASK-*` under five `INIT-*` epics. Eighteen of them declare
a `done_signal` under `docs/reports/`, which is what makes the completion gate
exercisable rather than theoretical.

### What it guards

| Guard | Where | Why it is there |
|---|---|---|
| All state under `var/demo/` inside the repo | [`demo.sh`](../scripts/demo.sh) `DEMO="$REPO/var/demo"` | The demo can never reach a real board. `/var/` is gitignored twice over (`.gitignore:16`, `.gitignore:38`). |
| `COORD_DB` passed explicitly to each client | [`demo.sh`](../scripts/demo.sh), the native launch loop | Without it the clients fall back to a default location; on a machine already running a board they would attach to that one. |
| `git init` inside `var/demo` | [`demo.sh`](../scripts/demo.sh), the seeding branch | The completion gate requires proof committed to version control, so the demo project has to be a real repository, not a directory. |
| Binaries launched directly, never through `open` | [`demo.sh`](../scripts/demo.sh), the `--native` launch loop | `open(1)` does not pass environment variables through, so the clients would ignore `COORD_DB`. See [§7.3](#73-a-client-that-ignores-coord_db). |
| `xcodegen` presence checked before building | [`demo.sh`](../scripts/demo.sh) `command -v xcodegen` | Fails with `brew install xcodegen` rather than an Xcode error forty lines deep. |
| WAL folded in at the end of the seed | [`demo.py:406-407`](../src/coordharness/demo.py) | A WAL database needs a `-shm` file even for a read-only open. See [§7.2](#72-sqlite_cantopen-on-a-database-that-is-plainly-there). |
| Server killed on exit | [`demo.sh`](../scripts/demo.sh) `trap 'kill $SERVER'` | Ctrl-C leaves nothing listening on the port. |

`COORD_BOARD_PORT` overrides the port; the default is `7870`
([`server.py`](../src/coordharness/board/server.py), `DEFAULT_PORT`). `--native` additionally writes
`var/demo/.coordharness/menubar_panel_config.json` with `transport: "db"`, which is what
makes the panel read the database rather than poll HTTP
([`Config.swift:26`](../apps/menubar/Sources/Data/Config.swift),
[`HarnessClient.swift:13-29`](../apps/menubar/Sources/Data/HarnessClient.swift)).

Reusing an existing `var/demo` is the default; the script says which it did.

---

## 2. The panel and the cockpit

Two macOS clients, both read-only, both built by `--native` from
[`apps/project.yml`](../apps/project.yml). Full build and test commands are in
[`apps/README.md`](../apps/README.md); scope and the data contract are in
[native clients](native-clients.md).

| App | Target | Product name | Where it appears |
|---|---|---|---|
| **COORD** | `CoordMenuBar` | `COORD.app` | The system menu bar. `LSUIElement: YES` — no Dock icon by design. |
| **COORD Cockpit** | `CoordCockpitWindow` | `COORD Cockpit.app` | The Dock. |

Both set `PRODUCT_NAME` and `CFBundleDisplayName` explicitly, because without them the
bundle falls back to the target name and the app switcher shows a build-system
identifier ([`project.yml`](../apps/project.yml), the comment above
`INFOPLIST_KEY_CFBundleDisplayName`).

### 2.1 The panel

A progress ring in the menu bar (`statusItemMode` defaults to `eta_ring`,
[`Config.swift:23`](../apps/menubar/Sources/Data/Config.swift)) and a popover built from
seven sections, in source order: an unlabelled lead section, then `Queued (backlog)`,
`Later`, `Needs Attention`, `Follow-up`, `By Initiative` and `Local Queue`
([`ContentStackAndRows.swift:209-395`](../apps/menubar/Sources/UI/ContentStackAndRows.swift),
the `SectionHeader(label:)` sites). Three default to collapsed —
`attentionCollapsed`, `followupCollapsed`, `localQueueCollapsed`
([`Config.swift:16-18`](../apps/menubar/Sources/Data/Config.swift)).

Defaults worth knowing: refresh every 2s, 9 rows visible before the fold, `⌘,` to open
the panel, and `panelDetached` / `panelAlwaysOnTop` both off
([`Config.swift:6-32`](../apps/menubar/Sources/Data/Config.swift)). Point
`COORD_MENUBAR_CONFIG` at a JSON file to change any of them. A single-instance flock in
Application Support stops a second copy taking the menu bar
([`SingleInstanceGuard.swift`](../apps/menubar/Sources/App/SingleInstanceGuard.swift)).

### 2.2 The mode slider and pause — read this before you use them

The slider has three stops, drawn `L`, `M`, `H` for light, medium and full
([`ModeSlider.swift:6-11`](../apps/menubar/Sources/UI/ModeSlider.swift)). A paused
harness draws `⏸` and greys the tint. Releasing the knob holds the requested stop
optimistically for 20 seconds until the live mode agrees, so the control does not snap
back while the request is in flight (`setLiveMode`, `ModeSlider.swift:24-36`).

Both controls are HTTP writers:

| Control | Request | Source |
|---|---|---|
| Mode slider | `POST {COORD_BOARD_URL}/api/mode?set=<light\|medium\|full>` | [`HarnessClient.swift:654-658`](../apps/menubar/Sources/Data/HarnessClient.swift) |
| Pause / Resume all | `POST {COORD_BOARD_URL}/api/bulk_control?action=pause\|resume` | [`HarnessClient.swift:672-681`](../apps/menubar/Sources/Data/HarnessClient.swift) |

**This repository's board serves neither route.** It handles `GET`, `HEAD` and
`OPTIONS`; every other method returns `405`
([`server.py`](../src/coordharness/board/server.py), `_readonly` bound to `do_POST`).
Driven against a demo board on an ephemeral port:

```text
POST /api/mode?set=light        -> 405 Allow: GET, HEAD, OPTIONS
POST /api/bulk_control?action=pause -> 405 Allow: GET, HEAD, OPTIONS
```

So in this repository the slider moves, holds for 20 seconds, and returns to whatever
the board reports. Nothing is broken and nothing is written. The controls are ported UI
for a writable console that is not part of this extraction; the read-only boundary is
deliberate and documented in [web board](web-board.md).

The shell twin, [`scripts/set_mode.sh`](../scripts/set_mode.sh), is a different mechanism
with the same vocabulary: `{full|medium|light|pause|auto}` written to
`$COORD_HOME/resource_mode.txt` plus a `governor_trigger` stamp, with `auto` resolving
from HID idle time and AC power.

What reads what, measured rather than assumed:

- `resource_mode.txt` **is read**, by exactly one consumer: `_resource_mode()` in
  [`board_context.py:170`](../src/coordharness/coord/board_context.py), which surfaces it
  as the `resource_mode` key of the `capsule` lens
  ([`board_context.py:82`](../src/coordharness/coord/board_context.py) fixes the path to
  `$COORD_HOME/resource_mode.txt`). Verified end to end: `set_mode.sh light` against a
  scratch `COORD_HOME`, then `_resource_mode()` returns `'light'`.
- `governor_trigger` is written and **never read** — no other module in the repository
  mentions it.
- `resource_modes.json` (plural, a different file) is read by
  [`coord/config.py:150-195`](../src/coordharness/coord/config.py) for
  `harness_autonomy_config`, which defaults to disabled. It does not ship, so the script
  prints `WARN: … not found — proceeding anyway` (`set_mode.sh:104-105`) and continues.

So `set_mode.sh` sets a value one read-only lens will display. Nothing schedules work off
it and nothing in this repository enforces a mode, which is the part that matters before
you reach for it as a throttle. [`scripts/mem_governor.sh`](../scripts/mem_governor.sh)
is unrelated: a memory admission gate (`wait`/`check`/`status`) with a 6 GB safety
buffer (`SAFETY_GB=6`).

### 2.3 The cockpit and its Map tab

The window is the whole board as a table, with a two-item segmented control:

| Tab | Loads |
|---|---|
| Cockpit | The native table, grouped and sortable |
| Map | The web coordination map, embedded |

Registered as `[("Cockpit", "/cockpit"), ("Map", "/map")]` in
[`CockpitRootView.swift:507`](../apps/menubar/Sources/Cockpit/UI/CockpitRootView.swift).
The Map tab is a `WKWebView` loading
`{COORD_BOARD_URL}/cockpit?native_map=1` with an 8-second timeout and cache bypass
([`CockpitMapWebView.swift:62-72`](../apps/menubar/Sources/Cockpit/UI/CockpitMapWebView.swift)).
The `native_map=1` parameter is what makes the page drop its own masthead so two headers
do not stack ([`cockpit.js`](../src/coordharness/board/static/cockpit.js), `start()`).

Consequences for the operator:

- **The Map tab needs the board running.** The table does not — with `transport: "db"`
  it reads `coord.db` directly. Blank map, populated table means the web server is down,
  not the database.
- **The map unloads the moment you leave it**, and again when the window closes
  ([`CockpitMapLifecycle.swift`](../apps/menubar/Sources/Cockpit/Core/CockpitMapLifecycle.swift):
  `surfaceChangeAction` returns `.unloadNow`, `windowCloseAction` returns `.unloadNow`).
  Re-entering the tab is a fresh load, not a resume.

### 2.4 What the native clients read, and its one stale edge

With `transport: "db"` — the default, [`Config.swift:26`](../apps/menubar/Sources/Data/Config.swift) —
the clients read a materialised projection inside `coord.db`, not the coordination tables
directly. **A board changed after seeding shows the seeded shape to the native clients
until it is re-seeded.** Measured: `coord claim OPS-503` then `coord done OPS-503` on a
freshly seeded board leaves `native_cockpit_rows` at 39 rows, `writer_seq` 1, and
`OPS-503` still `PLANNED`, while `coord board` reports it `done`.

The mechanism has three parts, and it is worth separating them because only one of them
is a missing call:

| Path | What it does | Effect here |
|---|---|---|
| `native_cockpit.refresh` | Rebuilds the projection outright | Called from exactly one place, the demo seeder ([`demo.py:402`](../src/coordharness/demo.py)) |
| `native_cockpit.request_refresh` | Enqueues a rebuild in `native_projection_refresh_queue` | Called by the MCP writers (`_refresh_native_cockpit`, eleven sites in [`mcp_coord_server.py`](../src/coordharness/coord/mcp_coord_server.py)) and by [`reaper.py`](../src/coordharness/coord/reaper.py) |
| `native_cockpit.flush_requested_refresh` | Drains that queue | Called only from `reaper.py` — see §2.5: the reaper now has a console script and an opt-in schedule, but nothing runs it unless an operator installs or invokes that schedule |

So the CLI never enqueues anything, and an MCP write enqueues a rebuild that nothing
drains **unless the reaper runs** (§2.5). Both surfaces end up stale for different
reasons. The web board is unaffected — it reads the coordination tables through its own
materialised copy on each refresh.

The packaged clients probe three endpoints against this port. One is served and two are
not — measured against a demo board on an ephemeral port:

```text
GET /api/menubar              -> 200
GET /api/state/compact        -> 404
GET /api/capability_inventory -> 404
```

`/api/menubar` **is served**, by `_menubar_document` over one atomic read of the cached
snapshot ([`server.py`](../src/coordharness/board/server.py), the `/api/menubar` branch of `do_GET`). The two 404s are
deliberate, and the reasoning is written out above the handler
([`server.py`](../src/coordharness/board/server.py), the `# The native client probes.` block): each was decided by reading
its Swift decoder, against the rule that the board serves a probe only where the decoder
can express "the board does not publish this" as absence. `CockpitState`'s summary holds
non-optional `Int`s filled in with `?? 0`, so any document at all would render "0 done
today" over a board that has finished work; `capability_inventory: []` would assert the
harness has no capability planes. A 404 leaves each client on its own read model, which
is the true answer. Removal of the two dead probes is ranked in
[next steps §4f](next-steps.md); that entry distinguishes the served menubar
document from the two intentionally unserved capability probes.

### 2.5 The reaper: what runs it, and what happens if nothing does

[`architecture.md`](architecture.md) makes the design claim: status is derived at read
time from lease validity and process evidence, never stored and trusted. That claim is
mostly self-fulfilling — `derive_work_status()` checks a claim's `expires_at` against
"now" on every board read, so a lapsed lease reads as `attention` rather than `running`
on the very next read, with nothing to run first. What is **not** self-fulfilling is
everything downstream of noticing: putting an expired claim back into circulation for
another agent, confirming a session's process actually died rather than just outliving
its last heartbeat, and draining the native-cockpit refresh queue from §2.4. All three
are one function, [`reaper.run_reaper()`](../src/coordharness/coord/reaper.py), and
until this change nothing called it outside a test.

**What it does**, in the order it does them: releases claims whose lease has expired
(`release_expired_claims_batch`, table-wide — not the same as the per-row release that
`claim_work` and a typed handoff already do opportunistically for the *one* work id
they touch), reaps sessions whose process is confirmed dead by `os.kill(pid, 0)` or
which never checked in at all, finalizes runs in the same way, renews claims for fleets
still reporting live, evaluates blocked/paused continuation predicates and cross-lane
request SLAs, and drains the native-cockpit projection-refresh queue.

**What it costs when nothing runs it** — measured against this repository, not assumed:

- A claim's row stays `status='running'` in the `claims` table until either its specific
  work id is claimed or handed off again (which releases only that one row) or the
  reaper's batch sweep runs. Nobody else can pick up that work in the meantime, even
  though the board already displays it as stale.
- No read path re-checks a session's `pid` the way [`board_rows()`](../src/coordharness/coord/coord_db.py)
  already does for `runs` (`pid_matches` against `runs.pid`, fresh on every read). A
  session's claim is time-leased, not pid-checked, at read time — so a crashed agent's
  claim can read as `running` for its full lease (`LEASE_DEFAULT_S`, one hour by
  default), not just until the next board read. This is the gap "process evidence" in
  the design claim actually depends on the reaper for.
- The native-cockpit refresh queue (§2.4) is drained only here. Without it, every MCP
  write that enqueued a refresh sits enqueued forever, and the native clients keep
  showing the seeded shape.
- Continuation predicates and cross-lane SLA escalations are evaluated only here; a
  blocked row whose unblocking condition became true stays blocked until something
  checks.

**How to run it.** `coord-reaper` (added by this change) is a normal console script:

```bash
coord-reaper --dry-run          # report what would change; writes nothing
coord-reaper                    # do it — the default is mutating, unlike `coord doctor`
coord-reaper --receipt PATH     # also write the JSON report (dry runs mark it "dry_run": true)
```

`--dry-run` runs the exact same logic as a real reap against a disposable snapshot of
the database (SQLite's own backup API, not a hand-written re-implementation of "is this
expired" — a second copy of that predicate is exactly the kind of thing that drifts), so
its preview cannot lie about what a real run would release.

**How to schedule it.** Nothing schedules it by default — installing the board with
`apps/install.sh` never installs this as a side effect. Opt in explicitly:

```bash
apps/install.sh --install-reaper-agent [--reaper-interval SECONDS]   # default: 300
```

This installs a second `launchd` LaunchAgent, `org.coordharness.reaper`, that runs
`coord-reaper` against the same database on that interval. It uses `RunAtLoad` +
`StartInterval`, not `KeepAlive` — the reaper is a batch job that exits 0 every time,
and `KeepAlive` would treat that clean exit as a crash and respawn it in a tight loop.
Its stdout/stderr land in `~/Library/Logs/COORD/coord-reaper.{stdout,stderr}.log`
(`$COORD_LOG_DIR` if set). Remove it with:

```bash
launchctl bootout "gui/$(id -u)/org.coordharness.reaper"
rm ~/Library/LaunchAgents/org.coordharness.reaper.plist
```

Cron, a different scheduler, or a human running `coord-reaper` on their own cadence
work just as well — the LaunchAgent is a convenient default, not a requirement. What
matters is that *something* runs it periodically; nothing in this repository does that
for you.

---

## 3. The web board and the map

```bash
coord-board --db .coordharness/coord.db
```

Loopback on `127.0.0.1:7870` by default. Flags:
`--host --port --db --allow-remote --allowed-host --refresh-seconds`
([`server.py`](../src/coordharness/board/server.py), the `main()` argument parser). Non-loopback binding
requires **both** `--allow-remote` and an explicit `--allowed-host`, and adds no
authentication; see [web board](web-board.md) and
[security and privacy](security-and-privacy.md).

| Route | Serves |
|---|---|
| `/` | The board: Overview, Work, Jobs, Graph, Activity |
| `/map`, `/cockpit` | The coordination map: Fleet, Dependencies, Shape, Crossings, Context |
| `/api/v1/snapshot` | `NativeSnapshotV1` — the sealed contract the native clients decode |
| `/api/v1/graph` | Typed relationship edges with provenance |
| `/api/v1/context` | `ContextV1` — parent, children, dependencies, dependents, siblings, declared proof and whether it was recorded |
| `/api/v1/timeline` | Per-row event history, swapped atomically with the other three so a drawer never asks for the history of a row its snapshot lacks |
| `/api/v1/schema` | The snapshot's JSON schema |
| `/api/menubar` | The panel's probe, projected from one atomic read of the cached snapshot ([§2.4](#24-what-the-native-clients-read-and-its-one-stale-edge)) |
| `/healthz` | `{"ok":true,"read_only":true,"service":"coord-board"}` |
| `/static/<name>` | Exactly the sixteen files named in `_STATIC_ALLOWLIST`, never a glob |

`ContextV1` is built by `build_context` in
[`board/snapshot.py`](../src/coordharness/board/snapshot.py) and is **web-board only** —
there is no CLI lens and no MCP tool for it. It is structural by design: a test asserts
no event body, decision or knowledge prose reaches the response
([`tests/test_board.py:507`](../tests/test_board.py),
`test_context_document_carries_structure_and_no_prose`).

### 3.1 The keyboard model

The map is keyboard-complete; the board is not, and the difference is deliberate rather
than an oversight to work around.

| Key | Effect | Source |
|---|---|---|
| `/` | Focus the row search, from any map view. Suppressed while typing in an input and when Cmd, Ctrl or Alt is held (Shift is not checked). | [`cockpit.js`](../src/coordharness/board/static/cockpit.js), `wireNavigation` |
| `↓` `↑` | Move through the ranked results; the listbox tracks `aria-activedescendant`. | `cockpit.js`, `wireSearch` |
| `Enter` (in search) | Open the highlighted row and clear the field. | `cockpit.js`, `wireSearch` |
| `Enter` / `Space` (elsewhere) | Activate the focused element — matrix cell, graph node, work card, context entry. | `cockpit.js`, `wireNavigation` |
| `Esc` | Close the results if open, otherwise close the drawer. | `cockpit.js`, both handlers |
| `#<row-id>` in the URL | Opens that row on load; the back button walks the trail. | `cockpit.js`, `start()` and the `popstate` listener |

Every element that names a row carries `data-row` and `tabindex="0"`, so it is reachable
by Tab and served by the same delegated handlers — that is why the views can re-render
every five seconds without losing their interactivity
([`view-density.js:112-116`](../src/coordharness/board/static/view-density.js) states the
contract; `cockpit.js` and `view-flow.js` follow it).

The board at `/` defines **no keyboard shortcuts of its own** — `app.js` registers no
`keydown` handler. Its tabs are real `<button>` elements, so Tab and Enter work by the
browser's own behaviour, but there is no `/` search and no drawer there. Use `/map` when
you want to navigate rather than read.

The map polls every 5 seconds (`setInterval` in `cockpit.js`); the board at `/` has no
poll of its own — `app.js` registers neither `setInterval` nor `setTimeout`, so it shows
the state it loaded with until you reload it. The server refreshes its snapshot
independently on `--refresh-seconds`, default 2.

---

## 4. The accent switch

Top-right of the nav on both pages, mounted by
[`accent.js`](../src/coordharness/board/static/accent.js). Two choices, Green and Blue,
stored in `localStorage` under `coordharness.accent` and applied before first paint so
the page does not flash the stylesheet default.

Each accent is a **complete palette**, not one hue swapped in: ground, panel, line,
hairline, hover, both card tints, muted text and ink all shift with it (the `tokens`
block per accent in `accent.js`). That is the fix for "switch to blue looked like it had
barely done anything" — a grey with a green bias reads green whatever the accent does.

**Amber and red do not follow the accent, on purpose.** They say what a row *is* —
waiting, failed — and changing them with the accent would change what the page means
rather than how it looks. If you are adding UI, take `--green` for the accent and
`--amber`/`--red` for state, and never write a literal.

Two known holes, both already ranked:

- Sharing a URL carries the row (`#<id>`) but not the accent
  ([next steps §3f](next-steps.md)).
- Two colours once escaped the switch — an inline literal, which outranks any token, and
  a `var()` fallback whose variable was never defined. They were found by sweeping
  computed styles, not by reading the stylesheet, and there is still no regression test
  ([next steps §4g](next-steps.md)).

---

## 5. Recipes: MCP first, CLI as the twin

**MCP is the fuller surface.** `_MCP_TOOL_NAMES` declares thirty-four tools
([`mcp_coord_server.py:55-63`](../src/coordharness/coord/mcp_coord_server.py)) and all
thirty-four carry an `@mcp.tool(...)` decorator. Two of them are wrapped in a visibility
gate — `if tool_visible("runs")` and `if tool_visible("handoff_existing")` — so what a
client actually sees is resolved at import from `_server_tool_catalog()`. On the default
unattested client profile that resolves to thirty-three visible, with `handoff_existing`
deferred. Against that: sixteen CLI subcommands — `board claim create doctor done handoff
heartbeat-claim inbox note onboard release request-audit route session verdict work-context`
(`coord --help`). Review is on both surfaces: `coord request-audit` and `coord verdict` write
the same events the MCP tools do. Preflight, `park`, facts, knowledge and closeout exist only
over MCP or the Python API. When both exist, prefer MCP: it
returns structured results, carries actor and session identity explicitly, and does not
depend on which directory the shell happens to be in.

### 5.0 What actually works on a standalone board — read this first

On a standalone board — one this repository seeded, rather than the repository the server
was extracted from — **two independent guards fail closed**, and between them they take
out more of the orientation surface than the tool list suggests. Reproduced against a
freshly seeded demo board:

```text
>>> _tool_preflight(actor="codex", session_id="codex:handbook", db_path=".../coord.db")
RuntimeError: locked data-local alias: …/.coordharness/deployment-data is missing;
expected the provisioned strict-layout symlink and indexed custody entry

>>> _tool_board(db_path=".../coord.db")
ExactQueryCoreError: exact-authority policy is not enforced and active

$ python -m coordharness.coord.board_context capsule
ExactQueryCoreError: exact-authority policy is not enforced and active
```

The two guards are unrelated to each other:

- **The locked-path guard.** `_tool_preflight` calls `assert_locked_data_local(_REPO_ROOT)`
  ([`mcp_coord_server.py:3343`](../src/coordharness/coord/mcp_coord_server.py) →
  [`locked_paths.py:168`](../src/coordharness/coord/locked_paths.py)). This one is
  `preflight`-specific.
- **The exact-authority guard.** `load_query_snapshot` raises unless the
  `coord_authority_policy` singleton is in `enforce` mode with a live generation
  ([`exact_query_core.py`](../src/coordharness/coord/exact_query_core.py),
  `load_exact_query_snapshot`). A seeded board is not, so this takes down MCP `board`
  **and every `board_context` lens** — `capsule`, `digest`, `focus`, `search` and
  `skeleton` were each driven and each raised it.

**So `board_context` is not the fallback.** What works on a seeded board is the `coord`
CLI, which reads `coord_db.board_rows` directly and never touches the exact-query core:

```text
$ coord board --group-by module
{"count": 37, "rows": [{"work_id": "OPS-503", "title": "Rate limits on the public API",
  "status": "done", "group": "api", "assignee": "codex"}, …
```

`coord claim`, `heartbeat-claim`, `done`, `release`, `inbox` and `doctor` work for the
same reason, as does the whole web board. This is the open deployment-profile boundary
declared in [MCP integration](mcp-integration.md) ("Fresh-board caveat") and ranked as
[next steps §4e](next-steps.md) — not a bug to bypass. Until it closes, orient with
`coord board` and the web board at `/`, then do the work over whichever surface answers.

Note also that `preflight` and the CLI capsule are two independently written orientation
paths, not one function exposed twice: `_tool_preflight` reads `roadmap_backlog.json` and
the query snapshot; `board_context capsule` reads `coord.db`. Expect their shapes to
differ — where both are available at all.

### 5.1 A session, end to end

<table>
<tr><th width="50%">MCP — lead with this</th><th width="50%">CLI — the twin</th></tr>
<tr><td>

```text
preflight(actor="codex",
          session_id="codex:refunds")

next_work(actor="codex")

work_context(work_id="OPS-503")

claim_work(work_id="OPS-503",
           actor="codex",
           session_id="codex:refunds",
           step="documenting the rate limits")
# -> claim_id, claim_fence

heartbeat(claim_id="clm-…",
          actor="codex",
          session_id="codex:refunds",
          step="proof drafted")

complete(claim_id="clm-…",
         actor="codex",
         session_id="codex:refunds",
         artifact_path="docs/reports/ops-503.md")
```

</td><td>

```bash
coord board --group-by module

# no CLI twin for work_context

export COORD_ACTOR=codex
export COORD_SESSION_ID="codex:refunds"

coord claim OPS-503 \
  --step "documenting the rate limits"
# -> claim_id, claim_fence

coord heartbeat-claim clm-… \
  --step "proof drafted"

git add docs/reports/ops-503.md
coord done OPS-503 \
  --artifact docs/reports/ops-503.md
```

</td></tr>
</table>

**Set `COORD_ACTOR`, not `CODEX_SESSION_ID`.** `resolve_identity`
([`ingest.py:116`](../src/coordharness/coord/ingest.py)) tests for a Claude session
*first*: if `CLAUDE_CODE_SESSION_ID` is present in the environment and `COORD_ACTOR` is
unset, the actor is `claude` and `CODEX_SESSION_ID` is never consulted. Inside a Claude
Code shell the obvious incantation therefore does nothing —

```text
$ CODEX_SESSION_ID="codex:refunds" coord claim OPS-503
ValueError: cannot claim work assigned to 'codex' from 'claude' session 'claude:5b7fb366-…';
use a typed handoff/controller transition
```

— while `COORD_ACTOR=codex COORD_SESSION_ID="codex:refunds"` resolves to
`{'actor': 'codex', 'session_id': 'codex:refunds'}` and the claim succeeds.
`CODEX_SESSION_ID` is only a fallback, consulted when neither `COORD_ACTOR` nor
`CLAUDE_CODE_SESSION_ID` is set.

Both paths run the same seven policy checks, in this order, and both report them in the
response: `creation_lint`, `loop_doctor`, `token_budget`, `structured_status`,
`output_budget`, `run_event_emit`, `deferred_tool_catalog` (the `pass_order` array in a
real `coord claim` response; the pipeline itself is
[policy pipeline](policy-pipeline.md)). `output_budget` is gated by
`COORD_OUTPUT_BUDGET` and skips when no inline output is supplied.

Two rules that bite regardless of surface:

- **Keep the `claim_id`; keep the `claim_fence` private.** The ID drives heartbeats and
  terminal actions. The fence proves the same live claim still authorises a tracked job
  launch, and goes nowhere else.
- **Create the artifact with normal filesystem tools, then complete.** Neither surface
  writes files. Markdown proof must be in Git's current index; staging is enough, no
  commit required ([getting started](getting-started.md)).

### 5.2 Stopping: released, paused, blocked

<table>
<tr><th width="50%">MCP</th><th width="50%">CLI</th></tr>
<tr><td>

```text
release(claim_id="clm-…", actor="codex",
        session_id="codex:refunds")

park(claim_id="clm-…", actor="codex",
     session_id="codex:refunds",
     next_step="rerun the synthetic check",
     resume_when="the fixture is available")

block(claim_id="clm-…", actor="codex",
      session_id="codex:refunds",
      next_step="…", resume_when="…")

resume_parked(...) / recover_blocked(...)
classify_blocked(...) / correct_tier(...)
```

</td><td>

```bash
coord release CLAIM --status released

coord release CLAIM --status paused \
  --next-step "rerun the synthetic check" \
  --resume-when "the fixture is available" \
  --resume-manual

coord release CLAIM --status blocked \
  --reason "the upstream fixture is absent" \
  --next-step "rebuild it from the ingest run" \
  --resume-when "the fixture is on disk" \
  --resume-manual

# no CLI twin for the recovery verbs
```

</td></tr>
</table>

One CLI verb (`release --status`) covers what MCP splits across `release`, `park` and
`block`; `--status` accepts `blocked paused released unclaimed`
(`coord release --help`). `--resume-predicate` and `--resume-manual` are mutually
exclusive. Paused and blocked are not euphemisms for unfinished — they record why
execution stopped, and a parked or blocked row keeps its disposition even after the
claim releases, where a bare running claim is requeued.

`--status blocked` takes two things the released case does not. `--reason` names the
criterion that is not met and is required — it is the same text MCP's `block()` passes
as `step`, and it lands in the same `release_claim(reason=…)` parameter. And blocked,
like paused, needs an explicit resume trigger, so one of `--resume-manual` or
`--resume-predicate` has to accompany `--resume-when`. Both requirements live in the
storage layer, so an incomplete blocked release is refused rather than recorded as a
block nobody can act on.

### 5.3 Reading the board

<table>
<tr><th width="50%">MCP</th><th width="50%">CLI</th></tr>
<tr><td>

```text
board(limit=100)          # 100 rows inline max
next_work(actor="codex")
work_context(work_id="OPS-503")
event_context(...)
runs(...)
```

</td><td>

```bash
coord board --group-by module

python -m coordharness.coord.board_context digest
python -m coordharness.coord.board_context focus OPS-503
python -m coordharness.coord.board_context search "rate limit"
python -m coordharness.coord.board_context skeleton --status open
python -m coordharness.coord.board_context capsule

coord-jobs status
```

</td></tr>
</table>

This is the one place the asymmetry runs the other way. The lenses in
[`board_context.py`](../src/coordharness/coord/board_context.py) — `digest`, `focus`,
`search`, `history`, `skeleton`, `changes`, `curate`, `capsule`, `export`, `successor`
(the full subcommand list from its `--help`) — have **no MCP equivalent at all**. `board`,
`work_context` and `knowledge_search` overlap in purpose and are separately coded:
`work_context` returns one row plus parent plus recent events, not `focus`'s scored
sibling and related set. If you want those shapes from an agent session, shell out.

**On a seeded standalone board none of these lenses run at all** — every one of them goes
through `load_query_snapshot` and raises the exact-authority error from
[§5.0](#50-what-actually-works-on-a-standalone-board--read-this-first). `coord board` is
the working substitute there, and it is a plain row listing, not a lens.

### 5.4 Communication, review and closeout

<table>
<tr><th width="50%">MCP</th><th width="50%">CLI</th></tr>
<tr><td>

```text
inbox(actor="codex")
inbox_recent(...)
note(...) / decision(...)
audit(...) / request_audit(...)
verdict(...)
handoff_existing(...)   # profile-gated
session_closeout(...)
```

</td><td>

```bash
coord inbox --actor codex

coord sign-off WORK_ID \
  --reason "the reviewing lane is offline and
            I read the artifact myself" \
  --ref docs/reports/refunds.md \
  --operation-id signoff-2026-08-29-a

# no CLI twin for any of the rest
```

</td></tr>
</table>

#### Signing off a review gate

`coord sign-off` is the human override of the T0 review gate, and the only writer of
`operator_ok` anywhere in this product. Reach for it when a T0 row is finished, its
proof exists, and the opposite lane cannot supply the verdict — the case where
`coord done` refuses with *"T0 review has not passed and no valid operator-ok event is
bound"* and nothing an agent can do clears it.

It is deliberately not on the MCP surface, and `post_event` and `upsert_work` both
refuse the field, so no agent surface can write it. That is not by itself the guard —
an agent runs this CLI too. The guard is that the verb asks **the controlling
terminal** for confirmation and reads the answer from `/dev/tty`, never from stdin: it
prints the row's identity, tier, proof and acceptance digest, and signs only if you
type the work id back. A process with no controlling terminal is refused outright, and
a piped or scripted answer is not read at all. No environment variable and no flag
opens it; a guard the guarded party can satisfy by assigning a value is not a guard.

Three fields are required — `--reason` in your own words, at least one `--ref` to what
you actually read, and an `--operation-id` so that re-running the identical command
replays the existing receipt instead of signing twice.

One refusal is worth knowing in advance: a sign-off is refused on a row with an **open
review barrier** (an outstanding `audit_request`, or an acceptance repair). Sign-off
substitutes for a peer verdict only where review was never requested
([review tiers](review-tiers.md)), so recording one against a live request would be a
valid event that every reader then ignores. The verb says so and names the barrier
event rather than reporting a success that changes nothing.

`handoff_existing` is the sole member of `_SERVER_PROMOTION_CANDIDATES`
([`mcp_coord_server.py:64`](../src/coordharness/coord/mcp_coord_server.py)) and registers
only where the deployment's client profile is attested. A real `coord claim` response
reports it under the `deferred_tool_catalog` check, which is how you tell whether your
profile has it — on an unattested profile:

```json
{"name": "deferred_tool_catalog", "status": "ok",
 "detail": {"candidate_tools": ["handoff_existing"], "not_registered": ["handoff_existing"],
            "promoted": [], "env_flag": "COORD_DEFERRED_TOOL_CATALOG"}}
```

`runs` sits behind the same `tool_visible(...)` gate but is in the visible set by default,
so it registers where `handoff_existing` does not.

### 5.5 Context and memory

<table>
<tr><th width="50%">MCP</th><th width="50%">CLI / Python</th></tr>
<tr><td>

```text
knowledge_search(query="…", sources=[…])
read_note(pointer="memory://…#slug")
facts_lookup(query="…")        # fuzzy, ranked
facts_query(module=…, status=…)  # exact filter
knowledge_index_status()
memory_proposals_list(...) / memory_proposals_get(...)
get_decision_context(...)
```

</td><td>

```python
from coordharness.knowledge import kfts, facts
kfts.search("rate limit")
kfts.read_note("memory://docs/x.md#slug")
facts.current_value("…")
facts.already_decided("…")
```

</td></tr>
</table>

`knowledge_search` federates facts, the full-text index, artifact manifests, accepted
memory and memory proposals; pass `sources` to keep it off `coord.db`, since the default
provider list in
[`context_federator.py`](../src/coordharness/knowledge/context_federator.py) includes
board providers that read the coordination database. Named budget profiles —
`brief orient work edit-prep impact docs deep code forensic` — are enumerated by
`context_query_profiles()` in the same module; `forensic` is manual-only. The
normalisation and scoring underneath are
[`query_scoring.py`](../src/coordharness/knowledge/query_scoring.py) and
[`query_aliases.py`](../src/coordharness/knowledge/query_aliases.py), shared by both
`kfts.py` and `facts.py`.

Full treatment: [context architecture](context-architecture.md) and
[graph and context](graph-and-context.md).

### 5.6 Parity matrix

Where each capability actually exists. Blank means it does not.

| Capability | MCP | CLI | Python API | Web board |
|---|---|---|---|---|
| Claim / heartbeat / complete | `claim_work`, `heartbeat`, `complete` | `coord claim`, `coord heartbeat-claim`, `coord done` | yes | — |
| Release / park / block | `release`, `park`, `block` | `coord release --status …` | yes | — |
| Blocked and tier recovery | `classify_blocked`, `recover_blocked`, `resume_parked`, `correct_tier` | — | yes | — |
| Board listing | `board` (fails closed on a standalone board) | `coord board` — the one that works there | yes | `/` |
| Board lenses (digest/focus/search/skeleton/capsule/history/changes/curate/export/successor) | — | `board_context` (all fail closed on a standalone board) | yes | — |
| Orientation | `preflight` (fails closed: locked-path guard) | `board_context capsule` (fails closed: exact-authority guard) | yes | `/` |
| Structural navigation (`ContextV1`) | — | — | `build_context` | `/api/v1/context` |
| Typed relationship graph | — | — | yes | `/api/v1/graph` |
| Federated knowledge query | `knowledge_search` | — | `compile_context_pack` | — |
| Facts lookup | `facts_lookup` (ranked), `facts_query` (exact) | — | `facts.search_facts`, `current_value`, `query_facts` | — |
| Index freshness | `knowledge_index_status` | — | `kfts.index_stats` | — |
| Memory proposal queue | `memory_proposals_list`, `memory_proposals_get` | — | `memory_proposals.list_proposals` | — |
| Note read by pointer | `read_note` | — | `kfts.read_note` | — |
| Inbox | `inbox`, `inbox_recent` | `coord inbox` | yes | — |
| Review and closeout | `verdict`, `audit`, `request_audit`, `session_closeout` | — | yes | — |
| Operator sign-off (review-gate override) | — (deliberately absent) | `coord sign-off` — needs a controlling terminal | `record_operator_sign_off` | — |
| Typed handoff | `handoff_existing` (profile-gated) | — | yes | — |
| Job telemetry | `runs` (visibility-gated, visible by default) | `coord-jobs status`, `coord-jobs launch` | yes | `/` Jobs tab |
| Safety and integrity checks | — | `coord doctor` | yes | — |
| Serve the projection | — | `coord-board` | yes | — |

---

## 6. Environment

| Variable | Selects | Read by |
|---|---|---|
| `COORD_DB` | The database, absolutely | CLI, MCP, both native clients |
| `COORD_ACTOR` | The acting lane (`codex`, `claude`, `local`) — **overrides the ambient session sniff** | `resolve_identity` ([`ingest.py:116`](../src/coordharness/coord/ingest.py)) |
| `COORD_SESSION_ID` | The session identity, explicitly, for either lane | `resolve_identity` |
| `COORD_PROJECT_ROOT` | The project relative proof paths resolve against | CLI, MCP, `demo.py` |
| `COORD_HOME` | The state directory (`.coordharness/`) | [`config.py`](../src/coordharness/config.py), `set_mode.sh` |
| `COORD_BOARD_URL` | The board the native clients attach to | [`HarnessEndpoint.swift`](../apps/menubar/Sources/App/HarnessEndpoint.swift) — the single resolution point |
| `COORD_BOARD_PORT` | The demo server's port (default 7870) | `demo.sh`, `server.py` |
| `COORD_USAGE_DASHBOARD_URL` | Full loopback HTTP URL of the deployment-owned canonical usage document | `UsageDashboardProxy`, `UsageAccountActionForwarder` |
| `COORD_MENUBAR_CONFIG` | The panel's appearance JSON | [`Config.swift:69`](../apps/menubar/Sources/Data/Config.swift) |
| `COORD_KNOWLEDGE_DB` | `knowledge.db` (default: state dir) | `config.knowledge_db_path()` |
| `COORD_OUTPUT_BUDGET` | The inline-output check | `output_budget.py`, reported in every policy response |
| `SOURCE_DATE_EPOCH` | The demo's synthetic clock | `demo.py`, the capture tooling |

`COORD_BOARD_URL` is the one to check first when a client shows the wrong board.
Thirteen endpoints across ten files once pointed at a fixed loopback port belonging to
another system; launched on a machine running it, the client attached and rendered that
board. Everything resolves through `HarnessEndpoint` now
([`HarnessEndpoint.swift`](../apps/menubar/Sources/App/HarnessEndpoint.swift): one
default, one `COORD_BOARD_URL` override), and the CI check for literal `127.0.0.1:<port>`
outside it **is written and green** —
[`tests/test_no_hardcoded_endpoints.py`](../tests/test_no_hardcoded_endpoints.py), seven
tests, including `test_repository_has_no_hardcoded_endpoints` and a `test_the_real_scan_can_go_red`
ablation that proves the scanner can fail:

```text
$ python -m pytest -q tests/test_no_hardcoded_endpoints.py
7 passed in 0.09s
```

[next steps §4a](next-steps.md) records this guard as built and keeps endpoint
configuration centralized in `HarnessEndpoint`.

The usage dashboard is a separate deployment boundary. The service launcher must
inject `COORD_USAGE_DASHBOARD_URL` with the canonical local `/api/usage/v1` URL;
CORD has no source-code fallback to another product's port. The proxy accepts only
loopback HTTP URLs without credentials or fragments. If the variable is absent,
the dashboard and account-action surfaces stay available but return their bounded
`upstream_not_configured`/unavailable documents instead of attaching elsewhere.

---

## 7. Troubleshooting

These are the failures this repository actually had, in the order they are most likely
to catch you again. Each names the mechanism, because each has a symptom that looks like
something else.

### 7.1 A view renders as unstyled markup and nothing errors

**Symptom.** A panel, chart or swatch appears with no styling at all — plain text and
default borders — and it looks deliberate rather than broken. Console clean, network
clean, no logged error.

**Mechanism.** The board sends `style-src 'self'` with no `unsafe-inline`
([`board/security.py:8-12`](../src/coordharness/board/security.py)). A `<style>` element
built by script gets a **null `sheet`** and every rule in it is dropped; `style="…"`
attributes are dropped too. Nothing fails loudly, because dropping a policy-violating
style is the policy working. Two view modules shipped this way and the accent swatches
were blank squares the entire time before anyone noticed.

**Fix.** All CSS ships as a served file listed in `_STATIC_ALLOWLIST`
([`server.py`](../src/coordharness/board/server.py), `_STATIC_ALLOWLIST`). Anything computed at runtime
goes through the CSSOM — `el.style.width = "…"` — which the policy permits, and which is
exactly how the accent switch and the bar widths work
([`cockpit.js`](../src/coordharness/board/static/cockpit.js), the `data-w` loop in
`paint()`).

**Guard.** [`tests/test_board.py:628`](../tests/test_board.py),
`test_no_surface_relies_on_inline_style`, sweeps every served `.js` and `.html` for a
parsed `style=` attribute or a `createElement("style")`, and asserts first that the
policy it defends has not been weakened. Do not fix a styling problem by adding
`unsafe-inline`; the test will catch it, and it is the wrong repair.

### 7.2 `SQLITE_CANTOPEN` on a database that is plainly there

**Symptom.** A reader — a viewer, a sandboxed client, another process — cannot open a
`coord.db` that exists, is readable, and works fine from the writing process.

**Mechanism.** A database in WAL mode needs a `-shm` shared-memory file *even for a
read-only open*, and creating it requires write permission in the containing directory.
A reader that cannot write beside the database gets `SQLITE_CANTOPEN`, which reads as
"missing file" and is not.

**Fix, on the producing side.** Fold the journal in before handing the file over. The
seeder does exactly this as its last act
([`demo.py:406-407`](../src/coordharness/demo.py)):

```python
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
conn.execute("PRAGMA journal_mode=DELETE")
```

**Fix, on the reading side.** Read a private copy. `_materialized_connection` in
[`board/snapshot.py:51`](../src/coordharness/board/snapshot.py) copies the database
and its `-wal` into a temporary directory, stamps both before and after, retries up to
three times, and raises rather than serve a torn read. That is also why the board can
promise it never writes beside the source: two tests hold it,
`test_snapshot_get_is_strictly_side_effect_free` and
`test_coord_board_is_byte_for_byte_read_only`
([`tests/test_board.py:174`, `:226`](../tests/test_board.py)).

### 7.3 A client that ignores `COORD_DB`

**Symptom.** A native client launched with `COORD_DB` set shows a different board — an
empty one, or worse, a real one on a machine already running a coordination board.

**Mechanism.** `open(1)` does not pass the calling shell's environment through to the
launched application. `open -a "COORD.app"` with `COORD_DB` exported in front of it hands
the app nothing, so it falls back to its default location and attaches to whatever is
there.

**Fix.** Launch the executable inside the bundle, which inherits the environment
normally. This is why `demo.sh` spells it out rather than using `open`, with the reason
in a comment above the loop:

```bash
COORD_DB="$DEMO/.coordharness/coord.db" \
COORD_PROJECT_ROOT="$DEMO" \
COORD_BOARD_URL="http://127.0.0.1:$PORT" \
  "$REPO/var/build/Build/Products/Release/COORD.app/Contents/MacOS/COORD"
```

The same shape appears in the [README](../README.md) for a hand build. The related
hazard — a hardcoded loopback port rather than a missing variable — is [§6](#6-environment).

### 7.4 `coord doctor` reports BLOCKED on the demo board

Expected, and worth knowing so it does not send you hunting. Against a freshly seeded
demo estate:

```text
{"schema": "coordharness.doctor.v1", "status": "BLOCKED", "read_only": true,
 "findings": [{"id": "doctor.jobs_projection", "status": "BLOCKED",
   "summary": "job sidecar or projection integrity could not be proven",
   "details": {"sidecar_count": 10, "live_run_count": 0,
               "problem_codes": ["sidecar_work_binding_missing"]}},
  {"id": "doctor.leases_reviews", "status": "PASS", …}, …]}
```

The demo writes ten job sidecars, and not all of them bind to a work row. Lease and
review coherence passes, as do the further findings elided above
(`doctor.lifecycle_writers` and the rest). `doctor` is read-only throughout — `coord --help`
describes it as "run read-only safety and integrity checks", and the response carries
`"read_only": true` ([safety doctor](safety-doctor.md)).

### 7.5 Quick discriminators

| Symptom | Most likely |
|---|---|
| Board loads, map is blank | Server up, but `/api/v1/graph` or `/api/v1/context` failed — the map fetches all three and paints once |
| Cockpit table populated, Map tab blank | The board server is down; the table reads `coord.db` directly |
| Native client shows rows that predate your last `coord done` | The `native_cockpit` projection is only refreshed by the seeder ([§2.4](#24-what-the-native-clients-read-and-its-one-stale-edge)) |
| Mode slider snaps back after ~20 seconds | Working as built — the board returns `405` ([§2.2](#22-the-mode-slider-and-pause--read-this-before-you-use-them)) |
| `404` every few seconds in the client log | `/api/state/compact` and `/api/capability_inventory`, deliberately unserved — `/api/menubar` returns 200 ([§2.4](#24-what-the-native-clients-read-and-its-one-stale-edge)) |
| MCP `preflight` raises `locked data-local …` | A strict deployment profile is active without its locked layout; use the generic profile only for genuinely generic local state ([§5.0](#50-what-actually-works-on-a-standalone-board--read-this-first)) |
| `ExactQueryCoreError: exact-authority policy is not enforced` | A strict-profile read reached state without active exact authority. Fresh generic `board` and context lenses use the explicitly labelled `generic_coord_db` path; do not downgrade deployment-owned state to bypass this error. |
| `cannot claim work assigned to 'codex' from 'claude'` | `CLAUDE_CODE_SESSION_ID` in the environment won the actor sniff. Set `COORD_ACTOR=codex` (plus `COORD_SESSION_ID`) — `CODEX_SESSION_ID` alone will not do it ([§5.1](#51-a-session-end-to-end)) |
| `coord: could not prepare the database` | The loader fails closed rather than create coordination tables in an unrelated file ([getting started](getting-started.md)) |

---

## See also

[Getting started](getting-started.md) ·
[MCP integration](mcp-integration.md) ·
[MCP server reference](mcp-server.md) ·
[Agent protocol](agent-protocol.md) ·
[Web board](web-board.md) ·
[Native clients](native-clients.md) ·
[Jobs and runs](jobs-and-runs.md) ·
[Safety doctor](safety-doctor.md) ·
[Security and privacy](security-and-privacy.md) ·
[Context architecture](context-architecture.md) ·
[Next steps](next-steps.md)
