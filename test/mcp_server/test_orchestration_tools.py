"""Tests for MCP orchestration tools and master orchestrator agent profile."""
from unittest.mock import patch, MagicMock
from pathlib import Path
import pytest


class TestApiHelpers:
    """Test internal _api_get/_api_post/_api_delete helpers."""

    def test_api_get(self):
        with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_req:
            mock_req.get.return_value = MagicMock(status_code=200, json=lambda: [{"id": "s1"}])
            mock_req.get.return_value.raise_for_status = MagicMock()
            from cli_agent_orchestrator.mcp_server.server import _api_get
            result = _api_get("/sessions")
            assert result == [{"id": "s1"}]

    def test_api_post(self):
        with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_req:
            mock_req.post.return_value = MagicMock(status_code=201, json=lambda: {"id": "t1"})
            mock_req.post.return_value.raise_for_status = MagicMock()
            from cli_agent_orchestrator.mcp_server.server import _api_post
            result = _api_post("/sessions", params={"provider": "q_cli"})
            assert result["id"] == "t1"

    def test_api_delete(self):
        with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_req:
            mock_req.delete.return_value = MagicMock(status_code=200, json=lambda: {"success": True})
            mock_req.delete.return_value.raise_for_status = MagicMock()
            from cli_agent_orchestrator.mcp_server.server import _api_delete
            result = _api_delete("/sessions/cao-s1")
            assert result["success"]


class TestToolRegistration:
    """Verify all MCP tools are registered."""

    def test_all_orchestration_tools_registered(self):
        from cli_agent_orchestrator.mcp_server.server import mcp
        tool_names = [t.name for t in mcp._tool_manager._tools.values()]
        expected = [
            # Original 3
            "handoff", "assign", "send_message",
            # Session management
            "list_sessions", "get_session", "create_session", "delete_session",
            "send_input_to_session", "get_session_output",
            # Terminal operations
            "list_terminals", "get_terminal_output", "send_terminal_input", "exit_terminal",
            # Agent discovery
            "list_agent_profiles", "list_providers",
            # Flow management
            "list_flows", "get_flow", "create_flow", "run_flow",
            "enable_flow", "disable_flow", "delete_flow",
            # Inbox
            "send_inbox_message", "get_inbox_messages",
            # Beads
            "list_beads", "create_bead", "create_epic", "get_epic_status",
            "get_ready_beads", "assign_bead", "close_bead",
        ]
        for name in expected:
            assert name in tool_names, f"MCP tool '{name}' not registered"

    def test_tool_count(self):
        from cli_agent_orchestrator.mcp_server.server import mcp
        tools = list(mcp._tool_manager._tools.values())
        # 3 original + 20 session/flow + 7 bead = 30
        assert len(tools) >= 30, f"Expected >=30 tools, got {len(tools)}"


class TestOrchestratorProfile:
    """Verify the master orchestrator agent profile."""

    def test_profile_exists(self):
        profile = Path(__file__).parent.parent.parent / "src" / "cli_agent_orchestrator" / "agent_store" / "master_orchestrator.md"
        assert profile.exists()

    def test_profile_has_frontmatter(self):
        profile = Path(__file__).parent.parent.parent / "src" / "cli_agent_orchestrator" / "agent_store" / "master_orchestrator.md"
        content = profile.read_text()
        assert content.startswith("---")
        assert "name: master_orchestrator" in content
        assert "mcpServers:" in content
        assert "cao-mcp-server" in content

    def test_profile_documents_all_tool_categories(self):
        profile = Path(__file__).parent.parent.parent / "src" / "cli_agent_orchestrator" / "agent_store" / "master_orchestrator.md"
        content = profile.read_text()
        categories = [
            "Session Management", "Agent Delegation", "Terminal Operations",
            "Flow Automation", "Agent Discovery", "Inbox"
        ]
        for cat in categories:
            assert cat in content, f"Category '{cat}' not documented in profile"

    def test_profile_documents_key_tools(self):
        profile = Path(__file__).parent.parent.parent / "src" / "cli_agent_orchestrator" / "agent_store" / "master_orchestrator.md"
        content = profile.read_text()
        tools = [
            "list_sessions", "create_session", "delete_session", "handoff", "assign",
            "send_message", "list_flows", "create_flow", "run_flow", "list_agent_profiles",
            "list_beads", "create_bead", "create_epic", "get_epic_status",
            "get_ready_beads", "assign_bead", "close_bead",
        ]
        for tool in tools:
            assert tool in content, f"Tool '{tool}' not documented in profile"
