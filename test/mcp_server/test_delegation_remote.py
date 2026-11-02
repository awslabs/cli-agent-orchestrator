"""Remote placement carries the caller's ceiling and fails closed on version skew.

Also covers the MCP workflow tools forwarding ``caller_id`` so the server can
bound workflow steps by the calling terminal's ceiling.
"""

import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from cli_agent_orchestrator.constants import API_BASE_URL
from cli_agent_orchestrator.mcp_server.server import workflow_resume, workflow_run, workflow_start
from cli_agent_orchestrator.utils.orchestration import (
    _assign_impl,
    _handoff_impl,
    _own_delegation_ceiling,
    _require_remote_delegation_ceiling,
)

_SRV = "cli_agent_orchestrator.utils.orchestration"
_MCP = "cli_agent_orchestrator.mcp_server.server"


def _response(status_code=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body if json_body is not None else {}
    resp.raise_for_status.return_value = None
    return resp


class TestRequireRemoteDelegationCeiling:
    @patch(f"{_SRV}.requests")
    def test_passes_when_the_node_advertises_the_feature(self, mock_requests):
        mock_requests.get.return_value = _response(
            200, {"status": "ok", "features": ["delegation_ceiling"]}
        )
        _require_remote_delegation_ceiling("http://cao-worker-0:9889", "cao-worker-0")
        assert mock_requests.get.call_args.args[0] == "http://cao-worker-0:9889/health"

    @patch(f"{_SRV}.requests")
    def test_older_node_without_the_feature_is_refused(self, mock_requests):
        mock_requests.get.return_value = _response(200, {"status": "ok"})
        with pytest.raises(ValueError, match="does not enforce delegation ceilings"):
            _require_remote_delegation_ceiling("http://cao-worker-0:9889", "cao-worker-0")

    @patch(f"{_SRV}.requests.get", side_effect=requests.ConnectionError("down"))
    def test_unreachable_node_is_refused(self, _get):
        with pytest.raises(ValueError, match="cannot verify"):
            _require_remote_delegation_ceiling("http://cao-worker-0:9889", "cao-worker-0")

    # The probe is a ``requests`` call built on ``base_url``, so the bearer census
    # (#991, ``test/test_bearer_call_site_census.py``) requires its ``headers=``
    # to come from a bearer helper. ``_auth_headers_for`` is the right one: it
    # is empty for any host but this node, so the probe never discloses the
    # local token to the remote node it is vetting. @haofeif re-review at
    # 95e580fd, P2.
    @patch(f"{_SRV}.get_local_bearer", return_value="tok")
    @patch(f"{_SRV}.requests.get")
    def test_probe_sends_no_local_bearer_to_a_remote_node(self, mock_get, _bearer):
        mock_get.return_value = _response(200, {"status": "ok", "features": ["delegation_ceiling"]})
        _require_remote_delegation_ceiling("http://cao-worker-0:9889", "cao-worker-0")
        assert mock_get.call_args.kwargs["headers"] is None

    @patch(f"{_SRV}.get_local_bearer", return_value="tok")
    @patch(f"{_SRV}.requests.get")
    def test_probe_carries_the_local_bearer_when_the_node_is_this_one(self, mock_get, _bearer):
        mock_get.return_value = _response(200, {"status": "ok", "features": ["delegation_ceiling"]})
        _require_remote_delegation_ceiling(API_BASE_URL, "localhost")
        assert mock_get.call_args.kwargs["headers"] == {"Authorization": "Bearer tok"}


class TestOwnDelegationCeiling:
    @patch(f"{_SRV}.requests")
    def test_reads_the_recorded_ceiling_from_the_own_row(self, mock_requests):
        mock_requests.get.return_value = _response(
            200, {"id": "a1b2c3d4", "allowed_tools": ["fs_read"], "delegate_tools": ["*"]}
        )
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            assert _own_delegation_ceiling() == ["*"]
        assert mock_requests.get.call_args.args[0] == f"{API_BASE_URL}/terminals/a1b2c3d4"

    @patch(f"{_SRV}.requests")
    def test_legacy_row_falls_back_to_own_allowlist(self, mock_requests):
        mock_requests.get.return_value = _response(
            200, {"id": "a1b2c3d4", "allowed_tools": ["fs_read"], "delegate_tools": None}
        )
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            assert _own_delegation_ceiling() == ["fs_read"]

    def test_requires_a_terminal_identity(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="CAO_TERMINAL_ID"):
                _own_delegation_ceiling()


class TestAssignRemoteFailsClosed:
    _ENV = {"CAO_TERMINAL_ID": "a1b2c3d4", "CAO_ADVERTISED_URL": "http://cao-supervisor:9889"}

    @patch(f"{_SRV}.requests")
    def test_no_feature_means_no_post(self, mock_requests):
        """An older remote node would ignore caller_ceiling and launch the worker
        unbounded, so nothing is posted and the message says why."""
        mock_requests.get.return_value = _response(200, {"status": "ok"})
        with patch.dict(os.environ, self._ENV, clear=False):
            result = _assign_impl("developer", "Task", target_host="cao-worker-0")
        assert result["success"] is False
        assert "does not enforce delegation ceilings" in result["message"]
        assert "cao-worker-0" in result["message"]
        mock_requests.post.assert_not_called()

    @patch(f"{_SRV}.requests")
    def test_ceiling_travels_when_the_node_enforces(self, mock_requests):
        health = _response(200, {"status": "ok", "features": ["delegation_ceiling"]})
        own_row = _response(
            200, {"id": "a1b2c3d4", "allowed_tools": ["fs_read"], "delegate_tools": ["fs_read"]}
        )
        mock_requests.get.side_effect = [health, own_row]
        mock_requests.post.return_value = _response(201, {"id": "beef0001", "session_name": "s"})
        with patch.dict(os.environ, self._ENV, clear=False):
            result = _assign_impl("developer", "Task", target_host="cao-worker-0")
        assert result["success"] is True
        assert mock_requests.post.call_args.kwargs["json"]["caller_ceiling"] == ["fs_read"]


class TestHandoffRemoteFailsClosed:
    @patch(f"{_SRV}._get_cleanup_nudge", return_value="")
    @patch(f"{_SRV}._resolve_remote_provider", return_value="mock_cli")
    @patch(f"{_SRV}.requests")
    def test_no_feature_means_no_run_step(self, mock_requests, _prov, _nudge):
        mock_requests.get.return_value = _response(200, {"status": "ok"})
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}, clear=False):
            result = asyncio.run(_handoff_impl("developer", "Task", target_host="cao-worker-1"))
        assert result.success is False
        assert "does not enforce delegation ceilings" in result.message
        mock_requests.post.assert_not_called()

    @patch(f"{_SRV}._get_cleanup_nudge", return_value="")
    @patch(f"{_SRV}._resolve_remote_provider", return_value="mock_cli")
    @patch(f"{_SRV}.requests")
    def test_ceiling_travels_in_the_run_step_payload(self, mock_requests, _prov, _nudge):
        health = _response(200, {"status": "ok", "features": ["delegation_ceiling"]})
        own_row = _response(200, {"id": "a1b2c3d4", "delegate_tools": ["@cao-mcp-server"]})
        mock_requests.get.side_effect = [health, own_row]
        mock_requests.post.return_value = _response(
            200, {"terminal_id": "beef0002", "last_message": "done", "status": "completed"}
        )
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}, clear=False):
            result = asyncio.run(_handoff_impl("developer", "Task", target_host="cao-worker-1"))
        assert result.success is True
        payload = mock_requests.post.call_args.kwargs["json"]
        assert payload["caller_ceiling"] == ["@cao-mcp-server"]
        assert "caller_id" not in payload


