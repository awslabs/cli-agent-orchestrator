"""Tests for send_message MCP tool."""

import os
from unittest.mock import patch


class TestSendMessageSelfSendGuard:
    """Tests for the self-send guard added for issue #24.

    A worker agent occasionally calls send_message with its own
    CAO_TERMINAL_ID as the receiver, which silently delivers the result
    into its own inbox instead of the supervisor's. The guard turns that
    into an explicit error so the worker can pick the correct receiver.
    """

    @patch("cli_agent_orchestrator.mcp_server.server._send_to_inbox")
    def test_send_message_rejects_self_send(self, mock_inbox):
        """Sending to the caller's own CAO_TERMINAL_ID should be rejected."""
        from cli_agent_orchestrator.mcp_server.server import _send_message_impl

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "worker-abc"}):
            result = _send_message_impl("worker-abc", "Done!")

        assert result["success"] is False
        assert "worker-abc" in result["error"]
        assert "own CAO_TERMINAL_ID" in result["error"]
        mock_inbox.assert_not_called()

    @patch("cli_agent_orchestrator.mcp_server.server._send_to_inbox")
    def test_send_message_allows_distinct_receiver(self, mock_inbox):
        """Sending to a different terminal should still go through."""
        from cli_agent_orchestrator.mcp_server.server import _send_message_impl

        mock_inbox.return_value = {"success": True}

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "worker-abc"}):
            _send_message_impl("supervisor-xyz", "Done!")

        mock_inbox.assert_called_once()
        assert mock_inbox.call_args[0][0] == "supervisor-xyz"

    @patch("cli_agent_orchestrator.mcp_server.server._send_to_inbox")
    def test_send_message_no_guard_when_cao_terminal_id_unset(self, mock_inbox):
        """Without CAO_TERMINAL_ID the guard is inert — _send_to_inbox runs
        and surfaces its own error path."""
        from cli_agent_orchestrator.mcp_server.server import _send_message_impl

        mock_inbox.return_value = {"success": True}

        with patch.dict(os.environ, {}, clear=True):
            _send_message_impl("any-receiver", "Hello")

        mock_inbox.assert_called_once()


class TestSendMessageSenderIdInjection:
    """Tests for sender ID injection in _send_message_impl."""

    @patch("cli_agent_orchestrator.mcp_server.server.ENABLE_SENDER_ID_INJECTION", True)
    @patch("cli_agent_orchestrator.mcp_server.server._send_to_inbox")
    def test_send_message_appends_sender_id_when_injection_enabled(self, mock_inbox):
        """When injection is enabled, send_message should append sender ID suffix."""
        from cli_agent_orchestrator.mcp_server.server import _send_message_impl

        mock_inbox.return_value = {"success": True}

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "sender-xyz"}):
            result = _send_message_impl("receiver-123", "Here are the results")

        sent_message = mock_inbox.call_args[0][1]
        assert sent_message.startswith("Here are the results")
        assert "[Message from terminal sender-xyz" in sent_message
        assert "Use send_message MCP tool for any follow-up work.]" in sent_message

    @patch("cli_agent_orchestrator.mcp_server.server.ENABLE_SENDER_ID_INJECTION", False)
    @patch("cli_agent_orchestrator.mcp_server.server._send_to_inbox")
    def test_send_message_no_suffix_when_injection_disabled(self, mock_inbox):
        """When injection is disabled, send_message should pass the message unchanged."""
        from cli_agent_orchestrator.mcp_server.server import _send_message_impl

        mock_inbox.return_value = {"success": True}

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "sender-xyz"}):
            result = _send_message_impl("receiver-123", "Here are the results")

        sent_message = mock_inbox.call_args[0][1]
        assert sent_message == "Here are the results"

    @patch("cli_agent_orchestrator.mcp_server.server.ENABLE_SENDER_ID_INJECTION", True)
    @patch("cli_agent_orchestrator.mcp_server.server._send_to_inbox")
    def test_send_message_sender_id_fallback_unknown(self, mock_inbox):
        """When CAO_TERMINAL_ID is not set, suffix should use 'unknown'."""
        from cli_agent_orchestrator.mcp_server.server import _send_message_impl

        mock_inbox.return_value = {"success": True}

        with patch.dict(os.environ, {}, clear=True):
            result = _send_message_impl("receiver-123", "Status update")

        sent_message = mock_inbox.call_args[0][1]
        assert "[Message from terminal unknown" in sent_message

    @patch("cli_agent_orchestrator.mcp_server.server.ENABLE_SENDER_ID_INJECTION", True)
    @patch("cli_agent_orchestrator.mcp_server.server._send_to_inbox")
    def test_send_message_suffix_is_appended_not_prepended(self, mock_inbox):
        """The sender ID should be a suffix, not a prefix."""
        from cli_agent_orchestrator.mcp_server.server import _send_message_impl

        mock_inbox.return_value = {"success": True}
        original = "Task complete. Here are the deliverables."

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "sender-999"}):
            _send_message_impl("receiver-123", original)

        sent_message = mock_inbox.call_args[0][1]
        assert sent_message.startswith(original)
        assert sent_message.index("[Message from terminal") > len(original)
