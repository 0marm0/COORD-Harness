# MCP integration

The MCP server exposes coordination as typed stdio tools. It is first-class, but not a second implementation: MCP handlers call the same lifecycle and query functions as the CLI and Python API against the same `coord.db`.

## Install and paths

```bash
python -m pip install -e '.[mcp]'
```

Use the installed executable:

```text
/absolute/path/to/COORD-Harness/.venv/bin/coord-mcp
```

The module fallback is exact and useful before the preview entry point is installed:

```text
/absolute/path/to/COORD-Harness/.venv/bin/python
-m
coordharness.coord.mcp_coord_server
```

Set both environment variables unless the subprocess is launched from the coordinated project:

- `COORD_PROJECT_ROOT=/absolute/path/to/project`
- `COORD_DB=/absolute/path/to/project/.coordharness/coord.db`

The first controls relative proof paths; the second selects lifecycle authority.

A third is optional:

- `COORD_KNOWLEDGE_DB=/absolute/path/to/project/.coordharness/knowledge.db`

It selects the fact ledger that `facts_lookup` and the facts provider inside
`knowledge_search` read. The server creates that store on startup if it is not there,
so a clean install answers knowledge queries from a store that actually exists.

### Absence is reported, not served as zero

A fact read names the store it read. Every successful `facts_lookup` reply carries a
`store` object with the resolved path and its state, so a `count: 0` is always
attributable to a ledger that was genuinely opened and searched.

When the ledger is missing, has no schema, or cannot be read, `facts_lookup` fails
with that named condition instead of returning an empty result, and `knowledge_search`
writes the same condition into the facts provider's `error` field and into
`context.errors`. The distinction is the point: a zero from a store that does not
exist is indistinguishable from a store that holds no match, and only one of those is
a real answer.

### Deployment profiles

The default `generic` profile is the portable standalone profile. It creates and migrates
the configured database and permits bounded read-only `preflight` against a fresh project.
Set `COORD_DEPLOYMENT_PROFILE=generic` explicitly when a client configuration might inherit
an unrelated shell environment.

All 33 default tools answer under `generic` against a fresh local database, including the
read surfaces — `preflight`, `board`, `next_work`, `work_context`, `facts_lookup`,
`knowledge_search`, `inbox`, and `runs`.

The `strict` profile deliberately enables repository-custody and exact-authority gates that
are deployment-specific. Do not select it for an ordinary fresh clone, and do not bypass a
strict-profile refusal by copying another installation's state.

Stated plainly, because the refusals are terse: a fresh checkout does not run `strict`
until an operator provisions and indexes the neutral local deployment-data symlink.
Until then, `preflight` fails closed on the missing strict-layout contract, while `board` and
`next_work` refuse with `exact-authority policy is not enforced and active`, because the
packaged migrations seed that policy in audit mode with no active generation. These are
fail-closed refusals working as designed for a deployment that has done its own authority
activation, not a defect to work around — use `generic`.

## Claude Code

Project-scoped CLI registration:

```bash
claude mcp add --scope project --transport stdio \
  -e COORD_PROJECT_ROOT=/absolute/path/to/project \
  -e COORD_DB=/absolute/path/to/project/.coordharness/coord.db \
  -e COORD_DEPLOYMENT_PROFILE=generic \
  coordharness -- /absolute/path/to/COORD-Harness/.venv/bin/coord-mcp
```

Project-scoped `.mcp.json`:

```json
{
  "mcpServers": {
    "coordharness": {
      "type": "stdio",
      "command": "/absolute/path/to/COORD-Harness/.venv/bin/coord-mcp",
      "args": [],
      "env": {
        "COORD_PROJECT_ROOT": "/absolute/path/to/project",
        "COORD_DB": "/absolute/path/to/project/.coordharness/coord.db",
        "COORD_DEPLOYMENT_PROFILE": "generic"
      }
    }
  }
}
```

Keep one server subprocess per live client process. Where Claude Code supplies `CLAUDE_CODE_SESSION_ID`, the server can bind lifecycle identity to it; otherwise pass `actor="claude"` and a stable `session_id="claude:…"` to tools that accept identity.

## Codex

CLI registration:

```bash
codex mcp add coordharness \
  --env COORD_PROJECT_ROOT=/absolute/path/to/project \
  --env COORD_DB=/absolute/path/to/project/.coordharness/coord.db \
  --env COORD_DEPLOYMENT_PROFILE=generic \
  -- /absolute/path/to/COORD-Harness/.venv/bin/coord-mcp
```


Codex config TOML:

```toml
[mcp_servers.coordharness]
command = "/absolute/path/to/COORD-Harness/.venv/bin/coord-mcp"
args = []

[mcp_servers.coordharness.env]
COORD_PROJECT_ROOT = "/absolute/path/to/project"
COORD_DB = "/absolute/path/to/project/.coordharness/coord.db"
COORD_DEPLOYMENT_PROFILE = "generic"
```

Codex identity can derive from its process environment. If the host does not forward a thread or session variable, pass `actor="codex"` and a stable `session_id="codex:…"` explicitly. Never reuse one session ID across concurrent Codex processes.

## Generic MCP client

A common JSON shape is shown below; verify the configuration schema for your client:

```json
{
  "mcpServers": {
    "coordharness": {
      "command": "/absolute/path/to/COORD-Harness/.venv/bin/coord-mcp",
      "args": [],
      "env": {
        "COORD_PROJECT_ROOT": "/absolute/path/to/project",
        "COORD_DB": "/absolute/path/to/project/.coordharness/coord.db",
        "COORD_DEPLOYMENT_PROFILE": "generic"
      }
    }
  }
}
```

