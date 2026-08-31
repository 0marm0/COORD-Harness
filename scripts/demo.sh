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

# Fail before seeding, not after: a raw traceback out of the board server
# would otherwise be the first thing this script shows for an already-taken
# port, and it never names the port or how to find what is holding it.
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "coord-board: address already in use: 127.0.0.1:$PORT" >&2
  echo "  another process is already listening on this port." >&2
  echo "  find it:      lsof -i :$PORT" >&2
  echo "  pick another: COORD_BOARD_PORT=<port> $0 $*" >&2
  echo "  or set:       coord-board --port <port>" >&2
  exit 2
fi

[ "$RESET" = 1 ] && rm -rf "$DEMO"

if [ ! -f "$DEMO/.coordharness/coord.db" ]; then
  echo "==> seeding a synthetic board in var/demo"
  mkdir -p "$DEMO/docs/reports"
  # var/demo is a plain subdirectory of this repository, not a separate clone.
  # `git -C "$DEMO" rev-parse --git-dir` would find THIS repository's own .git
  # by searching upward past $DEMO and report success without ever creating
  # $DEMO/.git -- every git command below would then silently operate on the
  # real repository containing this script. Check for $DEMO's own .git
  # directly instead.
  if [ ! -d "$DEMO/.git" ]; then
    git -C "$DEMO" init -q
  fi
  # Belt and suspenders: refuse to go any further unless git agrees $DEMO is
  # its own repository root, so add/commit below can never reach $REPO even if
  # the check above is ever wrong.
  DEMO_TOPLEVEL="$(git -C "$DEMO" rev-parse --show-toplevel)"
  DEMO_REAL="$(cd "$DEMO" && pwd)"
  if [ "$DEMO_TOPLEVEL" != "$DEMO_REAL" ]; then
    echo "FAIL: demo git root ($DEMO_TOPLEVEL) is not $DEMO_REAL; refusing to touch git" >&2
    exit 1
  fi
  # The completion gate requires artifacts committed to version control, so the
  # demo project needs to be a real repository, not just a directory. `-C`
  # (never `cd`) keeps every git invocation scoped to $DEMO even if an earlier
  # step failed, and the explicit pathspec keeps `add` from staging anything
  # outside it. A genuine git failure here is left to propagate (no `|| true`)
  # so it is never silently swallowed.
  git -C "$DEMO" add -- .
  if [ -n "$(git -C "$DEMO" status --porcelain)" ]; then
    git -C "$DEMO" -c user.name=demo -c user.email=demo@example.invalid commit -qm "demo project"
  else
    echo "==> demo repository has nothing to commit yet; leaving it empty"
  fi
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
echo "    Doctor: COORD_PROJECT_ROOT=\"$DEMO\" \"$REPO/.venv/bin/coord\" doctor"
echo "    Stop:   Ctrl-C"
wait $SERVER
