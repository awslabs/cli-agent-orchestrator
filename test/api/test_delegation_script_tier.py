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

    @staticmethod
    def _launch_capable(monkeypatch):
        """Arm a reviewer-started script run ``r3`` whose developer step WOULD launch.

        Everything after the refusal point is mocked so that, if the bound were
        dropped, the developer would LAUNCH (200, a stored row with execute_bash)
        and the assertions fail on that observation, not on a mock raising first
        (review of the first version of this test). Returns the backend mock so
        the test can assert no window was created.
        """
        from cli_agent_orchestrator.services import agent_step

        db_create_terminal(
            "0000beef", "cao-s", "rev", "claude_code", "reviewer", REVIEWER, delegate_tools=REVIEWER
        )
        monkeypatch.setitem(workflow_service.run_registry, "r3", _record("r3", "0000beef"))
        profiles = {"developer": AgentProfile(name="developer", description="d", role="developer")}
        monkeypatch.setattr(f"{_TS}.agent_profiles.load_agent_profile", lambda n: profiles[n])
        provider = AsyncMock()
        provider.initialize.return_value = True
        pm = MagicMock()
        pm.create_provider.return_value = provider
        monkeypatch.setattr(f"{_TS}.provider_manager", pm)
        monkeypatch.setattr(f"{_TS}.status_monitor", MagicMock())
        monkeypatch.setattr(f"{_TS}.fifo_manager", MagicMock())
        fifo_dir = MagicMock()
        fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")
        monkeypatch.setattr(f"{_TS}.FIFO_DIR", fifo_dir)
        backend = MagicMock()
        backend.session_exists.return_value = True
        backend.create_window.return_value = "developer-win"
        backend.supports_event_inbox.return_value = True
        monkeypatch.setattr("cli_agent_orchestrator.backends.registry._backend", backend)
        monkeypatch.setattr(agent_step, "wait_until_status", AsyncMock(return_value=True))
        monkeypatch.setattr(agent_step, "_wait_for_completion", AsyncMock())
        monkeypatch.setattr(
            "cli_agent_orchestrator.api.main.terminal_service.send_input", MagicMock()
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.api.main.terminal_service.get_output", lambda *a, **kw: "done"
        )
        return backend

    @staticmethod
    def _post_developer_step(client):
        with patch(
            "cli_agent_orchestrator.services.workflow_service.check_generation",
            return_value=None,
        ):
            return client.post(
                TERMINALS_RUN_STEP_ROUTE,
                json={
                    "provider": "claude_code",
                    "agent": "developer",
                    "prompt": "run make deploy",
                    "session_name": "cao-s",
                    "env_vars": _env("r3"),
                },
            )

    @staticmethod
    def _developer_rows():
        from cli_agent_orchestrator.clients import database

        with database.SessionLocal() as session:
            return (
                session.query(database.TerminalModel).filter_by(agent_profile="developer").count()
            )

    @pytest.mark.usefixtures("isolated_memory_db")
    def test_reviewer_started_script_cannot_launch_a_developer_step(
        self, client, monkeypatch, isolated_journal
    ):
        """End to end through the REAL run_agent_step and create_terminal: the
        script's developer step is refused with 403 against the reviewer's stored
        ceiling, and the script record's step settles FAILED with the refusal.

        The run carries the admission snapshot ``launch_guard.capture`` records at
        script admission (#1014), so the launch guard admits the step and the
        delegation ceiling is the check that refuses. Without that row the guard
        refuses first with 409 (next test) and this path is never reached."""
        import json

        from cli_agent_orchestrator.models.workflow import StepState
        from cli_agent_orchestrator.services import launch_guard

        backend = self._launch_capable(monkeypatch)
        workflow_journal.insert_run(
            "r3",
            "wf",
            json.dumps({"launch_guard": launch_guard.capture()}),
            "{}",
            "running",
            "t",
            tier="script",
            caller_id="0000beef",
        )

        resp = self._post_developer_step(client)
        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"]
        assert "Delegation refused" in detail and "0000beef" in detail
        assert "fs_*, execute_bash, web_fetch" in detail
        backend.create_window.assert_not_called()
        assert (
            self._developer_rows() == 0
        ), "no developer row may exist: the launch must not have happened"
        step = workflow_service.run_registry["r3"].step_states["step-1"]
        assert step.state == StepState.FAILED
        assert "Delegation refused" in (step.error or "")

    @pytest.mark.usefixtures("isolated_memory_db")
    def test_run_without_an_admission_snapshot_is_refused_409_before_the_ceiling(
        self, client, monkeypatch, isolated_journal
    ):
        """Pin the order when BOTH refusals apply. A live script record with no
        journal row has no recorded admission state, so ``launch_guard.check``
        (#1014) refuses with 409 ``plan_inputs_changed`` before ``create_terminal``
        ever compares the developer's allowlist with the reviewer's ceiling. The
        admission check wins on purpose: a run CAO cannot prove it admitted must be
        restarted whatever its steps ask for, and both refusals happen before any
        launch, so the order changes only which message the operator reads. Either
        way nothing launches."""
        backend = self._launch_capable(monkeypatch)
        assert workflow_journal.get_run("r3") is None

        resp = self._post_developer_step(client)
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["kind"] == "plan_inputs_changed"
        assert "recorded launch state is unreadable" in detail["message"]
        assert "Delegation refused" not in resp.text
        backend.create_window.assert_not_called()
        assert (
            self._developer_rows() == 0
        ), "no developer row may exist: the launch must not have happened"


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
        # #699 added working_directory to the same call; only the caller is under test.
        assert seen["caller_id"] == "a1b2c3d4"

    def test_run_script_workflow_records_the_caller(self, monkeypatch, isolated_journal):
        """The service puts caller_id on the record the run-step handler will read."""
        spec = _script_spec()
        captured = {}

        async def _drive(record, spec_path, env, **_kwargs):
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
        # #699 added working_directory to the same call; only the caller is under test.
        assert seen["caller_id"] == "a1b2c3d4"


