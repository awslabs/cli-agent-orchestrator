"""The portable supervisor launcher creates a session and preserves launch errors."""
import subprocess
import sys
import time

from conftest import KIT


def test_launcher_writes_the_pid_then_executes_in_a_new_session(tmp_path):
    pb = tmp_path / "pr-watch"
    (pb / "state").mkdir(parents=True)
    marker = tmp_path / "marker"
    target = pb / "tick_supervisor.sh"
    target.write_text(
        "#!/bin/bash\n"
        "test \"$(ps -o sid= -p $$ | tr -d ' ')\" = \"$$\" || exit 9\n"
        f"echo \"$PPID $$\" > {marker}\n"
        "echo launch-error >&2\n"
        "sleep 1\n"
    )
    target.chmod(0o755)
    launcher = KIT / "bin" / "start_tick_supervisor.py"
    r = subprocess.run([sys.executable, str(launcher), str(target)], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    pid_file = pb / "state" / "tick_supervisor.pid"
    end = time.monotonic() + 3
    while not marker.exists() and time.monotonic() < end:
        time.sleep(.02)
    assert marker.exists() and pid_file.read_text().strip().isdigit()
    end = time.monotonic() + 3
    log = pb / "state" / "tick_supervisor.launch.log"
    while not log.exists() and time.monotonic() < end:
        time.sleep(.02)
    assert "launch-error" in log.read_text()
