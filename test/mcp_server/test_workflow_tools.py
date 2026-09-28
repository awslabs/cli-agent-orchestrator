"""Tests for the Bolt-3 workflow MCP tools (issue #312, N5).

``workflow_run`` / ``workflow_cancel`` are thin HTTP clients over the run-engine
endpoints. They return a structured envelope on EVERY path and NEVER raise into
the agent loop (B3-BR-15 / non-blocking promise). Covered: success envelope, a
server-error envelope, and a transport-error envelope.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import requests

from cli_agent_orchestrator.constants import MCP_REQUEST_TIMEOUT, WORKFLOW_RUN_REQUEST_TIMEOUT
from cli_agent_orchestrator.mcp_server.server import (
    _mcp_timeout,
    workflow_cancel,
    workflow_list,
    workflow_result,
    workflow_resume,
    workflow_run,
    workflow_start,
    workflow_status,
    workflow_wait,
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

    # --- U3 (issue #505): optional caller-supplied run_id, forwarded on the wire ---
    def test_run_id_forwarded_on_wire(self):
        """FR1-1: a supplied run_id is placed on the request body sent to the
        blocking ``POST /workflows/runs`` (the id is on the wire)."""
        body = {"run_id": "my-run", "state": "completed", "steps": []}
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post", return_value=_resp(200, body)
        ) as post:
            out = asyncio.run(workflow_run("wf", inputs={}, run_id="my-run"))
        assert post.call_args.kwargs["json"]["run_id"] == "my-run"
        assert out["ok"] is True
        assert out["run_id"] == "my-run"

    def test_run_id_omitted_preserves_payload(self):
        """FR1-2: with no run_id the payload is byte-identical to today — no
        ``run_id`` key at all (the server mints the id) — and the success envelope
        is unchanged."""
        body = {
            "run_id": "srv-minted",
            "state": "completed",
            "steps": [{"id": "s1", "state": "completed", "attempts": 1}],
        }
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post", return_value=_resp(200, body)
        ) as post:
            out = asyncio.run(workflow_run("wf", inputs={"topic": "cats"}))
        sent = post.call_args.kwargs["json"]
        assert "run_id" not in sent
        assert sent == {"name_or_path": "wf", "inputs": {"topic": "cats"}}
        assert out["ok"] is True
        assert out["run_id"] == "srv-minted"
        assert out["state"] == "completed"
        assert out["steps"][0]["id"] == "s1"

    def test_run_id_collision_surfaces_error_envelope_no_raise(self):
        """FR1-3/FR1-4: a server 409 (admission gate collision) surfaces through the
        ``{ok: False, error}`` envelope — the tool never raises into the agent
        loop, and no client-side validation pre-empts the server's gate."""
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post",
            return_value=_resp(409, {"detail": "run_id 'dup' already exists"}),
        ):
            out = asyncio.run(workflow_run("wf", inputs={}, run_id="dup"))
        assert out["ok"] is False
        assert "already exists" in out["error"]

    def test_run_id_transport_error_envelope_no_raise(self):
        """FR1-4: a transport failure on the run_id path still lands in the
        never-raises transport envelope, not an exception."""
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post",
            side_effect=requests.ConnectionError("down"),
        ):
            out = asyncio.run(workflow_run("wf", inputs={}, run_id="x"))
        assert out["ok"] is False
        assert "could not reach cao-server" in out["error"]

    def test_run_id_path_still_uses_long_blocking_timeout(self):
        """FR1-5/C-6: supplying a run_id must NOT retarget the blocking path onto
        the short per-call ``MCP_REQUEST_TIMEOUT`` — the run stays blocking and
        keeps the worst-case-covering ``WORKFLOW_RUN_REQUEST_TIMEOUT``."""
        body = {"run_id": "x", "state": "completed", "steps": []}
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post", return_value=_resp(200, body)
        ) as post:
            asyncio.run(workflow_run("wf", inputs={}, run_id="x"))
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


