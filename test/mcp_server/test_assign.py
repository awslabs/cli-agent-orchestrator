"""Tests for assign MCP tool."""

import os
from unittest.mock import patch


class TestAssignSenderIdInjection:
    """Tests for sender ID injection in _assign_impl."""

    @patch("cli_agent_orchestrator.mcp_server.server.ENABLE_SENDER_ID_INJECTION", True)
    @patch("cli_agent_orchestrator.mcp_server.server._send_direct_input")
    @patch("cli_agent_orchestrator.mcp_server.server._create_terminal")
    def test_assign_appends_sender_id_when_injection_enabled(self, mock_create, mock_send):
        """When injection is enabled, assign should append sender ID suffix."""
        from cli_agent_orchestrator.mcp_server.server import _assign_impl

        mock_create.return_value = ("worker-1", "claude_code")
        mock_send.return_value = None

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "supervisor-abc123"}):
            result = _assign_impl("developer", "Analyze the logs")

        assert result["success"] is True
        sent_message = mock_send.call_args[0][1]
        assert sent_message.startswith("Analyze the logs")
        assert "[Assigned by terminal supervisor-abc123" in sent_message
        assert "send results back to terminal supervisor-abc123 using send_message]" in sent_message

    @patch("cli_agent_orchestrator.mcp_server.server.ENABLE_SENDER_ID_INJECTION", False)
    @patch("cli_agent_orchestrator.mcp_server.server._send_direct_input")
    @patch("cli_agent_orchestrator.mcp_server.server._create_terminal")
    def test_assign_no_suffix_when_injection_disabled(self, mock_create, mock_send):
        """When injection is disabled, assign should send the message unchanged."""
        from cli_agent_orchestrator.mcp_server.server import _assign_impl

        mock_create.return_value = ("worker-2", "claude_code")
        mock_send.return_value = None

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "supervisor-abc123"}):
            result = _assign_impl("developer", "Analyze the logs")

        assert result["success"] is True
        sent_message = mock_send.call_args[0][1]
        assert sent_message == "Analyze the logs"

    @patch("cli_agent_orchestrator.mcp_server.server.ENABLE_SENDER_ID_INJECTION", True)
    @patch("cli_agent_orchestrator.mcp_server.server._send_direct_input")
    @patch("cli_agent_orchestrator.mcp_server.server._create_terminal")
    def test_assign_sender_id_fallback_unknown(self, mock_create, mock_send):
        """When CAO_TERMINAL_ID is not set, suffix should use 'unknown'."""
        from cli_agent_orchestrator.mcp_server.server import _assign_impl

        mock_create.return_value = ("worker-3", "codex")
        mock_send.return_value = None

        with patch.dict(os.environ, {}, clear=True):
            result = _assign_impl("developer", "Build feature X")

        sent_message = mock_send.call_args[0][1]
        assert "[Assigned by terminal unknown" in sent_message

    @patch("cli_agent_orchestrator.mcp_server.server.ENABLE_SENDER_ID_INJECTION", True)
    @patch("cli_agent_orchestrator.mcp_server.server._send_direct_input")
    @patch("cli_agent_orchestrator.mcp_server.server._create_terminal")
    def test_assign_suffix_is_appended_not_prepended(self, mock_create, mock_send):
        """The sender ID should be a suffix, not a prefix."""
        from cli_agent_orchestrator.mcp_server.server import _assign_impl

        mock_create.return_value = ("worker-4", "claude_code")
        mock_send.return_value = None
        original = "Do the task described in /path/to/task.md"

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "sup-111"}):
            _assign_impl("developer", original)

        sent_message = mock_send.call_args[0][1]
        assert sent_message.startswith(original)
        assert sent_message.index("[Assigned by terminal") > len(original)
