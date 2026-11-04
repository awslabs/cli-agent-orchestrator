#!/usr/bin/env bash
# Local-only end-to-end check of the CAO Dojo page against a real cao-server
# and a real Claude Code agent: approve, then a fresh agent and reject, then a
# run with nothing pending. Exits 0 only if every assertion passes. Not wired
# into CI; it needs cao, a logged-in `claude`, and Playwright's Chromium.
#
#   examples/ag-ui/cao-dojo/tools/e2e-local.sh
#
# It is isolated by default: it starts its own cao-server, refuses a port
# that is already in use, and runs with a private CAO home (profile store,
# database) and a private tmux server, both removed on exit. Your own CAO home
# and your default tmux server are never touched.
#
# Environment:
#   CAO_HOME_DIR    default: a fresh temp dir, removed on exit
#   TMUX_TMPDIR     default: a fresh temp dir, whose tmux server is stopped and
#                   removed on exit. If you set it, only the demo session is shut
#                   down; that tmux server is left running.
#   CAO_PORT        default 9890 (the server this script starts)
#   PAGE_PORT       default 8790 (the static server for ../index.html)
#   E2E_VIDEO_DIR   opt-in: record each phase's page video into this directory
#   E2E_CAPTION     opt-in: caption drawn on the page in those videos
#   E2E_SHOT_DIR    opt-in: save a screenshot of each step into this directory
#   E2E_HOLD_MS     opt-in: pause on the card and the result so videos are watchable
#
# Playwright and its Chromium must be installed once:
#   (cd examples/ag-ui/cao-dojo/tools && npm install && npx playwright install chromium)
#
# Written for bash 3.2, which is what macOS ships.
set -euo pipefail

# A fleet pane's $TMUX outranks TMUX_TMPDIR; never let the demo reach that server.
unset TMUX TMUX_PANE CAO_TERMINAL_ID

TOOLS_DIR="$(cd "$(dirname "$0")" && pwd)"
PAGE_DIR="$(cd "$TOOLS_DIR/.." && pwd)"
DRIVER="$TOOLS_DIR/e2e-local.mjs"
PROFILE_FILE="$PAGE_DIR/dojo-approver.md"
CAO_PORT="${CAO_PORT:-9890}"
PAGE_PORT="${PAGE_PORT:-8790}"
CAO_URL="http://127.0.0.1:$CAO_PORT"
PAGE_ORIGIN="http://127.0.0.1:$PAGE_PORT"
SESSION="dojo-demo" # cao prefixes it, so tmux shows "cao-dojo-demo"
TMP_ROOT="${TMPDIR:-/tmp}"
TMP_ROOT="${TMP_ROOT%/}"
LOG_DIR="$(mktemp -d "$TMP_ROOT/cao-dojo-e2e.XXXXXX")"
OWN_HOME=""
OWN_TMUX=""
if [ -z "${CAO_HOME_DIR:-}" ]; then
  CAO_HOME_DIR="$(mktemp -d "$TMP_ROOT/cao-dojo-home.XXXXXX")"
  OWN_HOME=1
fi
if [ -z "${TMUX_TMPDIR:-}" ]; then
  TMUX_TMPDIR="$(mktemp -d "$TMP_ROOT/cao-dojo-tmux.XXXXXX")"
  OWN_TMUX=1
fi
export CAO_HOME_DIR TMUX_TMPDIR

export CAO_API_HOST=127.0.0.1 CAO_API_PORT="$CAO_PORT"

die() { echo "e2e-local: $*" >&2; exit 1; }

for cmd in cao cao-server claude node python3 curl tmux; do
  command -v "$cmd" >/dev/null 2>&1 || die "'$cmd' is not on PATH"
