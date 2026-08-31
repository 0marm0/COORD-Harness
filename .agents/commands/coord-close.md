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

`resume_when` alone is not accepted: the CLI's resume-trigger contract also
requires saying *how* the row gets picked back up, with exactly one of
`--resume-manual` (a person or agent judges it, there is no fact to check) or
`--resume-predicate` (a JSON condition — `artifact_exists`, `event_exists`,
`verdict_posted`, and a few others; `coord release --help` and
`continuation_contract.py` name the rest) that the board can check itself. A
release with `--resume-when` and neither flag is refused:
`resume_trigger_contract_invalid: resume_when requires --resume-predicate or
explicit --resume-manual`.

```bash
.venv/bin/coord release CLAIM-ID --status paused \
  --next-step 'wire the retry budget into the settlement path' \
  --resume-when 'the pooling change lands' \
  --resume-manual

.venv/bin/coord release CLAIM-ID --status blocked \
  --reason 'the upstream export is incomplete before the fifth of the month' \
  --next-step 're-run the export comparison' \
  --resume-when 'the upstream export is complete' \
  --resume-predicate '{"type":"artifact_exists","path":"reports/upstream-export-complete.marker"}'
```

The first case takes `--resume-manual` because nothing this row can query says
when the pooling change "lands" — only a person (or the pooling row's own
close) knows. The second names a real fact instead: once the export step
writes its completion marker, `--resume-predicate` lets the board recognise
that on its own rather than trusting someone to remember to look. MCP `park`
and `block` write the same contract, taking `resume_manual` or
`resume_predicate` the same way. Never weaken identity, proof, review, or
authority checks to make a transition pass.

## 4. Completion is authorship, not review

A terminal artifact is evidence, not authorization for deployment, publication, or
another lifecycle. Whether anyone independent has to look depends on the row's
*effective* tier (docs/review-tiers.md) — its declared tier plus a pattern check
over its own title, acceptance, and refs, computed fresh on every read, not
whatever was declared when it was created:

- **T1 or T2 (the common case)** — the row self-verifies. Don't call
  `request-audit`; it is refused for anything below T0
  (`audit_request rejected for effective tier T1; T2/T1 work self-verifies and
  may be included in one batched evidence review`). Fold this row's evidence
  into your lane's one batched review bundle for the day instead.
- **T0 — served, external-facing, irreversible, or ground-truth** — request a
  review:

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

**The row is genuinely T0 in nature but was declared T1/T2 at creation.** There is
no CLI verb to retier an existing row — `--tier` only exists on `coord create`.
The repair is MCP `correct_tier` (`expected_tier`/`new_tier`, a `reason`, and at
least one evidence `ref`); it is the row's own author who can raise its own
tier this way (only *lowering* it needs the opposite lane). Do not try to talk
`request-audit` into accepting a T1 row instead — that is the self-grading this
gate exists to stop.

## 5. Closing the session, not just the row

Release every claim you still hold, then record what the next session cannot
reconstruct: MCP `session_closeout` with a summary, successor hints, and
`dead_ends`. The dead ends are the most-skipped field and the least recoverable —
they are the only structured record of what was tried and did not work.
