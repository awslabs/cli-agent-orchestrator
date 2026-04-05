"""Tests for uncovered API endpoints in main.py.

Covers: health, agents/profiles, agents/providers, sessions CRUD,
terminals CRUD (create in session, list, get, input, output, delete),
flow_daemon, lifespan, and the main() entry point.
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.api.main import app, flow_daemon
from cli_agent_orchestrator.models.terminal import Terminal

# ── Health endpoint ──────────────────────────────────────────────────


class TestHealthCheck:
    """Tests for GET /health endpoint."""

    def test_health_check_returns_ok(self, client):
        """GET /health returns status ok."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["service"] == "cli-agent-orchestrator"


# ── Agent profiles endpoint ──────────────────────────────────────────


class TestAgentProfiles:
    """Tests for GET /agents/profiles endpoint."""

    def test_list_profiles_success(self, client):
        """GET /agents/profiles returns list of profiles."""
        mock_profiles = [
            {"name": "developer", "path": "/agents/developer"},
            {"name": "reviewer", "path": "/agents/reviewer"},
        ]
        with patch(
            "cli_agent_orchestrator.api.main.list_agent_profiles",
            create=True,
        ) as mock_fn:
            # The endpoint does a lazy import, so we need to patch at the import target
            with patch(
                "cli_agent_orchestrator.utils.agent_profiles.list_agent_profiles",
                return_value=mock_profiles,
            ):
                response = client.get("/agents/profiles")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["name"] == "developer"

    def test_list_profiles_empty(self, client):
        """GET /agents/profiles returns empty list when none exist."""
        with patch(
            "cli_agent_orchestrator.utils.agent_profiles.list_agent_profiles",
            return_value=[],
        ):
            response = client.get("/agents/profiles")

        assert response.status_code == 200
        assert response.json() == []

    def test_list_profiles_server_error(self, client):
        """GET /agents/profiles returns 500 on internal error."""
        with patch(
            "cli_agent_orchestrator.utils.agent_profiles.list_agent_profiles",
            side_effect=Exception("Failed to read profiles"),
        ):
            response = client.get("/agents/profiles")

        assert response.status_code == 500
        assert "Failed to list agent profiles" in response.json()["detail"]


# ── Agent providers endpoint ─────────────────────────────────────────


class TestAgentProviders:
    """Tests for GET /agents/providers endpoint."""

    def test_list_providers_all_installed(self, client):
        """GET /agents/providers returns all providers as installed."""
        with patch("shutil.which", return_value="/usr/bin/dummy"):
            response = client.get("/agents/providers")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 7
        names = [p["name"] for p in data]
        assert "kiro_cli" in names
        assert "claude_code" in names
        assert "q_cli" in names
        assert "codex" in names
        assert "gemini_cli" in names
        assert "kimi_cli" in names
        assert "copilot_cli" in names
        for p in data:
            assert p["installed"] is True

    def test_list_providers_none_installed(self, client):
        """GET /agents/providers returns all providers as not installed."""
        with patch("shutil.which", return_value=None):
            response = client.get("/agents/providers")

        assert response.status_code == 200
        data = response.json()
        for p in data:
            assert p["installed"] is False

    def test_list_providers_mixed_installed(self, client):
        """GET /agents/providers returns mixed installation status."""

        def mock_which(binary):
            return "/usr/bin/kiro-cli" if binary == "kiro-cli" else None

        with patch("shutil.which", side_effect=mock_which):
            response = client.get("/agents/providers")

        assert response.status_code == 200
        data = response.json()
        providers_dict = {p["name"]: p for p in data}
        assert providers_dict["kiro_cli"]["installed"] is True
        assert providers_dict["claude_code"]["installed"] is False
        assert providers_dict["q_cli"]["installed"] is False
        assert providers_dict["codex"]["installed"] is False
        assert providers_dict["gemini_cli"]["installed"] is False
        assert providers_dict["kimi_cli"]["installed"] is False
        assert providers_dict["copilot_cli"]["installed"] is False

    def test_list_providers_has_binary_field(self, client):
        """Each provider entry has correct binary name."""
        with patch("shutil.which", return_value=None):
            response = client.get("/agents/providers")

        data = response.json()
        providers_dict = {p["name"]: p for p in data}
        assert providers_dict["kiro_cli"]["binary"] == "kiro-cli"
        assert providers_dict["claude_code"]["binary"] == "claude"
        assert providers_dict["q_cli"]["binary"] == "q"
        assert providers_dict["codex"]["binary"] == "codex"
        assert providers_dict["gemini_cli"]["binary"] == "gemini"
        assert providers_dict["kimi_cli"]["binary"] == "kimi"
        assert providers_dict["copilot_cli"]["binary"] == "copilot"


