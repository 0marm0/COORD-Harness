# Comparison

**COORD's one-sentence differentiator:** completion is refused until the
declared proof artifact is in git's index, status is derived at read
time and never stored, and — for work tiered as needing independent
review — a lane cannot pass its own work.

That is a narrow claim, on purpose. COORD is not an orchestration framework,
not an agent runtime, and not a task tracker. It is a local SQLite database
(WAL mode, one writer) that a handful of clients — a CLI, an MCP server, a
Python API — write through under one typed lifecycle contract, so that
"claimed", "running", and "done" mean the same fenced thing regardless of
which client said so. If your problem is something else, one of the sections
below probably names the better tool. Every claim about COORD on this page
cites the file that makes it true; none of the claims about the other
projects go past their public category behavior, because that is as far as
this repository's evidence reaches.

## The differentiator, with receipts

**1. Completion of a declared proof artifact is refused until it is in
git's index.** `complete_claim` in
[`src/coordharness/coord/coord_db.py`](../src/coordharness/coord/coord_db.py)
calls `done_signal_satisfied`, which defers to
`done_signal_custodied` in
[`src/coordharness/jobs/status.py`](../src/coordharness/jobs/status.py) —
and that function's deciding check is `completion_proof_is_tracked(path,
root)`, a few lines above it in the same file, which shells out to `git
ls-files` and checks the artifact's path is in the result. A file that
exists on disk but was never `git add`ed fails this check; `complete_claim`
then raises with the exact `git add <path>` command to run, quoted verbatim
in that function's `raise ValueError(...)` block. **The check covers every
artifact type**, with one narrow exemption: the suffixes in
`DEFAULT_CUSTODY_EXEMPT_SUFFIXES` — `.parquet`, `.duckdb`, `.db`, `.joblib`,
`.bz2`, `.backup` — name databases, dataset dumps, serialized models and
archives, which cannot live in a git index at all. Those must still **exist**;
the exemption is about custody, not about proof. One environment variable,
`COORD_COMPLETION_CUSTODY_EXEMPT`, rebinds that list (or, set to `*`, turns
the requirement off), read in exactly one place so no surface can drift from
it. Before 0.1.0 the check was scoped to `.md` alone and every other suffix
completed on existence alone; widening it refuses completions that used to
succeed, which [`CHANGELOG.md`](../CHANGELOG.md) records as a behavior change.
[`examples/proof-gated-done/`](../examples/proof-gated-done) runs the case end
to end, including the refusal text captured verbatim from a real invocation.

**2. Status is derived at read time, never stored.** There is no code path
that writes `status = "running"` into a row and walks away — see
`docs/coordination-model.md`'s "status is derived" section, which points at
the expiry sweep every claim read/write runs: an expired lease is flipped to
`unclaimed` in the same transaction that noticed it, inside `claim_work` and
the batch sweep `release_expired_claims`, both in `coord_db.py`. A crashed
agent's row does not lie "running" until someone notices; the next touch —
anyone's — repairs it.

**3. A lane cannot pass its own work.** `post_audit_verdict` in
`coord_db.py` raises `same-lane PASS is forbidden: reviewer and author are
{author_lane}` if the reviewing actor's lane matches the authoring claim
history's lane — checked in code, not left to agent discipline. For work
tiered `T0` (external-facing, irreversible, or served-number changes —
`review_tier.py`), `completion_review_state` (also in `coord_db.py`)
additionally refuses `done` outright until either that independent verdict
lands or an explicit human sign-off is recorded.

(Line numbers are omitted on purpose: they go stale. The function names are
stable; `grep -n '<name>' src/coordharness/coord/coord_db.py` finds the
current line.)

None of this makes COORD an orchestrator, a scheduler, or an agent runtime.
It has an opinion about exactly one thing — what a shared lifecycle ledger
must refuse to say — and stays out of how your agents decide what to do next.

## Use something else if…

### …your agents are library calls inside one Python program

**LangGraph, CrewAI, AutoGen** — these are in-process graph/orchestration
frameworks: your agents are nodes or objects the same program instantiates,
calls, and gets return values from directly. If your agent runtime is a
single Python process and coordination means "which node runs next" or
"what does this agent's output feed into," you want a graph library, not an
external database. COORD assumes the opposite shape: independent processes
(possibly on different days, possibly a different agent than what claimed
the work last), which is why it exists as an external ledger at all. Reaching
for COORD to sequence steps inside one program's call graph is reaching for
the wrong tool — that coordination is already free inside the process; COORD
would only add an I/O round-trip and a schema to a problem that has neither
today.

### …one session is enough

**Claude Code subagents / the Task tool** — when a single Claude Code
session fans out to bounded subagents that return and the session finishes
before anyone else needs to know what happened, you don't need a durable,
cross-process ledger; the parent session's own context is the coordination
mechanism, and it works fine for exactly as long as that session is running.
COORD earns its cost when a *second* process — a different session, a
different agent, tomorrow's session picking up today's abandoned claim —
needs to find out what happened without that first session being alive to
tell it. (COORD's own fan-out guard, `record_child_attempt`, is built for
subagents that report back to an orchestrator holding a durable claim — see
[`examples/fleet-fan-out/`](../examples/fleet-fan-out) — which is a
complementary use, not a competing one: nothing stops a Claude Code session
from using the Task tool for the fan-out mechanics and COORD for the durable
record of what the fleet did.)

### …isolation, not shared state, is the problem

**git-worktree runners (vibe-kanban, claude-squad, Conductor)** — these
solve a different problem well: give each agent its own working tree so
concurrent agents never see each other's uncommitted changes underfoot. That
is orthogonal to what COORD does. Worktree isolation stops agents from
clobbering the same *files on disk at the same instant*; COORD's write-set
declaration (`examples/two-agents-one-file/`) stops two agents from
*deciding to work the same files* in the first place, which a worktree runner
cannot see or prevent — from inside separate trees, both agents' `git status`
looks clean right up until someone merges. If your actual pain is "two agents
stepped on each other's uncommitted edits," a worktree runner is the fix.
If your pain is "two agents both silently started the same job, and nobody
found out until the merge," that's COORD's problem, not theirs — and the two
are not mutually exclusive.

### …a to-do list is enough

**Markdown/issue-tracker task stores (Beads, Backlog.md, taskmaster-ai)** —
if what you need is a durable list of tasks with status fields an agent (or
a human) reads and edits, a flat file or an issue tracker is simpler, more
portable, and easier to inspect by hand than a SQLite ledger with a fenced
write API. COORD is a worse choice than a plain task list for a single agent
working through a checklist with no collision risk and no completion claim
that needs independent proof. What COORD adds — and what a status column in
a Markdown table cannot — is that "done" is not
just a field someone set: it is refused unless the declared proof is
actually in git's index (`examples/proof-gated-done/`), whatever type of
artifact it is, apart from the handful of kinds that cannot be tracked at
all (see the differentiator section above) — and "running" is not just a field someone
forgot to update: it is derived from a live, unexpired claim on every read.
If nothing in your workflow needs either guarantee enforced rather than
asked-for, the simpler store is the right call.

## What this page is not

It is not a benchmark, a feature matrix, or a claim that COORD is "better."
Each project above is well suited to the problem it was built for, and most
projects doing real multi-agent work will end up using more than one of
these categories together — a graph library to sequence one agent's steps,
a worktree runner to isolate its edits, and (if the problem grows to more
than one live agent working the same repository over more than one session)
something like COORD to keep the ledger of who claimed what honest. Pick
based on which failure mode you are actually trying to prevent.
