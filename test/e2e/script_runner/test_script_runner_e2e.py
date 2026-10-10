"""End-to-end OS-touching proofs for U4 ScriptRunner (issue #312, Bolt 3, C1).

These exercise the REAL ``asyncio.create_subprocess_exec`` path — the unit
suite (``test/services/test_script_runner.py``) mocks the subprocess, so these
are the practical proofs the code-generation test plan calls for:

- M1 fan-out shape: a real script that prints a ``CAO_WORKFLOW_OUTPUT:`` sentinel
  completes with the parsed output (loop/conditional/fan-out M1 shapes reduce to
  "a real Python process runs, prints a sentinel, exits 0").
- M2 chatty-child > 1 MiB: a real process writing more than the OS pipe buffer to
  stdout drains concurrently WITHOUT deadlock (the whole reason both pipes are
  pumped by dedicated reader tasks).
- real hang-then-reap: a real ``time.sleep`` child is reaped at the wall-clock
  bound and returns ``FAILED, kind=timeout`` within the bound + grace.

Excluded from the default matrix (``-m 'not e2e'``). They do NOT require a
running CAO server — the scripts here do not call back over run-step; they
exercise the spawn/drain/reap lifecycle only. A script that DID call run-step
would need the server, which is why the M1 fan-out proof keeps its script
self-contained. Run with: ``uv run pytest -m e2e test/e2e/test_script_runner_e2e.py``.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time

import pytest

from cli_agent_orchestrator.models.workflow_runtime import RunState
from cli_agent_orchestrator.services import script_runner, workflow_journal
from cli_agent_orchestrator.services.script_runner import run_script_workflow

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


def _kill_helper(pid_file) -> None:
    """Stop the background helper a test script started, if it is still running."""
    try:
        os.kill(int(pid_file.read_text()), signal.SIGKILL)
    except (FileNotFoundError, ValueError, ProcessLookupError):
        pass


class _RealSpec:
    """Duck-typed ScriptSpec whose source is written to a real temp file to exec."""

    def __init__(self, tmp_path, source: str, name: str = "e2e-wf"):
        self.path = str(tmp_path / f"{name}.py")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(source)
        self.source = source
        self.name = name
        self.content_hash = "e2e"


@pytest.fixture(autouse=True)
def _temp_db(tmp_path, monkeypatch):
    from cli_agent_orchestrator.clients.database import (
        _migrate_workflow_run,
        _migrate_workflow_run_step,
    )
    from cli_agent_orchestrator.services import workflow_service

    monkeypatch.setattr(
        "cli_agent_orchestrator.constants.DATABASE_FILE", tmp_path / "wf.db", raising=True
    )
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    workflow_service.run_registry.clear()
    workflow_service._active_drives.clear()
    yield


async def test_real_spawn_completes_with_sentinel(tmp_path):
    """M1: a real Python process prints a sentinel and exits 0 -> COMPLETED."""
    spec = _RealSpec(
        tmp_path,
        source='import json\nprint("CAO_WORKFLOW_OUTPUT:" + json.dumps({"ok": True}))\n',
    )
    result = await run_script_workflow(spec, {}, "e2e-ok")
    assert result.state == RunState.COMPLETED
    assert result.output == {"ok": True}


async def test_real_chatty_child_over_1mib_no_deadlock(tmp_path):
    """M2: a real child writing > 1 MiB to stdout drains without deadlock."""
    spec = _RealSpec(
        tmp_path,
        source=(
            "import sys, json\n"
            "sys.stdout.write('x' * (1024 * 1024 + 4096))\n"
            "print()\n"
            'print("CAO_WORKFLOW_OUTPUT:" + json.dumps({"drained": True}))\n'
        ),
    )
    result = await run_script_workflow(spec, {}, "e2e-chatty")
    assert result.state == RunState.COMPLETED
    assert result.output == {"drained": True}


async def test_real_hang_is_reaped_within_bound(tmp_path, monkeypatch):
    """A real sleeping child is reaped at the wall-clock bound -> FAILED,kind=timeout."""
    monkeypatch.setattr(script_runner, "WORKFLOW_SCRIPT_TIMEOUT", 0.5)
    monkeypatch.setattr(script_runner, "WORKFLOW_SCRIPT_TERM_GRACE", 0.5)
    spec = _RealSpec(tmp_path, source="import time\ntime.sleep(30)\n")
    result = await run_script_workflow(spec, {}, "e2e-hang")
    assert result.state == RunState.FAILED
    assert result.kind == "timeout"


async def test_real_background_child_holding_stdout_does_not_hold_the_run(tmp_path, monkeypatch):
    """A script that exits 0 after starting a long-lived helper completes promptly.

    The helper inherits stdout/stderr, so the pipes stay open after the script
    exits. The run must settle on the script's exit, not the helper's.
    """
    monkeypatch.setattr(script_runner, "WORKFLOW_SCRIPT_TIMEOUT", 5.0)
    monkeypatch.setattr(script_runner, "WORKFLOW_SCRIPT_TERM_GRACE", 0.5)
    pid_file = tmp_path / "helper.pid"
    spec = _RealSpec(
        tmp_path,
        source=(
            "import json, subprocess, sys\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(15)'])\n"
            f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
            'print("CAO_WORKFLOW_OUTPUT:" + json.dumps({"ok": True}), flush=True)\n'
        ),
    )
    try:
        started = time.monotonic()
        result = await run_script_workflow(spec, {}, "e2e-held-stdout")
        # The helper outlives this bound on purpose: on 3.10 the old unbounded
        # drain only returned once the helper exited.
        assert time.monotonic() - started < 5.0
        assert result.state == RunState.COMPLETED
        assert result.output == {"ok": True}
    finally:
        _kill_helper(pid_file)


async def test_real_background_child_still_writing_is_not_left_blocked(tmp_path, monkeypatch):
    """A helper that keeps writing after the run settles sees its pipe closed.

    Once the drain gives up, nothing reads the pipes. Left open, they fill and
    the helper blocks on its next write for good, holding the server's read fds.
    Closed, its next write fails with EPIPE and it can exit.
    """
    monkeypatch.setattr(script_runner, "WORKFLOW_SCRIPT_TIMEOUT", 5.0)
    monkeypatch.setattr(script_runner, "WORKFLOW_SCRIPT_TERM_GRACE", 0.5)
    pid_file = tmp_path / "helper.pid"
    marker = tmp_path / "helper.epipe"
    helper = tmp_path / "helper.py"
    helper.write_text(
        "import sys, time\n"
        "try:\n"
        "    for _ in range(100000):\n"
        "        sys.stdout.write('x' * 1023 + chr(10))\n"
        "        sys.stdout.flush()\n"
        "        time.sleep(0.0005)\n"
        "except BrokenPipeError:\n"
        f"    open({str(marker)!r}, 'w').close()\n",
        encoding="utf-8",
    )
    spec = _RealSpec(
        tmp_path,
        source=(
            "import subprocess, sys\n"
            f"p = subprocess.Popen([sys.executable, {str(helper)!r}])\n"
            f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
        ),
    )
    try:
        result = await run_script_workflow(spec, {}, "e2e-chatty-helper")
        assert result.state == RunState.COMPLETED
        assert any("still open" in w for w in result.warnings)
        deadline = time.monotonic() + 5.0
        while not marker.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert marker.exists(), "helper is still blocked writing to a pipe nobody reads"
    finally:
        _kill_helper(pid_file)


async def test_real_nonzero_exit_is_failed(tmp_path):
    """A real nonzero script persists stderr after the child process exits."""
    spec = _RealSpec(tmp_path, source='import sys\nsys.stderr.write("boom\\n")\nsys.exit(3)\n')
    result = await run_script_workflow(spec, {}, "e2e-crash")
    assert result.state == RunState.FAILED
    assert result.kind == "error"
    row = workflow_journal.get_run("e2e-crash")
    assert row is not None
    assert row.error == "boom"


async def test_real_shim_http_error_kind_reaches_stderr_tail(tmp_path):
    """An uncaught kinded refusal remains distinguishable in the run record."""
    spec = _RealSpec(
        tmp_path,
        source=(
            "from cao_workflow import ShimHTTPError\n"
            "raise ShimHTTPError("
            '409, \'{"detail":{"kind":"diverged","message":"step changed"}}\''
            ")\n"
        ),
    )

    result = await run_script_workflow(spec, {}, "e2e-kinded-refusal")

    assert result.state == RunState.FAILED
    assert result.kind == "error"
    row = workflow_journal.get_run("e2e-kinded-refusal")
    assert row is not None
    assert row.error is not None
    assert "run-step returned HTTP 409 (diverged): step changed" in row.error
