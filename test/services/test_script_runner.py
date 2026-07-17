"""Tests for U4 ScriptRunner (issue #312, Bolt 3, C1).

The subprocess is MOCKED throughout (fast, hermetic, default matrix) — the
OS-touching proofs live under the ``e2e`` marker in
``test/e2e/test_script_runner_e2e.py``. Coverage maps to the code-generation
test plan:

- M1 result-shape contract: a script COMPLETED result has the same base fields
  as a YAML run + ``kind=None``/``output``/``warnings=[]`` (INV-5).
- crash lifecycle: nonzero exit -> ``FAILED, kind=error``, stderr tail surfaced,
  orphan sweep fired.
- hang lifecycle: exit never arrives -> ``TimeoutBound`` -> ``_terminate`` ->
  ``FAILED, kind=timeout``; generation bumped on the timeout arm (INV-6).
- cancel lifecycle: signal-first order; a second cancel is a logged no-op.
- resume admission: absent -> 404 (``KeyError``); live -> 409
  (``ResumeNotAllowedError``); non-resumable -> 409; corrupt snapshot -> 422
  (``ResumeCorruptError``); happy resume materializes + execs a temp file and
  deletes it in ``finally``.
- sentinel present/absent/malformed/duplicate (last-match); skipped on FAILED.
- ring-buffer truncation marker.
- orphan sweep off the in-memory ``step_states`` (BR-31 5b).
- pipe-drain no-deadlock: a chatty child (> 1 MiB) drains without deadlock.

Async tests use ``@pytest.mark.asyncio``. The journal points at a temp SQLite
DB via the patched ``DATABASE_FILE`` (same fixture idiom as the U3 tests).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import List, Optional

import pytest

from cli_agent_orchestrator.clients.database import (
    _migrate_workflow_run,
    _migrate_workflow_run_step,
)
from cli_agent_orchestrator.models.workflow import StepState
from cli_agent_orchestrator.models.workflow_runtime import RunState, WorkflowRunResult
from cli_agent_orchestrator.services import script_runner, workflow_journal
from cli_agent_orchestrator.services.script_runner import (
    ScriptLintError,
    ScriptRunRecord,
    TimeoutBound,
    _bump,
    _RingBuffer,
    _scan_sentinel,
    _step_call_fingerprint,
    cancel_script_run,
    make_step_terminal_recorder,
    record_step_completion,
    resume_script_run,
    run_script_workflow,
)
from cli_agent_orchestrator.services.workflow_service import (
    ResumeCorruptError,
    ResumeNotAllowedError,
    StepRunState,
)


# ---------------------------------------------------------------------------
# Fixtures + fakes
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _patched_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Temp DB + tables; reset the shared registry/active-drives around each test."""
    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    # Isolate the process-local registry between tests.
    from cli_agent_orchestrator.services import workflow_service

    workflow_service.run_registry.clear()
    workflow_service._active_drives.clear()
    yield db_path
    workflow_service.run_registry.clear()
    workflow_service._active_drives.clear()


class _FakeScriptSpec:
    """Duck-typed stand-in for U5's ScriptSpec (U4 only reads these attrs)."""

    def __init__(self, source: str = "print('hi')", path: str = "/tmp/wf.py", name: str = "wf"):
        self.source = source
        self.path = path
        self.name = name
        self.content_hash = "deadbeef"


class _FakeProcess:
    """A fake ``asyncio.subprocess.Process`` for lifecycle tests.

    ``returncode`` is None until ``exit_rc`` is delivered. ``wait()`` returns
    immediately with ``exit_rc`` unless ``hang=True`` (then it awaits forever,
    exercising the wall-clock reaper). ``terminate``/``kill`` record the signal
    order and settle the returncode so ``_terminate``'s reap completes — UNLESS
    ``uncooperative=True``, in which case ``terminate()`` (SIGTERM) is recorded
    but does NOT settle the process (an uncooperative child that ignores
    SIGTERM), so only ``kill()`` (SIGKILL) releases ``wait()``.
    """

    def __init__(
        self,
        *,
        exit_rc: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        hang: bool = False,
        uncooperative: bool = False,
    ):
        self._exit_rc = exit_rc
        self.returncode: Optional[int] = None
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)
        self._hang = hang
        self._uncooperative = uncooperative
        self.signals: List[str] = []
        self._exited = asyncio.Event()
        if not hang:
            self.returncode = None  # settled by wait()

    async def wait(self) -> int:
        if self._hang and not self._exited.is_set():
            await self._exited.wait()
        if self.returncode is None:
            self.returncode = self._exit_rc
        return self.returncode

    def terminate(self) -> None:
        self.signals.append("SIGTERM")
        if self._uncooperative:
            return  # ignores SIGTERM — only kill() below releases wait()
        # A cooperative child exits on SIGTERM: settle + release wait().
        self.returncode = self._exit_rc
        self._exited.set()

    def kill(self) -> None:
        self.signals.append("SIGKILL")
        self.returncode = -9
        self._exited.set()


class _FakeStream:
    """A fake ``StreamReader`` yielding a fixed payload then EOF, in chunks."""

    def __init__(self, payload: bytes):
        self._payload = payload
        self._pos = 0

    async def read(self, n: int = -1) -> bytes:
        if self._pos >= len(self._payload):
            return b""
        chunk = self._payload[self._pos : self._pos + (n if n > 0 else len(self._payload))]
        self._pos += len(chunk)
        return chunk


def _install_fake_spawn(monkeypatch: pytest.MonkeyPatch, process: _FakeProcess) -> dict:
    """Patch ``asyncio.create_subprocess_exec`` to return ``process``; capture args."""
    captured: dict = {}

    async def _fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return process

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.script_runner.asyncio.create_subprocess_exec",
        _fake_exec,
    )
    return captured


