"""Workflow steps inherit the delegation ceiling of the terminal that started the run.

``run_agent_step`` is mocked; what is asserted is that every step creation
carries ``delegating_terminal_id=record.caller_id`` so ``create_terminal``
bounds the step by that terminal's stored ceiling (test_delegation_ceiling.py
covers the bound itself).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus
from cli_agent_orchestrator.models.workflow import RunState, StepState, WorkflowSpec, WorkflowStep
from cli_agent_orchestrator.models.workflow_runtime import StepOutputRecord
from cli_agent_orchestrator.services import workflow_service as ws
from cli_agent_orchestrator.services.agent_step import StepExecutionError

_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


@pytest.fixture(autouse=True)
def _clean_registry(tmp_path, monkeypatch: pytest.MonkeyPatch):
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


def _spec(schema=None) -> WorkflowSpec:
    return WorkflowSpec(
        name="wf",
        mode="sequential",
        steps=[
            WorkflowStep(
                id="s1", provider="claude_code", agent="dev", prompt="go", output_schema=schema
            )
        ],
    )


def _ok() -> AgentStepResult:
    return AgentStepResult(terminal_id="t1", last_message="done", status=TerminalStatus.COMPLETED)


@pytest.mark.asyncio
async def test_steps_carry_the_callers_id(monkeypatch):
    mock = AsyncMock(return_value=_ok())
    monkeypatch.setattr(ws, "run_agent_step", mock)

    await ws.start_run(_spec(), {}, "run-1", caller_id="a1b2c3d4")

    assert mock.call_args.kwargs["delegating_terminal_id"] == "a1b2c3d4"
    assert "caller_id" not in mock.call_args.kwargs, "the step's callback parent is unchanged"


@pytest.mark.asyncio
async def test_operator_runs_stay_unbounded(monkeypatch):
    mock = AsyncMock(return_value=_ok())
    monkeypatch.setattr(ws, "run_agent_step", mock)

    await ws.start_run(_spec(), {}, "run-2")

    assert mock.call_args.kwargs["delegating_terminal_id"] is None


@pytest.mark.asyncio
async def test_reprompt_also_carries_the_caller(monkeypatch):
    """The corrective reprompt creates a FRESH terminal; it must be bounded too."""
    calls = []

    async def _side(*a, **kw):
        calls.append(kw)
        if len(calls) == 2:
            ws.step_output_store.put(
                "run-3",
                "s1",
                StepOutputRecord(
                    run_id="run-3",
                    step_id="s1",
                    output={"answer": "42"},
                    validated=True,
                    errors=[],
                    state=StepState.COMPLETED,
                ),
            )
        return _ok()

    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))

    result = await ws.start_run(_spec(schema=_SCHEMA), {}, "run-3", caller_id="a1b2c3d4")

    assert result.state == RunState.COMPLETED
    assert len(calls) == 2
    assert all(kw["delegating_terminal_id"] == "a1b2c3d4" for kw in calls)


@pytest.mark.asyncio
async def test_a_refused_step_fails_the_run_loudly(monkeypatch):
    """create_terminal's refusal surfaces through run_agent_step as the step's
    error; the run fails with the message rather than launching the worker."""
    from cli_agent_orchestrator.utils.delegation import DelegationCeilingError

    refusal = DelegationCeilingError("Delegation refused: terminal a1b2c3d4 ...")
    monkeypatch.setattr(
        ws,
        "run_agent_step",
        AsyncMock(side_effect=StepExecutionError(str(refusal), kind="error", terminal_id=None)),
    )

    result = await ws.start_run(_spec(), {}, "run-4", caller_id="a1b2c3d4")

    assert result.state == RunState.FAILED
    assert "Delegation refused" in (result.steps[0].error or "")
