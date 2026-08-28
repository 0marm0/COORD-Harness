# Design notes

Architecture docs describe what the system does. This one is about the choices
behind it — what else was on the table, why that option lost, what we gave up by
not taking it. A few turned out to be free wins. A couple cost more than expected
and we kept them anyway because the alternative was worse.

## Status is derived, not stored

The obvious design is a `status` column: `"running"`, `"blocked"`, `"done"`. It's
fast and legible, and it's wrong the moment the process that set it dies — nothing
pushes a correction, so it keeps saying what it last said until something notices
and fixes it by hand.

Instead, the schema stores intent (what an agent last declared) separately from
liveness (whether a claim's lease is unexpired and its process still answers), and
computes displayed status by joining the two at read time. There is no cleanup job
whose whole purpose is finding stale rows, because there's no stale value sitting
in a column to find — the join recomputes it from the clock and the process table
on every read.

The cost is a view join instead of a column lookup, not a real cost at board scale.
Where it would matter — a client redrawing every frame — we added a second, cached
read path rather than compromise the property that the canonical read is always
correct.

## Completion needs controller-declared proof, not an agent's word

The lighter design lets an agent flip a status to "done" and trusts the report. That
fails the moment an agent finishes the wrong thing, or reports success because that
was the last line of its script regardless of what actually happened.

So completion is gated on a receipt: a work item declares up front what proof its
completion has to produce, and the completing call checks that the caller holds
the live claim, the explicit path matches the declaration, the proof is complete
under custody rules, and terminal/review barriers pass.

Markdown done-signals must be tracked by Git's current index. Staging is enough
for the current custody gate; a commit is not required. A project that needs
proof pinned to a commit should express that stronger condition in acceptance
or policy. The invariant remains explicit: completion cannot bypass declared
proof.

## SQLite in WAL mode, not a server

Coordinating several agent processes sounds like it wants a server — one API,
writes serialized in one place. We rejected that for this package's actual case:
several agents on one machine, one checkout, a session lasting minutes or a full
day. A server is a process someone has to keep alive before running one command.

SQLite's default locking doesn't fit either — a writer holds an exclusive lock on
the whole file, so one agent mid-write blocks every other agent's read. WAL mode
fixes that: writers append to a separate log, readers keep serving the last
consistent snapshot while a write is in flight, with a busy timeout so a momentary
conflict retries instead of raising and `synchronous = NORMAL` accepted as durable
against a process crash, not an OS crash mid-write — a fine trade for a log that
isn't the system of record for anything outside this repository.

The cost, stated plainly: this does not extend across machines. Every process
touching `coord.db` has to be on the same box, against the same filesystem. A fleet
spread across hosts needs a different board.

## Leases, not locks

A lock is binary and has to be explicitly released, which is a bad fit for
processes that die without running cleanup code — killed, a closed chat window, a
laptop that loses power mid-task. Lock semantics turn every one of those into a
row that stays claimed until a person notices and frees it by hand.

A lease expires on its own. Holding a claim means renewing it with a heartbeat
before it lapses; nothing else keeps it alive. Two things act on an unrenewed
lease: the read path treats it as not-running the moment it's past expiry, and a
periodic sweep flips the claim and resets the item to `queued` unless it was
deliberately parked or blocked. The default lease is an hour; a shorter one exists
for work that wants tighter checks.

The cost is precision — a lock-based design knows the instant a hold is released;
a lease-based one has a window, up to the lease length, where a dead agent's claim
still looks live. We accepted that because the alternative is worse: a lock
nobody frees blocks a row forever.

## One claim per row, enforced by the database

Two agents must not both hold one item, and we wanted that to hold even under a
bug in the calling code, not only under a careful caller. So it isn't application
logic checking "is this claimed?" before inserting — it's a partial unique index
on the claims table, scoped to the statuses that count as held. A second insert
against an already-claimed row is rejected by SQLite itself, atomically, regardless
of what two racing processes each believed a moment earlier.

Paused and blocked claims still hold the slot on purpose: suspended work is not
abandoned work. Moving ownership is a distinct, typed handoff, not a claim quietly
taking over someone else's slot.

## Why the policy pipeline mostly reports instead of blocking

Every write to the board runs a fixed sequence of checks — a work item id present,
a retry-loop pattern, token spend near budget. The obvious design is binary: pass
or refuse. We built exactly one check that way and left the rest advisory.

Most of what these checks catch is a caller drifting toward a bad pattern, not a
fact that's already false. An agent retrying an expensive test run a fourth time
hasn't corrupted anything — it's a signal worth surfacing, not a wall worth
building. Refusing the write would stop it from making progress it might fix on its
own next turn; reporting the finding back with the write's own response lets it
self-correct instead, with a human involved only if the pattern persists.

The one check promoted to a hard block validates a completion's terminal state —
refusing a "done" call carrying `failed` or `stagnated` — because a wrong write
there corrupts what "done" means for every downstream reader, and no later
self-correction fixes a board that already lied. The honest cost: an agent that
ignores its own warnings can push a write through nearly every other check.
Visibility is not the same guarantee as enforcement.

## Grouping axes are nullable, not a hierarchy

The early instinct was a strict tree — category, then project, then task, each row
pointing at one parent by foreign key. We backed off, because a rigid tree becomes
something that itself needs maintaining: regrouping work means a migration, and
people reslice a board far more often than the work underneath it changes.

Instead a work item carries independent, nullable tags — domain, module, lane, an
optional sublane. None references a lookup table, none is required, none nests by
constraint. Switching the default grouping from module to lane is a different
query, not a schema change. The cost is that nothing stops two people tagging
similar work differently — no foreign key forces a canonical vocabulary, so drift
is a discipline problem, not a database-enforced one. We took that trade because a
taxonomy nobody's work still matches is worse than one that's occasionally
inconsistent.

## Heartbeats renew a lease; they don't report progress

It's tempting to make a heartbeat double as a status update — "still working,
here's what I've found." We kept it to one job: extend an existing claim's expiry,
refusing to extend one already lapsed or not running. A heartbeat carrying
free-form progress would turn a cheap, frequent, mechanical call into something an
agent has to compose each time, and would blur a liveness signal with a narrative
one. Narrative updates belong to verbs built for them — a note, an audit — which
don't run on a timer and don't need to stay cheap enough to call every few minutes.

## Subagents roll up instead of getting their own rows

A session working one item might spawn a dozen subagents in parallel, or launch a
detached background run and keep going. One row per process turns a single
investigation into a wall of rows someone has to mentally regroup.

Every session and run can point at a parent; the board's top-level view only
surfaces sessions with no parent, rolling the rest into counts on that one row. A
dozen subagents chasing one problem reads as one row with a count of twelve, not
twelve competing for attention. The cost is resolution — the rolled-up row doesn't
say which of the twelve is stuck. That's deliberate: the rollup answers "what's
happening at a glance"; finding the stuck child is a second, explicit query.

## Bounded context instead of a bigger window

The obvious response to "the board and docs got too big to read in full" is a
model with more context. We think that solves the wrong problem — reading
everything at boot costs tokens whether or not it's relevant to the one row an
agent is about to touch, and a bigger window just raises the point at which that
waste becomes affordable without removing it.

So a session boots from a small, fixed-size read — counts, what's actively claimed,
what's deliberately paused and why, recent decisions — trimmed to a hard byte
ceiling. Going deeper is a named, explicit call: ask about one item and its
neighbors, search by keyword, pull a bounded slice of one document. Every bounded
response names the next call for more, so the bound is a default an agent can push
past on purpose, not a wall it hits by accident. The cost is real for a session
that genuinely needs broad context — it has to ask more than once for what an
unbounded dump would have handed it up front. That's smaller than the tax every
other session pays for context it never needed.

## The extraction regenerates prose instead of scrubbing it

This project was cut from a private codebase whose mechanical code — the state
machine, the schema, the CLI wiring — was already generic. The English around it
wasn't: docstrings, comments, and string literals carried real internal detail.
The tempting fix is a wordlist: copy the file, strip anything matching a list of
sensitive terms.

We rejected that because a wordlist only fails on vocabulary it already knows
about — nothing about an internal codename nobody thought to list, or a name left
in a stray comment. So the extraction removes all prose unconditionally, using the
language's own parser rather than a regex, so something that merely looks like
code inside a docstring isn't mistaken for code. What's left is renamed through an
explicit table and documented fresh, by someone who never saw the original
English, only the stripped mechanism. A denylist still runs afterward, but as a
check on the check, not the primary defense. The cost is real: this throws away
genuinely useful documentation along with the risky parts, and every comment here
had to be rewritten from nothing.

## The publication gate is an allowlist

The check that runs before anything publishes verifies every present file is
accounted for — ported, authored fresh, or on a short fixed infrastructure list —
rather than scanning for things that shouldn't be there. A denylist-shaped gate
reports clean for every category nobody thought to add; an allowlist-shaped one
refuses anything not already accounted for, including a category nobody
anticipated. The cost is friction: any new file must be declared before the gate
passes it, even when nothing's wrong with it — the right trade for a gate whose
whole job is catching what nobody thought to check for.

## Honest limits, today

A few things here are genuinely unfinished, not simplified for the write-up:

- **POSIX-only.** Claim liveness checking uses `os.kill(pid, 0)` and process-group
  semantics that don't exist on Windows. Importing this on Windows surfaces real
  gaps, not a degraded experience.
- **Projection clients are Preview.** The web board and clean-room macOS/iOS
  clients now exist and consume the versioned snapshot, but their compatibility
  contract can still change. They remain GET-only viewers, not lifecycle writers.
- **Read-only does not mean remotely safe.** Query-only SQLite, loopback binding,
  method refusal, and host checks defend the projection boundary. They do not add
  authentication, tenant isolation, or safe LAN/internet exposure.
- **Single-host only**, as above under the SQLite decision. There's no story for a
  fleet across more than one machine, and building one means revisiting the
  WAL-file decision at its root, not extending it.

None of this is secret. It's the next work, roughly in the order it would bite a
new user — and the reason this document exists is so the next person can see why
the current shape is what it is before deciding whether to change it.
