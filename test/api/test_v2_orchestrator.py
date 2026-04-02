"""Tests for master orchestrator launch endpoint."""
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from cli_agent_orchestrator.api.main import app
    return TestClient(app)


class TestLaunchOrchestrator:
    def test_launch_returns_session_info(self, client):
        mock_terminal = MagicMock()
        mock_terminal.session_name = "cao-orch-1"
        mock_terminal.id = "term-orch"
        with patch("cli_agent_orchestrator.api.v2.terminal_service") as mock_ts:
            mock_ts.create_terminal.return_value = mock_terminal
            res = client.post("/api/v2/orchestrator/launch", json={})
            assert res.status_code == 201
            data = res.json()
            assert data["session_id"] == "cao-orch-1"
            assert data["terminal_id"] == "term-orch"
            assert data["agent_profile"] == "master_orchestrator"
            assert data["provider"] == "claude_code"

    def test_launch_with_custom_provider(self, client):
        mock_terminal = MagicMock()
        mock_terminal.session_name = "cao-orch-2"
        mock_terminal.id = "term-orch-2"
        with patch("cli_agent_orchestrator.api.v2.terminal_service") as mock_ts:
            mock_ts.create_terminal.return_value = mock_terminal
            res = client.post("/api/v2/orchestrator/launch",
                              json={"provider": "kiro_cli"})
            data = res.json()
            assert data["provider"] == "kiro_cli"
            call_kwargs = mock_ts.create_terminal.call_args
            assert "kiro_cli" in str(call_kwargs)

    def test_launch_uses_master_orchestrator_profile_by_default(self, client):
        mock_terminal = MagicMock()
        mock_terminal.session_name = "cao-orch-3"
        mock_terminal.id = "term-orch-3"
        with patch("cli_agent_orchestrator.api.v2.terminal_service") as mock_ts:
            mock_ts.create_terminal.return_value = mock_terminal
            client.post("/api/v2/orchestrator/launch", json={})
            call_kwargs = mock_ts.create_terminal.call_args
            assert "master_orchestrator" in str(call_kwargs)


class TestOrchestratorAgentProfile:
    def test_agent_profile_exists(self):
        from pathlib import Path
        profile = Path(__file__).parent.parent.parent / "src" / "cli_agent_orchestrator" / "agent_store" / "master_orchestrator.md"
        assert profile.exists(), f"Agent profile not found at {profile}"

    def test_agent_profile_has_frontmatter(self):
        from pathlib import Path
        profile = Path(__file__).parent.parent.parent / "src" / "cli_agent_orchestrator" / "agent_store" / "master_orchestrator.md"
        content = profile.read_text()
        assert content.startswith("---")
        assert "name: master_orchestrator" in content
        assert "mcpServers:" in content

    def test_agent_profile_documents_all_tools(self):
        from pathlib import Path
        profile = Path(__file__).parent.parent.parent / "src" / "cli_agent_orchestrator" / "agent_store" / "master_orchestrator.md"
        content = profile.read_text()
        tools = ["handoff", "assign", "send_message", "create_bead", "create_epic",
                 "list_beads", "close_bead", "get_ready_beads", "list_sessions",
                 "get_session_output", "kill_session", "assign_bead", "get_epic_status"]
        for tool in tools:
            assert tool in content, f"Tool '{tool}' not documented in agent profile"
