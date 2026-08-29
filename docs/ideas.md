# Ideas and open problems

This is not a roadmap. It is a list of places the codebase stops short of
what it implies, sized so a contributor can find a problem for an afternoon
or one for a quarter and know which is which before starting. Each entry
names the actual gap, why it resists the easy fix, and the smallest change
that moves it forward without pretending to solve the whole thing.

If you fix one of these, match the shape already in the repo: a plain
function with a narrow contract, a test that exercises the guard rather than
the happy path, and a doc that explains the reasoning, not just the API.

## Job custody beyond launchd

`docs/jobs-and-runs.md` is honest about what's missing: the `runs` table,
the `job_progress` sidecar, and liveness-by-`pid`+`pid_started_at` are here
and tested. What launches a job, groups its process tree so a supervisor can
kill the whole thing as one unit, and watches for runaway memory — none of
that shipped. The original was `launchd` plists, `start_new_session` for
process groups, and macOS-specific memory-pressure reads: single-platform by
construction, left out of this repository.

The hard part isn't the process-group mechanics — POSIX gives you
`os.setpgrp()` and a `SIGTERM` to the negative PID on any Unix. It's that
"launch, supervise, kill" differs per target: `launchd` on macOS, `systemd`
(transient units via `systemd-run`, or a unit file) on Linux, `cgroups` for
hard resource caps rather than a signal. A general `Launcher` has to
abstract over three supervision models, not three ways to spawn a
subprocess — a thin abstraction that only handles the spawn will quietly
punt "kill everything this job started" back onto the caller.

**Smallest first step:** a `Launcher` protocol with one POSIX process-group
implementation (`start_new_session=True`, kill via negative PID) that runs
on macOS and Linux alike, no `launchd` or `systemd` involved — the part
every downstream launcher would still need underneath its own scheduling
logic. **Effort:** a weekend for the POSIX-only `Launcher`; a launcher that
also covers `systemd` transient units and `cgroups` limits is closer to a
month, and probably belongs to whoever actually needs the Linux target.

## A Windows-compatible core

Two things block `coordharness` from importing on Windows, both named
directly in the source: `fcntl.flock` guards the projection-refresh lock in
`native_cockpit.py` (a non-blocking exclusive lock, so two flushes never
race), and the liveness checks in `coord/reaper.py` and `jobs/status.py`
assume a POSIX process model. `fcntl` has no Windows module, so the import
fails before any function in that file runs.

The replacements don't compose the same way their POSIX counterparts do.
`msvcrt.locking()` locks a byte range rather than a whole descriptor, with
no non-blocking-with-clean-failure mode the way `LOCK_EX | LOCK_NB` has —
you get a retry loop instead of "someone else has this, skip." And Windows
has no POSIX process groups; the equivalent is a Job Object
(`CreateJobObject` / `AssignProcessToJobObject`), a different API with
different semantics for what counts as still alive. A `portalocker`-style
library solves the locking half generically; the process half is closer to
the `Launcher` problem above than to a drop-in shim.

**Smallest first step:** isolate every `fcntl` call behind one `filelock.py`
module with a POSIX branch and a stub Windows branch, and get the test suite
*importing* cleanly on Windows before every check behaves identically there —
turning "does this run on Windows" from an unknown into a trackable gap.
**Effort:** a few days to get imports clean and the test suite collecting
under CI on Windows; matching POSIX behavior exactly — locking semantics,
process-group kill, the reaper's liveness assumptions — is a multi-week
effort with its own test matrix, not a follow-on afternoon.

## Plugin entry points

`coordharness` has no `[project.entry-points]` group in `pyproject.toml` —
every extension point is a hardcoded import. `mcp_coord_server.py` knows a
fixed tool list; `status.py` knows the fixed shapes a `done_signal` resolves
to; `runners/mlx_runner.py` is one specific local-inference backend imported
by name. Wanting a new done-signal kind, a new MCP tool, or a different
model runner means editing core files — exactly what makes a fork drift
from upstream.

Python entry points via `importlib.metadata` are the established fix.
What makes it worth doing carefully is that each extension point sits at a
different trust level. A
`board_panels` plugin only shapes a read-only rendering of already-computed
data — low risk. A `reaper_policies` plugin decides which claims and runs get
reaped, i.e. it can make the board silently forget work — that one needs an
idempotent, side-effect-free contract before third-party code should touch
it.

