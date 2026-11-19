#!/usr/bin/env bash
# One-command dev harness: an isolated Kiro Crew (gateway on 127.0.0.1:5499) plus a scratch real cao-server
# (127.0.0.1:9990) with mock_cli sessions, with the CAO Shelf installed. Safe to run twice in a row.
#
#   harness.sh up [--day busy|quiet] [--no-seed] [--no-app]   start everything, seed, install the App
#   harness.sh status                                          what is running (by recorded pid)
#   harness.sh down [--purge-data]                             uninstall the App from the scratch home, stop both
#   harness.sh owner-check                                     compare the owner's kirocrew pids/ports with `up`
#
# Environment: SHELF_SCRATCH (default $HOME/kc-shelf-e2e; any other scratch folder works),
#              SHELF_KC_PORT (5499), SHELF_CAO_PORT (9990), SHELF_TRUST=all|app (default all, scratch only),
#              SHELF_MODEL_PORT (9981, the scripted model), SHELF_MODEL_SCRIPT (its JSON script),
#              SHELF_CLAUDE_BACKEND=0 (keep the gateway's default chat backend, no mock model).
# Nothing here calls `kirocrew stop|restart|service`; stopping is by recorded pid only (see lib.sh).

set -u
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
rig_check_scratch
rig_check_ports

HERE="$RIG_LIB_DIR"
BEFORE="$WORK_DIR/owner-before.txt"
DAY=busy
SEED=1
APP=1

cmd="${1:-}"
shift || true
PURGE=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --day)
            DAY="${2:?}"
            shift
            ;;
        --no-seed) SEED=0 ;;
        --no-app) APP=0 ;;
        --purge-data) PURGE=(--purge-data) ;;
        *) die "unknown option: $1" ;;
    esac
    shift
done

owner_check() {
    local now
    now="$(mktemp "$WORK_DIR/owner-now.XXXXXX")"
    rig_owner_snapshot >"$now"
    if diff -u "$BEFORE" "$now" >/dev/null; then
        say "owner check: kirocrew pids and ports 5476/9889 are exactly as before ($(sed -n '/^---/q;p' "$BEFORE" | wc -l) pids)"
        rm -f "$now"
        return 0
    fi
    say "owner check: DIFFERENCE from the snapshot taken at 'up':"
    diff -u "$BEFORE" "$now"
    rm -f "$now"
    return 1
}

case "$cmd" in
    up)
        rig_owner_snapshot >"$BEFORE"
        say "owner snapshot saved to $BEFORE ($(sed -n '/^---/q;p' "$BEFORE" | wc -l) kirocrew pids)"
        "$HERE/kiro_crew.sh" setup >/dev/null || die "kiro_crew.sh setup failed"
        "$HERE/kiro_crew.sh" start || die "gateway did not start"
        "$HERE/cao.sh" start || die "scratch CAO did not start"
        if [[ "$SEED" -eq 1 ]]; then
            python3 "$HERE/seed_cao.py" seed --day "$DAY" --port "$CAO_PORT" || die "seeding failed"
        fi
        if [[ "$APP" -eq 1 ]]; then
            if [[ "${SHELF_TRUST:-all}" == "app" ]]; then
                "$HERE/kiro_crew.sh" install-app --trust-app || die "install failed"
            else
                "$HERE/kiro_crew.sh" install-app --trust-all || die "install failed"
            fi
        fi
        say "up: gateway http://127.0.0.1:$KC_PORT  cao http://127.0.0.1:$CAO_PORT  (get a login URL: kiro_crew.sh token)"
        ;;
    status)
        "$HERE/kiro_crew.sh" status
        "$HERE/cao.sh" status
        ;;
    down)
        rc=0
        if rec_alive gateway; then
            "$HERE/kiro_crew.sh" teardown "${PURGE[@]}" || rc=1
        else
            "$HERE/kiro_crew.sh" stop || rc=1
        fi
        "$HERE/cao.sh" stop || rc=1
        if [[ -f "$BEFORE" ]]; then owner_check || rc=1; fi
        for pf in gateway mock-model cao-server tmux-server; do
            [[ -f "$(rec_file "$pf")" ]] && {
                say "pid record $pf still present"
                rc=1
            }
        done
        port_listening "$KC_PORT" && {
            say "port $KC_PORT still listening"
            rc=1
        }
        port_listening "$CAO_PORT" && {
            say "port $CAO_PORT still listening"
            rc=1
        }
        port_listening "$MODEL_PORT" && {
            say "port $MODEL_PORT still listening"
            rc=1
        }
        exit "$rc"
        ;;
    owner-check)
        [[ -f "$BEFORE" ]] || die "no snapshot; run 'up' first"
        owner_check
        ;;
    *)
        sed -n '2,/^set -u/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//' >&2
        exit 2
        ;;
esac
