"""Tests for terminal-related API endpoints including working directory and exit."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.models.terminal import Terminal


@pytest.fixture
def client():
    """Create a test client."""
    return TestClient(app)


class TestWorkingDirectoryEndpoint:
    """Test GET /terminals/{terminal_id}/working-directory endpoint."""

    def test_get_working_directory_success(self, client):
        """Test successful retrieval of working directory."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_working_directory.return_value = "/home/user/project"

            response = client.get("/terminals/abcd1234/working-directory")

            assert response.status_code == 200
            data = response.json()
            assert data["working_directory"] == "/home/user/project"
            mock_svc.get_working_directory.assert_called_once_with("abcd1234")

    def test_get_working_directory_returns_none(self, client):
        """Test when working directory is unavailable."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_working_directory.return_value = None

            response = client.get("/terminals/abcd1234/working-directory")

            assert response.status_code == 200
            assert response.json()["working_directory"] is None

    def test_get_working_directory_terminal_not_found(self, client):
        """Test 404 when terminal doesn't exist."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_working_directory.side_effect = ValueError("Terminal 'abcd5678' not found")

            response = client.get("/terminals/abcd5678/working-directory")

            assert response.status_code == 404
            assert "not found" in response.json()["detail"].lower()

    def test_get_working_directory_server_error(self, client):
        """Test 500 on internal error."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_working_directory.side_effect = Exception("TMux error")

            response = client.get("/terminals/abcd1234/working-directory")

            assert response.status_code == 500
            assert "Failed to get working directory" in response.json()["detail"]

    def test_get_working_directory_internal_error(self, client):
        """Test 500 when internal error occurs."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_working_directory.side_effect = RuntimeError("Internal service error")

            response = client.get("/terminals/abcd1234/working-directory")

            assert response.status_code == 500
            assert "Failed to get working directory" in response.json()["detail"]


class TestSessionCreationWithWorkingDirectory:
    """Test session creation with working_directory parameter."""

    def test_create_session_passes_working_directory(self, client, tmp_path):
        """Test that working_directory parameter is passed to service."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.create_terminal.return_value = Terminal(
                id="abcd1234",
                name="test-window",
                session_name="test-session",
                provider="q_cli",
                agent_profile="developer",
            )

            response = client.post(
                "/sessions",
                params={
                    "provider": "q_cli",
                    "agent_profile": "developer",
                    "working_directory": str(tmp_path),
                },
            )

            assert response.status_code == 201
            # Verify working_directory was passed
            call_kwargs = mock_svc.create_terminal.call_args.kwargs
            assert call_kwargs.get("working_directory") == str(tmp_path)

    def test_create_session_with_working_directory(self, client):
        """Test POST /sessions with working_directory parameter."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.create_terminal.return_value = Terminal(
                id="abcd1234",
                name="test-window",
                session_name="test-session",
                provider="q_cli",
                agent_profile="developer",
            )

            response = client.post(
                "/sessions",
                params={
                    "provider": "q_cli",
                    "agent_profile": "developer",
                    "working_directory": "/custom/path",
                },
            )

            assert response.status_code == 201
            call_kwargs = mock_svc.create_terminal.call_args.kwargs
            assert call_kwargs.get("working_directory") == "/custom/path"


class TestTerminalCreationWithWorkingDirectory:
    """Test terminal creation with working_directory parameter."""

    def test_create_terminal_passes_working_directory(self, client, tmp_path):
        """Test that working_directory parameter is passed to service."""
        with (
            patch("cli_agent_orchestrator.api.main.resolve_provider", side_effect=lambda _, fallback_provider: fallback_provider),
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
        ):
            mock_svc.create_terminal.return_value = Terminal(
                id="abcd5678",
                name="test-window",
                session_name="test-session",
                provider="q_cli",
                agent_profile="analyst",
            )

            response = client.post(
                "/sessions/test-session/terminals",
                params={
                    "provider": "q_cli",
                    "agent_profile": "analyst",
                    "working_directory": str(tmp_path),
                },
            )

            assert response.status_code == 201
            call_kwargs = mock_svc.create_terminal.call_args.kwargs
            assert call_kwargs.get("working_directory") == str(tmp_path)

    def test_create_terminal_in_session_with_working_directory(self, client):
        """Test POST /sessions/{session}/terminals with working_directory."""
        with (
            patch("cli_agent_orchestrator.api.main.resolve_provider", side_effect=lambda _, fallback_provider: fallback_provider),
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
        ):
            mock_svc.create_terminal.return_value = Terminal(
                id="abcd5678",
                name="test-window",
                session_name="test-session",
                provider="q_cli",
                agent_profile="analyst",
            )

            response = client.post(
                "/sessions/test-session/terminals",
                params={
                    "provider": "q_cli",
                    "agent_profile": "analyst",
                    "working_directory": "/session/path",
                },
            )

            assert response.status_code == 201
            call_kwargs = mock_svc.create_terminal.call_args.kwargs
            assert call_kwargs.get("working_directory") == "/session/path"


