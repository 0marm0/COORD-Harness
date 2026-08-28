SAFETY_GB=6

source "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)/lib_safety.sh" 2>/dev/null
headroom_gb() { mem_free_gb 2>/dev/null || echo "30.0"; }

case "$1" in
  status)
    echo "headroom: $(headroom_gb) GB (free+inactive+purgeable; safety buffer ${SAFETY_GB} GB)" ;;
  check)
    NEED="$2"; H=$(headroom_gb)
    awk -v h="$H" -v n="$NEED" -v s="$SAFETY_GB" 'BEGIN{exit !(h >= n+s)}' ;;
  wait)
    NEED="$2"; LABEL="${3:-job}"
    while true; do
      H=$(headroom_gb)
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
