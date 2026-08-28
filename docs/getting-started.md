# Getting started

This guide creates a private board, adds and claims one proof-gated work item, and
shows the result through the CLI. A separate synthetic walkthrough is included for
exploration. Neither path requires provider credentials or a hosted COORD service.

## Prerequisites

- Python 3.11 or newer
- Git
- A POSIX shell for the copy-paste examples

The complete macOS path also requires Xcode with its command-line tools selected
and XcodeGen (`brew install xcodegen`). CLI-only setup does not require either native tool.

Windows process-liveness support is not currently claimed. See [compatibility](compatibility.md).

## Install from this repository

On macOS, the complete stranger path is one command after cloning. It owns the clone's
`.coordharness/coord.db`, installs the native apps, and starts the sole local board
service on port 7870:

```bash
git clone https://github.com/0marm0/COORD-Harness.git
cd COORD-Harness
./scripts/setup-macos.sh
```

For CLI-only development on any supported platform:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[mcp,dev]'
```

If you only need the core Python API and `coord` CLI, install `-e .`. The MCP server needs `[mcp]`; development checks need `[dev]`.

Verify entry points:

```bash
.venv/bin/coord --help
.venv/bin/coord-board --help
.venv/bin/coord-jobs --help
```

`coord-mcp` is a stdio server, so do not run it as a help probe: it waits for an MCP client. Client configuration is its smoke test.

## Where state lives

By default:

```text
COORD-Harness/
└── .coordharness/
    ├── coord.db
    └── job_progress/
```

`COORD_PROJECT_ROOT` selects the project against which relative proof paths resolve. `COORD_HOME` moves the state directory. `COORD_DB` overrides the database path directly.

Keep `.coordharness/` out of Git. It can contain work titles, events, actor labels, and local process metadata.

For a complete machine-local layout, client wiring, uninstall instructions, and
verification matrix, continue with [standalone setup](standalone-setup.md).

## Create the first real work item

An empty project does not require the fictional demo or manual SQL. From the clone root:

```bash
export COORD_PROJECT_ROOT="$PWD"
export COORD_ACTOR="codex"
export COORD_SESSION_ID="codex:my-project"
.venv/bin/coord create DEMO-CDX-FIRST-WORK \
  --title "Verify the local harness" \
  --module harness \
  --done-signal reports/first-work.md \
  --acceptance "the local lifecycle completes from a fresh database" \
  --note "first standalone COORD work item"
.venv/bin/coord claim DEMO-CDX-FIRST-WORK --step "running the local verification"
```

The first command bootstraps `.coordharness/coord.db` when necessary and writes one queued work contract. `--done-signal` is a repository-relative proof path; COORD never invents a completion signal after work starts. Use `--assignee claude` for a Claude-owned row or omit it to use the active actor.

Three things a first run trips over:

- `--note` is **required** by `coord create`. Without it argparse exits 2 with `coord create: error: the following arguments are required: --note`.
- `COORD_ACTOR` and `COORD_SESSION_ID` are the identity that outranks every ambient variable. Set them explicitly. If a Codex session variable and `CLAUDE_CODE_SESSION_ID` are both present — which is exactly what happens when you paste these commands into a Claude Code shell — the CLI refuses with `ambiguous agent identity` rather than guessing who to attribute the work to.
- The `--done-signal` directory does not have to exist yet. A declared proof that has not been produced is a *pending* pointer, not an error; `coord doctor` counts it and does not block on it.

Check the result at any point:

```bash
.venv/bin/coord doctor
```

`coord doctor` is read-only: it creates no state directory, bootstraps no database, and writes nothing. It prints one JSON document and exits 0 only when every finding passes. On the board created above it passes.

The synthetic walkthrough below remains useful for exploring a populated board, but it is not a prerequisite for real work.

## Seed the synthetic board

From the repository root:

```bash
.venv/bin/python -m coordharness.demo
.venv/bin/coord board --group-by module
```

The seed is synthetic and fictional. It uses the current clock so live demo leases remain active; set `SOURCE_DATE_EPOCH` when you need a byte-reproducible capture. It writes 37 work rows under five initiatives — UI overhaul, Model development, Platform migration, Search relevance, and Operational hardening — with Claude, Codex, and service actors and rows in several lifecycle states. It does not copy or paraphrase a private board.

It also writes ten job sidecars into `.coordharness/job_progress/`, each bound to one of those work rows. Seed with the default paths: if you send the database somewhere else with `--db`, the sidecars stay behind in `.coordharness/` and `coord doctor` will report the split.

Confirm the seeded board is healthy before going further:

```bash
.venv/bin/coord doctor
```

It prints `"status": "PASS"` and exits 0.

## Claim one Codex-owned row

`OPS-503` is queued for the Codex lane and declares `docs/reports/ops-503.md` as its completion proof.

```bash
export COORD_ACTOR="codex"
export COORD_SESSION_ID="codex:demo"
.venv/bin/coord claim OPS-503 --step "documenting the public API rate limits"
```

Set `COORD_ACTOR` even when a Codex session variable is already in the environment. It is what makes the identity unambiguous, and without it the claim fails outright inside a Claude Code shell, where `CLAUDE_CODE_SESSION_ID` is also set.

The JSON response contains the concrete `claim_id` and its exact
`claim_fence`. Keep the ID for heartbeats and terminal actions. Keep the
fence private and pass it only when a tracked local job needs to prove that the
same live claim still authorizes launch:

```bash
.venv/bin/coord heartbeat-claim CLAIM_ID_FROM_THE_RESPONSE \
  --step "proof drafted; checking the result"
