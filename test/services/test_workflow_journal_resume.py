"""Tests for the durable run journal + resume (issue #312, Bolt 4 / N6).

Covers the load-bearing behavior from ``functional-design/business-logic-model.md``
(§1 write-through, §2 rebuild, §3 resume) and ``business-rules.md`` (B4-BR-*):

- write-through persists the run + step states across transitions (§1)
- ``_rebuild_record_from_journal`` reconstructs a RunRecord with the EXACT shipped
  field binding and seeds ALL spec steps PENDING then overlays journal rows (§2/F8)
- ``get_run_status`` rebuilds on a cache miss + re-populates; KeyError on absent (F1)
- resume kill-after-step-2 demo: steps 1-2 skipped, 3-4 re-run, run COMPLETED (§4)
- resume liveness guard rejects a live RUNNING run (B4-BR-7a -> 409)
- resume of COMPLETED/CANCELLED -> ResumeNotAllowedError; unknown -> KeyError;
  corrupt snapshot -> ResumeCorruptError (§5)
- a best-effort journal write failure does NOT break the live run (B4-BR-5)

``run_agent_step`` and ``step_output_store`` are mocked — no real terminals. The
journal points at a temp SQLite DB via the patched DATABASE_FILE; the two migrators
create the tables in a fixture.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import List
from unittest.mock import AsyncMock

import pytest

from cli_agent_orchestrator.clients.database import (
    _migrate_workflow_run,
    _migrate_workflow_run_step,
)
from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus
from cli_agent_orchestrator.models.workflow import (
    RunState,
    StepState,
    WorkflowSpec,
    WorkflowStep,
)
from cli_agent_orchestrator.models.workflow_runtime import StepOutputRecord
from cli_agent_orchestrator.services import workflow_journal
from cli_agent_orchestrator.services import workflow_service as ws

_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


@pytest.fixture(autouse=True)
def _patched_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the journal at a temp DB, create the tables, clean the registry."""
    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    ws.run_registry.clear()
    ws._active_drives.clear()
    ws.step_output_store._store.clear()
    yield db_path
    ws.run_registry.clear()
    ws._active_drives.clear()
    ws.step_output_store._store.clear()


def _ok(terminal_id: str = "t1") -> AgentStepResult:
    return AgentStepResult(
        terminal_id=terminal_id, last_message="done", status=TerminalStatus.COMPLETED
    )


def _step(step_id: str, *, schema=None) -> WorkflowStep:
    return WorkflowStep(
        id=step_id,
        provider="claude_code",
        agent="dev",
        prompt="go",
        output_schema=schema,
    )


def _spec(name: str = "wf", *, step_ids=("s1",), schema=None) -> WorkflowSpec:
    return WorkflowSpec(
        name=name,
        mode="sequential",
        steps=[_step(sid, schema=schema) for sid in step_ids],
    )


def _put_valid(run_id: str, step_id: str) -> None:
    ws.step_output_store.put(
        run_id,
        step_id,
        StepOutputRecord(
            run_id=run_id,
            step_id=step_id,
            output={"answer": "42"},
            validated=True,
            errors=[],
            state=StepState.COMPLETED,
        ),
    )


# ---------------------------------------------------------------------------
# §1 — write-through
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_write_through_persists_run_and_steps(monkeypatch, _patched_journal):
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    spec = _spec(step_ids=("s1", "s2"))
    await ws.start_run(spec, {}, "runWT")

    row = workflow_journal.get_run("runWT")
    assert row is not None
    assert row.workflow_name == "wf"
    assert row.state == RunState.COMPLETED.value
    assert row.finished_at is not None
    assert row.current_step_id is None  # cleared on finalize

    steps = {s.step_id: s for s in workflow_journal.get_steps("runWT")}
    assert set(steps) == {"s1", "s2"}
    assert steps["s1"].state == StepState.COMPLETED.value
    assert steps["s2"].state == StepState.COMPLETED.value