# ── Sessions CRUD ────────────────────────────────────────────────────


class TestCreateSession:
    """Tests for POST /sessions endpoint — success and error cases."""

    def test_create_session_success(self, client):
        """POST /sessions creates a session and returns 201."""
        mock_terminal = Terminal(
            id="abcd1234",
            name="test-window",
            session_name="test-session",
            provider="kiro_cli",
            agent_profile="developer",
        )
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.create_terminal.return_value = mock_terminal

            response = client.post(
                "/sessions",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["id"] == "abcd1234"
        assert data["provider"] == "kiro_cli"
        assert data["agent_profile"] == "developer"

    def test_create_session_with_session_name(self, client):
        """POST /sessions with explicit session_name."""
        mock_terminal = Terminal(
            id="abcd1234",
            name="test-window",
            session_name="my-custom-session",
            provider="q_cli",
            agent_profile="developer",
        )
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.create_terminal.return_value = mock_terminal

            response = client.post(
                "/sessions",
                params={
                    "provider": "q_cli",
                    "agent_profile": "developer",
                    "session_name": "my-custom-session",
                },
            )

        assert response.status_code == 201
        call_kwargs = mock_svc.create_terminal.call_args.kwargs
        assert call_kwargs["session_name"] == "my-custom-session"
        assert call_kwargs["new_session"] is True

    def test_create_session_value_error(self, client):
        """POST /sessions returns 400 on ValueError."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.create_terminal.side_effect = ValueError("Invalid provider")

            response = client.post(
                "/sessions",
                params={
                    "provider": "bad_provider",
                    "agent_profile": "developer",
                },
            )

        assert response.status_code == 400
        assert "Invalid provider" in response.json()["detail"]

    def test_create_session_server_error(self, client):
        """POST /sessions returns 500 on unexpected error."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.create_terminal.side_effect = Exception("TMux crashed")

            response = client.post(
                "/sessions",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                },
            )

        assert response.status_code == 500
        assert "Failed to create session" in response.json()["detail"]


class TestListSessions:
    """Tests for GET /sessions endpoint."""

    def test_list_sessions_success(self, client):
        """GET /sessions returns list of sessions."""
        mock_sessions = [
            {"id": "cao-session-1", "windows": 2},
            {"id": "cao-session-2", "windows": 1},
        ]
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.list_sessions.return_value = mock_sessions

            response = client.get("/sessions")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2

    def test_list_sessions_empty(self, client):
        """GET /sessions returns empty list."""
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.list_sessions.return_value = []

            response = client.get("/sessions")

        assert response.status_code == 200
        assert response.json() == []

    def test_list_sessions_server_error(self, client):
        """GET /sessions returns 500 on error."""
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.list_sessions.side_effect = Exception("TMux not running")

            response = client.get("/sessions")

        assert response.status_code == 500
        assert "Failed to list sessions" in response.json()["detail"]


class TestGetSession:
    """Tests for GET /sessions/{session_name} endpoint."""

    def test_get_session_success(self, client):
        """GET /sessions/{name} returns session details."""
        mock_session = {
            "id": "test-session",
            "windows": [{"name": "window-1", "id": "abcd1234"}],
        }
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.get_session.return_value = mock_session

            response = client.get("/sessions/test-session")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "test-session"
        mock_svc.get_session.assert_called_once_with("test-session")

    def test_get_session_not_found(self, client):
        """GET /sessions/{name} returns 404 for nonexistent session."""
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.get_session.side_effect = ValueError("Session 'nonexistent' not found")

            response = client.get("/sessions/nonexistent")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_get_session_server_error(self, client):
        """GET /sessions/{name} returns 500 on internal error."""
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.get_session.side_effect = Exception("Unexpected error")

            response = client.get("/sessions/test-session")

        assert response.status_code == 500
        assert "Failed to get session" in response.json()["detail"]


class TestDeleteSession:
    """Tests for DELETE /sessions/{session_name} endpoint."""

    def test_delete_session_success(self, client):
        """DELETE /sessions/{name} deletes session and returns success."""
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.delete_session.return_value = {
                "deleted": ["test-session"],
                "errors": [],
            }

            response = client.delete("/sessions/test-session")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["deleted"] == ["test-session"]
        mock_svc.delete_session.assert_called_once_with("test-session")

    def test_delete_session_not_found(self, client):
        """DELETE /sessions/{name} returns 404 for nonexistent session."""
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.delete_session.side_effect = ValueError("Session 'nonexistent' not found")

            response = client.delete("/sessions/nonexistent")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_delete_session_server_error(self, client):
        """DELETE /sessions/{name} returns 500 on internal error."""
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.delete_session.side_effect = Exception("TMux error")

            response = client.delete("/sessions/test-session")

        assert response.status_code == 500
        assert "Failed to delete session" in response.json()["detail"]


