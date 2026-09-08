"""Tests for cross-node placement + callback routing (one-agent-per-pod topology).

Covers the ``target_host`` parameter on assign/handoff (worker created on a
REMOTE CAO node via its REST API, with the remote HTTP calls mocked) and the
worker-side callback routing (``CAO_CALLBACK_URL`` / ``CAO_CALLBACK_TERMINAL_ID``
env vars steering send_message deliveries to the supervisor's node).
"""

import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from cli_agent_orchestrator.constants import API_BASE_URL
from cli_agent_orchestrator.mcp_server.server import delete_terminal
from cli_agent_orchestrator.utils.orchestration import (
    REMOTE_CONNECT_TIMEOUT,
    _assign_impl,
    _handoff_impl,
    _mcp_timeout,
    _resolve_remote_provider,
    _resolve_target_base_url,
    _send_message_impl,
    _send_to_inbox,
)

# The cross-node implementation lives in utils/orchestration.py, not
# mcp_server/server.py (issue #616 extracted the single shared seam behind both
# the MCP tools and the `cao agent` CLI). Only the `delete_terminal` TOOL
# wrapper is still server-local, and it delegates to the moved
# `_delete_terminal_impl`, so patching this module covers it too.
_SRV = "cli_agent_orchestrator.utils.orchestration"


