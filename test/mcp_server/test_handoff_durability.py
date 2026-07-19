"""Tests for durable handoff result retrieval by the MCP client (issue #447).

Verifies:
- _handoff_impl generates a job_id and passes it in the run-step payload.
- On requests.Timeout, the HandoffResult carries pending=True and job_id
  so the caller can retrieve the result later.
- The normal synchronous path still works and job_id/pending are absent.
"""

import asyncio
import uuid
from unittest.mock import MagicMock, patch

import requests

from cli_agent_orchestrator.mcp_server.server import (
    HandoffContext,
    _handoff_impl,
    get_handoff_result,
)


def _ctx(provider="kiro_cli", session_name=None, caller_id=None, allowed_tools=None):
    return HandoffContext(
        provider=provider,
        session_name=session_name,
        caller_id=caller_id,
        allowed_tools=allowed_tools,
    )


def _ok_response(terminal_id="dev-t1", last_message="done"):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "terminal_id": terminal_id,
        "last_message": last_message,
        "status": "completed",
    }
    return resp


class TestHandoffJobId:
    @patch("cli_agent_orchestrator.mcp_server.server._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.mcp_server.server._resolve_handoff_provider")
    def test_job_id_included_in_payload(self, mock_provider, _nudge):
        """Every handoff POST must include a job_id."""
        mock_provider.return_value = _ctx()
        with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_requests:
            mock_requests.post.return_value = _ok_response()
            mock_requests.Timeout = Exception
            asyncio.run(_handoff_impl("developer", "do task"))

        payload = mock_requests.post.call_args[1]["json"]
        assert "job_id" in payload
        # Must be a 32-char hex string (uuid4().hex format).
        jid = payload["job_id"]
        assert isinstance(jid, str) and len(jid) == 32
        int(jid, 16)  # raises if not valid hex

    @patch("cli_agent_orchestrator.mcp_server.server._resolve_handoff_provider")
    def test_transport_timeout_returns_pending_result_with_job_id(self, mock_provider):
        """On requests.Timeout the HandoffResult must carry pending=True and
        a non-None job_id so the caller can poll the retrieval endpoint."""
        mock_provider.return_value = _ctx()

        class FakeTimeout(Exception):
            pass

        with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_requests:
            mock_requests.post.side_effect = FakeTimeout("timed out")
            mock_requests.Timeout = FakeTimeout
            result = asyncio.run(_handoff_impl("developer", "do task", timeout=600))

        assert result.success is False
        assert result.pending is True
        assert result.job_id is not None
        assert len(result.job_id) == 32
        # Message must explain how to retrieve, via the get_handoff_result MCP
        # tool (PR #453 review finding 3) — a raw HTTP path gives the
        # supervisor LLM no callable path (no base URL, no auth).
        assert "get_handoff_result" in result.message
        assert result.job_id in result.message

    @patch("cli_agent_orchestrator.mcp_server.server._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.mcp_server.server._resolve_handoff_provider")
    def test_success_path_has_no_pending(self, mock_provider, _nudge):
        """Normal synchronous success must not set pending=True on the result."""
        mock_provider.return_value = _ctx()
        with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_requests:
            mock_requests.post.return_value = _ok_response()
            mock_requests.Timeout = Exception
            result = asyncio.run(_handoff_impl("developer", "do task"))

        assert result.success is True
        # pending is None (not set) on the normal path — not True.
        assert result.pending is not True

    @patch("cli_agent_orchestrator.mcp_server.server._resolve_handoff_provider")
    def test_each_call_generates_unique_job_id(self, mock_provider):
        """Separate calls must generate distinct job_ids (no collision)."""
        mock_provider.return_value = _ctx()
        ids_seen = set()

        class FakeTimeout(Exception):
            pass

        for _ in range(5):
            with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_requests:
                mock_requests.post.side_effect = FakeTimeout("timed out")
                mock_requests.Timeout = FakeTimeout
                result = asyncio.run(_handoff_impl("developer", "do task"))
            ids_seen.add(result.job_id)

        assert len(ids_seen) == 5


class TestGetHandoffResultTool:
    """The MCP tool half of the polling contract (PR #453 review finding 3):
    a pending handoff's job_id must be retrievable through a callable tool,
    not just a bare HTTP path the supervisor LLM cannot reach."""

    def test_completed_result_returned(self):
        with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get:
            mock_get.return_value.raise_for_status.return_value = None
            mock_get.return_value.json.return_value = {
                "state": "completed",
                "terminal_id": "dev-t1",
                "last_message": "done",
                "error_message": None,
            }
            result = get_handoff_result("cafe1234" * 4)
        assert result["success"] is True
        assert result["state"] == "completed"
        assert result["last_message"] == "done"

    def test_running_result_returned(self):
        with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get:
            mock_get.return_value.raise_for_status.return_value = None
            mock_get.return_value.json.return_value = {
                "state": "running",
                "terminal_id": None,
                "last_message": None,
                "error_message": None,
            }
            result = get_handoff_result("cafe1234" * 4)
        assert result["success"] is True
        assert result["state"] == "running"

    def test_unknown_job_id_returns_not_found(self):
        with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get:
            http_err = requests.HTTPError()
            http_err.response = MagicMock()
            http_err.response.status_code = 404
            mock_get.return_value.raise_for_status.side_effect = http_err
            result = get_handoff_result("deadbeef" * 4)
        assert result["success"] is False
        assert "No handoff result found" in result["message"]

    def test_generic_failure_returns_false(self):
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get",
            side_effect=Exception("connection refused"),
        ):
            result = get_handoff_result("deadbeef" * 4)
        assert result["success"] is False
        assert "Failed" in result["message"]
