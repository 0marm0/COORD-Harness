# Context architecture

> **Status: Generic profile supported; strict profile fail-closed.** A fresh generic
> `coord.db` supports the bounded capsule and lens commands directly. The strict deployment
> profile additionally requires its repository-custody and exact-authority policy; a strict
> refusal must not be bypassed by selecting generic for deployment-owned state.

An agent that starts a new session with no memory of the last one has one honest way to catch up: read
everything. That works at ten rows. It stops working once the board has a few hundred rows and the project
has a few hundred documents, because "read everything" now costs more tokens than the task itself, and most
of what gets read is irrelevant to the one row the agent is about to work on.

`coordharness` takes a different position: an agent should boot from a small, bounded read, and go get more
only when it names what it needs. This document covers the pieces that make that possible — a tiered
context model, bounded board reads, a federated query with named budget profiles, full-text indexed lookups
for facts and knowledge — and the backstop underneath all of them, `output_budget`. None of this hides
information: every bounded read carries an `expansion` block naming the next command to run for more. The
bound is a default, not a ceiling.

## Why "catch me up" is expensive twice

A pasted transcript costs tokens twice: producing it, since someone has to read the board and the docs and
summarize them — exactly the work a tiered system exists to avoid repeating every session — and consuming
it, since a long orientation dump sits in context for the rest of the session and every later turn re-pays
for it whether or not it's still relevant. Treating "give the agent everything, just in case" as safe is the
more expensive failure mode; it just fails slowly, one session at a time.

The fix isn't a smarter summarizer. It's keeping the always-loaded kernel small, keeping everything else a
named pointer away, and making retrieval itself cost-aware — bounded hit counts, bounded byte budgets,
bounded per-call output — so going deeper is a deliberate, visible choice.

## The tiered model

| Tier | What lives there | How an agent reaches it |
|---|---|---|
| Kernel | Harness identity, hard guards, where the board lives | Loaded automatically at session start |
| Linked detail | Board rows, documents, decisions, prior work | One bounded call — a lens, a profile query, a pointer read |
| Recall-only depth | Full history, superseded material, forensic sweeps | Explicit, usually manual, request |

The kernel is not a place for status. Whether a job is running, blocked, or done doesn't belong in a static
file, which isn't re-read every turn and so goes stale the moment reality moves. Live status is always
*derived*, computed fresh from the database at read time; the kernel only holds what's stable — identity,
hard rules, pointers to where live state can be queried. The middle tier, where the machinery below lives,
is the subject of the rest of this document.

## Boot: the capsule, not a pasted transcript

`board_context.build_capsule` is the "what's going on right now" read a session makes once at the start, in
place of asking a person or a prior transcript to catch it up. It answers five narrow questions: how many
work items are open (`health_summary`); which hold an active claim right now (`running`, capped to ten);
which were deliberately paused with a stated reason and resume condition, not merely dropped
(`resume_intents`); the last handful of recorded decisions (`recent_decisions`); and a fingerprint of which
policy rules are in force, so a session can tell if the rules changed since it last ran (`policy_epoch`).

Each read is wrapped in its own `try`/`except`; a failure drops just that section into an `omitted` list
rather than failing the whole capsule. The payload is then trimmed by `_trim_capsule_to_budget`, dropping
the least important list one item at a time until the JSON fits under `MAX_CAPSULE_BYTES` (6,144 bytes) — a
hard ceiling enforced by the producer, the idea that recurs throughout this document. The capsule ends with
a `pointers` block — fixed paths for the roadmap, the review-tier rules, and the recipe for going deeper on
one item (`focus <WORK_ID>`, below) — so a session never has to guess where to look next.

Fresh generic-project command:

```bash
python -m coordharness.coord.board_context capsule
```

## Bounded board reads: four lenses instead of one dump

Dumping the whole board is fine at ten rows. At a few hundred it's a wall of JSON that buries the three rows
a session needs under the ninety-seven it doesn't, at the same cost whether the question was narrow or
broad. `board_context.py` replaces that with four lenses, each shaped around a different question, each
with its own row cap and `expansion` pointer to the next lens:

