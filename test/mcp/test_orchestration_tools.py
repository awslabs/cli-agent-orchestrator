"""Tests for MCP orchestration tools — test the internal API helpers.

The @mcp.tool() decorator wraps functions as FunctionTool objects,
so we test the internal _api_get/_api_post helpers and the tool logic
by testing the underlying functions directly.
"""
import json
from unittest.mock import patch, MagicMock
import pytest


class TestApiHelpers:
    """Test the _api_get/_api_post/_api_delete helpers."""

    def test_api_get_calls_requests(self):
        with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_req:
            mock_req.get.return_value = MagicMock(status_code=200, json=lambda: [{"id": "s1"}])
            mock_req.get.return_value.raise_for_status = MagicMock()
            from cli_agent_orchestrator.mcp_server.server import _api_get
            result = _api_get("/api/v2/sessions")
            assert result == [{"id": "s1"}]

    def test_api_post_calls_requests(self):
        with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_req:
            mock_req.post.return_value = MagicMock(status_code=201, json=lambda: {"id": "b1"})
            mock_req.post.return_value.raise_for_status = MagicMock()
            from cli_agent_orchestrator.mcp_server.server import _api_post
            result = _api_post("/api/tasks", json={"title": "Test"})
            assert result["id"] == "b1"

    def test_api_delete_calls_requests(self):
        with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_req:
            mock_req.delete.return_value = MagicMock(status_code=200, json=lambda: {"success": True})
            mock_req.delete.return_value.raise_for_status = MagicMock()
            from cli_agent_orchestrator.mcp_server.server import _api_delete
            result = _api_delete("/api/v2/sessions/s1")
            assert result["success"]


class TestCreateBeadForTask:
    """Test the _create_bead_for_task helper."""

    def test_creates_bead_and_returns_id(self):
        with patch("cli_agent_orchestrator.mcp_server.server._api_post") as mock_post:
            mock_post.return_value = {"id": "bead-123", "title": "Test"}
            from cli_agent_orchestrator.mcp_server.server import _create_bead_for_task
            result = _create_bead_for_task("Test task", "Description")
            assert result == "bead-123"
            mock_post.assert_called_once_with("/api/tasks", json={
                "title": "Test task", "description": "Description", "priority": 2
            })

    def test_returns_none_on_failure(self):
        with patch("cli_agent_orchestrator.mcp_server.server._api_post", side_effect=Exception("fail")):
            from cli_agent_orchestrator.mcp_server.server import _create_bead_for_task
            result = _create_bead_for_task("Test")
            assert result is None


class TestCloseBeadHelper:
    """Test the _close_bead helper."""

    def test_closes_bead(self):
        with patch("cli_agent_orchestrator.mcp_server.server._api_post") as mock_post:
            mock_post.return_value = {"status": "closed"}
            from cli_agent_orchestrator.mcp_server.server import _close_bead
            _close_bead("b1")
            mock_post.assert_called_with("/api/tasks/b1/close")

    def test_does_not_raise_on_failure(self):
        with patch("cli_agent_orchestrator.mcp_server.server._api_post", side_effect=Exception("fail")):
            from cli_agent_orchestrator.mcp_server.server import _close_bead
            _close_bead("bad")  # Should not raise


class TestMcpToolRegistration:
    """Verify that all expected tools are registered on the MCP server."""

    def test_all_tools_registered(self):
        from cli_agent_orchestrator.mcp_server.server import mcp
        tool_names = [t.name for t in mcp._tool_manager._tools.values()]
        expected = [
            "handoff", "assign", "send_message",
            "list_sessions", "get_session_output", "kill_session",
            "list_beads", "create_bead", "create_epic",
            "get_epic_status", "get_ready_beads", "assign_bead", "close_bead",
        ]
        for name in expected:
            assert name in tool_names, f"Tool '{name}' not registered"

    def test_tool_count(self):
        from cli_agent_orchestrator.mcp_server.server import mcp
        tools = list(mcp._tool_manager._tools.values())
        assert len(tools) == 13  # 3 original + 10 new


class TestSendToInbox:
    """Test the _send_to_inbox helper."""

    def test_sends_message(self):
        with patch("cli_agent_orchestrator.mcp_server.server._api_post") as mock_post, \
             patch.dict("os.environ", {"CAO_TERMINAL_ID": "sender-1"}):
            mock_post.return_value = {"success": True}
            from cli_agent_orchestrator.mcp_server.server import _send_to_inbox
            result = _send_to_inbox("receiver-1", "Hello")
            assert result["success"]

    def test_raises_without_terminal_id(self):
        with patch.dict("os.environ", {}, clear=True):
            from cli_agent_orchestrator.mcp_server.server import _send_to_inbox
            with pytest.raises(ValueError, match="CAO_TERMINAL_ID"):
                _send_to_inbox("r1", "msg")
