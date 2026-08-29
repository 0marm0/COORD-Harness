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
fi

if [ "$FAST" = 1 ]; then
  step "browser suite: skipped (--fast)"
else
  step "browser suite, batched"
  mapfile -t browser_files < <(ls tests/*browser*.py 2>/dev/null || true)
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