# ===========================================================================
# U6 (issue #505): the five async lifecycle tools. Each returns a structured
# envelope on success, server error, AND transport error — never raises (EV-1),
# and each uses the normal per-call _mcp_timeout(), not the long blocking one.
# ===========================================================================
_SUBMIT_202 = {
    "run_id": "run1",
    "state": "running",
    "links": {"status": "/workflows/runs/run1", "result": "/workflows/runs/run1/result"},
}


class TestWorkflowStart:
    def test_success_envelope_surfaces_status_url(self):
        """T1 (MR-1): a 202 -> {ok, run_id, state, status_url} with a non-empty url."""
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post",
            return_value=_resp(202, _SUBMIT_202),
        ):
            out = asyncio.run(workflow_start("wf", inputs={"topic": "cats"}))
        assert out == {
            "ok": True,
            "run_id": "run1",
            "state": "running",
            "status_url": "/workflows/runs/run1",
        }
        assert out["status_url"]

    def test_run_id_forwarded_and_async_timeout(self):
        """T2 (MR-1, TR-1): a supplied run_id is on the wire, and the call uses the
        async _mcp_timeout(), NOT the long blocking WORKFLOW_RUN_REQUEST_TIMEOUT."""
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post",
            return_value=_resp(202, _SUBMIT_202),
        ) as post:
            asyncio.run(workflow_start("wf", inputs={}, run_id="my-run"))
        assert post.call_args.kwargs["json"]["run_id"] == "my-run"
        assert post.call_args.kwargs["timeout"] == _mcp_timeout()
        assert post.call_args.kwargs["timeout"] != WORKFLOW_RUN_REQUEST_TIMEOUT

    def test_run_id_omitted_not_on_wire(self):
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post",
            return_value=_resp(202, _SUBMIT_202),
        ) as post:
            asyncio.run(workflow_start("wf", inputs={"topic": "cats"}))
        assert "run_id" not in post.call_args.kwargs["json"]

    def test_server_error_and_transport_envelopes_no_raise(self):
        """T3 (EV-1): a non-2xx and a transport error both land in envelopes."""
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post",
            return_value=_resp(404, {"detail": "unknown workflow 'ghost'"}),
        ):
            out = asyncio.run(workflow_start("ghost"))
        assert out["ok"] is False
        assert "unknown workflow" in out["error"]

        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post",
            side_effect=requests.ConnectionError("down"),
        ):
            out = asyncio.run(workflow_start("wf"))
        assert out["ok"] is False
        assert "could not reach cao-server" in out["error"]


class TestWorkflowStatus:
    def test_success_envelope(self):
        """T4: a 200 snapshot -> {ok, run_id, state, current_step_id, steps}."""
        body = {
            "run_id": "run1",
            "state": "running",
            "current_step_id": "s1",
            "steps": [{"id": "s1", "state": "running", "attempts": 1}],
        }
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get",
            return_value=_resp(200, body),
        ) as get:
            out = asyncio.run(workflow_status("run1"))
        assert out["ok"] is True
        assert out["run_id"] == "run1"
        assert out["state"] == "running"
        assert out["current_step_id"] == "s1"
        assert out["steps"][0]["id"] == "s1"
        assert get.call_args.kwargs["timeout"] == _mcp_timeout()

    def test_not_found_envelope_no_raise(self):
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get",
            return_value=_resp(404, {"detail": "unknown run 'ghost'"}),
        ):
            out = asyncio.run(workflow_status("ghost"))
        assert out["ok"] is False
        assert "unknown run" in out["error"]

    def test_transport_error_envelope_no_raise(self):
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get",
            side_effect=requests.ConnectionError("down"),
        ):
            out = asyncio.run(workflow_status("run1"))
        assert out["ok"] is False
        assert "could not reach cao-server" in out["error"]


