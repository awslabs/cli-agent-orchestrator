"""HTTP surface of the delegation ceiling: 403 mapping, /health feature, workflow caller."""

from unittest.mock import AsyncMock, patch

from cli_agent_orchestrator.utils.delegation import REMOTE_FEATURE, DelegationCeilingError

_REFUSAL = DelegationCeilingError(
    "Delegation refused: terminal a1b2c3d4 (profile 'reviewer') may not launch 'developer' "
    "with tools it cannot delegate: execute_bash.",
    excess=["execute_bash"],
    parent_id="a1b2c3d4",
    child_profile="developer",
)


class TestCreateTerminalInSession:
    def test_over_wide_client_string_is_403_not_404(self, client):
        """The in-session path used to store the client's allowed_tools verbatim.
        The service now refuses; the route must surface that as 403 with the
        message, never as the generic ValueError 'not found' arm."""
        with patch(
            "cli_agent_orchestrator.api.main.terminal_service.create_terminal",
            new=AsyncMock(side_effect=_REFUSAL),
        ) as mock_create:
            response = client.post(
                "/sessions/cao-sess/terminals",
                params={
                    "agent_profile": "developer",
                    "caller_id": "a1b2c3d4",
                    "allowed_tools": "*",
                },
            )
        assert response.status_code == 403
        assert "Delegation refused" in response.json()["detail"]
        assert "execute_bash" in response.json()["detail"]
        # The client string reaches the service only to be checked there.
        assert mock_create.call_args.kwargs["allowed_tools"] == ["*"]
        assert mock_create.call_args.kwargs["caller_id"] == "a1b2c3d4"


class TestCreateSession:
    def test_refusal_is_403(self, client):
        with patch(
            "cli_agent_orchestrator.api.main.session_service.create_session",
            new=AsyncMock(side_effect=_REFUSAL),
        ):
            response = client.post(
                "/sessions",
                params={"agent_profile": "developer"},
                json={"caller_ceiling": ["fs_read"]},
            )
        assert response.status_code == 403
        assert "Delegation refused" in response.json()["detail"]

    def test_caller_ceiling_body_reaches_the_service(self, client):
        with patch(
            "cli_agent_orchestrator.api.main.session_service.create_session",
            new=AsyncMock(side_effect=_REFUSAL),
        ) as mock_create:
            client.post(
                "/sessions",
                params={"agent_profile": "developer"},
                json={"caller_ceiling": ["fs_read", "@cao-mcp-server"]},
            )
        assert mock_create.call_args.kwargs["caller_ceiling"] == ["fs_read", "@cao-mcp-server"]


class TestRunStep:
    def test_refusal_is_403(self, client):
        with patch(
            "cli_agent_orchestrator.api.main.run_agent_step",
            new=AsyncMock(side_effect=_REFUSAL),
        ) as mock_step:
            response = client.post(
                "/terminals/run-step",
                json={
                    "provider": "claude_code",
                    "agent": "developer",
                    "prompt": "go",
                    "caller_ceiling": ["fs_read"],
                },
            )
        assert response.status_code == 403
        assert "Delegation refused" in response.json()["detail"]
        assert mock_step.call_args.kwargs["caller_ceiling"] == ["fs_read"]


class TestHealth:
    def test_advertises_delegation_ceiling_feature(self, client):
        """Remote callers check this before placing work with target_host."""
        body = client.get("/health").json()
        assert REMOTE_FEATURE in body["features"]


