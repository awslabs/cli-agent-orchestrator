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
    cancel_script_run,
    make_step_terminal_recorder,
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
    order and settle the returncode so ``_terminate``'s reap completes.
    """

    def __init__(
        self,
        *,
        exit_rc: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        hang: bool = False,
    ):
        self._exit_rc = exit_rc
        self.returncode: Optional[int] = None
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)
        self._hang = hang
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
    # Constructed env is the exact 5-key allowlist (INV-2), no resume flag.
    env = captured["env"]
    assert set(env) == {
        "CAO_WORKFLOW_RUN_ID",
        "CAO_WORKFLOW_GENERATION",
        "CAO_API_BASE_URL",
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


@pytest.mark.asyncio
async def test_timeout_bound_helper_raises():
    """_await_exit_within_bound converts the elapsed bound into TimeoutBound."""
    proc = _FakeProcess(exit_rc=0, hang=True)
    with pytest.raises(TimeoutBound):
        await script_runner._await_exit_within_bound(proc, timeout=0.02)


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
    """The recorder writes terminal_id into the shared record's step_states (BR-31)."""
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
