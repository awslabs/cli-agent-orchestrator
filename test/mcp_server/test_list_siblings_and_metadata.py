"""Tests for the #432 list_siblings and update_metadata MCP tools."""

import os
from unittest.mock import MagicMock, patch

import requests

from cli_agent_orchestrator.mcp_server.server import (
    _list_siblings_impl,
    _mcp_timeout,
    _update_metadata_impl,
)


class TestListSiblingsImpl:
    """Tests for the _list_siblings_impl helper."""

    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_resolves_own_identity_from_env_not_an_argument(self, mock_get):
        """The tool takes no 'who am I' argument -- identity comes solely
        from this process's own CAO_TERMINAL_ID env var (#432)."""
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = [{"id": "sib-1", "group": ["t1"], "metadata": None}]
        mock_get.return_value = response

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            result = _list_siblings_impl(None)

        assert result == {
            "success": True,
            "siblings": [{"id": "sib-1", "group": ["t1"], "metadata": None}],
        }
        mock_get.assert_called_once_with(
            "http://127.0.0.1:9889/terminals/caller-abc/siblings",
            params={},
            timeout=_mcp_timeout(),
        )

    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_depth_forwarded_when_provided(self, mock_get):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = []
        mock_get.return_value = response

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            _list_siblings_impl(2)

        mock_get.assert_called_once_with(
            "http://127.0.0.1:9889/terminals/caller-abc/siblings",
            params={"depth": 2},
            timeout=_mcp_timeout(),
        )

    def test_no_terminal_id_returns_error_without_network_call(self):
        """Outside a CAO terminal (no CAO_TERMINAL_ID) the tool must fail
        fast with a clear error, not attempt to call the API with no
        identity to scope the query to."""
        with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get:
            with patch.dict(os.environ, {}, clear=True):
                result = _list_siblings_impl(None)

            assert result["success"] is False
            assert "CAO_TERMINAL_ID not set" in result["error"]
            mock_get.assert_not_called()

    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_depth_zero_rejection_surfaces_server_detail(self, mock_get):
        """The server rejects depth=0 with a 422; the tool should surface
        that detail rather than swallowing it."""
        response = MagicMock()
        response.json.return_value = {"detail": "depth must be >= 1"}
        http_error = requests.HTTPError("422 Client Error")
        http_error.response = response
        response.raise_for_status.side_effect = http_error
        mock_get.return_value = response

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            result = _list_siblings_impl(0)

        assert result["success"] is False
        assert "depth must be >= 1" in result["error"]

    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_connection_error_returns_structured_error(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("boom")

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            result = _list_siblings_impl(None)

        assert result["success"] is False
        assert "Failed to list siblings" in result["error"]


class TestUpdateMetadataImpl:
    """Tests for the _update_metadata_impl helper."""

    @patch("cli_agent_orchestrator.mcp_server.server.requests.patch")
    def test_resolves_own_identity_and_replaces_metadata(self, mock_patch):
        """The tool takes no target terminal id argument -- it can only ever
        update ITS OWN metadata, resolved from CAO_TERMINAL_ID (#432)."""
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"metadata": {"task": "reviewing PR"}}
        mock_patch.return_value = response

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            result = _update_metadata_impl({"task": "reviewing PR"})

        assert result == {"success": True, "metadata": {"task": "reviewing PR"}}
        mock_patch.assert_called_once_with(
            "http://127.0.0.1:9889/terminals/caller-abc/metadata",
            json={"metadata": {"task": "reviewing PR"}},
            timeout=_mcp_timeout(),
        )

    def test_no_terminal_id_returns_error_without_network_call(self):
        with patch("cli_agent_orchestrator.mcp_server.server.requests.patch") as mock_patch:
            with patch.dict(os.environ, {}, clear=True):
                result = _update_metadata_impl({"task": "x"})

            assert result["success"] is False
            assert "CAO_TERMINAL_ID not set" in result["error"]
            mock_patch.assert_not_called()

    @patch("cli_agent_orchestrator.mcp_server.server.requests.patch")
    def test_http_error_surfaces_server_detail(self, mock_patch):
        response = MagicMock()
        response.json.return_value = {"detail": "Terminal 'caller-abc' not found"}
        http_error = requests.HTTPError("404 Client Error")
        http_error.response = response
        response.raise_for_status.side_effect = http_error
        mock_patch.return_value = response

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            result = _update_metadata_impl({"task": "x"})

        assert result["success"] is False
        assert "Terminal 'caller-abc' not found" in result["error"]
