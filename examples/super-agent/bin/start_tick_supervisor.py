#!/usr/bin/env python3
"""Start tick_supervisor.sh in its own session and retain launch errors beside its state."""
import os
import sys
from pathlib import Path


def live(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: start_tick_supervisor.py PB/tick_supervisor.sh")
    script = Path(sys.argv[1]).resolve()
    state = script.parent / "state"
    pid_file = state / "tick_supervisor.pid"
    log_file = state / "tick_supervisor.launch.log"
    state.mkdir(parents=True, exist_ok=True)
    try:
        old_pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        old_pid = None
    if old_pid is not None and live(old_pid):
        raise SystemExit(f"STOP: a supervisor already runs (pid {old_pid})")
    pid_file.unlink(missing_ok=True)
    pid = os.fork()
    if pid:
        return
    os.setsid()
    with open(log_file, "a", encoding="utf-8") as log:
        os.dup2(log.fileno(), sys.stderr.fileno())
        with open(pid_file, "w", encoding="utf-8") as out:
            out.write(f"{os.getpid()}\n")
            out.flush()
            os.fsync(out.fileno())
        os.execvp("bash", ["bash", str(script)])


if __name__ == "__main__":
    main()