**Smallest first step:** pick the lowest-risk group — `board_panels` or
`notifications`, both pure output — define one `Protocol`, register it as an
entry-point group, and load it via `entry_points(group=...)` at startup,
wrapped in a per-plugin try/except so one broken plugin doesn't take down
the others. That establishes discovery, protocol, and isolation once; the
remaining groups (`done_signal_resolvers`, `mcp_tools`, `context_providers`,
`reaper_policies`, `verify_suites`, `model_runners`) each become "apply the
same pattern to a narrower interface." **Effort:** two or three days for the
first `Protocol`, its entry-point group, and the isolation wrapper; the
remaining six groups are a week or two total once the pattern exists to copy.

## Agent cost observability

`coord/token_ledger_rollup.py` already rolls token-spend events into weekly
aggregates, and the two migrations under `coord/migrations/` establish a
pattern worth reusing: rows immutable once written (`BEFORE UPDATE`/`BEFORE
DELETE` triggers refuse the write), keyed by content hash, corrections
landing as new versions rather than edits. Missing is a ledger of
model-call spend in that same shape, wired to `runs` so a cost figure
resolves back to the run, work item, and model that produced it.

This is harder than "add a table" because raw token counts answer the wrong
question once more than one model is in the fleet — different models cost
different amounts per token, and a hardcoded price table goes stale the
moment a vendor reprices. A trustworthy ledger needs the event itself
immutable and attributable, with price-to-dollars conversion a separate,
versioned lookup at read time — the same fact-versus-computed-at-read split
the schema already uses for liveness.

**Smallest first step:** a `coord_usage_events` table shaped like the
exact-authority migration (append-only, content-hash keyed, triggers
rejecting update/delete), fed by an `emit_usage_event(run_id, model,
input_tokens, output_tokens, ...)` call from wherever
`token_ledger_rollup.py` reads its raw events. Leave price conversion out of
the table entirely — a lookup applied by whoever renders the rollup, not a
fact the ledger should assert. **Effort:** a few days for the table and the
emit call at one call site; getting every model-call path in the fleet to
actually emit is the long tail, one integration at a time rather than one
migration.

## Multi-machine coordination

`docs/architecture.md` names this limit directly: SQLite-over-WAL does not
extend across machines. Every agent in scope today runs on one box against
one checkout. The moment two machines need to see the same board — a laptop
and a CI runner, two contributors each running a fleet against a shared
remote — the foundation stops applying.

What replaces it is a real fork, not a bigger timeout: either a thin server
in front of the same SQLite file (reintroducing the "one more process to
keep alive" cost `architecture.md` rejected for the single-machine case) or
swapping SQLite for something with native concurrent writers over a
network — Postgres, most plausibly, given how directly the schema's
triggers and partial unique indexes map onto it. Either path touches every
`connect()`/`connect_ro()` call site, and the liveness checks
(`pid_matches`, `os.kill(pid, 0)`) stop being enough alone — "is this
process alive" has to become "alive, on that host," and neither `runs` nor
`agent_sessions` carries a host identity today.

**Smallest first step:** add a `host_id` column to `runs` and
`agent_sessions` (default: local hostname), and make the liveness check
refuse to answer for a run recorded on a different host — report "unknown"
rather than a false alive or dead from a PID that only means something on
its own machine. Small, backward-compatible, and a prerequisite for what
follows. **Effort:** an afternoon for the column and the liveness-check
change; the server-or-Postgres decision behind it is a multi-week rewrite
that shouldn't start until the column ships and the gap it exposes turns
out to be real, not hypothetical.

## A shared whiteboard

The board coordinates through typed lifecycle verbs and structured events —
deliberately, since a free-text status field is what `docs/schema.md` argues
against. But there's a gap between "structured event" and scratch space two
agents can both see mid-task: one on the client side of a migration and one
on the server side seeing each other's in-progress state without completing
a claim to publish it. `note` and `audit` are close but not this — both
append-only and addressed, closer to a mailbox than a whiteboard something
gets erased and redrawn on.

The hard part isn't storage — a `whiteboards` table keyed by scope (work
item, module, fleet-wide) with a `version` column fits the schema's existing
patterns exactly. The hard part is what a whiteboard *means*: advisory
context that never blocks anything, or state something else reads and acts
on — a second place status can live, exactly what the derived-status
principle exists to prevent. Get that boundary wrong and the stored status
column is back, one layer removed.