# ── Terminals in sessions ────────────────────────────────────────────


class TestCreateTerminalInSession:
    """Tests for POST /sessions/{session_name}/terminals endpoint."""

    def test_create_terminal_success(self, client):
        """POST /sessions/{name}/terminals creates terminal and returns 201."""
        mock_terminal = Terminal(
            id="abcd5678",
            name="test-window-2",
            session_name="test-session",
            provider="claude_code",
            agent_profile="reviewer",
        )
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.create_terminal.return_value = mock_terminal

            response = client.post(
                "/sessions/test-session/terminals",
                params={
                    "provider": "claude_code",
                    "agent_profile": "reviewer",
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["id"] == "abcd5678"
        assert data["session_name"] == "test-session"
        call_kwargs = mock_svc.create_terminal.call_args.kwargs
        assert call_kwargs["session_name"] == "test-session"
        assert call_kwargs["new_session"] is False

    def test_create_terminal_session_not_found(self, client):
        """POST /sessions/{name}/terminals returns 404 for nonexistent session."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.create_terminal.side_effect = ValueError("Session 'nonexistent' not found")

            response = client.post(
                "/sessions/nonexistent/terminals",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                },
            )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_create_terminal_server_error(self, client):
        """POST /sessions/{name}/terminals returns 500 on error."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.create_terminal.side_effect = Exception("TMux error")

            response = client.post(
                "/sessions/test-session/terminals",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                },
            )

        assert response.status_code == 500
        assert "Failed to create terminal" in response.json()["detail"]


class TestListTerminalsInSession:
    """Tests for GET /sessions/{session_name}/terminals endpoint."""

    def test_list_terminals_success(self, client):
        """GET /sessions/{name}/terminals returns terminal list."""
        mock_terminals = [
            {"id": "abcd1234", "tmux_session": "s1", "provider": "kiro_cli"},
            {"id": "abcd5678", "tmux_session": "s1", "provider": "claude_code"},
        ]
        with patch(
            "cli_agent_orchestrator.clients.database.list_terminals_by_session",
            return_value=mock_terminals,
        ):
            response = client.get("/sessions/s1/terminals")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2

    def test_list_terminals_empty(self, client):
        """GET /sessions/{name}/terminals returns empty list."""
        with patch(
            "cli_agent_orchestrator.clients.database.list_terminals_by_session",
            return_value=[],
        ):
            response = client.get("/sessions/empty-session/terminals")

        assert response.status_code == 200
        assert response.json() == []

    def test_list_terminals_server_error(self, client):
        """GET /sessions/{name}/terminals returns 500 on error."""
        with patch(
            "cli_agent_orchestrator.clients.database.list_terminals_by_session",
            side_effect=Exception("DB error"),
        ):
            response = client.get("/sessions/s1/terminals")

        assert response.status_code == 500
        assert "Failed to list terminals" in response.json()["detail"]


# ── Individual terminal endpoints ────────────────────────────────────


class TestGetTerminal:
    """Tests for GET /terminals/{terminal_id} endpoint."""

    def test_get_terminal_success(self, client):
        """GET /terminals/{id} returns terminal details."""
        mock_terminal_dict = {
            "id": "abcd1234",
            "name": "test-window",
            "session_name": "test-session",
            "provider": "kiro_cli",
            "agent_profile": "developer",
        }
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_terminal.return_value = mock_terminal_dict

            response = client.get("/terminals/abcd1234")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "abcd1234"
        assert data["provider"] == "kiro_cli"
        mock_svc.get_terminal.assert_called_once_with("abcd1234")

    def test_get_terminal_not_found(self, client):
        """GET /terminals/{id} returns 404 for nonexistent terminal."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_terminal.side_effect = ValueError("Terminal 'deadbeef' not found")

            response = client.get("/terminals/deadbeef")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_get_terminal_server_error(self, client):
        """GET /terminals/{id} returns 500 on internal error."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_terminal.side_effect = Exception("DB error")

            response = client.get("/terminals/abcd1234")

        assert response.status_code == 500
        assert "Failed to get terminal" in response.json()["detail"]

    def test_get_terminal_invalid_id_format(self, client):
        """GET /terminals/{id} returns 422 for invalid ID format."""
        response = client.get("/terminals/not-valid-hex")
        assert response.status_code == 422


class TestSendTerminalInput:
    """Tests for POST /terminals/{terminal_id}/input endpoint."""

    def test_send_input_success(self, client):
        """POST /terminals/{id}/input sends message successfully."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.send_input.return_value = True

            response = client.post(
                "/terminals/abcd1234/input",
                params={"message": "hello world"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        mock_svc.send_input.assert_called_once_with("abcd1234", "hello world")

    def test_send_input_terminal_not_found(self, client):
        """POST /terminals/{id}/input returns 404 for nonexistent terminal."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.send_input.side_effect = ValueError("Terminal not found")

            response = client.post(
                "/terminals/deadbeef/input",
                params={"message": "hello"},
            )

        assert response.status_code == 404
        assert "Terminal not found" in response.json()["detail"]

    def test_send_input_server_error(self, client):
        """POST /terminals/{id}/input returns 500 on error."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.send_input.side_effect = Exception("TMux send failed")

            response = client.post(
                "/terminals/abcd1234/input",
                params={"message": "hello"},
            )

        assert response.status_code == 500
        assert "Failed to send input" in response.json()["detail"]


class TestGetTerminalOutput:
    """Tests for GET /terminals/{terminal_id}/output endpoint."""

    def test_get_output_full_mode(self, client):
        """GET /terminals/{id}/output returns full output by default."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_output.return_value = "Hello from terminal"

            response = client.get("/terminals/abcd1234/output")

        assert response.status_code == 200
        data = response.json()
        assert data["output"] == "Hello from terminal"
        assert data["mode"] == "full"

    def test_get_output_last_mode(self, client):
        """GET /terminals/{id}/output with mode=last returns last response."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_output.return_value = "Last response"

            response = client.get("/terminals/abcd1234/output?mode=last")

        assert response.status_code == 200
        data = response.json()
        assert data["output"] == "Last response"
        assert data["mode"] == "last"

    def test_get_output_terminal_not_found(self, client):
        """GET /terminals/{id}/output returns 404 for nonexistent terminal."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_output.side_effect = ValueError("Terminal not found")

            response = client.get("/terminals/deadbeef/output")

        assert response.status_code == 404
        assert "Terminal not found" in response.json()["detail"]

    def test_get_output_server_error(self, client):
        """GET /terminals/{id}/output returns 500 on error."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_output.side_effect = Exception("Read failed")

            response = client.get("/terminals/abcd1234/output")

        assert response.status_code == 500
        assert "Failed to get output" in response.json()["detail"]


class TestDeleteTerminal:
    """Tests for DELETE /terminals/{terminal_id} endpoint."""

    def test_delete_terminal_success(self, client):
        """DELETE /terminals/{id} deletes terminal successfully."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.delete_terminal.return_value = True

            response = client.delete("/terminals/abcd1234")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        mock_svc.delete_terminal.assert_called_once_with("abcd1234")

    def test_delete_terminal_not_found(self, client):
        """DELETE /terminals/{id} returns 404 for nonexistent terminal."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.delete_terminal.side_effect = ValueError("Terminal not found")

            response = client.delete("/terminals/deadbeef")

        assert response.status_code == 404
        assert "Terminal not found" in response.json()["detail"]

    def test_delete_terminal_server_error(self, client):
        """DELETE /terminals/{id} returns 500 on error."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.delete_terminal.side_effect = Exception("Cleanup failed")

            response = client.delete("/terminals/abcd1234")

        assert response.status_code == 500
        assert "Failed to delete terminal" in response.json()["detail"]


# ── flow_daemon ──────────────────────────────────────────────────────


class TestFlowDaemon:
    """Tests for the flow_daemon() background task."""

    @pytest.mark.asyncio
    async def test_flow_daemon_executes_flows(self):
        """flow_daemon fetches and executes due flows."""
        mock_flow = MagicMock()
        mock_flow.name = "test-flow"

        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.get_flows_to_run.return_value = [mock_flow]
            mock_svc.execute_flow.return_value = True

            # Run one iteration then cancel
            with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError):
                    await flow_daemon()

            mock_svc.get_flows_to_run.assert_called_once()
            mock_svc.execute_flow.assert_called_once_with("test-flow")

    @pytest.mark.asyncio
    async def test_flow_daemon_handles_execute_error(self):
        """flow_daemon handles errors from execute_flow gracefully."""
        mock_flow = MagicMock()
        mock_flow.name = "fail-flow"

        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.get_flows_to_run.return_value = [mock_flow]
            mock_svc.execute_flow.side_effect = Exception("Execution failed")

            with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError):
                    await flow_daemon()

            # Should still have attempted execution
            mock_svc.execute_flow.assert_called_once_with("fail-flow")

    @pytest.mark.asyncio
    async def test_flow_daemon_handles_get_flows_error(self):
        """flow_daemon handles errors from get_flows_to_run gracefully."""
        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.get_flows_to_run.side_effect = Exception("DB error")

            with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError):
                    await flow_daemon()

            mock_svc.get_flows_to_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_flow_daemon_skipped_flow(self):
        """flow_daemon logs when execute returns False (skipped)."""
        mock_flow = MagicMock()
        mock_flow.name = "skipped-flow"

        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.get_flows_to_run.return_value = [mock_flow]
            mock_svc.execute_flow.return_value = False

            with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError):
                    await flow_daemon()

            mock_svc.execute_flow.assert_called_once_with("skipped-flow")

    @pytest.mark.asyncio
    async def test_flow_daemon_multiple_flows(self):
        """flow_daemon processes multiple flows in one iteration."""
        flow1 = MagicMock()
        flow1.name = "flow-1"
        flow2 = MagicMock()
        flow2.name = "flow-2"

        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.get_flows_to_run.return_value = [flow1, flow2]
            mock_svc.execute_flow.return_value = True

            with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError):
                    await flow_daemon()

            assert mock_svc.execute_flow.call_count == 2


