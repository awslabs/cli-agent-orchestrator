"""Script-tier (.py) workflows are bounded by the starting terminal's ceiling (review A, B1).

The script subprocess dispatches steps through ``POST /terminals/run-step`` with
only the run id and generation in its env, so the handler reads the live
``ScriptRunRecord`` the fenced run id names and passes its ``caller_id`` to
``run_agent_step`` as ``delegating_terminal_id``. The record's ``caller_id`` is
set by the run, submit and resume routes from the request body. Nothing the
script asserts is trusted; the record is the source.
"""

from test.api.test_workflow_run_surface_tier import _GOOD_SCRIPT
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.clients.database import create_terminal as db_create_terminal
from cli_agent_orchestrator.constants import ROLE_TOOL_DEFAULTS, TERMINALS_RUN_STEP_ROUTE
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.workflow import RunState, ScriptSpec
from cli_agent_orchestrator.models.workflow_runtime import WorkflowRunResult
from cli_agent_orchestrator.services import script_runner, workflow_journal, workflow_service
from cli_agent_orchestrator.services.script_runner import (
    ScriptRunRecord,
    step_delegating_terminal_id,
)

_RUN_STEP = "cli_agent_orchestrator.api.main.run_agent_step"
_TS = "cli_agent_orchestrator.services.terminal_service"
REVIEWER = ROLE_TOOL_DEFAULTS["reviewer"]


@pytest.fixture
def isolated_journal(tmp_path, monkeypatch):
    from cli_agent_orchestrator.clients.database import (
        _migrate_workflow_run,
        _migrate_workflow_run_step,
    )

    monkeypatch.setattr(
        "cli_agent_orchestrator.constants.DATABASE_FILE", tmp_path / "wf.db", raising=True
    )
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    workflow_service.run_registry.clear()
    yield
    workflow_service.run_registry.clear()


def _record(run_id, caller_id=None):
    return ScriptRunRecord(
        run_id=run_id,
        workflow_name="wf",
        state=RunState.RUNNING,
        cancelled=False,
        current_step_id=None,
        step_states={},
        process=None,
        generation="1",
        started_at="2026-07-15T00:00:00Z",
        finished_at=None,
        caller_id=caller_id,
    )


def _env(run_id):
    return {
        "CAO_WORKFLOW_RUN_ID": run_id,
        "CAO_WORKFLOW_GENERATION": "1",
        "CAO_WORKFLOW_STEP_ID": "step-1",
    }


def _script_spec():
    return ScriptSpec(
        name="scriptwf", path="/tmp/scriptwf.py", source=_GOOD_SCRIPT, content_hash="deadbeef"
    )


class TestStepDelegatingTerminalId:
    def test_reads_the_live_script_record(self, monkeypatch):
        monkeypatch.setitem(workflow_service.run_registry, "r1", _record("r1", "a1b2c3d4"))
        assert step_delegating_terminal_id(_env("r1")) == "a1b2c3d4"

    def test_none_without_env_record_or_caller(self, monkeypatch):
        assert step_delegating_terminal_id(None) is None
        assert step_delegating_terminal_id({"CAO_WORKFLOW_STEP_ID": "s"}) is None
        assert step_delegating_terminal_id(_env("ghost")) is None
        monkeypatch.setitem(workflow_service.run_registry, "op", _record("op"))
        assert step_delegating_terminal_id(_env("op")) is None


class TestRunStepHandler:
    def test_script_step_carries_the_runs_caller(self, client, monkeypatch, isolated_journal):
        """The handler passes record.caller_id, not anything from the request."""
        from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus

        monkeypatch.setitem(workflow_service.run_registry, "r2", _record("r2", "a1b2c3d4"))
        result = AgentStepResult(
            terminal_id="abc12345", last_message="done", status=TerminalStatus.COMPLETED
        )
        with (
            patch(
                "cli_agent_orchestrator.services.workflow_service.check_generation",
                return_value=None,
            ),
            patch(_RUN_STEP, new=AsyncMock(return_value=result)) as m_run,
        ):
            resp = client.post(
                TERMINALS_RUN_STEP_ROUTE,
                json={
                    "provider": "claude_code",
                    "agent": "developer",
                    "prompt": "go",
                    "env_vars": _env("r2"),
                },
            )
        assert resp.status_code == 200
        assert m_run.call_args.kwargs["delegating_terminal_id"] == "a1b2c3d4"
        assert m_run.call_args.kwargs["caller_id"] is None

    def test_non_script_call_is_unbounded_as_before(self, client):
        from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus

        result = AgentStepResult(
            terminal_id="abc12345", last_message="done", status=TerminalStatus.COMPLETED
        )
        with patch(_RUN_STEP, new=AsyncMock(return_value=result)) as m_run:
            resp = client.post(
                TERMINALS_RUN_STEP_ROUTE,
                json={"provider": "claude_code", "agent": "developer", "prompt": "go"},
            )
        assert resp.status_code == 200
        assert m_run.call_args.kwargs["delegating_terminal_id"] is None

    @pytest.mark.usefixtures("isolated_memory_db")
    def test_reviewer_started_script_cannot_launch_a_developer_step(
        self, client, monkeypatch, isolated_journal
    ):
        """End to end through the REAL run_agent_step and create_terminal: the
        script's developer step is refused with 403 against the reviewer's stored
        ceiling, and the script record's step settles FAILED with the refusal."""
        from cli_agent_orchestrator.models.workflow import StepState

        db_create_terminal(
            "0000beef", "cao-s", "rev", "claude_code", "reviewer", REVIEWER, delegate_tools=REVIEWER
        )
        monkeypatch.setitem(workflow_service.run_registry, "r3", _record("r3", "0000beef"))
        profiles = {"developer": AgentProfile(name="developer", description="d", role="developer")}
        monkeypatch.setattr(f"{_TS}.agent_profiles.load_agent_profile", lambda n: profiles[n])
        monkeypatch.setattr(f"{_TS}.provider_manager", MagicMock())
        backend = MagicMock()
        backend.session_exists.return_value = True
        monkeypatch.setattr("cli_agent_orchestrator.backends.registry._backend", backend)

        with patch(
            "cli_agent_orchestrator.services.workflow_service.check_generation",
            return_value=None,
        ):
            resp = client.post(
                TERMINALS_RUN_STEP_ROUTE,
                json={
                    "provider": "claude_code",
                    "agent": "developer",
                    "prompt": "run make deploy",
                    "session_name": "cao-s",
                    "env_vars": _env("r3"),
                },
            )
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert "Delegation refused" in detail and "0000beef" in detail
        assert "fs_*, execute_bash, web_fetch" in detail
        backend.create_window.assert_not_called()
        step = workflow_service.run_registry["r3"].step_states["step-1"]
        assert step.state == StepState.FAILED
        assert "Delegation refused" in (step.error or "")