@pytest.mark.asyncio
async def test_write_through_persists_output_for_validated_step(monkeypatch, _patched_journal):
    async def _side(**kwargs):
        _put_valid(
            kwargs["env_vars"]["CAO_WORKFLOW_RUN_ID"], kwargs["env_vars"]["CAO_WORKFLOW_STEP_ID"]
        )
        return _ok()

    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))
    await ws.start_run(_spec(schema=_SCHEMA), {}, "runOut")

    steps = workflow_journal.get_steps("runOut")
    assert steps[0].state == StepState.COMPLETED.value
    assert steps[0].output_json is not None and "42" in steps[0].output_json


@pytest.mark.asyncio
async def test_best_effort_write_failure_does_not_break_run(monkeypatch, _patched_journal):
    # B4-BR-5: a journal write that raises must NOT propagate into the drive loop.
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    monkeypatch.setattr(
        ws.workflow_journal,
        "update_step",
        lambda *a, **k: (_ for _ in ()).throw(sqlite3.Error("boom")),
    )
    res = await ws.start_run(_spec(step_ids=("s1", "s2")), {}, "runFail")
    # The live run still completes on the in-memory floor.
    assert res.state == RunState.COMPLETED
    assert [s.state for s in res.steps] == [StepState.COMPLETED, StepState.COMPLETED]


# ---------------------------------------------------------------------------
# §2 — rebuild + get_run_status rebuild-on-miss
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rebuild_reconstructs_record_with_field_binding(monkeypatch, _patched_journal):
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    spec = _spec(name="myflow", step_ids=("s1", "s2"))
    await ws.start_run(spec, {}, "runRB")

    ws.run_registry.clear()  # simulate a cold process
    record = ws._rebuild_record_from_journal("runRB")
    assert record is not None
    assert record.run_id == "runRB"
    assert record.workflow_name == "myflow"  # F2: not the spec in this slot
    assert record.started_at  # F2: restored
    assert record.finished_at is not None  # F2: restored
    assert record.state == RunState.COMPLETED
    # F8: every spec step is present in step_states.
    assert set(record.step_states) == {"s1", "s2"}


def test_rebuild_seeds_all_spec_steps_even_when_journal_partial(_patched_journal):
    # F8 / B4-RD-3: a partially-written step set must not leave a step missing.
    spec = _spec(step_ids=("s1", "s2", "s3"))
    workflow_journal.insert_run(
        "runPart",
        spec.name,
        spec.model_dump_json(),
        "{}",
        RunState.RUNNING.value,
        "2026-01-01T00:00:00Z",
    )
    # Only s1 has a journal row (simulating a dropped insert for s2/s3).
    workflow_journal.insert_steps(
        "runPart", [("s1", StepState.COMPLETED.value)], "2026-01-01T00:00:01Z"
    )

    record = ws._rebuild_record_from_journal("runPart")
    assert record is not None
    assert set(record.step_states) == {"s1", "s2", "s3"}
    assert record.step_states["s1"].state == StepState.COMPLETED
    assert record.step_states["s2"].state == StepState.PENDING
    assert record.step_states["s3"].state == StepState.PENDING


@pytest.mark.asyncio
async def test_get_run_status_rebuilds_on_cache_miss(monkeypatch, _patched_journal):
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    await ws.start_run(_spec(step_ids=("s1", "s2")), {}, "runMiss")

    ws.run_registry.clear()  # cold read
    snap = ws.get_run_status("runMiss")
    assert snap.run_id == "runMiss"
    assert snap.state == RunState.COMPLETED
    assert {s.id for s in snap.steps} == {"s1", "s2"}
    # The cache was re-populated.
    assert "runMiss" in ws.run_registry


def test_get_run_status_keyerror_on_truly_absent(_patched_journal):
    # F1: absent from BOTH cache AND journal -> KeyError (contract unchanged).
    with pytest.raises(KeyError):
        ws.get_run_status("nope")


def test_rebuild_returns_none_on_absent(_patched_journal):
    assert ws._rebuild_record_from_journal("ghost") is None


def test_get_run_status_corrupt_run_row_degrades_to_keyerror(_patched_journal):
    # A corrupt spec_snapshot / inputs_json must degrade to "absent" (B4-RD-4):
    # get_run_status raises KeyError -> 404, never ValidationError/JSONDecodeError.
    workflow_journal.insert_run(
        "runCorrupt",
        "wf",
        "{not a valid spec",  # corrupt spec_snapshot
        "also not json",  # corrupt inputs_json
        RunState.FAILED.value,
        "2026-01-01T00:00:00Z",
    )
    with pytest.raises(KeyError):
        ws.get_run_status("runCorrupt")


