#!/usr/bin/env bash
# One-command clone setup: agent runtime/MCP wiring plus native Mac installation.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
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
command -v xcodebuild >/dev/null && xcodebuild -version >/dev/null 2>&1 || {
  echo "FAIL: Xcode command-line tools were not found; install Xcode and select it with xcode-select" >&2
  exit 2
}
command -v xcodegen >/dev/null || {
  echo "FAIL: XcodeGen was not found; install it with: brew install xcodegen" >&2
  exit 2
}
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
