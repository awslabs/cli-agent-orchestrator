#!/usr/bin/env bash
# Scratch CAO (a real cao-server with the mock_cli provider) for the e2e rig.
#
#   cao.sh start [--port N]   start cao-server on 127.0.0.1:N (default 9990) with its own CAO_HOME_DIR,
#                             its own tmux server socket, mock_cli on PATH; records the pid
#   cao.sh status
#   cao.sh stop               stop by recorded pid (and the scratch tmux server by recorded pid)
#   cao.sh reset              stop, then delete the scratch CAO_HOME_DIR contents (only if marked as ours)
#   add --dry-run before the subcommand to print what would be done
#
# Never touches 9889 (the owner's live CAO) or any pid it did not record. The scratch tmux server lives in
# $SHELF_SCRATCH/work-c/tmux-tmp so it never shares a socket with the owner's tmux.

set -u
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DRY=0
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY=1
    shift
fi
cmd="${1:-}"
shift || true
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)
            CAO_PORT="${2:?--port needs a value}"
            shift 2
            ;;
        *) die "unknown option: $1" ;;
    esac
done

rig_check_scratch
rig_check_ports

REPO_ROOT="$(cd "$EXAMPLE_DIR/../.." && pwd)"
MOCK_BIN="$REPO_ROOT/test/providers/fixtures/bin"
TMUX_TMP="$WORK_DIR/tmux-tmp"
CAO_USER_HOME="$WORK_DIR/cao-user"
CAO_LOG="$LOG_DIR/cao-server.log"

act() {
    if [[ "$DRY" -eq 1 ]]; then echo "+ $*"; else "$@"; fi
}

cao_path() {
    local tmux_dir
    tmux_dir="$(dirname "$(command -v tmux)")"
    echo "$CAO_VENV/bin:$MOCK_BIN:$tmux_dir:/usr/local/bin:/usr/bin:/bin"
}

scratch_tmux() {
    env -i PATH="$(cao_path)" TMUX_TMPDIR="$TMUX_TMP" tmux "$@"
}

do_start() {
    [[ -x "$CAO_VENV/bin/cao-server" ]] || die "no cao-server in $CAO_VENV/bin (set SHELF_CAO_VENV, or: uv venv $CAO_VENV && uv pip install --python $CAO_VENV/bin/python -e $REPO_ROOT)"
    [[ -x "$MOCK_BIN/mock_cli" ]] || die "mock_cli not found in $MOCK_BIN"
    command -v tmux >/dev/null 2>&1 || die "tmux is not installed"
    if rec_alive cao-server; then
        say "cao-server already running, pid $(rec_get cao-server pid)"
        return 0
    fi
    if port_listening "$CAO_PORT"; then
        die "port $CAO_PORT is already in use by pid(s): $(port_owner_pids "$CAO_PORT" | tr '\n' ' ') (not ours, not touching it)"
    fi
    if [[ "$DRY" -eq 1 ]]; then
        echo "+ setsid env -i HOME=$CAO_USER_HOME CAO_HOME_DIR=$CAO_HOME CAO_API_HOST=127.0.0.1 CAO_API_PORT=$CAO_PORT TMUX_TMPDIR=$TMUX_TMP CAO_MOCK_CLI_SCRIPTED_PROMPTS=1 PATH=... cao-server --terminal tmux --host 127.0.0.1 --port $CAO_PORT"
        return 0
    fi
    mkdir -p "$TMUX_TMP" "$CAO_USER_HOME" "$WORK_DIR/cwd"
    chmod 700 "$TMUX_TMP"
    echo "scratch CAO home owned by the rig" >"$CAO_HOME/.shelf-rig-owned"
    : >"$CAO_LOG"
    local pid
    pid="$(rig_spawn cao-server cao-server "CAO_HOME_DIR=$CAO_HOME" "$CAO_LOG" -- \
        env -i HOME="$CAO_USER_HOME" CAO_HOME_DIR="$CAO_HOME" CAO_API_HOST=127.0.0.1 CAO_API_PORT="$CAO_PORT" \
        TMUX_TMPDIR="$TMUX_TMP" CAO_MOCK_CLI_SCRIPTED_PROMPTS=1 PATH="$(cao_path)" LANG=C.UTF-8 \
        "$CAO_VENV/bin/cao-server" --terminal tmux --host 127.0.0.1 --port "$CAO_PORT")" || exit 1
    say "cao-server started, pid $pid, log $CAO_LOG"
    local i
    for ((i = 0; i < 120; i++)); do
        if curl -fsS --max-time 2 "http://127.0.0.1:$CAO_PORT/health" >/dev/null 2>&1; then
            say "cao-server healthy on 127.0.0.1:$CAO_PORT"
            return 0
        fi
        rec_alive cao-server || {
            tail -n 20 "$CAO_LOG" >&2
            rm -f "$(rec_file cao-server)"
            die "cao-server exited during start-up"
        }
        sleep 0.5
    done
    die "cao-server did not answer /health within 60 s; still recorded (pid $pid); stop it with: cao.sh stop"
}

record_scratch_tmux() {
    # The scratch tmux server daemonizes, so it is not a child of cao-server. Ask it for its own pid through
    # the scratch socket directory and record it with an identity proving it was started for this rig.
    local tpid
    tpid="$(scratch_tmux display-message -p '#{pid}' 2>/dev/null || true)"
    [[ "$tpid" =~ ^[0-9]+$ ]] || return 0
    if proc_env_has "$tpid" "TMUX_TMPDIR=$TMUX_TMP"; then
        rec_write tmux-server "$tpid" tmux-server "TMUX_TMPDIR=$TMUX_TMP"
    fi
}

do_stop() {
    if [[ "$DRY" -eq 1 ]]; then
        echo "+ kill recorded pid of cao-server ($(rec_get cao-server pid 2>/dev/null || echo none)) and of the scratch tmux server"
        return 0
    fi
    record_scratch_tmux
    rig_stop cao-server
    rig_stop tmux-server
    local i
    for ((i = 0; i < 50; i++)); do
        port_listening "$CAO_PORT" || break
        sleep 0.2
    done
    if port_listening "$CAO_PORT"; then
        say "warning: port $CAO_PORT is still listening (pid(s) $(port_owner_pids "$CAO_PORT" | tr '\n' ' '))"
        return 1
    fi
    say "cao-server stopped; port $CAO_PORT is free"
}

do_status() {
    if rec_alive cao-server; then
        say "cao-server: running, pid $(rec_get cao-server pid), port $CAO_PORT, CAO_HOME_DIR $CAO_HOME"
    else
        say "cao-server: not running (no matching pid record)"
        return 1
    fi
}

do_reset() {
    do_stop
    [[ -f "$CAO_HOME/.shelf-rig-owned" || ! -e "$CAO_HOME/db" ]] || die "$CAO_HOME is not marked as owned by the rig; not deleting"
    [[ "$(basename "$CAO_HOME")" == "cao-home" ]] || die "refusing to delete a directory not named cao-home: $CAO_HOME"
    act find "$CAO_HOME" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    act mkdir -p "$CAO_HOME"
    [[ "$DRY" -eq 1 ]] || echo "scratch CAO home owned by the rig" >"$CAO_HOME/.shelf-rig-owned"
    say "scratch CAO home emptied"
}

case "$cmd" in
    start) do_start ;;
    stop) do_stop ;;
    status) do_status ;;
    reset) do_reset ;;
    *)
        sed -n '2,/^set -u/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//' >&2
        exit 2
        ;;
esac
