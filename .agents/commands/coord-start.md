---
description: Orient against the board before claiming anything, under one stable session identity.
argument-hint: [lane:scope session label]
---

# Start a coordinated session

Orientation is a read. Do not claim, create, or edit anything while running this
command; finish by naming the row you intend to take, then run `coord-claim`.

## 0. Is this clone set up?

If `.venv/bin/coord` is missing, run `./scripts/setup.sh` and start a new session —
the checked-in launcher `scripts/coord-mcp-launch.sh` fails closed with that same
line, and a client that launched before setup will not rediscover the server.

## 1. Fix one identity for the whole thread

```bash
export COORD_ACTOR=claude          # or codex
export COORD_SESSION_ID=claude:billing-export
```

The label is `lane:scope` and it describes what this thread *does*, not what the
last prompt said. Claims are keyed to `(work_id, session_id)`: a session id minted
per prompt makes every resume look like a stranger asking for someone else's row,
and leaves one abandoned-looking session on the board per instruction. If the
runner exposes a stable id (a CI job id, a host session id), prefer it.

## 2. Verify the wiring once per clone

```bash
.venv/bin/coord onboard
# Registering installed MCP clients writes outside this clone, so it stays opt-in:
# .venv/bin/coord onboard --register-clients
```

Expect `PASS`. A missing installed-client registration reports `BLOCKED` with the
exact idempotent remediation command; an absent client is `SKIPPED`, not a failure.

## 3. Read the board before reading the code

1. MCP `preflight` with your actor and session id — open work assigned to you,
   claims you already hold, rows parked with a resume condition, and unread
   messages addressed to you.
2. MCP `next_work` to rank what to pick up; MCP `board` or `.venv/bin/coord board`
   for the live shape.
3. MCP `inbox_recent` (or `.venv/bin/coord inbox --actor "$COORD_ACTOR"`) if
   preflight showed messages.

Stop as soon as you can name the next row. Each read returns pointers; follow only
the pointers the task needs. Do not open a full board dump or the documentation set.

The `board_context` lenses (`capsule`, `digest`, `focus`, `search`) are richer but
fail closed with `ExactQueryCoreError` on a board whose exact-authority policy is
not in enforce mode with an active generation — which is the normal state of a
fresh or demo board. If a lens raises, that is the reason; use `coord board`.

## 4. Decide, do not act

- **preflight shows a claim you already hold** → resume that row under this same
  session id. Do not claim it again, and do not mint a new label.
- **A row is parked or blocked** → its `next_step` and `resume_when` are the
  handover; read them before assuming the work is fresh.
- **The board is empty** → create the first row with a proof it can actually be
  closed against. The durable id is `PREFIX-LANE-SLUG`; a bare `PAY-101` is
  refused because an id that does not name its owner stops being legible.

```bash
.venv/bin/coord create PAY-CLA-REFUND-PATH \
  --title 'Repair the refund path' \
  --module payments \
  --done-signal reports/refund-path.md \
  --acceptance 'the refund path completes against the declared proof' \
  --note 'first row for this workstream'
```

## Report before you stop

What is live, what is yours, what is waiting on you, and the one row you intend to
claim next — with its `done_signal`, so the close is already known to be reachable.
