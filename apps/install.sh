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
                        [--enable-native-operator-writes]
                        [--install-reaper-agent] [--reaper-interval SECONDS]

  --db PATH                 Existing or new COORD authority (default: ~/.coordharness/coord.db)
  --app-dir PATH            Native app destination (default: ~/Applications)
  --python PATH             Python 3.11+ interpreter (default: python3 on PATH)
  --no-launch               Install without opening the menu-bar app
  --enable-native-operator-writes
                            Opt in to authenticated, loopback-only native task
                            reassignment. Provisions an owner-only token beside
                            coord.db. Browser actions remain read-only.
  --install-reaper-agent    Opt in to a second LaunchAgent that runs `coord-reaper`
                            on a timer (see "Scheduling the reaper" below). Off
                            by default: nothing reaps expired claims or dead
                            sessions unless this flag is passed.
  --reaper-interval SECONDS How often the reaper LaunchAgent fires (default: 300).
                            Only meaningful with --install-reaper-agent.

COORD_DB is accepted as the database selection when --db is absent.

Scheduling the reaper
----------------------
`coord-reaper` releases expired claims, reaps sessions whose process has
died, and finalizes dead runs -- the background half of "status is derived,
not stored": a lapsed lease already reads as stale on the next board read
(see docs/architecture.md), but nothing puts the claim back into circulation,
and nothing re-checks a session's pid, until something actually walks the
tables and does it. This installer never runs it for you. Pass
--install-reaper-agent to also install a `launchd` agent
(org.coordharness.reaper) that runs `coord-reaper` against the same database
on the interval above; its stdout/stderr land in
~/Library/Logs/COORD/coord-reaper.{stdout,stderr}.log (or $COORD_LOG_DIR if
set). Remove it later with:
  launchctl bootout "gui/$(id -u)/org.coordharness.reaper"
  rm ~/Library/LaunchAgents/org.coordharness.reaper.plist
