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
import json
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


def _write_chatty_helper(tmp_path, marker):
    """Write a helper that keeps writing to stdout and touches ``marker`` on EPIPE."""
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
    return helper


async def _wait_for_marker(marker, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not marker.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)


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
    # These subprocess proofs exercise the runner, independently of plan approval.
    from cli_agent_orchestrator.services import settings_service

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"workflow": {"require_approval": False}}))
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", settings)
    monkeypatch.delenv("CAO_WORKFLOW_REQUIRE_APPROVAL", raising=False)
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
        assert time.monotonic() - started < 3.0
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
    helper = _write_chatty_helper(tmp_path, marker)
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
        await _wait_for_marker(marker)
        assert marker.exists(), "helper is still blocked writing to a pipe nobody reads"
    finally:
        _kill_helper(pid_file)


async def test_real_timeout_with_background_child_writing_settles_and_frees_it(
    tmp_path, monkeypatch
):
    """A timed-out script that ignores SIGTERM, with a helper still writing, settles at the bound.

    From 3.11 ``wait()`` also waits for pipe EOF, so the reap after SIGKILL used
    to block until the helper exited. The timeout arm then has to close the
    pipes, or the helper blocks on a write nobody reads.
    """
    monkeypatch.setattr(script_runner, "WORKFLOW_SCRIPT_TIMEOUT", 1.0)
    monkeypatch.setattr(script_runner, "WORKFLOW_SCRIPT_TERM_GRACE", 0.5)
    pid_file = tmp_path / "helper.pid"
    marker = tmp_path / "helper.epipe"
    helper = _write_chatty_helper(tmp_path, marker)
    spec = _RealSpec(
        tmp_path,
        source=(
            "import signal, subprocess, sys, time\n"
            f"p = subprocess.Popen([sys.executable, {str(helper)!r}])\n"
            f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(60)\n"
        ),
    )
    try:
        started = time.monotonic()
        result = await asyncio.wait_for(
            run_script_workflow(spec, {}, "e2e-timeout-chatty-helper"), timeout=10.0
        )
        assert time.monotonic() - started < 3.0
        assert result.state == RunState.FAILED
        assert result.kind == "timeout"
        await _wait_for_marker(marker)
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


async def test_approved_script_imports_real_sdk_despite_sibling_shadow(monkeypatch, tmp_path):
    """Both fresh start and resume import the SDK despite an authored cao_workflow.py."""
    from pathlib import Path

    import cao_workflow
    from cli_agent_orchestrator.clients.database import _migrate_workflow_index
    from cli_agent_orchestrator.services import (
        approval_gate,
        approval_store,
        launch_guard,
        settings_service,
        workflow_service,
        workflow_spec_service,
    )

    monkeypatch.setattr(approval_store, "DATABASE_FILE", tmp_path / "wf.db")
    _migrate_workflow_index()
    settings_service.SETTINGS_FILE.write_text('{"workflow": {"require_approval": true}}')
    monkeypatch.setattr(launch_guard.agent_profiles, "list_agent_profiles", lambda: [])
    monkeypatch.setattr(launch_guard.settings_service, "is_memory_enabled", lambda: False)
    specs = tmp_path / "specs"
    specs.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(script_runner, "WORKFLOW_SCRIPT_SCRATCH_DIR", tmp_path / "scratch")
    marker = tmp_path / "shadow-imported"
    sdk_record = root / "imported-sdk"
    source = (
        "import cao_workflow, json, os, sys\n"
        f"open({str(sdk_record)!r}, 'w').write(cao_workflow.__file__)\n"
        "print('CAO_WORKFLOW_OUTPUT:' + json.dumps({'sdk': cao_workflow.__file__}))\n"
        "sys.exit(0 if os.environ.get('CAO_WORKFLOW_RESUME') else 1)\n"
    )
    spec = workflow_spec_service.create_workflow("sdkproof", source, scan_dir=str(specs))
    with pytest.raises(approval_gate.PlanApprovalRequiredError) as refusal:
        await run_script_workflow(spec, {}, "sdk-refused", working_directory=str(root))
    approval_store.grant(refusal.value.plan_id, "test-operator")
    workflow_spec_service.create_workflow(
        "cao_workflow",
        f"open({str(marker)!r}, 'w').write('shadow imported')\n",
        scan_dir=str(specs),
    )
    assert (specs / "cao_workflow.py").is_file()
    paths = []
    spawn = script_runner.asyncio.create_subprocess_exec

    async def observe_spawn(*args, **kwargs):
        paths.append(Path(args[1]))
        return await spawn(*args, **kwargs)

    monkeypatch.setattr(script_runner.asyncio, "create_subprocess_exec", observe_spawn)
    try:
        fresh = await run_script_workflow(spec, {}, "sdk-proof", working_directory=str(root))
        assert fresh.state == RunState.FAILED
        assert not marker.exists(), "fresh run imported the authored SDK shadow"
        assert Path(sdk_record.read_text()).resolve() == Path(cao_workflow.__file__).resolve()
        assert not paths[-1].exists()
        resumed = await script_runner.resume_script_run("sdk-proof")
        assert resumed.state == RunState.COMPLETED
        assert Path(resumed.output["sdk"]).resolve() == Path(cao_workflow.__file__).resolve()
        assert not marker.exists()
        assert paths[0].name == "start-sdk-proof.py"
        assert paths[1].name == "resume-sdk-proof.py"
        assert all(not path.exists() for path in paths)
    finally:
        workflow_service.run_registry.clear()
        workflow_service._active_drives.clear()