**Smallest first step:** ship it explicitly advisory — a
`whiteboard_set(scope, key, value, session_id)` / `whiteboard_get(scope,
key)` pair, last-write-wins, with a rule enforced in code, not just docs,
that nothing in the policy pipeline or lifecycle verbs reads from it. That
gives agents a place to coordinate that isn't a queue of timestamped
messages, without reintroducing a stored status. **Effort:** a day or two
for the table and the two functions; the real cost isn't the code, it's the
ongoing discipline of stopping every future feature from quietly starting
to read it as state.

## Policy checks as a public extension point

`docs/policy-pipeline.md` documents how to add a check — write a
`PolicyHandler`, add it to `PASS_ORDER`, give it a default mode — but that's
a guide for editing `pipeline.py`, not a way for a dependent project to add
its own check without forking. The seven shipped checks are useful defaults
for a generic fleet, not a closed set. This is the same problem as the
entry points above, applied to a seam that's already clean: a
`PolicyHandler` is just `PolicyContext -> PassResult`, no different in
shape whether it ships with the package or not.

**Smallest first step:** once the entry-point pattern exists for one
low-risk group, apply it here — a `coordharness.policy_checks` group,
appended to `PASS_ORDER` after the seven built-ins, defaulting every
externally-registered check to `warn` regardless of what the plugin
requests. A third party's check earning `enforce` should be a deliberate
deployment decision, not something a plugin grants itself on install.
**Effort:** an afternoon, once the entry-point pattern exists for one other
group — the near-zero marginal cost is the reason to build this second, not
first.

## Richer proof types

`done_signal_custodied()` recognizes local artifact and structured data shapes.
That is not the only evidence "this is actually done" can look like — a
passing CI run, a deployed revision hash, or a signed attestation may be more
appropriate than a local file and are not first-class proof kinds today.

The difficulty isn't recognizing more shapes — it's that each has a
different verification cost and a different way to be faked. A commit-pinned
file is checkable locally, while a CI pass or a signed
receipt needs a call out to whatever produced it — `complete()` now depends
on a network call succeeding, a different reliability contract for the one
path meant to be hard to satisfy.

**Smallest first step:** add a `done_signal` kind field (today always
inferred from the path) with one new kind — `command`, a shell command that
must exit zero when `complete()` runs, no network dependency, testable the
same way the file-based checks are. That covers "a test suite passes"
without touching the harder, network-shaped cases. **Effort:** a day for
the `command` kind end to end, including a test; each further kind — a
CI-pass lookup, a signed receipt — is its own multi-day effort with its own
trust model, not a variation on the first.

## Evaluation harnesses for agent behaviour

Everything here assumes an agent following `docs/agent-protocol.md` in good
faith — claim before working, heartbeat to renew rather than narrate, close
with a real artifact. The policy pipeline catches some deviations
(`loop_doctor` on a repeated dangerous action, `structured_status` on a bad
terminal-state claim), but nothing asks the broader question repeatably:
given a fixed scenario, does an agent driven by a given model and prompt
actually follow the protocol, or claim work it doesn't hold, mark things
done without evidence, or loop past a stop condition the pipeline missed?

That's different from `tests/test_lifecycle_smoke.py`, which checks that the
*database* enforces its guards regardless of caller. An evaluation harness
checks that an *agent*, given the same seeded demo board, behaves well
without those guards catching the mistake after — a regression suite for
protocol adherence, not code correctness.

**Smallest first step:** a fixture library that seeds the demo board, replays
a scripted or model-driven session, and asserts on the resulting event
log — did the agent claim before writing, complete with a real artifact, or
trigger a warned check it should have self-corrected on. Start with
scenarios mapped onto guards already documented (claiming another actor's
work, completing without controller-declared proof, looping on a flagged
action), so the harness's first job is confirming known-bad behaviour gets
caught, before inventing new scenarios. **Effort:** a week for the fixture
library and the first three known-bad scenarios; growing it into a real
regression suite that runs across models on every change is ongoing work,
not a one-time build.

## What a hosted profile would need

Everything here assumes local trust: whoever can open `coord.db` can write
to it, and `actor` is a string a caller supplies, not an identity the system
verifies. That's correct for the target case — several agent processes on
one machine, one operator behind them — and wrong the moment `coordharness`
runs as a shared service multiple people use without trusting each other's
filesystem access. A hosted profile needs authentication, authorization (is
this actor allowed to claim *this* work), and probably per-tenant isolation
— none needed today.

