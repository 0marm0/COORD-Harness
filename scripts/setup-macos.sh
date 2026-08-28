#!/usr/bin/env bash
# One-command clone setup: agent runtime/MCP wiring, with native Mac installation by
# default. Pass --no-native for the CLI-only path, which needs neither Xcode nor
# XcodeGen and never touches apps/install.sh (which installs system-wide: a
# LaunchAgent, ~/Applications, ~/Library/Logs/COORD).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

NATIVE=1
for arg in "$@"; do
  case "$arg" in
    --no-native) NATIVE=0 ;;
  esac
done

# --no-native combined with a help flag must stay side-effect-free even when the
# help flag is not $1 (so the check below, which only inspects $1, would miss
# it) -- print CLI-only usage and exit before anything can touch this machine.
if [[ "$NATIVE" == 0 ]]; then
  for arg in "$@"; do
    case "$arg" in
      -h|--help)
        cat <<EOF
Usage: scripts/setup-macos.sh --no-native [options]

CLI-only setup: skips the Xcode/XcodeGen requirement and the native app
install entirely, and sets up only this clone's .venv, .coordharness/coord.db,
and MCP client wiring (lifecycle authority: $ROOT/.coordharness/coord.db).
EOF
        exit 0
        ;;
    esac
  done
fi

case "${1:-}" in
  -h|--help)
    printf '%s\n' "COORD clone setup (lifecycle authority: $ROOT/.coordharness/coord.db)"
    exec "$ROOT/apps/install.sh" "$@"
    ;;
esac

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "FAIL: scripts/setup-macos.sh requires macOS" >&2
  exit 2
fi

PYTHON_COMMAND="${COORD_PYTHON:-python3}"
DB_PATH="$ROOT/.coordharness/coord.db"
VENV="$ROOT/.venv"

command -v "$PYTHON_COMMAND" >/dev/null || {
  echo "FAIL: $PYTHON_COMMAND was not found" >&2
  exit 2
}
if [[ "$NATIVE" == 1 ]]; then
  command -v xcodebuild >/dev/null && xcodebuild -version >/dev/null 2>&1 || {
    echo "FAIL: Xcode command-line tools were not found; install Xcode and select it with xcode-select, or re-run with --no-native for the CLI-only path" >&2
    exit 2
  }
  command -v xcodegen >/dev/null || {
    echo "FAIL: XcodeGen was not found; install it with: brew install xcodegen (or re-run with --no-native for the CLI-only path)" >&2
    exit 2
  }
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
"$VENV/bin/coord" onboard --write-configs --register-clients --skip-client-probes

if [[ "$NATIVE" == 0 ]]; then
  printf '%s\n' \
    'CLI-only setup complete (no native apps installed; re-run without --no-native for those).' \
    "  Verify: $VENV/bin/coord --help" \
    "  Board:  $VENV/bin/coord-board --db $DB_PATH --host 127.0.0.1 --port 7870"
  exit 0
fi

"$ROOT/apps/install.sh" "$@" --db "$DB_PATH"

set +e
"$VENV/bin/coord" onboard
DOCTOR_STATUS=$?
set -e
if [[ "$DOCTOR_STATUS" == 2 ]]; then
  printf '%s\n' \
    'Setup is installed, but an interactive client trust step remains.' \
    'If Claude reports approval pending, run `claude` in this clone, approve coordharness, then rerun `.venv/bin/coord onboard`.'
  exit 2
fi
exit "$DOCTOR_STATUS"
