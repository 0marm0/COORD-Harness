# Talking to the board over MCP

`coordharness` exposes the same board the `coord` CLI drives through a Model Context Protocol
(MCP) server, so an agent — Claude Code, Codex, or anything else with an MCP client — can claim
work, check heartbeats, leave notes, and query the board's state as tool calls instead of shell
commands. This document covers the transport, how to register the server, its full tool
surface, and the mechanisms that keep a large tool surface cheap to load and its responses
bounded in size.

## What MCP is, briefly

MCP is a protocol for exposing a set of callable functions ("tools") to an LLM client over a
defined transport. A client — usually the harness running an agent's session — starts the
server as a subprocess, exchanges an initialize handshake, then lists the server's tools and
calls them the same way the agent calls any other tool: by name, with typed arguments, getting
back structured JSON. The server holds no opinion about which model is driving it.

## Transport: stdio

`coordharness`'s server uses stdio transport exclusively — no HTTP, no SSE. The client launches
it as a child process and speaks newline-delimited JSON over its stdin/stdout; there is no
listening port. It's the simplest transport MCP defines, and the right choice for something
launched once per agent session and living only as long as that session does.

The server is built with `coordharness.coord.mcp_coord_server.build_server(...)`, which returns
a `FastMCP` instance from the `mcp` Python SDK, and run with `python -m
coordharness.coord.mcp_coord_server`. Register it by pointing a client at that module and
setting `COORD_DB` to the SQLite database file it should read and write:

```json
{
  "mcpServers": {
    "coord": {
      "command": "/path/to/COORD-Harness/.venv/bin/python",
      "args": ["-m", "coordharness.coord.mcp_coord_server"],
      "env": {
        "COORD_DB": "/path/to/your/project/.coordharness/coord.db"
      }
    }
  }
}
```

`COORD_DB` is read by `coordharness.config`. When unset, the database defaults to
`<project>/.coordharness/coord.db`, where the project is `COORD_PROJECT_ROOT` or the MCP
subprocess working directory. Set both values explicitly in a client configuration.

The `coord-mcp` entry point and `build_server(...)` call the shared idempotent bootstrap, which
creates the base schema and applies every pending numbered migration before serving tools. The
same bootstrap is available as an explicit administration command:

```bash
python -m coordharness.coord.create_schema --db /path/to/your/project/.coordharness/coord.db
```

Running bootstrap again is safe and preserves existing data. Database creation alone does not
satisfy the repository-coupled exact-authority and locked-layout checks — but the two profiles
differ in what that costs. A fresh **generic** `coord.db` needs nothing else: `board` and
`preflight` answer over stdio as soon as bootstrap has run (verified directly with a raw MCP
stdio call against a just-created database — `isError` false on both). **Strict** profile is
the one that still fails closed on a fresh database: it additionally enforces the locked
data-local alias, its physical target, and the Git index/sparse-checkout guards
(`locked_paths.py`), and a database with none of that set up gets a stdio error naming exactly
which check failed. The server catalog is testable in both profiles, but only generic is
accepted end-to-end without further repository setup.

## The tool surface

Every tool the server registers is a thin `@mcp.tool()` wrapper around a plain Python function
(`_tool_claim_work`, `_tool_board`, and so on) that does the actual work against the database.
The split exists so the logic can be exercised directly in tests, without a live stdio process
in the loop — the wrapper's only job is translating MCP's calling convention into a normal
function call.

As registered in `src/coordharness/coord/mcp_coord_server.py`, the tools fall into five rough
groups. Full argument lists are in the source; the tables below name what each tool is for.

**Orientation** — what an agent calls at the start of a session, before touching any specific
row:

| Tool | Purpose |
|---|---|
| `preflight` | Bounded read-only session start: your assigned work, a capability handshake, and the client transport the server observed. |
| `orient` | Registers/renews this session's presence lease and returns claims, peers, and inbox together — the fuller session-start call. |
| `next_work` | Deterministic list of queued/planned rows already assigned to you. |

