"""Tests for send_message MCP tool."""

import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from cli_agent_orchestrator.constants import API_BASE_URL


class TestSendToInboxAuthHeaders:
    """Review on PR #634: _send_to_inbox is the one path all six send-message
    variants funnel through, so this is where the bearer must attach."""

    @patch("cli_agent_orchestrator.utils.orchestration.get_local_bearer", return_value="tok")
    @patch("cli_agent_orchestrator.utils.orchestration.requests.post")
    def test_attaches_bearer_when_auth_enabled(self, mock_post, _bearer):
        from cli_agent_orchestrator.utils.orchestration import _send_to_inbox

        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"success": True}
        mock_post.return_value = resp

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "badc0de1"}):
            _send_to_inbox("c0ffee01", "Done!")

        _, kwargs = mock_post.call_args
        assert kwargs["headers"] == {"Authorization": "Bearer tok"}
        assert mock_post.call_args[0][0] == f"{API_BASE_URL}/terminals/c0ffee01/inbox/messages"


class TestSendMessageSelfSendGuard:
    """Tests for the self-send guard added for issue #24.

    A worker agent occasionally calls send_message with its own
    CAO_TERMINAL_ID as the receiver, which silently delivers the result
    into its own inbox instead of the supervisor's. The guard turns that
    into an explicit error so the worker can pick the correct receiver.
    """

    @patch("cli_agent_orchestrator.utils.orchestration._send_to_inbox")
    def test_send_message_rejects_self_send(self, mock_inbox):
        """Sending to the caller's own CAO_TERMINAL_ID should be rejected."""
        from cli_agent_orchestrator.utils.orchestration import _send_message_impl

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "badc0de1"}):
            result = _send_message_impl("badc0de1", "Done!")

        assert result["success"] is False
        assert "badc0de1" in result["error"]
        assert "own CAO_TERMINAL_ID" in result["error"]
        mock_inbox.assert_not_called()

    @patch("cli_agent_orchestrator.utils.orchestration._target_binding_denial", return_value=None)
    @patch("cli_agent_orchestrator.utils.orchestration._send_to_inbox")
    def test_send_message_allows_distinct_receiver(self, mock_inbox, _unbound):
        """Sending to a different terminal should still go through."""
        from cli_agent_orchestrator.utils.orchestration import _send_message_impl

        mock_inbox.return_value = {"success": True}

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "badc0de1"}):
            _send_message_impl("c0ffee01", "Done!")

        mock_inbox.assert_called_once()
        assert mock_inbox.call_args[0][0] == "c0ffee01"

    @patch("cli_agent_orchestrator.utils.orchestration._send_to_inbox")
    def test_send_message_no_guard_when_cao_terminal_id_unset(self, mock_inbox):
        """Without CAO_TERMINAL_ID the guard is inert — _send_to_inbox runs
        and surfaces its own error path."""
        from cli_agent_orchestrator.utils.orchestration import _send_message_impl

        mock_inbox.return_value = {"success": True}

        with patch.dict(os.environ, {}, clear=True):
            _send_message_impl("any-receiver", "Hello")

        mock_inbox.assert_called_once()