def _response(status_code=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body if json_body is not None else {}
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(
            f"status {status_code}", response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


class TestResolveTargetBaseUrl:
    def test_bare_hostname_gets_default_port(self):
        assert (
            _resolve_target_base_url("cao-worker-0.cao-workers")
            == "http://cao-worker-0.cao-workers:9889"
        )

    def test_host_port_pair_is_kept(self):
        assert _resolve_target_base_url("cao-worker-1:1234") == "http://cao-worker-1:1234"

    def test_full_url_is_normalized_without_trailing_slash(self):
        assert _resolve_target_base_url("http://cao-worker-2:9889/") == "http://cao-worker-2:9889"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="target_host"):
            _resolve_target_base_url("  ")


class TestResolveRemoteProvider:
    @patch(f"{_SRV}.requests")
    def test_uses_remote_profile_provider(self, mock_requests):
        mock_requests.get.return_value = _response(
            200, {"name": "developer", "provider": "mock_cli"}
        )
        provider = _resolve_remote_provider("http://cao-worker-0:9889", "developer")
        assert provider == "mock_cli"
        mock_requests.get.assert_called_once_with(
            "http://cao-worker-0:9889/agents/profiles/developer",
            timeout=(REMOTE_CONNECT_TIMEOUT, _mcp_timeout()),
        )

    @patch(f"{_SRV}.requests")
    def test_missing_remote_profile_falls_back_to_default(self, mock_requests):
        from cli_agent_orchestrator.constants import DEFAULT_PROVIDER

        mock_requests.get.return_value = _response(404)
        assert _resolve_remote_provider("http://x:9889", "ghost") == DEFAULT_PROVIDER

    @patch(f"{_SRV}.requests")
    def test_unpinned_remote_profile_falls_back_to_default(self, mock_requests):
        from cli_agent_orchestrator.constants import DEFAULT_PROVIDER

        mock_requests.get.return_value = _response(200, {"name": "developer"})
        assert _resolve_remote_provider("http://x:9889", "developer") == DEFAULT_PROVIDER

    @pytest.mark.parametrize("status_code", [401, 429, 503])
    @patch(f"{_SRV}.requests")
    def test_remote_profile_http_error_is_not_treated_as_missing(self, mock_requests, status_code):
        mock_requests.get.return_value = _response(status_code)
        with pytest.raises(requests.HTTPError, match=f"status {status_code}"):
            _resolve_remote_provider("http://x:9889", "developer")

    def test_unreachable_node_raises_instead_of_falling_back(self):
        """A connection-level failure must fail FAST (it doubles as the
        reachability probe) — never guess DEFAULT_PROVIDER and then post the
        real work to the same dead node."""
        with patch(
            f"{_SRV}.requests.get", side_effect=requests.ConnectionError("no route to host")
        ):
            with pytest.raises(ValueError, match="cannot reach remote CAO node"):
                _resolve_remote_provider("http://cao-worker-9:9889", "developer")


class TestAssignRemote:
    """assign(target_host=...) creates the worker on the remote node with callback env."""

    _ENV = {
        "CAO_TERMINAL_ID": "a1b2c3d4",
        "CAO_ADVERTISED_URL": "http://cao-supervisor:9889/",
    }

    @patch(f"{_SRV}.requests")
    def test_posts_deferred_session_to_remote_node(self, mock_requests):
        mock_requests.post.return_value = _response(
            201, {"id": "beef0001", "session_name": "cao-remote"}
        )
        with patch.dict(os.environ, self._ENV, clear=False):
            result = _assign_impl(
                "developer", "Analyze the logs", target_host="cao-worker-0.cao-workers"
            )

        assert result["success"] is True
        assert result["terminal_id"] == "beef0001"
        assert result["target_host"] == "cao-worker-0.cao-workers"
        # MF3: the remote session must be surfaced with a usable cleanup route
        # (the raw "id" alone left the supervisor no way to free the slot).
        assert result["session_name"] == "cao-remote"
        assert result["delete_url"] == "http://cao-worker-0.cao-workers:9889/sessions/cao-remote"
        assert "delete_terminal('beef0001', target_host='cao-worker-0.cao-workers')" in (
            result["message"]
        )

        args, kwargs = mock_requests.post.call_args
        assert args[0] == "http://cao-worker-0.cao-workers:9889/sessions"
        # Remote calls bound the connect leg separately (S2).
        assert kwargs["timeout"] == (REMOTE_CONNECT_TIMEOUT, _mcp_timeout())
        # Provider deliberately omitted: the remote node resolves it from its
        # own installed profile store.
        assert kwargs["params"] == {"agent_profile": "developer"}
        body = kwargs["json"]
        # Task text plus the injected callback instructions suffix.
        assert body["initial_message"].startswith("Analyze the logs")
        assert "a1b2c3d4" in body["initial_message"]
        assert body["initial_message_orchestration_type"] == "assign"
        # Callback env: advertised URL (trailing slash stripped) + supervisor id.
        assert body["env_vars"] == {
            "CAO_CALLBACK_URL": "http://cao-supervisor:9889",
            "CAO_CALLBACK_TERMINAL_ID": "a1b2c3d4",
        }

    @patch(f"{_SRV}.requests")
    def test_elastic_callback_url_overrides_the_full_control_api(self, mock_requests):
        mock_requests.post.return_value = _response(
            201, {"id": "beef0001", "session_name": "cao-worker-deadbeef"}
        )
        with patch.dict(os.environ, self._ENV, clear=False):
            result = _assign_impl(
                "developer",
                "Analyze the logs",
                target_host="cao-worker-0",
                callback_url="http://cao-worker-broker:9890",
                remote_session_name="cao-worker-deadbeef",
            )

        assert result["success"] is True
        assert mock_requests.post.call_args.kwargs["params"]["session_name"] == (
            "cao-worker-deadbeef"
        )
        assert mock_requests.post.call_args.kwargs["json"]["env_vars"] == {
            "CAO_CALLBACK_URL": "http://cao-worker-broker:9890",
            "CAO_CALLBACK_TERMINAL_ID": "a1b2c3d4",
        }

    @patch(f"{_SRV}.requests")
    def test_elastic_assignment_rejects_a_different_remote_session(self, mock_requests):
        mock_requests.post.return_value = _response(
            201, {"id": "beef0001", "session_name": "cao-unexpected"}
        )
        with patch.dict(os.environ, self._ENV, clear=False):
            result = _assign_impl(
                "developer",
                "Analyze the logs",
                target_host="cao-worker-0",
                callback_url="http://cao-worker-broker:9890",
                remote_session_name="cao-worker-deadbeef",
            )

        assert result["success"] is False
        assert result["terminal_id"] == "beef0001"
        assert "cao-worker-deadbeef" in result["message"]
        assert "cao-unexpected" in result["message"]

    @patch(f"{_SRV}.requests")
    def test_fails_fast_without_advertised_url(self, mock_requests):
        env = {"CAO_TERMINAL_ID": "a1b2c3d4"}
        with patch.dict(os.environ, env, clear=True):
            result = _assign_impl("developer", "Task", target_host="cao-worker-0")
        assert result["success"] is False
        assert "CAO_ADVERTISED_URL" in result["message"]
        mock_requests.post.assert_not_called()

    @patch(f"{_SRV}.requests")
    def test_rejects_use_worktree_with_target_host(self, mock_requests):
        with patch.dict(os.environ, self._ENV, clear=False):
            result = _assign_impl(
                "developer", "Task", target_host="cao-worker-0", use_worktree=True
            )
        assert result["success"] is False
        assert "use_worktree" in result["message"]
        mock_requests.post.assert_not_called()

    @patch(f"{_SRV}._get_cleanup_nudge", return_value="")
    @patch(f"{_SRV}._create_terminal", return_value=("cafe0001", "mock_cli"))
    def test_omitted_target_host_keeps_local_path(self, mock_create, _nudge):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}, clear=False):
            result = _assign_impl("developer", "Task")
        assert result["success"] is True
        mock_create.assert_called_once()