def test_rebuild_skips_bad_state_and_ghost_step_rows(_patched_journal):
    # One bad step row must never abort the rebuild: a bogus ``state`` leaves the
    # seeded PENDING default in place; a step_id absent from the spec is dropped.
    spec = _spec(step_ids=("s1", "s2"))
    workflow_journal.insert_run(
        "runRows",
        spec.name,
        spec.model_dump_json(),
        "{}",
        RunState.RUNNING.value,
        "2026-01-01T00:00:00Z",
    )
    workflow_journal.insert_steps(
        "runRows",
        [("s1", StepState.COMPLETED.value), ("s2", StepState.PENDING.value)],
        "2026-01-01T00:00:00Z",
    )
    # s2 gets an invalid state value; a ghost step not in the spec also exists.
    workflow_journal.update_step("runRows", "s2", "BOGUS", 1, "2026-01-01T00:00:01Z")
    workflow_journal.insert_steps(
        "runRows", [("ghost", StepState.COMPLETED.value)], "2026-01-01T00:00:02Z"
    )

    record = ws._rebuild_record_from_journal("runRows")
    assert record is not None
    assert set(record.step_states) == {"s1", "s2"}  # ghost dropped
    assert record.step_states["s1"].state == StepState.COMPLETED
    assert record.step_states["s2"].state == StepState.PENDING  # seeded default kept


# ---------------------------------------------------------------------------
# §3/§4 — resume
# ---------------------------------------------------------------------------
def _seed_killed_after_step2(spec: WorkflowSpec, run_id: str) -> None:
    """Simulate a process that died after step 2 completed (the §4 demo)."""
    workflow_journal.insert_run(
        run_id,
        spec.name,
        spec.model_dump_json(),
        "{}",
        RunState.RUNNING.value,  # crashed mid-run, never settled
        "2026-01-01T00:00:00Z",
    )
    workflow_journal.insert_steps(
        run_id, [(s.id, StepState.PENDING.value) for s in spec.steps], "2026-01-01T00:00:00Z"
    )
    workflow_journal.update_step(run_id, "s1", StepState.COMPLETED.value, 1, "2026-01-01T00:00:01Z")
    workflow_journal.update_step(run_id, "s2", StepState.COMPLETED.value, 1, "2026-01-01T00:00:02Z")
    workflow_journal.update_step(run_id, "s3", StepState.RUNNING.value, 1, "2026-01-01T00:00:03Z")
    workflow_journal.update_run_current_step(run_id, "s3")


@pytest.mark.asyncio
async def test_resume_kill_after_step2_demo(monkeypatch, _patched_journal):
    spec = _spec(step_ids=("s1", "s2", "s3", "s4"))
    _seed_killed_after_step2(spec, "runK")
    # No live record in the registry -> a crash remnant, resumable.
    assert "runK" not in ws.run_registry

    ran: List[str] = []

    async def _side(**kwargs):
        ran.append(kwargs["env_vars"]["CAO_WORKFLOW_STEP_ID"])
        return _ok()

    mock = AsyncMock(side_effect=_side)
    monkeypatch.setattr(ws, "run_agent_step", mock)

    res = await ws.resume_from_last_completed("runK")

    # Steps 1-2 are NOT re-executed; only 3-4 re-run.
    assert ran == ["s3", "s4"]
    assert res.state == RunState.COMPLETED
    states = {s.id: s.state for s in res.steps}
    assert states == {
        "s1": StepState.COMPLETED,
        "s2": StepState.COMPLETED,
        "s3": StepState.COMPLETED,
        "s4": StepState.COMPLETED,
    }
    # The journal now shows the run settled COMPLETED with finished_at.
    row = workflow_journal.get_run("runK")
    assert row.state == RunState.COMPLETED.value
    assert row.finished_at is not None