# ---------------------------------------------------------------------------
# Pure-helper unit tests
# ---------------------------------------------------------------------------
def test_scan_sentinel_absent_returns_null():
    assert _scan_sentinel("no sentinel here\njust logs") == (None, [])


def test_scan_sentinel_last_match_wins():
    text = 'CAO_WORKFLOW_OUTPUT:{"n": 1}\nprogress\nCAO_WORKFLOW_OUTPUT:{"n": 2}'
    output, warnings = _scan_sentinel(text)
    assert output == {"n": 2}
    assert warnings == []


def test_scan_sentinel_malformed_payload_warns_output_null():
    output, warnings = _scan_sentinel("CAO_WORKFLOW_OUTPUT:{not json}")
    assert output is None
    assert len(warnings) == 1 and "malformed sentinel payload" in warnings[0]


def test_ring_buffer_truncation_marker():
    ring = _RingBuffer(cap=16)
    ring.append(b"0123456789")
    ring.append(b"abcdefghij")  # total 20 > 16 -> drop oldest 4
    text = ring.text()
    assert ring.truncated is True
    assert "output truncated" in text
    assert text.endswith("456789abcdefghij")


def test_ring_buffer_no_truncation_under_cap():
    ring = _RingBuffer(cap=100)
    ring.append(b"hello")
    assert ring.truncated is False
    assert ring.text() == "hello"


def test_bump_increments_integer_generation():
    assert _bump("1") == "2"
    assert _bump("9") == "10"


def test_bump_non_integer_anchors_to_two():
    assert _bump("not-a-number") == "2"


# ---------------------------------------------------------------------------
# Unit A — _build_env delivers CAO_WORKFLOW_INPUTS (FR-A3, BR-A5)
# ---------------------------------------------------------------------------
def test_build_env_exact_six_keys_with_inputs():
    env = script_runner._build_env("run-x", "1", {"topic": "birds", "count": 3})
    assert set(env) == {
        "CAO_WORKFLOW_RUN_ID",
        "CAO_WORKFLOW_GENERATION",
        "CAO_API_BASE_URL",
        "CAO_WORKFLOW_INPUTS",
        "PATH",
        "HOME",
    }
    # Compact JSON, deterministic separators.
    assert env["CAO_WORKFLOW_INPUTS"] == '{"topic":"birds","count":3}'


def test_build_env_two_arg_call_defaults_empty_inputs():
    """The legacy 2-arg call site still works; inputs default to ``{}``."""
    env = script_runner._build_env("run-x", "1")
    assert env["CAO_WORKFLOW_INPUTS"] == "{}"
    assert "CAO_WORKFLOW_RESUME" not in env


def test_build_env_resume_adds_flag_and_keeps_inputs():
    env = script_runner._build_env("run-x", "4", {"a": 1}, resume=True)
    assert env["CAO_WORKFLOW_RESUME"] == "1"
    assert env["CAO_WORKFLOW_INPUTS"] == '{"a":1}'


# ---------------------------------------------------------------------------
# A1 — run_script_workflow lifecycle
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_lint_fail_raises_before_any_spawn(monkeypatch: pytest.MonkeyPatch):
    """BR-1: a lint fail raises ScriptLintError; zero code runs, no journal row."""
    spawned = {"called": False}

    async def _boom(*a, **k):  # pragma: no cover — must never be reached
        spawned["called"] = True
        raise AssertionError("spawn must not happen on lint fail")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.script_runner.asyncio.create_subprocess_exec", _boom
    )
    # A CAO-internal import is a hard disallowed-import ERROR -> status "fail".
    spec = _FakeScriptSpec(source="import cli_agent_orchestrator\n")
    with pytest.raises(ScriptLintError) as ei:
        await run_script_workflow(spec, {}, "run-lint-fail")
    assert ei.value.findings  # carries U1 findings for U5's 422 body
    assert spawned["called"] is False
    assert workflow_journal.get_run("run-lint-fail") is None


@pytest.mark.asyncio
async def test_happy_completed_result_shape_and_sentinel(monkeypatch: pytest.MonkeyPatch):
    """M1 + A6: exit 0 -> COMPLETED, tier-neutral shape, sentinel output parsed."""
    proc = _FakeProcess(exit_rc=0, stdout=b'log line\nCAO_WORKFLOW_OUTPUT:{"answer": 42}\n')
    captured = _install_fake_spawn(monkeypatch, proc)
    result = await run_script_workflow(_FakeScriptSpec(), {}, "run-ok")

    assert isinstance(result, WorkflowRunResult)
    assert result.state == RunState.COMPLETED
    assert result.run_id == "run-ok"
    assert result.workflow_name == "wf"
    assert result.kind is None
    assert result.output == {"answer": 42}
    assert result.warnings == []
    # Journaled with tier=script, generation=1 (additive insert_run kwargs).
    row = workflow_journal.get_run("run-ok")
    assert row is not None and row.tier == "script" and row.generation == "1"
    assert row.state == "completed"
    # F3: the journaled started_at is the SAME timestamp as the record's, not a
    # second independent _now() call.
    assert row.started_at == result.started_at
    # Constructed env is the exact 6-key allowlist (INV-2 + BR-A5), no resume flag.
    env = captured["env"]
    assert set(env) == {
        "CAO_WORKFLOW_RUN_ID",
        "CAO_WORKFLOW_GENERATION",
        "CAO_API_BASE_URL",
        "CAO_WORKFLOW_INPUTS",
        "PATH",
        "HOME",
    }
    assert env["CAO_WORKFLOW_RUN_ID"] == "run-ok"
    assert env["CAO_WORKFLOW_GENERATION"] == "1"