class TestHandoffRemote:
    """handoff(target_host=...) drives run-step on the remote node."""

    @patch(f"{_SRV}._get_cleanup_nudge", return_value="")
    @patch(f"{_SRV}._resolve_remote_provider", return_value="mock_cli")
    @patch(f"{_SRV}.requests")
    def test_posts_run_step_to_remote_node(self, mock_requests, mock_provider, _nudge):
        mock_requests.post.return_value = _response(
            200, {"terminal_id": "beef0002", "last_message": "done", "status": "completed"}
        )
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}, clear=False):
            result = asyncio.run(_handoff_impl("developer", "Build it", target_host="cao-worker-1"))

        assert result.success is True
        assert result.output == "done"
        assert result.terminal_id == "beef0002"
        assert "cao-worker-1" in result.message

        mock_provider.assert_called_once_with("http://cao-worker-1:9889", "developer")
        args, kwargs = mock_requests.post.call_args
        assert args[0] == "http://cao-worker-1:9889/terminals/run-step"
        payload = kwargs["json"]
        assert payload["provider"] == "mock_cli"
        assert payload["agent"] == "developer"
        assert payload["prompt"] == "Build it"
        # Supervisor-node state must NOT leak into the remote request: the
        # remote DB has no supervisor session or terminal row.
        assert "session_name" not in payload
        assert "caller_id" not in payload
        assert "allowed_tools" not in payload

    @patch(f"{_SRV}._get_cleanup_nudge", return_value="")
    @patch(f"{_SRV}._resolve_handoff_provider")
    @patch(f"{_SRV}.requests")
    def test_omitted_target_host_posts_to_local_server(self, mock_requests, mock_ctx, _nudge):
        from cli_agent_orchestrator.utils.orchestration import HandoffContext

        mock_ctx.return_value = HandoffContext(
            provider="mock_cli", session_name="cao-s", caller_id="a1b2c3d4", allowed_tools=None
        )
        mock_requests.post.return_value = _response(
            200, {"terminal_id": "t", "last_message": "ok", "status": "completed"}
        )
        result = asyncio.run(_handoff_impl("developer", "Do it"))
        assert result.success is True
        args, _ = mock_requests.post.call_args
        assert args[0] == f"{API_BASE_URL}/terminals/run-step"


