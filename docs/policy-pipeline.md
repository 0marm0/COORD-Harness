# The policy pipeline

Every write to the board — claiming work, sending a heartbeat, handing off, marking
something done — passes through a fixed sequence of checks before the write lands.
This document covers what each check looks at, what it can do about what it finds,
and how to add a new one.

The pipeline exists because a coordination board is only as trustworthy as the
writes that land on it. An agent that claims sloppy work, loops on the same failing
action forever, or reports "done" without evidence produces a board that lies.
Rather than trust every caller to self-police, the harness runs the same checks on
every lifecycle action, in the same order, and hands the result straight back to
the agent that made the call.

## How a write reaches the pipeline

Each lifecycle action builds a `PolicyContext` — work item id, which boundary made
the call (CLI, an MCP tool, ...), the action name (`claim`, `heartbeat`, `done`,
`handoff`, ...), and whatever payload the action carries — and runs it through
`run_boundary_policy()`, which executes the ordered list of checks in turn. Each
check returns a `PassResult`: `ok`, `warning`, or `block`, with a reason string and
a `detail` dict for machine consumption.

Independent of that verdict, each check also carries a **mode**:

- **`report`** — even a `block` verdict is downgraded to a plain informational
  result; nothing is withheld.
- **`warn`** (the default for six of the seven checks) — a `block` verdict becomes
  a `warning`; the write still goes through, with the reason attached.
- **`enforce`** — a `block` verdict stays a block and the write is refused.

So a check's own logic for deciding something is wrong is entirely separate from
whether that verdict stops anything. One rule holds regardless of mode: if a
check's result claims it mutated coordination state directly, the pipeline
overrides it to a hard block — a policy check is only ever allowed to observe and
report, never to be the thing that changes board state ahead of the real lifecycle
handler. If a block survives its mode, the pipeline stops there; later checks do
not run for that write.

## The seven checks, in pass order

| # | Check | Looks at | Default mode |
|---|-------|----------|---------------|
| 1 | `creation_lint` | whether the write carries a work item id | warn |
| 2 | `loop_doctor` | repeated-action and recurring-work patterns | warn |
| 3 | `token_budget` | token spend against a declared budget | warn |
| 4 | `structured_status` | whether a "done" claim has a valid terminal state | warn |
| 5 | `output_budget` | size of any inline output the write carries | warn |
| 6 | `run_event_emit` | records a trace event for the write | report |
| 7 | `deferred_tool_catalog` | which heavy tools are visible to the caller | warn |

**`creation_lint`** checks that actions which create or attach to a work item
(`claim`, `handoff`, `launch`) actually carry a work item id — the cheapest,
narrowest check in the pipeline. A larger set of creation-quality rules
(descriptive titles, a resolvable owner, done-signal grammar) lives in a separate
module that runs once, when a work item is first created; this pass is just the
last-line check that the id itself is present on every later write.

**`loop_doctor`** watches for two signals: a tool invoked repeatedly when it is
tagged expensive or dangerous (a full test run, a delete) — a sign the agent is
retrying the same costly action rather than changing approach — and a work item
whose title or notes read as recurring or autonomous ("daemon", "watcher",
"scheduled") but carries no declared *loop contract* (a schema naming purpose, the
observe/choose/act/verify/record steps, stop states, and iteration/wall-clock
limits). An unbounded loop with no declared stop condition is exactly what this
check exists to catch.

**`token_budget`** compares tokens consumed against a per-work-item budget, when
one is declared, by rolling up recorded token-usage events. Crossing 80% of budget
returns a warning carrying budget, used, remaining, and ratio. Items with no
declared budget are skipped outright — the check invents no default.

**`structured_status`** is where a `block` is routinely allowed to bite. If the
work item declares a loop terminal state, the check validates it against a fixed
vocabulary (`success`, `clean_noop`, `blocked`, `approval_required`, `exhausted`,
`stagnated`, `failed`), and for a `done` action specifically requires the state to
be one of the two that may actually close an item (`success` or `clean_noop`). A
`done` call carrying `stagnated` or `failed` is refused, not warned about: the
pipeline promotes this one check from its default `warn` to `enforce`
automatically whenever the payload declares a structured terminal state. Work with
no such status at all passes through untouched — enforcement switches on only once
an agent opts in to declaring one.

**`output_budget`** measures the size of any inline output text a write carries
against a fixed limit (12,000 bytes by default). Going over produces a warning
naming the size and the limit, not a rejection — the intent is to nudge a caller
toward writing large output to a file and passing a pointer instead. With no
inline output present, the check reports it had nothing to measure.

