#!/usr/bin/env bash
# Shared safety helpers for the e2e rig. Source this file; do not run it.
#
# Rules this file enforces for every rig script:
#   * Scratch root (SHELF_SCRATCH) must be an absolute path that is not "/", not the user's home, not under
#     /tmp or /var/tmp, and contains no ".kiro", ".aws", ".claude" or ".ssh" component.
#   * The rig starts processes only through rig_spawn, which writes a pid record
#     ($RUN_DIR/<name>.pid: pid, process start time, kind, identity key=value).
#   * The rig stops processes only through rig_stop. rig_stop refuses unless the pid record exists AND the
#     live process still matches it (start time, and the identity variable in /proc/<pid>/environ).
#     It kills recorded pids only (the root and the descendants it lists before killing). No pkill, no
#     killall, no name matching, never "kirocrew stop|restart|service".
#   * The owner's real Kiro Crew gateway port (5476) and the owner's live CAO port (9889) are never used.

# shellcheck disable=SC2034  # variables are used by the scripts that source this file
set -u

RIG_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXAMPLE_DIR="$(cd "$RIG_LIB_DIR/../.." && pwd)"

REAL_HOME="$(getent passwd "$(id -u)" | cut -d: -f6)"
OWNER_GATEWAY_PORT=5476
OWNER_CAO_PORT=9889

SHELF_SCRATCH="${SHELF_SCRATCH:-$REAL_HOME/kc-shelf-e2e}"
ISO_HOME="${SHELF_ISO_HOME:-$SHELF_SCRATCH/home}"
CAO_HOME="${SHELF_CAO_HOME:-$SHELF_SCRATCH/cao-home}"
WORK_DIR="${SHELF_WORK_DIR:-$SHELF_SCRATCH/work-c}"
RUN_DIR="$WORK_DIR/run"
LOG_DIR="$WORK_DIR/logs"

KC_PORT="${SHELF_KC_PORT:-5499}"
CAO_PORT="${SHELF_CAO_PORT:-9990}"
KC_VERSION="${SHELF_KC_VERSION:-0.12.2}"
CAO_VENV="${SHELF_CAO_VENV:-$SHELF_SCRATCH/cao-venv}"
NODE_BIN_DIR="${SHELF_NODE_BIN:-}"

# Chats on the scratch gateway run on the Claude backend (claude-agent-acp adapter + Claude Code) against the
# scripted model server mock_model.py on loopback. SHELF_CLAUDE_BACKEND=0 leaves the gateway's default backend.
CLAUDE_BACKEND="${SHELF_CLAUDE_BACKEND:-1}"
MODEL_PORT="${SHELF_MODEL_PORT:-9981}"
MODEL_SCRIPT="${SHELF_MODEL_SCRIPT:-}"
ACP_VERSION="${SHELF_ACP_VERSION:-0.99.1}"
ACP_DIR="${SHELF_ACP_DIR:-$WORK_DIR/acp}"
ACP_BIN="$ACP_DIR/node_modules/.bin/claude-agent-acp"
CLAUDE_CODE_BIN="${SHELF_CLAUDE_CODE:-}"

die() {
    echo "rig: $*" >&2
    exit 1
}

say() {
    echo "rig: $*"
}

# ---------------------------------------------------------------------------------------------------
# Scratch and port validation
# ---------------------------------------------------------------------------------------------------

