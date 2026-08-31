# Agent protocol

This is the operating manual for working through the board rather than around it.
The mechanisms in `coordharness` — leases, typed handoffs, write-set declarations —
only produce a trustworthy picture of who is doing what if every agent (and every
human directing an agent) follows the same small contract. This document is that
contract, plus the reasoning behind each rule.

The running example throughout is the seeded demo board: a small team porting a
payments service, worked by three actors (`claude`, `codex`, `local`) across
seven sessions — `claude:frontend`, `claude:platform`, `claude:cloud-a`,
`codex:backend`, `codex:review`, `codex:cloud-b`, and `local:gpu`. Run
`python -m coordharness.demo --db <path>` to get the same board on your own
machine and follow along.

## The problem this solves

A coordination board is only useful if its rows mean what they say. If an agent
can claim work it doesn't hold, or report `done` without producing anything, or
run five subagents that nobody ever notices, the board becomes a second UI that
periodically lies. `coordharness` closes each of those gaps at the database layer
— you cannot claim another actor's work without a typed handoff, and you cannot
complete a claim without a live owning claim and controller-declared proof (see tests in
`tests/test_lifecycle_smoke.py` that exist specifically to catch a regression
here). But database guards only catch violations; they don't produce good
behaviour on their own. The protocol below is what good behaviour looks like.

## The session contract

Every agent session — a chat, a long-running loop, a CI job — follows the same
four-step shape:

1. **Orient.** Before doing anything, ask the board what's assigned to you, what's
   already running under your session, and what's waiting on you. The `preflight`
   tool returns your open work, your currently-held claims, anything parked with a
   resume condition, and unread messages addressed to you — enough to resume
   correctly without re-reading history. Do this once per session, not once per
   task.
