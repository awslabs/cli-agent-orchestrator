"""Tests for flow management API endpoints."""

from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.models.flow import Flow


@pytest.fixture
def client():
    """Create a test client."""
    return TestClient(app)


@pytest.fixture
def sample_flow():
    """Create a sample Flow for testing."""
    return Flow(
        name="test-flow",
        file_path="/path/to/test-flow.flow.md",
        schedule="0 * * * *",
        agent_profile="developer",
        provider="kiro_cli",
        script="",
        last_run=None,
        next_run=datetime(2026, 4, 1, 12, 0, 0),
        enabled=True,
    )


@pytest.fixture
def sample_flows(sample_flow):
    """Create a list of sample Flows for testing."""
    return [
        sample_flow,
        Flow(
            name="second-flow",
            file_path="/path/to/second-flow.flow.md",
            schedule="0 9 * * 1-5",
            agent_profile="reviewer",
            provider="claude_code",
            script="",
            last_run=datetime(2026, 3, 9, 9, 0, 0),
            next_run=datetime(2026, 3, 10, 9, 0, 0),
            enabled=False,
        ),
    ]


class TestListFlows:
    """Tests for GET /flows endpoint."""

    def test_list_flows_returns_all(self, client, sample_flows):
        """GET /flows returns a list of all flows."""
        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.list_flows.return_value = sample_flows

            response = client.get("/flows")

            assert response.status_code == 200
            data = response.json()
            assert len(data) == 2
            assert data[0]["name"] == "test-flow"
            assert data[1]["name"] == "second-flow"

    def test_list_flows_empty(self, client):
        """GET /flows returns empty list when no flows exist."""
        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.list_flows.return_value = []

            response = client.get("/flows")

            assert response.status_code == 200
            assert response.json() == []

    def test_list_flows_server_error(self, client):
        """GET /flows returns 500 on internal error."""
        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.list_flows.side_effect = Exception("DB connection failed")

            response = client.get("/flows")

            assert response.status_code == 500
            assert "Failed to list flows" in response.json()["detail"]


class TestGetFlow:
    """Tests for GET /flows/{name} endpoint."""

    def test_get_flow_found(self, client, sample_flow):
        """GET /flows/{name} returns the specified flow."""
        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.get_flow.return_value = sample_flow

            response = client.get("/flows/test-flow")

            assert response.status_code == 200
            data = response.json()
            assert data["name"] == "test-flow"
            assert data["schedule"] == "0 * * * *"
            assert data["agent_profile"] == "developer"
            mock_svc.get_flow.assert_called_once_with("test-flow")

    def test_get_flow_not_found(self, client):
        """GET /flows/{name} returns 404 for nonexistent flow."""
        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.get_flow.side_effect = ValueError("Flow 'nonexistent' not found")

            response = client.get("/flows/nonexistent")

            assert response.status_code == 404
            assert "not found" in response.json()["detail"]

    def test_get_flow_server_error(self, client):
        """GET /flows/{name} returns 500 on internal error."""
        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.get_flow.side_effect = Exception("Unexpected error")

            response = client.get("/flows/test-flow")

            assert response.status_code == 500
            assert "Failed to get flow" in response.json()["detail"]


class TestCreateFlow:
    """Tests for POST /flows endpoint."""

    def test_create_flow_returns_201(self, client, sample_flow):
        """POST /flows creates a flow and returns 201."""
        with (
            patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc,
            patch("cli_agent_orchestrator.api.main.CAO_HOME_DIR") as mock_home,
        ):
            mock_home.__truediv__ = lambda self, x: mock_home
            mock_home.mkdir = lambda **kwargs: None
            mock_home.__truediv__ = lambda self, x: type(
                "FakePath",
                (),
                {
                    "mkdir": lambda self, **kw: None,
                    "__truediv__": lambda self, x: type(
                        "FakeFile",
                        (),
                        {"write_text": lambda self, t: None},
                    )(),
                },
            )()

            mock_svc.add_flow.return_value = sample_flow

            response = client.post(
                "/flows",
                json={
                    "name": "test-flow",
                    "schedule": "0 * * * *",
                    "agent_profile": "developer",
                    "provider": "kiro_cli",
                    "prompt_template": "Do some work.",
                },
            )

            assert response.status_code == 201
            data = response.json()
            assert data["name"] == "test-flow"

    def test_create_flow_server_error(self, client):
        """POST /flows returns 500 on internal error."""
        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.add_flow.side_effect = Exception("DB error")

            # The endpoint writes a file first, so patch CAO_HOME_DIR too
            with patch("cli_agent_orchestrator.api.main.CAO_HOME_DIR") as mock_home:
                mock_home.__truediv__ = lambda self, x: mock_home
                mock_home.mkdir = lambda **kwargs: None
                mock_home.__truediv__ = lambda self, x: type(
                    "FakePath",
                    (),
                    {
                        "mkdir": lambda self, **kw: None,
                        "__truediv__": lambda self, x: type(
                            "FakeFile",
                            (),
                            {"write_text": lambda self, t: None},
                        )(),
                    },
                )()

                response = client.post(
                    "/flows",
                    json={
                        "name": "fail-flow",
                        "schedule": "0 * * * *",
                        "agent_profile": "developer",
                        "provider": "kiro_cli",
                        "prompt_template": "Do work.",
                    },
                )

                assert response.status_code == 500


