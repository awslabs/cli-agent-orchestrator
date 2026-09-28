"""Tests for MCP server utilities.

These exercise the HTTP-only ``get_terminal_record`` helper, which now fetches
the terminal record over the Backplane REST surface instead of the database
(preserving the MCP HTTP-only boundary; Requirement 7).
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from cli_agent_orchestrator.mcp_server.utils import (
    get_json,
    get_terminal_record,
    post_body_json,
)


class TestGetTerminalRecord:
    """Tests for the HTTP-only get_terminal_record function."""

    @patch("cli_agent_orchestrator.mcp_server.utils.requests.get")
    def test_get_terminal_record_found(self, mock_get):
        """Returns the parsed JSON record when the Backplane returns 200."""

        record = {"id": "term-123", "session_name": "test-session"}
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = record
        mock_get.return_value = mock_response

        result = get_terminal_record("term-123")

        assert result == record
        mock_response.raise_for_status.assert_called_once()

    @patch("cli_agent_orchestrator.mcp_server.utils.requests.get")
    def test_get_terminal_record_not_found(self, mock_get):
        """Returns None when the Backplane responds 404."""

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        result = get_terminal_record("nonexistent")

        assert result is None
        # A 404 is a normal "not found", not an error to raise on.
        mock_response.raise_for_status.assert_not_called()

    @patch("cli_agent_orchestrator.mcp_server.utils.requests.get")
    def test_get_terminal_record_returns_none_on_connection_error(self, mock_get):
        """A transport-level failure degrades to None rather than crashing."""

        mock_get.side_effect = requests.ConnectionError("server down")

        result = get_terminal_record("term-123")

        assert result is None

    @patch("cli_agent_orchestrator.mcp_server.utils.requests.get")
    def test_get_terminal_record_raises_on_server_error(self, mock_get):
        """A non-404 HTTP error is surfaced via raise_for_status."""

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.raise_for_status.side_effect = requests.HTTPError("500")
        mock_get.return_value = mock_response

        with pytest.raises(requests.HTTPError):
            get_terminal_record("term-123")

    @patch("cli_agent_orchestrator.mcp_server.utils.get_local_bearer", return_value="tok")
    @patch("cli_agent_orchestrator.mcp_server.utils.requests.get")
    def test_get_terminal_record_attaches_bearer(self, mock_get, _bearer):
        """H3: the internal GET carries the local bearer when auth is enabled."""

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "t"}
        mock_get.return_value = mock_response

        get_terminal_record("t")
        _, kwargs = mock_get.call_args
        assert kwargs["headers"] == {"Authorization": "Bearer tok"}

    @patch("cli_agent_orchestrator.mcp_server.utils.get_local_bearer", return_value=None)
    @patch("cli_agent_orchestrator.mcp_server.utils.requests.get")
    def test_get_terminal_record_no_bearer_default_off(self, mock_get, _bearer):
        """Default-off: no Authorization header (byte-for-byte unchanged)."""

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "t"}
        mock_get.return_value = mock_response

        get_terminal_record("t")
        _, kwargs = mock_get.call_args
        assert kwargs["headers"] is None


class TestGetJsonAndPostBodyJson:
    """The shared HTTP helpers behind the outcome tools.

    These carry the fix for the bearer-token defect: ``GET /terminals/{id}`` is
    scope-gated, so with auth enabled an unauthenticated internal hop 401'd, the
    terminal context resolved to ``None``, and memory scope silently collapsed to
    global. The symptom was invisible — a wrong-but-plausible scope, not an
    error — so the header attachment is pinned directly rather than only through
    the callers that happen to exercise it.
    """

    @staticmethod
    def _ok(payload):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = payload
        return response

    @patch("cli_agent_orchestrator.mcp_server.utils.get_local_bearer", return_value="tok")
    @patch("cli_agent_orchestrator.mcp_server.utils.requests.get")
    def test_get_json_attaches_bearer(self, mock_get, _bearer):
        mock_get.return_value = self._ok({"ok": True})

        assert get_json("/outcomes") == {"ok": True}
        _, kwargs = mock_get.call_args
        assert kwargs["headers"] == {"Authorization": "Bearer tok"}

    @patch("cli_agent_orchestrator.mcp_server.utils.get_local_bearer", return_value=None)
    @patch("cli_agent_orchestrator.mcp_server.utils.requests.get")
    def test_get_json_omits_header_default_off(self, mock_get, _bearer):
        mock_get.return_value = self._ok({"ok": True})

        get_json("/outcomes")
        _, kwargs = mock_get.call_args
        assert kwargs["headers"] is None

    @patch("cli_agent_orchestrator.mcp_server.utils.get_local_bearer", return_value="tok")
    @patch("cli_agent_orchestrator.mcp_server.utils.requests.post")
    def test_post_body_json_attaches_bearer_and_sends_a_body(self, mock_post, _bearer):
        mock_post.return_value = self._ok({"id": "o-1"})

        assert post_body_json("/outcomes", {"task_label": "t"}) == {"id": "o-1"}
        _, kwargs = mock_post.call_args
        assert kwargs["headers"] == {"Authorization": "Bearer tok"}
        # Body, not query string: friction_notes is free text that a URL mangles.
        assert kwargs["json"] == {"task_label": "t"}
        assert "params" not in kwargs

    @patch("cli_agent_orchestrator.mcp_server.utils.get_local_bearer", return_value=None)
    @patch("cli_agent_orchestrator.mcp_server.utils.requests.post")
    def test_post_body_json_omits_header_default_off(self, mock_post, _bearer):
        mock_post.return_value = self._ok({})

        post_body_json("/outcomes", {"task_label": "t"})
        _, kwargs = mock_post.call_args
        assert kwargs["headers"] is None

    @patch("cli_agent_orchestrator.mcp_server.utils.get_local_bearer", return_value=None)
    @patch("cli_agent_orchestrator.mcp_server.utils.requests.get")
    def test_get_json_drops_none_params_entirely(self, mock_get, _bearer):
        """``None`` params must vanish, not travel as the string "None"."""
        mock_get.return_value = self._ok([])

        get_json("/outcomes", session_name="s", agent_profile=None, limit=None)
        _, kwargs = mock_get.call_args
        assert kwargs["params"] == {"session_name": "s"}

    @patch("cli_agent_orchestrator.mcp_server.utils.get_local_bearer", return_value=None)
    @patch("cli_agent_orchestrator.mcp_server.utils.requests.get")
    def test_get_json_sends_no_params_when_all_are_none(self, mock_get, _bearer):
        """An all-``None`` set collapses to ``None``, not an empty dict."""
        mock_get.return_value = self._ok([])

        get_json("/outcomes", session_name=None)
        _, kwargs = mock_get.call_args
        assert kwargs["params"] is None

    @patch("cli_agent_orchestrator.mcp_server.utils.get_local_bearer", return_value=None)
    @patch("cli_agent_orchestrator.mcp_server.utils.requests.get")
    def test_get_json_propagates_http_errors(self, mock_get, _bearer):
        """A transport/auth failure must NOT be swallowed into a falsy result.

        Silently returning ``None`` here is what let a 401 read as "no terminal
        identity" and collapse the memory scope to global.
        """
        response = MagicMock()
        response.status_code = 401
        response.raise_for_status.side_effect = requests.HTTPError("401")
        mock_get.return_value = response

        with pytest.raises(requests.HTTPError):
            get_json("/outcomes")

    @patch("cli_agent_orchestrator.mcp_server.utils.get_local_bearer", return_value=None)
    @patch("cli_agent_orchestrator.mcp_server.utils.requests.post")
    def test_post_body_json_tolerates_an_empty_body(self, mock_post, _bearer):
        """Some mutations answer 200 with no JSON; that is success, not a crash."""
        response = MagicMock()
        response.status_code = 200
        response.json.side_effect = ValueError("no json")
        mock_post.return_value = response

        assert post_body_json("/outcomes", {"a": 1}) == {}
