"""Unit tests for the workflow run engine (issue #312, Bolt 3 / N5).

Covers the load-bearing algorithm from ``functional-design/business-logic-model.md``:
the two nested recovery loops (Traces A–E, §2a), determinism, input-validation
before any terminal is created, ``retries: 0``, ``_substitute`` (happy + unknown-ref
+ no-eval), cancel (boundary skip / 409 / 404), ``get_run_status`` (snapshot / 404 /
no-secret-leak), and the reserved seams raising ``NotBuiltYetError``.

``run_agent_step`` and ``step_output_store`` are mocked — no real terminals.
"""

from __future__ import annotations

from typing import List
from unittest.mock import AsyncMock

import pytest

from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus
from cli_agent_orchestrator.models.workflow import (
    NotBuiltYetError,
    RunState,
    StepState,
    WorkflowSpec,
    WorkflowStep,
)
from cli_agent_orchestrator.models.workflow_runtime import StepOutputRecord
from cli_agent_orchestrator.services import workflow_service as ws
from cli_agent_orchestrator.services.agent_step import StepExecutionError

_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


@pytest.fixture(autouse=True)
def _clean_registry(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Each test starts with an empty run registry + output store.

    ``DATABASE_FILE`` is pointed at an empty temp path so the engine's
    best-effort journal write-through (and start_run's journal dup-check)
    never touches — or reads — the developer's real database.
    """
    monkeypatch.setattr(
        "cli_agent_orchestrator.constants.DATABASE_FILE", tmp_path / "wf.db", raising=True
    )
    ws.run_registry.clear()
    ws._active_drives.clear()
    ws.step_output_store._store.clear()
    yield
    ws.run_registry.clear()
    ws._active_drives.clear()
    ws.step_output_store._store.clear()


def _ok(terminal_id: str = "t1") -> AgentStepResult:
    return AgentStepResult(
        terminal_id=terminal_id, last_message="done", status=TerminalStatus.COMPLETED
    )


def _spec(
    *,
    name: str = "wf",
    schema=None,
    retries=None,
    on_failure=None,
    mode: str = "sequential",
    steps=None,
) -> WorkflowSpec:
    if steps is None:
        steps = [
            WorkflowStep(
                id="s1",
                provider="claude_code",
                agent="dev",
                prompt="go",
                output_schema=schema,
                retries=retries,
                on_failure=on_failure,
            )
        ]
    return WorkflowSpec(name=name, mode=mode, steps=steps)


def _put_valid(run_id: str, step_id: str = "s1") -> None:
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


def _put_invalid(run_id: str, step_id: str = "s1") -> None:
    ws.step_output_store.put(
        run_id,
        step_id,
        StepOutputRecord(
            run_id=run_id,
            step_id=step_id,
            output={"wrong": 1},
            validated=False,
            errors=["bad"],
            state=StepState.COMPLETED_UNVALIDATED,
        ),
    )


# ---------------------------------------------------------------------------
# §2a — Worked traces of the two nested loops
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_trace_a_clean_run(monkeypatch):
    """Trace A: attempt 1 COMPLETED + validated output -> COMPLETED, attempts=1."""
    mock = AsyncMock(return_value=_ok())
    monkeypatch.setattr(ws, "run_agent_step", mock)

    # First call records a valid output so _collect_structured_output validates.
    async def _side(*a, **kw):
        _put_valid(kw["env_vars"]["CAO_WORKFLOW_RUN_ID"], kw["env_vars"]["CAO_WORKFLOW_STEP_ID"])
        return _ok()

    mock.side_effect = _side

    res = await ws.start_run(_spec(schema=_SCHEMA), {}, "runA")
    assert res.state == RunState.COMPLETED
    assert res.steps[0].state == StepState.COMPLETED
    assert res.steps[0].attempts == 1
    assert res.steps[0].output == {"answer": "42"}
    assert mock.await_count == 1


@pytest.mark.asyncio
async def test_trace_b_worker_crashes_twice_then_succeeds(monkeypatch):
    """Trace B: two StepExecutionErrors, third attempt COMPLETED -> attempts=3."""
    calls = {"n": 0}

    async def _side(*a, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise StepExecutionError("boom", kind="error", terminal_id="tx")
        return _ok()

    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))

    res = await ws.start_run(_spec(), {}, "runB")  # no schema -> free-form COMPLETED
    assert res.state == RunState.COMPLETED
    assert res.steps[0].state == StepState.COMPLETED
    assert res.steps[0].attempts == 3


@pytest.mark.asyncio
async def test_trace_c_reprompt_then_crash_consumes_attempt(monkeypatch):
    """Trace C: attempt1 COMPLETED+invalid -> reprompt RAISES -> consumes attempt;
    attempt2 COMPLETED, reprompted already -> COMPLETED_UNVALIDATED."""
    calls = {"n": 0}

    async def _side(*a, **kw):
        calls["n"] += 1
        run_id = kw["env_vars"]["CAO_WORKFLOW_RUN_ID"]
        step_id = kw["env_vars"]["CAO_WORKFLOW_STEP_ID"]
        if calls["n"] == 1:
            _put_invalid(run_id, step_id)  # primary run lands an invalid record
            return _ok()
        if calls["n"] == 2:
            raise StepExecutionError("reprompt crash", kind="error", terminal_id="tr")
        # attempt 2's primary run
        _put_invalid(run_id, step_id)
        return _ok()

    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))

    res = await ws.start_run(_spec(schema=_SCHEMA), {}, "runC")
    assert res.state == RunState.COMPLETED
    assert res.steps[0].state == StepState.COMPLETED_UNVALIDATED
    assert res.steps[0].attempts == 2  # reprompt crash consumed an attempt


@pytest.mark.asyncio
async def test_trace_d_schema_never_met(monkeypatch):
    """Trace D: COMPLETED+invalid, reprompt OK but still invalid -> UNVALIDATED,
    one attempt (the reprompt is the inner loop, not a retry)."""

    async def _side(*a, **kw):
        run_id = kw["env_vars"]["CAO_WORKFLOW_RUN_ID"]
        step_id = kw["env_vars"]["CAO_WORKFLOW_STEP_ID"]
        _put_invalid(run_id, step_id)
        return _ok()

    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))

    res = await ws.start_run(_spec(schema=_SCHEMA), {}, "runD")
    assert res.state == RunState.COMPLETED
    assert res.steps[0].state == StepState.COMPLETED_UNVALIDATED
    assert res.steps[0].attempts == 1


@pytest.mark.asyncio
async def test_trace_d_missing_after_reprompt_is_unvalidated_not_failure(monkeypatch):
    """Decision note D1: a MISSING return after the reprompt is == invalid ->
    COMPLETED_UNVALIDATED, NOT a failure (never raises through B3-BR-4)."""
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    # Never put anything into the store -> record stays missing.
    res = await ws.start_run(_spec(schema=_SCHEMA), {}, "runDm")
    assert res.state == RunState.COMPLETED
    assert res.steps[0].state == StepState.COMPLETED_UNVALIDATED
    assert res.steps[0].attempts == 1


@pytest.mark.asyncio
async def test_trace_e_all_attempts_crash_halt(monkeypatch):
    """Trace E: attempts 1..4 all crash -> FAILED; on_failure=halt -> run FAILED."""
    monkeypatch.setattr(
        ws,
        "run_agent_step",
        AsyncMock(side_effect=StepExecutionError("dead", kind="timeout", terminal_id="td")),
    )
    res = await ws.start_run(_spec(on_failure="halt"), {}, "runE")
    assert res.state == RunState.FAILED
    assert res.steps[0].state == StepState.FAILED
    assert res.steps[0].attempts == 4  # default 3 retries -> 4 attempts
    assert res.steps[0].error is not None


# ---------------------------------------------------------------------------
# on_failure halt / continue + _skip_remaining
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_on_failure_halt_skips_successors(monkeypatch):
    """A halted step stays FAILED; its un-run successors -> SKIPPED."""

    async def _side(*a, **kw):
        step_id = kw["env_vars"]["CAO_WORKFLOW_STEP_ID"]
        if step_id == "s1":
            raise StepExecutionError("dead", kind="error", terminal_id="t")
        return _ok()

    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))
    steps = [
        WorkflowStep(id="s1", provider="p", agent="g", prompt="a", on_failure="halt", retries=0),
        WorkflowStep(id="s2", provider="p", agent="g", prompt="b"),
    ]
    res = await ws.start_run(_spec(steps=steps), {}, "runHalt")
    assert res.state == RunState.FAILED
    states = {s.id: s.state for s in res.steps}
    assert states["s1"] == StepState.FAILED
    assert states["s2"] == StepState.SKIPPED


@pytest.mark.asyncio
async def test_on_failure_continue_run_completes(monkeypatch):
    """on_failure=continue: failed step recorded FAILED, run still COMPLETED."""

    async def _side(*a, **kw):
        step_id = kw["env_vars"]["CAO_WORKFLOW_STEP_ID"]
        if step_id == "s1":
            raise StepExecutionError("dead", kind="error", terminal_id="t")
        return _ok()

    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))
    steps = [
        WorkflowStep(
            id="s1", provider="p", agent="g", prompt="a", on_failure="continue", retries=0
        ),
        WorkflowStep(id="s2", provider="p", agent="g", prompt="b"),
    ]
    res = await ws.start_run(_spec(steps=steps), {}, "runCont")
    assert res.state == RunState.COMPLETED
    states = {s.id: s.state for s in res.steps}
    assert states["s1"] == StepState.FAILED
    assert states["s2"] == StepState.COMPLETED


@pytest.mark.asyncio
async def test_retries_zero_one_attempt(monkeypatch):
    """retries:0 -> exactly one attempt, no retry."""
    mock = AsyncMock(side_effect=StepExecutionError("x", kind="error", terminal_id="t"))
    monkeypatch.setattr(ws, "run_agent_step", mock)
    res = await ws.start_run(_spec(retries=0, on_failure="halt"), {}, "runR0")
    assert res.steps[0].attempts == 1
    assert mock.await_count == 1


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_determinism_same_spec_same_order(monkeypatch):
    """Same spec -> identical step order, repeated (B3-RD-1)."""
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    steps = [
        WorkflowStep(id="alpha", provider="p", agent="g", prompt="a"),
        WorkflowStep(id="beta", provider="p", agent="g", prompt="b"),
        WorkflowStep(id="gamma", provider="p", agent="g", prompt="c"),
    ]
    order1 = [s.id for s in ws._topological_order(_spec(steps=steps))]
    order2 = [s.id for s in ws._topological_order(_spec(steps=steps))]
    assert order1 == order2 == ["alpha", "beta", "gamma"]


# ---------------------------------------------------------------------------
# Input validation BEFORE any terminal
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_missing_required_input_no_terminal(monkeypatch):
    from cli_agent_orchestrator.models.workflow import InputDecl

    mock = AsyncMock(return_value=_ok())
    monkeypatch.setattr(ws, "run_agent_step", mock)
    spec = _spec()
    spec.inputs = {"topic": InputDecl(type="string", required=True)}
    with pytest.raises(ValueError, match="missing required input 'topic'"):
        await ws.start_run(spec, {}, "runMiss")
    assert mock.await_count == 0  # NO terminal created


@pytest.mark.asyncio
async def test_type_mismatch_input_no_terminal(monkeypatch):
    from cli_agent_orchestrator.models.workflow import InputDecl

    mock = AsyncMock(return_value=_ok())
    monkeypatch.setattr(ws, "run_agent_step", mock)
    spec = _spec()
    spec.inputs = {"count": InputDecl(type="int", required=True)}
    with pytest.raises(ValueError, match="must be an int"):
        await ws.start_run(spec, {"count": "not-an-int"}, "runType")
    assert mock.await_count == 0


@pytest.mark.asyncio
async def test_unknown_input_rejected(monkeypatch):
    mock = AsyncMock(return_value=_ok())
    monkeypatch.setattr(ws, "run_agent_step", mock)
    with pytest.raises(ValueError, match="unknown input 'bogus'"):
        await ws.start_run(_spec(), {"bogus": 1}, "runUnk")
    assert mock.await_count == 0


@pytest.mark.asyncio
async def test_mid_run_engine_error_finalizes_record_failed(monkeypatch):
    """B2: a WorkflowEngineError raised by _substitute mid-run (a bad template
    reference, raised OUTSIDE _run_step's StepExecutionError guard) must propagate
    to the boundary AND leave the registered record in a terminal FAILED state with
    finished_at set — never stranded in RUNNING (domain-entities lifecycle / RD-3)."""
    mock = AsyncMock(return_value=_ok())
    monkeypatch.setattr(ws, "run_agent_step", mock)

    # Step prompt references an upstream step that never ran -> _substitute raises
    # WorkflowEngineError. This passes input validation (no inputs declared), so the
    # record IS registered before _run_step calls _substitute.
    step = WorkflowStep(
        id="s1",
        provider="claude_code",
        agent="dev",
        prompt="use {{steps.ghost.output.x}}",
    )
    with pytest.raises(ws.WorkflowEngineError, match="produced no output"):
        await ws.start_run(_spec(steps=[step]), {}, "runEngineErr")

    # The worker was never reached (the error is raised before run_agent_step).
    assert mock.await_count == 0
    # The registered record settled to a terminal FAILED state, not RUNNING.
    rec = ws.run_registry["runEngineErr"]
    assert rec.state == RunState.FAILED
    assert rec.finished_at is not None
    assert rec.current_step_id is None
    assert rec.step_states["s1"].state == StepState.FAILED


# ---------------------------------------------------------------------------
# _substitute
# ---------------------------------------------------------------------------
def _record_with(inputs=None, outputs=None) -> ws.RunRecord:
    spec = _spec()
    rec = ws.RunRecord(
        run_id="r",
        workflow_name="wf",
        spec=spec,
        inputs=inputs or {},
        step_states={"s1": ws.StepRunState(step_id="s1")},
    )
    if outputs:
        for sid, out in outputs.items():
            st = ws.StepRunState(step_id=sid)
            st.output = StepOutputRecord(
                run_id="r", step_id=sid, output=out, validated=True, state=StepState.COMPLETED
            )
            rec.step_states[sid] = st
    return rec


def test_substitute_resolves_input_and_step_output():
    rec = _record_with(inputs={"topic": "cats"}, outputs={"prior": {"summary": "purr"}})
    out = ws._substitute(
        "topic={{workflow.inputs.topic}} prior={{steps.prior.output.summary}}", rec
    )
    assert out == "topic=cats prior=purr"


def test_substitute_unknown_input_raises_engine_error():
    rec = _record_with(inputs={})
    with pytest.raises(ws.WorkflowEngineError, match="unknown input"):
        ws._substitute("{{workflow.inputs.nope}}", rec)


def test_substitute_missing_upstream_output_raises_engine_error():
    rec = _record_with()
    with pytest.raises(ws.WorkflowEngineError, match="produced no output"):
        ws._substitute("{{steps.unrun.output.field}}", rec)


def test_substitute_unsupported_reference_raises():
    rec = _record_with()
    with pytest.raises(ws.WorkflowEngineError, match="unsupported template reference"):
        ws._substitute("{{os.environ.SECRET}}", rec)


def test_substitute_no_eval_of_input_value():
    """A {...}-bearing input VALUE is substituted literally, never interpreted."""
    rec = _record_with(inputs={"x": "{evil.format} {{nested}}"})
    # The placeholder resolves to the raw value; the value's own braces are NOT
    # re-evaluated (no recursive .format / eval).
    out = ws._substitute("v={{workflow.inputs.x}}", rec)
    assert out == "v={evil.format} {{nested}}"


# ---------------------------------------------------------------------------
# cancel_run
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cancel_at_boundary_skips_remaining(monkeypatch):
    """Cancel observed at a step boundary -> CANCELLED + remaining PENDING SKIPPED."""

    async def _side(*a, **kw):
        # Cancel the run while step s1 is "in flight" so the flag is seen at the
        # NEXT boundary.
        ws.cancel_run("runCancel")
        return _ok()

    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))
    steps = [
        WorkflowStep(id="s1", provider="p", agent="g", prompt="a"),
        WorkflowStep(id="s2", provider="p", agent="g", prompt="b"),
    ]
    res = await ws.start_run(_spec(steps=steps), {}, "runCancel")
    assert res.state == RunState.CANCELLED
    states = {s.id: s.state for s in res.steps}
    assert states["s1"] == StepState.COMPLETED  # in-flight step ran to completion
    assert states["s2"] == StepState.SKIPPED


def test_cancel_unknown_run_404():
    with pytest.raises(KeyError):
        ws.cancel_run("does-not-exist")


@pytest.mark.asyncio
async def test_cancel_terminal_run_conflict(monkeypatch):
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    await ws.start_run(_spec(), {}, "runDone")
    with pytest.raises(ValueError, match="already completed"):
        ws.cancel_run("runDone")


# ---------------------------------------------------------------------------
# get_run_status
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_run_status_snapshot_shape(monkeypatch):
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    await ws.start_run(_spec(), {}, "runStat")
    status = ws.get_run_status("runStat")
    assert status.run_id == "runStat"
    assert status.state == RunState.COMPLETED
    assert [s.id for s in status.steps] == ["s1"]
    assert status.steps[0].state == StepState.COMPLETED


def test_get_run_status_unknown_404():
    with pytest.raises(KeyError):
        ws.get_run_status("nope")


@pytest.mark.asyncio
async def test_get_run_status_carries_no_output_or_prompt(monkeypatch):
    """B3-SD-3: the snapshot DTO carries no per-step output/prompt fields."""

    async def _side(*a, **kw):
        _put_valid("runLeak", "s1")
        return _ok()

    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))
    await ws.start_run(_spec(schema=_SCHEMA), {}, "runLeak")
    status = ws.get_run_status("runLeak")
    step_fields = set(status.steps[0].model_dump().keys())
    assert step_fields == {"id", "state", "attempts"}
    assert "output" not in status.model_dump()


# ---------------------------------------------------------------------------
# Reserved seams
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reserved_mode_raises_not_built_yet(monkeypatch):
    """A mode: parallel spec -> NotBuiltYetError, NOT a silent sequential run."""
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    spec = _spec(mode="parallel")
    with pytest.raises(NotBuiltYetError):
        await ws.start_run(spec, {}, "runPar")


@pytest.mark.asyncio
async def test_reserved_loop_mode_raises(monkeypatch):
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    spec = _spec(mode="loop")
    with pytest.raises(NotBuiltYetError):
        await ws.start_run(spec, {}, "runLoop")


def test_reserved_seam_methods_raise():
    # ``resume_from_last_completed`` is NO LONGER reserved as of Bolt 4 / N6 — it is
    # un-reserved here and exercised by test_workflow_journal_resume.py. The
    # remaining parallel/loop/guard seams stay reserved (N7/N8).
    with pytest.raises(NotBuiltYetError):
        ws._run_parallel(None, None)
    with pytest.raises(NotBuiltYetError):
        ws._run_loop(None, None)
    with pytest.raises(NotBuiltYetError):
        ws._eval_on_stall(None, None)
    with pytest.raises(NotBuiltYetError):
        ws._eval_on_no_progress(None, None, None)


@pytest.mark.asyncio
async def test_duplicate_run_id_conflict(monkeypatch):
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    await ws.start_run(_spec(), {}, "dup")
    with pytest.raises(KeyError, match="already exists"):
        await ws.start_run(_spec(), {}, "dup")


@pytest.mark.asyncio
async def test_bad_run_id_rejected(monkeypatch):
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    with pytest.raises(ValueError):
        await ws.start_run(_spec(), {}, "../escape")
