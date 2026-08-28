# Standalone setup

This is the clean-machine path for using COORD with your own project and your own
Claude Code or Codex sessions. COORD is a local coordination authority; it is not
a hosted account service, an agent runtime, or a replacement for either provider
client.

## Install

The full macOS install requires macOS, Python 3.11 or newer, Git, Xcode with its
command-line tools selected, and XcodeGen (`brew install xcodegen`). The supported
one-command clone setup composes Python/MCP onboarding with the native app and service installer:

```bash
git clone https://github.com/0marm0/COORD-Harness.git
cd COORD-Harness
./scripts/setup-macos.sh
```

The wrapper selects the clone as the coordinated project and
`$PWD/.coordharness/coord.db` as its one lifecycle authority. It creates the repository
`.venv`, installs the MCP extra, bootstraps that database, idempotently registers
installed clients, and then calls `apps/install.sh`. The installer runs one loopback
board service at `http://127.0.0.1:7870` and installs the native apps against the same
exact database. Its service runtime lives under
`~/Library/Application Support/COORD/venv`; agent MCP clients deliberately use the
clone's `.venv/bin/python`. The checked configs and onboarding doctor verify the agent
runtime and clone-owned database. Claude may still require its one-time interactive
project approval, which the wrapper reports exactly.

CLI-only setup requires only Python 3.11 or newer, Git, and a POSIX shell:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[mcp]'
export COORD_PROJECT_ROOT="$PWD"
export COORD_HOME="$PWD/.coordharness"
export COORD_DB="$COORD_HOME/coord.db"
export COORD_KNOWLEDGE_DB="$COORD_HOME/knowledge.db"
export COORD_DEPLOYMENT_PROFILE=generic
.venv/bin/coord board
.venv/bin/coord onboard --write-configs --register-clients
```

Do not depend on an activated shell or GUI `PATH`.

## One project and one authority

The supported clone setup coordinates this clone. The wrapper sets these values; use
them explicitly for CLI-only setup:

```bash
export COORD_PROJECT_ROOT="$PWD"
export COORD_HOME="$PWD/.coordharness"
export COORD_DB="$COORD_HOME/coord.db"
export COORD_KNOWLEDGE_DB="$COORD_HOME/knowledge.db"
export COORD_DEPLOYMENT_PROFILE="generic"
```

Add `.coordharness/` to that project's `.gitignore`. The directory is local state:

```text
.coordharness/
├── coord.db             lifecycle authority and event history
├── knowledge.db         optional facts, search index, and memory proposals
├── job_progress/        bounded local-job sidecars
└── resource_modes.json  optional local resource policy
```

`COORD_DB` is the only lifecycle authority. Never copy another user's database
into a new installation, edit it manually, or point two unrelated projects at it.

## Create the first work contract

Use explicit identity variables so ambient provider-client variables cannot select
the wrong actor:

```bash
export COORD_ACTOR="codex"
export COORD_SESSION_ID="codex:my-project"
.venv/bin/coord create DEMO-CDX-FIRST-WORK \
  --title "Verify the local harness" \
  --module harness \
  --done-signal reports/first-work.md \
  --acceptance "a fresh local database completes one proof-gated row" \
  --note "first standalone work contract"
