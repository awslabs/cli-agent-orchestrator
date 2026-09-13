"""Tests for delete_terminal MCP tool and _get_cleanup_nudge helper."""

import os
from unittest.mock import MagicMock, patch

import requests

from cli_agent_orchestrator.mcp_server.server import (
    _get_terminal_context_from_env,
    delete_terminal,
)
from cli_agent_orchestrator.utils.orchestration import _current_terminal_id, _get_cleanup_nudge


class TestCurrentTerminalId:
    def test_empty_terminal_id_is_treated_as_unset(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": ""}):
            assert _current_terminal_id() is None


class TestGetCleanupNudge:
    def test_returns_empty_when_no_terminal_id_env(self):
        with patch.dict(os.environ, {}, clear=True):
            assert _get_cleanup_nudge() == ""

    def test_returns_empty_when_terminal_fetch_fails(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            with patch("cli_agent_orchestrator.utils.orchestration.requests.get") as mock_get:
                mock_get.return_value.status_code = 500
                assert _get_cleanup_nudge() == ""

    def test_returns_empty_when_no_session_name(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            with patch("cli_agent_orchestrator.utils.orchestration.requests.get") as mock_get:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = {}  # no session_name
                mock_get.return_value = mock_resp
                assert _get_cleanup_nudge() == ""

    def test_returns_empty_when_sessions_fetch_fails(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            with patch("cli_agent_orchestrator.utils.orchestration.requests.get") as mock_get:
                terminal_resp = MagicMock()
                terminal_resp.status_code = 200
                terminal_resp.json.return_value = {"session_name": "cao-test"}
                sessions_resp = MagicMock()
                sessions_resp.status_code = 500
                mock_get.side_effect = [terminal_resp, sessions_resp]
                assert _get_cleanup_nudge() == ""

    def test_returns_empty_when_below_threshold(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            with patch("cli_agent_orchestrator.utils.orchestration.requests.get") as mock_get:
                terminal_resp = MagicMock()
                terminal_resp.status_code = 200
                terminal_resp.json.return_value = {"session_name": "cao-test"}
                sessions_resp = MagicMock()
                sessions_resp.status_code = 200
                sessions_resp.json.return_value = [{}] * 5  # below threshold of 10
                mock_get.side_effect = [terminal_resp, sessions_resp]
                assert _get_cleanup_nudge() == ""

    def test_returns_nudge_when_at_threshold(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            with patch("cli_agent_orchestrator.utils.orchestration.requests.get") as mock_get:
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
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            with patch(
                "cli_agent_orchestrator.utils.orchestration.requests.get",
                side_effect=Exception("network error"),
            ):
                assert _get_cleanup_nudge() == ""

    def test_skips_lookup_for_malformed_terminal_id(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "supervisor-abc123"}):
            with patch("cli_agent_orchestrator.utils.orchestration.requests.get") as mock_get:
                assert _get_cleanup_nudge() == ""
        mock_get.assert_not_called()


class TestMemoryTerminalContext:
    def test_malformed_terminal_id_degrades_without_lookup(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "supervisor-abc123"}):
            with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get:
                assert _get_terminal_context_from_env() is None
        mock_get.assert_not_called()

    def test_unknown_terminal_404_is_no_context(self):
        """A 404 genuinely means "no such terminal" -> None, degrade to global."""
        response = MagicMock()
        response.status_code = 404
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "abc12345"}):
            with patch(
                "cli_agent_orchestrator.mcp_server.utils.get_json",
                side_effect=requests.HTTPError("404", response=response),
            ):
                assert _get_terminal_context_from_env() is None

    def test_auth_failure_propagates_instead_of_reading_as_no_context(self):
        """A 401/500 must NOT degrade to None.

        Returning None here is the original bug: with auth enabled the scope-gated
        terminal lookup 401'd, the context resolved to None, and the memory scope
        silently collapsed to global — a wrong-but-plausible scope rather than an
        error. Only a 404 may mean "no context".
        """
        for code in (401, 403, 500, 503):
            response = MagicMock()
            response.status_code = code
            with patch.dict(os.environ, {"CAO_TERMINAL_ID": "abc12345"}):
                with patch(
                    "cli_agent_orchestrator.mcp_server.utils.get_json",
                    side_effect=requests.HTTPError(str(code), response=response),
                ):
                    try:
                        _get_terminal_context_from_env()
                    except requests.HTTPError:
                        continue
                    raise AssertionError(f"HTTP {code} silently degraded to no-context")

    def test_transport_failure_propagates(self):
        """An unreachable server is not "no terminal identity" either."""
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "abc12345"}):
            with patch(
                "cli_agent_orchestrator.mcp_server.utils.get_json",
                side_effect=requests.ConnectionError("refused"),
            ):
                try:
                    _get_terminal_context_from_env()
                except requests.RequestException:
                    return
                raise AssertionError("a down server silently degraded to no-context")


