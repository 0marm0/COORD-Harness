#!/usr/bin/env bash
# One-command clone setup: creates this clone's .venv, .coordharness/coord.db, and
# (if absent) this clone's .codex/config.toml and .mcp.json.
#
# CHANGED (formerly scripts/setup-macos.sh): the venv/db/config lane below has no
# macOS dependency and now runs on every OS instead of refusing outside Darwin. Only
# the native macOS/iOS app lane (Xcode/XcodeGen + apps/install.sh -- a LaunchAgent,
# ~/Applications, ~/Library/Logs/COORD) stays Darwin-gated; on a non-Darwin host it is
# skipped with a one-line notice instead of exit 2. Both flags this script used to
# default ON now default OFF: `--native` is opt-in (nothing native-side installs
# unless asked), and `--register-clients` (global Codex/Claude MCP config, written
# outside this clone) is opt-in on every path, native included -- it used to default
# on for the native path. Added `--dry-run` (print planned actions, touch nothing)
# and `--check` (verify an existing install: venv, db, `coord doctor`). A real run now
# ends with a receipt block. scripts/setup-macos.sh is a 2-line shim that execs this
# file, kept only so its existing doc references keep working.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
DB_PATH="$ROOT/.coordharness/coord.db"
VENV="$ROOT/.venv"
PYTHON_COMMAND="${COORD_PYTHON:-python3}"

# Minimum Python version this project supports. Parsed from pyproject.toml's
# `requires-python` line so this guard cannot silently drift from the source
# of truth; if that line is ever missing or reshaped, fall back to a
# hardcoded floor -- keep it in sync with pyproject.toml's [project] table by
# hand if it does. Computed here (rather than just before the version check
# below) so it is also available to the --help text.
REQUIRED_PYTHON="$(grep -m1 '^requires-python' "$ROOT/pyproject.toml" 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -n1 || true)"
if [[ -z "$REQUIRED_PYTHON" ]]; then
  REQUIRED_PYTHON="3.11"  # fallback only -- keep in sync with pyproject.toml's requires-python
fi

check_python_version() {
  # $1: a python executable. Exit 0 if its version is >= $REQUIRED_PYTHON,
  # non-zero otherwise (including if the executable is missing or broken).
  "$1" -c "
import sys
required = tuple(int(p) for p in '$REQUIRED_PYTHON'.split('.'))
raise SystemExit(0 if sys.version_info[: len(required)] >= required else 1)
" 2>/dev/null
}

NATIVE=0
REGISTER_CLIENTS=0
DRY_RUN=0
CHECK=0
PASSTHROUGH_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --native) NATIVE=1 ;;
    --register-clients) REGISTER_CLIENTS=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --check) CHECK=1 ;;
    -h|--help)
      cat <<EOF
Usage: scripts/setup.sh [options]

Sets up this clone's .venv, .coordharness/coord.db, and clone-local
.codex/config.toml / .mcp.json (lifecycle authority: $ROOT/.coordharness/coord.db).
This lane has no macOS dependency and runs on any OS with Python 3.11+.