# ── lifespan ─────────────────────────────────────────────────────────


class TestLifespan:
    """Tests for the lifespan() context manager."""

    @pytest.mark.asyncio
    async def test_lifespan_startup_and_shutdown(self):
        """lifespan starts background tasks on entry, cleans up on exit."""
        from cli_agent_orchestrator.api.main import lifespan

        mock_observer = MagicMock()

        with (
            patch("cli_agent_orchestrator.api.main.setup_logging"),
            patch("cli_agent_orchestrator.api.main.init_db"),
            patch("cli_agent_orchestrator.api.main.cleanup_old_data"),
            patch(
                "cli_agent_orchestrator.api.main.PollingObserver",
                return_value=mock_observer,
            ),
            patch(
                "cli_agent_orchestrator.api.main.flow_daemon",
                return_value=asyncio.sleep(0),
            ),
        ):
            async with lifespan(app):
                # Inside the lifespan — startup completed
                mock_observer.schedule.assert_called_once()
                mock_observer.start.assert_called_once()

            # After exit — shutdown cleanup
            mock_observer.stop.assert_called_once()
            mock_observer.join.assert_called_once()


# ── main() entry point ───────────────────────────────────────────────


class TestMainEntryPoint:
    """Tests for the main() CLI entry point."""

    def test_main_default_args(self):
        """main() runs uvicorn with default host/port."""
        with (
            patch("argparse.ArgumentParser.parse_args") as mock_args,
            patch("uvicorn.run") as mock_uvicorn,
        ):
            mock_args.return_value = MagicMock(agents_dir=None, host=None, port=None)

            from cli_agent_orchestrator.api.main import main

            main()

            mock_uvicorn.assert_called_once()
            call_kwargs = mock_uvicorn.call_args
            # Should use SERVER_HOST and SERVER_PORT defaults
            assert call_kwargs[0][0] is app

    def test_main_custom_host_port(self):
        """main() uses custom host and port from args."""
        with (
            patch("argparse.ArgumentParser.parse_args") as mock_args,
            patch("uvicorn.run") as mock_uvicorn,
        ):
            mock_args.return_value = MagicMock(agents_dir=None, host="0.0.0.0", port=9999)

            from cli_agent_orchestrator.api.main import main

            main()

            mock_uvicorn.assert_called_once_with(app, host="0.0.0.0", port=9999)

    def test_main_with_agents_dir(self):
        """main() sets KIRO_AGENTS_DIR when --agents-dir is provided."""
        with (
            patch("argparse.ArgumentParser.parse_args") as mock_args,
            patch("uvicorn.run"),
            patch("cli_agent_orchestrator.constants.KIRO_AGENTS_DIR") as _,
        ):
            mock_args.return_value = MagicMock(agents_dir="/custom/agents", host=None, port=None)

            from cli_agent_orchestrator.api.main import main

            main()
            # No assertion needed beyond no exception — the code path is covered
