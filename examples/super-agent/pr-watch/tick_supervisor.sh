#!/bin/bash
# Keeps exactly one pr-watch/tick_loop.sh running, detached from any agent's shell, and turns each
# WAKE it prints into a CAO inbox message for the shepherd. Started by the conductor's Python launcher.
# Why: a loop run as an agent's background job dies with that job, or loses its wake when the turn ends.
#   SHEPHERD_ID  the shepherd's CAO terminal id (the wake goes there; it is also LOOP_OWNER in the log)
#   SENDER_ID    the conductor's CAO terminal id (the inbox sender)
#   CAO_API      optional; default: cao_api from workplace.json / workplace.local.json
# A WAKE is never dropped. Write-ahead: the WAKE line goes into state/tick_supervisor.pending (fsync'd)
# before the first delivery attempt; the file is then flushed oldest line first, each line removed only
# after a 2xx, with a bounded doubling backoff (DELIVER_TRIES, default 4; DELIVER_BACKOFF s, default 2).
# No new loop starts while a line is pending. TERM and INT are taken between steps, so a stop or a kill
# can only repeat a WAKE, never lose it. A pending path that isn't a usable regular file stops the
# supervisor (exit 4) and is left as it is; blank lines in it are skipped, never posted.
# Stops for good on not_writer (the lease moved): the notice goes to the shepherd, or waits in the
# pending file, and one NOT_WRITER line goes to the log and to state/tick_supervisor.stopped.
# Five consecutive error exits stop the supervisor with one LOOP_ERROR notice. Error pauses double
# from SUPERVISOR_SLEEP (default 5 seconds) up to INTERVAL, and a clean exit resets the pause.
# Never prints env values.
set -u
PB=$(cd -- "$(dirname -- "$0")" && pwd) || exit 2
API="${CAO_API:-$(python3 -I "$PB/../bin/workplace.py" get cao_api)}" \
  || { echo "STOP: the workplace config is unreadable (run: python3 bin/workplace.py show)"; exit 2; }
SHEPHERD="${SHEPHERD_ID:?SHEPHERD_ID is required}"
SENDER="${SENDER_ID:?SENDER_ID is required}"
LOG="$PB/state/tick_supervisor.log"
PIDF="$PB/state/tick_supervisor.pid"
PENDING="$PB/state/tick_supervisor.pending"
STOPPED="$PB/state/tick_supervisor.stopped"
TRIES="${DELIVER_TRIES:-4}"
BACKOFF="${DELIVER_BACKOFF:-2}"
CYCLES="${SUPERVISOR_CYCLES:-0}"  # tests only: stop after this many cycles (0 = run forever)

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$LOG"; }

if [ -f "$PIDF" ]; then
  EXISTING=$(cat "$PIDF")
  if [ "$EXISTING" != "$$" ] && kill -0 "$EXISTING" 2>/dev/null \
     && ps -o args= -p "$EXISTING" | grep -q 'tick_supervisor\.sh'; then
    echo "STOP: a supervisor already runs (pid $EXISTING)"; exit 1
  fi
fi
echo $$ > "$PIDF"
STOP_REQ=0
trap 'STOP_REQ=1' TERM INT  # acted on between steps: a WAKE already kept stays kept
stop_if_asked() { if [ "$STOP_REQ" = 1 ]; then rm -f -- "$PIDF"; log "supervisor exit"; exit 0; fi; }
pause() { sleep "$1" & wait $! 2>/dev/null; }  # a TERM ends the wait at once

deliver_to() {
  python3 - "$API" "$1" "$SENDER" "$2" <<'PY' >> "$LOG" 2>&1
import sys, urllib.parse, urllib.request
api, to, sender, msg = sys.argv[1:5]
q = urllib.parse.urlencode({"sender_id": sender, "message": msg})
req = urllib.request.Request(f"{api}/terminals/{to}/inbox/messages?{q}", method="POST")
with urllib.request.urlopen(req, timeout=15) as r:
    print("delivered", r.status)
    sys.exit(0 if 200 <= r.status < 300 else 1)  # urlopen already raises on any other final status
PY
}

deliver() { deliver_to "$SHEPHERD" "$1"; }

pend() {  # the pending file: check | append MSG | first (3: none) | drop. Exit 4 = the path is unusable.
  python3 -I - "$PENDING" "$@" <<'PY'
import os, stat, sys
path, op = sys.argv[1], sys.argv[2]
def unusable(why):
    print(f"STOP: pending file unusable: {why}", file=sys.stderr)
    sys.exit(4)
def check():
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(st.st_mode):
        unusable("not a regular file")
    if not os.access(path, os.R_OK | os.W_OK):
        unusable("not readable and writable")
    return True
def lines():
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        unusable(f"unreadable ({type(exc).__name__})")
def write(rows):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("".join(r + "\n" for r in rows))
        fh.flush(); os.fsync(fh.fileno())
    os.replace(tmp, path)
exists = check()
if op == "check":
    sys.exit(0)
if op == "append":
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(sys.argv[3].replace("\n", " ") + "\n")
        fh.flush(); os.fsync(fh.fileno())
    sys.exit(0)
rows = [r for r in lines() if r.strip()] if exists else []  # blank lines are skipped, never posted
if op == "first":
    if not rows:
        sys.exit(3)  # nothing pending
    print(rows[0])
    sys.exit(0)
if op == "drop":  # remove the oldest line, after its 2xx
    if rows[1:]:
        write(rows[1:])
    else:
        os.unlink(path)
    sys.exit(0)
sys.exit(2)
PY
}