class TestWorkerSideCallbackRouting:
    """send_message on a REMOTE worker routes replies to the supervisor's node."""

    _CB_ENV = {
        "CAO_TERMINAL_ID": "feed0001",
        "CAO_CALLBACK_URL": "http://cao-supervisor:9889",
        "CAO_CALLBACK_TERMINAL_ID": "a1b2c3d4",
    }

    @patch(f"{_SRV}.requests")
    def test_inbox_post_targets_callback_url_for_supervisor_receiver(self, mock_requests):
        mock_requests.post.return_value = _response(200, {"success": True})
        with patch.dict(os.environ, self._CB_ENV, clear=False):
            _send_to_inbox("a1b2c3d4", "results")
        args, kwargs = mock_requests.post.call_args
        assert args[0] == "http://cao-supervisor:9889/terminals/a1b2c3d4/inbox/messages"
        assert kwargs["params"]["sender_id"] == "feed0001"
        assert kwargs["headers"] is None

    @patch(f"{_SRV}.requests")
    def test_elastic_callback_carries_per_worker_gateway_credentials(self, mock_requests):
        mock_requests.post.return_value = _response(200, {"success": True})
        env = {
            **self._CB_ENV,
            "CAO_CALLBACK_URL": "http://cao-worker-broker:9890",
            "CAO_ELASTIC_WORKER_ID": "deadbeef",
            "CAO_ELASTIC_RELEASE_TOKEN": "release-token",
        }
        with patch.dict(os.environ, env, clear=True):
            _send_to_inbox("a1b2c3d4", "results")

        args, kwargs = mock_requests.post.call_args
        assert args[0] == "http://cao-worker-broker:9890/terminals/a1b2c3d4/inbox/messages"
        assert kwargs["headers"] == {
            "X-CAO-Worker-ID": "deadbeef",
            "X-CAO-Release-Token": "release-token",
        }

    @patch(f"{_SRV}.requests")
    def test_inbox_post_stays_local_without_callback_env(self, mock_requests):
        mock_requests.post.return_value = _response(200, {"success": True})
        env = {"CAO_TERMINAL_ID": "feed0001"}
        with patch.dict(os.environ, env, clear=True):
            _send_to_inbox("a1b2c3d4", "results")
        args, _ = mock_requests.post.call_args
        assert args[0] == f"{API_BASE_URL}/terminals/a1b2c3d4/inbox/messages"

    @patch(f"{_SRV}.requests")
    def test_local_404_retries_against_callback_url(self, mock_requests):
        """A receiver unknown locally (e.g. an ID quoted from the task text
        that lives on the supervisor's node) is retried once cross-node."""
        local_404 = _response(404)
        remote_ok = _response(200, {"success": True})
        mock_requests.post.side_effect = [local_404, remote_ok]
        with patch.dict(os.environ, self._CB_ENV, clear=False):
            result = _send_to_inbox("0badf00d", "results")
        assert result == {"success": True}
        assert mock_requests.post.call_count == 2
        second_args, _ = mock_requests.post.call_args_list[1]
        assert second_args[0] == "http://cao-supervisor:9889/terminals/0badf00d/inbox/messages"

    @patch(f"{_SRV}._send_to_inbox", return_value={"success": True, "message_id": 1})
    def test_send_message_defaults_receiver_to_callback_terminal(self, mock_inbox):
        with patch.dict(os.environ, self._CB_ENV, clear=False):
            result = _send_message_impl(None, "all done")
        assert result["success"] is True
        receiver = mock_inbox.call_args[0][0]
        assert receiver == "a1b2c3d4"

    @patch(f"{_SRV}.requests")
    def test_callback_target_404_does_not_retry_cross_node_again(self, mock_requests):
        """When the FIRST post already went to the callback URL (receiver is
        the recorded supervisor) and 404s, there is nowhere else to retry —
        the failure must surface, with exactly one HTTP call made."""
        mock_requests.post.return_value = _response(404)
        with patch.dict(os.environ, self._CB_ENV, clear=False):
            with pytest.raises(Exception, match="status 404"):
                _send_to_inbox("a1b2c3d4", "results")
        assert mock_requests.post.call_count == 1
        args, _ = mock_requests.post.call_args
        assert args[0] == "http://cao-supervisor:9889/terminals/a1b2c3d4/inbox/messages"


class TestAssignRemoteErrorSurface:
    """S1: remote HTTP errors must carry the node's JSON detail, not a bare status."""

    _ENV = {
        "CAO_TERMINAL_ID": "a1b2c3d4",
        "CAO_ADVERTISED_URL": "http://cao-supervisor:9889",
    }

    @patch(f"{_SRV}.requests")
    def test_remote_429_surfaces_terminal_limit_detail(self, mock_requests):
        mock_requests.post.return_value = _response(
            429,
            {
                "detail": (
                    "Terminal limit reached: this node already has 1 tracked "
                    "terminal(s) ... target a different node."
                )
            },
        )
        with patch.dict(os.environ, self._ENV, clear=False):
            result = _assign_impl("developer", "Task", target_host="cao-worker-0")
        assert result["success"] is False
        assert "Terminal limit reached" in result["message"]
        assert "cao-worker-0" in result["message"]
        assert result["terminal_id"] is None