class TestWorkflowResult:
    def test_success_envelope_spreads_retained_result(self):
        """T5 (FR-7.2): a 200 result -> {ok: True, **retained result} (kind, steps)."""
        body = {
            "run_id": "run1",
            "workflow_name": "wf",
            "state": "completed",
            "steps": [{"id": "s1", "state": "completed", "attempts": 1}],
            "started_at": "t",
            "finished_at": "t2",
            "kind": None,
        }
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get",
            return_value=_resp(200, body),
        ):
            out = asyncio.run(workflow_result("run1"))
        assert out["ok"] is True
        assert out["run_id"] == "run1"
        assert out["state"] == "completed"
        assert out["steps"][0]["id"] == "s1"
        assert "kind" in out

    def test_not_found_envelope_no_raise(self):
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get",
            return_value=_resp(404, {"detail": "unknown run 'ghost'"}),
        ):
            out = asyncio.run(workflow_result("ghost"))
        assert out["ok"] is False
        assert "unknown run" in out["error"]

    def test_transport_error_envelope_no_raise(self):
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get",
            side_effect=requests.Timeout("slow"),
        ):
            out = asyncio.run(workflow_result("run1"))
        assert out["ok"] is False
        assert "could not reach cao-server" in out["error"]


class TestWorkflowList:
    def test_success_envelope_shape(self):
        """T6 (MR-3): a 200 array -> {ok: True, runs: [...]}."""
        rows = [{"run_id": "run1", "workflow_name": "wf", "state": "completed"}]
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get",
            return_value=_resp(200, rows),
        ) as get:
            out = asyncio.run(workflow_list())
        assert out == {"ok": True, "runs": rows}
        assert get.call_args.kwargs["timeout"] == _mcp_timeout()

    def test_empty_array_is_valid_ok_envelope(self):
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get",
            return_value=_resp(200, []),
        ):
            out = asyncio.run(workflow_list(state="failed", limit=10))
        assert out == {"ok": True, "runs": []}

    def test_state_and_limit_forwarded(self):
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get",
            return_value=_resp(200, []),
        ) as get:
            asyncio.run(workflow_list(state="running", limit=7))
        assert get.call_args.kwargs["params"] == {"limit": 7, "state": "running"}

    def test_error_and_transport_envelopes_no_raise(self):
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get",
            return_value=_resp(400, {"detail": "illegal run state filter 'bogus'"}),
        ):
            out = asyncio.run(workflow_list(state="bogus"))
        assert out["ok"] is False
        assert "illegal run state" in out["error"]

        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get",
            side_effect=requests.ConnectionError("down"),
        ):
            out = asyncio.run(workflow_list())
        assert out["ok"] is False
        assert "could not reach cao-server" in out["error"]