class TestSendMessageSenderIdInjection:
    """Tests for sender ID injection in _send_message_impl."""

    @patch("cli_agent_orchestrator.utils.orchestration._target_binding_denial", return_value=None)
    @patch("cli_agent_orchestrator.utils.orchestration.ENABLE_SENDER_ID_INJECTION", True)
    @patch("cli_agent_orchestrator.utils.orchestration._send_to_inbox")
    def test_send_message_appends_sender_id_when_injection_enabled(self, mock_inbox, _unbound):
        """When injection is enabled, send_message should append sender ID suffix."""
        from cli_agent_orchestrator.utils.orchestration import _send_message_impl

        mock_inbox.return_value = {"success": True}

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "deadbeef"}):
            _send_message_impl("receiver-123", "Here are the results")

        sent_message = mock_inbox.call_args[0][1]
        assert sent_message.startswith("Here are the results")
        assert "[Message from terminal deadbeef" in sent_message
        assert "Use send_message MCP tool for any follow-up work.]" in sent_message

    @patch("cli_agent_orchestrator.utils.orchestration._target_binding_denial", return_value=None)
    @patch("cli_agent_orchestrator.utils.orchestration.ENABLE_SENDER_ID_INJECTION", False)
    @patch("cli_agent_orchestrator.utils.orchestration._send_to_inbox")
    def test_send_message_no_suffix_when_injection_disabled(self, mock_inbox, _unbound):
        """When injection is disabled, send_message should pass the message unchanged."""
        from cli_agent_orchestrator.utils.orchestration import _send_message_impl

        mock_inbox.return_value = {"success": True}

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "deadbeef"}):
            _send_message_impl("receiver-123", "Here are the results")

        sent_message = mock_inbox.call_args[0][1]
        assert sent_message == "Here are the results"

    @patch("cli_agent_orchestrator.utils.orchestration.ENABLE_SENDER_ID_INJECTION", True)
    @patch("cli_agent_orchestrator.utils.orchestration._send_to_inbox")
    def test_send_message_no_suffix_when_cao_terminal_id_unset(self, mock_inbox):
        """When CAO_TERMINAL_ID is not set, no suffix is injected (issue #284) —
        'unknown' must never be presented as a routable terminal ID."""
        from cli_agent_orchestrator.utils.orchestration import _send_message_impl

        mock_inbox.return_value = {"success": True}

        with patch.dict(os.environ, {}, clear=True):
            _send_message_impl("receiver-123", "Status update")

        sent_message = mock_inbox.call_args[0][1]
        assert sent_message == "Status update"
        assert "unknown" not in sent_message

    @patch("cli_agent_orchestrator.utils.orchestration._target_binding_denial", return_value=None)
    @patch("cli_agent_orchestrator.utils.orchestration.ENABLE_SENDER_ID_INJECTION", True)
    @patch("cli_agent_orchestrator.utils.orchestration._send_to_inbox")
    def test_send_message_suffix_is_appended_not_prepended(self, mock_inbox, _unbound):
        """The sender ID should be a suffix, not a prefix."""
        from cli_agent_orchestrator.utils.orchestration import _send_message_impl

        mock_inbox.return_value = {"success": True}
        original = "Task complete. Here are the deliverables."

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "deadbeef"}):
            _send_message_impl("receiver-123", original)

        sent_message = mock_inbox.call_args[0][1]
        assert sent_message.startswith(original)
        assert sent_message.index("[Message from terminal") > len(original)


