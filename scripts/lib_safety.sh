
# Every reader here is Darwin-only, and each was called with its errors sent to
# /dev/null. On a machine without them awk received no input, and its END block
# still printed "%.1f" of unset variables: a confident 0.0, indistinguishable
# from a machine genuinely out of memory. `mem_governor.sh wait` then held
# forever against a number nobody had measured. So: report the platform, and
# return non-zero, rather than name a figure.
mem_free_gb() {
    local pct total free_gb
    if ! command -v sysctl >/dev/null 2>&1 ||
        { ! command -v memory_pressure >/dev/null 2>&1 && ! command -v vm_stat >/dev/null 2>&1; }; then
        printf '%s\n' \
            "mem_free_gb: unsupported on this platform -- needs macOS sysctl plus memory_pressure or vm_stat" >&2
        return 3
    fi
    pct=$(memory_pressure 2>/dev/null | awk -F: '/free percentage/{gsub(/[ %]/,"",$2); print $2; exit}')
    total=$(sysctl -n hw.memsize 2>/dev/null)
    if [ -n "$pct" ] && [ -n "$total" ]; then
        awk -v p="$pct" -v t="$total" 'BEGIN{ printf "%.1f", p/100.0*t/1073741824 }'
        return 0
    fi
    # The fallback exits non-zero and prints nothing when it read no page
    # counts, so an empty result can never be rounded into a plausible number.
    free_gb=$(vm_stat 2>/dev/null | awk '
        /page size of/ { ps=$8 } /Pages free/ { f=$3 } /Pages inactive/ { i=$3 } /Pages purgeable/ { p=$3 }
        END { if (ps == "" || f == "") exit 1
              gsub(/\./,"",f); gsub(/\./,"",i); gsub(/\./,"",p); printf "%.1f", (f+i+p)*ps/1073741824 }')
    if [ -z "$free_gb" ]; then
        printf '%s\n' \
            "mem_free_gb: the macOS memory readers are present but returned nothing usable" >&2
        return 3
    fi
    printf '%s' "$free_gb"
}

safety_swap_used_gb() {
    sysctl -n vm.swapusage 2>/dev/null | awk '{
        for(i=1;i<=NF;i++) if($i=="used"){
            v=$(i+2); u=v; gsub(/[0-9.]/,"",u); gsub(/[A-Za-z]/,"",v);
            mult=(u=="G"?1:(u=="M"?1/1024:(u=="K"?1/1048576:1/1024)));
            printf "%.3f", v*mult; exit
        }
    }' || echo 0
}

safety_load_avg_1m() {
    sysctl -n vm.loadavg 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1
}

gpu_lane_lock_dir() {
    printf '%s\n' "${COORD_GPU_LOCK_DIR:-/tmp/coord_gpu.lock}"
}

gpu_lock_owner_entry() {
    local lock_dir="${COORD_GPU_LOCK_DIR:-/tmp/coord_gpu.lock}"
    [ -f "$lock_dir/owner" ] && cat "$lock_dir/owner" 2>/dev/null || true
}

cheap_signal_snapshot_json() {
    local free_gb="${1:-}" swap_gb="${2:-}" load_1m="${3:-}"
    [ -n "$free_gb" ] || free_gb="$(mem_free_gb 2>/dev/null || echo '')"
    [ -n "$swap_gb" ] || swap_gb="$(safety_swap_used_gb 2>/dev/null || echo '')"
    [ -n "$load_1m" ] || load_1m="$(safety_load_avg_1m 2>/dev/null || echo '')"
    local owner pgid child_pid wrapper_pid
    owner="$(gpu_lock_owner_entry)"
    pgid="$(gpu_pgid_field pgid 2>/dev/null || echo '')"
    child_pid="$(gpu_pgid_field child_pid 2>/dev/null || echo '')"
    wrapper_pid="$(gpu_pgid_field wrapper_pid 2>/dev/null || echo '')"
    /usr/bin/python3 - "$free_gb" "$swap_gb" "$load_1m" "$owner" "$pgid" "$child_pid" "$wrapper_pid" <<'PY' 2>/dev/null
import json, sys, time
free_gb, swap_gb, load_1m, owner, pgid, child_pid, wrapper_pid = sys.argv[1:]

def num(v):
    try:
        return round(float(v), 3)
    except Exception:
        return None

def integer(v):
    try:
        return int(v)
    except Exception:
        return None

print(json.dumps({
    "schema_version": 1,
    "sampled_at": time.time(),
    "source": "lib_safety.cheap_signal_snapshot_json",
    "free_ram_gb": num(free_gb),
    "swap_used_gb": num(swap_gb),
    "load_1m": num(load_1m),
    "gpu_lock_owner": owner or None,
    "gpu_lock_pgid": integer(pgid),
    "gpu_child_pid": integer(child_pid),
    "gpu_wrapper_pid": integer(wrapper_pid),
}, separators=(",", ":")))
PY
}

