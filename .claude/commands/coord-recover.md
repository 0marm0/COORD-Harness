---
description: Reconcile a row when the board and reality disagree — stale claims, dead sessions, blocked rows, overlapping write sets.
argument-hint: [WORK-ID]
---

# Recover a row the board is wrong about

The board disagreeing with reality is a normal event: a session died mid-turn, a
lease outlived the process it described, a row says running with nothing behind it.
The recovery is always to write the true state through a lifecycle verb. Never
hand-edit the database or a projection to make the two agree — the row's event
history is the evidence that the state was ever wrong.

## 1. Establish what is actually true

```bash
.venv/bin/coord work-context WORK-ID
.venv/bin/coord board
```

plus MCP `preflight` for your own held claims and MCP `event_context` for the row's
recent history. Identify the claim id, the holding session, and whether that
session is live. A claim held by a live session is not stale, however old it looks.

## 2. Match the symptom

**The claim is mine and the lease is near expiry.** Renew it and continue:
`.venv/bin/coord heartbeat-claim CLAIM-ID --step '...'`. Resume under the *same*
session id — a claim on a paused or blocked row is recognised by whether the
resuming session shares identity with the original holder, so a fresh label turns
your own resume into a stranger's request.

**The row says running and nothing is.** Release it with an honest status rather
than letting the lease lapse silently:

```bash
.venv/bin/coord release CLAIM-ID --status released --reason 'the holding session ended mid-turn'
```

Use `--status paused` with `--next-step` and `--resume-when` if the work is
genuinely resumable; use `--status blocked` with a `--reason` naming the criterion
if it is not. Either way `--resume-when` on its own is refused —
`resume_trigger_contract_invalid` — the CLI also needs exactly one of
`--resume-manual` (nothing to check, a person decides) or `--resume-predicate`
(a JSON condition the board can check itself); see `coord-close`'s step 3 for
worked examples of both.

**The row is blocked with no usable resume contract.** MCP `classify_blocked` to
type the blockage, then MCP `recover_blocked` once the criterion is met. For a
parked row whose condition has since been satisfied, MCP `resume_parked`.

**The row belongs to a live session that is not mine.** Do not take it. Send MCP
`note` to that lane, or have its owner run `coord-handoff`. A row taken out from
under a live holder is refused at the database layer anyway.

**Two claims are editing the same paths.** `.venv/bin/coord conflicts` names the
specific rows and the specific overlapping scopes. Split the scopes so one path has
one pen, and re-declare with `.venv/bin/coord declare-write-set`.

**Something looks structurally wrong.** `.venv/bin/coord doctor` runs read-only
safety and integrity checks. It diagnoses; it does not repair, and a failing check
is not a reason to bypass a gate.

## 3. Leave the row legible

After recovering, state the true position once — a claim step if you are continuing,
a resume contract if you are not — and say in your report which row you changed,
which verb you used, and what the board said before and after. A recovery that
nobody can see is the same defect one turn later.
