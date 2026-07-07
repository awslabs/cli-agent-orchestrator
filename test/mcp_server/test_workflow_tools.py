"""Tests for the Bolt-3 workflow MCP tools (issue #312, N5).

``workflow_run`` / ``workflow_cancel`` are thin HTTP clients over the run-engine
endpoints. They return a structured envelope on EVERY path and NEVER raise into
the agent loop (B3-BR-15 / non-blocking promise). Covered: success envelope, a
server-error envelope, and a transport-error envelope.
"""

import asyncio
from unittest.mock import MagicMock, patch

import requests

from cli_agent_orchestrator.constants import MCP_REQUEST_TIMEOUT, WORKFLOW_RUN_REQUEST_TIMEOUT
from cli_agent_orchestrator.mcp_server.server import (
    workflow_cancel,
    workflow_resume,
    workflow_run,
)


def _resp(status_code, json_body):
    m = MagicMock(spec=requests.Response)
    m.status_code = status_code
    m.json.return_value = json_body
    return m


class TestWorkflowRun:
    def test_success_envelope(self):
        body = {
            "run_id": "run1",
            "state": "completed",
            "steps": [{"id": "s1", "state": "completed", "attempts": 1}],
        }
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post", return_value=_resp(200, body)
        ):
            out = asyncio.run(workflow_run("wf", inputs={"topic": "cats"}))
        assert out["ok"] is True
        assert out["run_id"] == "run1"
        assert out["state"] == "completed"
        assert out["steps"][0]["id"] == "s1"

    def test_server_error_envelope_no_raise(self):
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post",
            return_value=_resp(404, {"detail": "unknown workflow 'ghost'"}),
        ):
            out = asyncio.run(workflow_run("ghost", inputs={}))
        assert out["ok"] is False
        assert "unknown workflow" in out["error"]

    def test_transport_error_envelope_no_raise(self):
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post",
            side_effect=requests.ConnectionError("down"),
        ):
            out = asyncio.run(workflow_run("wf", inputs={}))
        assert out["ok"] is False
        assert "could not reach cao-server" in out["error"]

    def test_run_uses_long_client_timeout_not_flat_30s(self):
        """B1 regression guard: the blocking run must NOT use the flat 30s timeout.

        ``start_run`` is awaited inline, so the HTTP request blocks for the whole
        run; a flat ``MCP_REQUEST_TIMEOUT`` (=30s) would raise ``requests.Timeout``
        and report a still-running run as a failure. Assert the run-specific
        worst-case-covering timeout is passed, and that it is strictly greater than
        ``MCP_REQUEST_TIMEOUT`` so it can never silently revert to 30s.
        """
        body = {"run_id": "run1", "state": "completed", "steps": []}
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post", return_value=_resp(200, body)
        ) as post:
            asyncio.run(workflow_run("wf", inputs={}))
        assert post.call_args.kwargs["timeout"] == WORKFLOW_RUN_REQUEST_TIMEOUT
        assert WORKFLOW_RUN_REQUEST_TIMEOUT > MCP_REQUEST_TIMEOUT


class TestWorkflowResume:
    def test_success_envelope(self):
        body = {
            "run_id": "run1",
            "state": "completed",
            "steps": [{"id": "s1", "state": "completed", "attempts": 1}],
        }
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post", return_value=_resp(200, body)
        ):
            out = asyncio.run(workflow_resume("run1"))
        assert out["ok"] is True
        assert out["run_id"] == "run1"
        assert out["state"] == "completed"
        assert out["steps"][0]["id"] == "s1"

    def test_server_error_envelope_no_raise(self):
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post",
            return_value=_resp(409, {"detail": "run 'run1' is completed; not resumable"}),
        ):
            out = asyncio.run(workflow_resume("run1"))
        assert out["ok"] is False
        assert "not resumable" in out["error"]

    def test_transport_error_envelope_no_raise(self):
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post",
            side_effect=requests.ConnectionError("down"),
        ):
            out = asyncio.run(workflow_resume("run1"))
        assert out["ok"] is False
        assert "could not reach cao-server" in out["error"]

    def test_timeout_envelope_no_raise(self):
        # A requests.Timeout is a RequestException — it must land in the same
        # never-raises transport envelope, not escape into the agent loop.
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post",
            side_effect=requests.Timeout("too slow"),
        ):
            out = asyncio.run(workflow_resume("run1"))
        assert out["ok"] is False
        assert "could not reach cao-server" in out["error"]

    def test_resume_uses_long_run_timeout_not_flat_30s(self):
        # Resume re-drives the whole run inline, so it must use the worst-case
        # WORKFLOW_RUN_REQUEST_TIMEOUT, not the flat per-call MCP_REQUEST_TIMEOUT.
        body = {"run_id": "run1", "state": "completed", "steps": []}
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post", return_value=_resp(200, body)
        ) as post:
            asyncio.run(workflow_resume("run1"))
        assert post.call_args.kwargs["timeout"] == WORKFLOW_RUN_REQUEST_TIMEOUT
        assert WORKFLOW_RUN_REQUEST_TIMEOUT > MCP_REQUEST_TIMEOUT


class TestWorkflowCancel:
    def test_success_envelope(self):
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post",
            return_value=_resp(200, {"success": True, "run_id": "run1"}),
        ) as post:
            out = asyncio.run(workflow_cancel("run1"))
        assert out["ok"] is True
        assert out["run_id"] == "run1"
        # cancel is a quick write — it correctly keeps the flat MCP_REQUEST_TIMEOUT.
        assert post.call_args.kwargs["timeout"] == MCP_REQUEST_TIMEOUT

    def test_conflict_envelope_no_raise(self):
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post",
            return_value=_resp(409, {"detail": "run 'run1' is already completed"}),
        ):
            out = asyncio.run(workflow_cancel("run1"))
        assert out["ok"] is False
        assert "already completed" in out["error"]

    def test_transport_error_envelope_no_raise(self):
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post",
            side_effect=requests.ConnectionError("down"),
        ):
            out = asyncio.run(workflow_cancel("run1"))
        assert out["ok"] is False
        assert "could not reach cao-server" in out["error"]