.venv/bin/coord claim DEMO-CDX-FIRST-WORK --step "checking the standalone lifecycle"
.venv/bin/coord board --group-by module
```

For a Claude-owned row, use `COORD_ACTOR=claude`, a stable process-unique
`COORD_SESSION_ID` beginning with `claude:`, and `--assignee claude`. Do not reuse
one session ID across concurrent processes. Completion requires the exact declared
artifact; Markdown proof must be non-empty and tracked by the current Git index.

## Connect Claude Code and Codex

Claude Code and Codex must already be installed and authenticated through their own
supported login flows. COORD neither reads nor stores provider credentials.

For a clone-in-place setup, `.codex/config.toml` and `.mcp.json` launch
`./.venv/bin/python` with relative project/state paths. Run
`.venv/bin/coord onboard --register-clients` to create only missing
installed-client entries with absolute runtime/state paths, then verify the instruction hierarchy, skill mirrors,
repo configs, registration state, Claude approval state, and live stdio `preflight`.
Copyable templates and the conflict-safe generator are described in
[agent onboarding](agent-onboarding.md); broader client examples remain in
[MCP integration](mcp-integration.md).

After approving Claude when prompted, verify `codex mcp get coordharness` and
`claude mcp get coordharness`. Each agent call uses a distinct actor and
process-unique session ID. A preflight error is a setup failure; do not fall back to direct SQL or a second
state file.

## Context and memory

The board already provides durable bounded context: work contracts, dependencies,
recent events, decisions, notes, and proof references. MCP `preflight`,
`work_context`, and `event_context` query that authority.

The separate knowledge store supports facts, full-text retrieval, memory proposals,
and immutable accepted-memory generations. These are derived recall layers, not
lifecycle state. In this preview:

- there is no supported one-command fresh-project index bootstrap;
- accepted memory requires explicit review and publication through Python APIs;
- nothing automatically imports a chat transcript or provider history;
- a missing or empty knowledge store must not prevent core lifecycle use.

Use the board first. Add knowledge and memory only after reviewing
[Context and memory](context-and-memory.md).

## Handoffs, jobs, routing, and loops

Typed handoff is an ownership transaction. Read `.venv/bin/coord work-context WORK_ID`, then
pass its exact version, assignee, and complete event-head set to `coord handoff`.
MCP `handoff_existing` remains available only under a promoted client profile. Both
surfaces call the canonical fenced DAO; a note alone never changes ownership.

`.venv/bin/coord-jobs launch` supervises one local command under a live claim. It needs the
exact `claim_id` and `claim_fence` returned by claim. It is not a general scheduler,
daemon, retry engine, or autonomous agent loop.

`.venv/bin/coord route --usage-db ...` is read-only advice. It compares declared budgets with
measured ledger rows and reports incomplete coverage. It does not choose a model,
move a claim, launch a client, or guarantee provider availability.

## Usage and system telemetry

The web and native previews include usage and machine-stat projections, but a fresh
install must treat both as optional:

- with no `COORD_USAGE_DASHBOARD_URL`, COORD reads current-user local state beneath
  `~/.claude` and `~/.codex` plus official CLI/app-server read interfaces;
- local CLI history is partial and noncanonical; Claude current quota is unavailable,
  while Codex quota is canonical only when official app-server returns current windows;
- COORD does not scrape credentials or browser sessions, and local login-open actions
  are unsupported with HTTP 501; authenticate in the official provider clients;
- one thread-safe local snapshot is shared for 30 seconds; cold concurrent reads
  coalesce behind one refresh, bounded waiters receive `warming`, and expired
  last-good data is served as `stale` while one background refresh thread runs;
- an explicit dashboard URL selects the validated fixed-loopback upstream;
- the board uses its built-in local collector by default for CPU, GPU, RAM, disk
  capacity, and disk I/O; it samples on demand, caches briefly, and installs no daemon;
- unsupported probes remain unavailable, and disk I/O rates need two samples;
- `COORD_SYSTEM_TELEMETRY_URL` optionally selects a credential-free loopback upstream;
- estimates and incomplete history must stay labelled as such.

Provider profile metadata defaults to `~/.coord/provider-profiles.json` and routing
policy to `~/.coord/provider-routing.json`; overrides are
`COORD_PROVIDER_PROFILES_PATH` and `COORD_PROVIDER_ROUTING_PATH`. These atomic mode-0600
files contain metadata and policy, not provider secrets. Routing stays advisory, never
automatically executes, and requires current quota before a provider is eligible.

Unavailable data is safer than convincing but incorrect data. See
[Security and privacy](security-and-privacy.md).

## Web and native views

The macOS wrapper installs and starts the sole board service on port 7870; do not start
a second process. For CLI-only setup, start that service explicitly:

```bash
.venv/bin/coord-board --db "$COORD_DB" --host 127.0.0.1 --port 7870
```

Verify `http://127.0.0.1:7870/healthz` and
`http://127.0.0.1:7870/api/v1/snapshot`. Do not expose the server to a LAN,
reverse proxy, or the internet; it has no authentication boundary.

The macOS lane is service-primary on the same loopback endpoint,
`http://127.0.0.1:7870`. Start and verify that service before opening the native
cockpit. A transitional direct-SQLite cockpit path is allowed only when an operator
explicitly configures the exact database path; it is not endpoint discovery, a
fallback authority, or a second lifecycle writer. See
[the native app README](../apps/README.md) for the current service lifecycle.

## Clean-install verification

Run this checklist before trusting the installation:

```bash
.venv/bin/coord --help
.venv/bin/coord doctor --project-root "$COORD_PROJECT_ROOT" --state-root "$COORD_HOME"
.venv/bin/coord onboard
.venv/bin/coord board --group-by module
.venv/bin/coord-jobs status
curl --fail http://127.0.0.1:7870/healthz
curl --fail http://127.0.0.1:7870/api/v1/snapshot >/dev/null
```

Then verify both MCP clients independently:

1. The tool catalog loads from the absolute harness virtual-environment path.
2. Generic `preflight` succeeds against this project's database.
3. Claude and Codex report distinct actor/session identities.
4. A second actor cannot claim an already held row.
5. Completion fails before the declared proof exists and succeeds only after it
   satisfies custody rules.
6. No provider quota or telemetry field is presented as real when its source is
   unavailable.

## Backup and uninstall

Stop MCP clients, `coord-board`, and tracked jobs before copying or removing state.
For a backup, copy the exact `.coordharness/` directory while no writer is active;
keep `coord.db`, `coord.db-wal`, and `coord.db-shm` together if they exist.

Uninstall the clone runtime with:

```bash
.venv/bin/python -m pip uninstall coordharness
```

Then remove the virtual environment or harness checkout if desired. Native apps are
separate; follow [their uninstall notes](../apps/README.md). Preserve
`.coordharness/` to keep history, or move that exact directory to Trash after
confirming it belongs to the intended project. Never use a broad recursive cleanup
command or an unresolved environment variable.
