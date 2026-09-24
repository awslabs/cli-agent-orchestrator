"""Tests for durable handoff result retrieval by the MCP client (issue #447).

Verifies:
- _handoff_impl generates a job_id and passes it in the run-step payload.
- On requests.Timeout, the HandoffResult carries pending=True and job_id
  so the caller can retrieve the result later.
- The normal synchronous path still works and job_id/pending are absent.
"""

import asyncio
from unittest.mock import MagicMock, patch

import requests

from cli_agent_orchestrator.mcp_server.server import get_handoff_result
from cli_agent_orchestrator.utils.orchestration import HandoffContext, _handoff_impl


class FakeTimeout(Exception):
    """Stand-in for ``requests.Timeout`` on the patched ``requests`` module.

    Used in EVERY test here, including the ones that never time out. Assigning
    the bare ``Exception`` to ``mock_requests.Timeout`` instead (PR #453 review
    nit) turns production's ``except requests.Timeout:`` into a catch-all, so a
    test could pass because some unrelated exception was swallowed into the
    pending branch. A dedicated sentinel only matches what the test itself raised.
    """


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
    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_job_id_included_in_payload(self, mock_provider, _nudge):
        """Every handoff POST must include a job_id."""
        mock_provider.return_value = _ctx()
        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = _ok_response()
            mock_requests.Timeout = FakeTimeout
            asyncio.run(_handoff_impl("developer", "do task"))

        payload = mock_requests.post.call_args[1]["json"]
        assert "job_id" in payload
        # Must be a 32-char hex string (uuid4().hex format).
        jid = payload["job_id"]
        assert isinstance(jid, str) and len(jid) == 32
        int(jid, 16)  # raises if not valid hex

    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_transport_timeout_returns_pending_result_with_job_id(self, mock_provider):
        """On requests.Timeout the HandoffResult must carry pending=True and
        a non-None job_id so the caller can poll the retrieval endpoint."""
        mock_provider.return_value = _ctx()
        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
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

    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_success_path_has_no_pending(self, mock_provider, _nudge):
        """Normal synchronous success must not set pending=True on the result."""
        mock_provider.return_value = _ctx()
        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = _ok_response()
            mock_requests.Timeout = FakeTimeout
            result = asyncio.run(_handoff_impl("developer", "do task"))

        assert result.success is True
        # pending is None (not set) on the normal path — not True.
        assert result.pending is not True

    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_each_call_generates_unique_job_id(self, mock_provider):
        """Separate calls must generate distinct job_ids (no collision)."""
        mock_provider.return_value = _ctx()
        ids_seen = set()
        for _ in range(5):
            with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
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


class TestGetHandoffResultAuthAndPlacement:
    """PR #453 review (haofeif), two P2 roots on the SAME request.

    The retrieval endpoint is scope-gated and the row lives on whichever node
    ran the step, so the GET needs the internal bearer header AND the caller's
    node selection. Both mirror what ``delete_terminal`` already does.
    """

    JOB = "cafe1234" * 4

    def _ok_get(self, mock_get):
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.json.return_value = {
            "state": "completed",
            "terminal_id": "dev-t1",
            "last_message": "done",
            "error_message": None,
        }

    def test_sends_internal_bearer_header_when_auth_enabled(self):
        """Without this header an auth-enabled deployment answers 401 to a
        caller legitimately holding the job_id."""
        with (
            patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get,
            patch(
                "cli_agent_orchestrator.mcp_server.utils.get_local_bearer",
                return_value="tok-123",
            ),
        ):
            self._ok_get(mock_get)
            result = get_handoff_result(self.JOB)

        assert result["success"] is True
        assert mock_get.call_args.kwargs["headers"] == {"Authorization": "Bearer tok-123"}

    def test_sends_no_header_when_auth_disabled(self):
        """Default-off posture must stay byte-for-byte unchanged: no token, no
        header (``_auth_headers() or None``), not an empty dict."""
        with (
            patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get,
            patch("cli_agent_orchestrator.mcp_server.utils.get_local_bearer", return_value=None),
        ):
            self._ok_get(mock_get)
            get_handoff_result(self.JOB)

        assert mock_get.call_args.kwargs["headers"] is None

    def test_local_retrieval_targets_the_supervisor_node(self):
        from cli_agent_orchestrator.constants import API_BASE_URL

        with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get:
            self._ok_get(mock_get)
            get_handoff_result(self.JOB)

        assert mock_get.call_args[0][0] == f"{API_BASE_URL}/handoff-results/{self.JOB}"

    def test_remote_retrieval_targets_the_node_that_ran_the_step(self):
        """handoff(target_host=...) persists the row in THAT node's database, so
        querying the supervisor's own base URL is a false not-found."""
        with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get:
            self._ok_get(mock_get)
            result = get_handoff_result(self.JOB, target_host="worker-7")

        assert result["success"] is True
        assert mock_get.call_args[0][0] == f"http://worker-7:9889/handoff-results/{self.JOB}"
        # A black-holed remote node must fail on CONNECT, not burn the read budget.
        assert isinstance(mock_get.call_args.kwargs["timeout"], tuple)

    def test_local_404_points_at_the_remote_possibility(self):
        """The one thing a supervisor can act on after a false not-found."""
        with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get:
            http_err = requests.HTTPError()
            http_err.response = MagicMock()
            http_err.response.status_code = 404
            mock_get.return_value.raise_for_status.side_effect = http_err
            result = get_handoff_result(self.JOB)

        assert result["success"] is False
        assert "target_host" in result["message"]

    def test_remote_404_names_the_node(self):
        with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get:
            http_err = requests.HTTPError()
            http_err.response = MagicMock()
            http_err.response.status_code = 404
            mock_get.return_value.raise_for_status.side_effect = http_err
            result = get_handoff_result(self.JOB, target_host="worker-7")

        assert "worker-7" in result["message"]


class TestTimeoutMessageNamesTheNode:
    """The recovery instruction has to be followable. For a remote handoff it
    must quote target_host, or the supervisor walks straight into the local-404.

    Driven through ``_run_step_and_build_result`` because that is the function
    that owns both the timeout branch and ``target_host`` -- no remote
    terminal-create machinery to stand up.
    """

    JOB = "cafe1234" * 4

    def _pending(self, target_host=None):
        from cli_agent_orchestrator.utils.orchestration import _run_step_and_build_result

        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.side_effect = FakeTimeout("timed out")
            mock_requests.Timeout = FakeTimeout
            return asyncio.run(
                _run_step_and_build_result(
                    {"job_id": self.JOB},
                    "developer",
                    "kiro_cli",
                    600,
                    0.0,
                    target_host=target_host,
                )
            )

    def test_remote_timeout_quotes_target_host(self):
        result = self._pending(target_host="worker-7")
        assert result.pending is True
        # Pinned to the RETRIEVAL clause, not just "target_host appears
        # somewhere": the pre-existing remote-cleanup hint in this same message
        # already says delete_terminal(..., target_host='worker-7'), so a looser
        # assertion passes with this fix reverted (confirmed by reverting it).
        assert (
            f"get_handoff_result tool, job_id={self.JOB}, target_host='worker-7'" in result.message
        )

    def test_local_timeout_omits_target_host(self):
        result = self._pending()
        assert result.pending is True
        assert f"get_handoff_result tool, job_id={self.JOB}" in result.message
        assert "target_host" not in result.message