**Lifecycle** — claiming, holding, and releasing a unit of work:

| Tool | Purpose |
|---|---|
| `claim_work` | Checks a row out atomically and returns `claim_id` plus the exact `claim_fence` used by tracked-job launch. Optional fields can seed a brand-new row or fill gaps on an existing one. |
| `heartbeat` | Renews a claim's lease so it doesn't expire mid-task. |
| `release` | Frees a claim; `status` is `released`, `paused`, or `blocked`. |
| `park` / `block` | Same shape as `release`, for the two cases needing a durable resume condition — work you'll pick back up, and work stuck on something external. |
| `complete` | Marks the claim and the work item done, after recording proof that an artifact exists. |
| `classify_blocked`, `recover_blocked`, `correct_tier`, `resume_parked` | Narrower repair verbs: correct a blocked row's recorded reason, requeue a blocked row with no live claim behind it, correct a declared review tier, or requeue one held-open paused row. Each takes an `expected_version` (or `expected_reason_class`/`expected_tier`), so the call fails cleanly if the row moved under you instead of silently overwriting someone else's edit. |

**Communication** — passing information between agents without touching ownership:

| Tool | Purpose |
|---|---|
| `note` | Append-only, neutral context on a row — not a status change. |
| `inbox` | Read an actor's inbox, optionally advancing the read cursor. |
| `inbox_recent` | The same inbox, most-recent first, without moving the cursor — safe to call speculatively. |
| `read_note` | Resolves a pointer returned by a query tool into its full body, on demand. |
| `handoff_existing` | Hands an existing, proof-bearing row to another actor under a compare-and-swap on its version, assignee, and recent event history. Cannot create new work, only transfer a row that already exists. **Deferred** (see below) — absent from the tool list unless explicitly promoted. |

**Evidence** — the record of decisions and review that a row's history accumulates:

| Tool | Purpose |
|---|---|
| `audit` | Appends a general-purpose event. A fixed list of reserved kinds — `decision`, `handoff`, the lifecycle verbs, and others — is rejected here; those only mint through their own typed tools, so the event log can't be spoofed by hand. |
| `request_audit` | Asks another actor to review a row, without changing who owns it. |
| `verdict` | Records a typed `PASS`/`FLAG`/`BLOCKED` outcome, idempotent on `operation_id` — call it twice with the same id and you get the same result, not two events. |
| `decision` | Mints a first-class ruling, the only path that can record the `decision` kind. Supports supersession and validity windows. Optional `memory_candidate=true` marks the ruling as a durable-fact candidate; it is false by default and does not itself write memory. |
| `session_closeout` | Closes out a session's work: fails if you still hold open claims or unanswered directed events. After that fence passes, offers current marked fact candidates to the server-bound knowledge store; failures leave replayable receipts and superseded candidates resolve stale. |
| `get_decision_context` | Reads back the current decision(s) in force for a row or scope, resolved the same way `decision` writes them. |

**Query** — read-only lookups against the board and its history:

| Tool | Purpose |
|---|---|
| `board` | The whole board, or a filtered slice of it, grouped by module/lane/whatever field you ask for. |
| `work_context` | Everything about one row: its own fields, its parent, and a compact summary of recent events. |
| `event_context` | Expands a single event by id. |
| `facts_lookup` | Queries a separate facts/decisions index, trying current-value lookup, then decided rulings, then full-text search, in that order. |
| `knowledge_search` | Full-text search across a federated set of providers (the board, its history, facts, accepted-memory notes). Returns pointers and short snippets, not full documents — pair with `read_note` to pull a hit's body. |
| `runs` | Process-liveness records: which claims have a live PID actually behind them right now. |

The visible catalog is profile-dependent. `handoff_existing` is a deferred tool:
the deployment must expose it through the compatibility/promotion contract before
a client can call it. `preflight` returns capability and transport metadata, so a
client should inspect that response rather than rely on a hard-coded tool count.