@pytest.mark.asyncio
async def test_resume_failed_run_reruns_failed_step(monkeypatch, _patched_journal):
    spec = _spec(step_ids=("s1", "s2"))
    workflow_journal.insert_run(
        "runFa",
        spec.name,
        spec.model_dump_json(),
        "{}",
        RunState.FAILED.value,
        "2026-01-01T00:00:00Z",
    )
    workflow_journal.insert_steps(
        "runFa", [(s.id, StepState.PENDING.value) for s in spec.steps], "2026-01-01T00:00:00Z"
    )
    workflow_journal.update_step("runFa", "s1", StepState.COMPLETED.value, 1, "t")
    workflow_journal.update_step("runFa", "s2", StepState.FAILED.value, 4, "t", error="boom")
    workflow_journal.update_run_state("runFa", RunState.FAILED.value, "2026-01-01T00:00:05Z")

    ran: List[str] = []

    async def _side(**kwargs):
        ran.append(kwargs["env_vars"]["CAO_WORKFLOW_STEP_ID"])
        return _ok()

    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))
    res = await ws.resume_from_last_completed("runFa")
    assert ran == ["s2"]  # s1 kept, s2 (failed) re-run
    assert res.state == RunState.COMPLETED


@pytest.mark.asyncio
async def test_resume_reopen_persists_cleared_current_step(monkeypatch, _patched_journal):
    # The reopen block clears current_step_id in memory AND persists it — the
    # durable row must not stay stale until the first resumed step journals.
    spec = _spec(step_ids=("s1", "s2"))
    workflow_journal.insert_run(
        "runCur",
        spec.name,
        spec.model_dump_json(),
        "{}",
        RunState.FAILED.value,
        "2026-01-01T00:00:00Z",
    )
    workflow_journal.insert_steps(
        "runCur", [(s.id, StepState.PENDING.value) for s in spec.steps], "2026-01-01T00:00:00Z"
    )
    workflow_journal.update_step("runCur", "s1", StepState.COMPLETED.value, 1, "t")
    workflow_journal.update_step("runCur", "s2", StepState.FAILED.value, 4, "t", error="boom")
    workflow_journal.update_run_current_step("runCur", "s2")  # stale pointer
    workflow_journal.update_run_state("runCur", RunState.FAILED.value, "2026-01-01T00:00:05Z")
    assert workflow_journal.get_run("runCur").current_step_id == "s2"

    # Stub the drive so the journal row is observed right after the reopen block,
    # before any step executes.
    monkeypatch.setattr(ws, "_drive", AsyncMock(return_value=None))
    await ws.resume_from_last_completed("runCur")

    assert workflow_journal.get_run("runCur").current_step_id is None


@pytest.mark.asyncio
async def test_resume_liveness_guard_rejects_live_run(_patched_journal):
    # B4-BR-7a: a run whose drive loop is executing IN THIS PROCESS cannot be
    # resumed. Liveness is _active_drives membership, not the cached record state.
    spec = _spec(step_ids=("s1",))
    ws.run_registry["runLive"] = ws.RunRecord(
        run_id="runLive",
        workflow_name="wf",
        spec=spec,
        inputs={},
        state=RunState.RUNNING,
    )
    ws._active_drives.add("runLive")
    workflow_journal.insert_run(
        "runLive", "wf", spec.model_dump_json(), "{}", RunState.RUNNING.value, "t"
    )
    with pytest.raises(ws.ResumeNotAllowedError):
        await ws.resume_from_last_completed("runLive")


@pytest.mark.asyncio
async def test_concurrent_resumes_only_one_drives(monkeypatch, _patched_journal):
    # Regression (reviewer BLOCKER): the reopen journal writes await BEFORE the
    # drive is marked live; a second resume arriving in that yield window used to
    # pass the liveness guard (step 4 deliberately accepts a RUNNING crash
    # remnant) and double-drive the run (B4-BR-7a). The liveness mark must be set
    # before the first await in the reopen block.
    spec = _spec(step_ids=("s1", "s2", "s3", "s4"))
    _seed_killed_after_step2(spec, "runRace")
    assert "runRace" not in ws.run_registry

    ran: List[str] = []

    async def _side(**kwargs):
        ran.append(kwargs["env_vars"]["CAO_WORKFLOW_STEP_ID"])
        await asyncio.sleep(0)  # yield so the concurrent resume interleaves
        return _ok()

    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))

    # Force the reopen journal write off-loop path to yield too, widening the
    # pre-fix race window between registration and the liveness mark.
    async def _yielding_to_thread(fn, *args, **kwargs):
        await asyncio.sleep(0)
        return fn(*args, **kwargs)

    monkeypatch.setattr(ws.asyncio, "to_thread", _yielding_to_thread)

    results = await asyncio.gather(
        ws.resume_from_last_completed("runRace"),
        ws.resume_from_last_completed("runRace"),
        return_exceptions=True,
    )

    oks = [r for r in results if not isinstance(r, BaseException)]
    errs = [r for r in results if isinstance(r, BaseException)]
    assert len(oks) == 1, f"exactly one resume must win, got results={results}"
    assert len(errs) == 1 and isinstance(errs[0], ws.ResumeNotAllowedError)
    assert oks[0].state == RunState.COMPLETED
    # The steps were driven exactly once — no double-drive.
    assert ran == ["s3", "s4"]
    assert "runRace" not in ws._active_drives