2. **Claim before substantive work.** A claim is a lease: `(work_id, session_id)`,
   with an expiry. The schema enforces at most one *held* claim per work item at a
   time — a unique index over `running`/`paused`/`blocked` claims, so a second
   session cannot take a row out from under a first one even mid-pause. Claim the
   row before you start reading code or writing files for it, not after. A row missing a
   `done_signal` or acceptance is refused by MCP `claim_work` and merely warned about by
   `coord claim`; `COORD_CLAIM_STRICT=1`/`=0` moves both surfaces to one behaviour (see
   [review-tiers.md](review-tiers.md#claim-readiness-and-why-the-two-surfaces-disagree-on-purpose)).
3. **Heartbeat only to renew the lease.** A heartbeat extends the claim's expiry.
   That's all it does. Call it when a claim is going to outlive its lease window
   and nothing else is due — not on every tool call, not as a running commentary.
4. **Close with `done`, `block`, or `park`.** `done` requires the claim you hold
   and an artifact that satisfies the work item's declared `done_signal` — a path
   that exists and, for Markdown proof, is tracked by Git's current index. `git add`
   is sufficient; a commit is not required. `block` and `park` both require a durable
   resume contract — a concrete `next_step` and a `resume_when` condition — not a
   vague "will get back to this."

## Progress lives in the verb, not the heartbeat

It's tempting to treat heartbeat as a chat channel — "still working on the
retry logic," "found the bug, fixing now." Resist this. A heartbeat call is a
lease renewal with an optional one-line `step` label; it is not the place for a
narrative, and nothing downstream reads a heartbeat history looking for
context. Concretely:

| Where progress belongs | What it captures |
|---|---|
| `claim(step=...)` | What you're about to do, at the moment you take the row |
| `heartbeat(step=...)` | A short label update if the immediate focus has shifted — still one line |
| `done(artifact=...)` | The finished thing, plus the artifact path that proves it |
| `block(next_step, resume_when)` | Exactly what's stuck and exactly what unblocks it |
| `park(next_step, resume_when)` | Exactly where you stopped and exactly when to pick it back up |

The reasoning is durability. A heartbeat stream is ephemeral chatter tied to a
lease that expires in an hour; the `next_step`/`resume_when` pair on a parked or
blocked row is a structured field a *different* agent — or the same agent three
weeks later — can query directly, without replaying a transcript. If you find
yourself putting a paragraph into a heartbeat call, that paragraph belongs in the
claim's step, the done artifact, or a park/block resume contract instead.

## One session identity, not one per prompt

A session ID is meant to persist across an agent's whole working stretch —
`claude:frontend`, `codex:backend` — not to be re-minted for every prompt or
every new chat window. Two things depend on this:

- **Lease continuity.** Claims are keyed to `(work_id, session_id)`. A resumed
  claim on a paused or blocked row is recognised by checking whether the
  resuming session shares an identity with the row's original holder. A fresh
  session ID for every prompt makes every resume look like a stranger asking for
  someone else's row.
- **Visibility.** `preflight` and the board projection group work by session.
  An orchestrator that mints `claude:task-1847`, `claude:task-1848`, ... for
  each new instruction produces a board with one abandoned-looking session per
  prompt, none of which anyone would think to check.

Pick one label per agent identity for the life of that agent's working thread —
scoped to what it actually does (`claude:frontend`, not `claude:whatever-the-
last-prompt-was`) — and keep using it. If the underlying runner exposes a stable
identifier (a CI job ID, a persistent session ID from the coding agent host),
prefer that over inventing a new label each time.

## Spawning a fleet is starting a job

If an agent fans out into several subagents to parallelise a piece of work, that
fan-out is a job like any other, and it needs a claim *before* it starts — not
after, not never. The reason is structural, not stylistic: subagents don't talk
to the board. They have no session, no claim, no lease. If the orchestrating
agent hasn't claimed the parent row, the entire fleet runs and finishes
invisibly — nothing on the board moved, no lease exists to expire, nobody
watching the board would know five agents were ever running.

`coordharness` makes this an enforced precondition rather than a convention.
Recording a subagent's outcome (`record_child_attempt` and its paired
`record_child_outcome`) requires an already-held claim; attempting it against
an unknown or non-live claim raises `UnclaimedFleetError` naming the rule
directly — record child attempts only under a live claim. There's a companion query,
`fleet_records_missing_model`, precisely for auditing fleets that got claimed
correctly but skipped recording which model ran each child — a fleet is worth
tracking as a fact (it ran) and an outcome (what happened), not just a claim
that stays open the whole time and closes with no detail.

In practice: claim the parent row, fan out, record each child's identity and
model as it starts, record its outcome as it finishes, then close the parent
row with `done` (with a real artifact) or `park`/`block` if the fleet didn't
finish the job.

## Handoffs between agents

Work sometimes needs to move from one kind of agent to another mid-task — a
planning agent hands execution to a different agent better suited to it, or a
background job needs a human-facing agent to review before it can continue.
A handoff is a typed operation, not a message: it names the target owner, a
task description, why the handoff is happening, and an acceptance condition the
receiving agent can check its own work against. It also carries an
idempotency key, so retrying a handoff that already landed replays the original
result instead of creating a duplicate.

Two guards matter here for anyone building on top of this:

- **You cannot claim work assigned to another actor by just calling claim.**
  Attempting it fails with an error pointing at the handoff path — claiming
  across an actor boundary has to go through the typed operation, so the
  reassignment is visible on the board rather than happening as a side effect
  of one agent deciding to start typing into another's row.
- **A handoff carries expected-state fields** — the row's current version, its
  current assignee — so it only applies if the row is still in the state the
  handoff assumed. Two agents racing to hand off the same row at once get one
  winner and one clean rejection, not a silently corrupted transition.

[![Five-step handoff sequence including the refused cross-owner claim](assets/handoff-sequence.svg)](assets/handoff-sequence.svg)

The refusal is the safety property: the receiver cannot make itself the owner.
The sender first submits a fenced transfer, the database commits the ownership
change atomically, and only then does the receiver claim under its own identity.

## Avoiding file collisions between two agents

The board coordinates rows, not files, by default — nothing stops two claimed
rows from touching the same files unless someone says what those files are.
For work where that matters, a claim can declare its write set: a list of
scopes (a file-path prefix is the common case) it intends to touch. Any agent
can then ask whether the *currently held* claims declare overlapping scopes,
and the answer names the specific rows and the specific overlapping paths —
`work-a` (claim `clm-1`) declares `src/billing/` while `work-b` (claim `clm-2`)
declares `src/billing/retries.py`, for example. A path scope is a prefix match
in both directions, so a broad claim over a directory correctly collides with
a narrow claim over one file inside it. A small set of scopes is refused
outright — anything that would resolve to the whole filesystem, or to the
running deployment's release symlink, is rejected as ungrantable rather than
silently accepted.

The practical rule: if two agents might touch the same part of a codebase in
the same window, declare write sets on both claims and check for overlaps
before either one starts editing, not after. For anything narrower — two
agents working genuinely separate modules — this step is unnecessary
ceremony; use it where the risk is real.

## Copy-paste instruction block

Drop this into an agent's system prompt to get the behaviour above without
re-deriving it:

```text
You coordinate through the shared board, not around it.

- At the start of a session, orient first (what's assigned to you, what
  claims you already hold, what's waiting for you) before doing anything else.
- Claim a work item before doing substantive work on it. Never edit, run, or
  investigate something you haven't claimed.
- Use one stable session identity for your whole working thread. Don't mint a
  new one per prompt or per task.
- Heartbeat only to renew a claim's lease before it expires. It is not a
  progress log — keep it to a one-line status label at most.
- Close every claim explicitly: `done` with real proof that
  satisfies the work item's declared proof; `block` or `park` with a concrete
  next step and a concrete resume condition. Never leave a claim to expire
  silently.
- If you spawn subagents or a background fleet to parallelise work, claim the
  parent row first. Subagents cannot claim work themselves — if you don't
  claim it, the work they do is invisible to everyone else. Record each
  subagent's identity, model, and outcome under that claim.
- To hand work to a different agent, use the handoff operation with a task,
  a reason, and an acceptance condition the receiving agent can check itself
  against. Don't just claim another actor's assigned work directly, and don't
  hand off by leaving a note somewhere and hoping it's read.
- If your work might touch the same files as another live agent, declare your
  write set and check for overlaps before you start editing.
```

## Where an agent's memory lives

Everything above is about *doing* work through the board. This section is about what
happens to the things you learn while doing it. An agent produces facts constantly —
a step it just took, a constraint it discovered, a number it measured, a decision it
made — and each of those has a different lifetime and a different eventual reader.
Putting all of them in one place is the failure mode: a transcript remembers
everything until the session ends and then nothing, which is exactly backwards.

There are five durable places a fact can go, and they are not interchangeable.

### The write decision

| Place | Written with | Lifetime | Who reads it |
|---|---|---|---|
| Claim step | `claim_work(step=...)`, `heartbeat(step=...)` | The lease | Anyone watching the board *right now* |
| Note | `note(work_id, body, refs)` | The row's event history | The other lane, on that row |
| Resume contract | `park`/`block` with `next_step` + `resume_when` | Until the row resumes | Whoever picks the row up, possibly weeks later |
| Committed artifact | `complete(artifact=...)` | The repository's history | Everyone, indefinitely |
| Decision | `decision(ruling, binds, scope)` | Until superseded | Every later session the ruling binds |
| Fact store | `facts.upsert_fact` / `facts.supersede` (Python API) | Bitemporal — superseded, never overwritten | Any session that would otherwise re-derive the value |

**A claim step is for what you are about to do, not what you learned.** It is a
one-line label attached to a lease that expires. `coord claim UI-101 --step
"splitting the preferences panel"` — the form used in
`tests/test_lifecycle_smoke.py` — tells a board watcher which part of the row is
live. Nothing downstream parses it, so nothing durable should depend on it.

**A note is for context the other lane needs on this row, now.** `_tool_note` in
`src/coordharness/coord/mcp_coord_server.py` addresses the note to the opposite
lane's inbox — an actor of `claude` produces a `to_selector` of `actor:codex` and
vice versa — and refuses any other actor value. It is deliberately small: the body
is capped at 2,048 characters, the title at 200, and the refs list at 32 pointers,
with the error text on the body cap telling you what to do instead ("use pointer
refs"). On the seeded demo board, `SRCH-403` carries a one-line note — "Latency
budget cannot be set until connection pooling lands." — while the dependency it
names lives in the row's `resume_when` ("Waiting on PLT-302; pooling changes the
numbers."). The seeded notes carry no refs; on a real row the pointer to
`PLT-302` is exactly what `refs` is for. A note is not a place to paste an
analysis; it is a place to point at one.

**A resume contract is for the fact that made you stop.** This is the `next_step` /
`resume_when` pair described under *Progress lives in the verb*, above — repeated
here only to place it in the hierarchy. On the demo board, `ML-202` is blocked with
`resume_when` = "Upstream export is incomplete before the 5th of the month." That
sentence is queryable as a column. The same sentence in a heartbeat would be gone
with the lease.

**A committed artifact is for the output itself.** `complete` requires an artifact
that satisfies the row's declared `done_signal` and is in version control —
`tests/test_lifecycle_smoke.py::test_completion_requires_a_committed_artifact`
exists to prove that an artifact which merely exists on disk is rejected. The demo
board's `UI-101` carries `done_signal` = `docs/reports/ui-101.md`; that file, once
tracked, is the memory. A row whose finding never became a file has no memory
outside its event log.

**A decision is for a ruling that constrains later work.** `decision` takes a
`ruling`, an optional `binds` list, a `scope`, and — importantly — a
`supersedes_event_id`, so rulings form a chain rather than accumulating in
contradiction. Reading them back is `get_decision_context`, which returns resolved
`heads` *and* `conflicts` for a work item or scope. "We are not going to support
the legacy theme switcher path" is a decision; six weeks later it is the reason a
new session doesn't reopen the question.

**The fact store is for a durable value someone would otherwise re-derive.**
`src/coordharness/knowledge/facts.py` holds statement/value rows with a status
(`live`, `superseded`, `closed`, `dark`, `parked`, `corrected`), an
`evidence_pointer`, and bitemporal validity. Superseding writes a new row and links
it; `supersession_chain` walks the history. `current_value` raises
`UnresolvedFactConflict` rather than picking a winner when two candidates tie under
every deterministic rule — a surfaced failure, not a silent one. "The reranker
evaluation ran on 4,812 judgements" belongs here; it is a number the next session
would otherwise recompute or, worse, guess.

Two honest limits on that last row. **There is no MCP tool and no CLI command that
writes a fact.** `upsert_fact` and `supersede` are a Python API only; the tool
surface (`facts_lookup`) is read-only. The other write path is the
`memory_proposals` human-review queue, capped at five proposals per source thread
per 24 hours, where the reviewer may not be the proposal's own `source_actor`.
An MCP `decision(memory_candidate=true, ...)` marks a typed fact candidate; only
after a successful `session_closeout` does the fenced producer offer that current,
non-superseded decision to the queue. The default is false. The MCP surface over
the queue remains read-only — `memory_proposals_list` and
`memory_proposals_get` show it, but no MCP tool reviews or promotes an entry.

The rule of thumb, in one line each: does anyone need this after my lease expires?
If no, it is a claim step. Does the other lane need it on this row now? A note.
Is it the output? An artifact. Does it constrain future work? A decision. Would
someone otherwise re-derive it? The fact store.

### The recall order

A fresh session should read in this order, and stop as soon as it has what it
needs.

| # | Step | MCP | CLI twin |
|---|---|---|---|
| 1 | Board lens — what is live, mine, and waiting | `preflight`, `orient`, `next_work`, `board` | `coord board`; `python -m coordharness.coord.board_context {capsule,digest,skeleton}` |
| 2 | Row context — everything about the one row | `work_context`, `event_context`, `get_decision_context`, `inbox_recent` | `coord inbox --actor ACTOR`; `board_context focus <WORK_ID>` |
| 3 | Knowledge search — has this already been answered | `knowledge_search`, `facts_lookup`, `facts_query` | none |
| 4 | Artifacts — the full text of one thing | `read_note` on a `memory://` pointer | read the file |

The order is not arbitrary. Each step is bounded and returns *pointers*, which turn
the next, larger read into a targeted one. `build_capsule` is trimmed to
`MAX_CAPSULE_BYTES` (6,144) and ends with an explicit `pointers` block naming the
recipe for going deeper. `output_budget.apply_output_budget` clips anything inline
to `INLINE_OUTPUT_LIMIT` (12,000 bytes) and writes the full text out to an
artifact when the caller supplies a directory for one. Reversing
the order — reading files first — is how a session spends its context before it
knows which file mattered.

Three things about that table are worth stating plainly rather than leaving a
reader to grep for them.

**The `board_context` lenses have no MCP equivalent.** `capsule`, `digest`,
`focus`, `search`, `history`, `skeleton`, `changes`, `curate`, and `export` are
CLI-only. The MCP server imports exactly one
symbol from `board_context` (`compact_row`, used inside an unrelated tool). `board`,
`work_context`, and `knowledge_search` overlap in *purpose* but are separately
written and narrower — `work_context` returns one row plus parent, epic, recent
events, and typed-handoff preconditions, where `build_focus` returns the row plus
parent, children, siblings, and a scored related-open/related-done set. An MCP-only
client cannot reach the `focus` shape without shelling out.

**`preflight` and `build_capsule` are two independent implementations, not one
function exposed twice.** `_tool_preflight` reads `roadmap_backlog.json` and ranks rows off
`exact_query_core.load_query_snapshot`; `board_context.build_capsule` reads board
rows and `load_recent_decisions`. Both end up calling `projection.health_summary`,
and that is the only piece of the answer they share. They answer a similar
question and can diverge. Treat neither as a wrapper for the other.

**The `board_context` lenses fail closed on a board whose exact-authority policy is
not enforced.** `load_exact_query_snapshot` requires the `coord_authority_policy`
singleton to be in `enforce` mode with an active generation, and raises otherwise.
A freshly seeded demo board is in `audit` mode with no active generation, so every
lens raises `ExactQueryCoreError: exact-authority policy is not enforced and
active` while `coord board` still answers normally. If the lenses fail on a new
project, that is the reason; it is not a missing database.

For step 3, `knowledge_search` federates over named sources — `board`, `facts`,
`kfts`, `artifact_manifest`, `accepted_memory`, `memory_proposals`, and
`board_history` — under a byte-budgeted profile. Two of those are board-backed
(`board`, `board_history`) and read `coord.db`, not the knowledge database; a
knowledge-only query has to pass `sources` explicitly. Note also that the `kfts`
source is only as current as its index, which is rebuilt by calling
`coordharness.knowledge.kfts.rebuild_index` — there is no CLI wrapper and no
automatic trigger. On a checkout that has never rebuilt it, `kfts.index_stats()`
reports `index_present: False` and `stale: True`. Check that — over MCP it is
`knowledge_index_status` — before concluding from a `kfts` miss that something was
never written down.

### The capsule pattern

When work needs to fan out, spawn fresh bounded agents and have them return
pointers — do not fork a conversation so the child inherits the parent's
transcript. The cost argument is that a fork re-pays for the whole lineage on every
turn, and most of that lineage is irrelevant to the child's actual task. The
correctness argument is stronger: a child that inherits a transcript inherits its
mistakes and its stale assumptions too, and has no way to tell which is which.

A capsule prompt carries five things and nothing else: the objective, the inputs as
pointers (a work id, an artifact path, a `memory://` pointer), the boundaries
(which files it may touch), the output shape, and a turn budget. What comes back is
a pointer plus a short conclusion — an artifact written to disk and its path — not
a payload riding the conversation.

This is a practice, not an enforced mechanism; nothing in `coordharness` inspects
how a subagent was prompted. What the code does enforce is the visibility half,
covered under *Spawning a fleet is starting a job* above: the parent claims the row
before fanning out, and `record_child_attempt` / `record_child_outcome` refuse to
run without a live held claim. A capsule fleet that skips the parent claim is
invisible whether or not its prompts were well shaped.

### What must travel with a handoff

The test for a handoff is simple: delete the sending session's transcript, and ask
whether the receiving lane can still act. The typed handoff operation is built to
force most of that. `handoff_existing` requires, as arguments and not as prose:

| Field | What it carries |
|---|---|
| `task`, `why`, `acceptance` | What to do, why it moved, and the condition the receiver can check its own work against |
| `owner_lane`, `target_intent` | Who now owns it and what state the row lands in |
| `refs`, `constraints` | Pointers to the evidence, and the boundaries that survive the transfer |
| `operation_id` | Idempotency — a retried handoff replays its original result instead of duplicating |
| `expected_version`, `expected_assignee`, `expected_head_event_ids` | The row state the handoff assumed, so a stale handoff is rejected rather than applied |
| `expected_protocol_epoch`, `expected_server_build_sha256`, `expected_server_instance_id`, `expected_client_profile_id` | The server and protocol the sender was talking to |

The expected-state fields are not ceremony: `work_context` returns a
`handoff_preconditions` block containing exactly the values to pass, so the intended
sequence is read the row, then hand off with what you read. Two agents racing to
hand off the same row get one winner and one clean rejection.

What the operation cannot force is the quality of `refs`. A handoff whose `refs` are
empty and whose `why` is "see our discussion" is well-formed and useless. The
protocol addition is: every claim in `why` that the receiver would have to take on
trust gets a ref — an artifact path, a `memory://` pointer, a work id, a decision.
If the evidence only exists in your transcript, write it to an artifact first and
ref that, or record it as a note on the row before handing off.

Note that `handoff_existing` is a deferred tool. It appears in the visible catalog
only when the deployment's client profile promotes it (`_SERVER_PROMOTION_CANDIDATES`
in the MCP server); on a profile that does not, the guard against claiming another
actor's work still applies. The CLI's `coord reassign` command is the concise agent
path: it reads the row version, assignee, and active assignment heads once and submits
those exact values to the same typed writer. A concurrent change is rejected; the
command never refreshes and silently retries a stale transfer.

### What automated routing means

Owner routing is not an implicit side effect of quota selection. `RoutePlanV1`
is a pure, provider-neutral planner: callers give it an exact work snapshot, active
assignment heads, lane capability/load snapshots, quota evidence, a versioned policy,
and explicit observation/expiry times. It returns an expiring recommendation with
hard exclusions, per-lane score components, confidence, work/policy/evidence hashes,
and `mutation_allowed=false`.

That last field is the safety boundary. A route plan does not claim, hand off, launch,
or update work. Applying one requires a separate fenced lifecycle operation. A future
automatic scheduler should be opt-in, limited to unassigned rows or rows explicitly
marked for automatic routing, and retain a kill switch, cooldown, and shadow audit.

Use the standalone snapshot surface when a caller already has explicit, versioned
evidence. It reads a file (or stdin with `-`), emits canonical JSON on stdout, and
returns nonzero without a plan for malformed, missing, incomplete, future, expired,
or stale evidence:

```bash
.venv/bin/python scripts/route_plan_snapshot.py route-plan-snapshot-v1.json
```

The input envelope is `RoutePlanSnapshotV1` with `work`, `assignment_heads`,
`lanes`, `usage`, `policy`, and `timestamps` objects, plus optional
`explicit_user_route`. Timestamps must explicitly provide `evidence_observed_at`,
`now`, and `expires_at`; the command never reads live telemetry, opens `coord.db`,
or applies its result.

For ending a whole session rather than moving one row, `session_closeout` takes a
`summary`, `successor_hints`, and `dead_ends`. The `dead_ends` list is the one most
often skipped and the most valuable: it is the only structured place that records
what was tried and did not work, which is otherwise the single largest thing lost
when a transcript ends.

### Tool map

| Need | MCP tool | CLI twin | Gap |
|---|---|---|---|
| Orient at session start | `preflight`, `orient` | `coord board`, `board_context capsule` | Two independent implementations; they can diverge |
| Rank what to pick up | `next_work` | `board_context digest`, `board_context skeleton` | No MCP equivalent for the lens shapes |
| Everything about one row | `work_context`, `event_context` | `board_context focus <WORK_ID>` | Different shapes; `focus` has siblings and a related set, `work_context` has events and handoff preconditions |
| Find a row by description | `board`, `knowledge_search` | `board_context search "..."` | No MCP equivalent for the lens |
| Rulings in force | `get_decision_context` | none | — |
| Messages addressed to me | `inbox`, `inbox_recent` | `coord inbox --actor ACTOR` | — |
| Search docs, facts, artifacts, memory | `knowledge_search` | none | `kfts` source needs `kfts.rebuild_index`; no CLI and no automatic trigger for the rebuild |
| Look up a durable value | `facts_lookup`, `facts_query` | none | Read-only; no write tool in either surface |
| Check whether the search index is current | `knowledge_index_status` | none | — |
| Read the memory-proposal queue | `memory_proposals_list`, `memory_proposals_get` | none | Read-only; proposing and reviewing are Python API only |
| Read one pointer's full text | `read_note` | read the file | — |
| Record mid-flight context | `note` | none | Body capped at 2,048 chars; goes to the opposite lane's inbox |
| Record a binding ruling | `decision` | none | — |
| Hand a row to another lane | `handoff_existing` | `coord handoff`, `coord reassign` | MCP is deferred; `reassign` snapshots fences once, then uses the same writer |
| End a session | `session_closeout` | none | — |

Related reading: [context architecture](context-architecture.md) for the tiered
model and the byte budgets, [graph and context](graph-and-context.md) for the
knowledge stores in detail, [MCP integration](mcp-integration.md) for the server
setup and the full tool catalog, and [coordination model](coordination-model.md)
for the lifecycle those tools write to.

## What this doesn't cover

This document is about the protocol — the sequence of calls and the reasoning
for each. It doesn't cover the database schema, the policy checks that run on
every lifecycle write, or how to stand up the MCP server; see the other
documents in this directory for those. It also doesn't prescribe how often to
orient, how many subagents is too many, or how fine-grained a write-set scope
should be — those are judgment calls that depend on the size of the codebase
and the number of agents working it concurrently, not something a protocol
document can size for you in the abstract.
