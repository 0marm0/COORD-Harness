# Security and privacy

This document is for two readers: someone deciding whether to run `coordharness` on a
machine that also holds real work, and someone extracting their own internal tool from a
private codebase who wants to know what a public repository must never contain. Both
questions turn on one fact: this is a single-host coordination layer, not a
hosted or multi-tenant service.

## The trust model

`coordharness` coordinates trusted processes on one machine, normally under one user
account and against one checkout. It has no account system, caller authentication, or
tenant isolation. The optional web board is a loopback, read-only projection; it does not
turn the product into a trusted network service.

**Lifecycle authority is local.** The board is a SQLite file (`coord.db`) that trusted
writer processes open in WAL mode on the same filesystem. MCP uses a local stdio child
process. The optional `coord-board` opens SQLite read-only and serves a versioned snapshot
on loopback. Operating-system process and file permissions are the primary access boundary;
the HTTP viewer adds no authorization boundary of its own.

**The database is a file, not a vault.** `coord.db` holds sessions, claims, work-item rows,
run records, and an event log — the shape of who did what, and when. It is not designed to
store source bodies, production data, prompts, or secrets, but work-item and event fields
contain caller-supplied text. A careless or compromised caller can put sensitive material
there. Treat the file as sensitive project metadata, keep it out of source control, and
protect it according to the data callers may have written.

**Caller identity is an assertion, not authentication.** When a process claims a piece of
work, it reports its own session id and actor name as part of the call, the way a shell
script reports its own `argv[0]` — nothing on the other end verifies that against a
certificate, a token, or an OS-level identity. That's reasonable when every caller is a
trusted process started by the same person, and stops being reasonable the instant a caller
might not be trusted — exactly the assumption that breaks if the board is exposed past the
machine it runs on.

**Board text is data for the reading agent, not instructions it follows.** Every freeform
field a caller can set — a work item's title or acceptance text, a note, a handoff body — is
exactly as trusted as the caller identity two paragraphs up: an unauthenticated assertion,
here carried as prose instead of a session id. An agent that reads the board (Claude, Codex,
or any other coordinated process) must treat that text as untrusted input to interpret, never
as a command to execute on the strength of appearing inside a work item. For example, an
acceptance-text field that reads "...and also run `rm -rf`" is not consent to run that
command; a compromised or careless caller can write anything into a title or note, the same
way it can write anything into a work item body per "The database is a file, not a vault"
above. The board is the one place in this system every lane is guaranteed to read, which
makes it the natural channel for this failure — the same class as a web page's text steering
a browsing agent — and the reason to call it out here rather than assume it follows from the
caller-identity point above.

**Do not expose this beyond localhost.** Nothing here does authentication, per-tenant
isolation, or transport encryption. The board server defaults to loopback and requires a
two-part explicit opt-in for non-loopback binding; that opt-in does not make remote use safe.
MCP is stdio rather than an HTTP listener. Do not weaken or work around those controls,
share `coord.db` over a network mount, or publish the viewer through a proxy. Multi-machine
or multi-tenant coordination requires real identity, authorization, and tenant-scoped data;
that product does not exist here.

**Before proposing to change any of this, read the guard audit.**
[`threat-model.md`](threat-model.md) works through the trust model above guard by guard —
the claim-ownership check, the one-live-claim index, lane inequality on review, the
completion proof gate, the deferred-tool handshake, the read-only projection — and asks of
each one whether it would still be doing work if `actor` were verified rather than asserted.
Some survive unchanged, some become enforcement points for the first time, and some turn out
to be decorative the moment a second person can write. It also lists what a hosted or shared
profile would actually require, as a checklist. That audit is the gate for every
multi-machine idea in this project, including adding authentication itself: a login screen
added before it moves the trust boundary without establishing which guards move with it.

## What the harness writes, and what it never writes

Writes fall into two places, kept deliberately narrow. **The board** holds structured rows:
work-item text a person or agent typed when creating an item, claim and lifecycle events,
run metadata (a process id and its start time, never its output), and a policy-check log of
which checks ran and what they decided. **Job-progress sidecars** (`job_progress/*.json`)
are one small file per long-running job. The standard `coord-jobs` path records bounded
operational metadata such as work/job/run identifiers, status, progress, process identity,
timestamps, attempt/fence information, and a short step. The low-level writer also accepts
caller-supplied `script`, `kind`, `owner`, and process-pattern fields; those are metadata, but
they can still disclose what a job is doing if a caller puts sensitive text in them.

The standard launcher does not persist full argv, stdout, environment dumps, or output file
bodies. Run records keep process identity and bounded metadata; job output belongs in the
job's artifact. Freeform titles, notes, events, sidecar step/owner/script fields, and other
caller-supplied text still require care: the schema cannot prevent a caller from pasting a
secret into them, and direct users of the low-level writer must sanitize those fields.

