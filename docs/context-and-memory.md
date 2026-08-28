# Context and memory

Two documents already exist next door and this one does not repeat them.
[`context-architecture.md`](context-architecture.md) argues *why* reads are bounded and what the
budgets are; [`graph-and-context.md`](graph-and-context.md) describes the *shape* the bounded reads
traverse. This chapter answers the question underneath both: **what actually remembers anything
here, what each store is for, what leaves the machine, and what is deliberately never written
down.**

> **Status.** The stores, budgets and read surfaces described here are implemented and tested in
> this repository. The board-backed capsule and lens commands carry the same caveat
> [`context-architecture.md`](context-architecture.md) states at its head: no fresh standalone
> deployment profile accepts them end-to-end yet, so the command lines below are implementation
> references rather than a supported fresh-project workflow. Where something is unimplemented, this
> chapter says so in the sentence that mentions it rather than in a footnote.

The harness keeps memory in three places, and they are not interchangeable:

| Store | File | Holds | Lifetime |
|---|---|---|---|
| Coordination database | `coord.db` (`config.coord_db_path`) | work rows, sessions, claims, runs, events, artifacts, display titles | the project's whole history; nothing is deleted on completion |
| Knowledge store | `knowledge.db` (`config.knowledge_db_path`, `COORD_KNOWLEDGE_DB`) | the fact ledger, the full-text index, the memory-proposal queue | rebuildable — the index is derived, the facts and proposals are not |
| Accepted-memory store | a generation directory on disk (`accepted_memory_r4`) | content-addressed accepted notes, a compiled boot kernel, publish receipts | append-only; generations are immutable once built |

Everything else an agent "knows" is transient. There is no transcript store, no conversation
archive, no vector database, and no per-agent scratch memory. A session that ends leaves behind
rows, events, artifacts and possibly a memory proposal — nothing else survives it.

---

## 1. What the coordination database remembers

The base schema is one file, `src/coordharness/coord/schema.sql`, applied by
`coord/create_schema.py`; two additive migrations (`coord/migrations/002_exact_authority.sql` and
`003_provenance_causal_trace.sql`) are applied on top of it by `bootstrap.py`.
[`schema.md`](schema.md) is the column-level reference; what follows is the *purpose* of the stores
work actually lands in, which is the part that decides where a given piece of context belongs. Two
further tables exist and are not listed below because no agent writes to them directly:
`schema_migrations` records which migrations were applied, and `request_consumption` tracks whether
a request event has been consumed by its recipient lane.

| Table | One row is | Written by | What it is for |
|---|---|---|---|
| `work_items` | a unit of assignable work | creation verbs, `handoff`, lifecycle writes | the durable identity of the work: `title`, `parent_id`, `intent_state`, `done_signal`, `acceptance_json`, `next_step`, `resume_when` |
| `agent_sessions` | one live agent process or chat | `cli._register_identity_session` (and the MCP equivalent) on any lifecycle verb | who is present, under what lease, rolled up to a `parent_session_id` for subagents |
| `claims` | one lease held against one work row | `coord_db.claim_work`, `heartbeat_claim`, `release_claim`, `complete_claim` | exclusive custody, and the **current** step (`claims.step`) |
| `runs` | a background job or fan-out spawned by a session | the job launcher (see [`jobs-and-runs.md`](jobs-and-runs.md)) | process identity and state, never process output |
| `events` | an append-only occurrence: a note, a decision, an audit request, a verdict, a closeout | `coord_db.post_event` and its typed wrappers (`post_decision_event`, `post_audit_verdict`, the closeout writer) | the narrative record — the only place prose lives in `coord.db` |
| `artifacts` | a completion proof | `coord_db.complete_claim`, and also `store_artifact` and `upsert_projection_work_if_changed` | binds a finished row to a path and its `sha256` |
| `inbox_cursors` | one recipient's read position in `events` | inbox reads | so "what's new for me" is a bounded query, not a re-scan |
| `display_titles` | a short human label for a row | the display-title writer | operator surfaces, not agent logic |

### Rows, steps and notes are three different kinds of memory

They are constantly confused, and the distinction is the whole reason the board reads well:

- **A row remembers the commitment.** `work_items.done_signal` and `acceptance_json` are what the
  work promised to produce. `complete_claim` refuses to close a claim whose `artifact_path` does not
  match the declared `done_signal`, and refuses again if the proof file is not actually there — both
  observed directly against a seeded board (§ *Commands run*, below). A row cannot remember being
  finished without something on disk agreeing.
