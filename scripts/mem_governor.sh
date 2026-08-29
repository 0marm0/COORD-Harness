#!/usr/bin/env bash
SAFETY_GB=6

source "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)/lib_safety.sh"
command -v mem_free_gb >/dev/null 2>&1 || {
  echo "mem_governor.sh: lib_safety.sh did not load from this script's directory" >&2
  exit 3
}

# No default headroom. The old `|| echo "30.0"` answered an unreadable machine
# with 30 GB of fabricated space, which admits every job; mem_free_gb's former
# silent 0.0 held every job forever. An admission gate that cannot measure the
# resource it is gating has to say so and stop, not pick a number.
headroom_gb() { mem_free_gb; }
UNMEASURED="mem_governor.sh: cannot measure free memory here, so nothing is admitted or held on a guess"

case "$1" in
  status)
    H=$(headroom_gb) || { echo "$UNMEASURED" >&2; exit 3; }
    echo "headroom: $H GB (free+inactive+purgeable; safety buffer ${SAFETY_GB} GB)" ;;
  check)
    NEED="$2"; H=$(headroom_gb) || { echo "$UNMEASURED" >&2; exit 3; }
    awk -v h="$H" -v n="$NEED" -v s="$SAFETY_GB" 'BEGIN{exit !(h >= n+s)}' ;;
  wait)
    NEED="$2"; LABEL="${3:-job}"
    while true; do
      H=$(headroom_gb) || { echo "$UNMEASURED" >&2; exit 3; }
      if awk -v h="$H" -v n="$NEED" -v s="$SAFETY_GB" 'BEGIN{exit !(h >= n+s)}'; then
        echo "$(date '+%H:%M:%S') [governor] ADMIT $LABEL (need ${NEED}G, headroom ${H}G)"
        exit 0
      fi
      echo "$(date '+%H:%M:%S') [governor] HOLD $LABEL (need ${NEED}G + ${SAFETY_GB}G buffer, headroom ${H}G) — retry 30s"
      sleep 30
    done ;;
  *)
    echo "usage: mem_governor.sh {wait|check} <needed_gb> [label] | status"; exit 2 ;;
esac