class TestWorkflowToolsForwardCaller:
    @patch(f"{_MCP}._tool_denied_reason", return_value=None)
    @patch(f"{_MCP}.requests.post")
    def test_workflow_run_sends_caller_id(self, mock_post, _denied):
        mock_post.return_value = _response(200, {"run_id": "r1", "state": "completed"})
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            asyncio.run(workflow_run("wf", inputs={}))
        assert mock_post.call_args.kwargs["json"]["caller_id"] == "a1b2c3d4"

    @patch(f"{_MCP}._tool_denied_reason", return_value=None)
    @patch(f"{_MCP}.requests.post")
    def test_workflow_start_sends_caller_id(self, mock_post, _denied):
        mock_post.return_value = _response(202, {"run_id": "r1", "links": {}})
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            asyncio.run(workflow_start("wf", inputs={}))
        assert mock_post.call_args.kwargs["json"]["caller_id"] == "a1b2c3d4"

    @patch(f"{_MCP}._tool_denied_reason", return_value=None)
    @patch(f"{_MCP}.requests.post")
    def test_workflow_resume_sends_caller_id(self, mock_post, _denied):
        mock_post.return_value = _response(200, {"run_id": "r1", "state": "completed"})
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            asyncio.run(workflow_resume("r1"))
        assert mock_post.call_args.kwargs["json"] == {"caller_id": "a1b2c3d4"}

    @patch(f"{_MCP}._tool_denied_reason", return_value=None)
    @patch(f"{_MCP}.requests.post")
    def test_operator_shell_sends_no_caller(self, mock_post, _denied):
        """No CAO_TERMINAL_ID: the payloads are byte-identical to before."""
        mock_post.return_value = _response(200, {"run_id": "r1", "state": "completed"})
        env = {k: v for k, v in os.environ.items() if k != "CAO_TERMINAL_ID"}
        with patch.dict(os.environ, env, clear=True):
            asyncio.run(workflow_run("wf", inputs={}))
            asyncio.run(workflow_resume("r1"))
        run_payload, resume_payload = (c.kwargs["json"] for c in mock_post.call_args_list)
        assert "caller_id" not in run_payload
        assert resume_payload is None