class TestRoutesCarryTheCaller:
    def test_run_route_passes_caller_id_to_the_script_runner(self, client, monkeypatch):
        spec = _script_spec()
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.workflow_spec_service.get_workflow",
            lambda name_or_path, scan_dir=None: spec,
        )
        monkeypatch.setattr(workflow_service, "_check_run_id_available", lambda rid: None)
        seen = {}

        async def _fake_run(spec_arg, inputs, run_id, **kwargs):
            seen.update(kwargs)
            return WorkflowRunResult(
                run_id=run_id,
                workflow_name=spec_arg.name,
                state=RunState.COMPLETED,
                started_at="2026-01-01T00:00:00Z",
            )

        monkeypatch.setattr(script_runner, "run_script_workflow", _fake_run)
        resp = client.post(
            "/workflows/runs",
            json={"name_or_path": "scriptwf", "inputs": {}, "caller_id": "a1b2c3d4"},
        )
        assert resp.status_code == 200
        assert seen == {"caller_id": "a1b2c3d4"}

    def test_run_script_workflow_records_the_caller(self, monkeypatch, isolated_journal):
        """The service puts caller_id on the record the run-step handler will read."""
        spec = _script_spec()
        captured = {}

        async def _drive(record, spec_path, env):
            captured["record"] = record
            return WorkflowRunResult(
                run_id=record.run_id,
                workflow_name="wf",
                state=RunState.COMPLETED,
                started_at="t",
            )

        monkeypatch.setattr(script_runner, "_drive_process", _drive)
        monkeypatch.setattr(script_runner.approval_gate, "ensure_plan_approved", lambda **kw: None)
        import asyncio

        asyncio.run(script_runner.run_script_workflow(spec, {}, "run-caller", caller_id="a1b2c3d4"))
        assert captured["record"].caller_id == "a1b2c3d4"
        assert step_delegating_terminal_id(_env("run-caller")) == "a1b2c3d4"

    def test_submit_route_records_the_caller(self, client, monkeypatch, isolated_journal):
        spec = _script_spec()
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.workflow_spec_service.get_workflow",
            lambda name_or_path, scan_dir=None: spec,
        )
        monkeypatch.setattr(script_runner.approval_gate, "ensure_plan_approved", lambda **kw: None)

        async def _prepared(record, spec_path, env):
            workflow_journal.update_run_state(record.run_id, RunState.COMPLETED.value, "t")
            return WorkflowRunResult(
                run_id=record.run_id, workflow_name="wf", state=RunState.COMPLETED, started_at="t"
            )

        monkeypatch.setattr(script_runner, "run_script_workflow_prepared", _prepared)
        resp = client.post(
            "/workflows/runs:submit",
            json={
                "name_or_path": "scriptwf",
                "inputs": {},
                "run_id": "sub-c",
                "caller_id": "a1b2c3d4",
            },
        )
        assert resp.status_code == 202
        assert workflow_service.run_registry["sub-c"].caller_id == "a1b2c3d4"

    def test_resume_route_passes_caller_id_to_the_script_runner(self, client, monkeypatch):
        row = workflow_journal.RunRow(
            run_id="run1",
            workflow_name="wf",
            spec_snapshot="{}",
            inputs_json="{}",
            state="failed",
            current_step_id=None,
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
            tier="script",
        )
        monkeypatch.setattr(workflow_journal, "get_run", lambda rid: row)
        seen = {}

        async def _fake_resume(run_id, **kwargs):
            seen.update(kwargs)
            return WorkflowRunResult(
                run_id=run_id, workflow_name="wf", state=RunState.COMPLETED, started_at="t"
            )

        monkeypatch.setattr(script_runner, "resume_script_run", _fake_resume)
        resp = client.post("/workflows/runs/run1/resume", json={"caller_id": "a1b2c3d4"})
        assert resp.status_code == 200
        assert seen == {"caller_id": "a1b2c3d4"}