If a client only supports a command plus argument array, use the module fallback:

```json
{
  "command": "/absolute/path/to/COORD-Harness/.venv/bin/python",
  "args": ["-m", "coordharness.coord.mcp_coord_server"],
  "env": {
    "COORD_PROJECT_ROOT": "/absolute/path/to/project",
    "COORD_DB": "/absolute/path/to/project/.coordharness/coord.db",
    "COORD_DEPLOYMENT_PROFILE": "generic"
  }
}
```

Generic clients should always pass an explicit actor and process-unique session ID on writer calls.

## Start-of-session sequence

Use this sequence only after `preflight` succeeds under the selected deployment profile.

1. Call `preflight(actor=..., session_id=...)` for bounded, read-only orientation.
2. Call `next_work(actor=...)` if no exact row is already assigned.
3. Expand only the chosen row with `work_context(work_id=...)`.
4. Call `claim_work(...)` before substantive work.
5. Keep the returned `claim_id`; use it for heartbeats and terminal actions.
   Keep the returned `claim_fence` private and pass it only to an exact
   tracked-job launch for the same work, session, and claim.

`orient` is a presence/lease writer in client profiles that expose it. Delegated subagents normally roll up beneath their orchestrator rather than registering independent sessions.

## Lifecycle example

Tool-call notation varies by client; the names and arguments below match the server:

```text
preflight(actor="codex", session_id="codex:ops-demo")

claim_work(
  work_id="OPS-503",
  actor="codex",
  session_id="codex:ops-demo",
  step="documenting the public API rate limits"
)

# The response includes claim_id plus the exact claim_fence. The lifecycle
# calls below use the ID; a tracked-job launch uses both values.

heartbeat(
  claim_id="CLAIM_ID_FROM_CLAIM_WORK",
  actor="codex",
  session_id="codex:ops-demo",
  step="proof drafted"
)

complete(
  claim_id="CLAIM_ID_FROM_CLAIM_WORK",
  actor="codex",
  session_id="codex:ops-demo",
  artifact_path="docs/reports/ops-503.md"
)
```

Create the artifact outside MCP using the agent's normal filesystem tools. The MCP server coordinates and validates the proof reference; it is not a general file-writing server.

`complete` applies the same custody rules as `coord done`, so a Markdown proof must be tracked by Git's current index before the call. Stage it with the agent's own shell — staging is enough, no commit is required. The stored proof is the repository-relative path the row declared, which keeps `coord doctor` green over the completion.

## Tool groups

| Group | Tools |
|---|---|
| Lifecycle | `claim_work`, `heartbeat`, `release`, `park`, `block`, `complete`, blocked/tier recovery tools |
| Board and runs | `board`, `runs`, `preflight`, `next_work`, `work_context`, `event_context` |
| Context and memory | `get_decision_context`, `knowledge_search`, `facts_lookup`, `read_note` |
| Communication | `inbox`, `inbox_recent`, `note`, `decision`, `audit`, `request_audit` |
| Review and closeout | `verdict`, `session_closeout` |
| Guarded handoff | `handoff_existing` when the deployment's deferred-tool profile exposes it |

The exact visible catalog is profile-dependent. Deferred tools stay hidden unless an explicit compatibility/promotion contract exposes them.

`handoff_existing` is intentionally not a casual reassignment button. It requires a
promoted, attested client profile plus the exact work version, assignment owner, and active
event heads returned by the read surface. If the tool is absent, use a note and leave the
assignment unchanged; do not edit SQLite directly.

## CLI equivalents

| Intent | MCP | CLI |
|---|---|---|
| Claim | `claim_work` | `coord claim WORK --step "…"` |
| Renew | `heartbeat` | `coord heartbeat-claim CLAIM --step "…"` |
| Release / pause / block | `release`, `park`, `block` | `coord release CLAIM --status ...` |
| Complete | `complete` | `coord done WORK --artifact PATH` |
| Board | `board` | `coord board --group-by FIELD` |
| Exact work context | `work_context` | `coord work-context WORK` |
| Typed handoff | guarded `handoff_existing` | `coord handoff WORK` with exact context fences |
| Inbox | `inbox` | `coord inbox --actor ACTOR` |

Preflight, broader context, facts, knowledge, review, and closeout remain richer MCP/Python surfaces. The generic CLI handoff uses the same canonical fenced DAO; it does not weaken the MCP deferred-tool promotion contract.

## Safety and debugging

- MCP uses stdio. Do not point an HTTP client at `coord-mcp`.
- Prefer the repository-local `./.venv/bin/python` with explicit `cwd = "."` in a project-scoped config; use absolute paths only when the runtime lives outside the project. Do not rely on an activated shell or GUI PATH.
- One MCP subprocess should not impersonate several concurrent actor sessions.
- Read tools use query-only connections; writer tools go through typed operations and policy.
- Never expose the database or board to an untrusted remote caller. Actor names are not authentication.
- If tools are missing, inspect `preflight` capability metadata and the client profile before assuming the server is broken.
- If proof resolves incorrectly, compare `COORD_PROJECT_ROOT` with the project containing the artifact.

See [MCP server reference](mcp-server.md), [agent protocol](agent-protocol.md), and [security and privacy](security-and-privacy.md).
