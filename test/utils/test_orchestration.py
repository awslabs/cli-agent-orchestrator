"""Tests for the status/result/cancel orchestration primitives (issue #616).

``_assign_impl``/``_handoff_impl``/``_send_message_impl``/``_create_terminal``/
``_resolve_handoff_provider``/``_get_cleanup_nudge``/``_delete_terminal_impl``
(moved here from ``mcp_server/server.py``) keep their existing coverage under
``test/mcp_server/`` -- those tests were retargeted to this module's namespace
rather than duplicated. This file covers the three primitives added for the
``cao agent`` CLI (status/result/cancel) that have no pre-existing MCP tool
or test.
"""

from unittest.mock import MagicMock, patch

import requests

from cli_agent_orchestrator.constants import API_BASE_URL
from cli_agent_orchestrator.utils.orchestration import (
    _auth_headers,
    _cancel_impl,
    _result_impl,
    _status_impl,
)


class TestAuthHeaders:
    """Tests for the local-auth bearer helper (review on PR #634).

    ``_auth_headers()`` is what every ``requests`` call in this module passes as
    ``headers=_auth_headers() or None`` -- without it, an auth-enabled cao-server
    rejects every orchestration call with a 401.
    """

    @patch("cli_agent_orchestrator.utils.orchestration.get_local_bearer", return_value="tok")
    def test_returns_bearer_header_when_token_configured(self, _bearer):
        assert _auth_headers() == {"Authorization": "Bearer tok"}

    @patch("cli_agent_orchestrator.utils.orchestration.get_local_bearer", return_value=None)
    def test_returns_empty_dict_when_no_token(self, _bearer):
        """Default-off: no Authorization header -- byte-for-byte unchanged."""
        assert _auth_headers() == {}


class TestStatusImpl:
    @patch("cli_agent_orchestrator.utils.orchestration.requests.get")
    def test_success_returns_terminal_metadata(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "id": "a1b2c3d4",
            "status": "idle",
            "agent_profile": "developer",
            "provider": "kiro_cli",
            "session_name": "cao-test",
        }
        resp.raise_for_status.return_value = None
        mock_get.return_value = resp

        result = _status_impl("a1b2c3d4")

        args, _ = mock_get.call_args
        assert args[0] == f"{API_BASE_URL}/terminals/a1b2c3d4"
        assert result == {
            "success": True,
            "terminal_id": "a1b2c3d4",
            "status": "idle",
            "agent_profile": "developer",
            "provider": "kiro_cli",
            "session_name": "cao-test",
        }

    @patch("cli_agent_orchestrator.utils.orchestration.requests.get")
    def test_not_found_returns_clear_error(self, mock_get):
        resp = MagicMock()
        resp.status_code = 404
        mock_get.return_value = resp

        result = _status_impl("deadbeef")

        assert result["success"] is False
        assert result["terminal_id"] == "deadbeef"
        assert "not found" in result["error"]

    @patch("cli_agent_orchestrator.utils.orchestration.requests.get")
    def test_http_error_surfaces_detail(self, mock_get):
        resp = MagicMock()
        resp.status_code = 500
        resp.json.return_value = {"detail": "boom on the server"}
        http_error = requests.HTTPError("500 Server Error")
        http_error.response = resp
        resp.raise_for_status.side_effect = http_error
        mock_get.return_value = resp

        result = _status_impl("a1b2c3d4")

        assert result["success"] is False
        assert result["error"] == "boom on the server"

    @patch(
        "cli_agent_orchestrator.utils.orchestration.requests.get",
        side_effect=requests.ConnectionError("refused"),
    )
    def test_connection_error_reports_server_down(self, mock_get):
        result = _status_impl("a1b2c3d4")

        assert result == {
            "success": False,
            "terminal_id": "a1b2c3d4",
            "error": "Failed to connect to cao-server. The server may not be running.",
        }

    @patch("cli_agent_orchestrator.utils.orchestration.requests.get", side_effect=Exception("boom"))
    def test_generic_exception_is_caught(self, mock_get):
        result = _status_impl("a1b2c3d4")

        assert result == {"success": False, "terminal_id": "a1b2c3d4", "error": "boom"}

    @patch("cli_agent_orchestrator.utils.orchestration.get_local_bearer", return_value="tok")
    @patch("cli_agent_orchestrator.utils.orchestration.requests.get")
    def test_attaches_bearer_when_auth_enabled(self, mock_get, _bearer):
        """Review on PR #634: the outgoing GET carries the local bearer when configured."""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": "a1b2c3d4", "status": "idle"}
        resp.raise_for_status.return_value = None
        mock_get.return_value = resp

        _status_impl("a1b2c3d4")

        _, kwargs = mock_get.call_args
        assert kwargs["headers"] == {"Authorization": "Bearer tok"}