```

The heartbeat renews the claim lease. Progress text is contextual metadata, not a second lifecycle status.

## Satisfy proof and complete

The quick demo intentionally requires `mkdir -p` so it also works in a clone without `docs/reports/`.

```bash
mkdir -p docs/reports
printf '%s\n' '# Public API rate limits' '' 'Synthetic demo proof.' \
  > docs/reports/ops-503.md

git add docs/reports/ops-503.md
.venv/bin/coord done OPS-503 --artifact docs/reports/ops-503.md
.venv/bin/coord board --group-by module
.venv/bin/coord doctor
```

Completion checks that this session holds the live claim, that the explicit path matches the controller-declared `done_signal`, that the proof is non-empty and allowed by custody rules, and that terminal/review guards permit completion. Markdown proof must be tracked by Git's current index; staging is sufficient, and no commit is required. The transition and receipt are written transactionally.

The `git add` is not decoration. Without it the Markdown proof is untracked and `done` refuses; in a directory that is not a Git repository at all it refuses for the same reason.

`coord done` records the proof as the repository-relative path the row declared, not as an absolute host path, so the final `coord doctor` reports the same `PASS` it did before the completion.

## Pause, block, or release instead

Use the `claim_id` returned by `coord claim`:

```bash
.venv/bin/coord release CLAIM_ID --status paused \
  --next-step "rerun the synthetic check" \
  --resume-when "the fixture is available" \
  --resume-manual
```

For a real blocker, use `--status blocked` with a durable next step and resume trigger. Use the default `released` status when you are simply giving up ownership. Paused and blocked are not euphemisms for unfinished work; they preserve why execution stopped.

## Connect Claude Code and Codex

The checked `.codex/config.toml` and `.mcp.json` both select this clone's
`.venv/bin/python` and `.coordharness/coord.db`. Register only missing installed-client
entries and run the live handshake:

```bash
.venv/bin/coord onboard --register-clients
```

If Claude reports `approval_pending=true`, run `claude` in this clone and approve the
`coordharness` project server. Then verify all layers again:

```bash
.venv/bin/coord onboard
codex mcp get coordharness
claude mcp get coordharness
```

Claude Code and Codex authenticate themselves to their providers. COORD does not
capture those credentials or sign you in. Continue with
[agent onboarding](agent-onboarding.md) for the instruction sentinel, first MCP calls,
typed handoff, tracked jobs, and knowledge boundaries.

## Open the preview web board

The macOS wrapper already starts the local read-only service; do not start a second
one. For CLI-only setup:

```bash
.venv/bin/coord-board --db .coordharness/coord.db --host 127.0.0.1 --port 7870
```

Open `http://127.0.0.1:7870/`. The versioned snapshot is `http://127.0.0.1:7870/api/v1/snapshot`; health is `http://127.0.0.1:7870/healthz`. Do not bind it beyond loopback.

The macOS lane is service-primary on `http://127.0.0.1:7870`, the same loopback
endpoint shown above. A transitional direct-SQLite cockpit path exists only when an
operator explicitly configures the exact database path; it is not a fallback authority.

## What setup does not automate

