# Review tiers

## 1. The problem

A coordination board lets several agents close work on their own: claim a row, do the work, attach an
artifact, mark it done. That is fine for most rows. It is not fine for all of them.

Some changes are cheap to get wrong and cheap to notice — a refactor that fails its own tests will fail
loudly, in review, in CI, or in the next person's face. Other changes are expensive to get wrong and easy
to miss: a value that gets served to a user, a switch that cannot be flipped back, a label that other
systems treat as ground truth. If an agent can write and grade its own homework on that second kind of row,
the board's "done" stops meaning anything.

The harness answers this with three review tiers. The tier controls how much independent scrutiny a row
needs before it can close, not how the work itself gets done.

## 2. Tiering

Every work row is either **T0**, **T1**, or **T2**.

| Tier | What it covers | Review requirement |
|------|-----------------|---------------------|
| T2 | Declared documentation, analysis, or tooling changes that are trivially reversible | Whatever automated gates the row's kind already runs. No dedicated review row, usually no review event at all. |
| T1 | The default for code and data changes | Can complete now. Evidence goes into a batched review — one bundle per reporting lane per day, not a review per row. |
| T0 | Anything served to a user, anything external-facing, anything irreversible, or anything treated as ground truth by other systems | Blocking. An independent reviewer must record a verdict on the row before it can close. |

T1 is the floor: a row with no signal either way defaults to T1, not T0. T0 is a **ceiling that some rows
are pinned to regardless of what anyone declares** — see below.

### How a row's tier is decided

A row can carry a *declared* tier — `T0`, `T1`, or `T2` set on the row itself. But the declared tier is not
the last word. `effective_review_tier()` also runs pattern checks over the row's own text — its title,
acceptance criteria, kind, module and sublane — plus its references: `done_signal`, `context_pack_ref`,
`depends_on`, and any explicit refs passed alongside it. Two families of pattern run: **prose patterns**
look for phrases like a served value or metric, an activation or cutover, a model or label promotion, an
external publication, ground truth, or the words "irreversible" and "cross-lane configuration"; **path
patterns** look at the same reference fields for path segments naming the same kinds of thing — a
`served_config/` directory, a `promotion_receipts/` folder, anything with `activation` or `cutover` in a
path component, a `ground_truth/` or `label_registry/` tree.

If any pattern matches, the row carries one or more **T0 predicate reasons** — short codes such as
`served_number`, `activation`, `ground_truth`, `irreversible`. The rule is simple: reasons present means
the effective tier is T0, no matter what was declared, unless the row has been explicitly authorized to
tier down (an opposite-lane acknowledgement or an operator event, referenced from the row). A declared
`T0` on a row with no matching reasons is honoured as declared. A row that declares nothing and matches
nothing defaults to T1.

