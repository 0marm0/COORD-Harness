#!/usr/bin/env bash
# Run locally what CI runs, in the order CI runs it.
#
# This exists because a green test suite is not a green build. The Python
# workflow lints before it tests, and a change can pass every test in the repo
# and still fail CI on one lint rule -- which is exactly how this script came to
# be written.
#
# The browser suite is deliberately batched. Running all of it in one pytest
# process is enough to be killed by a memory watchdog on a developer machine,
# and a SIGKILL reads like a hang rather than a result. CI splits the same work
# into a separate job for the same reason.
#
# Usage:
#   scripts/check.sh            # lint, tests, docs, publication gates
#   scripts/check.sh --fast     # skip the browser suite (the slow half)
set -euo pipefail

cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
FAST=0
[ "${1:-}" = "--fast" ] && FAST=1

if [ ! -x "$PY" ]; then
  echo "check: no interpreter at $PY" >&2
  echo "  create one:  python3 -m venv .venv && .venv/bin/pip install -e '.[mcp,dev]'" >&2
  echo "  or point at your own:  PYTHON=/path/to/python scripts/check.sh" >&2
  exit 2
fi

step() { printf '\n==> %s\n' "$1"; }

step "lint (ruff)"
"$PY" -m ruff check src tests tools .github/scripts

step "tests, excluding the browser suite"
"$PY" -m pytest -q -m 'not slow' --ignore-glob='tests/*browser*.py'

step "documentation and asset provenance"
"$PY" .github/scripts/validate_docs.py

step "publication and extraction gates"
"$PY" -m pytest -q tests/publication tests/extract
"$PY" tools/privacy_hygiene.py --history
"$PY" tools/public_hygiene_sweep.py

# The extraction gate reads Git's index, not the working tree. Running it on
# unstaged edits reports a clean candidate no matter what those edits contain,
# so say that rather than printing a result that cannot mean anything yet.
if [ -n "$(git diff --name-only)" ]; then
  step "extraction gate: SKIPPED"
  echo "    Unstaged changes are present, and this gate reads the index."
  echo "    Stage your work first, or it will report clean without seeing it."
else
  step "extraction gate"
  "$PY" tools/extract/gate.py
  # The bare gate runs two of its five legs. Fidelity needs the private source
  # and its pinned manifest; patterns needs the rename/refusal vocabulary, which
  # is gitignored because naming what you redact discloses it; history needs to
  # be asked for. The gate says so on its own last line -- and that line was read
  # as a pass for the life of this repository, while the three legs that had
  # never run were holding a house id convention, its fixtures, and its blobs.
  #
  # This is the check to run before publishing anything:
  #
  #   "$PY" tools/extract/gate.py \
  #     --source-root /path/to/the/private/repository \
  #     --source-manifest /path/to/manifest.private.json \
  #     --vocabulary tools/extract/vocabulary.json \
  #     --history --ref HEAD
  #
  # Only that form can print PUBLISHABLE. See docs/security-and-privacy.md.
  echo
  echo "    Two of five legs ran. A publication verdict needs --source-root,"
  echo "    --source-manifest, --vocabulary and --history; see docs/security-and-privacy.md."
fi

if [ "$FAST" = 1 ]; then
  step "browser suite: skipped (--fast)"
else
  step "browser suite, batched"
  # Built with a glob loop, not mapfile: macOS ships bash 3.2, where mapfile
  # does not exist. This script must run on a stock machine.
  browser_files=()
  for browser_file in tests/*browser*.py; do
    [ -e "$browser_file" ] && browser_files+=("$browser_file")
  done
  if [ "${#browser_files[@]}" -eq 0 ]; then
    echo "    no browser tests found"
  elif ! "$PY" -c 'import playwright' 2>/dev/null; then
    echo "    playwright is not installed, so every browser test would SKIP."
    echo "    A skipped suite is not a passing one. Install it with:"
    echo "      $PY -m pip install playwright && $PY -m playwright install chromium"
    exit 1
  else
    for ((i = 0; i < ${#browser_files[@]}; i += 5)); do
      "$PY" -m pytest -q "${browser_files[@]:i:5}"
    done
  fi
fi

printf '\nall checks passed\n'
