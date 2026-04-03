"""Tests for orchestrator launch/status/stop endpoints."""
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from cli_agent_orchestrator.api.main import app
    return TestClient(app, headers={"host": "localhost"})


class TestOrchestratorLaunch:
    def test_launch_returns_session_info(self, client):
        mock_terminal = MagicMock()
        mock_terminal.session_name = "cao-orch-1"
        mock_terminal.id = "term-orch"
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_ts:
            mock_ts.create_terminal.return_value = mock_terminal
            # Reset global state
            import cli_agent_orchestrator.api.main as m
            m._orchestrator_session_id = None

            res = client.post("/orchestrator/launch?provider=claude_code")
            assert res.status_code == 201
            data = res.json()
            assert data["session_id"] == "cao-orch-1"
            assert data["terminal_id"] == "term-orch"
            assert data["status"] == "launched"

    def test_launch_already_running_returns_existing(self, client):
        import cli_agent_orchestrator.api.main as m
        m._orchestrator_session_id = "cao-existing"
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_ss:
            mock_ss.get_session.return_value = {"session": {"id": "cao-existing"}}
            res = client.post("/orchestrator/launch")
            assert res.status_code == 201
            assert res.json()["status"] == "already_running"
        m._orchestrator_session_id = None


class TestOrchestratorStatus:
    def test_status_not_running(self, client):
        import cli_agent_orchestrator.api.main as m
        m._orchestrator_session_id = None
        res = client.get("/orchestrator/status")
        assert res.json() == {"running": False, "session_id": None}

    def test_status_running(self, client):
        import cli_agent_orchestrator.api.main as m
        m._orchestrator_session_id = "cao-orch"
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_ss:
            mock_ss.get_session.return_value = {"session": {"id": "cao-orch"}}
            res = client.get("/orchestrator/status")
            data = res.json()
            assert data["running"] is True
            assert data["session_id"] == "cao-orch"
        m._orchestrator_session_id = None

    def test_status_stale_reference(self, client):
        import cli_agent_orchestrator.api.main as m
        m._orchestrator_session_id = "cao-dead"
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_ss:
            mock_ss.get_session.side_effect = ValueError("not found")
            res = client.get("/orchestrator/status")
            assert res.json()["running"] is False
        m._orchestrator_session_id = None


class TestOrchestratorStop:
    def test_stop_when_running(self, client):
        import cli_agent_orchestrator.api.main as m
        m._orchestrator_session_id = "cao-stop"
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_ss:
            mock_ss.delete_session.return_value = {"success": True}
            res = client.delete("/orchestrator/stop")
            assert res.json()["success"] is True
            assert m._orchestrator_session_id is None

    def test_stop_when_not_running(self, client):
        import cli_agent_orchestrator.api.main as m
        m._orchestrator_session_id = None
        res = client.delete("/orchestrator/stop")
        assert res.json()["success"] is True
