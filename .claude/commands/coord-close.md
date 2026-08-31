---
description: Close a row against its declared proof, or park/block with a durable resume contract.
argument-hint: <WORK-ID>
---

# Close a row

The completion gate is proof-gated and fails closed. Run the checklist below
*before* calling `done`, so a missing proof is something you decide about rather
than something a refusal tells you.

## 1. Re-read the declared proof

```bash
.venv/bin/coord work-context WORK-ID
```

or MCP `work_context`. Take the row's `done_signal` verbatim. The gate accepts that
path and no other. Three states fail closed:

- the artifact is at a different path than the one declared;
- the proof is Markdown that Git's current index does not track (`git add` is
  enough — a commit is not required);
- you do not hold a live claim on the row.

Do not repoint `done_signal` at whatever you happened to produce. The declaration
is what made the row closable by someone other than you; editing it to fit the
output is how a board starts lying.

## 2. Complete

```bash
.venv/bin/coord done WORK-ID --artifact reports/refund-path.md
```

or MCP `complete` with the same artifact. The artifact path passed here must be the
declared one.

## 3. If the proof is not there, stop honestly

Park when you know the next action and it is resumable; block when progress needs
authority, credentials, or external state you do not control. Both require a
concrete `next_step` and a concrete `resume_when` — not "will get back to this".
That pair is a queryable column a different agent reads weeks later; the same
sentence in a heartbeat is gone with the lease.

```bash
.venv/bin/coord release CLAIM-ID --status paused \
  --next-step 'wire the retry budget into the settlement path' \
  --resume-when 'the pooling change lands'

.venv/bin/coord release CLAIM-ID --status blocked \
  --reason 'the upstream export is incomplete before the fifth of the month' \
  --next-step 're-run the export comparison' \
  --resume-when 'the upstream export is complete'
```

MCP `park` and `block` write the same contract. Never weaken identity, proof,
review, or authority checks to make a transition pass.

## 4. Completion is authorship, not review

A terminal artifact is evidence, not authorization for deployment, publication, or
another lifecycle. For work that is served, external-facing, irreversible, or
ground-truth, ask the other lane to look:

```bash
.venv/bin/coord request-audit WORK-ID \
  --task 'check the retry path against the declared proof' \
  --why 'this surface is served and the change is hard to detect from outside' \
  --ref reports/refund-path.md

.venv/bin/coord verdict WORK-ID --verdict PASS --ref reports/refund-path.md
```

MCP `request_audit` and MCP `verdict` write the same events. The refs name what the
reviewer actually read, and a verdict belongs on the row it reviews — a pass
recorded somewhere else leaves the row unreviewed.

## 5. Closing the session, not just the row

Release every claim you still hold, then record what the next session cannot
reconstruct: MCP `session_closeout` with a summary, successor hints, and
`dead_ends`. The dead ends are the most-skipped field and the least recoverable —
they are the only structured record of what was tried and did not work.