class TestExitTerminalEndpoint:
    """Test POST /terminals/{terminal_id}/exit endpoint.

    Verifies that text commands (e.g., /exit) are sent via send_input()
    and tmux special key sequences (e.g., C-d) are sent via send_special_key().
    """

    def test_exit_terminal_text_command(self, client):
        """Text exit commands (e.g., /exit) should use send_input."""
        mock_provider = MagicMock()
        mock_provider.exit_cli.return_value = "/exit"

        with (
            patch("cli_agent_orchestrator.api.main.provider_manager") as mock_pm,
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
        ):
            mock_pm.get_provider.return_value = mock_provider

            response = client.post("/terminals/abcd1234/exit")

            assert response.status_code == 200
            assert response.json() == {"success": True}
            mock_svc.send_input.assert_called_once_with("abcd1234", "/exit")
            mock_svc.send_special_key.assert_not_called()

    def test_exit_terminal_special_key(self, client):
        """Tmux key sequences (e.g., C-d) should use send_special_key."""
        mock_provider = MagicMock()
        mock_provider.exit_cli.return_value = "C-d"

        with (
            patch("cli_agent_orchestrator.api.main.provider_manager") as mock_pm,
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
        ):
            mock_pm.get_provider.return_value = mock_provider

            response = client.post("/terminals/abcd1234/exit")

            assert response.status_code == 200
            assert response.json() == {"success": True}
            mock_svc.send_special_key.assert_called_once_with("abcd1234", "C-d")
            mock_svc.send_input.assert_not_called()

    def test_exit_terminal_meta_key(self, client):
        """Meta key sequences (M-x) should also use send_special_key."""
        mock_provider = MagicMock()
        mock_provider.exit_cli.return_value = "M-x"

        with (
            patch("cli_agent_orchestrator.api.main.provider_manager") as mock_pm,
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
        ):
            mock_pm.get_provider.return_value = mock_provider

            response = client.post("/terminals/abcd1234/exit")

            assert response.status_code == 200
            mock_svc.send_special_key.assert_called_once_with("abcd1234", "M-x")
            mock_svc.send_input.assert_not_called()

    def test_exit_terminal_provider_not_found(self, client):
        """Should return 404 when provider is not found."""
        with patch("cli_agent_orchestrator.api.main.provider_manager") as mock_pm:
            mock_pm.get_provider.side_effect = ValueError("Terminal not found in database")

            response = client.post("/terminals/deadbeef/exit")

            assert response.status_code == 404

    def test_exit_terminal_server_error(self, client):
        """Should return 500 on unexpected errors."""
        mock_provider = MagicMock()
        mock_provider.exit_cli.return_value = "/exit"

        with (
            patch("cli_agent_orchestrator.api.main.provider_manager") as mock_pm,
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
        ):
            mock_pm.get_provider.return_value = mock_provider
            mock_svc.send_input.side_effect = RuntimeError("TMux error")

            response = client.post("/terminals/abcd1234/exit")

            assert response.status_code == 500
            assert "Failed to exit terminal" in response.json()["detail"]


class TestCrossProviderResolution:
    """Test that create_terminal_in_session resolves provider from agent profile
    while create_session always uses the explicit provider parameter."""

    def test_create_terminal_uses_profile_provider(self, client):
        """create_terminal_in_session should resolve provider from agent profile."""
        with (
            patch("cli_agent_orchestrator.api.main.resolve_provider") as mock_resolve,
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
        ):
            mock_resolve.return_value = "claude_code"
            mock_svc.create_terminal.return_value = Terminal(
                id="abcd1234",
                name="test-window",
                session_name="test-session",
                provider="claude_code",
                agent_profile="developer",
            )

            response = client.post(
                "/sessions/test-session/terminals",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                },
            )

            assert response.status_code == 201
            # Verify resolve_provider was called with the fallback
            mock_resolve.assert_called_once_with("developer", fallback_provider="kiro_cli")
            # Verify terminal_service got the resolved provider
            call_kwargs = mock_svc.create_terminal.call_args.kwargs
            assert call_kwargs["provider"] == "claude_code"

    def test_create_terminal_falls_back_when_no_profile_provider(self, client):
        """create_terminal_in_session should use fallback when profile has no provider."""
        with (
            patch("cli_agent_orchestrator.api.main.resolve_provider") as mock_resolve,
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
        ):
            # resolve_provider returns the fallback (no profile provider key)
            mock_resolve.return_value = "kiro_cli"
            mock_svc.create_terminal.return_value = Terminal(
                id="abcd5678",
                name="test-window",
                session_name="test-session",
                provider="kiro_cli",
                agent_profile="reviewer",
            )

            response = client.post(
                "/sessions/test-session/terminals",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "reviewer",
                },
            )

            assert response.status_code == 201
            call_kwargs = mock_svc.create_terminal.call_args.kwargs
            assert call_kwargs["provider"] == "kiro_cli"

    def test_create_session_does_not_resolve_provider(self, client):
        """create_session should NOT call resolve_provider — CLI flag is the override."""
        with (
            patch("cli_agent_orchestrator.api.main.resolve_provider") as mock_resolve,
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
        ):
            mock_svc.create_terminal.return_value = Terminal(
                id="abcd1234",
                name="test-window",
                session_name="test-session",
                provider="kiro_cli",
                agent_profile="supervisor",
            )

            response = client.post(
                "/sessions",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "supervisor",
                },
            )

            assert response.status_code == 201
            # resolve_provider should NOT have been called
            mock_resolve.assert_not_called()
            # terminal_service should get the raw provider param
            call_kwargs = mock_svc.create_terminal.call_args.kwargs
            assert call_kwargs["provider"] == "kiro_cli"

    def test_create_terminal_returns_500_on_resolve_error(self, client):
        """Internal errors during provider resolution should return 500."""
        with (
            patch("cli_agent_orchestrator.api.main.resolve_provider") as mock_resolve,
            patch("cli_agent_orchestrator.api.main.terminal_service"),
        ):
            mock_resolve.side_effect = Exception("Unexpected filesystem error")

            response = client.post(
                "/sessions/test-session/terminals",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                },
            )

            assert response.status_code == 500
            assert "Failed to create terminal" in response.json()["detail"]