- **A step remembers the present.** `claims.step` is set by `claim_work(step=...)` and updated by
  `heartbeat_claim(step=...)`. It is overwritten, not appended — it is a *current* state, and there
  is no step history.
- **`next_step` / `resume_when` remember the future.** They live on `work_items`, not on the claim,
  precisely because they must outlive the lease: they are what `park` and `block` write so a
  successor knows what the intent was. This is why the boot capsule surfaces `resume_intents` as its
  own section (`board_context._capsule_resume_intents`).
- **A note remembers what someone said.** `post_event(kind="note", ...)` fills `title` and `body`.
  Notes are the one genuinely freeform field in the coordination store, which is exactly why the
  public surfaces below withhold them.

### What the lifecycle does *not* append to `events`

This is load-bearing and easy to get wrong. `claim_work` and `heartbeat_claim` mutate the `claims`
table and write **no event row at all**; the CLI wraps them in a policy check
(`cli._run_lifecycle_policy` → [`policy-pipeline.md`](policy-pipeline.md)) and emits a reply, but
nothing lands in `events`. `release_claim` behaves the same way. Verified against a seeded demo
board: after a `claim_work` plus a `heartbeat_claim` on `UI-104`, the events table still held only
the five seeded `note` rows and `UI-104` had none of its own; the first row attributed to `UI-104`
appeared only after an explicit `post_event`, and a subsequent `release_claim` added no second one.

The consequence matters for anything reading `events` as a history: **the event log is a record of
what was *posted*, not a transition log of every lifecycle move.** A row that was claimed, worked
and released without anyone posting a note has no events. Absence of events is not absence of
activity, and no surface built on `events` may imply otherwise.

---

## 2. `ContextV1` — the structural half of the public board

`board/snapshot.py::build_context` (schema constant `CONTEXT_SCHEMA = "ContextV1"`) publishes, for
every board row, the shape of the work around it:

| Field | Meaning |
|---|---|
| `id` | the work id |
| `parent`, `children` | the hard parent/child edges from `work_items.parent_id` |
| `depends_on`, `dependents` | the dependency edge, resolved in both directions |
| `siblings` | rows sharing this row's parent, minus itself |
| `done_signal` | the declared completion artifact path |
| `artifact_recorded` | whether an `artifacts` row exists for this id — a boolean, never the artifact |
| `blocked_reason_class`, `resume_when`, `next_step` | the structured intent fields, which are enumerated or short |

It is deliberately separate from `NativeSnapshotV1`, which the native clients decode as a sealed
contract: widening the snapshot to carry navigation would version a schema for the sake of a web
view. And it is deliberately *structural*. The board is read-only and unauthenticated, so the fields
it adds are relationships, not content. `tests/test_board.py::test_context_document_carries_structure_and_no_prose`
asserts both halves — that the relations navigation needs are present, and
that `note`, `note_text`, `why_text`, `events`, `decisions` and `knowledge` are absent from every
item. Without the second half the endpoint could grow a leak and still pass.

Served at `GET /api/v1/context` by the route table in `board/server.py::do_GET`. There is no CLI and
no MCP tool for it; see the parity matrix in the appendix.

---

## 3. `TimelineV1` — occurrence without prose

The wire contract, which the producer and the front end were built against independently:

```json
{
  "schema_version": "TimelineV1",
  "generated_at": "<iso8601>",
  "source": "coord.db",
  "items": [
    {"id": "<work_id>", "events": [{"at": "<iso8601>", "kind": "<event kind>", "actor": "<lane>"}]}
  ]
}
```

Events are sorted ascending by time within an item, and items are sorted by id, so two reads of an
unchanged board serialise identically apart from `generated_at`.

**The redaction is the point.** The `events` table also holds `title`, `body`, `refs_json`,
`payload_json`, `session_id`, `to_selector`, `severity`, `verdict` and `trust`. None of those may
appear in the response in any form, under any key name, at any depth. `build_timeline` enforces this
at the query rather than at the serialiser: it runs

```sql
SELECT work_id,ts,kind,actor FROM events
 WHERE work_id IS NOT NULL AND TRIM(work_id) <> ''
 ORDER BY ts, event_id
```

— four columns fetched, three carried. Selecting and then dropping the prose columns would leave a
later edit one line away from a leak; never fetching them means the leak cannot be written by
accident.

Three further behaviours worth stating because they are decisions, not accidents:

- **Events with no `work_id` are excluded, not pooled.** Estate-wide bookkeeping (lease sweeps and
  similar) posts without a work item. Inventing a placeholder bucket would imply a row the board does
  not have.
