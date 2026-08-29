#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_LOCAL="${COORD_HOME:-$REPO_ROOT/.coordharness}"
MODES_JSON="$DATA_LOCAL/resource_modes.json"
MODE_FILE="$DATA_LOCAL/resource_mode.txt"
TRIGGER_FILE="$DATA_LOCAL/governor_trigger"
AUTO_MODE_DISABLE_FILE="$DATA_LOCAL/auto_mode_disabled"
MANUAL_OVERRIDE_FILE="$DATA_LOCAL/mode_manual_override.json"
AUTO_FULL_IDLE_S="${AUTO_FULL_IDLE_S:-720}"
MANUAL_MODE_HOLD_S="${COORD_MANUAL_MODE_HOLD_S:-0}"

mkdir -p "$DATA_LOCAL"

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 {full|medium|light|pause|auto} [--quiet]" >&2
    exit 2
fi
REQUESTED_MODE="$1"
NEW_MODE="$REQUESTED_MODE"
QUIET="${2:-}"

hid_idle_s() {
    ioreg -c IOHIDSystem 2>/dev/null | awk '/HIDIdleTime/ {print int($NF / 1000000000); exit}'
}

on_ac_power() {
    pmset -g batt 2>/dev/null | head -1 | grep -q "AC Power"
}

resolve_auto_mode() {
    if [[ "${COORD_AUTO_MODE_DISABLE:-0}" = "1" || -f "$AUTO_MODE_DISABLE_FILE" ]]; then
        echo "disabled"
        return 0
    fi
    local idle_s on_ac
    idle_s="$(hid_idle_s)"
    case "$idle_s" in ''|*[!0-9]*) idle_s=0 ;;
    esac
    on_ac=0
    on_ac_power && on_ac=1
    if [[ "$idle_s" -ge "$AUTO_FULL_IDLE_S" && "$on_ac" = "1" ]]; then
        echo "full"
    else
        echo "medium"
    fi
}

manual_override_mode() {
    [[ -f "$MANUAL_OVERRIDE_FILE" ]] || return 1
    python3 - "$MANUAL_OVERRIDE_FILE" "$MANUAL_MODE_HOLD_S" <<'PY'
import json, sys, time

path = sys.argv[1]
default_hold_s = int(float(sys.argv[2] or 0))
try:
    data = json.load(open(path))
except Exception:
    sys.exit(1)
mode = str(data.get("mode") or "")
if mode not in {"full", "medium", "light", "pause"}:
    sys.exit(1)
hold_s = int(float(data.get("hold_s", default_hold_s) or 0))
updated = int(float(data.get("updated", 0) or 0))
if hold_s > 0 and updated > 0 and time.time() - updated > hold_s:
    sys.exit(1)
print(mode)
PY
}

write_manual_override() {
    local mode="$1"
    local now tmp
    now="$(date '+%s')"
    tmp="${MANUAL_OVERRIDE_FILE}.tmp.$$"
    printf '{"mode":"%s","updated":%s,"hold_s":%s}\n' "$mode" "$now" "$MANUAL_MODE_HOLD_S" > "$tmp"
    mv -f "$tmp" "$MANUAL_OVERRIDE_FILE"
}

case "$NEW_MODE" in
    full|medium|light|pause) ;;
    auto)
        if [[ "$QUIET" != "--quiet" ]]; then
            rm -f "$MANUAL_OVERRIDE_FILE"
        elif HELD_MODE="$(manual_override_mode 2>/dev/null)"; then
            NEW_MODE="$HELD_MODE"
        else
            RESOLVED_MODE="$(resolve_auto_mode)"
            if [[ "$RESOLVED_MODE" = "disabled" ]]; then
                [[ "$QUIET" = "--quiet" ]] || echo "[set_mode] auto-mode disabled by $AUTO_MODE_DISABLE_FILE or COORD_AUTO_MODE_DISABLE=1"
                exit 0
            fi
            NEW_MODE="$RESOLVED_MODE"
        fi
        ;;
    *)
        echo "ERROR: unknown mode '$NEW_MODE' — must be one of: full medium light pause auto" >&2
        exit 2
        ;;
esac

if [[ ! -f "$MODES_JSON" ]]; then
    echo "WARN: $MODES_JSON not found — proceeding anyway"
else
    if ! python3 -c "
import json, sys
d = json.load(open('$MODES_JSON'))
if '$NEW_MODE' not in d.get('modes', {}):
    print('ERROR: mode not in resource_modes.json', file=sys.stderr)
    sys.exit(1)
" 2>&1; then
        echo "ERROR: '$NEW_MODE' is not a valid mode in $MODES_JSON" >&2
        exit 1
    fi
fi

OLD_MODE="(unset)"
[[ -f "$MODE_FILE" ]] && OLD_MODE="$(cat "$MODE_FILE" | tr -d '[:space:]')"
if [[ "$OLD_MODE" = "$NEW_MODE" ]]; then
    if [[ "$REQUESTED_MODE" != "auto" ]]; then
        write_manual_override "$NEW_MODE"
    fi
    [[ "$QUIET" = "--quiet" ]] || echo "[set_mode] already $NEW_MODE"
    exit 0
fi

TMPF="${MODE_FILE}.tmp.$$"
echo "$NEW_MODE" > "$TMPF"
mv -f "$TMPF" "$MODE_FILE"
if [[ "$REQUESTED_MODE" != "auto" ]]; then
    write_manual_override "$NEW_MODE"
fi

date '+%Y-%m-%dT%H:%M:%S%z' > "$TRIGGER_FILE"

if [[ "$QUIET" != "--quiet" ]]; then
    echo "[set_mode] $OLD_MODE → $NEW_MODE   (trigger written → governor will enforce within ~30s)"
    echo "[set_mode] mode file: $MODE_FILE"
    echo "[set_mode] governor status: $DATA_LOCAL/governor_status.json"
fi