class TestSendMessageCallerDefault:
    """Tests for receiver_id defaulting to the recorded caller (issue #284).

    handoff/assign persist the creating terminal's ID as caller_id on the
    worker's terminal row. When a worker omits receiver_id, send_message
    looks up its own row and routes the reply to that recorded caller —
    taking the worker LLM out of the routing path entirely.
    """

    @patch("cli_agent_orchestrator.utils.orchestration.ENABLE_SENDER_ID_INJECTION", False)
    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    @patch("cli_agent_orchestrator.utils.orchestration._send_to_inbox")
    def test_omitted_receiver_routes_to_recorded_caller(self, mock_inbox, mock_requests):
        """No receiver_id + recorded caller → message goes to the caller."""
        from cli_agent_orchestrator.utils.orchestration import _send_message_impl

        mock_response = MagicMock()
        mock_response.json.return_value = {"id": "badc0de1", "caller_id": "c0ffee01"}
        mock_response.raise_for_status.return_value = None
        mock_requests.get.return_value = mock_response
        mock_inbox.return_value = {"success": True}

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "badc0de1"}):
            result = _send_message_impl(None, "Results ready")

        mock_inbox.assert_called_once()
        assert mock_inbox.call_args[0][0] == "c0ffee01"
        assert result == {"success": True}

    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    @patch("cli_agent_orchestrator.utils.orchestration._send_to_inbox")
    def test_omitted_receiver_without_recorded_caller_errors(self, mock_inbox, mock_requests):
        """No receiver_id + NULL caller_id → clear error, nothing sent."""
        from cli_agent_orchestrator.utils.orchestration import _send_message_impl

        mock_response = MagicMock()
        mock_response.json.return_value = {"id": "badc0de1", "caller_id": None}
        mock_response.raise_for_status.return_value = None
        mock_requests.get.return_value = mock_response

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "badc0de1"}):
            result = _send_message_impl(None, "Results ready")

        assert result["success"] is False
        assert "no recorded caller" in result["error"]
        assert "receiver_id" in result["error"]
        mock_inbox.assert_not_called()

    @patch("cli_agent_orchestrator.utils.orchestration._send_to_inbox")
    def test_omitted_receiver_without_terminal_id_errors(self, mock_inbox):
        """No receiver_id + no CAO_TERMINAL_ID → clear error, nothing sent."""
        from cli_agent_orchestrator.utils.orchestration import _send_message_impl

        with patch.dict(os.environ, {}, clear=True):
            result = _send_message_impl(None, "Hello")

        assert result["success"] is False
        assert "CAO_TERMINAL_ID not set" in result["error"]
        mock_inbox.assert_not_called()

    @patch("cli_agent_orchestrator.utils.orchestration._target_binding_denial", return_value=None)
    @patch("cli_agent_orchestrator.utils.orchestration.ENABLE_SENDER_ID_INJECTION", False)
    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    @patch("cli_agent_orchestrator.utils.orchestration._send_to_inbox")
    def test_explicit_receiver_skips_caller_lookup(self, mock_inbox, mock_requests, _unbound):
        """An explicit receiver_id must be used as-is, no API lookup."""
        from cli_agent_orchestrator.utils.orchestration import _send_message_impl

        mock_inbox.return_value = {"success": True}

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "badc0de1"}):
            _send_message_impl("explicit-recv", "Results")

        mock_requests.get.assert_not_called()
        assert mock_inbox.call_args[0][0] == "explicit-recv"

    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    @patch("cli_agent_orchestrator.utils.orchestration._send_to_inbox")
    def test_omitted_receiver_own_terminal_lookup_404_errors_clearly(
        self, mock_inbox, mock_requests
    ):
        """Own terminal record gone (e.g. deleted) → actionable error, not a
        raw requests error string."""
        from cli_agent_orchestrator.utils.orchestration import _send_message_impl

        mock_requests.HTTPError = requests.HTTPError
        mock_response = MagicMock()
        mock_response.json.return_value = {"detail": "Terminal 'badc0de1' not found"}
        http_error = requests.HTTPError("404 Client Error")
        http_error.response = mock_response
        mock_response.raise_for_status.side_effect = http_error
        mock_requests.get.return_value = mock_response

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "badc0de1"}):
            result = _send_message_impl(None, "Results ready")

        assert result["success"] is False
        assert "caller lookup" in result["error"]
        assert "Terminal 'badc0de1' not found" in result["error"]
        assert "receiver_id" in result["error"]
        mock_inbox.assert_not_called()

    @patch("cli_agent_orchestrator.utils.orchestration._target_binding_denial", return_value=None)
    @patch("cli_agent_orchestrator.utils.orchestration.ENABLE_SENDER_ID_INJECTION", False)
    @patch("cli_agent_orchestrator.utils.orchestration._send_to_inbox")
    def test_receiver_deleted_before_reply_errors_clearly(self, mock_inbox, _unbound):
        """Recorded caller deleted before the reply lands → the API detail is
        surfaced so the agent knows the address is gone."""
        from cli_agent_orchestrator.utils.orchestration import _send_message_impl

        mock_response = MagicMock()
        mock_response.json.return_value = {"detail": "Terminal 'c0ffee01' not found"}
        http_error = requests.HTTPError("404 Client Error")
        http_error.response = mock_response
        mock_inbox.side_effect = http_error

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "badc0de1"}):
            result = _send_message_impl("c0ffee01", "Results ready")

        assert result["success"] is False
        assert "c0ffee01" in result["error"]
        assert "Terminal 'c0ffee01' not found" in result["error"]

    @patch("cli_agent_orchestrator.utils.orchestration.requests")
    @patch("cli_agent_orchestrator.utils.orchestration._send_to_inbox")
    def test_self_referential_caller_still_rejected_by_own_id_guard(
        self, mock_inbox, mock_requests
    ):
        """A corrupted row recording the worker as its own caller must not
        bypass the issue #24 self-send guard."""
        from cli_agent_orchestrator.utils.orchestration import _send_message_impl

        mock_response = MagicMock()
        mock_response.json.return_value = {"id": "badc0de1", "caller_id": "badc0de1"}
        mock_response.raise_for_status.return_value = None
        mock_requests.get.return_value = mock_response

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "badc0de1"}):
            result = _send_message_impl(None, "Results")

        assert result["success"] is False
        assert "own CAO_TERMINAL_ID" in result["error"]
        mock_inbox.assert_not_called()