- **Ordering is done in SQL by `(ts, event_id)`**, because the seeded board writes several events
  inside one transaction and they share a timestamp; without the tiebreak the order would be whatever
  the scan yielded.
- **The read does not touch the database it reads.** `build_timeline` goes through the same
  materialised-copy connection as `build_snapshot` and `build_graph`, so it cannot leave a `-wal`
  file beside a database someone else owns.

Tests: `tests/test_board.py::test_timeline_document_carries_occurrence_and_redacts_prose` plants a
sentinel string in the `title` *and* `body` of a real event on a row that is in the document, then
asserts the sentinel appears nowhere in the serialised JSON — which catches a leak regardless of what
key it arrives under — and separately asserts the withheld column names are absent, so a future edit
that adds one is caught even before it carries a sentinel value.
`tests/test_board.py::test_timeline_reads_without_writing_beside_the_source` asserts the read leaves
every mtime in the directory unchanged.

Served at `GET /api/v1/timeline` by the same route table in `board/server.py::do_GET` that answers
`/api/v1/snapshot`, `/api/v1/graph` and `/api/v1/context`, through the server's cached
`timeline()` accessor.

And the honest limit, carried forward from §1: because `claim_work` and `heartbeat_claim` write no
event rows, a `TimelineV1` item is a history of *posted* events for that row. It is not a claim
ledger. A row can be absent from `items` entirely and still have been worked.

---

## 4. Orientation is implemented twice, on purpose and without a shared core

A reader following [`mcp-integration.md`](mcp-integration.md)'s start-of-session sequence and a
reader following [`context-architecture.md`](context-architecture.md)'s capsule recipe are using two
independently written code paths that answer a similar question. This has not been stated anywhere
before, and it explains why their outputs differ.

| | CLI / Python capsule | MCP `preflight` |
|---|---|---|
| Entry point | `coord/board_context.py::build_capsule` | `coord/mcp_coord_server.py::_tool_preflight` |
| Reads | `coord.db` directly (projection health summary, visible rows, decisions) | `roadmap_backlog.json` plus `exact_query_core.load_query_snapshot` |
| Sections | `health_summary`, `running`, `resume_intents`, `recent_decisions`, `policy_epoch`, `resource_mode`, `pointers` | its own orientation payload (`assigned_open_ids`, `running_ids`, `blocked_ids`, `next_work_ids`, `capability_handshake`, `policy_epoch`) |
| Failure mode | each section is wrapped; a failure lands in `omitted` and the rest still returns | its own handling |
| Budget | `_trim_capsule_to_budget`, `MAX_CAPSULE_BYTES = 6_144` | the MCP output budget |
| Calls the other? | no | no — `_tool_preflight` does not call `build_capsule` |

Neither is wrong; they were written for different transports. But they are not one function exposed
two ways, and a change to the capsule does not change what an MCP client sees at boot.

---

## 5. Board lenses, and where MCP has no equivalent

`board_context.py`'s CLI registers ten subcommands — `digest`, `focus`, `search`, `history`,
`skeleton`, `changes`, `curate`, `capsule`, `export`, `successor` — of which the four lenses documented in
[`context-architecture.md`](context-architecture.md) are the ones a session reaches for. What that
document does not say is that **none of them is exposed over MCP.**

| Lens | CLI | MCP | Nearest MCP tool, and how it differs |
|---|---|---|---|
| `digest` | `python -m coordharness.coord.board_context digest` | none | `board` — a filtered row list, no bucketing or rollups |
| `focus <id>` | `... board_context focus PAY-101` | none | `work_context` — one row plus parent plus recent events; no scored sibling/related set |
| `search <q>` | `... board_context search "idempotency key"` | none | `knowledge_search` — a federated query whose providers happen to include the board, not a board-row keyword scan |
| `skeleton` | `... board_context skeleton` | none | none |

This is the opposite asymmetry from the one [`mcp-integration.md`](mcp-integration.md) already
acknowledges (MCP being richer than the small CLI). Here the CLI is richer, and an MCP client has no
path to these shapes short of shelling out. `mcp_coord_server.py` imports exactly one symbol from
`board_context` — `compact_row`, used inside a different tool for a different purpose.

---

## 6. The knowledge subsystem, module by module

The fact ledger, the FTS5 index and the proposal queue are three tables in **one SQLite file**,
`knowledge.db` (`kfts.DEFAULT_INDEX_DB`, resolved by `config.knowledge_db_path()`) — not three
databases. The other two storage-bearing modules in `src/coordharness/knowledge/` are outside it:
`accepted_memory_r4.py` writes a directory tree, and `context_federator.py` reads `coord.db` as well
as `knowledge.db`.