class TestWorkflowCaller:
    def test_run_forwards_caller_id_to_start_run(self, client):
        spec = object()
        with (
            patch(
                "cli_agent_orchestrator.services.workflow_spec_service.get_workflow",
                return_value=spec,
            ),
            patch(
                "cli_agent_orchestrator.services.workflow_service.start_run",
                new=AsyncMock(side_effect=ValueError("stop here")),
            ) as mock_start,
        ):
            response = client.post(
                "/workflows/runs",
                json={"name_or_path": "wf", "inputs": {}, "caller_id": "a1b2c3d4"},
            )
        assert response.status_code == 400
        assert mock_start.call_args.kwargs["caller_id"] == "a1b2c3d4"

    def test_run_without_caller_stays_unbounded(self, client):
        spec = object()
        with (
            patch(
                "cli_agent_orchestrator.services.workflow_spec_service.get_workflow",
                return_value=spec,
            ),
            patch(
                "cli_agent_orchestrator.services.workflow_service.start_run",
                new=AsyncMock(side_effect=ValueError("stop here")),
            ) as mock_start,
        ):
            client.post("/workflows/runs", json={"name_or_path": "wf", "inputs": {}})
        assert "caller_id" not in mock_start.call_args.kwargs

    def test_malformed_caller_id_is_422(self, client):
        response = client.post(
            "/workflows/runs",
            json={"name_or_path": "wf", "inputs": {}, "caller_id": "not-a-terminal"},
        )
        assert response.status_code == 422

    def test_resume_forwards_caller_id(self, client):
        from types import SimpleNamespace

        with (
            patch(
                "cli_agent_orchestrator.services.workflow_journal.get_run",
                return_value=SimpleNamespace(tier="yaml"),
            ),
            patch(
                "cli_agent_orchestrator.services.workflow_service.resume_from_last_completed",
                new=AsyncMock(side_effect=KeyError("unknown run_id 'r1'")),
            ) as mock_resume,
        ):
            response = client.post("/workflows/runs/r1/resume", json={"caller_id": "a1b2c3d4"})
        assert response.status_code == 404
        assert mock_resume.call_args.kwargs["caller_id"] == "a1b2c3d4"


class TestReviewBIdentityAndSidecar:
    def test_in_session_create_without_caller_id_is_403(self, client):
        """Review B F3(a): omission used to take the operator branch (201,
        unbounded). An in-session create must name its delegating terminal."""
        with patch(
            "cli_agent_orchestrator.api.main.terminal_service.create_terminal",
            new=AsyncMock(),
        ) as mock_create:
            response = client.post(
                "/sessions/cao-sess/terminals",
                params={"agent_profile": "developer", "provider": "claude_code"},
            )
        assert response.status_code == 403
        assert "caller_id is required" in response.json()["detail"]
        mock_create.assert_not_called()

    def test_memory_sidecar_is_bounded_by_the_primary_row(self, client):
        """Review B F2: the sidecar create carries delegating_terminal_id=primary.id
        so its ceiling is capped by the primary's recorded ceiling."""
        from cli_agent_orchestrator.models.terminal import Terminal, TerminalStatus

        primary = Terminal(
            id="0000a1b2",
            name="w",
            provider="claude_code",
            session_name="cao-sess",
            agent_profile="reviewer",
            status=TerminalStatus.IDLE,
            last_active="2026-01-01T00:00:00Z",
        )
        with (
            patch(
                "cli_agent_orchestrator.api.main.session_service.create_session",
                new=AsyncMock(return_value=primary),
            ),
            patch("cli_agent_orchestrator.api.main.resolve_provider", return_value="claude_code"),
            patch(
                "cli_agent_orchestrator.services.terminal_service.create_terminal",
                new=AsyncMock(),
            ) as sidecar_create,
        ):
            response = client.post(
                "/sessions",
                params={"agent_profile": "reviewer", "memory_manager": "true"},
            )
        assert response.status_code == 201
        assert sidecar_create.call_args.kwargs["agent_profile"] == "memory_manager"
        assert sidecar_create.call_args.kwargs["delegating_terminal_id"] == "0000a1b2"


# ---------------------------------------------------------------------------
# Round 4 (reviews at 05786d86): the operator form of the in-session create,
# the memory sidecar's refusal made visible, and the YAML :submit caller.
# ---------------------------------------------------------------------------
import pytest  # noqa: E402