`validate_tier_declaration()` is the strict entry point used when a row is created or edited: if the row
declares T1 or T2 but the pattern checks find T0 reasons and no authorization is on file, it refuses with
the specific reasons that fired. A human or an agent describing a row honestly ("this activates the new
config") cannot accidentally under-declare it, because the words that describe the danger are the words
the classifier watches for.

**This is a starting point, not a finished taxonomy.** The patterns above are illustrative — the words a
generic infrastructure team would use for "this is served," "this cannot be undone," "this is the record
other systems trust." A real deployment should read `review_tier.py` and extend or replace the pattern
tables with the vocabulary of its own domain. A classifier that recognises nothing will silently leave
everything at T1.

## 3. Verdicts are events on the work row, not a status field

There is no `reviewed` column that gets flipped to true. A review verdict — `PASS`, `FLAG`, or `BLOCKED` —
is recorded as an event, of kind `audit_verdict`, attached to a specific `work_id`, written by a specific
`actor`. Whether a row counts as reviewed is computed at read time by walking its events, never stored.

That computation, `classify_verdict_status()`, answers one question: is there a verdict on **this row**,
written by an actor in the **independent lane** (not the lane that authored the work), recorded **after**
the point at which review was actually requested? If yes, the row is reviewed. Every other outcome is a
named, distinguishable kind of *not reviewed*:

- **`self_verdict`** — the most recent verdict on the row was written by the same lane that authored the
  work. An agent cannot pass its own review.
- **`cross_row_verdict`** — no verdict exists on this row, but a verdict was found on some *other* row whose
  references, payload, title, or body mention this row's ID. This is the laundering case: closing row A by
  pointing at a verdict that was actually about row B. It is treated as unreviewed even though a verdict
  exists somewhere in the database.
- **`verdict_absent`** — nothing on file at all.
- **`moot_closed_without_verdict`** — the row was closed as moot (superseded, no longer applicable) without
  ever getting a verdict, and no exemption applies.

A row that was never claimed, has no artifact, and is marked superseded with an explicit "superseded by"
note is treated as reviewed by construction — there is nothing an independent reviewer could check on work
that never happened.

### Why the barrier matters

A verdict only counts if it comes *after* the review was requested — the **review barrier**. The barrier is
the later of two events: an `audit_request` (someone asked for review) or an `acceptance_contract_repaired`
event (the row's acceptance criteria were fixed after a prior rejection). Verdicts written before the
barrier are stale; they were about an earlier version of the row's acceptance contract, and reusing them
would let a row skip review of whatever changed since. Operator sign-off (`operator_ok`) can substitute for
a peer verdict, but only when no review barrier is currently open — an operator "yes" from before a request
does not answer the request.

## 4. What counts as ready for review

`review_ready_t0_queue()` is the worklist an independent reviewer actually pulls from. A row appears in it
only if all of the following hold: its effective tier is T0; it is in an active state (running, blocked, or
attention); it was authored by one of the two independent lanes; a review was actually requested for it (an
`audit_request` event, or a `blocked_reason_class` that explicitly says the row is waiting on review); its
`done_signal` already resolves to something real; and `classify_verdict_status()` says it is not yet
reviewed. The required reviewer is always the *other* lane from whoever authored the row — an agent is
never queued to review its own work.

Rows sort into three priority classes — served value or fail-open path first, other served or external
surfaces second, everything else T0 last — then by how long the request has waited. `owed_t0_verdicts()`
runs the same check backwards over rows already in a terminal state, to find anything that closed without
ever picking up its verdict.

### Claim readiness, and why the two surfaces disagree on purpose

Before a row can be claimed it is checked for the fields a review depends on: a descriptive title, a
`done_signal`, and — for T0/T1 — acceptance criteria. That is `claim_readiness()` in `coord_db.py`, the
single definition both surfaces call.

The two surfaces do different things with the same answer, and the asymmetry is deliberate rather than an
oversight:

| Surface | Row missing fields | Why |
| --- | --- | --- |
| MCP `claim_work` | **refuses**, listing the missing fields (`ClaimReadinessError`, which carries `.missing`) | This is the door agents come through. A row claimed without a `done_signal` cannot be closed against anything, so the cheapest place to stop is before the lease exists. |
| CLI `coord claim` | **warns on stderr and proceeds**, and repeats the field list in its JSON under `claim_readiness` | The CLI is also the repair tool. Refusing here would make the incomplete row unclaimable from the one surface available to a headless shell session. |

`COORD_CLAIM_STRICT` is the only knob, read in exactly one place
(`claim_readiness_enforcement()`):

- `COORD_CLAIM_STRICT=1` — both surfaces refuse. Use this in CI, or on any deployment where every row is
  expected to be born complete.
- `COORD_CLAIM_STRICT=0` — both surfaces warn and proceed. The MCP response then carries the same
  `claim_readiness` block the CLI's JSON does.
- unset (or anything unrecognised) — each surface keeps the default in the table above.

Either way the fields are named. A warning that does not say *which* field is missing is the failure mode
this replaced.

## 5. Limits, as shipped

The tier and integrity checks are pure functions over the database — they compute, they do not enforce
gates by themselves. Something has to call `validate_tier_declaration()` when a row is written and check
`classify_verdict_status()` before a close is allowed to stick. In this build those callers are the
policy pipeline, the MCP tool surface, **and the command-line client**: `coord request-audit` writes an
`audit_request` event and `coord verdict` writes an `audit_verdict`, so a T0 row can be reviewed and
closed without an MCP client at all. That matters because an agent driven through a shell — which is how
a headless Codex session usually runs — would otherwise be unable to review anything, leaving every T0
row closable by the single lane that wrote it while the gates all read green.

The enforcement itself is unchanged and still lives in the shared functions; the CLI verbs are a surface
over them, not a second implementation. `park`, `preflight` and the facts/knowledge readers remain
MCP-and-Python-API only.

The pattern tables in `review_tier.py` are English-language and regex-based. They will not catch a row
described in another language, an abbreviation they do not know, or a genuinely new kind of irreversible
action nobody has named yet. Treat the shipped patterns as a template to tune, not a guarantee.
