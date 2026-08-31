# Security policy

## Supported versions

`coordharness` has not cut a tagged release yet. `pyproject.toml` currently
declares `0.1.0`, but no version has been published to a package index and no
Git tag exists for it. There is exactly one supported line: the current state
of this repository's default branch. Security fixes land there; there is no
older released version to backport to, because none has ever been released.

Once a version is actually tagged and distributed, this section will name
which lines receive fixes. Until then, "supported" means "the latest commit,"
and nothing else.

## Reporting a vulnerability

Report suspected vulnerabilities privately, through GitHub's private
vulnerability reporting for this repository: open the repository's
**Security** tab and use **Report a vulnerability**. That opens a private
draft security advisory visible only to you and the maintainer, so a fix can
be prepared before any detail becomes public.

Private vulnerability reporting is a per-repository GitHub setting, and the
maintainer intends to keep it turned on for this repository, so the button
above should be there. If it ever is not — do not fall back to a public
issue, a discussion, or email; none of those are private. Instead, open the
narrowest possible public issue: say only that you have a security report
and need a private channel, with no reproduction steps, no affected
component, and no other technical detail. That is enough for the maintainer
to see it and follow up privately, without putting anything sensitive where
the public can read it first.

Do not open a public issue, and do not describe exploit details, credentials,
or private data in an issue, a discussion, or a pull request.

This project does not publish a contact email, and none is invented here. A
real one would go stale or get scraped, and either way it would be a second,
weaker channel next to the GitHub advisory flow above, which is the one that
actually reaches the maintainer. Do not email a report; there is nowhere to
send it.

Include what you found, the smallest reproduction you have, and what you
think the impact is. A proof-of-concept command or a failing test is more
useful than a description of the general category.

## Response expectations

This project is maintained by one person, not a security team, and not full
time. There is no service-level agreement, and this document will not invent
one it cannot keep. In practice:

- Expect an acknowledgment before a fix. The goal is to acknowledge a private
  report within a couple of weeks, not hours — and if that slips, a follow-up
  comment on the same advisory thread is welcome, not impatient.
- A confirmed issue gets a plan and, where it makes sense, a coordinated
  disclosure timeline, proposed once the report is understood rather than
  promised in advance.
- Severity and available time both set the pace. A narrow bug in a single
  guard and a report that touches the completion or review-tier gate are not
  the same amount of work to fix safely, and this document will not pretend
  they are.

## Scope

`coordharness` coordinates trusted processes on one machine, normally under
one user account, against one local SQLite file (`coord.db`). That trust
model is stated in [`docs/security-and-privacy.md`](docs/security-and-privacy.md)
and audited guard by guard in [`docs/threat-model.md`](docs/threat-model.md).
Both are required reading before filing a report about identity,
authorization, or the review pipeline — the scope below follows directly from
what they already establish, not from a separate policy invented for this
file.

### In scope

Reports about a guard behaving worse than documented are welcome, for
example:

- A policy-pipeline check, a claim-holder check, or the review-tier
  classifier that can be bypassed by a caller going through a *supported*
  interface — the CLI, the MCP tool surface, or the Python API — without
  already holding the access that guard assumes.
- A path-handling bug that lets a caller escape the intended board,
  job-progress, or repository directory.
- An MCP stdio-handling flaw, or a board-server request-handling bug, that
  does something other than what
  [`docs/security-and-privacy.md`](docs/security-and-privacy.md) says it
  does — in particular anything that makes the read-only board projection
  accept a write, or that weakens the loopback and origin checks on its POST
  routes.
- The publication gate (`tools/extract/gate.py`) or the privacy scanner
  (`tools/privacy_hygiene.py`) failing to catch something they claim to
  catch.
- A supply-chain issue in this repository's own build or release tooling.

### Out of scope

These are not oversights; they are the stated boundary of a single-host,
single-user tool, and [`docs/threat-model.md`](docs/threat-model.md) names
them explicitly under its non-goals rather than leaving them implicit:

- **An actor who can already write `coord.db` directly** — open it, edit it,
  or replace it with a SQLite client of their own. Every guard in this
  codebase is a Python function running inside the same trusted process; none
  of them defends against someone who can touch the file underneath that
  process. The boundary here is the operating system's file permissions, not
  this project's code, and a report that only shows "I edited the database
  file and the invariant broke" is not a finding.
- **An actor who can allocate their own controlling terminal and drive both
  ends of it.** The one channel reserved for a human operator — the
  `operator_ok` sign-off that can substitute for a peer review verdict — is
  gated on there being a real controlling terminal to answer from. An actor
  that fabricates one is impersonating the operator, which this project does
  not attempt to detect from inside the process being impersonated.
- **An actor able to run arbitrary code as the user account already running
  `coordharness`** — equivalently, a compromised agent runtime executing
  someone else's instructions. If the process this project trusts has been
  compromised, every check that process performs is being run by the
  attacker; there is no guard against that from inside the process it
  compromised, and there cannot be.
- Anything that requires authentication, multi-tenant isolation, or a hosted
  network service. `coordharness` has none of these today and is not a
  multi-tenant product; see
  [`docs/threat-model.md`](docs/threat-model.md#4-what-a-hosted-or-shared-profile-would-actually-require)
  for what such a thing would need before it existed — that section is a
  checklist for a future design, not a description of anything shipped.
- Findings that only reproduce against a hand-edited `coord.db`, a modified
  checkout that removed a guard, or an interface not reachable from the CLI,
  the MCP server, or the public Python API.

If you are not sure whether something is in scope, report it anyway through
the private channel above and say so. A boundary call belongs to the
maintainer reading the report, not to a contributor guessing in advance.
