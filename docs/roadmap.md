# Roadmap

This is a direction, not a promise. Nothing here has a date, a committed
release, or an owner assigned by default. It exists so a contributor can
tell "we're actively moving toward this" from "this is an idea nobody has
started" (that's `docs/ideas.md` — read it first; every item below points
back to an entry there). Order within a tier is not priority order. Items
move between tiers, or off the list entirely, without notice.

The three tiers mean different things:

- **Near** — the gap is understood, the smallest first step is known, and
  doing it doesn't require a design decision that could go more than one
  reasonable way.
- **Next** — the gap is understood but the first step depends on something
  in *near* landing first, or on a design choice (a schema shape, a trust
  boundary) that's worth getting right once rather than iterating on live.
- **Someday** — real, worth doing eventually, but either large enough that
  starting it now would compete with everything else on this list, or
  waiting on evidence (real usage, a real second use case) that doesn't
  exist yet.

## Near

**Entry points for the lowest-risk plugin group.** `board_panels` or
`notifications` — pure output, nothing that can make the board forget a
claim — become the proof that `importlib.metadata` entry points fit this
codebase's shape before any higher-trust group gets the same treatment.
Depends on nothing already on this roadmap. See "Plugin entry points" in
`docs/ideas.md`.

**A `host_id` column on `runs` and `agent_sessions`.** Doesn't change
behavior for the single-machine case — everyone gets `local hostname` by
default — but it's the one schema change every multi-machine path
downstream needs and can't retrofit later without a migration touching live
data. See "Multi-machine coordination."

**A `command` kind for `done_signal`.** One new value on an existing field,
checkable locally, no network dependency. Covers "a test suite passes" as
proof of done without touching the harder proof types that need a network
call. See "Richer proof types."

**A read-only TUI.** Reuses the projection `native_cockpit.py` already
computes and the `connect_ro()` path that already exists; adds no new write
path and no new lock contender. See "A TUI."

## Next

**A second policy-checks group, and a public plugin point for policy
checks specifically.** Depends on the entry-point pattern from *near*
existing and being trusted; once it does, this is close to free — a
`coordharness.policy_checks` group appended after the seven built-ins,
every externally-registered check defaulting to `warn`. See "Policy checks
as a public extension point."

**A `coord_usage_events` table wired to `runs`.** The shape to copy —
append-only, content-hash keyed, update/delete refused by trigger — already
exists twice in the schema; this is applying it a third time to spend
instead of provenance or work state. What makes it *next* rather than
*near* is that it's only useful once call sites actually emit to it, and
that's an ongoing integration effort, not a single migration. See "Agent
cost observability."

**A read-only cost-aware scheduling signal.** Depends on the usage-events
table above existing and on `coverage_state` being trustworthy enough to
gate a live decision on — which is itself an open question this needs to
answer conservatively (return `unknown`, not a guess) before it's safe to
build. See "Cost-aware scheduling from the usage ledger."

**A general provenance-edge table, proven on one new predicate.** Depends
on a real design decision — which part of migration 003's discipline
generalizes across predicates and which stays predicate-specific — that's
worth thinking through once rather than getting wrong twice. See
"Provenance-bound graph edges as a general pattern."

**Declared write sets and a read-only conflict query.** The field and the
query are near-sized on their own; they're staged here because they're
only worth building once there's a real place to point contributors who
want to try it against a live fleet, and because the honest first version
ships with zero blocking behavior — the interesting design question (when
does an overlap actually matter) needs real data this doesn't have yet.
See "Conflict detection from declared write sets."

## Someday

**Multi-machine coordination past the `host_id` column** — the actual
server-or-Postgres decision. Deliberately not started until the column
above ships and the two-machine case shows up as a real need, not a
hypothetical one.

**A Windows-compatible core**, and **job custody beyond `launchd`**
(`systemd`, `cgroups`, a real cross-platform `Launcher`). Both are
substantial, both are single-platform gaps that don't block anyone running
one fleet on one machine today, and both are better sized down by whoever
actually needs the target platform than guessed at in advance.

**General time-travel over the event log.** The narrow `replay_work_item`
first step is *near*-sized on its own and not listed above only because
nothing else depends on it; genuine event sourcing across every table is
its own project, gated on whether the narrow version turns out to be useful
enough to justify the bigger one.

**Evaluation harnesses for agent behaviour**, past the first fixture
library and its three known-bad scenarios. Growing it into something that
runs across models on every change is ongoing work with no natural
stopping point, which is exactly why it isn't staged as a single next step.

**Anything resembling a hosted, multi-tenant profile.** Everything in this
codebase today assumes local trust — whoever can open `coord.db` can write
to it. The honest first step toward a hosted profile is an audit of which
existing guards survive an untrusted `actor`, not a login screen; that audit
hasn't happened, and nothing past it belongs on a roadmap yet. See "What a
hosted profile would need."

## What this list is not

It is not a backlog with story points, not a set of commitments to any
timeline, and not a ranking of what matters most to any particular user of
this project. A contributor picking up an item from *near* should still
read its full entry in `docs/ideas.md` — this file only says roughly when,
`ideas.md` says what and why it's hard.