The trap is treating this as smaller than it is. Identity is not network
auth: a login screen in front of the same trust model doesn't change that
`actor` is an unvalidated free-text field, and every lifecycle function still
assumes the caller is who they say they are. A hosted profile is a different
security posture, not a detail bolted onto the existing one — every guard
described here (one held claim per row, a handoff instead of a silent
reassignment) protects against *accidents between cooperating agents*, not
a malicious or compromised caller.

**Smallest first step:** before writing any auth code, write down which
existing guards hold if `actor` becomes an untrusted, server-verified
identity instead of a trusted local string. The version-based concurrency
checks are the likely survivors; anything trusting a self-reported
`session_id` is the likely casualty. A login screen before that audit just
moves the trust boundary without checking what's behind it. **Effort:** the
audit itself is a few days of reading and writing down conclusions; the
auth and authorization layer it might justify is a multi-month rewrite,
well outside anything sketched here.

## Cost-aware scheduling from the usage ledger

`usage/ledger.py` already records provider spend per account and model —
`provider_native_cost_nanos`, `api_rate_estimate_nanos` — as an append-only,
content-hash-keyed ledger with an explicit `coverage_state` per period
(`complete`, `partial`, `unknown`, `error`) for when the numbers aren't
fully reconciled yet. None of that reaches a routing decision. `next_work`
and `claim()` hand out work to whichever agent asks, with no notion of "this
account is close to its budget for the period" or "route the cheap items to
the cheap model."

The ledger's own honesty about incompleteness is what makes this hard, not
the arithmetic. A period mid-flight can be `partial` because a session is
still running with usage not yet imported — gating a live claim on a number
that's still moving risks blocking work on a budget figure that's about to
be wrong in either direction. A scheduler that trusts a `partial` total as
final either blocks work that was actually still affordable, or lets
spend run past a ceiling because the last hour of usage hadn't landed yet.
Any cost-aware gate has to treat `coverage_state` as a load-bearing field,
not a bookkeeping footnote it's fine to skip past.

**Smallest first step:** a read-only `remaining_budget(account, period)`
helper that returns `unknown` — not a number — whenever the period's
`coverage_state` isn't `complete`, exposed as one MCP tool an agent can
check before choosing to claim, advisory only, with `next_work` and
`claim()` left untouched. That's the same advisory-before-enforced shape as
the whiteboard idea above: prove the number is trustworthy before anything
depends on it. **Effort:** a weekend for the read-only helper and the tool.
Turning it into an actual scheduler that biases assignment by remaining
budget — and that doesn't quietly starve an account that looks broke but
isn't — is a multi-week piece with real false-block risk, not attempted
here.

## Replay and time-travel over the event log

`run_events` is a genuine append-only log — `seq`, `category` (`lifecycle`,
`message`, `tool`, `token`, `artifact`, `trace`, `error`, `security`),
`work_id`, `run_id` — with an idempotency key so a retried write can't
double-post. But work items, claims, and runs themselves are mutated in
place, UPDATE not INSERT, so the only state the board can answer questions
about is *now*. "What did this claim look like right before it got
released" or "reconstruct the context an agent was acting on at event 4110"
means reading raw event JSON by hand; there's no `as_of(seq)` query anywhere
in the codebase.