rig_check_scratch() {
    local p="$SHELF_SCRATCH"
    [[ -n "$p" ]] || die "SHELF_SCRATCH is empty"
    [[ "$p" == /* ]] || die "SHELF_SCRATCH must be absolute: $p"
    [[ "$p" != "/" ]] || die "SHELF_SCRATCH must not be /"
    [[ "$p" != "$REAL_HOME" && "$p" != "$REAL_HOME/" ]] || die "SHELF_SCRATCH must not be the home directory"
    case "$p" in
        /tmp | /tmp/* | /var/tmp | /var/tmp/*) die "SHELF_SCRATCH must not be under /tmp (wiped on restart): $p" ;;
    esac
    case "/$p/" in
        */.kiro/* | */.aws/* | */.claude/* | */.ssh/*) die "SHELF_SCRATCH must not contain .kiro, .aws, .claude or .ssh: $p" ;;
    esac
    local d
    for d in "$ISO_HOME" "$CAO_HOME" "$WORK_DIR" "$ACP_DIR"; do
        case "$d" in
            "$p"/*) ;;
            *) die "$d must live inside SHELF_SCRATCH ($p)" ;;
        esac
    done
    [[ "$ISO_HOME" != "$REAL_HOME" ]] || die "isolated HOME equals the real HOME"
    mkdir -p "$ISO_HOME" "$CAO_HOME" "$RUN_DIR" "$LOG_DIR" || die "cannot create scratch directories"
}

rig_check_ports() {
    [[ "$KC_PORT" != "$OWNER_GATEWAY_PORT" ]] || die "refusing port $OWNER_GATEWAY_PORT: it belongs to the owner's real Kiro Crew gateway"
    [[ "$CAO_PORT" != "$OWNER_CAO_PORT" ]] || die "refusing port $OWNER_CAO_PORT: it belongs to the owner's live CAO"
    [[ "$KC_PORT" =~ ^[0-9]+$ && "$CAO_PORT" =~ ^[0-9]+$ && "$MODEL_PORT" =~ ^[0-9]+$ ]] || die "ports must be numbers"
    case "$MODEL_PORT" in
        "$OWNER_GATEWAY_PORT" | "$OWNER_CAO_PORT" | "$KC_PORT" | "$CAO_PORT") die "refusing model port $MODEL_PORT: it is the owner's or another rig port" ;;
    esac
}

port_listening() {
    ss -ltn 2>/dev/null | awk -v p=":$1" '$4 ~ p"$" {found=1} END {exit found ? 0 : 1}'
}

port_owner_pids() {
    ss -ltnp 2>/dev/null | awk -v p=":$1" '$4 ~ p"$"' | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u
}

# ---------------------------------------------------------------------------------------------------
# Process records
# ---------------------------------------------------------------------------------------------------

proc_start_time() {
    # Field 22 of /proc/<pid>/stat (clock ticks since boot). The command name may hold spaces, so cut
    # after the closing parenthesis.
    local stat
    stat="$(cat "/proc/$1/stat" 2>/dev/null)" || return 1
    stat="${stat##*) }"
    # shellcheck disable=SC2086
    set -- $stat
    echo "${20}"
}

proc_env_has() {
    # proc_env_has <pid> <KEY=VALUE>  (exact match on one environment entry)
    tr '\0' '\n' <"/proc/$1/environ" 2>/dev/null | grep -qxF -- "$2"
}

rec_file() {
    echo "$RUN_DIR/$1.pid"
}

rec_get() {
    # rec_get <name> <field>
    local f
    f="$(rec_file "$1")"
    [[ -f "$f" ]] || return 1
    sed -n "s/^$2=//p" "$f" | head -n 1
}

rec_write() {
    # rec_write <name> <pid> <kind> <identity KEY=VALUE>
    local name="$1" pid="$2" kind="$3" ident="$4" st
    st="$(proc_start_time "$pid")" || die "process $pid is gone before it could be recorded"
    {
        echo "pid=$pid"
        echo "start=$st"
        echo "kind=$kind"
        echo "identity=$ident"
        echo "recorded_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$(rec_file "$name")"
}

# rec_alive <name>: 0 when the record exists and the same process is still running.
rec_alive() {
    local pid st ident
    pid="$(rec_get "$1" pid)" || return 1
    st="$(rec_get "$1" start)" || return 1
    ident="$(rec_get "$1" identity)" || return 1
    [[ -n "$pid" && "$pid" -gt 1 ]] || return 1
    [[ -d "/proc/$pid" ]] || return 1
    [[ "$(proc_start_time "$pid")" == "$st" ]] || return 1
    proc_env_has "$pid" "$ident"
}

descendants() {
    # descendants <pid>: pids below <pid> by parent links, one per line (not including the root)
    ps -eo pid=,ppid= | awk -v root="$1" '
        {kids[$2] = kids[$2] " " $1}
        END {
            n = split(root, queue, " "); i = 1
            while (i <= n) {
                m = split(kids[queue[i]], k, " ")
                for (j = 1; j <= m; j++) { print k[j]; queue[++n] = k[j] }
                i++
            }
        }'
}

# rig_spawn <name> <kind> <identity KEY=VALUE> <logfile> -- <command...>
# Starts the command in its own session (setsid), in the background, and writes the pid record.
# The command is expected to have been prefixed with `env -i ...` by the caller so its environment is
# explicit. Prints the pid.
rig_spawn() {
    local name="$1" kind="$2" ident="$3" logfile="$4"
    shift 5
    if rec_alive "$name"; then
        die "$name already running (pid $(rec_get "$name" pid)); stop it first"
    fi
    rm -f "$(rec_file "$name")"
    (
        exec setsid "$@" >>"$logfile" 2>&1 </dev/null
    ) &
    local pid=$!
    sleep 0.3
    [[ -d "/proc/$pid" ]] || die "$name exited immediately; see $logfile"
    proc_env_has "$pid" "$ident" || {
        kill "$pid" 2>/dev/null
        die "$name started without the expected identity ($ident); killed pid $pid and aborted"
    }
    rec_write "$name" "$pid" "$kind" "$ident"
    echo "$pid"
}

# rig_stop <name>
# Stops the recorded process and what descends from it. Refuses when the record does not match.
rig_stop() {
    local name="$1" pid ident
    local f
    f="$(rec_file "$name")"
    if [[ ! -f "$f" ]]; then
        say "$name: no pid record, nothing to stop"
        return 0
    fi
    pid="$(rec_get "$name" pid)"
    ident="$(rec_get "$name" identity)"
    if ! rec_alive "$name"; then
        if [[ -n "$pid" && -d "/proc/$pid" ]]; then
            die "$name: pid $pid is alive but does not match the record (start time or identity); NOT killing it. Remove $f by hand if you are sure."
        fi
        say "$name: recorded pid $pid is not running; removing stale record"
        rm -f "$f"
        return 0
    fi
    local kids=() k
    while read -r k; do
        [[ -n "$k" ]] && kids+=("$k")
    done < <(descendants "$pid")
    # Descendants of a verified, recorded root are ours by lineage (a process cannot descend from the scratch
    # gateway and belong to anyone else); they are listed before they are killed.
    local victims=("$pid")
    for k in "${kids[@]}"; do
        victims+=("$k")
    done
    say "$name: stopping pid $pid and its ${#kids[@]} descendants: ${kids[*]}"
    local v
    for v in "${victims[@]}"; do
        [[ "$v" -gt 1 && "$v" != "$$" ]] && kill -TERM "$v" 2>/dev/null
    done
    local i
    for ((i = 0; i < 100; i++)); do
        local any=0
        for v in "${victims[@]}"; do
            [[ -d "/proc/$v" ]] && any=1
        done
        [[ $any -eq 0 ]] && break
        sleep 0.2
    done
    for v in "${victims[@]}"; do
        if [[ -d "/proc/$v" ]]; then
            say "$name: pid $v ignored TERM, sending KILL"
            kill -KILL "$v" 2>/dev/null
        fi
    done
    rm -f "$f"
}

# Print the owner's LONG-LIVED Kiro Crew and CAO processes, for before/after comparison: the roots of the owner's
# kirocrew process tree (a kirocrew process whose parent is not one) and every process listening on 5476 or 9889,
# each as "pid ppid start-ticks", then the listeners. The short-lived children the owner's gateway starts and
# ends by itself (its chat sessions) are left out, so the owner using Kiro Crew during a run is not a difference;
# a gateway or CAO that was stopped or restarted is (its pid or start time changes). Lines that belong to the
# scratch home are left out. Only reads the process table.
rig_owner_snapshot() {
    local all roots listen_pids p
    # shellcheck disable=SC2009  # pgrep -f would also match this script's own command line
    all="$(ps -eo pid=,ppid=,args= | grep -F kirocrew | grep -vF "$ISO_HOME" | grep -vF "grep -F kirocrew" | grep -vF "e2e/rig" \
        | grep -vE '^ *[0-9]+ +[0-9]+ +(/[a-z/]*/)?(bash|sh|zsh|grep|ugrep|rg|ps|awk|sed|timeout|env)( |$)' | awk '{print $1, $2}')"
    # a root has no kirocrew process among ALL its ancestors (the owner's chat sessions run under a
    # non-kirocrew agent process that is itself below the gateway)
    roots="$(awk 'NR==FNR {parent[$1]=$2; next} {seen[$1]=1; cand[++n]=$1}
        END {for (i=1; i<=n; i++) {p=parent[cand[i]]; root=1; hops=0
                 while (p > 1 && hops++ < 64) {if (p in seen) {root=0; break}; p=parent[p]}
                 if (root) print cand[i]}}' <(ps -eo pid=,ppid=) <(echo "$all"))"
    listen_pids="$(ss -ltnpH 2>/dev/null | awk -v a=":$OWNER_GATEWAY_PORT\$" -v b=":$OWNER_CAO_PORT\$" '$4 ~ a || $4 ~ b' \
        | grep -oE 'pid=[0-9]+' | cut -d= -f2)"
    for p in $(printf '%s\n%s\n' "$roots" "$listen_pids" | grep -E '^[0-9]+$' | sort -nu); do
        [[ -r "/proc/$p/stat" ]] || continue
        echo "$p $(awk '{print $4, $22}' "/proc/$p/stat")"
    done
    echo "--- listeners"
    ss -ltn 2>/dev/null | awk '{print $4}' | grep -E ":($OWNER_GATEWAY_PORT|$OWNER_CAO_PORT)$" | sort -u
}