from cli_agent_orchestrator.clients.database import get_terminal_metadata  # noqa: E402
from cli_agent_orchestrator.constants import ROLE_TOOL_DEFAULTS  # noqa: E402
from cli_agent_orchestrator.models.agent_profile import AgentProfile  # noqa: E402
from cli_agent_orchestrator.models.terminal import Terminal, TerminalStatus  # noqa: E402

_TS = "cli_agent_orchestrator.services.terminal_service"
_OPERATOR_ROUTE = "/sessions/cao-sess/terminals:operator"
_PROFILES = {
    "developer": AgentProfile(name="developer", description="d", role="developer"),
    "sup": AgentProfile(name="sup", description="s", role="supervisor"),
}


@pytest.fixture
def real_create(monkeypatch, isolated_memory_db):
    """Real ``create_terminal`` against real rows; tmux, provider and FIFOs mocked."""
    from unittest.mock import MagicMock

    ids = iter(f"{n:08x}" for n in range(0xB0000000, 0xB00000FF))
    monkeypatch.setattr(f"{_TS}.generate_terminal_id", lambda: next(ids))
    monkeypatch.setattr(f"{_TS}.generate_window_name", lambda p: f"{p}-win")
    monkeypatch.setattr(f"{_TS}.agent_profiles.load_agent_profile", lambda name: _PROFILES[name])
    monkeypatch.setattr(f"{_TS}.status_monitor", MagicMock())
    monkeypatch.setattr(f"{_TS}.fifo_manager", MagicMock())
    fifo_dir = MagicMock()
    fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")
    monkeypatch.setattr(f"{_TS}.FIFO_DIR", fifo_dir)
    provider = AsyncMock()
    provider.initialize.return_value = True
    pm = MagicMock()
    pm.create_provider.return_value = provider
    monkeypatch.setattr(f"{_TS}.provider_manager", pm)
    backend = MagicMock()
    backend.session_exists.return_value = True
    backend.create_window.return_value = "win"
    backend.supports_event_inbox.return_value = True
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry._backend", backend)