@pytest.mark.asyncio
async def test_status_then_resume_of_crash_remnant_succeeds(monkeypatch, _patched_journal):
    # Regression (reviewer BLOCKER): a status read of a crash remnant rebuilds a
    # RUNNING record into the cache; that cached RUNNING state must NOT trip the
    # resume liveness guard — nothing is actually executing.
    spec = _spec(step_ids=("s1", "s2", "s3", "s4"))
    _seed_killed_after_step2(spec, "runSR")
    assert "runSR" not in ws.run_registry

    # Status FIRST: rebuilds + caches the remnant as RUNNING.
    snap = ws.get_run_status("runSR")
    assert snap.state == RunState.RUNNING
    assert "runSR" in ws.run_registry

    ran: List[str] = []

    async def _side(**kwargs):
        ran.append(kwargs["env_vars"]["CAO_WORKFLOW_STEP_ID"])
        return _ok()

    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))
    res = await ws.resume_from_last_completed("runSR")
    assert ran == ["s3", "s4"]
    assert res.state == RunState.COMPLETED


@pytest.mark.asyncio
async def test_active_drive_mark_cleared_after_run_and_resume(monkeypatch, _patched_journal):
    # The liveness mark must not leak: cleared after a normal run AND a resume.
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    await ws.start_run(_spec(step_ids=("s1",)), {}, "runMark")
    assert "runMark" not in ws._active_drives

    spec = _spec(step_ids=("s1", "s2", "s3", "s4"))
    _seed_killed_after_step2(spec, "runMark2")
    await ws.resume_from_last_completed("runMark2")
    assert "runMark2" not in ws._active_drives


@pytest.mark.asyncio
async def test_resume_completed_run_rejected(_patched_journal):
    spec = _spec(step_ids=("s1",))
    workflow_journal.insert_run(
        "runDone", "wf", spec.model_dump_json(), "{}", RunState.COMPLETED.value, "t"
    )
    with pytest.raises(ws.ResumeNotAllowedError):
        await ws.resume_from_last_completed("runDone")


@pytest.mark.asyncio
async def test_resume_cancelled_run_rejected(_patched_journal):
    spec = _spec(step_ids=("s1",))
    workflow_journal.insert_run(
        "runCx", "wf", spec.model_dump_json(), "{}", RunState.CANCELLED.value, "t"
    )
    with pytest.raises(ws.ResumeNotAllowedError):
        await ws.resume_from_last_completed("runCx")


@pytest.mark.asyncio
async def test_resume_unknown_run_keyerror(_patched_journal):
    with pytest.raises(KeyError):
        await ws.resume_from_last_completed("ghost")


@pytest.mark.asyncio
async def test_resume_bad_run_id_valueerror(_patched_journal):
    with pytest.raises(ValueError):
        await ws.resume_from_last_completed("../etc")


@pytest.mark.asyncio
async def test_resume_corrupt_snapshot_raises_corrupt(_patched_journal):
    # A snapshot that won't deserialize -> ResumeCorruptError -> 422.
    workflow_journal.insert_run(
        "runBad", "wf", "{not valid json spec", "{}", RunState.FAILED.value, "t"
    )
    workflow_journal.insert_steps("runBad", [("s1", StepState.FAILED.value)], "t")
    with pytest.raises(ws.ResumeCorruptError):
        await ws.resume_from_last_completed("runBad")