@pytest.mark.asyncio
async def test_crash_nonzero_exit_failed_kind_error(monkeypatch: pytest.MonkeyPatch):
    """Nonzero exit -> FAILED, kind=error, stderr tail surfaced, sweep fired."""
    swept = {"run": None}

    async def _fake_sweep(run_id):
        swept["run"] = run_id

    monkeypatch.setattr(script_runner, "_reconcile_orphans", _fake_sweep)
    proc = _FakeProcess(exit_rc=1, stderr=b"Traceback: boom\n")
    _install_fake_spawn(monkeypatch, proc)

    result = await run_script_workflow(_FakeScriptSpec(), {}, "run-crash")
    assert result.state == RunState.FAILED
    assert result.kind == "error"
    assert any("boom" in w for w in result.warnings)  # stderr tail surfaced
    assert swept["run"] == "run-crash"


@pytest.mark.asyncio
async def test_crash_skips_sentinel_scan_br9a(monkeypatch: pytest.MonkeyPatch):
    """BR-9a: a sentinel printed then nonzero exit yields output=null (scan skipped)."""
    monkeypatch.setattr(script_runner, "_reconcile_orphans", _noop_sweep)
    proc = _FakeProcess(
        exit_rc=2, stdout=b'CAO_WORKFLOW_OUTPUT:{"leaked": true}\n', stderr=b"then crash"
    )
    _install_fake_spawn(monkeypatch, proc)
    result = await run_script_workflow(_FakeScriptSpec(), {}, "run-crash-sentinel")
    assert result.state == RunState.FAILED
    assert result.output is None  # sentinel NOT surfaced on a failed run


@pytest.mark.asyncio
async def test_hang_timeout_reap_bumps_generation(monkeypatch: pytest.MonkeyPatch):
    """Hang -> TimeoutBound -> _terminate -> FAILED,kind=timeout; gen bumped (INV-6)."""
    monkeypatch.setattr(script_runner, "_reconcile_orphans", _noop_sweep)
    # Tiny bound so the reaper fires fast in the test.
    monkeypatch.setattr(script_runner, "WORKFLOW_SCRIPT_TIMEOUT", 0.05)
    monkeypatch.setattr(script_runner, "WORKFLOW_SCRIPT_TERM_GRACE", 0.05)
    proc = _FakeProcess(exit_rc=0, hang=True)
    _install_fake_spawn(monkeypatch, proc)

    result = await run_script_workflow(_FakeScriptSpec(), {}, "run-hang")
    assert result.state == RunState.FAILED
    assert result.kind == "timeout"
    assert "SIGTERM" in proc.signals  # _terminate escalated
    # Generation bumped + persisted on the timeout arm (INV-6, the straggler fence).
    row = workflow_journal.get_run("run-hang")
    assert row is not None and row.generation == "2"
    # F10d: the wall-clock timeout marker is surfaced in warnings for observability.
    assert any("[wall-clock timeout]" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_timeout_bound_helper_raises():
    """_await_exit_within_bound converts the elapsed bound into TimeoutBound."""
    proc = _FakeProcess(exit_rc=0, hang=True)
    with pytest.raises(TimeoutBound):
        await script_runner._await_exit_within_bound(proc, timeout=0.02)


# ---------------------------------------------------------------------------
# A4 — _terminate escalation (F7, Important #2)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_terminate_escalates_to_sigkill_when_uncooperative():
    """An uncooperative child (ignores SIGTERM) is escalated to SIGKILL and reaped."""
    proc = _FakeProcess(exit_rc=0, hang=True, uncooperative=True)
    await script_runner._terminate(proc, grace=0.05)
    assert proc.signals == ["SIGTERM", "SIGKILL"]
    assert proc.returncode == -9  # reaped after kill()


@pytest.mark.asyncio
async def test_terminate_cooperative_never_escalates():
    """A cooperative child settles on SIGTERM alone — kill() is never called."""
    proc = _FakeProcess(exit_rc=0, hang=True)
    await script_runner._terminate(proc, grace=0.05)
    assert proc.signals == ["SIGTERM"]
    assert proc.returncode == 0


@pytest.mark.asyncio
async def test_spawn_failure_returns_failed_never_raises(monkeypatch: pytest.MonkeyPatch):
    """F4: a spawn-time OSError is caught -> FAILED,kind=error; never raises.

    Module contract: only the lint gate and admission gates raise out of U4 —
    a spawn failure (e.g. the interpreter vanished) is a run failure, not an
    engine invariant violation.
    """
    monkeypatch.setattr(script_runner, "_reconcile_orphans", _noop_sweep)

    async def _boom_exec(*a, **k):
        raise FileNotFoundError("no such file: python3")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.script_runner.asyncio.create_subprocess_exec",
        _boom_exec,
    )
    result = await run_script_workflow(_FakeScriptSpec(), {}, "run-spawn-fail")
    assert result.state == RunState.FAILED
    assert result.kind == "error"
    assert any("spawn failed" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_chatty_child_no_deadlock(monkeypatch: pytest.MonkeyPatch):
    """M2: a child writing > 1 MiB to stdout drains concurrently without deadlock."""
    big = b"x" * (1024 * 1024 + 500) + b'\nCAO_WORKFLOW_OUTPUT:{"done": true}\n'
    proc = _FakeProcess(exit_rc=0, stdout=big)
    _install_fake_spawn(monkeypatch, proc)
    result = await asyncio.wait_for(
        run_script_workflow(_FakeScriptSpec(), {}, "run-chatty"), timeout=5.0
    )
    assert result.state == RunState.COMPLETED
    # The sentinel is in the tail, so it survives the ring-buffer cap.
    assert result.output == {"done": True}


# ---------------------------------------------------------------------------
# A3 — cancel_script_run
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cancel_signal_first_order(monkeypatch: pytest.MonkeyPatch):
    """Signal-first order: gen bump -> terminate -> sweep -> journal CANCELLED."""
    order: List[str] = []

    async def _fake_sweep(run_id):
        order.append("sweep")

    monkeypatch.setattr(script_runner, "_reconcile_orphans", _fake_sweep)
    proc = _FakeProcess(exit_rc=0)

    orig_terminate = script_runner._terminate

    async def _tracked_terminate(process, grace):
        order.append("terminate")
        await orig_terminate(process, grace)

    monkeypatch.setattr(script_runner, "_terminate", _tracked_terminate)

    _seed_script_run("run-cancel", generation="1")
    record = _make_record("run-cancel", process=proc, generation="1")

    await cancel_script_run(record)
    assert order == ["terminate", "sweep"]
    assert record.state == RunState.CANCELLED
    assert record.finished_at is not None
    # Generation bumped + persisted (DR-11) BEFORE terminate.
    row = workflow_journal.get_run("run-cancel")
    assert row is not None and row.generation == "2"
    assert row.state == "cancelled"  # retained -> resumable for scripts


@pytest.mark.asyncio
async def test_cancel_drive_race_drive_yields_to_cancelled(monkeypatch: pytest.MonkeyPatch):
    """F2: cancel wins a race with the drive's own exit interpretation.

    A cancel signals the process; the process then "exits" (rc != 0, as an
    unrelated SIGTERM-adjacent death would look). ``_drive_process`` must check
    ``record.cancelled`` BEFORE interpreting ``rc`` and defer to the CANCELLED
    result cancel_script_run already journaled — never overwrite it with
    FAILED.
    """
    monkeypatch.setattr(script_runner, "_reconcile_orphans", _noop_sweep)
    proc = _FakeProcess(exit_rc=1)  # would-be FAILED if cancel had not already fired
    _install_fake_spawn(monkeypatch, proc)

    _seed_script_run("run-race", generation="1")
    record = _make_record("run-race", process=None, generation="1")
    from cli_agent_orchestrator.services import workflow_service

    workflow_service.run_registry["run-race"] = record
    env = script_runner._build_env("run-race", "1")

    async def _cancel_mid_flight():
        # Simulate cancel_script_run having already fired: signal + journal
        # CANCELLED, set the flag the drive must respect.
        record.cancelled = True
        record.state = RunState.CANCELLED
        record.finished_at = script_runner._now()
        await asyncio.to_thread(
            workflow_journal.update_run_state,
            "run-race",
            RunState.CANCELLED.value,
            record.finished_at,
        )

    await _cancel_mid_flight()
    result = await script_runner._drive_process(record, "/tmp/wf.py", env)

    assert result.state == RunState.CANCELLED
    assert result.kind == "cancelled"
    row = workflow_journal.get_run("run-race")
    assert row is not None and row.state == "cancelled"  # drive did not overwrite it


@pytest.mark.asyncio
async def test_cancel_idempotent_second_is_noop(monkeypatch: pytest.MonkeyPatch):
    """BR-19: a second cancel on an already-cancelling record is a logged no-op."""
    monkeypatch.setattr(script_runner, "_reconcile_orphans", _noop_sweep)
    _seed_script_run("run-cancel2", generation="1")
    record = _make_record("run-cancel2", process=_FakeProcess(), generation="1")
    record.cancelled = True  # already cancelling
    await cancel_script_run(record)
    # No generation bump happened (the guard returned before step 1).
    row = workflow_journal.get_run("run-cancel2")
    assert row is not None and row.generation == "1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_state", [RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED]
)
async def test_cancel_terminal_record_is_service_noop(
    monkeypatch: pytest.MonkeyPatch, terminal_state: RunState
):
    """Direct callers cannot rewrite a retained terminal record to CANCELLED."""

    async def _must_not_sweep(run_id):
        raise AssertionError("terminal run must return before cancellation work")

    monkeypatch.setattr(script_runner, "_reconcile_orphans", _must_not_sweep)
    _seed_script_run("run-terminal", state=terminal_state.value, generation="1")
    process = _FakeProcess()
    record = _make_record("run-terminal", process=process, generation="1")
    record.state = terminal_state
    record.finished_at = "2026-07-08T00:00:01Z"

    await cancel_script_run(record)

    row = workflow_journal.get_run("run-terminal")
    assert row is not None and row.state == terminal_state.value
    assert row.generation == "1"
    assert record.state == terminal_state
    assert record.cancelled is False
    assert process.signals == []