- COORD does not install, authenticate, or manage Claude Code or Codex accounts.
- With no `COORD_USAGE_DASHBOARD_URL`, usage reads only the current user local
  Claude and Codex state. Local CLI history is partial, Claude quota stays unavailable,
  and Codex quota is canonical only when official `codex app-server` returns current
  windows. Local reads share a thread-safe 30-second snapshot. Cold concurrent reads
  coalesce; bounded waiters receive an honest warming response, and expired snapshots
  are served as stale while one background refresh runs. An explicit URL selects the
  validated loopback upstream instead.
- COORD does not implement a local provider login flow. Login-open actions return HTTP
  501; authenticate with the official Claude Code or Codex client.
- CPU, GPU, RAM, disk-capacity, and disk-I/O tiles use COORD local collector by
  default. The board samples on demand and caches briefly; no daemon is installed. Each
  unsupported or failed metric remains unavailable instead of becoming zero. An explicit
  `COORD_SYSTEM_TELEMETRY_URL` selects a credential-free loopback upstream instead.
- `coord route` is advisory over a usage ledger you pass with `--usage-db`; it does
  not launch or redirect an agent.
- Context and memory modules are bounded, derived recall surfaces. This release has
  no one-command knowledge-index or accepted-memory bootstrap, and neither store is
  lifecycle authority.
- Typed handoff requires exact row fences. Generic projects can use `coord work-context`
  followed by `coord handoff`; the MCP writer remains deferred to promoted profiles.
- `coord-jobs launch` supervises one declared command. COORD does not ship an
  autonomous retry loop, scheduler, or always-on multi-agent supervisor.

## Reset a disposable demo

Stop any process using the demo board, then remove only the exact `.coordharness/` directory inside the disposable clone. Do not use an unresolved environment variable, home directory, or broad recursive target.

Re-running `python -m coordharness.demo` against an existing board is idempotent for the synthetic work rows but does not promise to erase lifecycle history. A fresh disposable clone is the cleanest reset.

## Troubleshooting

### `coord: could not prepare the database`

Check that the parent directory is writable and the path is not a zero-byte or foreign SQLite file. The loader fails closed instead of creating coordination tables inside an unrelated database.

### `unknown work_id 'OPS-503'`

Run `python -m coordharness.demo` against the same `COORD_DB` used by `coord`.

### `ValueError: ambiguous agent identity`

Both `CLAUDE_CODE_SESSION_ID` and a Codex session variable are set, so the CLI cannot tell which lane to attribute the work to and refuses rather than guessing. Set `COORD_ACTOR=claude` or `COORD_ACTOR=codex` (with a matching `COORD_SESSION_ID`) and re-run. This is the normal case when you run the Codex examples inside a Claude Code shell.

### `coord create: error: the following arguments are required: --note`

`coord create` requires `--note`. It is the row's origin statement, not an optional comment.

### `claim` says the row belongs to another actor

Use the correct actor/session identity or a typed handoff. Do not overwrite the assignee or reuse another process's session ID.

### `done` says proof is missing or incomplete

Confirm that the artifact path exactly matches the row's declared `done_signal`, resolves beneath `COORD_PROJECT_ROOT`, exists, is non-empty, and is not a telemetry/control marker rejected by custody rules.

A Markdown proof must additionally be tracked by Git's current index. `git add` the file — staging is enough, no commit is needed. The same refusal appears when the project is not a Git repository, because nothing can be tracked there; initialise one, or declare a non-Markdown proof path.

### `coord doctor` reports `database_outside_state_root`

The database you selected exists, but it is not inside the state root doctor was given. This is the layout produced by, for example, seeding with `--db var/demo/coord.db` while the state directory stays at `.coordharness/`. Either pass a matching `--state-root`, or keep the database at the default `.coordharness/coord.db`. Doctor does not open a database it cannot place inside the trusted state root, and it reports the condition by name rather than claiming the file is absent.

### `coord doctor` lists my work under `pending_pointer_fields`

That is not an error. A row that declares a proof it has not produced yet is pending, and pending pointers do not block. A pointer only becomes `invalid` when it escapes the project root, uses traversal or an unknown URI scheme, or when a **terminal** row's proof is missing.

### The board viewer cannot open the database

Create the database first with any `coord` command or the demo seeder. Confirm the viewer and CLI point to the same absolute `COORD_DB`.