unusable_stop() {  # an unusable pending path: one clear line, exit 4, the file left as it is
  log "STOP: pending file unusable ($PENDING); left as it is; supervisor exits 4"
  echo "STOP: pending file unusable ($PENDING); see $LOG"
  rm -f -- "$PIDF"; exit 4
}

pend_or_stop() {  # run pend in this shell; exit 4 from it stops the supervisor
  local rc
  pend "$@" 2>>"$LOG"; rc=$?
  [ "$rc" -eq 4 ] && unusable_stop
  return "$rc"
}

deliver_retry() {  # $1 = message; up to TRIES attempts with a doubling backoff; 0 only after a 2xx
  local n=1 wait="$BACKOFF"
  while true; do
    deliver "$1" && return 0
    log "deliver failed (attempt $n of $TRIES)"
    [ "$n" -ge "$TRIES" ] && return 1
    [ "$STOP_REQ" = 1 ] && return 1
    pause "$wait"; n=$((n + 1)); wait=$((wait * 2))
    [ "$STOP_REQ" = 1 ] && return 1
  done
}

flush_pending() {  # 0 when nothing is left pending; oldest line first, each removed only after its 2xx
  local first rc
  while true; do
    first=$(pend first 2>>"$LOG"); rc=$?  # a subshell: its exit 4 is handled here
    [ "$rc" -eq 3 ] && return 0  # nothing left
    [ "$rc" -ne 0 ] && unusable_stop  # 4, or any other failure to read it
    deliver_retry "$first" || return 1
    pend_or_stop drop
    log "delivered a kept WAKE"
  done
  return 0
}

log "supervisor start pid $$ shepherd=$SHEPHERD"
CYCLE=0
ERRORS=0
ERROR_PAUSE="${SUPERVISOR_SLEEP:-5}"
while true; do
  CYCLE=$((CYCLE + 1))
  if [ "$CYCLES" -gt 0 ] && [ "$CYCLE" -gt "$CYCLES" ]; then rm -f -- "$PIDF"; log "supervisor exit (cycles)"; exit 0; fi
  pend_or_stop check
  if ! flush_pending; then
    stop_if_asked
    log "pending WAKE still undelivered; no new loop yet"; pause "${SUPERVISOR_ERROR_SLEEP:-60}"; stop_if_asked; continue
  fi
  stop_if_asked
  OUT=$(CAO_TERMINAL_ID="$SHEPHERD" bash "$PB/tick_loop.sh" 2>>"$LOG"); RC=$?
  LINE=$(printf '%s\n' "$OUT" | grep -m1 '^WAKE ' || true)
  log "loop exit rc=$RC ${LINE:-no WAKE line}"
  if [ "$RC" -ne 0 ]; then
    ERRORS=$((ERRORS + 1))
    REASON="${LINE##* :: }"
    [ -n "$LINE" ] || REASON="exit_status_$RC"
  else
    ERRORS=0
    ERROR_PAUSE="${SUPERVISOR_SLEEP:-5}"
  fi
  if [ -n "$LINE" ]; then
    if [ "$ERRORS" -ge 5 ]; then
      MSG="$LINE  [from tick_supervisor: the supervisor stopped; report BLOCKED to the conductor; do NOT start a loop yourself]"
    else
      MSG="$LINE  [from tick_supervisor: the loop runs under the conductor's supervisor; handle this wake per shepherd-playbook §2 but do NOT start a loop yourself]"
    fi
    pend_or_stop append "$MSG" && log "kept WAKE in $PENDING before delivery"  # write-ahead
    flush_pending || log "deliver failed; the WAKE stays kept for the next cycle"
  fi
  case "$LINE" in *not_writer*)
    if pend check 2>/dev/null && [ -f "$PENDING" ]; then WHERE="the notice waits in $PENDING"; else WHERE="the shepherd was told"; fi
    NOTE="NOT_WRITER $(date -u +%Y-%m-%dT%H:%M:%SZ): the writer lease moved; supervisor stopped; $WHERE"
    log "$NOTE"; printf '%s\n' "$NOTE" > "$STOPPED"; echo "$NOTE"
    rm -f -- "$PIDF"; exit 3;;
  esac
  if [ "$ERRORS" -ge 5 ]; then
    if pend check 2>/dev/null && [ -f "$PENDING" ]; then WHERE="the error wake waits in $PENDING"; else WHERE="the shepherd was told"; fi
    NOTE="LOOP_ERROR $(date -u +%Y-%m-%dT%H:%M:%SZ): $REASON; supervisor stopped; $WHERE"
    log "$NOTE"; printf '%s\n' "$NOTE" > "$STOPPED"; echo "$NOTE"
    deliver_to "$SENDER" "$NOTE" || log "conductor stop notice could not be delivered"
    rm -f -- "$PIDF"; exit 2
  fi
  stop_if_asked
  if [ "$RC" -ne 0 ]; then
    pause "$ERROR_PAUSE"
    ERROR_PAUSE=$((ERROR_PAUSE * 2))
    [ "$ERROR_PAUSE" -le "${INTERVAL:-1800}" ] || ERROR_PAUSE="${INTERVAL:-1800}"
  else
    pause "${SUPERVISOR_SLEEP:-5}"
  fi
  stop_if_asked
done