Real time-travel is full event sourcing — every writer's effect expressed
as a pure function of an event, replayed from empty to rebuild any prior
state — and that's a much bigger invariant than "the row and the event
happen to roughly agree," which is what the schema guarantees today. Rows
are updated directly by lifecycle functions; events are recorded alongside
as a matter of narrative discipline, not derived from each other in either
direction. Getting genuine time-travel means either replaying the full
event stream into a shadow schema on demand (slow, and only as complete as
every event type's replay function) or periodic snapshots plus a diff
window (cheaper, but "how far back can you actually go" becomes a real
operational question with a real answer that isn't "always").

**Smallest first step:** a narrow `replay_work_item(work_id, up_to_seq)`
that reconstructs one row's `status` and a handful of other fields purely
from `lifecycle`-category events for that `work_id` — which already encode
their own from/to transitions — and raises rather than guesses the moment
it hits an event shape it doesn't recognize between the start and the
target `seq`. Not general event sourcing, just enough to answer "what state
was this one item in after this one event," honestly. **Effort:** a few
days for the narrow single-row replay. General time-travel across the whole
board — every table, every writer, on demand — is quarter-scale work
restructuring how state is derived, not a natural next step from the
narrow version.

## Provenance-bound graph edges as a general pattern

`coord/migrations/003_provenance_causal_trace.sql` already builds real
provenance discipline for one relationship: an artifact's content-hashed
object versions, grouped into import batches, with a `heads` table that
only moves forward and a quarantine table for anything that fails
admission. Every row traces back to a `batch_id`, and every batch carries
its own manifest hash — exactly the "never assert an edge with no
`source_ref`" discipline the project's provenance principle calls for. It's
also entirely specific to one predicate: "this artifact version came from
this import." There's no way to assert, with the same rigor, that a
decision depended on a work item, or that one run's output fed a second
run's input — those relationships exist in practice and nowhere in the
schema, tracked only in whatever refs an agent happened to type into a
`note`.

A generic `(subject, predicate, object, source_ref)` edge table is easy to
sketch and would be worse than useless on its own — a foreign-key-light
join table carries none of the discipline that makes migration 003 trustworthy:
content-hash checks, a batch as the atomic unit of admission, heads that
never move backward. Reproducing all of that per predicate defeats the
point of generalizing; a table with none of it is just an unenforced tag.
The real work is finding the subset of that discipline that's predicate-
agnostic and keeping the rest as per-predicate policy layered on top.

**Smallest first step:** carve out `coord_provenance_edges(subject_id,
predicate, object_id, source_ref, evidence_json, created_at)` with the one
constraint that generalizes cleanly — `source_ref` is `NOT NULL`, no
exceptions — and prove it on exactly one new predicate (`run.produced ->
work_item.artifact` is the most obvious candidate, since `runs` and
artifacts already both carry the identifiers an edge would need) before
calling the pattern general. **Effort:** a week to carve out the table and
prove it on one predicate. Migrating every existing implicit relationship
in the schema onto it is a longer, incremental effort measured in
predicates added, not a single migration.

## A TUI

The clients today are the `coord` CLI (one verb, one invocation, one exit),
the MCP server (tool calls from inside an agent's own turn), and a native
desktop cockpit that reads a cached projection built by
`native_cockpit.py`. Nothing renders a live-updating view in a terminal —
a table of open claims, recent runs, and the tail of `run_events` that
updates in place the way `top` does, for a human watching a fleet without
opening a GUI app or re-running a CLI command every few seconds.
`native_cockpit.py` already computes exactly the projection a TUI would
need; today it only feeds a native app's data layer.

The hard part isn't the rendering — Textual or a plain curses loop over the
same projection is well understood. It's staying live without becoming
another writer that contends with the lock `native_cockpit.py` already
takes seriously: that module guards its projection refresh with
`fcntl.flock` specifically so two flushes never race. A naive TUI polling
raw tables every second from several terminals at once adds read load for
no reason, and if it ever writes anything — even a UI-only "selected row"
marker — it's one more contender on a codepath that was tuned to avoid
exactly that kind of contention.

**Smallest first step:** a strictly read-only TUI that only ever opens the
existing `connect_ro()` path and polls the *cached projection* table
`native_cockpit.py` already maintains, at a deliberately coarse interval —
reusing every guarantee that module provides instead of re-deriving board
state independently in a second codepath. **Effort:** a few days for a
minimal read-only table view. A fully interactive TUI — claiming or
completing work from inside it — is a larger effort, because every action
then has to route through the same policy pipeline the CLI does rather than
shortcut it, which is most of the CLI's own complexity re-earned in a
different frontend.

## Conflict detection from declared write sets — built

This shipped. `coord claim --write-scope [KIND=]VALUE` declares a scope when a
claim is taken, `coord declare-write-set` adds one to a claim already held, and
`coord conflicts` answers, read-only, which currently-held claims intend to
touch the same paths. The same three are on the MCP surface. An overlap names
both sides — work id, claim id, session and scope — so an agent is told which
peer it is about to collide with, not merely that a collision exists.

Two decisions made while building it are worth keeping.

**It reports, it does not block.** The original entry argued that a naive block
would stop plenty of harmless concurrent work, and that still stands: two claims
on `README.md` are almost always fine, two on the same migration almost never
are, and nothing here can yet tell those apart. Overlaps are information an
agent weighs. Enforcement remains a separate, harder decision that wants real
usage data first.

**A claim that declared nothing is reported as undeclared, not as clean.** The
report carries `undeclared_claims` beside its findings. Without it, an empty
result reads as "no conflicts" when it may mean "nobody said what they were
touching" — and a silent all-clear is worse than no answer, because it is
believed.

What remains open is the severity signal the original entry wanted: an overlap
on a lockfile and one on a shared schema are the same event today. Grading them
needs observations of how often a declared overlap actually mattered, which only
exist once people run this.