@pytest.mark.asyncio
async def test_start_run_rejects_journaled_run_id_and_keeps_row(monkeypatch, _patched_journal):
    # A durable journal row claims the run_id even after a restart (empty
    # registry): start_run must 409 (KeyError) and must NOT clobber the row.
    spec = _spec(name="oldflow", step_ids=("s1",))
    workflow_journal.insert_run(
        "runReuse",
        spec.name,
        spec.model_dump_json(),
        "{}",
        RunState.FAILED.value,
        "2026-01-01T00:00:00Z",
    )
    original_snapshot = workflow_journal.get_run("runReuse").spec_snapshot
    assert "runReuse" not in ws.run_registry  # fresh process

    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    with pytest.raises(KeyError):
        await ws.start_run(_spec(name="newflow", step_ids=("x1",)), {}, "runReuse")

    # The journal row is untouched — no INSERT OR REPLACE clobber.
    assert workflow_journal.get_run("runReuse").spec_snapshot == original_snapshot


@pytest.mark.asyncio
async def test_resume_templates_from_rebuilt_kept_step_output(monkeypatch, _patched_journal):
    # A PENDING step whose prompt references {{steps.s1.output.answer}} must be
    # substituted from the REBUILT kept step's persisted output in a fresh process.
    spec = WorkflowSpec(
        name="wf",
        mode="sequential",
        steps=[
            _step("s1", schema=_SCHEMA),
            WorkflowStep(
                id="s2",
                provider="claude_code",
                agent="dev",
                prompt="use {{steps.s1.output.answer}} please",
            ),
        ],
    )
    workflow_journal.insert_run(
        "runTpl",
        spec.name,
        spec.model_dump_json(),
        "{}",
        RunState.RUNNING.value,
        "2026-01-01T00:00:00Z",
    )
    workflow_journal.insert_steps(
        "runTpl", [(s.id, StepState.PENDING.value) for s in spec.steps], "2026-01-01T00:00:00Z"
    )
    workflow_journal.update_step(
        "runTpl",
        "s1",
        StepState.COMPLETED.value,
        1,
        "2026-01-01T00:00:01Z",
        output_json='{"answer": "42"}',
    )
    assert "runTpl" not in ws.run_registry  # fresh process — rebuild from journal

    prompts: List[str] = []

    async def _side(**kwargs):
        prompts.append(kwargs["prompt"])
        return _ok()

    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))
    res = await ws.resume_from_last_completed("runTpl")
    assert res.state == RunState.COMPLETED
    assert prompts == ["use 42 please"]


@pytest.mark.asyncio
async def test_resume_corrupt_state_raises_corrupt_not_bare_valueerror(_patched_journal):
    # FIX-4: a corrupt (non-enum) state string in the durable row must map to
    # ResumeCorruptError (-> 422, consistent with corrupt-snapshot handling),
    # never a bare ValueError that the boundary would mislabel as a 400.
    spec = _spec(step_ids=("s1",))
    workflow_journal.insert_run(
        "runBadState", "wf", spec.model_dump_json(), "{}", "BOGUS_STATE", "t"
    )
    with pytest.raises(ws.ResumeCorruptError, match="corrupt state"):
        await ws.resume_from_last_completed("runBadState")


def test_get_run_status_non_dict_inputs_json_degrades_to_keyerror(_patched_journal):
    # FIX-5: "null" parses as JSON without raising but leaves record.inputs
    # non-dict; the rebuild must treat it as a corrupt row -> absent -> KeyError.
    spec = _spec(step_ids=("s1",))
    workflow_journal.insert_run(
        "runNullIn",
        spec.name,
        spec.model_dump_json(),
        "null",  # valid JSON, not an object
        RunState.FAILED.value,
        "2026-01-01T00:00:00Z",
    )
    with pytest.raises(KeyError):
        ws.get_run_status("runNullIn")