class TestHandoffPrecheckUsesTheRecordedCeiling:
    """Review A, T1: ``_resolve_handoff_provider`` must pass the parent's recorded
    ceiling to the precheck. With a parent row whose ``allowed_tools`` is NULL
    (legacy) but whose ``delegate_tools`` is the reviewer list, dropping the
    ``parent_ceiling`` argument would fall back to "unrestricted" and let the
    developer through; this test fails on that mutation."""

    @patch("cli_agent_orchestrator.utils.tool_mapping.resolve_allowed_tools")
    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    @patch(f"{_SRV}.resolve_provider", return_value="claude_code")
    def test_recorded_ceiling_bounds_the_handoff(self, _prov, mock_load, mock_resolve):
        from cli_agent_orchestrator.constants import ROLE_TOOL_DEFAULTS
        from cli_agent_orchestrator.utils.delegation import DelegationCeilingError
        from cli_agent_orchestrator.utils.orchestration import _resolve_handoff_provider

        profile = MagicMock()
        profile.allowedTools = None
        profile.role = "developer"
        profile.mcpServers = None
        mock_load.return_value = profile
        mock_resolve.return_value = ROLE_TOOL_DEFAULTS["developer"]
        meta = _response(
            200,
            {
                "provider": "claude_code",
                "session_name": "cao-sup",
                "agent_profile": "reviewer",
                "allowed_tools": None,
                "delegate_tools": ROLE_TOOL_DEFAULTS["reviewer"],
            },
        )
        with (
            patch.dict(os.environ, {"CAO_TERMINAL_ID": "c0ffee01"}),
            patch(f"{_SRV}.requests") as mock_requests,
        ):
            mock_requests.get.return_value = meta
            with pytest.raises(DelegationCeilingError) as excinfo:
                _resolve_handoff_provider("developer")
        assert excinfo.value.excess == ["fs_*", "execute_bash", "web_fetch"]
        assert "c0ffee01" in str(excinfo.value) and "reviewer" in str(excinfo.value)
