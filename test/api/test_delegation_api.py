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