@pytest.mark.asyncio
async def test_resume_non_dict_inputs_json_maps_to_keyerror(_patched_journal):
    # The RESUME path: the row exists but the rebuild degrades the corrupt
    # inputs_json to absent -> the defensive branch raises KeyError -> 404.
    spec = _spec(step_ids=("s1",))
    workflow_journal.insert_run(
        "runArrIn",
        spec.name,
        spec.model_dump_json(),
        "[1, 2]",  # valid JSON array, not an object
        RunState.FAILED.value,
        "2026-01-01T00:00:00Z",
    )
    with pytest.raises(KeyError):
        await ws.resume_from_last_completed("runArrIn")


@pytest.mark.asyncio
async def test_resume_engine_error_settles_failed_and_clears_drive_mark(
    monkeypatch, _patched_journal
):
    # FIX-3a: drive resume through _drive's WorkflowEngineError branch (a PENDING
    # step whose prompt references a step that never produced output). Assert the
    # run settles FAILED with finished_at, the journal carries the terminal state,
    # the exception propagates, and the liveness mark is cleared.
    spec = WorkflowSpec(
        name="wf",
        mode="sequential",
        steps=[
            _step("s1"),
            WorkflowStep(
                id="s2",
                provider="claude_code",
                agent="dev",
                prompt="use {{steps.ghost.output.x}}",
            ),
        ],
    )
    workflow_journal.insert_run(
        "runEngErr",
        spec.name,
        spec.model_dump_json(),
        "{}",
        RunState.FAILED.value,
        "2026-01-01T00:00:00Z",
    )
    workflow_journal.insert_steps(
        "runEngErr", [(s.id, StepState.PENDING.value) for s in spec.steps], "t"
    )
    workflow_journal.update_step("runEngErr", "s1", StepState.COMPLETED.value, 1, "t")
    workflow_journal.update_step("runEngErr", "s2", StepState.FAILED.value, 4, "t", error="boom")
    workflow_journal.update_run_state("runEngErr", RunState.FAILED.value, "t2")

    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    with pytest.raises(ws.WorkflowEngineError, match="produced no output"):
        await ws.resume_from_last_completed("runEngErr")

    rec = ws.run_registry["runEngErr"]
    assert rec.state == RunState.FAILED
    assert rec.finished_at is not None
    row = workflow_journal.get_run("runEngErr")
    assert row.state == RunState.FAILED.value
    assert row.finished_at is not None
    assert "runEngErr" not in ws._active_drives


@pytest.mark.asyncio
async def test_resume_mixed_on_failure_continue_run(monkeypatch, _patched_journal):
    # FIX-6: a mixed on_failure=continue run — COMPLETED s1 is kept, FAILED s2 and
    # PENDING s3 re-run on resume.
    spec = WorkflowSpec(
        name="wf",
        mode="sequential",
        steps=[
            _step("s1"),
            WorkflowStep(
                id="s2",
                provider="claude_code",
                agent="dev",
                prompt="go",
                on_failure="continue",
            ),
            _step("s3"),
        ],
    )
    workflow_journal.insert_run(
        "runMix",
        spec.name,
        spec.model_dump_json(),
        "{}",
        RunState.RUNNING.value,  # crashed mid-run
        "2026-01-01T00:00:00Z",
    )
    workflow_journal.insert_steps(
        "runMix", [(s.id, StepState.PENDING.value) for s in spec.steps], "t"
    )
    workflow_journal.update_step("runMix", "s1", StepState.COMPLETED.value, 1, "t")
    workflow_journal.update_step("runMix", "s2", StepState.FAILED.value, 4, "t", error="boom")

    ran: List[str] = []

    async def _side(**kwargs):
        ran.append(kwargs["env_vars"]["CAO_WORKFLOW_STEP_ID"])
        return _ok()

    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))
    res = await ws.resume_from_last_completed("runMix")

    assert ran == ["s2", "s3"]  # s1 kept; failed + pending re-run
    assert res.state == RunState.COMPLETED
    states = {s.id: s.state for s in res.steps}
    assert states == {
        "s1": StepState.COMPLETED,
        "s2": StepState.COMPLETED,
        "s3": StepState.COMPLETED,
    }


@pytest.mark.asyncio
async def test_resume_corrupt_is_valueerror_subclass(_patched_journal):
    # No contract break: both resume subtypes are ValueError subclasses.
    assert issubclass(ws.ResumeCorruptError, ValueError)
    assert issubclass(ws.ResumeNotAllowedError, ValueError)
