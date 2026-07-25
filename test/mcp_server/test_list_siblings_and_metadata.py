"""Tests for the #432 list_siblings and update_metadata MCP tools."""

import os
from unittest.mock import MagicMock, patch

import requests

from cli_agent_orchestrator.mcp_server.server import (
    _list_siblings_impl,
    _mcp_timeout,
    _require_discovery_marker,
    _update_metadata_impl,
)

# Every test below that isn't specifically exercising the discovery-marker
# gate itself (see TestRequireDiscoveryMarker /
# TestDiscoveryMarkerEnforcementInImpls) patches it out to a permissive
# no-op, so the pre-existing requests.get/patch assertions keep testing
# exactly what they tested before that gate was added (#432 design
# discussion, issue #432 comment thread, 2026-07-17/18).
_PERMISSIVE_MARKER = patch(
    "cli_agent_orchestrator.mcp_server.server._require_discovery_marker", return_value=None
)


class TestListSiblingsImpl:
    """Tests for the _list_siblings_impl helper."""

    @_PERMISSIVE_MARKER
    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_resolves_own_identity_from_env_not_an_argument(self, mock_get, mock_marker):
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

    @_PERMISSIVE_MARKER
    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_depth_forwarded_when_provided(self, mock_get, mock_marker):
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

    @_PERMISSIVE_MARKER
    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_cross_session_true_forwarded_as_query_param(self, mock_get, mock_marker):
        """cross_session=True (issue #432 design discussion) must reach the
        REST endpoint as a query param -- the default (False) sends no
        param at all, matching the depth-omitted shape above."""
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = []
        mock_get.return_value = response

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            _list_siblings_impl(None, cross_session=True)

        mock_get.assert_called_once_with(
            "http://127.0.0.1:9889/terminals/caller-abc/siblings",
            params={"cross_session": "true"},
            timeout=_mcp_timeout(),
        )

    @_PERMISSIVE_MARKER
    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_cross_session_false_omits_the_param(self, mock_get, mock_marker):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = []
        mock_get.return_value = response

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            _list_siblings_impl(None, cross_session=False)

        mock_get.assert_called_once_with(
            "http://127.0.0.1:9889/terminals/caller-abc/siblings",
            params={},
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

    @_PERMISSIVE_MARKER
    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_depth_zero_rejection_surfaces_server_detail(self, mock_get, mock_marker):
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

    @_PERMISSIVE_MARKER
    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_connection_error_returns_structured_error(self, mock_get, mock_marker):
        mock_get.side_effect = requests.ConnectionError("boom")

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            result = _list_siblings_impl(None)

        assert result["success"] is False
        assert "Failed to list siblings" in result["error"]


class TestUpdateMetadataImpl:
    """Tests for the _update_metadata_impl helper."""

    @_PERMISSIVE_MARKER
    @patch("cli_agent_orchestrator.mcp_server.server.requests.patch")
    def test_resolves_own_identity_and_replaces_metadata(self, mock_patch, mock_marker):
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

    @_PERMISSIVE_MARKER
    @patch("cli_agent_orchestrator.mcp_server.server.requests.patch")
    def test_http_error_surfaces_server_detail(self, mock_patch, mock_marker):
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


class TestRequireDiscoveryMarker:
    """Tests for _require_discovery_marker, the opt-in gate for sibling
    discovery tools (issue #432 design discussion, tedswinyar + klabulan,
    2026-07-17/18: a separate opt-in marker rather than bundling discovery
    into @cao-mcp-server's all-or-nothing grant)."""

    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_granted_when_discovery_in_allowed_tools(self, mock_get):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"allowed_tools": ["@cao-mcp-server", "discovery"]}
        mock_get.return_value = response

        result = _require_discovery_marker("caller-abc", "list siblings")

        assert result is None

    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_denied_when_orchestration_granted_but_not_discovery(self, mock_get):
        """The exact scenario the design discussion was about: a profile
        with orchestration tools (@cao-mcp-server) but NOT discovery must be
        denied -- discovery is not implied by having handoff/assign/
        send_message."""
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"allowed_tools": ["@cao-mcp-server", "fs_read"]}
        mock_get.return_value = response

        result = _require_discovery_marker("caller-abc", "list siblings")

        assert result is not None
        assert result["success"] is False
        assert "discovery" in result["error"]

    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_granted_when_unrestricted_wildcard(self, mock_get):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"allowed_tools": ["*"]}
        mock_get.return_value = response

        assert _require_discovery_marker("caller-abc", "list siblings") is None

    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_granted_when_allowed_tools_is_none(self, mock_get):
        """allowed_tools=None (no role/allowedTools resolved at all) means
        unrestricted, matching resolve_allowed_tools' own semantics
        elsewhere in this codebase -- not a denial."""
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"allowed_tools": None}
        mock_get.return_value = response

        assert _require_discovery_marker("caller-abc", "list siblings") is None

    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_fails_closed_on_lookup_error(self, mock_get):
        """An unresolvable allowed_tools lookup (network error, non-200,
        etc.) must deny rather than silently grant -- fail-closed, same
        posture as _own_terminal_id_or_error."""
        mock_get.side_effect = requests.ConnectionError("boom")

        result = _require_discovery_marker("caller-abc", "list siblings")

        assert result is not None
        assert result["success"] is False


class TestDiscoveryMarkerEnforcementInImpls:
    """Confirms _list_siblings_impl/_update_metadata_impl actually call the
    gate and short-circuit on denial, rather than the gate existing but
    never being wired in."""

    @patch("cli_agent_orchestrator.mcp_server.server._require_discovery_marker")
    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_list_siblings_denied_without_calling_siblings_endpoint(self, mock_get, mock_marker):
        mock_marker.return_value = {"success": False, "error": "not granted 'discovery'"}

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            result = _list_siblings_impl(None)

        assert result == {"success": False, "error": "not granted 'discovery'"}
        # The marker check itself uses requests.get for /terminals/{id}; the
        # siblings-list GET must never additionally fire once denied.
        mock_get.assert_not_called()

    @patch("cli_agent_orchestrator.mcp_server.server._require_discovery_marker")
    @patch("cli_agent_orchestrator.mcp_server.server.requests.patch")
    def test_update_metadata_denied_without_calling_metadata_endpoint(
        self, mock_patch, mock_marker
    ):
        mock_marker.return_value = {"success": False, "error": "not granted 'discovery'"}

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "caller-abc"}):
            result = _update_metadata_impl({"task": "x"})

        assert result == {"success": False, "error": "not granted 'discovery'"}
        mock_patch.assert_not_called()
