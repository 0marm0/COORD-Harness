#!/usr/bin/env bash
# Remove only the installed COORD runtime, service, and native bundles.
# Lifecycle data and user configuration are preserved intentionally.
set -euo pipefail

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: apps/uninstall.sh [--app-dir PATH]

Stops and removes the dedicated COORD board LaunchAgent, the isolated installed
runtime, and the two COORD app bundles. The selected coord.db, ~/.coordharness,
and native preferences are preserved. Delete data separately only after making
and verifying a backup.
EOF
}

LABEL="org.coordharness.board"
LABEL_REAPER="org.coordharness.reaper"
APP_DIR_INPUT="${COORD_APP_DIR:-$HOME/Applications}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir)
      [[ $# -ge 2 ]] || fail "--app-dir requires a path"
      APP_DIR_INPUT="$2"
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

absolute_existing_parent() {
  local raw parent leaf
  raw="$(expand_home "$1")"
  [[ "$raw" == /* ]] || raw="$PWD/$raw"
  parent="$(dirname -- "$raw")"
  leaf="$(basename -- "$raw")"
  if [[ -d "$parent" ]]; then
    printf '%s/%s\n' "$(cd -- "$parent" && pwd -P)" "$leaf"
  else
    printf '%s\n' "$raw"
  fi
}

APP_DIR="$(absolute_existing_parent "$APP_DIR_INPUT")"
RUNTIME_ROOT="${COORD_RUNTIME_ROOT:-$HOME/Library/Application Support/COORD}"
RUNTIME_ROOT="$(absolute_existing_parent "$RUNTIME_ROOT")"
LOG_DIR="${COORD_LOG_DIR:-$HOME/Library/Logs/COORD}"
LOG_DIR="$(absolute_existing_parent "$LOG_DIR")"
LAUNCH_AGENT_DIR="${COORD_LAUNCH_AGENT_DIR:-$HOME/Library/LaunchAgents}"
LAUNCH_AGENT_DIR="$(absolute_existing_parent "$LAUNCH_AGENT_DIR")"
PLIST="$LAUNCH_AGENT_DIR/$LABEL.plist"
PLIST_REAPER="$LAUNCH_AGENT_DIR/$LABEL_REAPER.plist"
MENU_TARGET="$APP_DIR/COORD.app"
WINDOW_TARGET="$APP_DIR/COORD Cockpit.app"

[[ "$APP_DIR" != "/" && "$APP_DIR" != "$HOME" ]] || fail "refusing unsafe app directory: $APP_DIR"
[[ "$RUNTIME_ROOT" != "/" && "$RUNTIME_ROOT" != "$HOME" ]] || fail "refusing unsafe runtime root: $RUNTIME_ROOT"
[[ "$LOG_DIR" != "/" && "$LOG_DIR" != "$HOME" ]] || fail "refusing unsafe log directory: $LOG_DIR"

preserved_db="${COORD_DB:-}"
if [[ -z "$preserved_db" ]]; then
  preserved_db="$(defaults read org.coordharness.menubar coordharness.coordDBPath 2>/dev/null || true)"
fi
if [[ -z "$preserved_db" ]]; then
  preserved_db="$HOME/.coordharness/coord.db"
fi

echo "1/4  stop native apps"
osascript -e 'quit app "COORD"' >/dev/null 2>&1 || true
osascript -e 'quit app "COORD Cockpit"' >/dev/null 2>&1 || true
pkill -x "COORD" >/dev/null 2>&1 || true
pkill -x "COORD Cockpit" >/dev/null 2>&1 || true

echo "2/4  stop and remove dedicated LaunchAgent"
launchctl bootout "gui/$UID/$LABEL" >/dev/null 2>&1 || true
if [[ -e "$PLIST" ]]; then
  installed_label="$(/usr/libexec/PlistBuddy -c 'Print :Label' "$PLIST" 2>/dev/null || true)"
  [[ "$installed_label" == "$LABEL" ]] || fail "refusing to remove foreign plist at $PLIST"
  rm -f -- "$PLIST"
fi

# Optional, only present if the operator passed --install-reaper-agent to
# apps/install.sh -- absence here is not an error. Mirrors the board
# LaunchAgent removal above: guarded bootout + label-verified rm.
launchctl bootout "gui/$UID/$LABEL_REAPER" >/dev/null 2>&1 || true
if [[ -e "$PLIST_REAPER" ]]; then
  installed_label_reaper="$(/usr/libexec/PlistBuddy -c 'Print :Label' "$PLIST_REAPER" 2>/dev/null || true)"
  [[ "$installed_label_reaper" == "$LABEL_REAPER" ]] \
    || fail "refusing to remove foreign plist at $PLIST_REAPER"
  rm -f -- "$PLIST_REAPER"
fi

bundle_identifier() {
  /usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$1/Contents/Info.plist" 2>/dev/null || true
}

remove_bundle() {
  local target="$1" expected_id="$2"
  [[ -e "$target" ]] || return 0
  [[ "$(bundle_identifier "$target")" == "$expected_id" ]] \
    || fail "refusing to remove non-COORD bundle at $target"
  rm -rf -- "$target"
}

echo "3/4  remove exact native bundles"
remove_bundle "$MENU_TARGET" org.coordharness.menubar
remove_bundle "$WINDOW_TARGET" org.coordharness.cockpit.window

echo "4/4  remove isolated runtime and service logs"
if [[ -e "$RUNTIME_ROOT" ]]; then
  [[ -f "$RUNTIME_ROOT/.coord-install-marker" ]] \
    || fail "refusing to remove unmarked runtime at $RUNTIME_ROOT"
  [[ "$(tr -d '\r\n' < "$RUNTIME_ROOT/.coord-install-marker")" == "$LABEL" ]] \
    || fail "refusing to remove runtime with an unknown marker at $RUNTIME_ROOT"
  rm -rf -- "$RUNTIME_ROOT"
fi
if [[ -d "$LOG_DIR" ]]; then
  rm -f -- "$LOG_DIR/coord-board.stdout.log" "$LOG_DIR/coord-board.stderr.log"
  rmdir "$LOG_DIR" >/dev/null 2>&1 || true
fi

echo
echo "COORD runtime and apps removed."
echo "Preserved database: $preserved_db"
echo "Preserved state/config: $HOME/.coordharness"
echo "Preserved native preferences for a safe reinstall."