class TestWorkflowWait:
    def test_converges_to_terminal_then_result(self):
        """T7 (MR-2): poll running -> terminal, then fetch result -> {ok, run_id,
        state, kind, steps} with the terminal state.

        NO run-level ``output`` key (PR #525 review): the ``/result`` route it wraps has
        no run-level output to give — there is no such column on ``workflow_run`` — so
        the key this envelope used to carry was always null. The stub below therefore
        mirrors the real route and omits it. Per-step outputs are unaffected.
        """
        running = _resp(200, {"run_id": "run1", "state": "running", "steps": []})
        terminal = _resp(200, {"run_id": "run1", "state": "completed", "steps": []})
        result_body = _resp(
            200,
            {
                "run_id": "run1",
                "workflow_name": "wf",
                "state": "completed",
                "steps": [
                    {
                        "id": "s1",
                        "state": "completed",
                        "attempts": 1,
                        "output": {"answer": 42},
                    }
                ],
                "kind": None,
            },
        )
        with (
            patch(
                "cli_agent_orchestrator.mcp_server.server.requests.get",
                side_effect=[running, terminal, result_body],
            ),
            patch("cli_agent_orchestrator.mcp_server.server.asyncio.sleep", new=AsyncMock()),
        ):
            out = asyncio.run(workflow_wait("run1"))
        assert out["ok"] is True
        assert out["run_id"] == "run1"
        assert out["state"] == "completed"
        assert out["steps"][0]["id"] == "s1"
        # Per-step output still reaches the agent; only the run-level key is gone.
        assert out["steps"][0]["output"] == {"answer": 42}
        assert "output" not in out
        assert "kind" in out

    def test_poll_uses_async_timeout_not_long(self):
        """T9 (TR-1): the poll GET uses _mcp_timeout(), never the long blocking one."""
        terminal = _resp(200, {"run_id": "run1", "state": "completed", "steps": []})
        result_body = _resp(200, {"run_id": "run1", "state": "completed", "steps": []})
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get",
            side_effect=[terminal, result_body],
        ) as get:
            asyncio.run(workflow_wait("run1"))
        assert get.call_args_list[0].kwargs["timeout"] == _mcp_timeout()
        assert get.call_args_list[0].kwargs["timeout"] != WORKFLOW_RUN_REQUEST_TIMEOUT

    def test_transport_error_on_poll_no_raise(self):
        """T8 (EV-1): a transport error during the poll -> {ok: False, error}."""
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get",
            side_effect=requests.ConnectionError("down"),
        ):
            out = asyncio.run(workflow_wait("run1"))
        assert out["ok"] is False
        assert "could not reach cao-server" in out["error"]

    def test_not_found_on_poll_no_raise(self):
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get",
            return_value=_resp(404, {"detail": "unknown run 'ghost'"}),
        ):
            out = asyncio.run(workflow_wait("ghost"))
        assert out["ok"] is False
        assert "unknown run" in out["error"]

    def test_result_fetch_error_after_terminal_no_raise(self):
        """T8 (EV-1): a terminal poll then a failing result fetch still returns an
        envelope, never raises."""
        terminal = _resp(200, {"run_id": "run1", "state": "failed", "steps": []})
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get",
            side_effect=[terminal, requests.ConnectionError("down")],
        ):
            out = asyncio.run(workflow_wait("run1"))
        assert out["ok"] is False
        assert "could not reach cao-server" in out["error"]

    def test_wait_surfaces_failure_envelope_from_result_body(self):
        """U9-T10 (FR-7.1): a FAILED run's ``workflow_wait`` carries the failure
        envelope through from the result body into its dict."""
        terminal = _resp(200, {"run_id": "run1", "state": "failed", "steps": []})
        result_body = _resp(
            200,
            {
                "run_id": "run1",
                "workflow_name": "wf",
                "state": "failed",
                "steps": [{"id": "s1", "state": "failed", "attempts": 3}],
                "kind": "error",
                "failure_envelope": {
                    "failing_step": "s1",
                    "attempt": 3,
                    "error_kind": "error",
                    "terminal_reference": "run1",
                    "next_command": "cao workflow result run1",
                },
            },
        )
        with (
            patch(
                "cli_agent_orchestrator.mcp_server.server.requests.get",
                side_effect=[terminal, result_body],
            ),
            patch("cli_agent_orchestrator.mcp_server.server.asyncio.sleep", new=AsyncMock()),
        ):
            out = asyncio.run(workflow_wait("run1"))
        assert out["ok"] is True
        assert out["state"] == "failed"
        assert out["failure_envelope"]["failing_step"] == "s1"
        assert out["failure_envelope"]["attempt"] == 3
        assert out["failure_envelope"]["next_command"] == "cao workflow result run1"

    def test_wait_completed_run_omits_failure_envelope(self):
        """U9 (NFR-3): a COMPLETED run carries no failure envelope, so the key stays
        absent — the success shape is unchanged."""
        terminal = _resp(200, {"run_id": "run1", "state": "completed", "steps": []})
        result_body = _resp(
            200, {"run_id": "run1", "state": "completed", "steps": [], "kind": None}
        )
        with (
            patch(
                "cli_agent_orchestrator.mcp_server.server.requests.get",
                side_effect=[terminal, result_body],
            ),
            patch("cli_agent_orchestrator.mcp_server.server.asyncio.sleep", new=AsyncMock()),
        ):
            out = asyncio.run(workflow_wait("run1"))
        assert out["ok"] is True
        assert "failure_envelope" not in out