# ---------------------------------------------------------------------------
# A2 — resume admission + happy path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resume_absent_run_raises_keyerror():
    with pytest.raises(KeyError):
        await resume_script_run("nope")


@pytest.mark.asyncio
async def test_resume_live_run_raises_not_allowed(monkeypatch: pytest.MonkeyPatch):
    """Gate 2: a run made live through the REAL drive path -> ResumeNotAllowedError.

    Regression for b4c1 (double-drive): liveness must be established by the actual
    ``run_script_workflow`` drive registering into ``_active_drives`` — NOT by
    hand-seeding set membership. We spawn a never-exiting (hang) process so the
    run stays mid-drive, wait until the drive marks itself live, then attempt a
    concurrent resume and assert the 409. Finally we release the process so the
    drive task settles and clears the liveness mark.
    """
    from cli_agent_orchestrator.services import workflow_service

    monkeypatch.setattr(script_runner, "_reconcile_orphans", _noop_sweep)
    proc = _FakeProcess(exit_rc=0, hang=True)
    _install_fake_spawn(monkeypatch, proc)

    drive = asyncio.create_task(run_script_workflow(_FakeScriptSpec(), {}, "run-live"))
    try:
        # Wait for the real drive path to register liveness (never hand-seeded).
        for _ in range(200):
            if "run-live" in workflow_service._active_drives:
                break
            await asyncio.sleep(0.005)
        assert "run-live" in workflow_service._active_drives  # established by the drive

        with pytest.raises(ResumeNotAllowedError):
            await resume_script_run("run-live")
    finally:
        # Release the hang so the drive task finalizes and clears the mark.
        proc.terminate()
        await asyncio.wait_for(drive, timeout=5.0)
    assert "run-live" not in workflow_service._active_drives  # cleared on drive exit