This is a deliberate privacy transform, not an accident of what happened to be convenient:
**bounded metadata and opaque references, not output bodies.** Compare the
sidecar's actual fields to what a naive version might log instead — the exact command that
launched the job (a flag value can be a credential or a customer identifier), the job's full
stdout (it can carry anything the job printed), or the absolute path it's running from
(which routinely encodes a real username, as `/Users/<name>/...` or `/home/<name>/...`
always does). Telemetry is exactly the writing nobody reviews carefully in the moment —
generated in passing while the interesting work happens elsewhere — which makes it the
easiest place for something sensitive to end up persisted without anyone deciding to put it
there. The structured sidecar stays narrow even though event text remains caller-controlled.
Hold any new sidecar field or event-log column to the same test: can it carry a command line,
file body, credential, or path with a username, and if so what rejects or redacts it?

## Provider accounts, usage, and machine telemetry

Claude Code and Codex own their provider authentication. COORD does not need, copy, or
persist their passwords, browser cookies, API keys, or refresh tokens to coordinate work.
With no `COORD_USAGE_DASHBOARD_URL`, the board uses a current-user local fallback. It reads
only bounded state beneath `~/.claude` and `~/.codex`, calls official `claude auth status --json`, and calls official `codex app-server` account and rate-limit reads. Local CLI history
is always partial and noncanonical. Claude current quota remains explicitly unavailable; Codex
quota is canonical only when app-server returns current windows. A configured URL instead uses
the validated fixed-loopback upstream. A UI label that says an account is connected is not
evidence that quota or cost history is complete. Local snapshots have a 30-second TTL:
concurrent cold calls coalesce behind one refresh, bounded waiters receive `warming`, and an
expired last-good snapshot is marked `stale` while one background refresh thread runs. Forced refresh remains
bounded and may honestly return warming or stale. Local login-open actions are unsupported and
return HTTP 501; authentication remains in the official provider clients.

Usage records, when supplied, belong in a separate local usage ledger, not `coord.db`.
Provider profile metadata defaults to `~/.coord/provider-profiles.json`; routing policy
defaults to `~/.coord/provider-routing.json`. `COORD_PROVIDER_PROFILES_PATH` and
`COORD_PROVIDER_ROUTING_PATH` override those locations. The files are written atomically with
mode 0600 and contain metadata and policy, not provider secrets.
Account keys should be opaque local labels, and cost estimates must remain distinguishable
from provider-billed amounts. `coord route` reads that ledger and returns advice with coverage
disclosures; it never changes a work item's assignee or launches a provider client.

System telemetry is also a local projection. With no upstream configured, the board uses the
built-in dependency-free collector: CPU and memory use psutil when already installed or bounded
macOS probes, GPU uses IOAccelerator device utilization on macOS, disk occupancy uses Foundation
available-for-important-usage capacity, and disk rates require two monotonic I/O-counter samples.
The collector is sampled by the board and briefly cached; it is not an installed daemon. An
explicit `COORD_SYSTEM_TELEMETRY_URL` switches to a credential-free loopback upstream.

Machine metrics can disclose workload patterns and capacity even without filenames or process
argv. Keep telemetry on loopback, do not commit captures, and treat unsupported or failed metrics
as unknown. Never replace an unavailable GPU or disk metric with a guessed value or zero.

The native preview stores presentation preferences and last-good read models in the user's
normal application support/defaults locations. Those caches are not lifecycle authority and
should be removed during a privacy-sensitive uninstall.

## What a public repository must never contain

Treat this as an absolute list, not a starting point to negotiate down from — a determined
reader can turn any one item into a real finding about the private system it came from, and
none of them are needed to demonstrate or use the harness.

- **Live databases** — not a real `coord.db`, not a trimmed copy. Even trimmed, it carries
  real work-item titles and a real shape of who worked on what. Ship a schema and a seed
  script for a synthetic one instead.
- **Real work items** — titles and acceptance text someone typed about actual work are, by
  definition, about something real. Examples should describe invented work — a payments
  service, a migration script — never a paraphrase with the names filed off.
- **Memories, notes, or telemetry bodies** — freeform text about what an agent did is the
  highest-density leak surface here; prose accumulates context no schema has a field for.
  It's why the porter strips every docstring and comment rather than redacting the sensitive
  ones (see [`extraction.md`](extraction.md)).
- **Absolute host paths** — `/Users/<name>/...` or `/home/<name>/...` discloses a real
  username and directory layout. Every path here is relative or derived at runtime.
- **Private, real-data, or unreviewed screenshots** — a UI capture can frame sensitive
  data even when its purpose is only to demonstrate layout. This repository permits only
  fixed-clock captures generated from its explicit synthetic seed, with dimensions,
  hashes, capture method, and synthetic status recorded in
  [`assets/provenance.json`](assets/provenance.json). The publication gate validates
  those bytes and declarations; it cannot sanitize an arbitrary image.
- **Private or source-derived signing values** — developer team identifiers, provisioning
  profiles, app-group identifiers, private bundle identifiers, API keys — belong nowhere here
  even when expired; they still teach a reader the private system's naming. Neutral
  `org.coordharness...` bundle identifiers are clean-room build placeholders, not signing
  credentials or a distribution claim.