class TestHandoffRemoteFailureCleanup:
    """MF2a: a failed remote step must not leave the worker pod's only slot occupied."""

    @staticmethod
    def _run_step_error(status_code, kind, tid):
        return _response(
            status_code,
            {"detail": {"message": f"step failed ({kind})", "kind": kind, "terminal_id": tid}},
        )

    @patch(f"{_SRV}._get_cleanup_nudge", return_value="")
    @patch(f"{_SRV}._resolve_remote_provider", return_value="mock_cli")
    @patch(f"{_SRV}.requests")
    def test_remote_worker_error_triggers_cleanup_delete(self, mock_requests, _prov, _nudge):
        mock_requests.post.return_value = self._run_step_error(502, "error", "beef0003")
        mock_requests.delete.return_value = _response(200)
        result = asyncio.run(_handoff_impl("developer", "Task", target_host="cao-worker-0"))
        assert result.success is False
        assert "worker errored" in result.message
        assert "cleaned up" in result.message
        mock_requests.delete.assert_called_once()
        args, _ = mock_requests.delete.call_args
        assert args[0] == "http://cao-worker-0:9889/terminals/beef0003"

    @patch(f"{_SRV}._get_cleanup_nudge", return_value="")
    @patch(f"{_SRV}._resolve_remote_provider", return_value="mock_cli")
    @patch(f"{_SRV}.requests")
    def test_remote_timeout_kind_maps_and_cleans_up(self, mock_requests, _prov, _nudge):
        mock_requests.post.return_value = self._run_step_error(504, "timeout", "beef0004")
        mock_requests.delete.return_value = _response(200)
        result = asyncio.run(
            _handoff_impl("developer", "Task", timeout=42, target_host="cao-worker-0")
        )
        assert result.success is False
        assert "timed out after 42 seconds" in result.message
        assert result.terminal_id == "beef0004"
        mock_requests.delete.assert_called_once()

    @patch(f"{_SRV}._get_cleanup_nudge", return_value="")
    @patch(f"{_SRV}._resolve_remote_provider", return_value="mock_cli")
    # Patch the two methods individually (NOT the whole requests module) so
    # _cleanup_remote_terminal's `except requests.RequestException` still sees
    # the real exception class.
    @patch(f"{_SRV}.requests.delete", side_effect=requests.ConnectionError("unreachable"))
    @patch(f"{_SRV}.requests.post")
    def test_failed_cleanup_message_carries_manual_route(
        self, mock_post, mock_delete, _prov, _nudge
    ):
        mock_post.return_value = self._run_step_error(502, "error", "beef0005")
        result = asyncio.run(_handoff_impl("developer", "Task", target_host="cao-worker-0"))
        assert result.success is False
        assert "terminal beef0005 lives on cao-worker-0" in result.message
        assert "DELETE http://cao-worker-0:9889/terminals/beef0005" in result.message
        assert "delete_terminal('beef0005', target_host='cao-worker-0')" in result.message

    @patch(f"{_SRV}._get_cleanup_nudge", return_value="")
    @patch(f"{_SRV}._resolve_handoff_provider")
    @patch(f"{_SRV}.requests")
    def test_local_failure_does_not_call_delete(self, mock_requests, mock_ctx, _nudge):
        """Local behavior unchanged: server-side cleanup guidance/deletes are
        a remote-only addition."""
        from cli_agent_orchestrator.utils.orchestration import HandoffContext

        mock_ctx.return_value = HandoffContext(
            provider="mock_cli", session_name="cao-s", caller_id="a1b2c3d4", allowed_tools=None
        )
        mock_requests.post.return_value = self._run_step_error(502, "error", "beef0006")
        result = asyncio.run(_handoff_impl("developer", "Task"))
        assert result.success is False
        mock_requests.delete.assert_not_called()

    @patch(f"{_SRV}._get_cleanup_nudge", return_value="")
    @patch(f"{_SRV}._resolve_remote_provider", return_value="mock_cli")
    @patch(f"{_SRV}.requests")
    def test_remote_429_maps_detail_without_cleanup(self, mock_requests, _prov, _nudge):
        """A capacity rejection (429, plain-string detail, no terminal ever
        created) surfaces the node's detail and has nothing to clean up."""
        mock_requests.post.return_value = _response(
            429, {"detail": "Terminal limit reached: ... target a different node."}
        )
        result = asyncio.run(_handoff_impl("developer", "Task", target_host="cao-worker-0"))
        assert result.success is False
        assert "Terminal limit reached" in result.message
        mock_requests.delete.assert_not_called()