@pytest.mark.asyncio
async def test_resume_toctou_second_concurrent_resume_rejected(monkeypatch: pytest.MonkeyPatch):
    """F5: a second concurrent resume is fenced even while the first is pre-spawn.

    Without marking ``_active_drives`` immediately after Gate 2, two resumes for
    the same run_id could both pass Gate 2 before either registers liveness (a
    TOCTOU double-drive). We block the FIRST resume inside a patched
    ``update_run_generation`` (after Gate 2 passes, before spawn — it runs via
    ``asyncio.to_thread``, so blocking with a plain ``threading.Event`` does not
    freeze the event loop) and assert a SECOND concurrent resume attempt hits
    Gate 2 and gets ``ResumeNotAllowedError`` while the first is still blocked.
    """
    import threading

    from cli_agent_orchestrator.services import workflow_service
    from cli_agent_orchestrator.services.workflow_service import update_run_generation

    _seed_script_run("run-toctou", state="failed", generation="1")

    first_blocked = threading.Event()
    release_first = threading.Event()

    def _blocking_update_run_generation(run_id, generation):
        first_blocked.set()
        release_first.wait(timeout=5.0)
        return update_run_generation(run_id, generation)

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.workflow_service.update_run_generation",
        _blocking_update_run_generation,
    )

    first = asyncio.create_task(resume_script_run("run-toctou"))
    try:
        for _ in range(500):
            if first_blocked.is_set():
                break
            await asyncio.sleep(0.01)
        assert first_blocked.is_set()
        # The first resume passed Gate 2 and is now blocked pre-spawn — it must
        # already be marked live so a second concurrent resume is fenced.
        assert "run-toctou" in workflow_service._active_drives
        with pytest.raises(ResumeNotAllowedError):
            await resume_script_run("run-toctou")
    finally:
        release_first.set()
        # Let the first resume's drive (real spawn, mocked below) settle.
        proc = _FakeProcess(exit_rc=0)
        _install_fake_spawn(monkeypatch, proc)
        await asyncio.wait_for(first, timeout=5.0)
    assert "run-toctou" not in workflow_service._active_drives


@pytest.mark.asyncio
async def test_resume_traversal_run_id_rejected_no_file_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """B2: a traversal run_id is rejected (ValueError) before any path/exec use.

    A run_id like ``../../../tmp/evil`` must never reach ``_materialize_snapshot``
    where it would compose ``scratch/resume-{run_id}.py`` and get exec'd (arbitrary
    file write + code exec). The shared key validator rejects it at the top of
    ``resume_script_run`` — no snapshot file is created anywhere.
    """
    # Point the scratch root at an empty temp dir so we can assert nothing landed.
    scratch = tmp_path / "scratch"
    monkeypatch.setattr(script_runner, "WORKFLOW_SCRIPT_SCRATCH_DIR", scratch, raising=True)

    async def _boom_spawn(*a, **k):  # pragma: no cover — must never be reached
        raise AssertionError("spawn must not happen on a traversal run_id")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.script_runner.asyncio.create_subprocess_exec",
        _boom_spawn,
    )

    evil = "../../../tmp/evil"
    with pytest.raises(ValueError):
        await resume_script_run(evil)

    # No file was written outside (or inside) the scratch root — validation fired
    # before materialize, so the traversal target never exists.
    assert not (tmp_path / "tmp" / "evil").exists()
    assert not (Path("/tmp") / "evil.py").exists()
    if scratch.exists():
        assert list(scratch.iterdir()) == []


@pytest.mark.asyncio
async def test_resume_completed_run_not_resumable():
    """Gate 3: a COMPLETED run is terminal -> ResumeNotAllowedError (409)."""
    _seed_script_run("run-done", state="completed", generation="1")
    with pytest.raises(ResumeNotAllowedError):
        await resume_script_run("run-done")


@pytest.mark.asyncio
async def test_resume_corrupt_snapshot_raises_corrupt():
    """Gate 4: a spec_snapshot with no source -> ResumeCorruptError (422)."""
    workflow_journal.insert_run(
        run_id="run-corrupt",
        workflow_name="wf",
        spec_snapshot="{not valid json",
        inputs_json="{}",
        state="failed",
        started_at="2026-07-08T00:00:00Z",
        tier="script",
        generation="1",
    )
    with pytest.raises(ResumeCorruptError):
        await resume_script_run("run-corrupt")


@pytest.mark.asyncio
async def test_resume_happy_materializes_and_deletes_temp(monkeypatch: pytest.MonkeyPatch):
    """A FAILED script run resumes: bump+persist gen, exec a temp file, delete it."""
    monkeypatch.setattr(script_runner, "_reconcile_orphans", _noop_sweep)
    source = "print('resumed')\n"
    workflow_journal.insert_run(
        run_id="run-resume",
        workflow_name="wf",
        spec_snapshot=json.dumps({"source": source, "path": "/tmp/wf.py"}),
        inputs_json="{}",
        state="failed",
        started_at="2026-07-08T00:00:00Z",
        tier="script",
        generation="3",
    )
    proc = _FakeProcess(exit_rc=0, stdout=b"CAO_WORKFLOW_OUTPUT:null\n")
    captured = _install_fake_spawn(monkeypatch, proc)

    result = await resume_script_run("run-resume")
    assert result.state == RunState.COMPLETED
    # Generation bumped BEFORE spawn and persisted (INV-6): 3 -> 4.
    row = workflow_journal.get_run("run-resume")
    assert row is not None and row.generation == "4"
    # Resume env carries CAO_WORKFLOW_RESUME=1 + the bumped generation.
    env = captured["env"]
    assert env["CAO_WORKFLOW_RESUME"] == "1"
    assert env["CAO_WORKFLOW_GENERATION"] == "4"
    # The exec'd path is the engine-owned materialized temp file, NOT the on-disk
    # author file — and it is deleted in the finally after reap (BR-30).
    exec_path = captured["args"][1]
    assert exec_path.endswith("resume-run-resume.py")
    assert not Path(exec_path).exists()


