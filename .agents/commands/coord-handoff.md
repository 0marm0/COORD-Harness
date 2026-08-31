---
description: Move responsibility for a row to the other lane with exact fences and evidence the receiver can act on.
argument-hint: <WORK-ID> <receiving lane>
---

# Hand a row to another lane

Responsibility moves by a typed, fenced transfer. It does not move by leaving a
note and hoping, and it does not move by the receiver claiming the row: claiming
across an owner boundary is refused, and the refusal is the safety property — the
receiver cannot make itself the owner.

## 1. Read the row and copy its fences

```bash
.venv/bin/coord work-context WORK-ID
```

The response carries a `handoff_preconditions` block containing exactly the values
the transfer needs: the row's current version, its current assignee, and its active
head event ids. Copy them; do not remember them from an earlier read. Two agents
racing to hand off the same row get one winner and one clean rejection, which only
works if both sent the state they actually saw.

## 2. Write it so it survives your transcript

The test for a handoff: delete this session's transcript and ask whether the
receiving lane can still act. Every claim in `--why` that the receiver would
otherwise take on trust gets a `--ref` — an artifact path, a work id, a decision.
If the evidence exists only in this conversation, write it to a file first and ref
that, or record it with MCP `note` on the row before transferring.

```bash
.venv/bin/coord handoff WORK-ID \
  --owner-lane codex \
  --task 'finish the retry path against the declared proof' \
  --why 'the receiving lane owns the settlement slice' \
  --acceptance 'reports/refund-path.md satisfies the row acceptance' \
  --operation-id handoff-refund-path-0001 \
  --expected-version VERSION-FROM-WORK-CONTEXT \
  --expected-assignee claude \
  --expected-head-event-id HEAD-EVENT-ID \
  --ref docs/agent-protocol.md \
  --constraint 'the coordination database stays the lifecycle authority'
```

For an empty head-event list, omit `--expected-head-event-id`. The `--operation-id`
is the idempotency key: a retried transfer replays its original result instead of
duplicating.

## 3. If the row drifted, decide again

A rejected transfer means the row is no longer in the state you assumed. Re-read
it and make the decision again. Never reuse stale fences, and never reassign by
editing the database or a projection.

## 4. After the transfer

The ownership change commits first; only then does the receiving lane claim under
its own identity with `coord-claim`. Tell the receiver it is there — MCP `note`
addressed to that lane carries context on the row, and MCP `inbox` is where the
receiver finds it. A note is a pointer to the work, never the transfer itself.

MCP `handoff_existing` is a deferred tool: it appears in the visible catalog only
on a client profile that promotes it. The CLI above calls the same fenced core, so
prefer it rather than concluding the operation is unavailable.