class TestSendToInboxDeliveryOutcome:
    """reply-delivery-failure D5: the POST can wait INBOX_DELIVERY_OUTCOME_WAIT."""

    @staticmethod
    def _response(body, status_code=200):
        resp = MagicMock()
        resp.status_code = status_code
        resp.raise_for_status.return_value = None
        resp.json.return_value = body
        return resp

    @staticmethod
    def _outcome_wait():
        from cli_agent_orchestrator import constants

        return constants.INBOX_DELIVERY_OUTCOME_WAIT

    @pytest.mark.parametrize("mcp_timeout", [30.0, 1000.0], ids=["mcp-shorter", "mcp-longer"])
    @patch("cli_agent_orchestrator.utils.orchestration.requests.post")
    def test_timeout_is_the_larger_of_the_mcp_timeout_and_the_outcome_wait(
        self, mock_post, mcp_timeout
    ):
        from cli_agent_orchestrator.utils.orchestration import _send_to_inbox

        mock_post.return_value = self._response({"success": True, "delivery": "delivered"})
        with (
            patch.dict(os.environ, {"CAO_TERMINAL_ID": "badc0de1"}),
            patch(
                "cli_agent_orchestrator.utils.orchestration._mcp_timeout",
                return_value=mcp_timeout,
            ),
        ):
            _send_to_inbox("c0ffee01", "Done!")
        expected = max(mcp_timeout, self._outcome_wait() + 10)
        assert mock_post.call_args.kwargs["timeout"] == expected

    @patch("cli_agent_orchestrator.utils.orchestration.requests.post")
    def test_callback_retry_uses_the_same_timeout(self, mock_post):
        from cli_agent_orchestrator.utils.orchestration import _send_to_inbox

        mock_post.side_effect = [
            self._response({}, status_code=404),
            self._response({"success": True, "delivery": "queued"}),
        ]
        with (
            patch.dict(os.environ, {"CAO_TERMINAL_ID": "badc0de1"}),
            patch("cli_agent_orchestrator.utils.orchestration._mcp_timeout", return_value=30.0),
            patch(
                "cli_agent_orchestrator.utils.orchestration._callback_route",
                return_value=("http://supervisor-node:9889", "5ca1ab1e"),
            ),
        ):
            _send_to_inbox("c0ffee01", "Done!")
        timeouts = [c.kwargs["timeout"] for c in mock_post.call_args_list]
        assert timeouts == [self._outcome_wait() + 10] * 2

    @pytest.mark.parametrize(
        "body",
        [
            {"success": False, "message_id": 7, "delivery": "failed", "error": "lost read"},
            {"success": False, "message_id": 7, "delivery": "uncertain", "error": "lost send"},
            {"success": True, "message_id": 7, "delivery": "in_progress", "error": None},
        ],
        ids=["failed", "uncertain", "in_progress"],
    )
    @patch("cli_agent_orchestrator.utils.orchestration._target_binding_denial", return_value=None)
    @patch("cli_agent_orchestrator.utils.orchestration.requests.post")
    def test_outcome_reaches_the_tool_result_unchanged(self, mock_post, _unbound, body):
        """Guard: success false from the POST must not be turned into success or an error."""
        from cli_agent_orchestrator.utils.orchestration import _send_message_impl

        mock_post.return_value = self._response(dict(body))
        with (
            patch.dict(os.environ, {"CAO_TERMINAL_ID": "badc0de1"}),
            patch("cli_agent_orchestrator.utils.orchestration._mcp_timeout", return_value=30.0),
        ):
            result = _send_message_impl("c0ffee01", "Done!")
        assert result == body
