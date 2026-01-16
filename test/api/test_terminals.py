"""Tests for terminal-related API endpoints including working directory."""

from unittest.mock import patch

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
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
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
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
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