**`run_event_emit`** is the one check whose default mode is `report`, because its
job is not to judge the write but to leave a trace of it: a compact record of the
boundary, action, work id, and a handful of payload fields, appended to an
event log. It is explicitly best-effort — an unavailable event store is reported,
not treated as a reason to block the write it is merely trying to log.

**`deferred_tool_catalog`** filters a caller's requested tool list against a small
set of tools registered as heavy (expensive or high blast-radius) and not yet
promoted for that caller. Anything on the heavy list that is not promoted comes
back as deferred rather than handed to the caller, who sees only the lighter set
until it is explicitly promoted.

## A real policy block

This is the `policy` object returned from a real `claim` call against a freshly
seeded demo board (all seven checks passed, so every result reads `ok`):

```json
{
  "ok": true,
  "blocked": false,
  "block_reason": null,
  "warning_count": 0,
  "results": [
    { "order": 1, "name": "creation_lint", "mode": "warn", "status": "ok",
      "reason": "creation metadata observed" },
    { "order": 2, "name": "loop_doctor", "mode": "warn", "status": "ok",
      "reason": "loop contract check passed" },
    { "order": 3, "name": "token_budget", "mode": "warn", "status": "ok",
      "reason": "token budget within warn threshold" },
    { "order": 4, "name": "structured_status", "mode": "warn", "status": "ok",
      "reason": "structured status observed" },
    { "order": 5, "name": "output_budget", "mode": "warn", "status": "ok",
      "reason": "no inline output supplied" },
    { "order": 6, "name": "run_event_emit", "mode": "report", "status": "ok",
      "reason": "run event emission is best effort",
      "detail": { "emitted": true, "event_id": 1 } },
    { "order": 7, "name": "deferred_tool_catalog", "mode": "warn", "status": "ok",
      "reason": "tool catalog visible set allowed" }
  ],
  "warnings": [],
  "pass_order": ["creation_lint", "loop_doctor", "token_budget",
    "structured_status", "output_budget", "run_event_emit",
    "deferred_tool_catalog"]
}
```

`detail` is trimmed above for readability; each result carries a fuller dict in
practice — budgets, thresholds, event ids. `pass_order` is always present and
always in this sequence, so a caller can reason positionally ("did check 3 run
before this failed?") without parsing names.

## Why the pipeline reports back instead of just gating

A pipeline that only gated writes — accept or reject, silently — would leave the
calling agent to guess why a write was refused, or that anything was noticed at
all. Every result the pipeline produces, including the ones that never block
anything, is handed straight back with the write's own response. An agent that
sees *"loop_doctor: strong recurring/autonomy signal but no
acceptance_json.loop_contract or loop_contract_ref"* attached to its own claim can
add the missing contract on its next action, without a human noticing the gap
first. That is the actual point of running most checks in `warn` mode: make the
problem visible to the agent causing it, at the moment it happens, and let the
agent self-correct. Escalating to a hard block is reserved for the few places
— a terminal-state claim on `done` chief among them — where a wrong write would
corrupt what "done" means for everyone downstream.

## Adding a check

A check is a plain function — a `PolicyHandler` — that takes a `PolicyContext` and
returns a `PassResult` via one of its constructors (`.ok`, `.report`, `.warn`, or
`.block`). To add one:

1. Write the handler in `pipeline.py` (or import one from elsewhere and wrap it).
2. Add its name to the `PASS_ORDER` tuple, in the position it should run.
3. Add a default entry to `DEFAULT_PASS_MODES` — `"warn"` unless the check should
   be silent by default (`"report"`) or should stop writes out of the box
   (`"enforce"`, which should be rare and deliberate).
4. Register the handler in `default_policy_passes()`'s handler map, under the same
   name.

Because mode is decoupled from a check's own verdict, a new check can ship in
`warn` mode from day one — safe to deploy — and be promoted to `enforce` later
once its false-positive rate is known. The conditional promotion that
`structured_status` uses (escalate only when the payload actually declares a
terminal state) is the pattern to copy for any check that should escalate
narrowly rather than unconditionally.

## Honest limits

Six of the seven checks default to `warn`, not `enforce`. In the shipped
configuration, the only path that reliably stops a write outright is
`structured_status` on a `done` action once the payload opts in to a structured
terminal state — everything else is advisory unless a deployment's local
configuration explicitly raises a check's mode. That means most of what this
pipeline buys is *visibility*, not enforcement: an agent that ignores its own
warnings can still push a write through six advisory checks in a row. The two
guards that are structurally enforced elsewhere in the harness — you cannot claim
another actor's work, and you cannot complete work without controller-declared proof —
are not part of this pipeline at all; they live in the lifecycle code the
pipeline runs alongside, not in a policy check.
