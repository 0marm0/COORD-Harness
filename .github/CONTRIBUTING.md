# Contributing to coordharness

This repository has one property that shapes almost everything below: most of the code under
`src/` was not written here. It was ported, mechanically, from a private codebase (see
`docs/extraction.md` for why and how). That fact determines which files you can edit directly and
what the gate checks before a pull request merges. Read this file before your first change — the
workflow is how the separation from the private source stays intact, not optional ceremony.

## Setting up

Python 3.11+ is required. Create a virtual environment and install the package editable, with
both extras — `dev` for the test tooling, `mcp` for the optional coordination server:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[mcp,dev]"
```

The `mcp` extra is optional at runtime (the package works without it; only the MCP server module
needs it), but install it locally so the full test suite runs instead of skipping that module.

There is no separate lint/format install step — `ruff` comes in with `dev`. The development
extra pins the exact reviewed Ruff release so a newly enabled upstream rule cannot make hosted
checks disagree with a contributor's clean local run. Upgrade that pin deliberately, together
with any required lint migration.

## Running tests

```bash
pytest
```

runs the whole suite. One test is marked `slow` because it builds a virtual environment and
installs a wheel into it — expect it to take longer than the rest of the suite combined. To skip
it during a quick local loop:

```bash
pytest -m "not slow"
```

Run the full suite, slow test included, before opening a pull request. Do not skip it out of habit
once it is inconvenient — it exists because the fast tests provably cannot catch the class of bug
it catches.

### Why the wheel-install test exists

Every other test in this suite runs against a source checkout, with `src/` importable on
`PYTHONPATH`. That is convenient, and it is also blind to a specific category of defect: anything
that only breaks once the package is actually installed into a clean environment, with no checkout
sitting on the path underneath it. An earlier review of this codebase found four real bugs that no
checkout-based test saw, because a checkout papers over exactly what installing exposes:

- a runtime resource (a JSON file read at import time) was missing from
  `[tool.setuptools.package-data]`, so the wheel built and installed cleanly but the module failed
  to import outside the checkout, where the file no longer sat on disk next to the importing code;
- a path was computed by walking up a fixed number of parent directories from `__file__`; that
  arithmetic assumes the source layout and lands somewhere inside `site-packages` once installed;
- the CLI's first command against a fresh project crashed with `no such table`, because nothing
  created the schema — every checkout that had ever run once before had a leftover database;
- migrations shipped in the wheel but were never applied, so tables from a later schema version
  were silently absent even though the `.sql` files sat right there in the package.

All four passed a source-checkout test suite. All four failed the moment `pip install` ran in a
directory with no checkout in sight. `tests/test_packaging.py` builds a real wheel, installs it
into a throwaway venv with `PYTHONPATH` stripped from the environment, and drives the installed
`coord` console script from a fresh project directory — the same conditions a real user hits on
day one. If you change `[tool.setuptools]`, add a runtime-read resource file, or touch how the CLI
locates its database or applies migrations, run this test specifically:

```bash
pytest tests/test_packaging.py -v
```

## The porter and the gate

Open `src/coordharness/coord/` or `src/coordharness/jobs/` and you are looking at ported code.
Check `tools/extract/manifest.json`: if a file's destination path is listed there, it did not
originate in this repository, and **you must not hand-edit it directly.**

The reason is mechanical, not a style preference. The publication gate's fidelity check
(`tools/extract/gate.py`) re-runs the porter against each ported file's recorded source and
compares the result byte-for-byte against what is checked in. A hand-edit — even one character —
makes that comparison fail, because the checked-in file no longer equals what the porter produces.
The gate cannot tell a well-intentioned fix from drift; both look identical to it.

If a ported file needs to change, the change goes through the manifest, not the file:

1. Add an exact `find` / `with` / `count` / `reason` transform under that file's `edits` list in
   the *working* manifest (`tools/extract/manifest.private.json`, gitignored because its exact
   search and replacement strings are drawn from the private source). The published
   `manifest.json` is a redacted projection: it retains transform counts and reasons, not private
   equality oracles.
2. Re-run the porter for that file:

   ```bash
   python tools/extract/port.py --source-root /path/to/the/private/repository --dry-run
   ```

   Drop `--dry-run` once the diff looks right.
3. Commit the regenerated file. Its provenance in the manifest now matches what is checked in
   again, and the fidelity check passes.

This path is only available to someone with access to the original private repository — almost
never an outside contributor. If you don't have that access and you've found a real bug in ported
code, open an issue describing the bug and the fix you'd make. Don't patch the file directly; that
patch cannot pass the gate and will be reverted.

### What you can edit freely

Anything listed in `tools/extract/authored.json` was written fresh here and carries no provenance
constraint: `config.py`, `demo.py`, `entry.py`, `util.py`, everything under `docs/` and `tests/`,
and `README.md`. Edit these the normal way — ordinary source files, not the output of a tool.

### The manifest allowlist rule

Every file here must be accounted for, individually, in one of two places: `manifest.json`
(ported) or `authored.json` (written here). Glob patterns are rejected by the loader — an entry
containing `*`, `?`, or `[` is a hard error. Add a new file's entry with a one-line reason before
you commit it, or the coverage check below fails on your pull request.

### Running the gate before a pull request

```bash
python tools/extract/gate.py --history
```

Also run the repository-safe baseline privacy check:

```bash
python tools/privacy_hygiene.py --history
```

Official maintainers repeat it with an external raw vocabulary file. That file
must remain outside Git; the script hashes its contents in memory and never
prints the forbidden phrases.

The extraction command runs coverage, patterns, shape, and reachable-history checks using only this repository. Fidelity
needs authorized private inputs and is not something most contributors can run:

```bash
python tools/extract/gate.py \
  --source-root /path/to/the/private/repository \
  --source-manifest tools/extract/manifest.private.json \
  --vocabulary tools/extract/vocabulary.json \
  --history
```

Without all three private inputs, the gate reports fidelity as skipped rather than silently
passing it — a skip is not a pass, and the result is only a clean candidate. Get the public gate
to exit 0 before opening a pull request; maintainers run full fidelity before a release.

## Code style

- Standard library first. The core package has zero required dependencies (`mcp` is an optional
  extra for one module); do not add a new required dependency without discussing it first — it
  changes what "no dependencies" means for everyone downstream.
- Type annotations on function and method signatures. CI checks `src`, `tests`, `tools`, and its
  helper scripts; run `ruff check src tests tools .github/scripts` locally before pushing.
- No docstrings or comments carrying over unexplained context from elsewhere — write them fresh,
  in your own words, for what the code in front of you actually does.

## Writing a test that would actually fail

The most common way a test looks like coverage but provides none: asserting that a string is
present somewhere in a source file, rather than asserting on behavior. A test that greps a `.py`
file for a function name or a line of text stays green through a refactor that keeps the string
but breaks the behavior it was meant to guard — a real regression here shipped exactly that way
once, past a check that confirmed a name still appeared in source rather than that the code still
did the right thing at runtime. Prefer calling the function and asserting on its return value or
its observable effect (a row in the database, a file written, a non-zero exit code) over
inspecting source text. `test_imports.py`'s dangling-reference check is the one deliberate
exception here, and its own docstring explains why a text check is the right tool there — that is
the bar for reaching for one.

## Commit and PR conventions

Commit messages use `<type>: <description>` — `feat`, `fix`, `refactor`, `docs`, `test`, `chore`,
matching the existing log (`git log --oneline`). Keep the summary line imperative and short; put
context in the body if the change needs it.

Before opening a pull request:

```bash
pytest -m "not slow"    # fast loop while iterating
pytest                   # full suite, including the wheel-install test, before opening
ruff check src tests tools .github/scripts
python tools/extract/gate.py --history
```

Describe what changed and why in the PR body, and flag explicitly if the change touches a ported
file's manifest entry rather than only authored files — that category needs the extra scrutiny of
someone who can verify fidelity against the source.