@pytest.mark.asyncio
async def test_resume_reads_inputs_json_and_delivers_verbatim(monkeypatch: pytest.MonkeyPatch):
    """FR-A6/REL-A1: resume reads the journaled inputs_json and delivers it to
    _build_env VERBATIM (byte-identical replay), with NO re-validation."""
    monkeypatch.setattr(script_runner, "_reconcile_orphans", _noop_sweep)
    # The journaled RESOLVED map from the original run.
    journaled = {"topic": "birds", "count": 3}
    workflow_journal.insert_run(
        run_id="run-inputs",
        workflow_name="wf",
        spec_snapshot=json.dumps({"source": "print('x')\n", "path": "/tmp/wf.py"}),
        inputs_json=json.dumps(journaled),
        state="failed",
        started_at="2026-07-08T00:00:00Z",
        tier="script",
        generation="1",
    )
    proc = _FakeProcess(exit_rc=0, stdout=b"CAO_WORKFLOW_OUTPUT:null\n")
    captured = _install_fake_spawn(monkeypatch, proc)

    result = await resume_script_run("run-inputs")
    assert result.state == RunState.COMPLETED
    # The re-delivered CAO_WORKFLOW_INPUTS is the SAME bytes _build_env would emit
    # for the journaled map — deterministic replay (compact json.dumps).
    env = captured["env"]
    assert env["CAO_WORKFLOW_INPUTS"] == json.dumps(journaled, separators=(",", ":"))
    assert env["CAO_WORKFLOW_RESUME"] == "1"


@pytest.mark.asyncio
async def test_resume_malformed_inputs_json_degrades_to_empty(monkeypatch: pytest.MonkeyPatch):
    """A corrupt inputs_json degrades to {} on resume (delivers what it can),
    never aborting the resume — the snapshot source is still valid."""
    monkeypatch.setattr(script_runner, "_reconcile_orphans", _noop_sweep)
    workflow_journal.insert_run(
        run_id="run-badinputs",
        workflow_name="wf",
        spec_snapshot=json.dumps({"source": "print('x')\n", "path": "/tmp/wf.py"}),
        inputs_json="[not, a, dict]",  # non-object -> degrade to {}
        state="failed",
        started_at="2026-07-08T00:00:00Z",
        tier="script",
        generation="1",
    )
    proc = _FakeProcess(exit_rc=0, stdout=b"CAO_WORKFLOW_OUTPUT:null\n")
    captured = _install_fake_spawn(monkeypatch, proc)

    result = await resume_script_run("run-badinputs")
    assert result.state == RunState.COMPLETED
    assert captured["env"]["CAO_WORKFLOW_INPUTS"] == "{}"


# ---------------------------------------------------------------------------
# A5 — orphan sweep off the in-memory step_states (BR-31 5b)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_orphan_sweep_tears_down_in_flight_terminals(monkeypatch: pytest.MonkeyPatch):
    """In-flight step terminals are torn down; a terminal step is left alone."""
    deleted: List[str] = []

    def _fake_delete(terminal_id, registry=None):
        deleted.append(terminal_id)
        return True

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.delete_terminal", _fake_delete
    )
    record = _make_record("run-sweep", process=None, generation="1")
    record.step_states = {
        "s1": StepRunState(step_id="s1", state=StepState.RUNNING, terminal_id="term-1"),
        "s2": StepRunState(step_id="s2", state=StepState.COMPLETED, terminal_id="term-2"),
        "s3": StepRunState(step_id="s3", state=StepState.RUNNING, terminal_id=None),
    }
    from cli_agent_orchestrator.services import workflow_service

    workflow_service.run_registry["run-sweep"] = record

    await script_runner._reconcile_orphans("run-sweep")
    # Only s1 (in-flight + has a terminal) is torn down.
    assert deleted == ["term-1"]


@pytest.mark.asyncio
async def test_orphan_sweep_teardown_failure_never_raises(monkeypatch: pytest.MonkeyPatch):
    """INV-4: a teardown failure is logged, never raised into the drive path."""

    def _boom_delete(terminal_id, registry=None):
        raise RuntimeError("terminal already gone")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.delete_terminal", _boom_delete
    )
    record = _make_record("run-sweep-fail", process=None, generation="1")
    record.step_states = {
        "s1": StepRunState(step_id="s1", state=StepState.RUNNING, terminal_id="term-x"),
    }
    from cli_agent_orchestrator.services import workflow_service

    workflow_service.run_registry["run-sweep-fail"] = record
    # Must not raise.
    await script_runner._reconcile_orphans("run-sweep-fail")
    # F10c: the swallowed teardown failure leaves the record's in-flight step
    # untouched — the sweep never mutates state on a failure, it only logs.
    assert record.step_states["s1"].state == StepState.RUNNING
    assert record.step_states["s1"].terminal_id == "term-x"


# ---------------------------------------------------------------------------
# BR-31 terminal recorder wiring
# ---------------------------------------------------------------------------
def test_terminal_recorder_none_without_script_record():
    """No live ScriptRunRecord -> no recorder (YAML/handoff callers unaffected)."""
    assert make_step_terminal_recorder(None) is None
    assert make_step_terminal_recorder({"CAO_WORKFLOW_RUN_ID": "x"}) is None  # no step id
    # run/step present but no record in the registry.
    assert (
        make_step_terminal_recorder({"CAO_WORKFLOW_RUN_ID": "ghost", "CAO_WORKFLOW_STEP_ID": "s1"})
        is None
    )