class TestDeleteFlow:
    """Tests for DELETE /flows/{name} endpoint."""

    def test_delete_flow_success(self, client):
        """DELETE /flows/{name} removes a flow and returns success."""
        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.remove_flow.return_value = True

            response = client.delete("/flows/test-flow")

            assert response.status_code == 200
            assert response.json() == {"success": True}
            mock_svc.remove_flow.assert_called_once_with("test-flow")

    def test_delete_flow_not_found(self, client):
        """DELETE /flows/{name} returns 404 for nonexistent flow."""
        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.remove_flow.side_effect = ValueError("Flow 'nonexistent' not found")

            response = client.delete("/flows/nonexistent")

            assert response.status_code == 404
            assert "not found" in response.json()["detail"]

    def test_delete_flow_server_error(self, client):
        """DELETE /flows/{name} returns 500 on internal error."""
        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.remove_flow.side_effect = Exception("DB error")

            response = client.delete("/flows/test-flow")

            assert response.status_code == 500
            assert "Failed to remove flow" in response.json()["detail"]


class TestEnableFlow:
    """Tests for POST /flows/{name}/enable endpoint."""

    def test_enable_flow_success(self, client):
        """POST /flows/{name}/enable enables a flow and returns success."""
        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.enable_flow.return_value = True

            response = client.post("/flows/test-flow/enable")

            assert response.status_code == 200
            assert response.json() == {"success": True}
            mock_svc.enable_flow.assert_called_once_with("test-flow")

    def test_enable_flow_not_found(self, client):
        """POST /flows/{name}/enable returns 404 for nonexistent flow."""
        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.enable_flow.side_effect = ValueError("Flow 'nonexistent' not found")

            response = client.post("/flows/nonexistent/enable")

            assert response.status_code == 404
            assert "not found" in response.json()["detail"]

    def test_enable_flow_server_error(self, client):
        """POST /flows/{name}/enable returns 500 on internal error."""
        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.enable_flow.side_effect = Exception("Internal error")

            response = client.post("/flows/test-flow/enable")

            assert response.status_code == 500
            assert "Failed to enable flow" in response.json()["detail"]


class TestDisableFlow:
    """Tests for POST /flows/{name}/disable endpoint."""

    def test_disable_flow_success(self, client):
        """POST /flows/{name}/disable disables a flow and returns success."""
        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.disable_flow.return_value = True

            response = client.post("/flows/test-flow/disable")

            assert response.status_code == 200
            assert response.json() == {"success": True}
            mock_svc.disable_flow.assert_called_once_with("test-flow")

    def test_disable_flow_not_found(self, client):
        """POST /flows/{name}/disable returns 404 for nonexistent flow."""
        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.disable_flow.side_effect = ValueError("Flow 'nonexistent' not found")

            response = client.post("/flows/nonexistent/disable")

            assert response.status_code == 404
            assert "not found" in response.json()["detail"]

    def test_disable_flow_server_error(self, client):
        """POST /flows/{name}/disable returns 500 on internal error."""
        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.disable_flow.side_effect = Exception("Internal error")

            response = client.post("/flows/test-flow/disable")

            assert response.status_code == 500
            assert "Failed to disable flow" in response.json()["detail"]


class TestRunFlow:
    """Tests for POST /flows/{name}/run endpoint."""

    def test_run_flow_success(self, client):
        """POST /flows/{name}/run executes a flow and returns result."""
        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.execute_flow.return_value = True

            response = client.post("/flows/test-flow/run")

            assert response.status_code == 200
            assert response.json() == {"executed": True}
            mock_svc.execute_flow.assert_called_once_with("test-flow")

    def test_run_flow_skipped(self, client):
        """POST /flows/{name}/run returns executed=false when script says no."""
        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.execute_flow.return_value = False

            response = client.post("/flows/test-flow/run")

            assert response.status_code == 200
            assert response.json() == {"executed": False}

    def test_run_flow_not_found(self, client):
        """POST /flows/{name}/run returns 404 for nonexistent flow."""
        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.execute_flow.side_effect = ValueError("Flow 'nonexistent' not found")

            response = client.post("/flows/nonexistent/run")

            assert response.status_code == 404
            assert "not found" in response.json()["detail"]

    def test_run_flow_server_error(self, client):
        """POST /flows/{name}/run returns 500 on internal error."""
        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.execute_flow.side_effect = Exception("Execution failed")

            response = client.post("/flows/test-flow/run")

            assert response.status_code == 500
            assert "Failed to execute flow" in response.json()["detail"]