class TestResultImpl:
    @patch("cli_agent_orchestrator.utils.orchestration.requests.get")
    def test_success_returns_last_output(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"output": "task complete", "mode": "last"}
        resp.raise_for_status.return_value = None
        mock_get.return_value = resp

        result = _result_impl("a1b2c3d4")

        args, kwargs = mock_get.call_args
        assert args[0] == f"{API_BASE_URL}/terminals/a1b2c3d4/output"
        assert kwargs["params"] == {"mode": "last"}
        assert result == {"success": True, "terminal_id": "a1b2c3d4", "output": "task complete"}

    @patch("cli_agent_orchestrator.utils.orchestration.requests.get")
    def test_not_found_returns_clear_error(self, mock_get):
        resp = MagicMock()
        resp.status_code = 404
        mock_get.return_value = resp

        result = _result_impl("deadbeef")

        assert result["success"] is False
        assert "not found" in result["error"]

    @patch(
        "cli_agent_orchestrator.utils.orchestration.requests.get",
        side_effect=requests.ConnectionError("refused"),
    )
    def test_connection_error_reports_server_down(self, mock_get):
        result = _result_impl("a1b2c3d4")

        assert result["success"] is False
        assert "Failed to connect" in result["error"]

    @patch("cli_agent_orchestrator.utils.orchestration.requests.get", side_effect=Exception("boom"))
    def test_generic_exception_is_caught(self, mock_get):
        result = _result_impl("a1b2c3d4")

        assert result == {"success": False, "terminal_id": "a1b2c3d4", "error": "boom"}

    @patch("cli_agent_orchestrator.utils.orchestration.get_local_bearer", return_value="tok")
    @patch("cli_agent_orchestrator.utils.orchestration.requests.get")
    def test_attaches_bearer_when_auth_enabled(self, mock_get, _bearer):
        """Review on PR #634: the outgoing GET carries the local bearer when configured."""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"output": "task complete"}
        resp.raise_for_status.return_value = None
        mock_get.return_value = resp

        _result_impl("a1b2c3d4")

        _, kwargs = mock_get.call_args
        assert kwargs["headers"] == {"Authorization": "Bearer tok"}


class TestCancelImpl:
    @patch("cli_agent_orchestrator.utils.orchestration.requests.post")
    def test_default_sends_interrupt_key(self, mock_post):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        mock_post.return_value = resp

        result = _cancel_impl("a1b2c3d4")

        args, kwargs = mock_post.call_args
        assert args[0] == f"{API_BASE_URL}/terminals/a1b2c3d4/key"
        assert kwargs["params"] == {"key": "C-c"}
        assert result["success"] is True
        assert result["terminal_id"] == "a1b2c3d4"

    @patch("cli_agent_orchestrator.utils.orchestration.requests.post")
    def test_not_found_returns_clear_error(self, mock_post):
        resp = MagicMock()
        resp.status_code = 404
        mock_post.return_value = resp

        result = _cancel_impl("deadbeef")

        assert result["success"] is False
        assert "not found" in result["error"]

    @patch(
        "cli_agent_orchestrator.utils.orchestration.requests.post",
        side_effect=requests.ConnectionError("refused"),
    )
    def test_connection_error_reports_server_down(self, mock_post):
        result = _cancel_impl("a1b2c3d4")

        assert result["success"] is False
        assert "Failed to connect" in result["error"]

    @patch("cli_agent_orchestrator.utils.orchestration._delete_terminal_impl")
    @patch("cli_agent_orchestrator.utils.orchestration.requests.post")
    def test_delete_true_delegates_to_delete_terminal_impl(self, mock_post, mock_delete):
        """delete=True skips the interrupt entirely and defers to the same
        path delete_terminal (MCP tool) uses -- no POST .../key call."""
        mock_delete.return_value = {
            "success": True,
            "message": "Terminal a1b2c3d4 deleted successfully",
        }

        result = _cancel_impl("a1b2c3d4", delete=True)

        mock_delete.assert_called_once_with("a1b2c3d4")
        mock_post.assert_not_called()
        assert result == {"success": True, "message": "Terminal a1b2c3d4 deleted successfully"}

    @patch("cli_agent_orchestrator.utils.orchestration.get_local_bearer", return_value="tok")
    @patch("cli_agent_orchestrator.utils.orchestration.requests.post")
    def test_attaches_bearer_when_auth_enabled(self, mock_post, _bearer):
        """Review on PR #634: the outgoing POST carries the local bearer when configured."""
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        mock_post.return_value = resp

        _cancel_impl("a1b2c3d4")

        _, kwargs = mock_post.call_args
        assert kwargs["headers"] == {"Authorization": "Bearer tok"}