| Module | Purpose | Entry points | Storage | Tests | Maturity |
|---|---|---|---|---|---|
| `facts.py` | bitemporal fact ledger — statement, value, status, evidence pointer, supersession chain | `current_value`, `search_facts`, `query_facts`, `already_decided`, `get_fact`, `supersession_chain`, `fact_lifecycle`, `resolve_fact_evidence`, `stats`, `upsert_fact`, `supersede` | `facts` table | **no test file of its own**; `query_facts`, `stats` and (through `query_facts(text=...)`) `search_facts` are covered indirectly by `tests/knowledge/test_knowledge_read_surface.py`, while `current_value`, `supersession_chain`, `already_decided`, `resolve_fact_evidence` and `resolve_fact_conflict` are named by no test in the tree | shipped, partly untested |
| `kfts.py` | full-text index over project markdown, one FTS5 row per heading section, so hits carry `line_start`/`line_end` | `search`, `read_note`, `index_stats`, `rebuild_index`, `find_similar_memory` | `knowledge_fts` virtual table; read connections use `mode=ro` + `PRAGMA query_only=ON` | `tests/knowledge/test_kfts_v2_runtime.py`, `test_memory_dedup_gate.py` | shipped |
| `context_federator.py` | fan one query across every provider, dedupe, apply a named byte/hit budget; also the general pointer resolver | `compile_context_pack`, `context_query_profiles`, `config_for_context_profile`, `default_context_providers`, `read_context_pointer`, `ContextFederator` | reads `coord.db` (board providers) **and** `knowledge.db` (facts/kfts/proposals) and the accepted-memory tree | `tests/knowledge/test_archive_pointer_aliases.py` (the archive-alias branch of `read_context_pointer`), `tests/knowledge/test_accepted_memory_r4.py` (one `ContextFederator` packet), and the pointer/config helpers in `tests/test_neutral_config_files.py` and `tests/test_neutral_runtime_defaults.py`; `compile_context_pack` and `context_query_profiles` have no direct unit test | shipped, thinly tested |
| `memory_proposals.py` | human-in-the-loop queue for candidate memory | `propose_memory`, `review_proposal`, `promote_proposal_to_fact`, `list_proposals`, `get_proposal` | `memory_proposals` table | `tests/test_public_generalization.py` (`propose_memory` + the self-review refusal in `review_proposal`) and `tests/knowledge/test_knowledge_read_surface.py` (`list_proposals` / `get_proposal` through the read surface) | shipped |
| `proposal_producer.py` | offers explicitly marked durable facts to the review queue after the session-closeout fence | `produce_session_memory_proposals`, `produce_memory_candidate` | reads typed decision events from `coord.db`; writes proposals to the bound `knowledge.db` | `tests/knowledge/test_memory_proposal_producer.py` covers eligibility, scope, lineage, dedup/rate limits, custom database binding, blocked/empty closeout, and failure replay | shipped, opt-in |
| `accepted_memory_r4.py` | build immutable generations of accepted notes and compile the boot kernel | `atomize_markdown`, `build_generation`, `verify_generation`, `publish_current`, `rollback_current`, `open_current_generation`, `client_kernel_bytes` | a generation directory, content-addressed `objects/<sha256>.md` | `tests/knowledge/test_accepted_memory_r4.py` — the best-covered module here | shipped |
| `query_aliases.py` | the alias table (`QueryAliasGroup`) that query expansion runs through | data + dataclass only | no direct test | shipped, internal |
| `query_scoring.py` | shared pure tokenisation/scoring primitives used by **both** `kfts.py` and `facts.py` | `normalize_search_text`, `query_phrases`, `query_tokens`, `field_terms`, `term_matches` | none — no I/O | `tests/knowledge/test_query_scoring.py` | shipped |
| `read_surface.py` | the single read-only implementation behind the MCP knowledge read tools | `facts_query`, `knowledge_index_status`, `memory_proposals_list`, `memory_proposals_get` | reads `knowledge.db` only | `tests/knowledge/test_knowledge_read_surface.py` — 31 test functions, 49 cases after parametrisation, including one that fingerprints the store to prove a read wrote nothing | new, well covered |

Three details in that table are worth pulling out because they change how you use the modules.