## Read/write split

Every write goes through `connect()`, an ordinary read-write SQLite connection in WAL mode.
Every read-only tool — `board`, `work_context`, `event_context`, `runs`, `inbox_recent`,
`get_decision_context`, and the knowledge/facts tools — goes through `connect_ro()` instead,
which opens the file with SQLite's `mode=ro` URI flag and additionally sets `PRAGMA query_only
= ON`. That's a genuine read-only connection at the SQLite level, not just a tool that happens
not to call `UPDATE`: a write attempted on that connection is refused by SQLite itself, before
it reaches application logic. Both kinds of connection are opened fresh for each call and
closed when the call returns — the server holds nothing open between calls, so a long-lived MCP
process never blocks a concurrent writer, and a read tool never contends with one either.

## Deferred tools

Not every registered tool is visible to every client. A small "heavy" set —
`handoff_existing` is currently the only member — is hidden by default and only appears once a
caller supplies a promotion manifest hash matching an accepted value the server was built with.
That accepted set ships empty, so `handoff_existing` stays hidden until a deployment explicitly
opts in.

A tool surface is a cost independent of whether any given tool gets called: every tool the
client sees is bytes in the initial tool listing, and a wide listing makes it easier for a
model to reach for the wrong tool under pressure. `handoff_existing` is also the one lifecycle
tool that changes who owns a row rather than what state it's in, with a heavier
compare-and-swap contract than the rest — a reasonable candidate for loading only into a client
that's actually going to exercise it. The mechanism itself is generic:
`coordharness.coord.deferred_tools` filters a supplied set of tool names against a hidden set
and an environment-driven promotion hash, with no assumption about which tool ends up on either
list. A deployment that wants a different tool hidden, or `handoff_existing` promoted, changes
the accepted-hash table and the `COORD_DEFERRED_TOOL_CATALOG` /
`COORD_DEFERRED_PROMOTION_MANIFEST_SHA256` environment variables — no change to the tool's own
code.

## Output budget

Two independent size limits keep a single tool response from flooding the conversation. A
generic budget, `coordharness.coord.output_budget.apply_output_budget`, applies to policy and
audit-shaped output: if a response's UTF-8 byte length exceeds an inline limit (12,000 bytes by
default), the full text is written to a file on disk and the response returns a truncated
inline copy plus a pointer to that file, with `truncated: true` so the caller knows to go fetch
the rest. It's on by default and controlled by `COORD_OUTPUT_BUDGET`.

`knowledge_search` carries its own, separate shrinking loop rather than using that generic
budget: it drops search results, then diagnostic fields, then trims the echoed query string,
checking after each step whether the response now fits a fixed transport-byte budget, with a
small reserve held back for the JSON-RPC envelope itself. It's also the one tool registered
with `structured_output=False`, so it hands back a pre-serialized JSON string rather than a
dict — the only way to control the exact wire size of what goes out. Both mechanisms bound
different shapes of payload: one is bounding arbitrary text, the other a ranked list where
dropping items is a more useful degradation than truncating mid-item.

## A worked example

A session picking up work on, say, an API migration might open with:

```
preflight(actor="claude")
  -> { assigned: [...], capability: {...}, client_transport: {...} }

claim_work(work_id="API-MIGRATE-7", step="draft the new client wrapper")
  -> { claim_id: "c_9f2a...", claim_fence: "PRIVATE_FENCE", work_id: "API-MIGRATE-7" }

heartbeat(claim_id="c_9f2a...", step="wrapper drafted, writing tests")

complete(claim_id="c_9f2a...", artifact_path="tests/test_new_client.py",
         artifact_kind="done_signal")
  -> { ok: true, work_id: "API-MIGRATE-7", status: "done" }
```

No session state lives inside the MCP server process beyond what's in the database, so a
client can disconnect and reconnect without losing anything, and two different agents calling
these tools against the same `COORD_DB` see the same board.
