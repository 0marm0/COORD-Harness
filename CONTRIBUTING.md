# Contributing

This is what the project actually wants from a contribution, distilled from
how it is built and gated rather than restated as boilerplate. Read this
before your first change; most of it exists because something already went
wrong once without it.

## One fact that shapes almost everything below

Most of the code under `src/coordharness/coord/` and `src/coordharness/jobs/`
was not written in this repository. It was mechanically ported from a private
codebase (see [`docs/extraction.md`](docs/extraction.md) for why and how).
Check `tools/extract/manifest.json`: if a file's destination path is listed
there, **do not hand-edit it directly.** The publication gate's fidelity
check re-derives each ported file from its recorded source and compares the
result byte-for-byte against what is checked in; a hand-edit, even one
character, makes that comparison fail, because the checked-in file no longer
equals what the porter produces. It cannot tell a well-intentioned fix from
drift — both look identical to it.

If you don't have access to the original private repository — almost every
outside contributor — and you've found a real bug in ported code, open an
issue describing the bug and the fix you'd make. Don't patch the file; that
patch cannot pass the gate and will be reverted. Anything listed in
`tools/extract/authored.json` instead — `config.py`, `demo.py`, `entry.py`,
`util.py`, everything under `docs/` and `tests/`, and `README.md` — was
written here and carries no such constraint. Edit those the normal way.

## Setting up

Python 3.11+ is required:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[mcp,dev]"
```

Install both extras locally — `dev` for the test tooling, `mcp` for the
optional coordination server module — or the MCP-dependent tests will skip
instead of running.

## Running the checks this project actually gates on

```bash
scripts/check.sh          # lint, tests, docs, publication gates
scripts/check.sh --fast   # same, without the browser suite
```

Read the comment at the top of that script before reinventing the invocation
yourself: **the browser suite is deliberately batched, five files at a time,
because running all of it in one `pytest` process is enough to trip a memory
watchdog on a developer machine.** A watchdog `SIGKILL` does not look like a
failing test — it looks like the run hanging, with no assertion, no
traceback, and no useful exit code, at whatever file happened to be running
when the memory ceiling was hit. If you run the browser tests by hand, keep
them in small batches (`scripts/check.sh`'s loop is the reference shape); do
not fold them into one `pytest tests/*browser*.py` invocation and assume a
long silence is progress.

`scripts/check.sh` also refuses to run the extraction gate meaningfully over
unstaged changes — the gate reads Git's *index*, so it reports a clean
candidate that saw none of your edits rather than pretending to check them.
Stage your work first if you want that step to mean anything locally.

## Running tests

```bash
pytest                    # the whole suite
pytest -m "not slow"      # fast loop while iterating
```

One test is marked `slow` because it builds a virtualenv and installs a wheel
into it (`tests/test_packaging.py`) — expect it to take longer than the rest
of the suite combined. It exists because a source checkout hides an entire
class of bug (a runtime resource missing from packaging metadata, a
`__file__`-relative path that assumes the checkout layout, a schema nobody
applies against a fresh database) that only shows up once the package is
actually installed with no checkout on `PYTHONPATH`. Run the full suite,
slow test included, before opening a pull request — do not skip it once it
becomes inconvenient; that is exactly when it earns its cost.

## What this project's own review discipline means for your PR

This repository ships a coordination harness whose own design refuses to
call anything "done" on an unsupported assertion — see
[`docs/review-tiers.md`](docs/review-tiers.md) and
[`docs/threat-model.md`](docs/threat-model.md) (§2.6, the completion proof
gate). Hold your own pull request to the same standard it enforces on the
work it coordinates:

- **Show the evidence, don't summarize it.** "Tests pass" is not evidence;
  the exact command and its actual output (or a CI run) is. A completion
  claim this project's own board would accept always resolves to something
  checkable — do the same in a PR description.
- **Scale scrutiny to what changed, the way `docs/review-tiers.md` scales
  it.** A reversible documentation or tooling fix needs little more than the
  gates above passing. A change to identity handling, a claim-holder or
  review-tier check, the completion gate, or anything in
  `src/coordharness/coord/` that this document's threat model discusses is
  the equivalent of a T0 row in this project's own vocabulary: expect closer
  review, and don't be surprised if it takes longer than a docs change to
  land, for the same reason the harness itself would not let that class of
  row self-close.
- **A ported file's manifest entry is not a normal diff.** If your change
  touches `tools/extract/manifest.json` or a file it governs, say so
  explicitly in the PR body — that category needs someone who can verify
  fidelity against the private source, which most reviewers of a given PR
  will not be able to do from the public repository alone.

## The public-hygiene constraint on every contribution

`tools/privacy_hygiene.py` scans every file in the working tree — tracked
*and* untracked, not just what you've staged — for an absolute path rooted at
a real user's home directory (the macOS and Linux shapes, with a name that
isn't a generic placeholder) and a denylist of forbidden phrase digests. Run
it before pushing:

```bash
python tools/privacy_hygiene.py --history
```

This is not advisory. A public repository extracted from a private one has
exactly one property that must never regress: no real path, name, or private
vocabulary crossing into it, ever, including inside a test written to prove
a scanner works. If a test needs an example of the *shape* the scanner
rejects, assemble it from string parts at runtime (`"/" + "Users" + "/" +
"name"`) rather than writing the literal pattern into the source file — the
scanner reads source bytes, not runtime values, and a literal needle in a
test is exactly the kind of file it is built to catch. This has already
happened twice; don't make it a third time.

`docs/security-and-privacy.md` states the full list of what a public
candidate must never contain (live databases, real work-item text, absolute
paths, unreviewed screenshots, anything source-derived). Read it before
adding a fixture, a screenshot, or an example that might look real.

## The mirrored agent-experience trees

`.claude/commands/` and `.agents/commands/` — and `.claude/skills/` and
`.agents/skills/` alongside them — are two on-disk copies of the same agent
onboarding package, kept byte-identical by
[`tests/test_agent_commands.py`](tests/test_agent_commands.py). There is no
generator: if you edit the content in one client's tree, copy the exact
bytes into the other tree's copy before you commit. A one-character drift
between the two — a renamed CLI verb updated in only one file, a stray
trailing space — fails that test, and it exists because the two trees have
drifted silently before.

## Code style

- Standard library first. The core package has zero required dependencies
  (`mcp` is an optional extra for one module); don't add a new required
  dependency without discussing it first.
- Type annotations on function and method signatures.
- No docstrings or comments carried over unexplained from elsewhere — write
  them fresh, for what the code in front of you actually does.
- Prefer asserting on behavior (a function's return value, a row written to
  the database, a non-zero exit code) over asserting that a string of source
  text is present somewhere in a file. A test that only greps a `.py` file
  for a name stays green through a refactor that keeps the name but breaks
  what it does.

## Commit and PR conventions

Commit messages use `<type>: <description>` — `feat`, `fix`, `refactor`,
`docs`, `test`, `chore` — matching the existing log (`git log --oneline`).
Keep the summary line imperative and short; put context in the body.

Before opening a pull request:

```bash
pytest -m "not slow"          # fast loop while iterating
pytest                          # full suite, including the wheel-install test
ruff check src tests tools .github/scripts
python tools/extract/gate.py
python tools/privacy_hygiene.py --history
python tools/public_hygiene_sweep.py
```

Describe what changed and why, and flag explicitly if the change touches a
ported file's manifest entry rather than only authored files.