**The federated provider list is not knowledge-only.** `default_context_providers` always builds
`BoardContextProvider` and `ArtifactManifestProvider` — both of which read `coord.db` — alongside the
knowledge providers, and appends `BoardHistoryProvider` for the three `include_board_history`
profiles (`docs`, `deep`, `forensic`). A caller who wants a knowledge-only answer must name the
sources explicitly through the MCP `knowledge_search` tool's `sources` parameter. Note that the
allowlist it validates against, `_KNOWLEDGE_PROVIDER_NAMES` in `mcp_coord_server.py`, itself contains
`board` and `board_history`: naming sources is how a caller *excludes* the board providers, not
something that happens by default.

**The proposal producer is opt-in and behind the closeout fence.** A typed `decision` becomes
eligible only when its caller sets `memory_candidate=true`; the default is false, so normative
decisions, notes, heartbeats, summaries and empty sessions do not become facts merely because a
session ended. After every formal closeout blocker has passed, `session_closeout` offers the
current, non-superseded marked facts to the knowledge store bound to that MCP server. Work-scoped
facts stay `work`; global facts stay `global`; module and initiative scopes map conservatively to
`project` while their exact authoritative scope remains in provenance. The existing semantic
deduplication gate and five-per-source-thread rate cap remain authoritative. A failed write leaves
a typed receipt in `coord.db`; a later closeout replays it, but first recomputes current decision
heads so a superseded fact can only resolve stale, never reappear. The MCP proposal-list/get tools
remain read-only and review/promotion remain separate human actions.

**The fact ledger's own judgement calls are the least-tested code in this chapter.**
`current_value` raises `UnresolvedFactConflict` when candidate live rows tie under every
deterministic rule in `resolve_fact_conflict`; nothing exercises that path. Treat a conflict-time
change to `facts.py` as unguarded.

**Read-only is a measured property in `read_surface.py`, not an assumption.** `facts.query_facts`
and `kfts.index_stats` have genuine read-only connection paths (`facts._read_conn`, `kfts._conn_ro`).
`memory_proposals.list_proposals` and `get_proposal` do **not**: both go through
`memory_proposals._conn`, which does `mkdir(parents=True)` and `executescript(SCHEMA_SQL)` — on an
existing store that is byte-neutral, but against a missing file or a file without that table it
would *create* what it claims to be reading. So the proposal readers probe first, through
`_probe_store` (opened `mode=ro` with `PRAGMA query_only=ON`), and call the underlying accessor only
when the `memory_proposals` table already exists; otherwise they return an empty result whose `store`
descriptor reports `present` / `readable` / `table_present`. Removing that guard reintroduces a write
on a read path.

### The MCP knowledge read tools

Four read tools were added to `_MCP_TOOL_NAMES` in `coord/mcp_coord_server.py`, each a thin wrapper
(`_tool_facts_query`, `_tool_knowledge_index_status`, `_tool_memory_proposals_list`,
`_tool_memory_proposals_get`) over the matching `read_surface` function:

| Tool | Answers | Bounds |
|---|---|---|
| `facts_query` | "all live facts in module X" — exact `module`/`status` filters, optional ranked `text` | `DEFAULT_FACTS_LIMIT = 50`, `MAX_FACTS_LIMIT = 200`; reports `match_mode` so a caller knows whether rows came from a filter or a ranked match |
| `knowledge_index_status` | "is the full-text index fresh enough to trust a search?" | no query; returns `kfts.index_stats` with its staleness reasons |
| `memory_proposals_list` | "what is waiting for human review?" | `DEFAULT_PROPOSALS_LIMIT = 25`, `MAX_PROPOSALS_LIMIT = 100` |
| `memory_proposals_get` | one proposal by id | single row |

They complement rather than replace the older `facts_lookup` (fuzzy, ranked) and `knowledge_search`
(federated). The implementation sits in `knowledge/read_surface.py` rather than in the server so a
future CLI wrapper can return byte-identical payloads — parity by construction rather than by
convention. No such CLI exists yet; `test_cli_twin_names_are_snake_case_siblings` only fixes the
naming convention a twin would use. Every reply carries a `store` descriptor — file path, table, and whether the file is
present — so a caller never has to guess which store answered.

---

## 7. The federated query and its named profiles

`context_query_profiles()` in `context_federator.py` defines nine budgets, picked once per use case
rather than re-argued per call:

| Profile | Purpose | Max hits | Max bytes | Closed-out board history | Manual only |
|---|---|---|---|---|---|
| `orient` | light boot orientation | 6 | 5,000 | no | no |
| `brief` | startup / chat-turn orientation | 8 | 6,000 | no | no |
| `code` | code/symbol-oriented context | 16 | 24,000 | no | no |
| `work` | focused current-work retrieval (the default) | 18 | 20,000 | no | no |
| `edit-prep` | implementation context before editing a file | 20 | 24,000 | no | no |
| `impact` | blast-radius check before a broad edit | 22 | 26,000 | no | no |
| `docs` | documentation and prior-recommendation retrieval | 24 | 28,000 | **yes** | no |
| `deep` | operator-approved broad cross-domain recall | 28 | 32,000 | **yes** | no |
| `forensic` | manual broad recall for audits | 40 | 40,000 | **yes** | **yes** |

Printed directly from `context_query_profiles()`; see § *Commands run*. `forensic` is `manual_only`:
building its config requires an explicit `allow_manual=True`, so the widest profile cannot fire by
accident on a routine call. The last three switch on `include_board_history`, which appends
`BoardHistoryProvider` in `default_context_providers` and pulls in closed-out rows the smaller
profiles skip — a detail [`context-architecture.md`](context-architecture.md) states for `docs` and
`deep` only.

Underneath every provider's ranking sit `query_scoring.py` (normalisation and term matching) and
`query_aliases.py` (expansion). They are shared primitives, not `kfts` internals — `facts.py` imports
the same functions — which is why a query behaves consistently whether it lands on prose or on the
fact ledger.

Finding and reading stay two calls. Every hit is a pointer plus a snippet; the full text of one hit
comes from `read_context_pointer` (`DEFAULT_POINTER_READ_BYTES = 12_000`) or `kfts.read_note`.

---

## 8. Output budgets

`coord/output_budget.py` is the backstop under all of the above: text under `INLINE_OUTPUT_LIMIT`
(12,000 bytes) passes through, and anything larger is clipped inline and reported as
`truncated: True` with the true byte count, so nothing is dropped *silently*. The full text is
written to a sidecar file — named `<prefix>-<first 16 hex of its SHA-256>.txt`, with the reply
carrying the path — only when the caller supplies an `artifact_dir`; the parameter defaults to
`None`, and both in-tree callers (the MCP audit/verdict writer and `modeld_lite`) do supply one.
It is gated by one environment flag, `ENV_FLAG = "COORD_OUTPUT_BUDGET"`, on by default, and it runs
as one of the checks the policy pipeline applies on lifecycle writes, at `warn` severity — the check
inventory lives in [`policy-pipeline.md`](policy-pipeline.md). It has no dedicated test module of
its own; no test file in the tree names it.

---

## 9. The accepted-memory pipeline, and one correction to the record

Proposal → adjudication → acceptance → activation, as described in
[`graph-and-context.md`](graph-and-context.md) — with two corrections, the second of which matters a
great deal: that document describes an operational safety ritual that does not exist in this code.

**Proposal.** `memory_proposals.propose_memory` keys rows by a content hash so the same proposal
twice increments `seen_count` rather than duplicating; a proposal must carry an `evidence_pointer` or
a `provenance` block (`"memory proposals require evidence_pointer or provenance"`); and a per-session
cap of five proposals per 24 hours (`"per-session proposal cap (5/24h) reached for"`) keeps one noisy
session from flooding the queue.

**Adjudication.** `review_proposal` refuses when `reviewer` equals the proposal's `source_actor` — a
proposal does not adjudicate itself into memory. An accepted proposal can be promoted into the fact
ledger with `promote_proposal_to_fact`, carrying its evidence pointer forward.

**Acceptance.** `build_generation` reads each note with a stat-before/stat-after check that rejects a
file changed mid-read, hashes it into `objects/<sha256>.md`, and computes a `version_id` that hashes
the note's content together with its `previous_version_id`, so edit history is a hash chain.
`_compile_kernel` requires the twenty blocks `K00`–`K19` in order and rejects the result unless both
`within_15_kib` (`KERNEL_MAX_BYTES = 15 * 1024`) and `strictly_below_75_percent` hold.

> **Correction 1 — "compress" is the wrong word.** The transform `_compile_kernel` applies is
> `"retain exact K00-K19 semantic text; remove provenance citations compacted into manifest"` — in
> code, a regex that strips `⟦…⟧` citation markers from the selected region — and
> `strictly_below_75_percent` then compares the citation-stripped size against `selected_input_bytes`
> (`KERNEL_MAX_INPUT_FRACTION_NUMERATOR/DENOMINATOR = 3/4`). It is a size ratio over a text
> transform. No compression library is involved; a reader looking for a zlib call will not find one.

**Activation.** `publish_current` moves the `CURRENT` pointer under a compare-and-swap on
`expected_current` and writes a hash-identified receipt naming the old and new generation.
`client_kernel_bytes(store_root, client)` re-checks the size and ratio proof on read, so a kernel that
somehow got published out of budget still fails at the point of use. `rollback_current` restores a
**prior generation id**.

