"""Tests for MCP server utilities.

These exercise the HTTP-only ``get_terminal_record`` helper, which now fetches
the terminal record over the Backplane REST surface instead of the database
(preserving the MCP HTTP-only boundary; Requirement 7).
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from cli_agent_orchestrator.mcp_server.utils import get_terminal_record


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
