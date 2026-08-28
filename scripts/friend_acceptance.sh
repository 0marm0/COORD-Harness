#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
MODE="automated"
if [[ "${1:-}" == "--human" ]]; then MODE="human"; elif [[ -n "${1:-}" ]]; then
  echo "usage: $0 [--human]" >&2; exit 2
fi
RUN_ROOT="${COORD_FRIEND_ROOT:-$(mktemp -d "${TMPDIR:-/tmp}/coord-friend.XXXXXX")}"
case "$RUN_ROOT" in /|"$HOME"|"$ROOT") echo "unsafe run root: $RUN_ROOT" >&2; exit 2 ;; esac
FRESH_HOME="$RUN_ROOT/home"; VENV="$RUN_ROOT/venv"; DIST="$RUN_ROOT/dist"
PROJECT="$RUN_ROOT/synthetic-project"; DERIVED="$RUN_ROOT/DerivedData"
NATIVE_INSTALL="$RUN_ROOT/Applications"; LOG="$RUN_ROOT/friend-acceptance.log"
mkdir -p "$FRESH_HOME" "$DIST" "$PROJECT" "$NATIVE_INSTALL"; touch "$LOG"
ln -s "$VENV" "$PROJECT/.venv"
mkdir -p "$PROJECT/docs" "$PROJECT/.agents/skills" "$PROJECT/.claude/skills"
cp "$ROOT/AGENTS.md" "$ROOT/CLAUDE.md" "$PROJECT/"
for doc in agent-onboarding agent-protocol context-architecture context-and-memory jobs-and-runs; do
  cp "$ROOT/docs/$doc.md" "$PROJECT/docs/"
done
cp -R "$ROOT/.agents/skills/operating-coordharness" "$PROJECT/.agents/skills/"
cp -R "$ROOT/.claude/skills/operating-coordharness" "$PROJECT/.claude/skills/"
BOARD_PID=""
cleanup() { if [[ -n "$BOARD_PID" ]]; then kill "$BOARD_PID" 2>/dev/null || true; wait "$BOARD_PID" 2>/dev/null || true; fi; }
trap cleanup EXIT
step() { printf '\n[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" | tee -a "$LOG"; }

step "Build wheel and sdist; install the wheel under a fresh HOME"
python3 -m venv "$VENV"; PY="$VENV/bin/python"
HOME="$FRESH_HOME" "$PY" -m pip install --upgrade pip build
HOME="$FRESH_HOME" "$PY" -m build --wheel --sdist --outdir "$DIST" "$ROOT"
WHEEL="$(find "$DIST" -maxdepth 1 -type f -name '*.whl' -print -quit)"
SDIST="$(find "$DIST" -maxdepth 1 -type f -name '*.tar.gz' -print -quit)"
[[ -n "$WHEEL" && -n "$SDIST" ]]
HOME="$FRESH_HOME" "$PY" -m pip install "$WHEEL" 'mcp>=1.20,<2.0'
BIN="$VENV/bin"
for command in coord coord-board coord-jobs coord-models coord-mcp; do test -x "$BIN/$command"; done

step "Create a synthetic-only project, database, lifecycle, and sample board"
git -C "$PROJECT" init -q
export HOME="$FRESH_HOME" COORD_PROJECT_ROOT="$PROJECT" COORD_HOME="$PROJECT/.coordharness"
export COORD_DB="$COORD_HOME/coord.db" COORD_DEPLOYMENT_PROFILE="generic" COORD_ACTOR="codex"
export COORD_SESSION_ID="codex:friend-acceptance" CODEX_SESSION_ID="codex:friend-acceptance"
"$BIN/coord" board >/dev/null
"$BIN/coord" doctor >/dev/null
"$PY" -m coordharness.demo --db "$COORD_DB" >/dev/null

step "Register installed MCP clients and verify config, registration, and handshake"
set +e
HOME="$FRESH_HOME" "$BIN/coord" onboard --project-root "$PROJECT" \
  --write-configs --register-clients >"$RUN_ROOT/onboarding.json"
ONBOARD_STATUS=$?
set -e
[[ "$ONBOARD_STATUS" == 0 || "$ONBOARD_STATUS" == 2 ]]
"$PY" - "$RUN_ROOT/onboarding.json" <<'PY'
import json, pathlib, sys
report = json.loads(pathlib.Path(sys.argv[1]).read_text())
findings = {item["id"]: item for item in report["findings"]}
assert findings["onboarding.agent_configs"]["status"] == "PASS"
assert findings["onboarding.mcp_stdio"]["status"] == "PASS"
for client in ("codex", "claude"):
    finding = findings[f"onboarding.{client}_client_registration"]
    if finding["details"].get("available"):
        assert finding["details"].get("server_listed") is True
        if client == "claude" and finding["details"].get("approval_pending"):
            assert "approve coordharness" in finding["summary"]
        else:
            assert finding["status"] == "PASS"
PY

"$BIN/coord" board >"$RUN_ROOT/board.json"
"$PY" - "$RUN_ROOT/board.json" <<'PY'
import json, pathlib, sys
assert json.loads(pathlib.Path(sys.argv[1]).read_text()).get("rows")
PY

step "Perform a real MCP initialize/list-tools handshake"
"$PY" - "$BIN/coord-mcp" <<'PY'
import asyncio, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
async def main():
    server = StdioServerParameters(command=sys.argv[1], env=dict(os.environ))
    async with stdio_client(server) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            assert (await session.list_tools()).tools
asyncio.run(main())
PY

PORT="$("$PY" - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0)); print(sock.getsockname()[1])
PY
)"
step "Start service and probe web, cockpit, atlas, and mesh on port $PORT"
"$BIN/coord-board" --db "$COORD_DB" --host 127.0.0.1 --port "$PORT" >"$RUN_ROOT/board-service.log" 2>&1 &
BOARD_PID=$!
for _ in $(seq 1 50); do
  curl --fail --silent "http://127.0.0.1:$PORT/api/v1/snapshot" >"$RUN_ROOT/snapshot.json" && break
  sleep 0.2
