"""Unit and subprocess tests for the one-server-per-home lock (#406, #709).

The in-process contention tests rely on a property of ``flock``: locks belong
to an open file description, so a second ``open()`` of the same file in the
SAME process contends exactly as another process would. The subprocess tests
cover what no in-process test can: the kernel releasing the lock on SIGKILL,
and a child process NOT keeping it alive after its parent is gone.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cli_agent_orchestrator.utils import server_lock
from cli_agent_orchestrator.utils.server_lock import (
    ServerLockHeldError,
    ServerLockHolder,
    acquire_server_lock,
    format_refusal,
    read_holder,
)

STARTED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _hold_raw(path: Path, content: bytes) -> int:
    """Hold the lock the way a foreign process would, with arbitrary file content."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.ftruncate(fd, 0)
    os.write(fd, content)
    return fd


# --- acquire -----------------------------------------------------------------


def test_acquire_writes_diagnostics_and_holds(tmp_path: Path) -> None:
    lock_path = tmp_path / "home" / "server.lock"

    lock = acquire_server_lock(port=9889, path=lock_path)
    try:
        assert lock.held
        data = json.loads(lock_path.read_text())
        assert data["pid"] == os.getpid()
        assert data["port"] == 9889
        assert STARTED_AT.match(data["started_at"])
        assert isinstance(data["version"], str) and data["version"]
        assert set(data) == {"pid", "port", "started_at", "version"}
        assert lock.holder == read_holder(lock_path)
    finally:
        lock.release()


def test_lock_file_is_owner_only_and_not_inheritable(tmp_path: Path) -> None:
    lock_path = tmp_path / "server.lock"
    with acquire_server_lock(port=1, path=lock_path) as lock:
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
        assert lock._fd is not None
        assert os.get_inheritable(lock._fd) is False


def test_default_path_is_the_module_attribute(tmp_path: Path, monkeypatch) -> None:
    """The lifespan passes no path; the module attribute decides, at call time."""
    redirected = tmp_path / "redirected" / "server.lock"
    monkeypatch.setattr(server_lock, "SERVER_LOCK_FILE", redirected)
    with acquire_server_lock(port=7):
        assert redirected.exists()


# --- contend -----------------------------------------------------------------


def test_second_acquire_is_refused_with_holder_detail(tmp_path: Path) -> None:
    lock_path = tmp_path / "server.lock"
    first = acquire_server_lock(port=9889, path=lock_path)
    try:
        with pytest.raises(ServerLockHeldError) as excinfo:
            acquire_server_lock(port=9890, path=lock_path)
        err = excinfo.value
        assert err.home == tmp_path
        assert err.holder is not None
        assert err.holder.pid == os.getpid()
        assert err.holder.port == 9889
        lines = err.message().splitlines()
        assert lines[0].startswith(f"cao: another server (PID {os.getpid()}, port 9889, started ")
        assert lines[0].endswith("Z) already owns")
        assert lines[1] == f"     {tmp_path}"
        assert (
            lines[2]
            == "     Stop it, or run this instance with a different CAO_HOME_DIR and --port."
        )
        assert (
            lines[3]
            == "     Two servers on one data directory corrupt inbox delivery and terminal state."
        )
        assert len(lines) == 4
        # The loser did not clobber the winner's diagnostics.
        assert json.loads(lock_path.read_text())["port"] == 9889
    finally:
        first.release()


