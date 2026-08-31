# The untrusted-actor guard audit

This document exists to be read *before* anyone adds a login screen, an account
system, or a second machine to this project. It answers one question, guard by
guard: **if `actor` stopped being a string the caller types and became something
a server verified, which of the guards already in this codebase would still be
doing work, which would become redundant, and which would turn out to have been
decorative all along?**

The reason to write this first is that authentication does not add safety on its
own — it moves the trust boundary. A login screen in front of a system whose
guards all assume a cooperating caller produces a system that is exactly as safe
as before, with a new and false impression of safety. The useful output of an
audit like this is not "add auth"; it is a list of the specific guards that would
have to be rebuilt on a verified identity, and the specific ones that could then
be deleted.

Every claim about a guard below cites the file it lives in. Where reading the
code did not settle a question, it is listed under
[Open questions](#8-open-questions) rather than asserted. Claims are labelled
**MEASURED** when they come from reading the source in this repository at the
current revision, and **MODELED** when they are reasoning about a system that
does not exist yet.

This is a document, not a system. Nothing here ships code.

---

## 1. Today's trust model, stated plainly

One machine. One trusted user. One checkout. Every process that writes to the
board was started by the same person, and **caller identity is an assertion**.

That last sentence is not a criticism of the design; it is the design, and the
code says so out loud. The claim-ownership guard's own docstring notes that "the
caller's identity is asserted, not proven -- that is the trust model the rest of
this module already uses"
(`src/coordharness/coord/coord_db.py`). The user-facing statement of the same
fact is the "Caller identity is an assertion, not authentication" paragraph in
[`security-and-privacy.md`](security-and-privacy.md).

Concretely, here is where identity comes from (MEASURED):

- A writer names itself by calling `register_session(conn, session_id, actor, ...)`
  in `src/coordharness/coord/coord_db.py`. There is no credential parameter. The
  function's only identity constraints are that the actor is non-empty, that a
  `lane:` prefix on the `session_id` agrees with the actor, and that an existing
  session row cannot be relabelled to a different actor.
- Over MCP, identity arrives either as explicit `actor` / `session_id` tool
  arguments or from the *client process's own environment* —
  `COORD_ACTOR`, `COORD_SESSION_ID`, `CLAUDE_CODE_SESSION_ID`, and the Codex
  equivalents — resolved by `resolve_identity()` in
  `src/coordharness/coord/ingest.py` and consumed by `_resolve_tool_identity()`
  in `src/coordharness/coord/mcp_coord_server.py`.
- The lane vocabulary itself is configuration: `configured_lanes()` in
  `src/coordharness/coord/config.py` reads `COORD_LANES` on every call and
  defaults to the two built-in lanes.

So the full identity chain is: an environment variable, or a function argument,
in a process the caller launched. Nothing at any layer compares that to a
credential, an OS user, a socket peer, or a signature. **The security boundary is
the operating system's process and file permissions on `coord.db`** — anyone who
can write that file is already inside every guard in this document, because the
guards are Python functions in the writer's own process, not a server the writer
talks to.

That is worth stating precisely, because it sets the ceiling on everything
below. In this architecture the control plane is a *library*, and a library
cannot defend against its own caller. A guard here can prevent a mistake, make a
misuse legible in the event log, or refuse an ambiguous request. It cannot
prevent a determined writer from doing anything the SQLite file format allows.

### What "server-verified actor" would mean

For the rest of this document, the hypothetical is narrow and deliberately
modest (MODELED): **the process that performs coordination writes is separate
from the process that requests them, and it derives `actor` from an
authenticated channel rather than from the request body.** Requests still arrive
over a local or remote transport; the difference is only that a caller can no
longer choose which actor it is.

That single change is enough to sort the guards, because it splits them cleanly
into three kinds: guards whose predicate is a *fact about the database* (these
survive), guards whose predicate is a *fact about the caller's identity* (these
become real for the first time, or become redundant), and guards whose predicate
is *something the caller states about itself* (these are the decorative ones).

---

## 2. Guard inventory

Each guard is rated:

- **Survives** — the guard's predicate does not depend on caller honesty; it
  keeps its full value under an untrusted actor.
- **Becomes real** — the guard is correctly written but currently checks an
  assertion; a verified `actor` upgrades it from a mistake-preventer to an
  enforcement point.
- **Becomes redundant** — the guard exists only to compensate for the absence of
  verified identity, and would be deletable.
- **Decorative** — under an untrusted actor the guard can be satisfied by the
  attacker at will, and provides no defence.

### 2.1 Compare-and-swap version fencing — *Survives (scope is narrower than it looks)*

**Where:** `src/coordharness/coord/coord_db.py`. Mutators that take an
`expected_version` argument read the row's `version`, refuse if it differs, and
write `expected_version + 1`. The `version` column is on `work_items`, `claims`
and `agent_sessions` in `src/coordharness/coord/schema.sql`.

**What it defends against:** the lost update. Two editors read a row, both write,
and the second silently erases the first. The fence turns that into a refusal
the caller must handle.

**Under an untrusted actor:** it survives completely, because its predicate is a
fact about the stored row, not about who is asking. An attacker cannot forge a
version number; it can only lose the race.

**The important caveat (MEASURED):** the fencing covers the *administrative
correction* verbs, not the hot lifecycle path. The functions that take
`expected_version` are the repair and reclassification ones — context backfill,
acceptance-contract repair, context-pointer correction, invalid-projection
reconciliation, policy-moot review closure, tier correction, parked-work resume,
blocked-work classification, and the blocked-resume-predicate migration. **`claim_work`,
`heartbeat_claim`, `release_claim` and `complete_claim` do not take an
`expected_version` at all**; their concurrency safety comes from the unique index
below and from being inside a single transaction. That is a defensible design —
the lifecycle verbs are state-machine transitions with their own precondition
checks rather than read-modify-write edits — but anyone who reads "the board uses
CAS fencing" and concludes the claim path is fenced would be wrong.

