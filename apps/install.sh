#!/usr/bin/env bash
# Install a self-contained local COORD runtime, board service, and native apps.
# The installed Python environment is independent of this source checkout.
set -euo pipefail

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: apps/install.sh [--db PATH] [--app-dir PATH] [--python PATH] [--no-launch]

  --db PATH       Existing or new COORD authority (default: ~/.coordharness/coord.db)
  --app-dir PATH  Native app destination (default: ~/Applications)
  --python PATH   Python 3.11+ interpreter (default: python3 on PATH)
  --no-launch     Install without opening the menu-bar app

COORD_DB is accepted as the database selection when --db is absent.
EOF
}

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
BOARD_URL="http://127.0.0.1:7870"
LABEL="org.coordharness.board"
DB_INPUT="${COORD_DB:-$HOME/.coordharness/coord.db}"
APP_DIR_INPUT="${COORD_APP_DIR:-$HOME/Applications}"
PYTHON_COMMAND="${COORD_PYTHON:-python3}"
LAUNCH_APPS=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --db)
      [[ $# -ge 2 ]] || fail "--db requires a path"
      DB_INPUT="$2"
      shift 2
      ;;
    --app-dir)
      [[ $# -ge 2 ]] || fail "--app-dir requires a path"
      APP_DIR_INPUT="$2"
      shift 2
      ;;
    --python)
      [[ $# -ge 2 ]] || fail "--python requires an executable"
      PYTHON_COMMAND="$2"
      shift 2
      ;;
    --no-launch)
      LAUNCH_APPS=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown option: $1"
      ;;
  esac
done

expand_home() {
  case "$1" in
    "~") printf '%s\n' "$HOME" ;;
    "~/"*) printf '%s/%s\n' "$HOME" "${1#\~/}" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

absolute_path() {
  local raw parent leaf
  raw="$(expand_home "$1")"
  [[ "$raw" == /* ]] || raw="$PWD/$raw"
  parent="$(dirname -- "$raw")"
  leaf="$(basename -- "$raw")"
  mkdir -p -- "$parent"
  printf '%s/%s\n' "$(cd -- "$parent" && pwd -P)" "$leaf"
}

DB_PATH="$(absolute_path "$DB_INPUT")"
APP_DIR="$(absolute_path "$APP_DIR_INPUT")"
RUNTIME_ROOT="${COORD_RUNTIME_ROOT:-$HOME/Library/Application Support/COORD}"
RUNTIME_ROOT="$(absolute_path "$RUNTIME_ROOT")"
VENV="$RUNTIME_ROOT/venv"
LOG_DIR="${COORD_LOG_DIR:-$HOME/Library/Logs/COORD}"
LOG_DIR="$(absolute_path "$LOG_DIR")"
LAUNCH_AGENT_DIR="${COORD_LAUNCH_AGENT_DIR:-$HOME/Library/LaunchAgents}"
LAUNCH_AGENT_DIR="$(absolute_path "$LAUNCH_AGENT_DIR")"
PLIST="$LAUNCH_AGENT_DIR/$LABEL.plist"
DERIVED="$REPO_ROOT/var/build"
BUILD_LOG="$REPO_ROOT/var/install_log.txt"
CONFIG_PATH="$HOME/.coordharness/menubar_panel_config.json"

[[ "$DB_PATH" != "/" && "$DB_PATH" != "$HOME" ]] || fail "refusing unsafe database path: $DB_PATH"
[[ "$RUNTIME_ROOT" != "/" && "$RUNTIME_ROOT" != "$HOME" ]] || fail "refusing unsafe runtime root: $RUNTIME_ROOT"
[[ "$APP_DIR" != "/" && "$APP_DIR" != "$HOME" ]] || fail "refusing unsafe app directory: $APP_DIR"

for command_name in xcodegen xcodebuild codesign ditto curl launchctl defaults; do
  command -v "$command_name" >/dev/null || fail "$command_name is required"
done
command -v "$PYTHON_COMMAND" >/dev/null || fail "$PYTHON_COMMAND was not found"
"$PYTHON_COMMAND" - <<'PY' || fail "Python 3.11 or newer is required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY

echo "1/7  create isolated Python runtime"
mkdir -p -- "$RUNTIME_ROOT" "$LOG_DIR" "$LAUNCH_AGENT_DIR" "$APP_DIR"
if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON_COMMAND" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --upgrade "$REPO_ROOT"
[[ -x "$VENV/bin/coord-board" ]] || fail "coord-board was not installed into $VENV"
printf '%s\n' "$LABEL" > "$RUNTIME_ROOT/.coord-install-marker"

echo "2/7  create or migrate selected database"
"$VENV/bin/python" -m coordharness.coord.create_schema --db "$DB_PATH"
[[ -s "$DB_PATH" ]] || fail "database bootstrap did not create $DB_PATH"

echo "3/7  persist shared native configuration"
for domain in org.coordharness.menubar org.coordharness.cockpit.window org.coordharness.cockpit.mac; do
  defaults write "$domain" coordharness.baseURL -string "$BOARD_URL"
  defaults write "$domain" coordharness.coordDBPath -string "$DB_PATH"
done
"$VENV/bin/python" - "$CONFIG_PATH" <<'PY'
import json
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
payload = {}
if path.exists():
    try:
        candidate = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(candidate, dict):
            payload = candidate
    except (OSError, ValueError):
        pass
payload["transport"] = "http"
temporary = path.with_name(path.name + ".coord-install.tmp")
temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(temporary, path)
PY

echo "4/7  install and start dedicated board LaunchAgent"
"$VENV/bin/python" - "$PLIST" "$LABEL" "$VENV/bin/coord-board" "$DB_PATH" \
  "$RUNTIME_ROOT" "$LOG_DIR/coord-board.stdout.log" "$LOG_DIR/coord-board.stderr.log" <<'PY'
import pathlib
import plistlib
import sys

path, label, executable, database, working_directory, stdout_path, stderr_path = sys.argv[1:]
payload = {
    "Label": label,
    "ProgramArguments": [
        executable,
        "--db", database,
        "--host", "127.0.0.1",
        "--port", "7870",
    ],
    "EnvironmentVariables": {
        "COORD_DB": database,
        "COORD_BOARD_URL": "http://127.0.0.1:7870",
    },
    "WorkingDirectory": working_directory,
    "RunAtLoad": True,
    "KeepAlive": True,
    "ProcessType": "Background",
    "ThrottleInterval": 5,
    "StandardOutPath": stdout_path,
    "StandardErrorPath": stderr_path,
}
destination = pathlib.Path(path)
temporary = destination.with_name(destination.name + ".coord-install.tmp")
with temporary.open("wb") as stream:
    plistlib.dump(payload, stream, sort_keys=True)
temporary.replace(destination)
PY
chmod 600 "$PLIST"
launchctl bootout "gui/$UID/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$UID" "$PLIST"
launchctl kickstart -k "gui/$UID/$LABEL"

echo "5/7  health-check local board"
healthy=0
health_payload=""
for _attempt in {1..40}; do
  service_state="$(launchctl print "gui/$UID/$LABEL" 2>/dev/null || true)"
  health_payload="$(curl --fail --silent --show-error --max-time 2 "$BOARD_URL/healthz" 2>/dev/null || true)"
  if [[ "$service_state" == *"state = running"* ]] \
    && [[ "$health_payload" == *'"service": "coord-board"'* || "$health_payload" == *'"service":"coord-board"'* ]]; then
    healthy=1
    break
  fi
  sleep 0.5
done
if [[ "$healthy" != 1 ]]; then
  tail -n 30 "$LOG_DIR/coord-board.stderr.log" 2>/dev/null || true
  fail "LaunchAgent $LABEL did not become the healthy coord-board at $BOARD_URL/healthz"
fi
curl --fail --silent --show-error --max-time 5 "$BOARD_URL/api/v1/snapshot" >/dev/null \
  || fail "coord-board health passed but snapshot API failed"

echo "6/7  build and sign native apps"
xcodegen generate --spec "$SCRIPT_DIR/project.yml" >/dev/null
mkdir -p -- "$(dirname -- "$BUILD_LOG")"
for scheme in CoordMenuBar CoordCockpitWindow; do
  if ! xcodebuild -project "$SCRIPT_DIR/CoordCockpit.xcodeproj" -scheme "$scheme" \
    -configuration Release -derivedDataPath "$DERIVED" -destination 'platform=macOS' \
    CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY='' \
    ENABLE_HARDENED_RUNTIME=NO build > "$BUILD_LOG" 2>&1; then
    grep -E "error:" "$BUILD_LOG" | head -20 >&2 || true
    fail "native build failed for $scheme; see $BUILD_LOG"
  fi
done

PRODUCTS="$DERIVED/Build/Products/Release"
MENU_APP="$PRODUCTS/COORD.app"
WINDOW_APP="$PRODUCTS/COORD Cockpit.app"
[[ -d "$MENU_APP" ]] || fail "no app produced at $MENU_APP"
[[ -d "$WINDOW_APP" ]] || fail "no app produced at $WINDOW_APP"
codesign --force --deep --sign - "$MENU_APP"
codesign --force --deep --sign - "$WINDOW_APP"

echo "7/7  install native app bundles"
osascript -e 'quit app "COORD"' >/dev/null 2>&1 || true
osascript -e 'quit app "COORD Cockpit"' >/dev/null 2>&1 || true
pkill -x "COORD" >/dev/null 2>&1 || true
pkill -x "COORD Cockpit" >/dev/null 2>&1 || true

bundle_identifier() {
  /usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$1/Contents/Info.plist" 2>/dev/null || true
}

install_bundle() {
  local source="$1" target="$2" expected_id="$3" backup
  backup="$target.coord-install-backup.$$"
  if [[ -e "$target" ]]; then
    [[ "$(bundle_identifier "$target")" == "$expected_id" ]] \
      || fail "refusing to replace non-COORD bundle at $target"
    mv -- "$target" "$backup"
  fi
  if ditto "$source" "$target"; then
    [[ ! -e "$backup" ]] || rm -rf -- "$backup"
  else
    [[ ! -e "$backup" ]] || mv -- "$backup" "$target"
    fail "could not install $target"
  fi
}

MENU_TARGET="$APP_DIR/COORD.app"
WINDOW_TARGET="$APP_DIR/COORD Cockpit.app"
install_bundle "$MENU_APP" "$MENU_TARGET" org.coordharness.menubar
install_bundle "$WINDOW_APP" "$WINDOW_TARGET" org.coordharness.cockpit.window

if [[ "$LAUNCH_APPS" == 1 ]]; then
  open "$MENU_TARGET"
fi

echo
echo "COORD installation complete"
echo "  endpoint:    $BOARD_URL"
echo "  database:    $DB_PATH"
echo "  LaunchAgent: $PLIST"
echo "  menu bar:    $MENU_TARGET"
echo "  cockpit:     $WINDOW_TARGET"
echo "  uninstall:   $SCRIPT_DIR/uninstall.sh"
echo "The database is preserved by repairs and by the default uninstall."