done
PW_INSTALL="(cd examples/ag-ui/cao-dojo/tools && npm install && npx playwright install chromium)"
(cd "$TOOLS_DIR" && node -e '
  const { chromium } = require("@playwright/test");
  process.exit(require("fs").existsSync(chromium.executablePath()) ? 0 : 1);
') >/dev/null 2>&1 || die "Playwright or its Chromium is not installed: $PW_INSTALL"
[ "$CAO_PORT" != 9889 ] || die "refusing CAO_PORT=9889: that is the default fleet port; pick another"
for port in "$CAO_PORT" "$PAGE_PORT"; do
  if curl -s -o /dev/null -m 2 "http://127.0.0.1:$port/"; then
    die "something is already serving port $port; stop it or pick another port"
  fi
done

SERVER_PID=""
PAGE_PID=""
WORK_ROOT=""
# shellcheck disable=SC2329 # invoked by the EXIT trap
cleanup() {
  status=$?
  if [ -n "$SERVER_PID" ]; then
    cao shutdown --session "cao-$SESSION" >/dev/null 2>&1 || true
    stop_pid "$SERVER_PID" "cao-server"
    if curl -s -o /dev/null -m 2 "$CAO_URL/health"; then
      echo "e2e-local: warning: port $CAO_PORT still answers after stopping cao-server" >&2
    fi
  fi
  if [ -n "$PAGE_PID" ]; then stop_pid "$PAGE_PID" "the static server"; fi
  if [ -n "$OWN_TMUX" ]; then stop_private_tmux; fi
  remove_work_root
  if [ -n "$OWN_HOME" ]; then rm -rf "$CAO_HOME_DIR"; fi
  if [ "$status" -eq 0 ]; then rm -rf "$LOG_DIR"; else echo "e2e-local: logs kept in $LOG_DIR" >&2; fi
}
# Signal a pid this script started and wait for it to exit; after a 30 s
# grace period, SIGKILL that same pid. Never by name or pattern.
# shellcheck disable=SC2329 # called from cleanup, which the EXIT trap runs
stop_pid() {
  kill "$1" 2>/dev/null || return 0
  i=0
  while kill -0 "$1" 2>/dev/null && [ "$i" -lt 30 ]; do
    i=$((i + 1))
    sleep 1
  done
  kill -0 "$1" 2>/dev/null || return 0
  echo "e2e-local: warning: $2 (pid $1) did not exit after 30s; sending SIGKILL" >&2
  kill -9 "$1" 2>/dev/null || true
  sleep 1
  if kill -0 "$1" 2>/dev/null; then echo "e2e-local: warning: $2 (pid $1) still running" >&2; fi
}
# cao-server starts the private tmux server with exit-empty off, so it outlives
# its last session. Stop it by pid, asked of that private socket only.
# shellcheck disable=SC2329 # called from cleanup, which the EXIT trap runs
stop_private_tmux() {
  tmux kill-session -t "cao-$SESSION" 2>/dev/null || true
  # Remove the socket dir only once the server is stopped; removing it under a
  # live server would leave that server running and unreachable.
  if [ -n "$(tmux ls 2>/dev/null)" ]; then
    echo "e2e-local: warning: private tmux server still has sessions; left running in $TMUX_TMPDIR" >&2
    return 0
  fi
  tpid="$(tmux display-message -p '#{pid}' 2>/dev/null || true)"
  if [ -z "$tpid" ]; then
    echo "e2e-local: warning: could not read the private tmux server's pid; left $TMUX_TMPDIR in place" >&2
    return 0
  fi
  stop_pid "$tpid" "the private tmux server"
  rm -rf "$TMUX_TMPDIR"
}
# A coding CLI's exit hooks can still write into its work dir after its session
# is shut down, recreating it. Wait until nothing holds the agents' work dirs,
# then remove them, and again if they reappear.
# shellcheck disable=SC2329 # called from cleanup, which the EXIT trap runs
remove_work_root() {
  case "$WORK_ROOT" in
    "$TMP_ROOT"/cao-dojo-work.*) ;;
    *) return 0 ;;
  esac
  i=0
  while [ "$i" -lt 30 ] && command -v lsof >/dev/null 2>&1 &&
    [ -n "$(lsof -t +D "$WORK_ROOT" 2>/dev/null || true)" ]; do
    i=$((i + 1))
    sleep 1
  done
  for _ in 1 2 3; do
    if [ -d "$WORK_ROOT" ] && [ ! -L "$WORK_ROOT" ]; then rm -rf "$WORK_ROOT"; fi
    sleep 2
    [ -e "$WORK_ROOT" ] || return 0
  done
  echo "e2e-local: warning: $WORK_ROOT keeps reappearing; left in place" >&2
}
trap cleanup EXIT
trap 'exit 130' INT TERM

echo "== serving $PAGE_DIR on $PAGE_ORIGIN"
python3 -m http.server "$PAGE_PORT" --bind 127.0.0.1 --directory "$PAGE_DIR" >"$LOG_DIR/page.log" 2>&1 &
PAGE_PID=$!

