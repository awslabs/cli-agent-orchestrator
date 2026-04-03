"""Tests for orchestrator launch/status/stop endpoints."""
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from cli_agent_orchestrator.api.main import app
    return TestClient(app, headers={"host": "localhost"})


@pytest.fixture(autouse=True)
def reset_orchestrator_state():
    """Reset orchestrator state before each test."""
    import cli_agent_orchestrator.api.main as m
    m._orchestrator_session_id = None
    yield
    m._orchestrator_session_id = None


class TestOrchestratorLaunch:
    def test_launch_returns_session_info(self, client):
        mock_terminal = MagicMock()
        mock_terminal.session_name = "cao-orch-1"
        mock_terminal.id = "term-orch"
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_ts, \
             patch("cli_agent_orchestrator.api.main._find_existing_orchestrator", return_value=None):
            mock_ts.create_terminal.return_value = mock_terminal
            res = client.post("/orchestrator/launch?provider=claude_code")
            assert res.status_code == 201
            data = res.json()
            assert data["session_id"] == "cao-orch-1"
            assert data["status"] == "launched"

    def test_launch_already_running_returns_existing(self, client):
        import cli_agent_orchestrator.api.main as m
        m._orchestrator_session_id = "cao-existing"
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_ss:
            mock_ss.get_session.return_value = {"session": {"id": "cao-existing"}}
            res = client.post("/orchestrator/launch")
            assert res.status_code == 201
            assert res.json()["status"] == "already_running"

    def test_launch_reconnects_to_orphaned_session(self, client):
        """If an orchestrator session exists in tmux but server forgot about it, reconnect."""
        with patch("cli_agent_orchestrator.api.main._find_existing_orchestrator", return_value="cao-orphan"):
            res = client.post("/orchestrator/launch")
            assert res.status_code == 201
            data = res.json()
            assert data["session_id"] == "cao-orphan"
            assert data["status"] == "reconnected"


class TestOrchestratorStatus:
    def test_status_not_running(self, client):
        with patch("cli_agent_orchestrator.api.main._find_existing_orchestrator", return_value=None):
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

    def test_status_finds_orphaned_session(self, client):
        """Status check discovers an orchestrator that survived a server restart."""
        with patch("cli_agent_orchestrator.api.main._find_existing_orchestrator", return_value="cao-found"):
            res = client.get("/orchestrator/status")
            data = res.json()
            assert data["running"] is True
            assert data["session_id"] == "cao-found"

    def test_status_stale_reference_falls_back_to_scan(self, client):
        import cli_agent_orchestrator.api.main as m
        m._orchestrator_session_id = "cao-dead"
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_ss, \
             patch("cli_agent_orchestrator.api.main._find_existing_orchestrator", return_value=None):
            mock_ss.get_session.side_effect = ValueError("not found")
            res = client.get("/orchestrator/status")
            assert res.json()["running"] is False


class TestOrchestratorStop:
    def test_stop_when_running(self, client):
        import cli_agent_orchestrator.api.main as m
        m._orchestrator_session_id = "cao-stop"
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_ss, \
             patch("cli_agent_orchestrator.api.main._find_existing_orchestrator", return_value=None):
            mock_ss.delete_session.return_value = {"success": True}
            res = client.delete("/orchestrator/stop")
            data = res.json()
            assert data["success"] is True
            assert "cao-stop" in data["stopped"]
            assert m._orchestrator_session_id is None

    def test_stop_cleans_up_orphans(self, client):
        """Stop also finds and kills orphaned orchestrator sessions."""
        call_count = {"n": 0}
        def find_mock():
            call_count["n"] += 1
            return "cao-orphan" if call_count["n"] == 1 else None

        with patch("cli_agent_orchestrator.api.main.session_service") as mock_ss, \
             patch("cli_agent_orchestrator.api.main._find_existing_orchestrator", side_effect=find_mock):
            mock_ss.delete_session.return_value = {"success": True}
            res = client.delete("/orchestrator/stop")
            data = res.json()
            assert data["success"] is True
            assert "cao-orphan" in data["stopped"]

    def test_stop_when_not_running(self, client):
        with patch("cli_agent_orchestrator.api.main._find_existing_orchestrator", return_value=None):
            res = client.delete("/orchestrator/stop")
            assert res.json()["success"] is True


class TestFindExistingOrchestrator:
    def test_finds_orchestrator_by_agent_profile(self):
        from cli_agent_orchestrator.api.main import _find_existing_orchestrator
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_ss:
            mock_ss.list_sessions.return_value = [{"id": "cao-s1"}, {"id": "cao-s2"}]
            mock_ss.get_session.side_effect = [
                {"terminals": [{"agent_profile": "developer"}]},
                {"terminals": [{"agent_profile": "master_orchestrator"}]},
            ]
            result = _find_existing_orchestrator()
            assert result == "cao-s2"

    def test_returns_none_when_no_orchestrator(self):
        from cli_agent_orchestrator.api.main import _find_existing_orchestrator
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_ss:
            mock_ss.list_sessions.return_value = [{"id": "cao-s1"}]
            mock_ss.get_session.return_value = {"terminals": [{"agent_profile": "developer"}]}
            assert _find_existing_orchestrator() is None

    def test_handles_empty_sessions(self):
        from cli_agent_orchestrator.api.main import _find_existing_orchestrator
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_ss:
            mock_ss.list_sessions.return_value = []
            assert _find_existing_orchestrator() is None
