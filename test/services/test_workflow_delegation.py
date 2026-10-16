"""Workflow steps inherit the delegation ceiling of the terminal that started the run.

``run_agent_step`` is mocked; what is asserted is that every step creation
carries ``delegating_terminal_id=record.caller_id`` so ``create_terminal``
bounds the step by that terminal's stored ceiling (test_delegation_ceiling.py
covers the bound itself).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from cli_agent_orchestrator.clients.database import create_terminal as db_create_terminal
from cli_agent_orchestrator.constants import ROLE_TOOL_DEFAULTS
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus
from cli_agent_orchestrator.models.workflow import RunState, StepState, WorkflowSpec, WorkflowStep
from cli_agent_orchestrator.models.workflow_runtime import StepOutputRecord
from cli_agent_orchestrator.services import workflow_journal
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


# ---------------------------------------------------------------------------
# Review A, B2: the REAL refusal from the REAL create_terminal must settle the
# step FAILED and the run FAILED, never escape the engine.
# ---------------------------------------------------------------------------

_TS = "cli_agent_orchestrator.services.terminal_service"
_REVIEWER = ROLE_TOOL_DEFAULTS["reviewer"]


@pytest.fixture
def real_create_terminal(monkeypatch):
    """run_agent_step and create_terminal run for real; tmux/provider are mocked and
    the reviewer's row is a real SQLite row (isolated_memory_db)."""
    from unittest.mock import AsyncMock as _AsyncMock

    from cli_agent_orchestrator.services import agent_step, terminal_service

    profiles = {"dev": AgentProfile(name="dev", description="d", role="developer")}
    monkeypatch.setattr(f"{_TS}.agent_profiles.load_agent_profile", lambda n: profiles[n])
    provider = _AsyncMock()
    provider.initialize.return_value = True
    pm = MagicMock()
    pm.create_provider.return_value = provider
    monkeypatch.setattr(f"{_TS}.provider_manager", pm)
    monkeypatch.setattr(f"{_TS}.status_monitor", MagicMock())
    monkeypatch.setattr(f"{_TS}.fifo_manager", MagicMock())
    fifo_dir = MagicMock()
    fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")
    monkeypatch.setattr(f"{_TS}.FIFO_DIR", fifo_dir)
    # The refusal fires BEFORE any of this is reached. It is mocked so that, if
    # the bound were ever dropped (the mutation Review B ran), the worker would
    # launch and the ASSERTIONS below fail, rather than a mock raising first.
    backend = MagicMock()
    backend.session_exists.return_value = False
    backend.create_window.return_value = "win"
    backend.supports_event_inbox.return_value = True
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry._backend", backend)
    monkeypatch.setattr(agent_step, "wait_until_status", _AsyncMock(return_value=True))
    monkeypatch.setattr(agent_step, "_wait_for_completion", _AsyncMock())
    monkeypatch.setattr(terminal_service, "send_input", MagicMock())
    monkeypatch.setattr(terminal_service, "get_output", lambda *a, **kw: "done")
    db_create_terminal(
        "0000beef", "cao-s", "rev", "claude_code", "reviewer", _REVIEWER, delegate_tools=_REVIEWER
    )
    return backend


@pytest.fixture
def journal_tables(tmp_path, monkeypatch):
    from cli_agent_orchestrator.clients.database import (
        _migrate_workflow_run,
        _migrate_workflow_run_step,
    )

    monkeypatch.setattr(
        "cli_agent_orchestrator.constants.DATABASE_FILE", tmp_path / "wf.db", raising=True
    )
    _migrate_workflow_run()
    _migrate_workflow_run_step()


@pytest.mark.asyncio
@pytest.mark.usefixtures("isolated_memory_db", "journal_tables")
async def test_real_refusal_settles_the_step_and_run_failed(real_create_terminal):
    """No mock of the error type: create_terminal raises DelegationCeilingError
    against the reviewer's stored ceiling; the step is FAILED with the refusal,
    the run is FAILED, the journal row is settled, nothing escaped."""
    spec = WorkflowSpec(
        name="wf",
        mode="sequential",
        steps=[WorkflowStep(id="s1", provider="claude_code", agent="dev", prompt="go")],
    )

    result = await ws.start_run(spec, {}, "run-refused", caller_id="0000beef")

    assert result.state == RunState.FAILED
    step = result.steps[0]
    assert step.state == StepState.FAILED
    assert step.attempts == 1, "a refusal is deterministic and must not be retried"
    assert "Delegation refused" in (step.error or "")
    assert "0000beef" in step.error and "fs_*, execute_bash, web_fetch" in step.error
    real_create_terminal.create_session.assert_not_called()
    real_create_terminal.create_window.assert_not_called()
    row = workflow_journal.get_run("run-refused")
    assert row is not None and row.state == RunState.FAILED.value
    (step_row,) = workflow_journal.get_steps("run-refused")
    assert step_row.state == StepState.FAILED.value
    assert "run-refused" not in ws._active_drives


@pytest.mark.asyncio
@pytest.mark.usefixtures("isolated_memory_db", "journal_tables")
async def test_real_refusal_with_on_failure_continue_keeps_sequencing(real_create_terminal):
    spec = WorkflowSpec(
        name="wf",
        mode="sequential",
        steps=[
            WorkflowStep(
                id="s1", provider="claude_code", agent="dev", prompt="go", on_failure="continue"
            ),
        ],
    )
    result = await ws.start_run(spec, {}, "run-continue", caller_id="0000beef")
    assert result.steps[0].state == StepState.FAILED
    assert "Delegation refused" in (result.steps[0].error or "")