echo "== starting cao-server on $CAO_URL (log: $LOG_DIR/server.log)"
CAO_AGUI_ENABLED=1 CAO_CORS_ORIGINS="$PAGE_ORIGIN" \
  cao-server --host 127.0.0.1 --port "$CAO_PORT" >"$LOG_DIR/server.log" 2>&1 &
SERVER_PID=$!
i=0
until curl -fsS -m 2 "$CAO_URL/health" >/dev/null 2>&1; do
  i=$((i + 1))
  [ "$i" -le 60 ] || die "cao-server did not answer $CAO_URL/health within 60s"
  kill -0 "$SERVER_PID" 2>/dev/null || die "cao-server exited; see $LOG_DIR/server.log"
  sleep 1
done
i=0
until curl -fsS -m 2 -o /dev/null "$PAGE_ORIGIN/index.html"; do
  i=$((i + 1))
  [ "$i" -le 20 ] || die "the static server did not serve index.html; see $LOG_DIR/page.log"
  sleep 0.5
done

# Each probe is its own run, with a fresh runId.
run_probe() {
  probe="$(printf '{"threadId":"e2e-local","runId":"probe-%s-%s","state":{},"messages":[],"tools":[],"context":[],"forwardedProps":{}}' "$$" "$(date +%s)$RANDOM")"
  curl -s -m 5 -X POST "$CAO_URL/agui/v1/run" -H 'Content-Type: application/json' \
    -H 'Accept: text/event-stream' -d "$probe" || true
}
# The first open interrupt's id from a probe's SSE body, read as JSON so key
# order does not matter.
first_interrupt_id() {
  python3 -c '
import json, sys
for line in sys.stdin:
    if not line.startswith("data:"):
        continue
    try:
        frame = json.loads(line[5:])
    except ValueError:
        continue
    if not isinstance(frame, dict) or frame.get("type") != "RUN_FINISHED":
        continue
    outcome = frame.get("outcome") or {}
    if outcome.get("type") == "interrupt":
        for intr in outcome.get("interrupts") or []:
            if isinstance(intr, dict) and intr.get("id"):
                print(intr["id"])
                sys.exit(0)
'
}
case "$(run_probe)" in
  *'"type":"RUN_FINISHED"'*) ;;
  *) die "POST $CAO_URL/agui/v1/run did not finish; is this CAO with the run-plane fix (#1107) and the [agui] extra?" ;;
esac
cao install "$PROFILE_FILE" >/dev/null
WORK_ROOT="$(mktemp -d "$TMP_ROOT/cao-dojo-work.XXXXXX")"

# Launch a fresh agent in a throwaway work dir and wait until CAO reports its
# permission prompt as an open interrupt on the run plane.
fresh_agent() {
  cao shutdown --session "cao-$SESSION" >/dev/null 2>&1 || true
  E2E_WORKDIR="$(mktemp -d "$WORK_ROOT/agent.XXXXXX")"
  export E2E_WORKDIR
  cao launch --agents dojo-approver --session-name "$SESSION" --working-directory "$E2E_WORKDIR" \
    --headless --auto-approve --async "Do your task now." >"$LOG_DIR/launch.log" 2>&1 ||
    { cat "$LOG_DIR/launch.log" >&2; die "cao launch failed"; }
  i=0
  # The driver waits for this interrupt's card to arrive over the stream.
  E2E_INTERRUPT_ID=""
  until E2E_INTERRUPT_ID="$(run_probe | first_interrupt_id)" && [ -n "$E2E_INTERRUPT_ID" ]; do
    i=$((i + 1))
    [ "$i" -le 120 ] || die "the agent never raised a permission interrupt within 240s"
    sleep 2
  done
  export E2E_INTERRUPT_ID
}

result=0
phase() {
  echo "== $1"
  if ! PAGE_URL="$PAGE_ORIGIN/index.html" CAO_URL="$CAO_URL" node "$DRIVER" "$1"; then result=1; fi
}

phase page
fresh_agent
phase approve
fresh_agent
phase reject
cao shutdown --session "cao-$SESSION" >/dev/null 2>&1 || true
phase none

if [ "$result" -eq 0 ]; then echo "e2e-local: PASS"; else echo "e2e-local: FAIL"; fi
exit "$result"
