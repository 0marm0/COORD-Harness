# Agent onboarding

This is the clean-room path for a new clone. It uses only repository-relative runtime
paths, creates a private generic `coord.db`, and verifies both the CLI and the actual
stdio MCP server. It does not require provider credentials.

On macOS, the complete after-clone path requires Python 3.11 or newer, Git,
Xcode with its command-line tools selected, and XcodeGen (`brew install xcodegen`),
then runs one command:

```bash
./scripts/setup-macos.sh
```

It coordinates this clone, owns `.coordharness/coord.db`, registers installed MCP
clients, invokes `apps/install.sh`, and starts the single service on
`http://127.0.0.1:7870`. MCP clients use the clone's `.venv`; the service keeps its
separate installed runtime but reads the same database. Continue below only for the
CLI-only/manual equivalent or for verification after the wrapper.

## 1. Install into the project virtual environment

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[mcp,dev]'
export COORD_PROJECT_ROOT="$PWD"
export COORD_DB="$PWD/.coordharness/coord.db"
export COORD_DEPLOYMENT_PROFILE=generic
.venv/bin/coord board
```

`coord board` is the intentional bootstrap write. It creates and migrates
`.coordharness/coord.db`; keep that directory out of Git. The `strict` deployment
profile adds custody gates for a specific installation and is not a clean-room shortcut.

## 2. Configure Codex and Claude without private paths

The checked-in `.codex/config.toml` and `.mcp.json` invoke
`./.venv/bin/python -m coordharness.coord.mcp_coord_server`. They do not depend on an
activated shell, a GUI application's PATH, or an original developer's absolute path.
The byte-identical copyable templates live in `.codex/templates/`.

For another fresh project that already contains the root instructions and mirrored
skills, run:

```bash
.venv/bin/coord onboard --write-configs --register-clients
```

Both mutations are opt-in. The config writer creates missing files only and reports a
conflict instead of replacing an existing table. Client registration first runs
`codex mcp get coordharness` or `claude mcp get coordharness`; it leaves an existing
entry unchanged and invokes `mcp add` only when the entry is missing. The generated
registration commands use the clone's absolute `.venv/bin/python`, project root,
`coord.db`, and `knowledge.db`, so a GUI-launched client does not depend on shell
activation or `PATH`. The JSON receipt includes the exact argv under
`registration_command`.

Claude project servers require a one-time trust choice. If the receipt says
`approval_pending=true`, run `claude` from the clone, approve `coordharness`, exit, and
rerun the doctor. This is an interactive client trust gate, not a database or MCP
transport failure.

## 3. Verify discovery and transport

```bash
.venv/bin/coord onboard
codex mcp get coordharness
claude mcp get coordharness
```

`coord onboard` is machine-readable and distinguishes four layers: checked-in portable
repo config, installed-client registration, Claude approval state, and a live stdio MCP
handshake. It also checks root instructions, byte-identical skill mirrors, current
`coord.db`, core tool discovery, and a real MCP `preflight` call. An absent client is
`SKIPPED`; a missing installed-client registration is `BLOCKED` with the exact
idempotent remediation command. `--register-clients` is the only mode that mutates
client configuration.

Codex reads project instructions once at session start. An exact optional model-backed
check is:

```bash
codex exec -C "$PWD" --sandbox read-only --ask-for-approval never   'Do not call tools. Return only the value of COORDHARNESS_AGENT_INSTRUCTION_SENTINEL from the loaded project instructions.'
```

Expected output: `v1`. Start a new session after changing `AGENTS.md`; a running session
does not rediscover it.

## 4. Create, inspect, claim, and prove the first item

```bash
export COORD_ACTOR=codex
export COORD_SESSION_ID=codex:first-session
.venv/bin/coord create DEMO-CDX-FIRST-WORK \
  --title 'Verify the local harness' \
  --module harness \
  --done-signal reports/first-work.md \
  --acceptance 'the clean-room lifecycle completes' \
  --note 'first generic work item'
.venv/bin/coord work-context DEMO-CDX-FIRST-WORK
.venv/bin/coord claim DEMO-CDX-FIRST-WORK --step 'writing declared proof'
mkdir -p reports
printf '%s\n' '# First work' '' 'Clean-room proof.' > reports/first-work.md
git add reports/first-work.md
.venv/bin/coord done DEMO-CDX-FIRST-WORK --artifact reports/first-work.md
```

The completion command is proof-gated. A different file, an unstaged Markdown proof,
or a missing claim fails closed.

## 5. Use the bounded MCP reads

In Codex or Claude, call these in order:

1. `preflight(actor="codex", session_id="codex:first-session")`
2. `board(limit=20)` and `next_work(actor="codex")`
3. `work_context(work_id="DEMO-CDX-FIRST-WORK", actor="codex")`
4. follow only the returned pointers needed for the task.

An empty board returns empty lists with `query_core.mode="generic_coord_db"`. An unknown
work ID returns `ok=false` and `error.code="work_not_found"`; it is not fabricated and
it is not a strict-profile authority error.

For CLI traversal over the same `coord.db`:

```bash
.venv/bin/python -m coordharness.coord.board_context --json capsule
.venv/bin/python -m coordharness.coord.board_context --json digest --actor codex
.venv/bin/python -m coordharness.coord.board_context --json focus DEMO-CDX-FIRST-WORK
.venv/bin/python -m coordharness.coord.board_context --json search 'clean room'
```

## 6. Transfer responsibility with exact fences

Read `coord work-context WORK_ID`, then copy its exact version, assignee, and all active
head event IDs into one typed transfer. For an empty head list, omit
`--expected-head-event-id`:

```bash
.venv/bin/coord handoff DEMO-CDX-FIRST-WORK \
  --owner-lane claude \
  --task 'review and finish the declared proof' \
  --why 'Claude owns the receiving slice' \
  --acceptance 'reports/first-work.md satisfies the row acceptance' \
  --operation-id handoff-first-work-0001 \
  --expected-version VERSION_FROM_WORK_CONTEXT \
  --expected-assignee codex \
  --ref docs/agent-onboarding.md \
  --constraint 'preserve coord.db as lifecycle authority'
```

If the row changed, reread it and decide again. Do not reuse stale fences or reassign by
editing the database. The MCP `handoff_existing` tool remains deferred unless an
attested deployment promotes it; the generic CLI calls the same canonical fenced DAO.

## 7. Track durable jobs; query knowledge separately

After claiming a row, launch long work with its exact claim ID and fence:

```bash
.venv/bin/coord-jobs launch \
  --job-id FIRST-JOB \
  --roadmap-id DEMO-CDX-FIRST-WORK \
  --session-id codex:first-session \
  --claim-id CLAIM_ID \
  --claim-fence CLAIM_FENCE \
  --cap-gb 1 -- .venv/bin/python -c 'print("bounded job")'
.venv/bin/coord-jobs status
```

For recall, use MCP `facts_query`, `facts_lookup`, `knowledge_index_status`, and bounded
`knowledge_search`. A fresh missing `knowledge.db` honestly returns zero facts and a
missing/stale index. `knowledge.db`, KFTS hits, accepted memory, and proposals never
claim, hand off, or complete work; only `coord.db` does.