class TestOperatorFormOfInSessionCreate:
    """P1 (both reviews): the Web UI's "Add Agent" needs an operator-shaped in-session create."""

    def test_operator_form_launches_and_records_the_declared_ceiling(self, client, real_create):
        """No caller: the operator branch, recorded like a POST /sessions launch."""
        response = client.post(
            _OPERATOR_ROUTE, params={"agent_profile": "developer", "provider": "claude_code"}
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["caller_id"] is None
        assert body["delegate_tools"] == ROLE_TOOL_DEFAULTS["developer"]
        row = get_terminal_metadata(body["id"])
        assert row["caller_id"] is None
        assert row["delegate_tools"] == ROLE_TOOL_DEFAULTS["developer"]

    def test_operator_form_records_a_supervisor_unbounded(self, client, real_create):
        """Same answer POST /sessions gives: the profile's declared ceiling stands."""
        response = client.post(
            _OPERATOR_ROUTE, params={"agent_profile": "sup", "provider": "claude_code"}
        )
        assert response.status_code == 201, response.text
        assert response.json()["delegate_tools"] == ["*"]
        assert get_terminal_metadata(response.json()["id"])["delegate_tools"] == ["*"]

    def test_operator_form_never_forwards_a_caller(self, client):
        created = Terminal(id="c0ffee01", name="w", provider="claude_code", session_name="cao-sess")
        with patch(f"{_TS}.create_terminal", new=AsyncMock(return_value=created)) as mock_create:
            client.post(
                _OPERATOR_ROUTE,
                params={"agent_profile": "developer", "provider": "claude_code"},
                json={"description": "added from the UI"},
            )
        kwargs = mock_create.call_args.kwargs
        assert not {"caller_id", "delegating_terminal_id", "caller_ceiling"} & set(kwargs)
        assert kwargs["new_session"] is False
        assert kwargs["session_name"] == "cao-sess"
        assert kwargs["description"] == "added from the UI"

    @pytest.mark.parametrize(
        "params, body, named",
        [
            ({"caller_id": "a1b2c3d4"}, None, "caller_id"),
            ({"delegating_terminal_id": "a1b2c3d4"}, None, "delegating_terminal_id"),
            ({}, {"caller_ceiling": ["fs_read"]}, "caller_ceiling"),
            ({}, {"caller_id": "a1b2c3d4"}, "caller_id"),
            (
                {},
                {"env_vars": {"CAO_CALLBACK_TERMINAL_ID": "a1b2c3d4"}},
                "env_vars.CAO_CALLBACK_TERMINAL_ID",
            ),
        ],
    )
    def test_operator_form_with_a_caller_identity_is_refused(self, client, params, body, named):
        """The laundering shape: operator path AND a caller identity. Refused, nothing created."""
        with patch(f"{_TS}.create_terminal", new=AsyncMock()) as mock_create:
            response = client.post(
                _OPERATOR_ROUTE,
                params={"agent_profile": "developer", "provider": "claude_code", **params},
                json=body,
            )
        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert "carries no caller identity" in detail
        assert named in detail
        assert "POST /sessions/{name}/terminals with caller_id" in detail
        mock_create.assert_not_called()

    def test_sibling_route_still_refuses_an_omitted_caller(self, client):
        """The operator form does not loosen the agents' route: omission stays 403."""
        with patch(f"{_TS}.create_terminal", new=AsyncMock()) as mock_create:
            response = client.post(
                "/sessions/cao-sess/terminals",
                params={"agent_profile": "developer", "provider": "claude_code"},
            )
        assert response.status_code == 403
        mock_create.assert_not_called()

    def test_operator_form_is_a_registered_post_route(self):
        """Scope gating itself is pinned structurally for EVERY route by
        test_scope_coverage.py and test/security/test_authz_matrix.py (which
        enumerate app.routes); this only pins that the route exists under the
        documented sub-path and method, so a rename shows up here by name."""
        from cli_agent_orchestrator.api.main import app

        paths = {(m, r.path) for r in app.routes for m in getattr(r, "methods", ())}
        assert ("POST", "/sessions/{session_name}/terminals:operator") in paths
        assert ("POST", "/sessions/{session_name}/terminals") in paths


def _primary(profile, tools):
    return Terminal(
        id="0000a1b2",
        name="w",
        provider="claude_code",
        session_name="cao-sess",
        agent_profile=profile,
        allowed_tools=tools,
        delegate_tools=tools,
        status=TerminalStatus.IDLE,
        last_active="2026-01-01T00:00:00Z",
    )


class TestMemorySidecarVisibility:
    """P3 #1: a refused sidecar must be visible to the operator, not only a log line."""

    @pytest.mark.parametrize(
        "profile, tools, missing",
        [
            ("workflow_scout", ROLE_TOOL_DEFAULTS["workflow_scout"], "fs_list"),
            (
                "tool_restrictions_custom_restricted",
                ["fs_read", "fs_list", "execute_bash"],
                "@cao-mcp-server",
            ),
        ],
    )
    def test_narrow_primary_refuses_the_sidecar_in_the_response(
        self, client, profile, tools, missing
    ):
        with (
            patch(
                "cli_agent_orchestrator.api.main.session_service.create_session",
                new=AsyncMock(return_value=_primary(profile, tools)),
            ),
            patch("cli_agent_orchestrator.api.main.resolve_provider", return_value="claude_code"),
            patch(f"{_TS}.create_terminal", new=AsyncMock()) as sidecar_create,
        ):
            response = client.post(
                "/sessions", params={"agent_profile": profile, "memory_manager": "true"}
            )
        assert response.status_code == 201, response.text
        outcome = response.json()["memory_manager"]
        assert outcome["state"] == "refused"
        assert missing in outcome["detail"]
        assert "memory_manager" in outcome["detail"]
        assert "cao launch --memory" in outcome["detail"]
        sidecar_create.assert_not_called()

    def test_covering_primary_schedules_the_sidecar(self, client):
        with (
            patch(
                "cli_agent_orchestrator.api.main.session_service.create_session",
                new=AsyncMock(return_value=_primary("reviewer", ROLE_TOOL_DEFAULTS["reviewer"])),
            ),
            patch("cli_agent_orchestrator.api.main.resolve_provider", return_value="claude_code"),
            patch(f"{_TS}.create_terminal", new=AsyncMock()) as sidecar_create,
        ):
            response = client.post(
                "/sessions", params={"agent_profile": "reviewer", "memory_manager": "true"}
            )
        assert response.status_code == 201
        assert response.json()["memory_manager"] == {"state": "scheduled", "detail": None}
        assert sidecar_create.call_args.kwargs["delegating_terminal_id"] == "0000a1b2"

    def test_no_sidecar_requested_reports_nothing(self, client):
        with patch(
            "cli_agent_orchestrator.api.main.session_service.create_session",
            new=AsyncMock(return_value=_primary("reviewer", ROLE_TOOL_DEFAULTS["reviewer"])),
        ):
            response = client.post("/sessions", params={"agent_profile": "reviewer"})
        assert response.status_code == 201
        assert response.json()["memory_manager"] is None


class TestYamlSubmitJournalsTheCaller:
    """Nit (both reviews): the YAML branch of :submit journals caller_id, like the script branch."""

    @pytest.fixture
    def yaml_submit(self, client, monkeypatch, tmp_path):
        from cli_agent_orchestrator.clients.database import (
            _migrate_workflow_run,
            _migrate_workflow_run_step,
        )
        from cli_agent_orchestrator.models.workflow import RunState, WorkflowSpec, WorkflowStep
        from cli_agent_orchestrator.models.workflow_runtime import WorkflowRunResult
        from cli_agent_orchestrator.services import workflow_journal, workflow_service

        monkeypatch.setattr(
            "cli_agent_orchestrator.constants.DATABASE_FILE", tmp_path / "wf.db", raising=True
        )
        _migrate_workflow_run()
        _migrate_workflow_run_step()
        workflow_service.run_registry.clear()
        workflow_service._active_drives.clear()
        spec = WorkflowSpec(
            name="wf",
            steps=[WorkflowStep(id="s1", provider="claude_code", agent="dev", prompt="go")],
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.workflow_spec_service.get_workflow",
            lambda name_or_path, scan_dir=None: spec,
        )

        async def _prepared(record):
            record.state = RunState.COMPLETED
            record.finished_at = workflow_service._now()
            workflow_journal.update_run_state(
                record.run_id, RunState.COMPLETED.value, record.finished_at
            )
            return WorkflowRunResult(
                run_id=record.run_id,
                workflow_name="wf",
                state=RunState.COMPLETED,
                started_at=record.started_at,
            )

        monkeypatch.setattr(workflow_service, "start_run_prepared", _prepared)
        yield workflow_journal, workflow_service
        workflow_service.run_registry.clear()
        workflow_service._active_drives.clear()

    def test_yaml_submit_journals_and_binds_the_caller(self, client, yaml_submit):
        journal, service = yaml_submit
        response = client.post(
            "/workflows/runs:submit",
            json={"name_or_path": "wf", "inputs": {}, "run_id": "sub-y", "caller_id": "a1b2c3d4"},
        )
        assert response.status_code == 202, response.text
        assert journal.get_run("sub-y").caller_id == "a1b2c3d4"
        assert service.run_registry["sub-y"].caller_id == "a1b2c3d4"

    def test_yaml_submit_without_a_caller_journals_null(self, client, yaml_submit):
        journal, service = yaml_submit
        response = client.post(
            "/workflows/runs:submit", json={"name_or_path": "wf", "inputs": {}, "run_id": "sub-o"}
        )
        assert response.status_code == 202, response.text
        assert journal.get_run("sub-o").caller_id is None
        assert service.run_registry["sub-o"].caller_id is None