class TestDeleteTerminal:
    def test_success(self):
        with patch("cli_agent_orchestrator.utils.orchestration.requests.delete") as mock_delete:
            mock_delete.return_value.raise_for_status.return_value = None
            result = delete_terminal("t1")
        assert result["success"] is True
        assert "t1" in result["message"]

    def test_deferred_cleanup_returns_retryable_failure(self):
        with patch("cli_agent_orchestrator.utils.orchestration.requests.delete") as mock_delete:
            mock_delete.return_value.raise_for_status.return_value = None
            mock_delete.return_value.json.return_value = {"success": False}
            result = delete_terminal("t1")
        assert result["success"] is False
        assert "retry" in result["message"]

    def test_deferred_cleanup_conflict_status_returns_retryable_failure(self):
        with patch("cli_agent_orchestrator.utils.orchestration.requests.delete") as mock_delete:
            mock_delete.return_value.status_code = 409
            result = delete_terminal("t1")
        assert result["success"] is False
        assert "retry" in result["message"]
        mock_delete.return_value.raise_for_status.assert_not_called()

    def test_not_found_returns_false(self):
        with patch("cli_agent_orchestrator.utils.orchestration.requests.delete") as mock_delete:
            http_err = requests.HTTPError()
            http_err.response = MagicMock()
            http_err.response.status_code = 404
            mock_delete.return_value.raise_for_status.side_effect = http_err
            result = delete_terminal("t1")
        assert result["success"] is False
        assert "not found" in result["message"]

    def test_http_error_non_404(self):
        with patch("cli_agent_orchestrator.utils.orchestration.requests.delete") as mock_delete:
            http_err = requests.HTTPError("500 Server Error")
            http_err.response = MagicMock()
            http_err.response.status_code = 500
            mock_delete.return_value.raise_for_status.side_effect = http_err
            result = delete_terminal("t1")
        assert result["success"] is False
        assert "Failed" in result["message"]

    def test_generic_exception(self):
        with patch(
            "cli_agent_orchestrator.utils.orchestration.requests.delete",
            side_effect=Exception("connection refused"),
        ):
            result = delete_terminal("t1")
        assert result["success"] is False
        assert "Failed" in result["message"]

    @patch("cli_agent_orchestrator.utils.orchestration.get_local_bearer", return_value="tok")
    def test_attaches_bearer_when_auth_enabled(self, _bearer):
        """Review on PR #634: the outgoing DELETE carries the local bearer when configured."""
        with patch("cli_agent_orchestrator.utils.orchestration.requests.delete") as mock_delete:
            mock_delete.return_value.raise_for_status.return_value = None
            delete_terminal("t1")

        _, kwargs = mock_delete.call_args
        assert kwargs["headers"] == {"Authorization": "Bearer tok"}


class TestMemoryToolsSurfaceContextFailures:
    """The behaviour change the PR body advertises, pinned.

    The memory tools previously fell back to a GLOBAL-scope write when the
    terminal lookup failed — a silent scope downgrade. Now the failure propagates
    out of ``_get_terminal_context_from_env`` and each tool reports it, so an
    operator sees "cannot reach cao-server" instead of a memory quietly filed in
    the wrong scope. Only the outcome tools had coverage for this shape.
    """

    @staticmethod
    def _run(coro):
        import asyncio

        return asyncio.run(coro)

    def test_memory_store_reports_a_transport_failure(self):
        from cli_agent_orchestrator.mcp_server import server as srv

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "abc12345"}):
            with patch.object(
                srv,
                "_get_terminal_context_from_env",
                side_effect=requests.ConnectionError("refused"),
            ):
                result = self._run(
                    srv.memory_store(
                        content="c",
                        memory_type="user",
                        scope="session",
                        key=None,
                        tags=None,
                    )
                )

        assert result["success"] is False
        # Not a silent global-scope write, and not "disabled".
        assert "disabled" not in result, result
        assert result["error"]

    def test_memory_recall_reports_a_transport_failure(self):
        from cli_agent_orchestrator.mcp_server import server as srv

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "abc12345"}):
            with patch.object(
                srv,
                "_get_terminal_context_from_env",
                side_effect=requests.ConnectionError("refused"),
            ):
                result = self._run(
                    srv.memory_recall(query=None, scope=None, memory_type=None, limit=10)
                )

        assert result["success"] is False
        assert "disabled" not in result, result
        assert result["error"]
