#!/usr/bin/env bash
# Record the robotics replay trace against a private cao-server. Never touches port 9889,
# a running fleet or any real seat. Usage: run.sh <out-dir> [port]
#
# Needs: tmux, uv, and a checkout synced with `uv sync` (the recorder refuses a CAO that is
# not imported from this checkout). Pulls mujoco and pyyaml for the run only (`uv run --with`).
# Use a throwaway HOME if you do not want uv or mise to look at yours.
#
# The cleanup trap runs when the recorder (the foreground child) exits. A SIGTERM sent to
# run.sh alone therefore waits for `uv run` to finish, at most until the 420 s alarm below;
# to stop a run now, signal the recorder's python process, and the trap then cleans up.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="${ROBOTICS_REPLAY_CHECKOUT:-$(cd "$HERE/../../../.." && pwd)}"
OUT="${1:?out dir}"; PORT="${2:-19931}"
TX="$(mktemp -d /tmp/robotics-replay.XXXX)"
cleanup() {
  # The recorder's server keeps its tmux socket at
  # $TMPDIR/cao-dojo-trace.*/tmux/tmux-$UID/default and removes that directory itself after
  # a successful run. After a failed or killed run it is left behind: stop only that private
  # tmux server, by its socket, while the socket still exists, and the private cao-server,
  # by the pid in its own lock file after the checks below, then remove the scratch dir.
  # A run killed while its cao-server is still starting leaves a server that has not yet
  # written its lock. Give each server directory time to get one, so that server is
  # found and stopped rather than left running (and recreating files under $TX). The wait is
  # up to 60 s: on a loaded machine cao-server can take tens of seconds to import and lock.
  for home in "$TX"/cao-dojo-trace.*/cao; do
    [ -d "$home" ] || continue
    for _ in $(seq 1 120); do [ -f "$home/server.lock" ] && break; sleep 0.5; done
  done
  for lock in "$TX"/cao-dojo-trace.*/cao/server.lock; do
    [ -f "$lock" ] || continue
    # Prints a pid only when all three hold: the lock names an integer pid above 1 (so never
    # 0, which would signal this script's own process group); the lock is still HELD (the
    # server keeps an exclusive flock on it for its lifetime and never unlinks the file, so a
    # lock we can take means the server is gone and the pid may belong to something else);
    # and that pid's command line is a cao-server.
    # The helper prints "kill <pid>", "skip <reason>" or "unverified <reason>".
    verdict="unverified python3 is not on PATH"
    if command -v python3 >/dev/null 2>&1; then
      verdict="$(python3 - "$lock" <<'PY' 2>&1 || true
import fcntl, json, subprocess, sys
path = sys.argv[1]
try:
    pid = json.load(open(path)).get("pid")
except (OSError, ValueError) as e:
    sys.exit(print(f"unverified the lock file is unreadable ({e})"))
if type(pid) is not int or pid <= 1:
    sys.exit(print(f"skip the lock names no usable pid ({pid!r})"))
with open(path) as f:
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        pass  # held: the server is alive
    else:
        sys.exit(print("skip the lock is not held, so the server is gone"))
try:
    cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
except OSError as e:
    sys.exit(print(f"unverified ps cannot run ({e})"))
if "cao-server" not in cmd.stdout:
    sys.exit(print(f"skip pid {pid} is not a cao-server"))
print(f"kill {pid}")
PY
)"
    fi
    where="${lock#"$TX"/}"
    case "$verdict" in
      "kill "*)
        pid="${verdict#kill }"
        if kill "$pid" 2>/dev/null; then
          echo "run.sh: stopped the private cao-server (pid $pid)" >&2
        else
          echo "run.sh: could not signal the private cao-server (pid $pid) behind $where; stop it yourself" >&2
        fi ;;
      "skip "*)
        echo "run.sh: no live private cao-server behind $where (${verdict#skip }); nothing signalled" >&2 ;;
      *)
        echo "run.sh: could not verify the cao-server behind $where (${verdict#unverified }); nothing signalled" >&2 ;;
    esac
  done
  for s in "$TX"/cao-dojo-trace.*/tmux/tmux-*/default; do
    if [ -S "$s" ]; then
      env -u TMUX -u TMUX_PANE tmux -S "$s" kill-server 2>/dev/null || true
      # The server removes its socket as it exits; wait for that (up to 5 s) so the rm
      # below does not race it.
      for _ in 1 2 3 4 5 6 7 8 9 10; do [ -S "$s" ] || break; sleep 0.5; done
      echo "run.sh: stopped the private tmux server at ${s#"$TX"/}" >&2
    fi
  done
  rm -rf "$TX" 2>/dev/null || { sleep 1; rm -rf "$TX"; } || true
}
trap cleanup EXIT INT TERM
mkdir -p "$OUT"
env -u TMUX -u TMUX_PANE -u CAO_TERMINAL_ID -u CAO_HOME_DIR \
  TMPDIR="$TX" ROBOTICS_REPLAY_CHECKOUT="$REPO" PATH="$REPO/.venv/bin:$PATH" \
  perl -e 'alarm 420; exec @ARGV' \
  uv run --project "$REPO" --extra agui --with mujoco==3.17.0 --with pyyaml==6.0.3 \
  python "$HERE/record_robotics_trace.py" --port "$PORT" \
    --out "$OUT/robotics-mock-trace.ndjson" --cmdlog "$OUT/factory-command-log.json" \
    --raw-out "$TX/raw.ndjson"
