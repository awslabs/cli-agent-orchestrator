"""The portable supervisor launcher creates a session and preserves launch errors."""
import os
import select
import signal
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


def test_launcher_closes_the_callers_stdout_while_the_supervisor_runs(tmp_path):
    pb = tmp_path / "pr-watch"
    (pb / "state").mkdir(parents=True)
    target = pb / "tick_supervisor.sh"
    target.write_text("#!/bin/bash\nwhile true; do sleep 1; done\n")
    target.chmod(0o755)
    launcher = KIT / "bin" / "start_tick_supervisor.py"
    launch = subprocess.Popen(
        [sys.executable, str(launcher), str(target)], stdout=subprocess.PIPE, text=False
    )
    pid_file = pb / "state" / "tick_supervisor.pid"
    try:
        end = time.monotonic() + 3
        while not pid_file.exists() and time.monotonic() < end:
            time.sleep(.02)
        pid = int(pid_file.read_text())
        assert os.kill(pid, 0) is None
        ready, _, _ = select.select([launch.stdout], [], [], 2)
        assert ready, "launcher stdout remained open while its supervisor ran"
        assert os.read(launch.stdout.fileno(), 1) == b""
    finally:
        if pid_file.exists():
            os.kill(int(pid_file.read_text()), signal.SIGTERM)
        launch.wait(timeout=3)


def test_launcher_ignores_a_live_pid_that_is_not_a_supervisor(tmp_path):
    pb = tmp_path / "pr-watch"
    state = pb / "state"
    state.mkdir(parents=True)
    target = pb / "tick_supervisor.sh"
    marker = tmp_path / "started"
    target.write_text(f"#!/bin/bash\ntouch {marker}\n")
    target.chmod(0o755)
    other = subprocess.Popen(["sleep", "30"])
    (state / "tick_supervisor.pid").write_text(f"{other.pid}\n")
    try:
        launcher = KIT / "bin" / "start_tick_supervisor.py"
        result = subprocess.run(
            [sys.executable, str(launcher), str(target)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0, result.stderr
        end = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < end:
            time.sleep(.02)
        assert marker.exists()
    finally:
        other.terminate()
        other.wait(timeout=3)