_LIB_SAFETY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
_PROTECTED_FILE="$_LIB_SAFETY_DIR/protected_jobs.txt"
_PROTECTED_FALLBACK_RE='run_ui\.py|webapp\.py|[Cc]laude|resource_watchdog|resource_governor|mem_governor|uvicorn|gpu_job'
protected_re() {
    local patterns
    if [ -f "$_PROTECTED_FILE" ]; then
        patterns="$(grep -vE '^[[:space:]]*(#|$)' "$_PROTECTED_FILE" | paste -sd'|' -)"
    fi
    [ -n "$patterns" ] && { printf '%s\n' "$patterns"; return; }
    printf '%s\n' "$_PROTECTED_FALLBACK_RE"
}

gpu_lane_owner_pid() {
    gpu_lock_owner_entry | grep -oE '^[0-9]+' 2>/dev/null | head -1
}

gpu_lane_pgid_file() {
    printf '%s/pgid.json\n' "$(gpu_lane_lock_dir)"
}

GPU_PGID_FILE="${COORD_GPU_LOCK_DIR:-/tmp/coord_gpu.lock}/pgid.json"

gpu_pgid_field() {
    local pgid_file
    pgid_file="$(gpu_lane_pgid_file)"
    [ -f "$pgid_file" ] || return 0
    /usr/bin/python3 - "$pgid_file" "$1" <<'PY' 2>/dev/null
import json, sys
try:
    v = json.load(open(sys.argv[1])).get(sys.argv[2])
    if v is not None:
        print(v)
except Exception:
    pass
PY
}

safety_pid_alive() {
    local pid="${1:-}"
    case "$pid" in ''|*[!0-9]*) return 1 ;; esac
    kill -0 "$pid" 2>/dev/null
}

safety_pid_command() {
    local pid="${1:-}"
    safety_pid_alive "$pid" || return 1
    ps -o command= -p "$pid" 2>/dev/null || true
}

gpu_lock_has_live_owner() {
    local owner owner_pid pgid child_pid wrapper_pid cmd
    owner="$(gpu_lock_owner_entry)"
    owner_pid="${owner%%:*}"
    pgid="$(gpu_pgid_field pgid)"
    child_pid="$(gpu_pgid_field child_pid)"
    wrapper_pid="$(gpu_pgid_field wrapper_pid)"

    if [ -n "$pgid" ] && gpu_pgid_is_ours "$pgid"; then
        return 0
    fi
    if safety_pid_alive "$wrapper_pid"; then
        cmd="$(safety_pid_command "$wrapper_pid")"
        printf '%s\n' "$cmd" | grep -q "gpu_job.sh" && return 0
    fi
    if safety_pid_alive "$child_pid"; then
        return 0
    fi
    if safety_pid_alive "$owner_pid"; then
        cmd="$(safety_pid_command "$owner_pid")"
        printf '%s\n' "$cmd" | grep -q "gpu_job.sh" && return 0
    fi
    return 1
}

release_stale_gpu_lock() {
    local lock_dir owner pgid child_pid wrapper_pid
    lock_dir="$(gpu_lane_lock_dir)"
    [ -d "$lock_dir" ] || return 1
    if gpu_lock_has_live_owner; then
        return 1
    fi
    owner="$(gpu_lock_owner_entry)"
    pgid="$(gpu_pgid_field pgid)"
    child_pid="$(gpu_pgid_field child_pid)"
    wrapper_pid="$(gpu_pgid_field wrapper_pid)"
    rm -f "$lock_dir/owner" "$lock_dir/pgid.json" 2>/dev/null || true
    rmdir "$lock_dir" 2>/dev/null || return 1
    printf 'released stale GPU lane lock (owner=%s pgid=%s child=%s wrapper=%s)\n' \
        "${owner:-unknown}" "${pgid:-none}" "${child_pid:-none}" "${wrapper_pid:-none}"
    return 0
}

gpu_pgid_is_ours() {
    local pgid="$1" wpid cpid own_pgid
    case "$pgid" in ''|*[!0-9]*) return 1 ;; esac
    own_pgid="$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')"
    [ "$pgid" != "$own_pgid" ] || return 1
    kill -0 -- -"$pgid" 2>/dev/null || return 1
    cpid="$(gpu_pgid_field child_pid)"
    if [ -n "$cpid" ]; then
        [ "$(ps -o pgid= -p "$cpid" 2>/dev/null | tr -d ' ')" = "$pgid" ] || return 1
    fi
    wpid="$(gpu_pgid_field wrapper_pid)"
    if [ -n "$wpid" ] && [ "$wpid" -gt 0 ] 2>/dev/null && kill -0 "$wpid" 2>/dev/null; then
        ps -o command= -p "$wpid" 2>/dev/null | grep -q "gpu_job.sh" || return 1
    fi
    return 0
}

kill_pgid_escalate() {
    local pgid="$1" grace="${2:-4}" i=0 steps own_pgid
    case "$pgid" in ''|*[!0-9]*) return 2 ;; esac
    own_pgid="$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')"
    [ -n "$own_pgid" ] && [ "$pgid" = "$own_pgid" ] && return 2
    kill -TERM -- -"$pgid" 2>/dev/null || true
    steps=$(( grace * 4 ))
    while [ "$i" -lt "$steps" ]; do
        kill -0 -- -"$pgid" 2>/dev/null || break
        sleep 0.25; i=$(( i + 1 ))
    done
    kill -KILL -- -"$pgid" 2>/dev/null || true
    sleep 0.2
    kill -0 -- -"$pgid" 2>/dev/null && return 1 || return 0
}