done
kill -0 "$BOARD_PID"
for route in / /cockpit /ops /mesh; do
  name="$(printf '%s' "$route" | tr '/' '_' | sed 's/^_$/web/')"
  curl --fail --silent "http://127.0.0.1:$PORT$route" >"$RUN_ROOT/${name}.html"
done

if [[ "$(uname -s)" == "Darwin" ]]; then
  step "Build every distributable native scheme without signing"
  command -v xcodegen >/dev/null; command -v xcodebuild >/dev/null
  (
    cd "$ROOT/apps"; xcodegen generate
    for scheme in CoordCockpitMac CoordMenuBar CoordCockpitWindow; do
      xcodebuild -quiet -project CoordCockpit.xcodeproj -scheme "$scheme" -configuration Release         -destination 'platform=macOS' -derivedDataPath "$DERIVED" CODE_SIGNING_ALLOWED=NO build
    done
    xcodebuild -quiet -project CoordCockpit.xcodeproj -scheme CoordCockpitIOS -configuration Release       -sdk iphonesimulator -destination 'generic/platform=iOS Simulator'       -derivedDataPath "$DERIVED" CODE_SIGNING_ALLOWED=NO build
  )
  for app in "CoordCockpitMac.app" "COORD.app" "COORD Cockpit.app"; do
    source_app="$(find "$DERIVED/Build/Products/Release" -maxdepth 1 -type d -name "$app" -print -quit)"
    [[ -n "$source_app" ]]; ditto "$source_app" "$NATIVE_INSTALL/$app"
  done
else
  step "FAIL: native release schemes require macOS; this is not an acceptance pass"
  exit 2
fi

if [[ "$MODE" == "human" ]]; then
  step "Human visual pass; automated checks are complete"
  if [[ "$(uname -s)" == "Darwin" ]]; then
    for route in / /cockpit /ops /mesh; do open "http://127.0.0.1:$PORT$route"; done
    [[ -d "$NATIVE_INSTALL/COORD.app" ]] && open "$NATIVE_INSTALL/COORD.app"
    [[ -d "$NATIVE_INSTALL/COORD Cockpit.app" ]] && open "$NATIVE_INSTALL/COORD Cockpit.app"
  fi
  printf '%s\n' "Use docs/friend-acceptance.md. Confirm menu, cockpit, web/atlas/mesh, synthetic labels, and no real data."
  read -r -p "Type PASS to continue to uninstall preservation: " HUMAN_RESULT
  [[ "$HUMAN_RESULT" == "PASS" ]]
else
  step "Human visual pass not run; use '$0 --human' tomorrow"
fi

step "Uninstall distribution and prove database preservation"
DB_HASH_BEFORE="$(shasum -a 256 "$COORD_DB" | awk '{print $1}')"
HOME="$FRESH_HOME" "$PY" -m pip uninstall -y coordharness >/dev/null
UNINSTALLED_NATIVE="$RUN_ROOT/uninstalled-native-apps"
mv "$NATIVE_INSTALL" "$UNINSTALLED_NATIVE"
[[ ! -e "$BIN/coord" && ! -e "$NATIVE_INSTALL" && -f "$COORD_DB" ]]
DB_HASH_AFTER="$(shasum -a 256 "$COORD_DB" | awk '{print $1}')"
[[ "$DB_HASH_BEFORE" == "$DB_HASH_AFTER" ]]
step "PASS: clean-room friend acceptance"
printf '%s\n' "mode=$MODE" "run_root=$RUN_ROOT" "preserved_database=$COORD_DB"   "database_sha256=$DB_HASH_AFTER" "log=$LOG"