def test_unreadable_diagnostics_refuse_without_pid_detail(tmp_path: Path) -> None:
    lock_path = tmp_path / "server.lock"
    raw_fd = _hold_raw(lock_path, b"\x00not json at all")
    try:
        with pytest.raises(ServerLockHeldError) as excinfo:
            acquire_server_lock(port=9890, path=lock_path)
        assert excinfo.value.holder is None
        lines = excinfo.value.message().splitlines()
        assert lines[0] == "cao: another server already owns"
        assert lines[1] == f"     {tmp_path}"
        assert len(lines) == 4
    finally:
        os.close(raw_fd)


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"[]",
        b'{"pid": "4242", "port": 1, "started_at": "x", "version": "v"}',
        b'{"pid": true, "port": 1, "started_at": "x", "version": "v"}',
        b'{"pid": 1, "port": 2}',
    ],
)
def test_read_holder_rejects_malformed_shapes(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "server.lock"
    path.write_bytes(content)
    assert read_holder(path) is None


def test_read_holder_missing_file_is_none(tmp_path: Path) -> None:
    assert read_holder(tmp_path / "absent.lock") is None


def test_refusal_renders_started_at_to_the_minute() -> None:
    holder = ServerLockHolder(pid=4242, port=9889, started_at="2026-10-21T14:03:27Z", version="x")
    assert format_refusal(Path("/Users/you/.aws/cli-agent-orchestrator"), holder) == (
        "cao: another server (PID 4242, port 9889, started 2026-10-21T14:03Z) already owns\n"
        "     /Users/you/.aws/cli-agent-orchestrator\n"
        "     Stop it, or run this instance with a different CAO_HOME_DIR and --port.\n"
        "     Two servers on one data directory corrupt inbox delivery and terminal state."
    )


def test_refusal_keeps_an_unparseable_started_at_verbatim() -> None:
    holder = ServerLockHolder(pid=1, port=2, started_at="yesterday", version="x")
    assert "started yesterday)" in format_refusal(Path("/h"), holder)


# --- release -----------------------------------------------------------------


def test_release_allows_reacquire_and_keeps_the_file(tmp_path: Path) -> None:
    lock_path = tmp_path / "server.lock"
    first = acquire_server_lock(port=1, path=lock_path)
    first.release()
    assert not first.held
    first.release()  # idempotent
    assert lock_path.exists(), "the lock file is never unlinked"
    with acquire_server_lock(port=2, path=lock_path):
        assert json.loads(lock_path.read_text())["port"] == 2


def test_context_manager_releases_on_exit(tmp_path: Path) -> None:
    lock_path = tmp_path / "server.lock"
    with acquire_server_lock(port=1, path=lock_path) as lock:
        with pytest.raises(ServerLockHeldError):
            acquire_server_lock(port=2, path=lock_path)
    assert not lock.held
    acquire_server_lock(port=3, path=lock_path).release()


def test_refuse_prints_to_stderr_and_exits_one(tmp_path: Path, capsys) -> None:
    holder = ServerLockHolder(pid=4242, port=9889, started_at="2026-10-21T14:03:27Z", version="x")
    err = ServerLockHeldError(tmp_path / "server.lock", holder)
    with pytest.raises(SystemExit) as excinfo:
        server_lock.refuse(err)
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == err.message() + "\n"


# --- subprocess: kernel release on SIGKILL, no inheritance ----------------------

_HOLDER_SCRIPT = r"""
import os, subprocess, sys, time
from pathlib import Path
from cli_agent_orchestrator.utils.server_lock import acquire_server_lock

lock = acquire_server_lock(port=int(sys.argv[2]), path=Path(sys.argv[1]))
child = None
if len(sys.argv) > 3 and sys.argv[3] == "spawn-child":
    # close_fds=False so ONLY O_CLOEXEC stands between the child and the lock fd.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], close_fds=False)
print(f"held {os.getpid()} {child.pid if child else 0}", flush=True)
time.sleep(60)
"""


def _spawn_holder(lock_path: Path, tmp_home: Path, *extra: str) -> tuple[subprocess.Popen, int]:
    env = dict(os.environ, CAO_HOME_DIR=str(tmp_home), PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT, str(lock_path), "4242", *extra],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if not line.startswith("held "):
        proc.kill()
        _, err = proc.communicate(timeout=10)
        pytest.fail(f"holder did not start: stdout={line!r} stderr={err}")
    _, _pid, child_pid = line.split()
    return proc, int(child_pid)


def _wait_until_acquirable(lock_path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            acquire_server_lock(port=1, path=lock_path).release()
            return
        except ServerLockHeldError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def test_sigkill_releases_the_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "home" / "server.lock"
    holder, _ = _spawn_holder(lock_path, tmp_path / "home")
    try:
        with pytest.raises(ServerLockHeldError) as excinfo:
            acquire_server_lock(port=1, path=lock_path)
        assert excinfo.value.holder is not None
        assert excinfo.value.holder.pid == holder.pid
        assert excinfo.value.holder.port == 4242

        holder.kill()  # SIGKILL: no cleanup code runs in the holder
        holder.wait(timeout=10)
        assert holder.returncode == -signal.SIGKILL

        _wait_until_acquirable(lock_path)
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)


def test_child_process_does_not_inherit_the_lock(tmp_path: Path) -> None:
    """Close-on-exec: a provider subprocess outliving the server must not hold the home."""
    lock_path = tmp_path / "home" / "server.lock"
    holder, child_pid = _spawn_holder(lock_path, tmp_path / "home", "spawn-child")
    try:
        assert child_pid > 0
        with pytest.raises(ServerLockHeldError):
            acquire_server_lock(port=1, path=lock_path)

        holder.kill()
        holder.wait(timeout=10)
        os.kill(child_pid, 0)  # the child is still alive...
        _wait_until_acquirable(lock_path)  # ...and does not hold the lock
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)
        try:
            os.kill(child_pid, signal.SIGKILL)
            os.waitpid(child_pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
