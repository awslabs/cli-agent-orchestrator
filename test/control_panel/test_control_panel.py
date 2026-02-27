"""Tests for the control panel FastAPI interface layer."""

from unittest.mock import MagicMock, patch

import pytest
import requests
from fastapi.testclient import TestClient

from cli_agent_orchestrator.control_panel.main import app


@pytest.fixture
def client() -> TestClient:
    """Create a test client for the control panel app."""
    return TestClient(app)


def test_health_endpoint_success(client: TestClient) -> None:
    """Test health endpoint when cao-server is reachable."""
    with patch("cli_agent_orchestrator.control_panel.main.requests.get") as mock_get:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_get.return_value = mock_response

        response = client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["cao_server_status"] == "healthy"


def test_health_endpoint_cao_unreachable(client: TestClient) -> None:
    """Test health endpoint when cao-server is unreachable."""
    with patch("cli_agent_orchestrator.control_panel.main.requests.get") as mock_get:
        mock_get.side_effect = requests.exceptions.ConnectionError()

        response = client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["cao_server_status"] == "unreachable"


def test_proxy_get_request(client: TestClient) -> None:
    """Test proxying a GET request to cao-server."""
    with patch("cli_agent_orchestrator.control_panel.main.requests.request") as mock_request:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'{"result": "success"}'
        mock_response.headers = {"Content-Type": "application/json"}
        mock_request.return_value = mock_response

        response = client.get("/sessions")

        assert response.status_code == 200
        assert response.headers.get("x-request-id")
        call_headers = mock_request.call_args.kwargs["headers"]
        assert call_headers.get("X-Request-Id")
        mock_request.assert_called_once()


def test_proxy_post_request(client: TestClient) -> None:
    """Test proxying a POST request to cao-server."""
    with patch("cli_agent_orchestrator.control_panel.main.requests.request") as mock_request:
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.content = b'{"id": "test123"}'
        mock_response.headers = {"Content-Type": "application/json"}
        mock_request.return_value = mock_response

        response = client.post("/sessions", json={"agent_profile": "test", "provider": "kiro_cli"})

        assert response.status_code == 201
        mock_request.assert_called_once()


def test_proxy_handles_cao_server_error(client: TestClient) -> None:
    """Test proxy handles cao-server connection errors."""
    with patch("cli_agent_orchestrator.control_panel.main.requests.request") as mock_request:
        mock_request.side_effect = requests.exceptions.ConnectionError("Connection failed")

        response = client.get("/sessions")

        assert response.status_code == 502
        data = response.json()
        assert "Failed to reach cao-server" in data["detail"]


def test_proxy_forwards_query_parameters(client: TestClient) -> None:
    """Test proxy forwards query parameters to cao-server."""
    with patch("cli_agent_orchestrator.control_panel.main.requests.request") as mock_request:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"[]"
        mock_response.headers = {}
        mock_request.return_value = mock_response

        client.get("/sessions?limit=10&offset=20")

        call_args = mock_request.call_args
        assert "limit=10" in call_args.kwargs["url"]
        assert "offset=20" in call_args.kwargs["url"]


def test_proxy_delete_request(client: TestClient) -> None:
    """Test proxying a DELETE request to cao-server."""
    with patch("cli_agent_orchestrator.control_panel.main.requests.request") as mock_request:
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_response.content = b""
        mock_response.headers = {}
        mock_request.return_value = mock_response

        response = client.delete("/sessions/test-session")

        assert response.status_code == 204
        mock_request.assert_called_once()