def test_terminal_recorder_records_into_step_states():
    """The recorder writes terminal_id into the shared record's step_states (BR-31).

    F9(a) new-StepRunState branch: no ``step_states[step_id]`` entry exists yet,
    so the recorder creates one (RUNNING) before writing ``terminal_id``.
    """
    record = _make_record("run-rec", process=None, generation="1")
    from cli_agent_orchestrator.services import workflow_service

    workflow_service.run_registry["run-rec"] = record
    recorder = make_step_terminal_recorder(
        {"CAO_WORKFLOW_RUN_ID": "run-rec", "CAO_WORKFLOW_STEP_ID": "s1"}
    )
    assert recorder is not None
    recorder("term-created")
    assert record.step_states["s1"].terminal_id == "term-created"
    assert record.step_states["s1"].state == StepState.RUNNING


def test_terminal_recorder_updates_already_present_step_state():
    """F9(a) already-present branch: an existing StepRunState's terminal_id is
    overwritten rather than the entry being replaced."""
    record = _make_record("run-rec2", process=None, generation="1")
    record.step_states["s1"] = StepRunState(
        step_id="s1", state=StepState.RUNNING, attempts=2, terminal_id="term-old"
    )
    from cli_agent_orchestrator.services import workflow_service

    workflow_service.run_registry["run-rec2"] = record
    recorder = make_step_terminal_recorder(
        {"CAO_WORKFLOW_RUN_ID": "run-rec2", "CAO_WORKFLOW_STEP_ID": "s1"}
    )
    assert recorder is not None
    recorder("term-new")
    assert record.step_states["s1"].terminal_id == "term-new"
    assert record.step_states["s1"].attempts == 2  # untouched, only terminal_id updates


def test_terminal_recorder_none_for_non_script_record():
    """F9(c) a live but non-``ScriptRunRecord`` (YAML tier) registry entry -> None."""
    from cli_agent_orchestrator.models.workflow import WorkflowSpec
    from cli_agent_orchestrator.services import workflow_service
    from cli_agent_orchestrator.services.workflow_service import RunRecord

    yaml_record = RunRecord(
        run_id="run-yaml",
        workflow_name="wf",
        spec=WorkflowSpec.model_validate(
            {
                "name": "wf",
                "version": "1",
                "mode": "sequential",
                "steps": [
                    {"id": "s1", "provider": "kiro_cli", "agent": "developer", "prompt": "do it"}
                ],
            }
        ),
        inputs={},
        state=RunState.RUNNING,
        cancelled=False,
        current_step_id=None,
        step_states={},
        started_at="2026-07-08T00:00:00Z",
        finished_at=None,
    )
    workflow_service.run_registry["run-yaml"] = yaml_record
    assert (
        make_step_terminal_recorder(
            {"CAO_WORKFLOW_RUN_ID": "run-yaml", "CAO_WORKFLOW_STEP_ID": "s1"}
        )
        is None
    )


# ---------------------------------------------------------------------------
# Bug 2 — record_step_completion (RUNNING -> COMPLETED/FAILED transition)
# ---------------------------------------------------------------------------
def _kw(run_id: str, step_id: str) -> dict:
    """The run/step env a genuine script run-step call carries."""
    return {"CAO_WORKFLOW_RUN_ID": run_id, "CAO_WORKFLOW_STEP_ID": step_id}


def test_step_call_fingerprint_stable_and_field_separated():
    """The fingerprint is deterministic and its fields cannot collide across the
    boundary (``a|b`` must differ from ``ab|``)."""
    fp1 = _step_call_fingerprint("kiro_cli", "dev", "go")
    fp2 = _step_call_fingerprint("kiro_cli", "dev", "go")
    assert fp1 == fp2 and len(fp1) == 64  # sha256 hexdigest
    # A field-boundary shift must change the digest (NUL separation).
    assert _step_call_fingerprint("a", "b", "c") != _step_call_fingerprint("ab", "", "c")


def test_completion_guard_none_without_script_record():
    """Same BR-31 guard as the recorder: no run/step env or no live script
    record -> None (YAML/handoff callers untouched)."""
    assert record_step_completion(None, provider="p", agent="a", prompt="x") is None
    assert (
        record_step_completion({"CAO_WORKFLOW_RUN_ID": "x"}, provider="p", agent="a", prompt="x")
        is None
    )
    # run/step present but no record in the registry.
    assert record_step_completion(_kw("ghost", "s1"), provider="p", agent="a", prompt="x") is None


def test_completion_transitions_running_to_completed(_patched_journal):
    """Happy path: a settle with no error flips a seeded RUNNING step to COMPLETED,
    bumps attempts, and journals the completed row (so resume can replay it)."""
    from cli_agent_orchestrator.services import workflow_service

    record = _make_record("run-c", process=None, generation="1")
    record.step_states["s1"] = StepRunState(
        step_id="s1", state=StepState.RUNNING, terminal_id="term-1"
    )
    workflow_service.run_registry["run-c"] = record

    settle = record_step_completion(
        _kw("run-c", "s1"), provider="kiro_cli", agent="dev", prompt="go"
    )
    assert settle is not None
    settle("term-1", None)

    st = record.step_states["s1"]
    assert st.state == StepState.COMPLETED
    assert st.attempts == 1
    assert st.error is None
    # The completed step is written through to the journal with a stable
    # fingerprint AND its attempts persisted (so a resume's lookup_replay /
    # rebuild sees the real state, not append_step's hardcoded attempts=0).
    row = workflow_journal.get_step("run-c", "s1")
    assert row is not None
    assert row.state == "completed"
    assert row.attempts == 1
    assert row.call_fingerprint == _step_call_fingerprint("kiro_cli", "dev", "go")


