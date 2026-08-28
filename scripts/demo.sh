#!/usr/bin/env bash
# Run the whole harness against a seeded, synthetic board.
#
# Everything this creates lives under var/demo/ inside the repository, so it
# cannot reach a real board: the native clients are launched with COORD_DB
# pointed at the demo database explicitly. That matters -- the clients otherwise
# fall back to a default location, and on a machine already running a
# coordination board they would attach to it.
#
#   ./scripts/demo.sh              seed + serve the web board
#   ./scripts/demo.sh --native     also build and launch the macOS clients
#   ./scripts/demo.sh --reset      discard the demo board and seed a fresh one
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEMO="$REPO/var/demo"
PORT="${COORD_BOARD_PORT:-7870}"
PYTHON="${PYTHON:-python3}"
NATIVE=0
RESET=0

for arg in "$@"; do
  case "$arg" in
    --native) NATIVE=1 ;;
    --reset)  RESET=1 ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

[ "$RESET" = 1 ] && rm -rf "$DEMO"

if [ ! -f "$DEMO/.coordharness/coord.db" ]; then
  echo "==> seeding a synthetic board in var/demo"
  mkdir -p "$DEMO/docs/reports"
  git -C "$DEMO" rev-parse --git-dir >/dev/null 2>&1 || git -C "$DEMO" init -q
  # The completion gate requires artifacts committed to version control, so the
  # demo project needs to be a real repository, not just a directory.
  ( cd "$DEMO" && git add -A >/dev/null 2>&1 || true
    git -c user.name=demo -c user.email=demo@example.invalid commit -qm "demo project" >/dev/null 2>&1 || true )
  COORD_PROJECT_ROOT="$DEMO" PYTHONPATH="$REPO/src" "$PYTHON" -m coordharness.demo
else
  echo "==> reusing the board in var/demo (pass --reset to rebuild it)"
fi

if [ "$NATIVE" = 1 ]; then
  command -v xcodegen >/dev/null || { echo "xcodegen not found: brew install xcodegen" >&2; exit 1; }
  echo "==> generating the Xcode project"
  ( cd "$REPO/apps" && xcodegen generate >/dev/null )
  for scheme in CoordMenuBar CoordCockpitWindow; do
    echo "==> building $scheme"
    ( cd "$REPO/apps" && xcodebuild -project CoordCockpit.xcodeproj -scheme "$scheme" \
        -configuration Release -derivedDataPath "$REPO/var/build" build >/dev/null )
  done
  cat > "$DEMO/.coordharness/menubar_panel_config.json" <<'JSON'
{ "transport": "db", "fetchTimeoutSecs": 4, "slowRingTick": false }
JSON
fi

echo "==> serving the board on http://127.0.0.1:$PORT"
COORD_PROJECT_ROOT="$DEMO" PYTHONPATH="$REPO/src" \
  "$PYTHON" -m coordharness.board.server --port "$PORT" &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT
sleep 2

if [ "$NATIVE" = 1 ]; then
  # Launched as binaries, not with `open`: `open` does not pass environment
  # variables through, so the clients would ignore COORD_DB and look elsewhere.
  # target:product -- the bundle is named for the product, not the target, so
  # the app menu does not show a build-system identifier.
  for pair in "CoordMenuBar:COORD" "CoordCockpitWindow:COORD Cockpit"; do
    app="${pair%%:*}"
    product="${pair#*:}"
    echo "==> launching $product against the demo board"
    COORD_DB="$DEMO/.coordharness/coord.db" \
    COORD_PROJECT_ROOT="$DEMO" \
    COORD_BOARD_URL="http://127.0.0.1:$PORT" \
    COORD_MENUBAR_CONFIG="$DEMO/.coordharness/menubar_panel_config.json" \
      "$REPO/var/build/Build/Products/Release/$product.app/Contents/MacOS/$product" \
      >"$REPO/var/$app.log" 2>&1 &
  done
  echo
  echo "    The menu bar item appears in the system menu bar; click it for the popover."
  echo "    To capture a window for the docs: Cmd-Shift-4, then Space, then click it."
fi

echo
echo "    Board:  http://127.0.0.1:$PORT"
echo "    Stop:   Ctrl-C"
wait $SERVER
