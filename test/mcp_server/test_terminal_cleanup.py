"""Tests for delete_terminal MCP tool and _get_cleanup_nudge helper."""

import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from cli_agent_orchestrator.mcp_server.server import _get_cleanup_nudge, delete_terminal


class TestGetCleanupNudge:
    def test_returns_empty_when_no_terminal_id_env(self):
        with patch.dict(os.environ, {}, clear=True):
            assert _get_cleanup_nudge() == ""

    def test_returns_empty_when_terminal_fetch_fails(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "t1"}):
            with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get:
                mock_get.return_value.status_code = 500
                assert _get_cleanup_nudge() == ""

    def test_returns_empty_when_no_session_name(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "t1"}):
            with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = {}  # no session_name
                mock_get.return_value = mock_resp
                assert _get_cleanup_nudge() == ""

    def test_returns_empty_when_sessions_fetch_fails(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "t1"}):
            with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get:
                terminal_resp = MagicMock()
                terminal_resp.status_code = 200
                terminal_resp.json.return_value = {"session_name": "cao-test"}
                sessions_resp = MagicMock()
                sessions_resp.status_code = 500
                mock_get.side_effect = [terminal_resp, sessions_resp]
                assert _get_cleanup_nudge() == ""

    def test_returns_empty_when_below_threshold(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "t1"}):
            with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get:
                terminal_resp = MagicMock()
                terminal_resp.status_code = 200
                terminal_resp.json.return_value = {"session_name": "cao-test"}
                sessions_resp = MagicMock()
                sessions_resp.status_code = 200
                sessions_resp.json.return_value = [{}] * 5  # below threshold of 10
                mock_get.side_effect = [terminal_resp, sessions_resp]
                assert _get_cleanup_nudge() == ""

    def test_returns_nudge_when_at_threshold(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "t1"}):
            with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get:
                terminal_resp = MagicMock()
                terminal_resp.status_code = 200
                terminal_resp.json.return_value = {"session_name": "cao-test"}
                sessions_resp = MagicMock()
                sessions_resp.status_code = 200
                sessions_resp.json.return_value = [{}] * 10  # at threshold
                mock_get.side_effect = [terminal_resp, sessions_resp]
                nudge = _get_cleanup_nudge()
                assert "10 terminals" in nudge
                assert "delete_terminal" in nudge

    def test_returns_empty_on_exception(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "t1"}):
            with patch(
                "cli_agent_orchestrator.mcp_server.server.requests.get",
                side_effect=Exception("network error"),
            ):
                assert _get_cleanup_nudge() == ""


class TestDeleteTerminal:
    def test_success(self):
        with patch("cli_agent_orchestrator.mcp_server.server.requests.delete") as mock_delete:
            mock_delete.return_value.raise_for_status.return_value = None
            result = delete_terminal("t1")
        assert result["success"] is True
        assert "t1" in result["message"]

    def test_not_found_returns_false(self):
        with patch("cli_agent_orchestrator.mcp_server.server.requests.delete") as mock_delete:
            http_err = requests.HTTPError()
            http_err.response = MagicMock()
            http_err.response.status_code = 404
            mock_delete.return_value.raise_for_status.side_effect = http_err
            result = delete_terminal("t1")
        assert result["success"] is False
        assert "not found" in result["message"]

    def test_http_error_non_404(self):
        with patch("cli_agent_orchestrator.mcp_server.server.requests.delete") as mock_delete:
            http_err = requests.HTTPError("500 Server Error")
            http_err.response = MagicMock()
            http_err.response.status_code = 500
            mock_delete.return_value.raise_for_status.side_effect = http_err
            result = delete_terminal("t1")
        assert result["success"] is False
        assert "Failed" in result["message"]

    def test_generic_exception(self):
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.delete",
            side_effect=Exception("connection refused"),
        ):
            result = delete_terminal("t1")
        assert result["success"] is False
        assert "Failed" in result["message"]