def test_completion_adopts_validated_structured_output(_patched_journal):
    """A worker that returned schema-valid output via workflow_return has that
    output copied onto the step state and settles COMPLETED."""
    from cli_agent_orchestrator.services import workflow_service
    from cli_agent_orchestrator.services.step_output_store import record_step_output

    record = _make_record("run-out", process=None, generation="1")
    record.step_states["s1"] = StepRunState(step_id="s1", state=StepState.RUNNING)
    workflow_service.run_registry["run-out"] = record
    # Worker emitted a valid output (no schema -> validated=True).
    record_step_output("run-out", "s1", {"answer": 42})

    settle = record_step_completion(_kw("run-out", "s1"), provider="p", agent="a", prompt="go")
    settle("term-1", None)

    st = record.step_states["s1"]
    assert st.state == StepState.COMPLETED
    assert st.output is not None and st.output.output == {"answer": 42}
    # The structured output round-trips to the durable row (not NULL), preserving
    # complete status history and the reserved lookup_replay primitive's data.
    row = workflow_journal.get_step("run-out", "s1")
    assert row is not None and row.output_json is not None
    assert json.loads(row.output_json) == {"answer": 42}


def test_completion_unvalidated_output_settles_completed_unvalidated(_patched_journal):
    """Edge case: a present-but-schema-invalid output settles
    COMPLETED_UNVALIDATED (missing==invalid), mirroring the YAML tier."""
    from cli_agent_orchestrator.services import workflow_service
    from cli_agent_orchestrator.services.step_output_store import record_step_output

    record = _make_record("run-inv", process=None, generation="1")
    record.step_states["s1"] = StepRunState(step_id="s1", state=StepState.RUNNING)
    workflow_service.run_registry["run-inv"] = record
    # Output fails its schema -> validated=False, state COMPLETED_UNVALIDATED.
    record_step_output(
        "run-inv",
        "s1",
        {"answer": "not-an-int"},
        {"type": "object", "properties": {"answer": {"type": "integer"}}, "required": ["answer"]},
    )

    settle = record_step_completion(_kw("run-inv", "s1"), provider="p", agent="a", prompt="go")
    settle("term-1", None)

    st = record.step_states["s1"]
    assert st.state == StepState.COMPLETED_UNVALIDATED
    assert st.output is not None and st.output.validated is False


def test_completion_error_transitions_running_to_failed(_patched_journal):
    """Edge case: a settle carrying an error flips the step to FAILED, records the
    error string, and journals FAILED."""
    from cli_agent_orchestrator.services import workflow_service

    record = _make_record("run-f", process=None, generation="1")
    record.step_states["s1"] = StepRunState(step_id="s1", state=StepState.RUNNING)
    workflow_service.run_registry["run-f"] = record

    settle = record_step_completion(_kw("run-f", "s1"), provider="p", agent="a", prompt="go")
    settle("term-1", "terminal term-1 reached ERROR status")

    st = record.step_states["s1"]
    assert st.state == StepState.FAILED
    assert st.error == "terminal term-1 reached ERROR status"
    row = workflow_journal.get_step("run-f", "s1")
    assert row is not None and row.state == "failed"
    # The error string persists durably (append_step hardcodes error=NULL; the
    # update_step second write is what carries it).
    assert row.error == "terminal term-1 reached ERROR status"


def test_completion_creates_step_state_when_missing(_patched_journal):
    """Edge case: if no prior RUNNING seed exists (the terminal-created callback
    never fired), the settle still records the transition rather than KeyError-ing."""
    from cli_agent_orchestrator.services import workflow_service

    record = _make_record("run-m", process=None, generation="1")  # empty step_states
    workflow_service.run_registry["run-m"] = record

    settle = record_step_completion(_kw("run-m", "s1"), provider="p", agent="a", prompt="go")
    settle(None, None)

    assert "s1" in record.step_states
    assert record.step_states["s1"].state == StepState.COMPLETED
    assert record.step_states["s1"].attempts == 1


def test_completion_journal_failure_never_raises(monkeypatch, _patched_journal):
    """INV-4: a journal write failure during settle is swallowed — the in-memory
    transition still lands, the call never raises."""
    from cli_agent_orchestrator.services import workflow_service

    record = _make_record("run-j", process=None, generation="1")
    record.step_states["s1"] = StepRunState(step_id="s1", state=StepState.RUNNING)
    workflow_service.run_registry["run-j"] = record

    def _boom(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(workflow_journal, "append_step", _boom)

    settle = record_step_completion(_kw("run-j", "s1"), provider="p", agent="a", prompt="go")
    settle("term-1", None)  # must not raise
    # In-memory transition still applied despite the journal write failing.
    assert record.step_states["s1"].state == StepState.COMPLETED


def test_get_run_status_script_cold_miss_uses_journal_row_without_yaml_rebuild(
    monkeypatch: pytest.MonkeyPatch,
):
    """A journal-only script run returns its row state and no reconstructed steps."""
    from cli_agent_orchestrator.services import workflow_service

    _seed_script_run("run-cold", state="failed", generation="3")

    def _must_not_rebuild(run_id):
        raise AssertionError("script status must not use the YAML journal rebuild")

    monkeypatch.setattr(workflow_service, "_rebuild_record_from_journal", _must_not_rebuild)

    status = workflow_service.get_run_status("run-cold")

    assert status.run_id == "run-cold"
    assert status.state == RunState.FAILED
    assert status.current_step_id is None
    assert status.steps == []
    assert "run-cold" not in workflow_service.run_registry


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _noop_sweep(run_id):
    return None


def _make_record(run_id: str, *, process, generation: str) -> ScriptRunRecord:
    return ScriptRunRecord(
        run_id=run_id,
        workflow_name="wf",
        state=RunState.RUNNING,
        cancelled=False,
        current_step_id=None,
        step_states={},
        process=process,
        generation=generation,
        started_at="2026-07-08T00:00:00Z",
        finished_at=None,
        tier="script",
    )


def _seed_script_run(run_id: str, *, state: str = "running", generation: str = "1") -> None:
    workflow_journal.insert_run(
        run_id=run_id,
        workflow_name="wf",
        spec_snapshot=json.dumps({"source": "print('x')\n", "path": "/tmp/wf.py"}),
        inputs_json="{}",
        state=state,
        started_at="2026-07-08T00:00:00Z",
        tier="script",
        generation=generation,
    )
