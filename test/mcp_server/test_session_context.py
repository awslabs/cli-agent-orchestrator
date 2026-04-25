"""Tests for U5 — session_context MCP Tool.

Covers:
- U5.1: session_context MCP tool returns event timeline
- U5.1: Defaults to CAO_SESSION_NAME env var when session_name not provided
- U5.1: Returns error when no session_name and no env var
- U5.1: Returns empty event list for new/nonexistent session
- U5.1: Events in chronological order
- U5.2: CAO_SESSION_NAME injected in tmux create_session and create_window
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# U5.1 — session_context MCP tool
# ---------------------------------------------------------------------------


TIMELINE_PATH = "cli_agent_orchestrator.clients.database.get_session_timeline"


class TestSessionContextMcpTool:
    @patch(TIMELINE_PATH)
    def test_returns_events_for_session(self, mock_timeline):
        from cli_agent_orchestrator.mcp_server.server import session_context

        mock_timeline.return_value = [
            {
                "event_type": "agent_launched",
                "terminal_id": "t1",
                "provider": "claude_code",
                "summary": "Agent launched",
                "created_at": datetime(2026, 4, 18, 10, 0, 0),
            },
            {
                "event_type": "task_started",
                "terminal_id": "t1",
                "provider": "claude_code",
                "summary": "Fix the login bug",
                "created_at": datetime(2026, 4, 18, 10, 1, 0),
            },
        ]

        result = run_async(session_context(session_name="cao-test-session", limit=20))

        assert result["success"] is True
        assert result["session_name"] == "cao-test-session"
        assert len(result["events"]) == 2
        assert result["events"][0]["event_type"] == "agent_launched"
        assert result["events"][1]["event_type"] == "task_started"
        mock_timeline.assert_called_once_with("cao-test-session", limit=20)

    @patch(TIMELINE_PATH)
    def test_returns_empty_events_for_new_session(self, mock_timeline):
        from cli_agent_orchestrator.mcp_server.server import session_context

        mock_timeline.return_value = []

        result = run_async(session_context(session_name="cao-new-session"))

        assert result["success"] is True
        assert result["events"] == []

    @patch.dict("os.environ", {"CAO_SESSION_NAME": "cao-env-session"})
    @patch(TIMELINE_PATH)
    def test_defaults_to_env_var(self, mock_timeline):
        from cli_agent_orchestrator.mcp_server.server import session_context

        mock_timeline.return_value = []

        result = run_async(session_context())

        assert result["success"] is True
        assert result["session_name"] == "cao-env-session"
        mock_timeline.assert_called_once_with("cao-env-session", limit=20)

    @patch.dict("os.environ", {}, clear=True)
    def test_error_when_no_session_name(self):
        # Remove CAO_SESSION_NAME if it exists
        import os

        from cli_agent_orchestrator.mcp_server.server import session_context

        os.environ.pop("CAO_SESSION_NAME", None)

        result = run_async(session_context())

        assert result["success"] is False
        assert "CAO_SESSION_NAME" in result["error"]

    @patch(TIMELINE_PATH)
    def test_respects_limit_parameter(self, mock_timeline):
        from cli_agent_orchestrator.mcp_server.server import session_context

        mock_timeline.return_value = []

        run_async(session_context(session_name="s1", limit=5))

        mock_timeline.assert_called_once_with("s1", limit=5)

    @patch(TIMELINE_PATH)
    def test_rejects_negative_limit(self, mock_timeline):
        from cli_agent_orchestrator.mcp_server.server import session_context

        mock_timeline.return_value = []

        run_async(session_context(session_name="s1", limit=-1))

        # Negative limit should fall back to default 20
        mock_timeline.assert_called_once_with("s1", limit=20)

    @patch(TIMELINE_PATH)
    def test_clamps_very_large_limit(self, mock_timeline):
        from cli_agent_orchestrator.mcp_server.server import session_context

        mock_timeline.return_value = []

        run_async(session_context(session_name="s1", limit=5000))

        # Limit > 1000 should fall back to default 20
        mock_timeline.assert_called_once_with("s1", limit=20)

    @patch(TIMELINE_PATH)
    def test_events_contain_required_fields(self, mock_timeline):
        from cli_agent_orchestrator.mcp_server.server import session_context

        mock_timeline.return_value = [
            {
                "event_type": "task_completed",
                "terminal_id": "t2",
                "provider": "gemini_cli",
                "summary": "Refactored database",
                "created_at": datetime(2026, 4, 18, 12, 0, 0),
            },
        ]

        result = run_async(session_context(session_name="s1"))

        event = result["events"][0]
        assert "event_type" in event
        assert "terminal_id" in event
        assert "provider" in event
        assert "summary" in event
        assert "created_at" in event

    @patch(TIMELINE_PATH)
    def test_handles_database_error(self, mock_timeline):
        from cli_agent_orchestrator.mcp_server.server import session_context

        mock_timeline.side_effect = RuntimeError("DB connection failed")

        result = run_async(session_context(session_name="s1"))

        assert result["success"] is False
        assert "DB connection failed" in result["error"]


# ---------------------------------------------------------------------------
# U5.2 — CAO_SESSION_NAME env var in tmux
# ---------------------------------------------------------------------------


class TestCaoSessionNameEnvVar:
    @patch("cli_agent_orchestrator.clients.tmux.libtmux.Server")
    def test_create_session_sets_env_var(self, MockServer, tmp_path):
        from cli_agent_orchestrator.clients.tmux import TmuxClient

        mock_server = MockServer.return_value
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_window.name = "dev-window"
        mock_session.windows = [mock_window]
        mock_server.new_session.return_value = mock_session

        client = TmuxClient.__new__(TmuxClient)
        client.server = mock_server

        client.create_session("cao-my-session", "dev-window", "term-123", str(tmp_path))

        call_kwargs = mock_server.new_session.call_args
        env = call_kwargs.kwargs.get("environment", {})
        assert env["CAO_SESSION_NAME"] == "cao-my-session"
        assert env["CAO_TERMINAL_ID"] == "term-123"

    @patch("cli_agent_orchestrator.clients.tmux.libtmux.Server")
    def test_create_window_sets_env_var(self, MockServer, tmp_path):
        from cli_agent_orchestrator.clients.tmux import TmuxClient

        mock_server = MockServer.return_value
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_window.name = "worker-window"
        mock_session.new_window.return_value = mock_window
        mock_server.sessions.get.return_value = mock_session

        client = TmuxClient.__new__(TmuxClient)
        client.server = mock_server

        client.create_window("cao-my-session", "worker-window", "term-456", str(tmp_path))

        call_kwargs = mock_session.new_window.call_args
        env = call_kwargs.kwargs.get("environment", {})
        assert env["CAO_SESSION_NAME"] == "cao-my-session"
        assert env["CAO_TERMINAL_ID"] == "term-456"