- **`digest`** — "what does the board look like overall?" Buckets rows into running, blocked, recently
  changed, and assigned-to-actor (12 per bucket), plus cheap rollup counts by status and module.
- **`focus <work_id>`** — "tell me about this item and what's near it." Returns the row in full plus its
  parent, children, siblings, and a scored *related* set — same-parent scores above same-module, which
  scores above a shared token in an artifact path. Built for "I'm about to work on X," not "show me the board."
- **`search <query>`** — a scored keyword search over up to 100 candidates, diversified so one busy module
  can't crowd out the results.
- **`skeleton`** — a thin per-row summary — id, status, module, priority, nothing else — for a whole status
  class, capped at 100 rows.

Focused generic-project commands:

```bash
python -m coordharness.coord.board_context focus PAY-CDX-REFUND-PATH
python -m coordharness.coord.board_context search "idempotency key"
```

All four share a compaction step, `compact_row`, which strips a row to a fixed field set and only adds
titles, notes, and artifact pointers when a lens asks for the fuller view (`focus` does; `search` and
`skeleton` don't). Every response ends with an `expansion` dict of next commands, so narrowing scope is
never a dead end.

## The federated context query: one call, several sources, a named budget

The board is one source of context, not the only one — there are also structured facts, a full-text index
over project documentation, artifact manifests, and notes from earlier sessions. `context_federator.py` lets
a session ask one question and get a merged, deduplicated, budgeted answer across all of them, instead of
querying several stores separately and reconciling the results by hand.

A `ContextFederator` holds a list of providers — board, facts, full-text knowledge, artifact manifests,
accepted-memory notes, memory proposals, and optionally closed-out board history — each implementing one
`search(query, work_id, limit)` method. The federator calls every provider, catches any single provider's
exception without failing the whole query, deduplicates hits pointing at the same source, orders them, then
applies two caps: a maximum hit count and a maximum total byte size, trimming further hit by hit if the
count cap alone isn't enough.

What makes this more than "search with a big limit" is that the budget is a **named profile**, picked once
per use case rather than a parameter every caller has to remember to set correctly:

| Profile | Purpose | Max hits | Bytes |
|---|---|---|---|
| `orient` | Light default boot orientation | 6 | 5,000 |
| `brief` | Startup / chat-turn orientation | 8 | 6,000 |
| `work` | Focused current-work retrieval (the default) | 18 | 20,000 |
| `edit-prep` | Implementation context before editing a file | 20 | 24,000 |
| `impact` | Blast-radius check before a broad edit | 22 | 26,000 |
| `code` | Code/symbol-oriented context | 16 | 24,000 |
| `docs` | Documentation and prior-recommendation retrieval | 24 | 28,000 |
| `deep` | Operator-approved broad cross-domain recall | 28 | 32,000 |
| `forensic` | Manual broad recall for audits | 40 | 40,000 |

`forensic` is `manual_only` — building its config requires an explicit `allow_manual=True`, so a query can't
accidentally trigger a forensic-depth sweep. A profile also changes *which* sources get consulted: `docs`
and `deep` turn on `include_board_history`, pulling in closed-out items smaller profiles skip.

```python
from coordharness.knowledge.context_federator import compile_context_pack

pack = compile_context_pack("idempotency key replay", work_id="PAY-CLA-IDEMPOTENCY-REPLAY", profile="work")
```

The result is organized into named sections (`current_work`, `facts`, `knowledge`, `artifact_manifests`,
...), trimmed section by section from the back until it fits the profile's byte budget — same idea as the
capsule. Every hit is a pointer plus a short snippet, never a full document body; a caller wanting one hit's
full text calls `read_context_pointer` on that pointer, with its own separate cap (12,000 bytes, hard-capped
at 40,000). Expanding one named thing is explicit; expanding everything a search touched is not something a
single call does by accident.

## Full-text indexed facts and knowledge: a question costs a query, not a file read

Two more stores exist so "do we already know this" doesn't require opening a file. **`facts.py`** is a
small structured ledger. Each `Fact` is a statement, a value, a status
(`live`/`superseded`/`corrected`/`parked`/`closed`), an evidence pointer, and, where relevant, a link to the
fact it supersedes. `current_value(statement, module=...)` is one indexed lookup that resolves conflicts
between live-status rows through an explicit `resolve_fact_conflict` step, rather than silently picking
whichever is most recent. `already_decided(query)` runs the same search scoped to closed/parked/corrected
facts — "has this been settled" is a different question from "what's true now," with its own entry point.

**`kfts.py`** ("knowledge full-text search") indexes project documentation into a SQLite `fts5` virtual
table, one row per document *section* rather than per file, so a hit points at a specific heading, not
"somewhere in this file." A query is normalized (`camelCase`/`snake_case` split into words, punctuation
collapsed), expanded through a small alias table, and turned into an FTS `OR` query; results are ranked by a
heuristic favoring title/heading matches over body matches and penalizing sources already known to be stale.

Both stores live in `.coordharness/knowledge.db`, and neither is created for you. `coord board`
bootstraps `coord.db` only; no MCP read builds the knowledge database, so on a fresh checkout
`facts_lookup` fails closed with `FactStoreUnavailable` while `facts_query` and
`knowledge_index_status` answer by reporting the store as absent. Indexing and accepted-memory
bootstrap are library workflows you run deliberately — that is the Preview qualifier on this row in
[`feature-status.json`](feature-status.json).

Without an index like this, "has anyone already decided this" costs roughly what reading the relevant files
costs, every time it comes up. With it, the question costs one indexed query no matter how large the
document set grows — growth shows up as index size, never as per-query cost. `kfts.read_note` extends the
same discipline to a *found* result: given a pointer, it returns a byte-bounded window of that section, not
the whole file. Finding something and reading all of it are two different calls, on purpose.

## Output budgets: a floor under every tool call

Everything above bounds one *kind* of read. `output_budget.py` is the backstop underneath all of them — a
generic cap on any text a policy-governed call is about to return. If the encoded text fits under
`inline_limit` (12,000 bytes by default), it passes through untouched. If it doesn't, the inline reply is
clipped, and — if the caller supplied an artifact directory — the *full* text is written to disk under a
filename derived from its own SHA-256, with the response carrying a path to that file. Nothing is silently
dropped; what's over budget is still on disk, named, and reachable, gated by one environment flag
(`COORD_OUTPUT_BUDGET`, on by default).

This is deliberately the least clever piece of the architecture. Every other mechanism here makes a
*judgment* about what's relevant and returns less because of it; `output_budget` makes no judgment — it's a
hard ceiling that catches whatever those judgments got wrong, or whatever an unbounded caller forgot to
bound itself. It runs as one of seven checks the policy pipeline applies on every lifecycle write, so a
runaway write can't blow past budget unnoticed.

## Capsules, not forks: why a fresh agent beats a long one

The tiered model explains what a session reads. This last piece is about *which* session reads it.

The instinct, once a conversation has built up useful context, is to keep extending it — fork it, spawn a
sub-task inside the same long-running thread, so new work inherits everything old work already established.
That's usually backwards for cost: cost scales with the number of calls made against a context **times the
median size of that context**, not the call count alone. Every call against a long conversation re-reads the
whole accumulated history to produce its next token, relevant or not — ten calls against a small, fresh
capsule cost a fraction of ten calls against a fork of a long-running session, even though both are "ten
calls."

The alternative: spawn a small, fresh agent with a scoped brief — a capsule, a `focus` result, one
profile-bounded context pack — do the bounded piece of work, and let its context end when the work does.
The next piece of work gets its own fresh capsule, not a continued fork of the last one's transcript.
Concretely: boot from the capsule and the relevant lens rather than a conversation summary; write large
results to disk and return a pointer rather than a payload riding the reply, exactly what
`apply_output_budget`'s artifact fallback does mechanically; and don't poll a long-running task from inside
the context that's waiting on it — the wait loop's own state doesn't need to sit alongside whatever
expensive session state prompted the check.

None of this is enforced the way the byte budgets are — it's a design principle the primitives above make
cheap to follow. A pointer-heavy capsule is easy to hand to a fresh agent; a context pack bounded to a named
profile is easy to pass as one argument to a spawned task. The tiering and budgeting exist so starting fresh
is never more expensive than continuing to scroll — the condition under which agents actually choose to.