class TestCallerBindingSurvivesRestart:
    """Review B round 3, R2: the binding must not live only in memory."""

    def test_orphaned_script_run_step_is_refused(self, client, isolated_journal):
        """Journal row for a reviewer-started script run, registry empty (the server
        restarted): the step is refused, nothing launches."""

        workflow_journal.insert_run(
            "r9",
            "wf",
            "{}",
            "{}",
            "running",
            "t",
            tier="script",
            generation="1",
            caller_id="0000beef",
        )
        assert "r9" not in workflow_service.run_registry
        with patch(_RUN_STEP, new=AsyncMock()) as m_run:
            resp = client.post(
                TERMINALS_RUN_STEP_ROUTE,
                json={
                    "provider": "claude_code",
                    "agent": "developer",
                    "prompt": "go",
                    "env_vars": _env("r9"),
                },
            )
        assert resp.status_code == 403, resp.text
        assert "no live record" in resp.json()["detail"]
        assert "cao workflow resume r9" in resp.json()["detail"]
        m_run.assert_not_called()

    def test_live_script_run_still_passes(self, client, isolated_journal, monkeypatch):
        from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus

        workflow_journal.insert_run("r10", "wf", "{}", "{}", "running", "t", tier="script")
        monkeypatch.setitem(workflow_service.run_registry, "r10", _record("r10", "0000beef"))
        result = AgentStepResult(
            terminal_id="abc12345", last_message="done", status=TerminalStatus.COMPLETED
        )
        with patch(_RUN_STEP, new=AsyncMock(return_value=result)) as m_run:
            resp = client.post(
                TERMINALS_RUN_STEP_ROUTE,
                json={
                    "provider": "claude_code",
                    "agent": "developer",
                    "prompt": "go",
                    "env_vars": _env("r10"),
                },
            )
        assert resp.status_code == 200, resp.text
        assert m_run.call_args.kwargs["delegating_terminal_id"] == "0000beef"

    def test_caller_is_journaled_and_resume_rebinds_to_the_original(
        self, monkeypatch, isolated_journal
    ):
        """The run row carries caller_id; a script resume by another actor (or the
        operator) re-binds to the ORIGINAL caller, never widening."""
        import asyncio

        from cli_agent_orchestrator.services.workflow_service import bound_caller

        spec = _script_spec()
        captured = {}

        async def _drive(record, spec_path, env, **_kwargs):
            captured["record"] = record
            workflow_journal.update_run_state(record.run_id, RunState.FAILED.value, "t")
            return WorkflowRunResult(
                run_id=record.run_id, workflow_name="wf", state=RunState.FAILED, started_at="t"
            )

        monkeypatch.setattr(script_runner, "_drive_process", _drive)
        monkeypatch.setattr(script_runner.approval_gate, "ensure_plan_approved", lambda **kw: None)
        asyncio.run(script_runner.run_script_workflow(spec, {}, "run-j", caller_id="0000beef"))
        assert workflow_journal.get_run("run-j").caller_id == "0000beef"

        workflow_service.run_registry.clear()
        asyncio.run(script_runner.resume_script_run("run-j", caller_id="0000cafe"))
        assert captured["record"].caller_id == "0000beef", "the original caller bounds the resume"
        workflow_service.run_registry.clear()
        asyncio.run(script_runner.resume_script_run("run-j"))
        assert captured["record"].caller_id == "0000beef", "an operator resume does not widen"

        assert bound_caller(None, "0000cafe") == "0000cafe"
        assert bound_caller("0000beef", None) == "0000beef"