Options:
  --native              also install the native macOS/iOS apps: a LaunchAgent,
                         ~/Applications, ~/Library/Logs/COORD. Requires macOS,
                         Xcode command-line tools, and XcodeGen. Off by default.
                         On a non-Darwin host this flag is a no-op: setup prints
                         a one-line notice and skips the native lane instead of
                         failing.
  --register-clients    also register this clone with the Codex and Claude MCP
                         clients installed on this machine. Off by default on
                         every path -- it writes configuration outside this
                         clone (on the Codex side, your global config).
  --dry-run              print exactly what this run would do and exit;
                         nothing is created, installed, or written.
  --check                verify an existing install (.venv, coord.db, and
                         \`coord doctor\`) and exit with doctor's exit code;
                         nothing is created or written.
  -h, --help             show this help and exit

Environment:
  COORD_PYTHON           the python3 command this script uses to create the
                         venv (default: python3). Set this when the default
                         python3 is below Python $REQUIRED_PYTHON, e.g.:
                           COORD_PYTHON=python3.11 ./scripts/setup.sh

Any other option is forwarded to apps/install.sh (native lane only).
EOF
      exit 0
      ;;
    *) PASSTHROUGH_ARGS+=("$arg") ;;
  esac
done

# --check: read-only verification of an existing install. Independent of every
# other flag -- it never creates or writes anything.
if [[ "$CHECK" == 1 ]]; then
  STATUS_OK=1
  if [[ -x "$VENV/bin/python" ]]; then
    echo "venv: present ($VENV)"
  else
    echo "venv: MISSING ($VENV)"
    STATUS_OK=0
  fi
  if [[ -f "$DB_PATH" ]]; then
    echo "db:   present ($DB_PATH)"
  else
    echo "db:   MISSING ($DB_PATH)"
    STATUS_OK=0
  fi
  if [[ "$STATUS_OK" == 1 && -x "$VENV/bin/coord" ]]; then
    export COORD_PROJECT_ROOT="$ROOT"
    export COORD_HOME="$ROOT/.coordharness"
    export COORD_DB="$DB_PATH"
    set +e
    "$VENV/bin/coord" doctor
    DOCTOR_STATUS=$?
    set -e
    echo "coord doctor exit: $DOCTOR_STATUS"
    exit "$DOCTOR_STATUS"
  fi
  echo "coord doctor: SKIPPED (venv or db missing)"
  exit 2
fi

# --dry-run: print the plan for the flags given, then exit before touching
# anything -- no venv, no pip install, no coord invocation, no apps/install.sh.
if [[ "$DRY_RUN" == 1 ]]; then
  echo "DRY RUN -- nothing will be created, installed, or written."
  echo "Would ensure a virtualenv at:  $VENV"
  echo "Would run:                     $PYTHON_COMMAND -m pip install --upgrade '$ROOT[mcp]' (inside that venv)"
  echo "Would bootstrap database at:   $DB_PATH (via: coord board)"
  if [[ "$REGISTER_CLIENTS" == 1 ]]; then
    echo "Would run:                     coord onboard --write-configs --register-clients --skip-client-probes"
    echo "  (registers this clone with installed Codex/Claude MCP clients -- writes outside this clone)"
  else
    echo "Would run:                     coord onboard --write-configs --skip-client-probes"
    echo "  (clients NOT registered; pass --register-clients to also do that)"
  fi
  if [[ "$NATIVE" == 1 ]]; then
    if [[ "$(uname -s)" == "Darwin" ]]; then
      echo "Would check for Xcode command-line tools and XcodeGen"
      echo "Would run:                     apps/install.sh ${PASSTHROUGH_ARGS[*]:-} --db $DB_PATH"
      echo "  (installs a LaunchAgent, ~/Applications, ~/Library/Logs/COORD)"
    else
      echo "Native apps:                   would be skipped (macOS only; this host is $(uname -s))"
    fi
  else
    echo "Native apps:                   skipped (pass --native to install them; macOS only)"
  fi
  exit 0
fi

# --- real run ---

command -v "$PYTHON_COMMAND" >/dev/null || {
  echo "FAIL: $PYTHON_COMMAND was not found" >&2
  exit 2
}

if ! check_python_version "$PYTHON_COMMAND"; then
  echo "FAIL: $PYTHON_COMMAND is $("$PYTHON_COMMAND" -V 2>&1), but COORD needs Python $REQUIRED_PYTHON+." >&2
  echo "  Install Python $REQUIRED_PYTHON+ and re-run, or point this script at one:" >&2
  echo "    COORD_PYTHON=python3.11 ./scripts/setup.sh" >&2
  exit 2
fi

if [[ -x "$VENV/bin/python" ]] && ! check_python_version "$VENV/bin/python"; then
  echo "existing .venv uses $("$VENV/bin/python" -V 2>&1), below the required Python $REQUIRED_PYTHON+ -- rebuilding it" >&2
  rm -rf "$VENV"
fi
if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON_COMMAND" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --upgrade "$ROOT[mcp]"

export COORD_PROJECT_ROOT="$ROOT"
export COORD_HOME="$ROOT/.coordharness"
export COORD_DB="$DB_PATH"
export COORD_KNOWLEDGE_DB="$ROOT/.coordharness/knowledge.db"
export COORD_DEPLOYMENT_PROFILE=generic
"$VENV/bin/coord" board >/dev/null
ONBOARD_ARGS=(onboard --write-configs --skip-client-probes)
if [[ "$REGISTER_CLIENTS" == 1 ]]; then
  ONBOARD_ARGS=(onboard --write-configs --register-clients --skip-client-probes)
fi
"$VENV/bin/coord" "${ONBOARD_ARGS[@]}"

NATIVE_STATUS="skipped (pass --native to install them; macOS only)"
if [[ "$NATIVE" == 1 ]]; then
  if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "skipping native apps (macOS only)"
    NATIVE_STATUS="skipped (--native requested, but this host is $(uname -s), not macOS)"
  else
    command -v xcodebuild >/dev/null && xcodebuild -version >/dev/null 2>&1 || {
      echo "FAIL: Xcode command-line tools were not found; install Xcode and select it with xcode-select" >&2
      exit 2
    }
    command -v xcodegen >/dev/null || {
      echo "FAIL: XcodeGen was not found; install it with: brew install xcodegen" >&2
      exit 2
    }
    "$ROOT/apps/install.sh" "${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}" --db "$DB_PATH"
    set +e
    "$VENV/bin/coord" onboard
    NATIVE_ONBOARD_STATUS=$?
    set -e
    if [[ "$NATIVE_ONBOARD_STATUS" == 2 ]]; then
      printf '%s\n' \
        'Setup is installed, but an interactive client trust step remains.' \
        'If Claude reports approval pending, run `claude` in this clone, approve coordharness, then rerun `.venv/bin/coord onboard`.'
    fi
    NATIVE_STATUS="installed"
  fi
fi

set +e
"$VENV/bin/coord" doctor
DOCTOR_STATUS=$?
set -e
case "$DOCTOR_STATUS" in
  0) DOCTOR_VERDICT="PASS" ;;
  2) DOCTOR_VERDICT="BLOCKED" ;;
  *) DOCTOR_VERDICT="unknown (exit $DOCTOR_STATUS)" ;;
esac

printf '\n%s\n' "== setup receipt =="
printf '%s\n' \
  "  clone path:         $ROOT" \
  "  db path:             $DB_PATH" \
  "  clients registered:  $(if [[ "$REGISTER_CLIENTS" == 1 ]]; then echo 'yes (global Codex/Claude MCP config written)'; else echo 'no (pass --register-clients to register them)'; fi)" \
  "  native apps:          $NATIVE_STATUS" \
  "  coord doctor:         $DOCTOR_VERDICT" \
  "" \
  "  next command:  $VENV/bin/coord-board --db $DB_PATH --host 127.0.0.1 --port 7870" \
  "  uninstall:     $(if [[ "$NATIVE_STATUS" == "installed" ]]; then echo "$ROOT/apps/uninstall.sh (native apps; preserves the database), then "; fi)$VENV/bin/python -m pip uninstall coordharness (or: rm -rf $VENV $ROOT/.coordharness)"

exit "$DOCTOR_STATUS"