> **Correction 2 — there is no "roll back to absent" step, and no live-activation script.**
> [`graph-and-context.md`](graph-and-context.md) currently describes an activation that "deliberately
> rolls the pointer back to absent, verifies it's really gone, republishes," and its mermaid diagram
> names a node `rollback_to_absent`. No such function exists: `publish_current` requires a non-null
> `generation_id`, `rollback_current` is a thin wrapper that republishes a prior generation, and
> `grep -rn rollback_to_absent` over the repository matches only that document. The nearest real
> thing is one test —
> `tests/knowledge/test_accepted_memory_r4.py::test_atomic_publish_cas_rollback_and_dual_client_equality`
> — whose rollback target is the *first* generation, never absence. Treat the ritual as unwritten.

---

## 10. The storage boundary

Who reads what, who writes it, and whether it reaches an unauthenticated surface. "Public" here means
*served by the read-only board over HTTP*; the board binds loopback by default and has no auth, so
"public" is the honest word for anything it emits (see [`security-and-privacy.md`](security-and-privacy.md)).

| Store / field group | Read by | Written by | Public? |
|---|---|---|---|
| `work_items` structural fields (`work_id`, `parent_id`, `intent_state`, `module`, `priority`, `done_signal`) | every board surface, lenses, `ContextV1`, snapshot | lifecycle verbs, `handoff` | **yes** — `NativeSnapshotV1` and `ContextV1` |
| `work_items.title` / `display_titles` | board surfaces, native clients | creation verbs, display-title writer | **yes** — the snapshot carries `title` |
| `claims.step` | snapshot (`current_step`), lenses | `claim_work`, `heartbeat_claim` | **yes**, as the current step only; no step history exists |
| `claims` lease internals (`lease_token`, `expires_at`) | lifecycle verbs, reaper, liveness | claim verbs | no — liveness is published as derived status, not as a token |
| `events.kind` / `events.ts` / `events.actor` | inbox, `work_context`, `event_context`, `TimelineV1` | `post_event` and typed wrappers | **yes**, in `TimelineV1` — occurrence only |
| `events.title`, `body`, `refs_json`, `payload_json`, `session_id`, `to_selector`, `severity`, `verdict`, `trust` | inbox, `work_context`, `event_context`, MCP clients | `post_event` | **no** — never selected by `build_timeline`; never carried by `ContextV1` |
| `runs` metadata | job surfaces, `runs` tool | job launcher | partly — job rows appear in the snapshot; process output never does |
| `artifacts` (path, `sha256`) | `complete_claim`, closeout, audits | `complete_claim` | only as `ContextV1`'s `artifact_recorded` boolean |
| `knowledge.db` — `facts` | `facts_lookup`, `facts_query`, federated query | `upsert_fact`, `supersede`, `promote_proposal_to_fact` | **no** — no board route reads `knowledge.db` |
| `knowledge.db` — `knowledge_fts` | `knowledge_search`, `read_note`, `find_similar_memory` | `rebuild_index` only | no |
| `knowledge.db` — `memory_proposals` | `memory_proposals_list`/`_get`, federated query | `propose_memory`, `review_proposal` | no |
| accepted-memory generations | `open_current_generation`, `client_kernel_bytes`, the accepted-memory provider | `build_generation`, `publish_current`, `rollback_current` | no |
| `job_progress/*.json` sidecars | job surfaces, snapshot merge | the job launcher | partly — bounded metadata; see [`security-and-privacy.md`](security-and-privacy.md) |

The single rule that generates most of this table: **the board publishes structure and occurrence;
prose stays behind the MCP/CLI boundary, which is stdio and local.**

---

## 11. What is deliberately not remembered

Each of these is an absence someone will eventually propose filling. The reason is recorded here so
the proposal starts from the argument rather than from scratch.

- **Conversation transcripts.** Nothing stores an agent's messages. The claim in
  [`context-architecture.md`](context-architecture.md) — that a pasted transcript costs tokens twice —
  is only true if the harness declines to make transcripts the unit of continuity. State lives in rows,
  events and artifacts; a successor boots from a capsule, never from a predecessor's history.
- **Step history.** `claims.step` is overwritten on every heartbeat. Keeping every step would make the
  events table a progress log, and progress chatter is precisely what the coordination protocol asks
  agents to keep *out* of the record ([`agent-protocol.md`](agent-protocol.md)).