@pytest.mark.usefixtures("isolated_memory_db", "journal_tables")
def test_blocking_run_route_returns_the_failed_run_not_500(real_create_terminal, monkeypatch):
    """Review A: the blocking POST /workflows/runs used to 500 and leave the row RUNNING."""
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api.main import app
    from cli_agent_orchestrator.plugins import PluginRegistry

    spec = WorkflowSpec(
        name="wf",
        mode="sequential",
        steps=[WorkflowStep(id="s1", provider="claude_code", agent="dev", prompt="go")],
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.workflow_spec_service.get_workflow",
        lambda name_or_path, scan_dir=None: spec,
    )
    app.state.plugin_registry = PluginRegistry()
    client = TestClient(app)
    resp = client.post(
        "/workflows/runs",
        json={"name_or_path": "wf", "inputs": {}, "run_id": "run-http", "caller_id": "0000beef"},
        headers={"Host": "localhost"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "failed"
    assert "Delegation refused" in body["steps"][0]["error"]
    assert workflow_journal.get_run("run-http").state == "failed"


# ---------------------------------------------------------------------------
# Review A, T1: the replay site must carry the kwarg (fails if it is dropped).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.usefixtures("journal_tables")
async def test_replay_carries_the_replaying_terminal(monkeypatch):
    import json

    spec = WorkflowSpec(
        name="wf",
        mode="sequential",
        steps=[WorkflowStep(id="s1", provider="claude_code", agent="dev", prompt="go")],
    )
    workflow_journal.insert_run(
        run_id="runR",
        workflow_name="wf",
        spec_snapshot=spec.model_dump_json(),
        inputs_json=json.dumps({}),
        state=RunState.COMPLETED.value,
        started_at="2026-01-01T00:00:00Z",
        tier="yaml",
    )
    workflow_journal.insert_steps("runR", [("s1", StepState.PENDING.value)], "2026-01-01T00:00:00Z")
    mock = AsyncMock(return_value=_ok())
    monkeypatch.setattr(ws, "run_agent_step", mock)

    await ws.replay_single_step("runR", "s1", caller_id="a1b2c3d4")

    assert mock.call_args.kwargs["delegating_terminal_id"] == "a1b2c3d4"


@pytest.mark.asyncio
@pytest.mark.usefixtures("isolated_memory_db", "journal_tables")
async def test_replay_reports_a_real_refusal_in_the_payload(real_create_terminal):
    import json

    spec = WorkflowSpec(
        name="wf",
        mode="sequential",
        steps=[WorkflowStep(id="s1", provider="claude_code", agent="dev", prompt="go")],
    )
    workflow_journal.insert_run(
        run_id="runRR",
        workflow_name="wf",
        spec_snapshot=spec.model_dump_json(),
        inputs_json=json.dumps({}),
        state=RunState.COMPLETED.value,
        started_at="2026-01-01T00:00:00Z",
        tier="yaml",
    )
    workflow_journal.insert_steps(
        "runRR", [("s1", StepState.PENDING.value)], "2026-01-01T00:00:00Z"
    )

    payload = await ws.replay_single_step("runRR", "s1", caller_id="0000beef")

    assert payload["error_kind"] == "error"
    assert "Delegation refused" in payload["error"]
    assert payload["terminal_id"] is None


# ---------------------------------------------------------------------------
# Review B round 3, R2: the caller is journaled; a YAML resume re-binds to it.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.usefixtures("journal_tables")
async def test_yaml_resume_rebinds_to_the_journaled_caller(monkeypatch):
    spec = WorkflowSpec(
        name="wf",
        mode="sequential",
        steps=[WorkflowStep(id="s1", provider="claude_code", agent="dev", prompt="go")],
    )
    import json

    failing = AsyncMock(side_effect=StepExecutionError("boom", kind="error", terminal_id=None))
    monkeypatch.setattr(ws, "run_agent_step", failing)
    result = await ws.start_run(spec, {}, "run-start", caller_id="0000beef")
    assert result.state == RunState.FAILED
    assert workflow_journal.get_run("run-start").caller_id == "0000beef", "journaled at start"

    # A crashed run as the journal would hold it after a restart: FAILED row carrying
    # the original caller, its step PENDING, no live record.
    workflow_journal.insert_run(
        run_id="run-rb",
        workflow_name="wf",
        spec_snapshot=spec.model_dump_json(),
        inputs_json=json.dumps({}),
        state=RunState.FAILED.value,
        started_at="2026-01-01T00:00:00Z",
        caller_id="0000beef",
    )
    workflow_journal.insert_steps(
        "run-rb", [("s1", StepState.PENDING.value)], "2026-01-01T00:00:00Z"
    )
    ws.run_registry.clear()  # the restart
    ok = AsyncMock(return_value=_ok())
    monkeypatch.setattr(ws, "run_agent_step", ok)
    await ws.resume_from_last_completed("run-rb", caller_id="0000cafe")
    assert ok.call_args.kwargs["delegating_terminal_id"] == "0000beef"

    workflow_journal.update_run_state("run-rb", RunState.FAILED.value, None)
    workflow_journal.update_step(
        run_id="run-rb",
        step_id="s1",
        state=StepState.PENDING.value,
        attempts=0,
        updated_at="2026-01-01T00:00:02Z",
        output_json=None,
    )
    ws.run_registry.clear()
    await ws.resume_from_last_completed("run-rb")  # operator resume
    assert ok.call_args.kwargs["delegating_terminal_id"] == "0000beef"


@pytest.mark.usefixtures("journal_tables")
def test_workflow_run_row_has_the_caller_column(tmp_path):
    import sqlite3

    from cli_agent_orchestrator import constants

    with sqlite3.connect(str(constants.DATABASE_FILE)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(workflow_run)")}
    assert "caller_id" in columns
    workflow_journal.insert_run("legacy", "wf", "{}", "{}", "running", "t")
    assert workflow_journal.get_run("legacy").caller_id is None
