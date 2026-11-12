"""What cao-server refuses under the remote terminal backend in #745 Phase 1.

1. User code (Python workflow scripts and flow pre-scripts) would run on the
   server's own host, not on a runner: refused where it would start, with 409
   from the API, before anything is journaled or spawned; a flow's schedule
   still advances. Storing a definition is not refused.
2. API authentication: agents on runners would have no credential for their
   orchestration calls, so cao-server refuses to start with both.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.api.main import app, lifespan
from cli_agent_orchestrator.models.workflow import ScriptSpec
from cli_agent_orchestrator.services import flow_service, script_runner, workflow_journal
from cli_agent_orchestrator.utils.execution_host import (
    ServerHostCodeRefused,
    refuse_server_host_code,
    remote_auth_refusal,
)

_GET_BACKEND = "cli_agent_orchestrator.backends.registry.get_backend"


class _Remote:
    resolves_paths_locally = False


class _Local:
    resolves_paths_locally = True


@pytest.fixture
def remote(monkeypatch):
    monkeypatch.setattr(_GET_BACKEND, lambda: _Remote())


@pytest.fixture
def spawned(monkeypatch):
    """Every way a script could start; none may be reached."""
    calls = []

    async def _spawn(*args, **kwargs):
        calls.append("spawn")
        raise AssertionError("a script was spawned")

    monkeypatch.setattr(script_runner, "_drive_process", _spawn)
    monkeypatch.setattr(script_runner, "_drive_frozen", _spawn)
    monkeypatch.setattr(script_runner, "lint_script", lambda *a, **k: calls.append("lint") or None)
    monkeypatch.setattr(workflow_journal, "insert_run", lambda *a, **k: calls.append("insert"))
    return calls


def _spec():
    return ScriptSpec(name="wf", path="/tmp/wf.py", source="print(1)\n", content_hash="d" * 8)


# --- the guard ------------------------------------------------------------------


def test_the_guard_refuses_only_under_a_backend_that_runs_terminals_elsewhere(monkeypatch):
    monkeypatch.setattr(_GET_BACKEND, lambda: _Local())
    refuse_server_host_code("x")  # no-op
    monkeypatch.setattr(_GET_BACKEND, lambda: object())  # a backend without the hook
    refuse_server_host_code("x")
    monkeypatch.setattr(_GET_BACKEND, lambda: _Remote())
    with pytest.raises(ServerHostCodeRefused, match="terminal.backend=remote"):
        refuse_server_host_code("x")
    assert not issubclass(ServerHostCodeRefused, ValueError)  # never a 404 or 400


# --- the script runner (every caller: API, MCP, CLI) ----------------------------


class TestScriptRunner:
    def test_a_run_is_refused_before_lint_journal_or_spawn(self, remote, spawned):
        with pytest.raises(ServerHostCodeRefused):
            asyncio.run(script_runner.run_script_workflow(_spec(), {}, "run-1"))
        assert spawned == []

    def test_a_prepared_run_is_refused_before_spawn(self, remote, spawned):
        record = script_runner.ScriptRunRecord(
            run_id="run-2",
            workflow_name="wf",
            state="running",
            cancelled=False,
            current_step_id=None,
            step_states={},
            process=None,
            generation="1",
            started_at="t",
            finished_at=None,
        )
        with pytest.raises(ServerHostCodeRefused):
            asyncio.run(script_runner.run_script_workflow_prepared(record, source="", env={}))
        assert spawned == [] and "run-2" not in script_runner._active_drives

    def test_a_resume_is_refused_before_admission(self, remote, spawned, monkeypatch):
        looked_up = []
        monkeypatch.setattr(workflow_journal, "get_run", lambda rid: looked_up.append(rid))
        with pytest.raises(ServerHostCodeRefused):
            asyncio.run(script_runner.resume_script_run("run-3"))
        assert looked_up == [] and spawned == []


# --- the API -----------------------------------------------------------------------


class TestApi:
    @pytest.fixture
    def script_spec(self, monkeypatch):
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.workflow_spec_service.get_workflow",
            lambda name_or_path, scan_dir=None: _spec(),
        )

    def test_a_blocking_run_answers_409(self, client, remote, spawned, script_spec, monkeypatch):
        from cli_agent_orchestrator.services import workflow_service

        monkeypatch.setattr(workflow_service, "_check_run_id_available", lambda rid: None)
        resp = client.post("/workflows/runs", json={"name_or_path": "wf", "inputs": {}})
        assert resp.status_code == 409, resp.text
        assert "terminal.backend=remote" in resp.json()["detail"]
        assert spawned == []

    def test_an_async_submission_answers_409_and_journals_nothing(
        self, client, remote, spawned, script_spec
    ):
        resp = client.post(
            "/workflows/runs:submit", json={"name_or_path": "wf", "inputs": {}, "run_id": "sub-1"}
        )
        assert resp.status_code == 409, resp.text
        assert spawned == []  # no lint, no insert: no RUNNING row is left behind

    def test_a_resume_answers_409(self, client, remote, spawned, monkeypatch):
        row = workflow_journal.RunRow(
            run_id="run1",
            workflow_name="wf",
            spec_snapshot="{}",
            inputs_json="{}",
            state="failed",
            current_step_id=None,
            started_at="t",
            finished_at="t",
            tier="script",
        )
        monkeypatch.setattr(workflow_journal, "get_run", lambda rid: row)
        resp = client.post("/workflows/runs/run1/resume", json={})
        assert resp.status_code == 409, resp.text
        assert spawned == []

    def test_a_flow_with_a_pre_script_answers_409_not_500(self, client, remote, monkeypatch):
        async def refused(name):
            refuse_server_host_code(f"flow {name!r}: its pre-script")

        monkeypatch.setattr(flow_service, "execute_flow", refused)
        resp = client.post("/flows/nightly/run")
        assert resp.status_code == 409, resp.text


# --- flows: service, scheduler, CLI -----------------------------------------------


class TestFlows:
    @pytest.fixture
    def flow(self, tmp_path, monkeypatch):
        script = tmp_path / "pre.sh"
        script.write_text('#!/bin/sh\necho \'{"execute": true, "output": {}}\'\n')
        script.chmod(0o755)
        flow_file = tmp_path / "nightly.flow.md"
        flow_file.write_text("---\nname: nightly\nschedule: '0 0 * * *'\n---\nDo it.\n")
        from cli_agent_orchestrator.models.flow import Flow

        advanced = []
        ran = []
        monkeypatch.setattr(
            flow_service,
            "get_flow",
            lambda name: Flow(
                name=name,
                file_path=str(flow_file),
                schedule="0 0 * * *",
                agent_profile="developer",
                script=str(script),
            ),
        )
        monkeypatch.setattr(
            flow_service, "db_update_flow_run_times", lambda name, **kw: advanced.append(name)
        )
        monkeypatch.setattr(flow_service.subprocess, "run", lambda *a, **k: ran.append(a))
        return advanced, ran

    def test_execute_flow_advances_the_schedule_then_refuses_the_pre_script(self, remote, flow):
        advanced, ran = flow
        with pytest.raises(ServerHostCodeRefused):
            asyncio.run(flow_service.execute_flow("nightly"))
        assert advanced == ["nightly"]  # the daemon does not retry it every poll
        assert ran == []

    def test_the_cli_reports_the_refusal_and_exits_non_zero(self, remote, flow, monkeypatch):
        from cli_agent_orchestrator.cli.commands import schedule as schedule_mod

        monkeypatch.setattr(schedule_mod, "init_db", lambda: None)

        async def run_flow(name):
            return await flow_service.execute_flow(name)

        monkeypatch.setattr(schedule_mod, "_run_flow_with_pipeline", run_flow)
        result = CliRunner().invoke(schedule_mod.schedule, ["run", "nightly"])
        assert result.exit_code == 1
        assert "terminal.backend=remote" in result.output
        assert flow[1] == []

    def test_a_local_backend_still_runs_the_pre_script(self, flow, monkeypatch):
        monkeypatch.setattr(_GET_BACKEND, lambda: _Local())
        advanced, ran = flow
        with pytest.raises(Exception) as exc:  # the stubbed subprocess returns no result
            asyncio.run(flow_service.execute_flow("nightly"))
        assert not isinstance(exc.value, ServerHostCodeRefused)
        assert len(ran) == 1


# --- API auth ------------------------------------------------------------------------


class TestApiAuth:
    @pytest.fixture(autouse=True)
    def no_auth(self, monkeypatch):
        for name in (
            "CAO_AUTH_LOCAL_TOKEN",
            "CAO_AUTH_JWKS_URI",
            "AUTH0_DOMAIN",
            "CAO_AUTH_ISSUER",
        ):
            monkeypatch.delenv(name, raising=False)

    def test_local_token_mode_is_refused(self, remote, monkeypatch):
        monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", "a" * 32)
        assert "does not support API authentication" in remote_auth_refusal()

    @pytest.mark.parametrize("variable", ["CAO_AUTH_JWKS_URI", "AUTH0_DOMAIN"])
    def test_idp_mode_is_refused(self, remote, monkeypatch, variable):
        value = "https://idp.example/.well-known/jwks.json" if "JWKS" in variable else "x.auth0.com"
        monkeypatch.setenv(variable, value)
        assert "does not support API authentication" in remote_auth_refusal()

    def test_without_auth_or_with_a_local_backend_it_starts(self, monkeypatch):
        monkeypatch.setattr(_GET_BACKEND, lambda: _Remote())
        assert remote_auth_refusal() is None
        monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", "a" * 32)
        monkeypatch.setattr(_GET_BACKEND, lambda: _Local())
        assert remote_auth_refusal() is None

    @pytest.mark.asyncio
    async def test_the_lifespan_refuses_before_logging_the_lock_the_token_or_the_database(
        self, remote, monkeypatch
    ):
        monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", "a" * 32)
        touched = []
        with (
            patch(
                "cli_agent_orchestrator.api.main.setup_logging",
                side_effect=lambda: touched.append("logging"),
            ),
            patch(
                "cli_agent_orchestrator.api.main.install_access_log_redaction",
                side_effect=lambda: touched.append("access log"),
            ),
            patch(
                "cli_agent_orchestrator.api.main.server_lock.acquire_server_lock",
                side_effect=lambda **k: touched.append("lock"),
            ),
            patch(
                "cli_agent_orchestrator.api.main.runner_token",
                side_effect=lambda: touched.append("token"),
            ),
            patch(
                "cli_agent_orchestrator.api.main.init_db",
                side_effect=lambda: touched.append("db"),
            ),
            patch("cli_agent_orchestrator.api.main.bus.set_loop", new=AsyncMock()),
        ):
            with pytest.raises(SystemExit) as exc:
                async with lifespan(app):
                    pass
        assert exc.value.code == 1
        assert touched == []

    def test_an_imported_app_refuses_to_start_too(self, remote, monkeypatch, capsys):
        """``uvicorn cli_agent_orchestrator.api.main:app`` runs the same lifespan.

        The test client surfaces the lifespan's SystemExit as a cancellation;
        uvicorn exits non-zero on it.
        """
        import concurrent.futures

        from fastapi.testclient import TestClient

        monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", "a" * 32)
        touched = []
        with (
            patch("cli_agent_orchestrator.api.main.setup_logging"),
            patch(
                "cli_agent_orchestrator.api.main.server_lock.acquire_server_lock",
                side_effect=lambda **k: touched.append("lock"),
            ),
        ):
            with pytest.raises((SystemExit, concurrent.futures.CancelledError)):
                with TestClient(app):
                    pass
        assert touched == []
        assert "does not support API authentication" in capsys.readouterr().err


class TestRunnerToken:
    """Without a runner token the server says what to set, not a traceback (#745)."""

    @pytest.mark.asyncio
    async def test_the_lifespan_refuses_with_a_directed_message(self, remote, capsys):
        from cli_agent_orchestrator.backends.remote_backend import RemoteBackend

        reached = []
        with (
            patch("cli_agent_orchestrator.api.main.setup_logging"),
            patch("cli_agent_orchestrator.api.main.install_access_log_redaction"),
            patch("cli_agent_orchestrator.api.main.server_lock.acquire_server_lock"),
            patch(
                "cli_agent_orchestrator.api.main.get_backend",
                return_value=RemoteBackend.__new__(RemoteBackend),
            ),
            patch("cli_agent_orchestrator.api.main.runner_token", return_value=None),
            patch(
                "cli_agent_orchestrator.api.main.init_db",
                side_effect=lambda: reached.append("db"),
            ),
            patch("cli_agent_orchestrator.api.main.bus.set_loop", new=AsyncMock()),
        ):
            with pytest.raises(SystemExit) as exc:
                async with lifespan(app):
                    pass
        assert exc.value.code == 1
        assert reached == []
        err = capsys.readouterr().err
        assert "terminal.backend=remote requires a runner token" in err
        assert "CAO_RUNNER_TOKEN_FILE" in err and "CAO_RUNNER_TOKEN" in err