- **Process output.** `runs` keeps a process id and its start time, never stdout. Job output belongs in
  the job's artifact, which is hashed and referenced, not inlined.
- **Prose on the public board.** Covered above; the enforcement is at the SQL query, not the
  serialiser.
- **Any second machine's state.** `coord.db` is single-host. Multi-machine coordination would need real
  identity and tenant-scoped data, and that product does not exist here.
- **Evidence-pointer resolution in the fact ledger.** `upsert_fact` stores an `evidence_pointer`
  without resolving it, unlike the acceptance-repair path in `coord_db.py`, which hashes the file
  before allowing the write. This one is a gap rather than a decision, and
  [`graph-and-context.md`](graph-and-context.md) already names it as such — repeated here so it is not
  mistaken for a policy.
- **Anything a caller pastes into a freeform field.** The schema cannot stop a secret being typed into
  a note body or a sidecar `step`. That is a review responsibility, not a mechanism, and
  [`security-and-privacy.md`](security-and-privacy.md) is the reference.

---

## Appendix — capability parity across surfaces

Where each context/memory capability is reachable from. This is the asymmetry made explicit, so a
reader does not have to grep for it.

| Capability | Python API | CLI | MCP | Web board |
|---|---|---|---|---|
| Boot capsule | `board_context.build_capsule` | `python -m coordharness.coord.board_context capsule` | no (`preflight` is a separate implementation) | no |
| `digest` / `focus` / `search` / `skeleton` lenses | `board_context` | yes | **no** | no |
| Structural context (`ContextV1`) | `snapshot.build_context` | no | no | `GET /api/v1/context` |
| Event occurrence (`TimelineV1`) | `snapshot.build_timeline` | no | no | `GET /api/v1/timeline` |
| Board snapshot / graph | `snapshot.build_snapshot`, `build_graph` | serve via `coordharness.board.server` | `board` (row list) | `/api/v1/snapshot`, `/api/v1/graph` |
| Federated context pack | `context_federator.compile_context_pack` | no | `knowledge_search` (its own federator wiring) | no |
| Pointer read | `context_federator.read_context_pointer`, `kfts.read_note` | no | `read_note` | no |
| Fact search (fuzzy) | `facts.search_facts` | no | `facts_lookup` | no |
| Fact filter (structured) | `facts.query_facts` / `read_surface.facts_query` | no | `facts_query` | no |
| Index freshness | `kfts.index_stats` | no | `knowledge_index_status` | no |
| Memory proposals (read) | `memory_proposals.list_proposals` / `get_proposal` | no | `memory_proposals_list`, `memory_proposals_get` | no |
| Accepted-memory kernel read | `accepted_memory_r4.open_current_generation`, `client_kernel_bytes` | no | no | no |
| Index rebuild (write) | `kfts.rebuild_index` | no | no — no MCP write path into the index | no |

---

## Commands run while writing this chapter

Every behavioural claim above that is not a citation to a symbol was observed by running one of
these against a throwaway board seeded with `coordharness.demo.seed`.

```
$ PYTHONPATH=src python -m pytest -q tests/test_board.py -k "timeline or context"
3 passed, 11 deselected in 0.17s

$ PYTHONPATH=src python -m pytest -q tests/knowledge/test_knowledge_read_surface.py
49 passed in 0.90s

$ PYTHONPATH=src python -c "from coordharness.knowledge.context_federator import \
    context_query_profiles; ..."
brief 8 6000 history=False manual=False
orient 6 5000 history=False manual=False
work 18 20000 history=False manual=False
edit-prep 20 24000 history=False manual=False
impact 22 26000 history=False manual=False
docs 24 28000 history=True manual=False
deep 28 32000 history=True manual=False
code 16 24000 history=False manual=False
forensic 40 40000 history=True manual=True
```

Lifecycle probe (seeded demo board, throwaway copy):

```
kinds before:                  [('note', 5)]
work_id: UI-104  claim: clm-ada00cae4c8d
kinds after claim+heartbeat:   [('note', 5)]   # the five seeded notes; claim and heartbeat added none
UI-104 events:                 0
claims.step:                   still going     # overwritten by the heartbeat, not appended

complete_claim(artifact_path="docs/context-and-memory.md")
  -> ValueError: complete_claim artifact_path must match the controller-declared done_signal
     for claim 'clm-ada00cae4c8d'
complete_claim(artifact_path="docs/reports/ui-104.md")   # the declared done_signal
  -> ValueError: complete_claim artifact proof does not exist or is incomplete
     for claim 'clm-ada00cae4c8d': docs/reports/ui-104.md
```