- **Fixtures derived from real records** — swapping a few tokens in a real item isn't
  synthetic data; unswapped structure and phrasing still identify the source. Fixtures here
  come from an explicit schema, never an edited real example.

## The publication gate: five checks

Nothing above is enforced by discipline alone. `tools/extract/gate.py` runs before anything
is published, built as an allowlist over what's present rather than a denylist of known-bad
content — a denylist only fails on a category someone thought to write a pattern for. An
allowlist inverts that: a file is guilty until it's accounted for.

| Check | Verifies | Failure means |
|---|---|---|
| **coverage** | Every path in the exact Git candidate is in the port manifest, the authored-files list, or a short infrastructure list. | An undeclared file — a build artifact or something added without a declared path. |
| **fidelity** | Every ported file, reproduced from its pinned private-source commit blob, matches the candidate byte-for-byte. | The declared transform no longer explains the candidate or its pinned source lineage. |
| **patterns** | A denylist of shapes — home paths, credential-like tokens, emails, known domain terms — over the exact candidate bytes. | Something typed directly into a file the first two checks would not catch. |
| **shape** | No symlinks or bytecode, bounded sizes, UTF-8 text, and only provenance-registered PNGs that pass strict signature, chunk, CRC, metadata, and trailing-byte checks. | An undeclared or malformed carrier that could evade the text scans or conceal appended data. |
| **history** | Every reachable path, blob, commit message, author, and committer passes the release rules. | Sensitive content or identity remains reachable even though the current tree looks clean. |

Run the public structural checks from the repository root during development. After the
candidate has a clean, final commit, include the reachable-history check:

```bash
python tools/extract/gate.py
python tools/extract/gate.py --history
```

Fidelity additionally needs an authorized private source repository and
non-published manifest. It reads the manifest's pinned commit blobs rather than
mutable source working-tree bytes:

```bash
python tools/extract/gate.py \
  --source-root /path/to/the/private/repository \
  --source-manifest tools/extract/manifest.private.json \
  --vocabulary tools/extract/vocabulary.json \
  --history
```

Without all three authorized private inputs, fidelity reports itself as skipped rather than
silently claiming a pass it never checked. History likewise reports skipped unless `--history`
is present. A contributor without the original private repository cannot run fidelity, by
design. Only the combined maintainer run may report `PUBLISHABLE`; an exit-zero subset is a clean
candidate. Publication still requires the explicit rights and maintainer approvals in
[releasing](releasing.md).

## Why a fresh `git init`, not a history rewrite

The tempting way to split a subsystem out of a private monorepo is a history filter —
`git filter-repo`, `git subtree split` — run against the paths being kept. That was rejected
here.

History is a leak surface independent of the working tree at HEAD. A commit carries the real
author's name and email regardless of the final file content. A file added and deleted three
commits later is gone from HEAD but still sitting in the pack, reachable with `git log --all`
— a path filter doesn't chase that down, and a determined reader will. Commit messages are
prose about internal decisions, and a message naming a real incident survives a path filter
exactly as well as the code change it describes, because the filter decides which paths a
commit touches, never what the commit *says*. Rewriting history instead — scrubbing
messages, purging deleted blobs — narrows this but doesn't close it: it's a promise that one
tool caught everything, retroactively, over history nobody designed for public reading.

This repository instead began from a `git init` with no ancestor commit that ever touched
the private source tree; every commit here was made against already-ported, already-reviewed
content — a structurally stronger guarantee than any filter offers, at the cost of the
original history, which was never the point of publishing the tool.

One consequence follows directly: **any credential that was ever live in the private
history must be revoked or rotated, independent of what this repository does.** A fresh
`git init` changes nothing about a key that already existed somewhere else. If an extraction
sweep turns up something that looks like a real credential, rotate it first, then worry
about whether the repository itself is clean.

### If unwanted data is already in a remote history

Deleting a file in a new commit does not delete its old blob, commit metadata, pull-request
refs, forks, caches, or clones. Keep the repository private while investigating. Rotate any
credential first. For a repository with no public-history requirement, the strongest release
shape is a reviewed orphan snapshot in a new repository, followed by archival or deletion of
the contaminated remote. If history must be preserved, use a reviewed history-rewrite plan,
coordinate the force update with every collaborator, and ask the hosting provider to purge
cached or pull-request refs that ordinary pushes cannot replace. Existing clones must be
discarded or carefully cleaned so they do not reintroduce removed objects.

History rewriting and remote deletion are destructive operations. Back up the private source,
record the exact refs being replaced, and require an explicit maintainer decision before doing
either. GitHub's current procedure is documented in
[Removing sensitive data from a repository](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository).

## Reporting a vulnerability

If you find a security issue in `coordharness` itself — a policy-pipeline bypass, a
path-handling bug that escapes the intended board directory, an MCP stdio-handling flaw —
report it privately, so a fix can land before the details are public. Open a private
security advisory on the repository (GitHub's advisory feature, under the Security tab)
rather than filing a regular issue or discussing specifics in a public thread. Include what
you found, how to reproduce it, and what you think the impact is; a maintainer will follow up.