class TestWorkflowResultFailureEnvelope:
    """U9 (FR-7.1): ``workflow_result`` spreads the retained body verbatim, so a
    FAILED run's ``failure_envelope`` surfaces in the dict and a COMPLETED run
    carries none."""

    def test_result_spreads_failure_envelope_for_failed_run(self):
        body = {
            "run_id": "run1",
            "workflow_name": "wf",
            "state": "failed",
            "steps": [{"id": "s1", "state": "failed", "attempts": 2}],
            "kind": "timeout",
            "failure_envelope": {
                "failing_step": "s1",
                "attempt": 2,
                "error_kind": "timeout",
                "terminal_reference": "run1",
                "next_command": "cao workflow result run1",
            },
        }
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get",
            return_value=_resp(200, body),
        ):
            out = asyncio.run(workflow_result("run1"))
        assert out["ok"] is True
        assert out["failure_envelope"]["error_kind"] == "timeout"
        assert out["failure_envelope"]["terminal_reference"] == "run1"

    def test_result_completed_run_has_no_failure_envelope(self):
        body = {"run_id": "run1", "state": "completed", "steps": [], "kind": None}
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get",
            return_value=_resp(200, body),
        ):
            out = asyncio.run(workflow_result("run1"))
        assert out["ok"] is True
        assert "failure_envelope" not in out


class TestTimeoutSplitGuardC6:
    """U8 (issue #505, C-6): the consolidating MCP timeout split-guard. Asserts BOTH
    families in one place — the blocking tools (``workflow_run``, ``workflow_resume``)
    keep the long ``WORKFLOW_RUN_REQUEST_TIMEOUT``; the async tools (``workflow_start``,
    ``workflow_status``, ``workflow_result``, ``workflow_list``, and each
    ``workflow_wait`` poll) use the normal ``_mcp_timeout()``; and the constant
    relationship ``WORKFLOW_RUN_REQUEST_TIMEOUT > MCP_REQUEST_TIMEOUT`` (== the
    ceiling of ``_mcp_timeout()``) holds so an async tool can never silently revert
    to the long timeout. Overlaps the per-tool timeout assertions above; this is the
    single guard that fails loudly if EITHER family drifts."""

    def test_c6_timeout_split_guard_mcp(self):
        run_body = {"run_id": "run1", "state": "completed", "steps": []}

        # (a) The constant relationship — the split can never collapse.
        assert WORKFLOW_RUN_REQUEST_TIMEOUT > MCP_REQUEST_TIMEOUT
        assert WORKFLOW_RUN_REQUEST_TIMEOUT > _mcp_timeout()

        # (b) BLOCKING tools -> the long timeout.
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post",
            return_value=_resp(200, run_body),
        ) as post:
            asyncio.run(workflow_run("wf", inputs={}))
        assert post.call_args.kwargs["timeout"] == WORKFLOW_RUN_REQUEST_TIMEOUT

        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post",
            return_value=_resp(200, run_body),
        ) as post:
            asyncio.run(workflow_resume("run1"))
        assert post.call_args.kwargs["timeout"] == WORKFLOW_RUN_REQUEST_TIMEOUT

        # (c) ASYNC tools -> the normal per-call timeout. workflow_start is a POST;
        # status / result / list / the wait poll are GETs.
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.post",
            return_value=_resp(202, _SUBMIT_202),
        ) as post:
            asyncio.run(workflow_start("wf", inputs={}))
        assert post.call_args.kwargs["timeout"] == _mcp_timeout()

        for coro in (
            workflow_status("run1"),
            workflow_result("run1"),
            workflow_list(),
        ):
            with patch(
                "cli_agent_orchestrator.mcp_server.server.requests.get",
                return_value=_resp(200, {"run_id": "run1", "state": "completed", "steps": []}),
            ) as get:
                asyncio.run(coro)
            assert get.call_args.kwargs["timeout"] == _mcp_timeout()

        # workflow_wait's poll GET (the async per-poll call) also uses _mcp_timeout().
        terminal = _resp(200, {"run_id": "run1", "state": "completed", "steps": []})
        result_body = _resp(200, {"run_id": "run1", "state": "completed", "steps": []})
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get",
            side_effect=[terminal, result_body],
        ) as get:
            asyncio.run(workflow_wait("run1"))
        assert get.call_args_list[0].kwargs["timeout"] == _mcp_timeout()