Prefer cron, a different scheduler, or a manual `coord-reaper` on your own
cadence instead -- the LaunchAgent here is one convenient default, not a
requirement.
EOF
}

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
BOARD_URL="http://127.0.0.1:7870"
LABEL="org.coordharness.board"
LABEL_REAPER="org.coordharness.reaper"
DB_INPUT="${COORD_DB:-$HOME/.coordharness/coord.db}"
APP_DIR_INPUT="${COORD_APP_DIR:-$HOME/Applications}"
PYTHON_COMMAND="${COORD_PYTHON:-python3}"
LAUNCH_APPS=1
# Opt-in only: installing the board above must never also schedule the
# reaper as a side effect. See "Scheduling the reaper" in usage() for why
# this exists and what it does.
INSTALL_REAPER_AGENT=0
ENABLE_NATIVE_OPERATOR_WRITES=0
REAPER_INTERVAL_S="${COORD_REAPER_INTERVAL_S:-300}"

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
    --install-reaper-agent)
      INSTALL_REAPER_AGENT=1
      shift
      ;;
    --enable-native-operator-writes)
      ENABLE_NATIVE_OPERATOR_WRITES=1
      shift
      ;;
    --reaper-interval)
      [[ $# -ge 2 ]] || fail "--reaper-interval requires a number of seconds"
      REAPER_INTERVAL_S="$2"
      shift 2
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
PLIST_REAPER="$LAUNCH_AGENT_DIR/$LABEL_REAPER.plist"
DERIVED="$REPO_ROOT/var/build"
BUILD_LOG="$REPO_ROOT/var/install_log.txt"
CONFIG_PATH="$HOME/.coordharness/menubar_panel_config.json"

[[ "$DB_PATH" != "/" && "$DB_PATH" != "$HOME" ]] || fail "refusing unsafe database path: $DB_PATH"
[[ "$RUNTIME_ROOT" != "/" && "$RUNTIME_ROOT" != "$HOME" ]] || fail "refusing unsafe runtime root: $RUNTIME_ROOT"
[[ "$APP_DIR" != "/" && "$APP_DIR" != "$HOME" ]] || fail "refusing unsafe app directory: $APP_DIR"
[[ "$REAPER_INTERVAL_S" =~ ^[0-9]+$ && "$REAPER_INTERVAL_S" -ge 30 ]] \
  || fail "--reaper-interval must be a whole number of seconds, >= 30 (got: $REAPER_INTERVAL_S)"

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
NATIVE_OPERATOR_TOKEN_PATH="$(dirname -- "$DB_PATH")/operator-token"
if [[ "$ENABLE_NATIVE_OPERATOR_WRITES" == 1 ]]; then
  "$VENV/bin/python" - "$NATIVE_OPERATOR_TOKEN_PATH" <<'PY'
import os
import pathlib
import secrets
import stat
import sys

path = pathlib.Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
if not path.exists():
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(secrets.token_urlsafe(48) + "\n")
metadata = path.lstat()
if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
    raise SystemExit("native operator token must be a regular non-symlink file")
if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.getuid():
    raise SystemExit("native operator token must be owner-owned mode 0600")
PY
fi

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
  "$RUNTIME_ROOT" "$LOG_DIR/coord-board.stdout.log" "$LOG_DIR/coord-board.stderr.log" \
  "$ENABLE_NATIVE_OPERATOR_WRITES" "$NATIVE_OPERATOR_TOKEN_PATH" <<'PY'
import pathlib
import plistlib
import sys

(
    path,
    label,
    executable,
    database,
    working_directory,
    stdout_path,
    stderr_path,
    enable_native_operator_writes,
    native_operator_token_path,
) = sys.argv[1:]
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
if enable_native_operator_writes == "1":
    payload["EnvironmentVariables"].update(
        {
            "COORD_NATIVE_OPERATOR_WRITES": "1",
            "COORD_NATIVE_OPERATOR_TOKEN_FILE": native_operator_token_path,
        }
    )
destination = pathlib.Path(path)
temporary = destination.with_name(destination.name + ".coord-install.tmp")
with temporary.open("wb") as stream:
    plistlib.dump(payload, stream, sort_keys=True)
temporary.replace(destination)
PY
chmod 600 "$PLIST"
launchctl bootout "gui/$UID/$LABEL" >/dev/null 2>&1 || true
bootstrap_ok=0
for _attempt in {1..10}; do
  if launchctl bootstrap "gui/$UID" "$PLIST"; then
    bootstrap_ok=1
    break
  fi
  sleep 0.5
done
[[ "$bootstrap_ok" == 1 ]] \
  || fail "LaunchAgent $LABEL could not be bootstrapped after a bounded retry"
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
if [[ "$ENABLE_NATIVE_OPERATOR_WRITES" == 1 ]]; then
  echo "  operator:    native reassignment enabled (loopback + private token)"
else
  echo "  operator:    read-only (enable with --enable-native-operator-writes)"
fi
echo "  uninstall:   $SCRIPT_DIR/uninstall.sh"
echo "The database is preserved by repairs and by the default uninstall."

# Opt-in only (--install-reaper-agent): nothing above this line schedules the
# reaper, and installing the board must never do it as a side effect. Without
# this -- or an equivalent cron entry, or a human running `coord-reaper` by
# hand -- expired claims are never released and dead sessions are never
# reaped; see the "Scheduling the reaper" section of `apps/install.sh --help`
# and docs/operators-handbook.md for what that costs.
if [[ "$INSTALL_REAPER_AGENT" == 1 ]]; then
  echo
  echo "optional: install reaper LaunchAgent (--install-reaper-agent)"
  [[ -x "$VENV/bin/coord-reaper" ]] || fail "coord-reaper was not installed into $VENV"
  "$VENV/bin/python" - "$PLIST_REAPER" "$LABEL_REAPER" "$VENV/bin/coord-reaper" "$DB_PATH" \
    "$RUNTIME_ROOT" "$REAPER_INTERVAL_S" \
    "$LOG_DIR/coord-reaper.stdout.log" "$LOG_DIR/coord-reaper.stderr.log" <<'PY'
import pathlib
import plistlib
import sys

(
    path, label, executable, database, working_directory, interval_s,
    stdout_path, stderr_path,
) = sys.argv[1:]
payload = {
    "Label": label,
    "ProgramArguments": [executable, "--db", database],
    "EnvironmentVariables": {"COORD_DB": database},
    "WorkingDirectory": working_directory,
    # RunAtLoad + StartInterval, deliberately not KeepAlive: coord-reaper is a
    # batch job that runs to completion and exits 0 every time, not a
    # long-lived service like coord-board above. KeepAlive treats a clean
    # exit as a crash and respawns immediately, which would turn a periodic
    # sweep into a tight loop; StartInterval is launchd's own "run this every
    # N seconds" primitive and is what a run-to-completion job should use.
    "RunAtLoad": True,
    "StartInterval": int(interval_s),
    "ProcessType": "Background",
    "StandardOutPath": stdout_path,
    "StandardErrorPath": stderr_path,
}
destination = pathlib.Path(path)
temporary = destination.with_name(destination.name + ".coord-install.tmp")
with temporary.open("wb") as stream:
    plistlib.dump(payload, stream, sort_keys=True)
temporary.replace(destination)
PY
  chmod 600 "$PLIST_REAPER"
  launchctl bootout "gui/$UID/$LABEL_REAPER" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$UID" "$PLIST_REAPER"
  launchctl kickstart -k "gui/$UID/$LABEL_REAPER"
  echo "  reaper agent: $PLIST_REAPER (every ${REAPER_INTERVAL_S}s)"
  echo "  reaper logs:  $LOG_DIR/coord-reaper.stdout.log, $LOG_DIR/coord-reaper.stderr.log"
  echo "  remove with:  launchctl bootout \"gui/\$UID/$LABEL_REAPER\"; rm \"$PLIST_REAPER\""
fi