### 2.2 The one-live-claim unique index — *Survives*

**Where:** `src/coordharness/coord/schema.sql`:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS ix_one_held_claim ON claims(work_id)
  WHERE status IN ('running', 'paused', 'blocked');
```

**What it defends against:** two sessions simultaneously holding the same work
item. This is the strongest guard in the system precisely because it is not
Python: SQLite refuses the second insert regardless of what the caller believes,
asserts, or intends.

**Under an untrusted actor:** it survives fully, and it is the model the other
guards should aspire to. But note what it *is*: a mutual-exclusion guard, not an
ownership guard. It guarantees at most one live claim per row. It says nothing
about who that one holder is or whether they should be. An untrusted writer that
simply claims every open row first has violated no invariant and broken no index
— see [attack 3](#33-claim-squatting).

### 2.3 The claim-holder check — *Becomes real (today it stops accidents, not attackers)*

**Where:** `_assert_claim_holder_unlocked()` in
`src/coordharness/coord/coord_db.py`, called from inside the transaction of every
claim mutator, with a public read-only wrapper `assert_claim_holder()`.

**What it defends against:** a peer using a `claim_id` as if it were a
capability. The docstring is explicit that a claim id "is not a secret and was
never a capability: `claim` returns it, the board prints it, and handoff payloads
carry it," and that before this check a peer could block, park or complete
another session's work while the resulting row still read as the holder's own
action.

**Under an untrusted actor:** this is the guard that matters most and the one
most in need of a verified identity, because its predicate is *"the caller says
it is the holder."* It compares the caller-supplied `session_id` against the
holder recorded on the claim row. An untrusted writer states the holder's session
id and passes. Today the guard is genuinely valuable — it stops the accidental
cross-session mutation that the docstring describes, and it makes the intent of
every call legible — but it is not an authorization check. With a verified
`actor` it becomes one, and it is already written in the right place (one
choke point that both the CLI and MCP reach).

**Two specific weakenings to carry forward (MEASURED):**

*The session family.* If the caller's session id does not equal the holder's, the
check falls through to `_related_session_ids_unlocked()`, which treats two
sessions as one owner when they share an actor and *either* the same
`external_thread_id` *or* the same `worktree_id`, or when one session id is the
other's lane-suffixed form. Those fields are written at `register_session` time
and come from the client process's environment via `resolve_identity()` in
`src/coordharness/coord/ingest.py` — they are not tool parameters, but an
untrusted actor running its own client controls its own environment. So under the
untrusted-actor hypothesis, *claiming to be in the holder's session family is as
easy as claiming to be the holder*. Any future authentication work must make
family membership a server-side fact, not an environment-derived one, or the
family path becomes the way around the guard it sits inside.

*The system-caller escape hatch.* The same function accepts a `system_caller`
argument and, when it is one of `reaper:expired_lease`, `reaper:zombie_session`
or `handoff:ownership_transfer` (`CLAIM_MUTATION_SYSTEM_CALLERS` in the same
file), **returns before any ownership comparison at all**. This is a self-asserted
string that fully bypasses the guard. Its saving grace today is reach: a grep of
`mcp_coord_server.py`, `cli.py` and `agent_cli.py` finds no occurrence of
`system_caller`, so it is a Python-library parameter, not a wire parameter
(MEASURED). Anyone who can pass it already has in-process access, at which point
they can write the SQLite file directly. It is therefore not a live hole — but it
is a bypass that must not be promoted to any transport, and if the control plane
ever becomes a server, this argument has to become an internal call path rather
than a parameter.

### 2.4 Session/actor consistency — *Becomes redundant*

**Where:** `expected_actor_for_session_id()` and `_validate_session_actor()` in
`src/coordharness/coord/coord_db.py`, plus the relabel refusal inside
`register_session()`, and the mirroring checks in `_resolve_tool_identity()` and
`_resolve_process_bound_identity()` in
`src/coordharness/coord/mcp_coord_server.py`.

**What it defends against:** incoherent self-description. A session id prefixed
`codex:` may not be registered with `actor=claude`; an existing session row may
not be relabelled to a different actor; and for a handful of verbs the asserted
identity must equal the identity in the MCP client's process environment.

**Under an untrusted actor:** decorative *as authentication*, and that is not
what it is for. Nothing prevents an untrusted writer from registering a fresh
`codex:whatever` session with `actor=codex` and being, from the board's point of
view, Codex. What these checks buy is that an identity cannot be *internally
inconsistent* — which matters a great deal when identity is a naming convention,
because the review guard in §2.5 derives a reviewing lane by inverting it, and
`resolve_identity()` refuses outright rather than guess when both vendors'
session variables are present.

The `_resolve_process_bound_identity()` check is the clearest case of a guard
that exists only because identity is unverifiable: it binds an assertion to the
process environment because that is the strongest available proxy. Give the
system a verified `actor` and this whole family of checks — prefix agreement,
relabel refusal, environment matching — has nothing left to do. **It should be
deleted at that point, not kept "for defence in depth."** Keeping a consistency
check that reads like an identity check is how a system acquires a false sense of
its own boundary.

### 2.5 Lane inequality on review — *Decorative*

**Where:** `post_audit_verdict()` in `src/coordharness/coord/coord_db.py`, with
the supporting analysis in `src/coordharness/coord/review_integrity.py`.

**What it defends against:** self-approval. A `PASS` verdict requires an
unambiguous reviewer lane, requires the work's authoring lane to be
determinable via `_latest_claim_author_lane_unlocked()`, and refuses when they
are equal:

```
same-lane PASS is forbidden: reviewer and author are {author_lane}
```

`review_integrity.py` states the principle well: independence is lane
*inequality* with the author, never membership in one hardcoded pair, so a lane
added through `COORD_LANES` reviews on the same terms and still cannot clear
itself. The verdict path also carries real anti-replay machinery — a required
`operation_id`, a canonical `request_sha256`, and for a repair-cycle `PASS`, a
binding to the exact negative verdict event plus at least one evidence ref the
negative verdict did not carry.

**Under an untrusted actor: this is the most consequential decorative guard in
the system.** The lane is not an independent fact; it is a *component of the same
self-asserted identity*. A single untrusted writer registers `claude:a`, claims
and completes the work as `claude`, then registers `codex:b` and posts
`PASS` as `codex`. Every check above is satisfied. The lanes are unequal, the
operation id is fresh, the request hash is canonical, the evidence refs are
whatever the attacker wrote — and the work is now, on the record, independently
reviewed.

The anti-replay machinery is not wasted: it survives, because hashes and event
bindings are facts about the database. What does not survive is the *premise*,
which is that two lanes are two parties. Under a verified `actor` the premise
becomes true and this guard becomes one of the most valuable in the system.
Until then, dual-lane review is a guard against a lane's own carelessness, and
should never be described as a guard against a lane's bad faith.

### 2.6 The proof gate on completion — *Survives partially*

**Where:** `complete_claim()` in `src/coordharness/coord/coord_db.py`, supported
by `done_signal_satisfied()` and `done_signal_blocking_declaration()` in the same
file.

**What it defends against:** declaring work done with nothing to show for it.
The gate is layered, and each layer refuses rather than warns: the claim must
still be `running` and unexpired; no live local run may be attached; an
unresolved negative audit verdict blocks; unmet T0 review blocks unless a valid
operator-ok event is bound; the artifact kind must be proof-capable; and
critically, the work item must carry a **controller-declared `done_signal`** —
`complete_claim` refuses outright if `work_items.done_signal` is empty, and if an
explicit `artifact_path` is also supplied it must resolve, through `realpath`, to
the same file. `done_signal_satisfied()` then requires either that a
`coord:event:<id>` proof names an event that exists, or that the declared file is
actually present — and also carried by
git's index: `done_signal_custodied()` in `src/coordharness/jobs/status.py`
shells out to `git ls-files` and refuses completion until the file is staged.
**This custody leg covers every artifact type**, with one narrow exemption:
the suffixes named in `DEFAULT_CUSTODY_EXEMPT_SUFFIXES` (`.parquet`,
`.duckdb`, `.db`, `.joblib`, `.bz2`, `.backup`) are databases, dataset dumps,
serialized models and archives, which a git index cannot hold; those must
still exist, and nothing waives existence. `COORD_COMPLETION_CUSTODY_EXEMPT`
rebinds that list, and set to `*` disables the custody leg entirely — which is
the honest residual risk here: an operator (or anything that can set that
process's environment) can turn this leg off, exactly as `COORD_CLAIM_STRICT`
can move the claim-readiness leg. Before 0.1.0 the leg was scoped to `.md`
and every other suffix passed on existence alone, so a `.json` or `.html`
report could be declared done and never committed.
`done_signal_blocking_declaration()`
additionally reads the first twelve lines of a Markdown/text proof and refuses
a completion whose own artifact says `NOT READY`, `FAILED`, `BLOCKED` or
`INCOMPLETE`.

**Under an untrusted actor:** the *structure* survives — every one of these
predicates is a fact about the database or the filesystem, not a caller
assertion, and none can be satisfied by simply claiming it has been satisfied.
That is genuinely more than most of this inventory can say.

What does not survive is *sufficiency*. The proof is a file the same writer can
create, or an event the same writer can post. The blocking-declaration scan reads
twelve lines for four uppercase words and is trivially avoided by an artifact
that says nothing. So the gate reliably stops "done with no artifact" and stops
"done contradicted by its own artifact"; it does not and cannot establish that
the artifact is true. Under an untrusted actor it raises the cost of a false
completion from zero to "write a plausible file." Rank it as a strong
integrity guard and a weak adversarial one, and do not let a future design treat
a satisfied `done_signal` as evidence about the world.

### 2.7 The deferred-tool handshake — *Decorative (and fail-closed today)*

**Where:** `src/coordharness/coord/deferred_tools.py`.

**What it defends against:** exposing a heavy tool — currently the single name
`handoff_existing` — to clients that have not been vetted for it. Promotion of a
deferred tool requires the environment to supply
`COORD_DEFERRED_PROMOTION_MANIFEST_SHA256` matching a per-tool entry in
`ACCEPTED_PROMOTION_MANIFEST_SHA256`; a parallel client-profile attestation
compares `COORD_MCP_CLIENT_PROFILE_SHA256` against
`ACCEPTED_CLIENT_PROFILE_SHA256`.

**Measured facts, both worth stating plainly:**

1. **Both accept-lists are empty.** `ACCEPTED_PROMOTION_MANIFEST_SHA256`,
   `ACCEPTED_CLIENT_PROFILE_SHA256` and `ACCEPTED_CLIENT_PROFILE_ACTORS` are
   declared as `{}` in `deferred_tools.py` and are assigned nowhere else in the
   source tree. The consequence is that **no promotion can currently succeed and
   no client profile can currently attest** — `filter_deferred_tools` always
   leaves `handoff_existing` deferred, and `client_profile_attestation` returns
   state `absent` or `unaccepted`. This is fail-closed, which is the right
   direction to fail, but it means the handshake is an unexercised mechanism
   rather than an operating control, and no test of it can currently distinguish
   "correctly refused" from "cannot ever accept."
2. **The attestation is self-supplied on both sides of the comparison that
   matters.** The profile id and the hash both come from the caller's own
   environment. The only thing the caller does not control is the accept-list —
   which is a constant in the shipped source, so it is not a secret either. Under
   an untrusted actor with the ability to run its own client, a populated
   accept-list would be satisfiable by reading the source and setting two
   environment variables. This is a **capability-surface control** — it decides
   which tools a cooperating client sees — not an authorization control, and it
   should never be documented as one.

**One inconsistency to flag (MEASURED, low severity today):**
`client_profile_attestation()` validates the accepted actor with a hardcoded
`raw_accepted_actor in {"claude", "codex"}`, while the rest of the control plane
derives lanes from `configured_lanes()` / `COORD_LANES`. A deployment that
configures a third lane could never register an accepted client profile for it.
The empty accept-list makes this unreachable now, so it is a latent
inconsistency rather than a bug — but it is exactly the kind of hardcoded pair
that `review_integrity.py` went out of its way to avoid, and it should be
reconciled before the accept-list is ever populated.

### 2.8 The read-only projection — *Survives (with two precise caveats)*

**Where:** `src/coordharness/board/server.py` and
`src/coordharness/board/security.py`; the read-only database accessor
`connect_ro()` is in `src/coordharness/coord/config.py`.

**What it defends against:** the viewer becoming a write path, and a browser page
on another origin driving it.

The enforcement is real and layered (MEASURED):

- `do_PUT`, `do_PATCH` and `do_DELETE` are all bound to `_readonly`, which
  answers `405` with an `Allow: GET, HEAD, OPTIONS` header.
- `connect_ro()` opens the database with the `mode=ro` URI and then sets
  `PRAGMA query_only = ON` — belt and braces, and neither depends on the caller.
- The default bind is `127.0.0.1`; a non-loopback bind raises unless
  `allow_remote=True` *and* at least one explicit allowed host is given.
- `security.py` supplies a `Host` allow-list, an `Origin` check on unsafe
  methods, and a restrictive `Content-Security-Policy` with `form-action 'none'`
  and `frame-ancestors 'none'`.

**Under an untrusted actor:** the read-only property of *the coordination
database* survives, because it is enforced by SQLite rather than by routing. That
is the right place for it.

**Caveat one:** "read-only" describes `coord.db`, not the process, and it is
true only in the server's default configuration. `do_POST` is *not* bound to
`_readonly`: **three** routes accept POST. Two, `/api/v1/usage-actions` and
`/api/v1/provider-management`, mutate provider-account and profile state. They
are hedged carefully — loopback bind *and* loopback `Host` *and* a present,
matching `Origin`, plus a required `X-Coord-Usage-Action: v1` header, a JSON
content type, a bounded body, no query string, and an exact key-set match
against a declared action shape.

The third, `/api/native/action` (`server.py:1494`), is different in kind: it
is opt-in, gated behind the `COORD_NATIVE_OPERATOR_WRITES=1` environment
variable (`server.py:283`) and a `0o077`-clean, uid-owned operator-token file,
and when active it dispatches to `_post_native_operator_action` (`:1567`) →
`native_operator_action` (`:761`), which opens a **read-write** connection to
`coord.db` and calls `coord_db.post_operator_reassignment(...)` — a
coordination-database write from the board process itself, not just
provider/profile state. It is bearer-token gated
(`hmac.compare_digest`, `:1604`) and peer-checked with `is_loopback_bind`
against the bind address, the `Host` header, and the socket peer (`:1574-1576`),
and it is off by default — the shipped container image's `CMD` never sets the
flag (`docs/containers.md:18,95`). But when the flag and token are present,
the macOS menu bar *is* a write client:
`apps/menubar/Sources/Cockpit/Core/NativeOperatorTokenSource.swift:10` builds
this exact URL and
`apps/menubar/Sources/Cockpit/UI/NativeCockpitActionBroker.swift:9,37` POSTs
to it with the bearer token.

All three POST routes are a write surface on a server described as read-only,
and any hosted design must account for them separately — "no client writes"
holds only in the default configuration.

**Caveat two:** none of this is authorization. `host_allowed` and
`origin_allowed` establish *where the request came from*, never *who sent it*.
Any process on the machine can reach loopback. The projection is safe because it
cannot write the board, not because it knows its callers — and that distinction
is the whole subject of this document.

### 2.9 The policy pipeline — *Decorative by configuration*

**Where:** the pipeline is documented in
[`policy-pipeline.md`](policy-pipeline.md), which is admirably direct about its
own limits: six of the seven checks default to `warn` rather than `enforce`, so
in the shipped configuration "an agent that ignores its own warnings can still
push a write through six advisory checks in a row."

**Under an untrusted actor:** advisory checks are not guards. An attacker does
not read warnings. This is not a defect — the pipeline's stated purpose is
visibility, and its own documentation already draws the line between what it
does and the two structurally enforced guards (§2.3 and §2.6) that live outside
it. It is listed here so that a future reader counting "seven policy checks" does
not count them as seven defences.

### 2.10 Reserved event namespaces — *Survives*

**Where:** the public `post_event` path in
`src/coordharness/coord/coord_db.py`.

**What it defends against:** a forged operator sign-off. The public writer
refuses to mint an `operator_ok` event kind, refuses to mint an idempotency key
in the reserved `operator-ok:` namespace (explicitly to stop a squatter turning
the operator's next real sign-off into a replay collision — denial of the escape
hatch rather than forgery of it), downgrades an `actor=operator, trust=operator`
event to `trust=external`, and refuses `trust=system` outright. The `events`
table's `idempotency_key` carries a `UNIQUE` constraint in
`schema.sql`, and the MCP verdict path hardcodes `trust="agent"`.

**Under an untrusted actor:** survives at this layer, because these are
structural refusals in the writer rather than checks on who is calling. The trust
correctly relocates to *the typed human-only writer* that can mint those events —
which becomes a first-class authentication target the moment a second person can
write, and which this audit does not cover in depth (see
[Open questions](#8-open-questions)).

### 2.11 File modes on the database and its sidecars — *Survives (with a named residual)*

**Where:** `enforce_db_file_modes()` in `src/coordharness/coord/config.py`, called
from `connect()` and from `apply_schema()` in `coord/create_schema.py`.

**What it defends against:** disclosure of the whole board — acceptance text,
notes, handoff bodies, session labels — to any other account on the machine. §5
names the filesystem as the trust boundary, which makes the mode on these files
the boundary itself.

**What was measured, before the change.** A database created by
`bootstrap_database()` under the common `022` umask, then written to:

| file | mode |
|---|---|
| `coord.db` | `0644` |
| `coord.db-wal` | `0644` |
| `coord.db-shm` | `0644` |

Nothing in the codebase set a mode or a umask on any of the three. The `-wal`
file holds committed page images and `-shm` the shared index into it, so both
disclose exactly what the database does. All three were world-readable.

**Two further measurements shape the fix**, and both say that a single chmod at
creation would not have held:

1. SQLite deletes `-wal` and `-shm` on the last clean close of a database and
   creates them again on the next write. A chmod applied to the sidecars alone
   is gone after one open/close cycle — measured directly: sidecars set to `0600`
   came back at `0644`.
2. SQLite derives the creation mode of a sidecar from the **main database file's**
   mode. With the database at `0600`, the recreated `-wal` and `-shm` were `0600`
   without anything chmod-ing them. This is why the enforcement is placed on the
   database, and why it runs *before* the connection opens as well as after.

**Under an untrusted actor:** survives, in the sense that it is a mode on a file
and not a check on a caller. It is also the only guard in this document that
defends against the attacker §5 declares out of scope, which is worth stating
plainly: it does not make a local user who can write `coord.db` harmless, it
removes the far larger class of local users who could previously merely *read* it.

**Residual exposure, stated rather than papered over:**

- **Creation is not atomic with the tightening.** The Python DB-API offers no way
  to open a database file with an explicit mode, so SQLite creates it at the umask
  default and the chmod follows. A new database is world-readable for the interval
  between creation and the end of schema application. Measured, and asserted in
  `tests/test_db_file_modes.py`.
- **A read-only open does not tighten anything.** `connect_ro()` is a read
  accessor and remains one, so a process that only ever reads leaves a loose
  database loose.
- **A database this process does not own cannot be chmod-ed.** The enforcement
  reports the refusal rather than raising, because refusing to open the board
  would be a worse outcome than opening it. Callers that need the guarantee must
  read the returned report; nothing currently does.
- **The containing directory's mode is not enforced.** A `0700` parent would close
  the creation window above; that is a deployment decision this code does not make.
- **Any pre-existing sidecar of a database opened only through some path that does
  not call `connect()`** keeps whatever mode it has. The census of such paths was
  not performed. MODELED.

### 2.12 `operator_ok` authority — *Survives, and the terminal channel is fixed*

**Where:** `record_operator_sign_off()` in `src/coordharness/coord/coord_db.py`,
reachable only from the `sign-off` verb in `coord/cli.py`.

**What it defends against:** an agent minting the one event that substitutes for
an opposite-lane verdict. §2.10 establishes that the public writer refuses; this
row is the audit of the writer the trust relocates *to*.

**The census (MEASURED, and pinned in `tests/test_operator_ok_authority.py`).**
Every `INSERT INTO events` in the codebase — 33 sites — lives in
`coord/coord_db.py`; no other module writes the table. Of those, exactly one
spells `operator_ok` as the kind, inside `record_operator_sign_off`. Three write a
`kind` that is not a literal:

| writer | how its `kind` is chosen | can it be `operator_ok`? |
|---|---|---|
| `post_event` | the caller's parameter | No — refused by name, and `trust='system'` is refused separately |
| `post_typed_controller_source_event` | one of two module constants selected by an `event_type` argument | No — the caller never supplies the kind |
| `_insert_completion_receipt_unlocked` | `f"{actor}_done"`, actor constrained to the lane set | No — the literal `_done` suffix bounds it |

**A sign-off is the event *plus* the binding.** `_has_valid_operator_ok_unlocked`
reads `work_items.operator_ok_event_id` and then re-validates the event it points
at: kind, `actor='operator'`, `trust='system'`, and a payload carrying
`writer_contract=operator_ok.v1` and a digest over the work row's own identity and
acceptance. `upsert_work` refuses `operator_ok_event_id` as a typed receipt field.
So a caller has to forge both halves, and the agent-facing writers can produce
neither: an agent that mints the most operator-shaped event `post_event` will
allow, and binds it by hand, still does not hold a sign-off (asserted).

**Could a compromised or merely buggy agent-side caller reach the minter?**
`record_operator_sign_off` is called from exactly one place in the source tree,
`coord/cli.py`. It is absent from the MCP server, from `agent_cli.py`, and from
the board. The MCP surface mentions `operator_ok` only through the read-model
validator. A *buggy* agent-side caller therefore cannot reach it at all; there is
no supported surface that leads there. A *compromised* one that can import
`coord_db` and open the database is outside every guard in this document by §5,
and this one is no exception — the function itself says so.

**The defect, which was in the channel and not in the writer (MEASURED, then
FIXED).** The human-only property does not come from the writer, which cannot
see who called it; it comes from `_read_controlling_terminal_confirmation` in
`coord/cli.py` asking the controlling terminal. That reader used to open
`/dev/tty` for updating — `open("/dev/tty", "r+", buffering=1)`. **A terminal
is not seekable, and a buffered read/write stream requires seekability, so
that open raised `io.UnsupportedOperation` on every terminal** — measured
against both a pty device node and a genuine controlling terminal obtained via
`pty.fork()`. Because `io.UnsupportedOperation` subclasses `OSError`, the
existing `except OSError` converted it into `OperatorConsentUnavailable`: the
channel told a person sitting at a real terminal that there was no terminal.
Every test of that reader substituted `open`, so the real call had never run
against one — which is exactly how the defect reached this branch undetected.

The consequence was fail-closed, not fail-open — nobody could sign, rather
than anybody could — so it widened no attack surface. But it meant the escape
hatch around the review gate was a dead letter on this platform, and the claim
that "the operator can always override" did not hold.

**Fixed on this branch, commit `c818f79`, two commits before this document's
own commit `dec5f4a` first described the defect.** `coord/cli.py:198-200` now
opens two one-directional handles instead of one `"r+"` — `open(device, "r")`
for the read, `open(device, "w")` for the write — so neither handle asks for
seekability and the open no longer raises against a real terminal. No `"r+"`
open remains in the file. The regression test added in the same commit,
`test_a_real_terminal_can_actually_answer` in
`tests/test_operator_sign_off.py`, is deliberately not a mock: it calls
`os.openpty()` to allocate a real controlling terminal and reads a real
answer through it, because a mock is exactly what let the original defect
pass a full test suite undetected — substituting `open` proves the writer's
logic, not the channel's compatibility with an actual terminal.
`tests/test_operator_ok_authority.py` separately pins the platform property
(a terminal opened `"r+"` is not seekable) as a fact about terminals, not
about `cli.py`, so it stays green regardless of which handle strategy the
reader uses and documents why the two-handle fix was necessary.

**The precondition that does hold (MEASURED).** A process the harness spawns for
an agent has no controlling terminal at all: `open("/dev/tty")` fails with
`ENXIO` from inside an agent-run subprocess. That is the premise the guard rests
on, and it is asserted rather than assumed, so a future runner that gives agent
processes a terminal makes the test fail instead of silently making the guard
weaker. An agent that allocates its own pty and drives both ends is impersonation,
and §5 already places it out of scope.

### 2.13 Guards this audit deliberately does not count

`_validate_existing_db_file()` in `src/coordharness/coord/config.py` refuses a
zero-byte file, a non-SQLite header, a valid SQLite file with no tables at all,
and a database whose tables do not include the coordination sentinels. The
`host_id` columns added by `_ADD_COLUMNS` in
`src/coordharness/coord/create_schema.py` and indexed by
`src/coordharness/coord/migrations/004_host_identity.sql` make a `pid` mean
something on a named machine.

Neither is a security guard. The first is a misconfiguration guard — it stops you
pointing the harness at the wrong file, which is a correctness and data-loss
concern. The second is a *prerequisite* for multi-machine work, called out as
such in [`roadmap.md`](roadmap.md) under "Near": it is "the one schema change
every multi-machine path downstream needs and can't retrofit later without a
migration touching live data." Recording which host a session ran on is
necessary before liveness can mean anything across machines; it is not evidence
about who that session was.

### 2.14 Summary table

| # | Guard | Where | Verdict |
|---|---|---|---|
| 2.1 | CAS version fencing | `coord/coord_db.py` | Survives — but covers correction verbs, not the claim path |
| 2.2 | One-live-claim unique index | `coord/schema.sql` | Survives — mutual exclusion, not ownership |
| 2.3 | Claim-holder check | `coord/coord_db.py` | Becomes real — family path and `system_caller` must be closed |
| 2.4 | Session/actor consistency | `coord/coord_db.py`, `coord/mcp_coord_server.py` | Becomes redundant — delete, don't keep |
| 2.5 | Lane inequality on review | `coord/coord_db.py`, `coord/review_integrity.py` | **Decorative** — one writer can be both lanes |
| 2.6 | Proof gate on completion | `coord/coord_db.py` | Survives partially — stops absent proof, not false proof |
| 2.7 | Deferred-tool handshake | `coord/deferred_tools.py` | **Decorative** — and fail-closed with empty accept-lists |
| 2.8 | Read-only projection | `board/server.py`, `board/security.py` | Survives, by default — but three POST routes, and origin is not identity |
| 2.9 | Policy pipeline | `coord/` + [`policy-pipeline.md`](policy-pipeline.md) | Decorative by configuration — advisory, by design |
| 2.10 | Reserved event namespaces | `coord/coord_db.py` | Survives — trust relocates to the human-only writer |
| 2.11 | File modes on the db and sidecars | `coord/config.py`, `coord/create_schema.py` | Survives — measured `0644` before, `0600` now; residuals named |
| 2.12 | `operator_ok` authority | `coord/coord_db.py`, `coord/cli.py` | Survives — the terminal channel's `"r+"` defect is repaired in `cli.py:198-200` |

---

## 3. Attacks that become possible the moment a second person can write

Ranked by expected damage, on the assumption that the attacker can run its own
client against the same `coord.db` but cannot yet edit the file byte-by-byte.
(If it can, this list is moot and only file permissions matter.) All MODELED.

### 3.1 Verdict laundering — *highest*

Register two sessions in two lanes, author in one, `PASS` in the other. Defeats
§2.5 completely and, through it, the T0 review gate inside `complete_claim`
(§2.6), because that gate's question is whether a passing verdict exists — not
whether the reviewer was a different person. The result is a permanent,
plausible record of independent review that never happened. This ranks first
because it is cheap, needs no race, leaves a *clean* audit trail rather than a
suspicious one, and corrupts the artifact the whole review tier exists to
produce.

### 3.2 Claim-family impersonation

Complete, block or park another agent's live work by presenting the holder's
session id, or by joining its session family through a matching
`external_thread_id` or `worktree_id` (§2.3). The recorded action reads as the
holder's own — which is precisely the trace-loss the claim-holder guard was
written to fix, reintroduced from the other side. Ranks second because it
produces silent, misattributed state changes on work someone else is actively
doing.

### 3.3 Claim squatting

Claim every open row and renew the leases. No guard is violated: the unique index
(§2.2) is satisfied by one holder per row, and that holder is the attacker. The
reaper's expiry path is the only recovery, and heartbeats defeat it. A pure
denial of service, ranked third because it is loud and self-evident — the board
shows exactly who holds everything — where 3.1 and 3.2 are quiet.

### 3.4 False completion

Write a plausible artifact at the declared `done_signal` path, or post the event
a `coord:event:` proof names, and close the row (§2.6). The gate is satisfied
honestly; the artifact is fiction. Ranked below the first three because it
requires the attacker to already hold the claim, and because the artifact
persists as evidence that can later be read and disputed.

### 3.5 Board text as an injection channel into other agents

Already documented in
[`security-and-privacy.md`](security-and-privacy.md#the-trust-model) under "Board
text is data for the reading agent, not instructions it follows." A second writer
makes this a live channel rather than a hypothetical one: the board is the one
surface every lane is guaranteed to read. Ranked here rather than higher because
the mitigation is agent-side discipline and is already written down; the change a
second writer makes is to the *likelihood*, not to the mechanism.

### 3.6 Metadata disclosure by reading

Anyone who can write can read. `coord.db` holds work titles, acceptance text,
notes, handoff bodies, `cwd` values, and an event log — the shape of what is
being worked on and by whom. As [`security-and-privacy.md`](security-and-privacy.md)
puts it, the database is a file, not a vault. Ranked last only because it is
passive; for some deployments it would rank first.

### 3.7 Not in this list, and why

`system_caller` (§2.3) is excluded because a grep of the MCP, CLI and agent-CLI
surfaces finds no path that passes it (MEASURED); reaching it requires
in-process access, at which point direct file writes are available and the whole
model collapses. It belongs on the checklist below, not on this list.

---

## 4. What a hosted or shared profile would actually require

A checklist, ordered so that each item is executable and verifiable on its own.
This is the price of a login screen — not the login screen. All MODELED.

**Identity and transport**

1. Separate the control plane from its callers. Today the writer *is* the
   caller's own process. Until a write crosses a boundary the caller does not
   control, no amount of checking inside that process is enforcement.
2. Authenticate the channel, and derive `actor` from it. The request body must
   stop being an identity source.
3. Remove the environment-variable identity path (`COORD_ACTOR`,
   `COORD_SESSION_ID`, `CLAUDE_CODE_SESSION_ID` and the Codex variables, via
   `resolve_identity()` in `coord/ingest.py`) from any authenticated deployment,
   or scope it explicitly to the single-host profile. Two identity sources means
   the weaker one is the real one.
4. Make lane a property of the authenticated principal, not a prefix on a string
   the caller chose. This is the single change that converts §2.5 from decorative
   to load-bearing.
5. Mint `session_id` server-side. A caller that cannot choose its session id
   cannot join another session's family.

**Authorization**

6. Add an explicit authorization layer — currently there is none, only ownership
   comparisons. Decide and write down who may claim which rows, who may post a
   verdict on whose work, and who may mint an operator sign-off.
7. Make session-family membership a server-side fact. Delete the
   `external_thread_id` / `worktree_id` widening in
   `_related_session_ids_unlocked()` or re-derive it from authenticated
   principals (§2.3).
8. Remove `system_caller` as a parameter. Reaper and handoff ownership transfer
   should be internal call paths that cannot be named from outside (§2.3).
9. Delete the consistency checks that verified identity makes redundant (§2.4)
   rather than keeping them as decoration.

**Data and tenancy**

10. Scope the database per tenant. `coord.db` has no tenant column and no
    row-level filter; every read sees everything.
11. Decide a retention and disclosure policy for the freeform fields — titles,
    acceptance text, notes, handoff bodies, sidecar `owner`/`script`/`step` —
    that callers can fill with anything, per
    [`security-and-privacy.md`](security-and-privacy.md).
12. Re-examine the three POST routes on the board server (§2.8) under a model
    where loopback no longer implies trust.

**Integrity**

13. Extend `expected_version` fencing to the lifecycle mutators, or write down
    why the transaction plus the unique index is sufficient for them (§2.1).
14. Record the authenticated principal on every event, distinctly from the
    asserted actor, so that after any incident the two can be compared.
15. Reconcile the hardcoded `{"claude", "codex"}` actor check in
    `client_profile_attestation()` with `configured_lanes()` before the
    accept-lists are ever populated (§2.7).
16. Rate-limit claims per principal, or claim squatting (§3.3) is unbounded.

**Prerequisites already in place**

17. `host_id` on `runs` and `agent_sessions` has landed
    (`coord/migrations/004_host_identity.sql`). Multi-machine liveness has the
    column it needs; it does not yet have the identity.

---

## 5. Non-goals

Stated explicitly, because a threat model that does not say what it declines to
defend will be read as having defended it.

- **This project is not becoming a multi-tenant service**, and this document is
  not a proposal that it should. It is the audit that must exist before that
  question can be answered honestly.
- **Defence against a local user who can write `coord.db` is out of scope.** That
  user is inside every guard here by construction. File permissions are the
  boundary; nothing in Python can substitute for them.
- **Defence against a compromised agent runtime is out of scope.** If the process
  the harness trusts is executing an attacker's instructions, the harness's own
  checks are being run by the attacker.
- **Cryptographic integrity of the event log is out of scope.** Events are
  append-only by convention and by writer discipline, not by signature or hash
  chain. Nothing here would detect after-the-fact tampering by someone with file
  access.
- **Encrypting `coord.db` at rest is out of scope.** It is a plain SQLite file
  and should be protected as one.
- **Confidentiality between lanes is out of scope, now and probably always.**
  Both lanes are meant to read everything; that is the point of a shared board.
- **This document does not authorize or design an authentication feature.** It
  enumerates what such a feature would have to carry. Choosing a mechanism is a
  separate decision with its own review.

---

## 6. How to keep this document honest

The failure mode for a document like this is that a guard gets strengthened, or
weakened, and the audit silently stops describing the code. Three habits:

- When a guard listed in §2 changes, change its row here in the same edit. A
  verdict of "decorative" is a claim about current code, not a permanent
  property.
- When a new guard is added, classify it against the §2 rubric — does its
  predicate depend on a fact about the database, a fact about identity, or the
  caller's own statement? — before writing its documentation, not after.
- Treat every "survives" in this document as the weaker claim it is: the guard
  survives *this* hypothetical, in which the attacker speaks only through the
  supported interfaces.

---

## 7. Relationship to the other documents

[`security-and-privacy.md`](security-and-privacy.md) states the trust model for
someone deciding whether to run this on a machine that holds real work. This
document is the engineering audit underneath that statement, aimed at someone
proposing to change it.

[`policy-pipeline.md`](policy-pipeline.md) documents the advisory checks and is
already explicit that they are advisory; §2.9 does not restate it, only places it
in the inventory.

[`roadmap.md`](roadmap.md) carries the multi-machine direction and the `host_id`
prerequisite; this document is the gate that direction has to pass through.

---

## 8. Open questions

Listed because reading the code did not settle them, and a confident wrong answer
in a threat model is worse than an acknowledged gap.

1. **~~File permissions on `coord.db`.~~ Answered — see [§2.11](#211-file-modes-on-the-database-and-its-sidecars--survives-with-a-named-residual).**
   Measured: nothing set a mode or a umask, and the database, `-wal` and `-shm`
   were all created `0644`. They are now held at `0600`, enforced on the database
   file because SQLite copies that mode onto the sidecars it recreates. What
   remains open is narrower and is stated as residual exposure in §2.11: the
   non-atomic creation window, read-only opens, unowned databases, the containing
   directory's mode, and any open path that does not route through `connect()`.
2. **~~The typed human-only writer.~~ Audited and repaired — see [§2.12](#212-operator_ok-authority--survives-and-the-terminal-channel-is-fixed).**
   Measured: one minter, one caller, no agent-facing surface reaches it, and a
   sign-off requires both an event and a binding that the agent-facing writers can
   produce neither half of. The audit also found that the human-only *channel*
   was inoperable — the reader opened a terminal in a mode no terminal supports,
   so it refused a real operator. That was fail-closed, and the repair (two
   one-directional handles instead of one `"r+"`) landed on this branch,
   `cli.py:198-200`, commit `c818f79`. What remains open, as a design question
   rather than a defect: whether the channel should also record which terminal
   answered, and whether a sign-off should expire.
3. **Clock handling in the lease and reaper paths.** Expiry comparisons use a
   database-derived timestamp; whether an untrusted writer can influence it, and
   whether skew can cause a live claim to be reaped or an expired one to persist,
   was not tested.
4. **Reachability of the session-family widening over MCP.** The family fields
   are environment-derived rather than tool parameters (§2.3). Whether a specific
   supported client configuration lets an attacker set them to another session's
   values was reasoned about, not demonstrated. It should be tested before being
   relied on in either direction.
5. **Whether any guard in §2 is currently unexercised in the way §2.7 is.** The
   empty accept-lists were found by grepping for their names. No systematic sweep
   for other constants that make a check unreachable was performed.
6. **The board's POST routes.** All three: their input validation was read and
   is careful. The downstream effect of the third, `/api/native/action`, is
   traced in §2.8 (`coord_db.post_operator_reassignment`, a `coord.db` write);
   the other two's effects on provider and profile state were not traced, so
   §2.8 makes no claim about what a successful call to either of them can
   change.
7. **Whether `roadmap.md` states this document as a hard gate.** The roadmap
   carries the multi-machine direction and the `host_id` prerequisite; a literal
   "the threat model must precede authentication" sentence was not found there.
   The gate is asserted by this document and by the reasoning in §1, and should
   be written into the roadmap explicitly if it is to bind.
