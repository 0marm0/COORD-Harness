---
description: Claim a row after checking it can be closed, with a step, a write set, and a claim before any fan-out.
argument-hint: <WORK-ID> [what you are about to do]
---

# Claim a row

A claim is a lease, not a bookmark. Take it before you start reading code or
writing files for the row — not after the work is half done.

## 1. Read the row before taking it

```bash
.venv/bin/coord work-context WORK-ID
```

or MCP `work_context`. Three things decide whether this claim is safe:

- **Assignee.** If the row belongs to another actor, claiming it is refused and
  the error points at the typed transfer. Use `coord-handoff` from its owner
  instead of trying again.
- **`done_signal`.** A concrete repository-relative path. This is the *only* file
  the completion gate will accept later; check it now, while changing it is still
  cheap.
- **Acceptance.** The condition your finished work is measured against.

A row missing `done_signal` or acceptance is refused outright by MCP `claim_work`
and merely *warned about* by `.venv/bin/coord claim`. The warning is not
permission: a row you can claim but cannot close is the most common way an agent
spends a session and then has nothing the gate will take. Fix the row first
(`.venv/bin/coord create` for a new one), or set `COORD_CLAIM_STRICT=1` so both
surfaces refuse identically.

## 2. Claim with a legible step

```bash
.venv/bin/coord claim WORK-ID --step 'splitting the refund retry path'
```

or MCP `claim_work` with `step`. The step says what you are *about to do*, in one
line, so someone watching the board right now knows which part of the row is live.
It is not a place for findings — nothing downstream parses it and it dies with the
lease.

## 3. Declare a write set when another session is live

```bash
.venv/bin/coord claim WORK-ID --step 'refund retries' --write-scope path=src/billing/
.venv/bin/coord conflicts
```

or MCP `declare_write_set` and `write_set_conflicts`. Path scopes match as prefixes
in both directions, so a claim over a directory correctly collides with a claim
over one file inside it. Check *before* the first edit; a conflict found afterwards
is a merge, not a coordination. For two agents in genuinely separate modules this
is unnecessary ceremony — use it where the risk is real.

## 4. Fanning out is starting a job — claim first

Subagents do not talk to the board. They have no session, no claim, and no lease.
If you fan out without claiming the parent row, the whole fleet runs and finishes
invisibly: nothing moved, no lease exists to expire, and nobody watching would know
it ran. Claim the parent row, fan out, record each child's identity, model, and
outcome under that claim, then close the parent row. Recording a child's outcome
without a live held claim is refused by name.

## 5. Renew only to keep the lease

```bash
.venv/bin/coord heartbeat-claim CLAIM-ID --step 'refund retries, second pass'
```

or MCP `heartbeat`. A heartbeat extends the expiry; that is all it does. Call it
when a long turn is about to outlive its lease window or at a material milestone —
not on every tool call, and never as narration. Durable context goes elsewhere: MCP
`note` for what the other lane needs on this row, a park or block resume contract
for the fact that made you stop, the artifact for the output itself, and MCP
`decision` for a ruling that constrains later work.

Close the row with `coord-close`. Never leave a claim to expire silently.