class TestHandoffRemoteGuards:
    @patch(f"{_SRV}.requests")
    def test_use_worktree_with_target_host_rejected(self, mock_requests):
        result = asyncio.run(
            _handoff_impl("developer", "Task", target_host="cao-worker-0", use_worktree=True)
        )
        assert result.success is False
        assert "use_worktree" in result.message
        mock_requests.post.assert_not_called()

    @patch(f"{_SRV}._get_cleanup_nudge", return_value="")
    @patch(
        f"{_SRV}._resolve_remote_provider",
        side_effect=ValueError("cannot reach remote CAO node at http://x:9889"),
    )
    @patch(f"{_SRV}.requests")
    def test_unreachable_node_fails_fast_without_posting_work(self, mock_requests, _prov, _nudge):
        result = asyncio.run(_handoff_impl("developer", "Task", target_host="x"))
        assert result.success is False
        assert "cannot reach remote CAO node" in result.message
        mock_requests.post.assert_not_called()

    @patch(f"{_SRV}._get_cleanup_nudge", return_value="")
    @patch(f"{_SRV}._resolve_remote_provider", return_value="mock_cli")
    @patch(f"{_SRV}.requests")
    def test_remote_run_step_uses_connect_read_timeout_tuple(self, mock_requests, _prov, _nudge):
        mock_requests.post.return_value = _response(
            200, {"terminal_id": "t", "last_message": "ok", "status": "completed"}
        )
        asyncio.run(_handoff_impl("developer", "Task", timeout=600, target_host="cao-worker-0"))
        _, kwargs = mock_requests.post.call_args
        assert kwargs["timeout"] == (REMOTE_CONNECT_TIMEOUT, 780.0)


class TestDeleteTerminalRemote:
    """MF2b: delete_terminal(target_host=...) frees a slot on a remote node."""

    @patch(f"{_SRV}.requests.delete")
    def test_remote_delete_targets_the_remote_node(self, mock_delete):
        mock_delete.return_value.raise_for_status.return_value = None
        result = delete_terminal("beef0007", target_host="cao-worker-0.cao-workers")
        assert result["success"] is True
        assert "cao-worker-0.cao-workers" in result["message"]
        args, kwargs = mock_delete.call_args
        assert args[0] == "http://cao-worker-0.cao-workers:9889/terminals/beef0007"
        assert kwargs["timeout"] == (REMOTE_CONNECT_TIMEOUT, _mcp_timeout())

    @patch(f"{_SRV}.requests.delete")
    def test_local_delete_unchanged_without_target_host(self, mock_delete):
        mock_delete.return_value.raise_for_status.return_value = None
        result = delete_terminal("beef0008")
        assert result["success"] is True
        args, kwargs = mock_delete.call_args
        assert args[0] == f"{API_BASE_URL}/terminals/beef0008"
        assert kwargs["timeout"] == _mcp_timeout()

    @patch(f"{_SRV}.requests.delete")
    def test_remote_not_found_reports_node(self, mock_delete):
        http_err = requests.HTTPError()
        http_err.response = MagicMock()
        http_err.response.status_code = 404
        mock_delete.return_value.raise_for_status.side_effect = http_err
        result = delete_terminal("beef0009", target_host="cao-worker-1")
        assert result["success"] is False
        assert "not found" in result["message"]
        assert "cao-worker-1" in result["message"]
