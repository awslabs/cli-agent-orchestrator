#!/bin/bash
# Zero-token read-only PR watcher. Deploy only with the previous loop stopped.
set -u
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd) || exit 2
PYTHON="${PYTHON:-python3}"
INTERVAL="${INTERVAL:-1800}"
RETRY_DELAY="${RETRY_DELAY:-30}"
LOOP_OWNER="${CAO_TERMINAL_ID:-unknown}"
ROOT=$("$PYTHON" "$SCRIPT_DIR/config.py") || { echo 'WAKE none :: configuration_error'; exit 2; }
cd -- "$ROOT" || { echo 'WAKE none :: watch_dir_error'; exit 2; }
writer_check() {
  "$PYTHON" "$SCRIPT_DIR/config.py" --check-writer || { echo 'WAKE none :: not_writer'; exit 2; }
}
writer_check
mkdir -p reports || exit 2
LOCK=state/tick_loop.lock
"$PYTHON" "$SCRIPT_DIR/config.py" --loop-lock "$$" || exit $?
trap '"$PYTHON" "$SCRIPT_DIR/config.py" --loop-unlock "$$" >/dev/null 2>&1' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
pause() { sleep "$1" >/dev/null 2>&1 & wait $!; }

ts() { date -u +%Y-%m-%dT%H%M%SZ; }
error_count() {
  "$PYTHON" -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("errors",[])))' "$1" 2>/dev/null || echo parse
}
filter() {
  local RESULT
  RESULT=$("$PYTHON" "$SCRIPT_DIR/digest_store.py" "$1") || { echo "WAKE $1 partial=${R1:-} retry=${R2:-} :: filter_error"; exit 2; }
  IFS=$'\t' read -r MODE FILTER_REPORT REASON <<< "$RESULT"
  local POLICY
  POLICY=$("$PYTHON" "$SCRIPT_DIR/wake_rules.py" "$FILTER_REPORT") || { echo "WAKE $FILTER_REPORT :: rule_error"; exit 2; }
  MODE="${POLICY%% *}"
  REASON="${POLICY#WAKE }"
}
emit_wake() {
  echo "WAKE $FILTER_REPORT ${1:-} :: $REASON"
  # Retire only after emitting the handover; a crash earlier preserves the queue and the wake
  # journal, so the next start replays this WAKE (at most one repeat).
  "$PYTHON" "$SCRIPT_DIR/digest_store.py" --delivered "$FILTER_REPORT" "${1:+${1#*=}}" || exit 2
  case "$REASON" in poll_errors=*) exit 2;; esac
  exit 0
}
replay() {
  # An undelivered WAKE from an interrupted run is emitted before any new poll (F11).
  local RESULT
  RESULT=$("$PYTHON" "$SCRIPT_DIR/digest_store.py" --replay) || { echo "WAKE none :: replay_error"; exit 2; }
  IFS=$'\t' read -r MODE FILTER_REPORT REASON <<< "$RESULT"
  if [ "$MODE" = WAKE ]; then emit_wake replay; fi
}
while true; do
  replay
  writer_check
  T=$(ts); R1="reports/tick-$T.json"
  "$PYTHON" "$SCRIPT_DIR/poll.py" > "$R1" 2>"reports/tick-$T.stderr"
  ERR=$(error_count "$R1")
  if [ "$ERR" != 0 ]; then
    pause "$RETRY_DELAY"
    writer_check
    R2="reports/tick-$T-retry.json"
    "$PYTHON" "$SCRIPT_DIR/poll.py" > "$R2" 2>>"reports/tick-$T.stderr"
    ERR2=$(error_count "$R2")
    # Ingest BOTH partial reports before delivering, so neither side loses events.
    filter "$R1"; M1=$MODE; F1=$FILTER_REPORT; WHY1=$REASON
    filter "$R2"; M2=$MODE; F2=$FILTER_REPORT; WHY2=$REASON
    if [ "$M1" = WAKE ]; then
      # Refresh R1's handover to include any digest accumulated by the retry.
      filter "$R1"; REASON=$WHY1; emit_wake "retry=$F2"
    fi
    if [ "$M2" = WAKE ]; then emit_wake "partial=$F1"; fi
    if [ "$ERR2" != 0 ]; then
      writer_check
      echo "- $(date -u '+%Y-%m-%d %H:%M') UTC tick (loop $LOOP_OWNER): poll errors twice ($ERR/$ERR2), see $R1 $R2" | "$PYTHON" "$SCRIPT_DIR/config.py" --append-log || exit 2
      REASON="poll_errors=$ERR/$ERR2"; emit_wake "partial=$F1"
    fi
  else
    filter "$R1"
    if [ "$MODE" = WAKE ]; then emit_wake; fi
  fi
  writer_check
  echo "- $(date -u '+%Y-%m-%d %H:%M') UTC tick (loop $LOOP_OWNER): $MODE ($FILTER_REPORT)" | "$PYTHON" "$SCRIPT_DIR/config.py" --append-log || exit 2
  touch state/tick_loop.clean || exit 2
  pause "$INTERVAL"
done
