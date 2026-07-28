"""Tests for the U10 bounded MCP live-event follower (issue #505, FR-4.9/FR-7.4).

``workflow_events`` is a thin, CONSUMER-ONLY HTTP client over #504's events-follow
SSE route. #504's server route is NOT in this tree yet; every test STUBS the
streamed SSE response (a mock whose ``iter_lines`` replays the FINAL frame
contract). Like the other lifecycle MCP tools it returns a dict envelope on EVERY
path and NEVER raises into the agent loop (EV-1).

Gaps in the envelope come from server-DECLARED ``event: gap`` frames — the
``test_no_gap_frame_no_synthesized_gap`` test proves the tool does NOT infer a gap
from a seq jump (GD-1, render-not-infer). Marker: ``integration`` (NOT e2e).

black + isort (line 100).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from cli_agent_orchestrator.mcp_server.server import workflow_events

pytestmark = pytest.mark.integration


def _sse_lines(*frames: str):
    lines: list[str] = []
    for frame in frames:
        lines.extend(frame.split("\n"))
    return lines


def _event_frame(seq, event_type, step_id, state, ts="2026-07-28T00:00:00Z"):
    data = {
        "seq": seq,
        "run_id": "run1",
        "event_type": event_type,
        "step_id": step_id,
        "state": state,
        "ts": ts,
    }
    return f"event: {event_type}\ndata: {json.dumps(data)}\nid: {seq}\n"


def _gap_frame(after_seq, before_seq, missing_count, reason="append_failed"):
    data = {
        "after_seq": after_seq,
        "before_seq": before_seq,
        "missing_count": missing_count,
        "reason": reason,
    }
    return f"event: gap\ndata: {json.dumps(data)}\n"


def _stream_resp(*frames: str, status_code=200):
    r = MagicMock(spec=requests.Response)
    r.status_code = status_code
    r.iter_lines.return_value = iter(_sse_lines(*frames))
    r.close.return_value = None
    return r


class TestWorkflowEventsSuccess:
    def test_success_envelope_with_events_and_terminal_state(self):
        """Success envelope: events in seq order + terminal run state, gaps empty."""
        stream = _stream_resp(
            _event_frame(1, "step.completed", "s1", "completed"),
            _event_frame(2, "run.completed", None, "completed"),
        )
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get", return_value=stream
        ) as get:
            out = asyncio.run(workflow_events("run1"))
        assert out["ok"] is True
        assert out["run_id"] == "run1"
        assert out["state"] == "completed"
        assert [e["seq"] for e in out["events"]] == [1, 2]
        assert out["gaps"] == []
        # Requested the SSE variant of the events route.
        args, kwargs = get.call_args
        assert args[0].endswith("/workflows/runs/run1/events")
        assert kwargs["headers"]["Accept"] == "text/event-stream"
        assert kwargs["stream"] is True

    def test_declared_gap_surfaced_in_gaps_list(self):
        """GD-2: a server-DECLARED gap frame appears verbatim in ``gaps`` — the
        declared range, not a computed one."""
        stream = _stream_resp(
            _event_frame(19, "step.completed", "s1", "completed"),
            _gap_frame(after_seq=19, before_seq=23, missing_count=3),
            _event_frame(23, "run.completed", None, "completed"),
        )
        with patch("cli_agent_orchestrator.mcp_server.server.requests.get", return_value=stream):
            out = asyncio.run(workflow_events("run1"))
        assert out["ok"] is True
        assert out["gaps"] == [
            {"after_seq": 19, "before_seq": 23, "missing_count": 3, "reason": "append_failed"}
        ]
        # The gap frame is NOT counted as an event (no id, not a transition).
        assert [e["seq"] for e in out["events"]] == [19, 23]

    def test_no_gap_frame_no_synthesized_gap(self):
        """GD-1 (render-not-infer, MANDATED): a seq jump 19 -> 23 with NO declared
        gap frame yields an EMPTY ``gaps`` list. FAILS if the tool infers gaps from
        numbering."""
        stream = _stream_resp(
            _event_frame(19, "step.completed", "s1", "completed"),
            _event_frame(23, "run.completed", None, "completed"),
        )
        with patch("cli_agent_orchestrator.mcp_server.server.requests.get", return_value=stream):
            out = asyncio.run(workflow_events("run1"))
        assert out["ok"] is True
        assert out["gaps"] == []

    def test_bounded_by_max_events(self):
        """The follower stops after ``max_events`` frames even with no terminal — an
        MCP call cannot stream indefinitely."""
        stream = _stream_resp(
            _event_frame(1, "step.completed", "s1", "completed"),
            _event_frame(2, "step.completed", "s2", "completed"),
            _event_frame(3, "step.completed", "s3", "completed"),
            _event_frame(4, "run.completed", None, "completed"),
        )
        with patch("cli_agent_orchestrator.mcp_server.server.requests.get", return_value=stream):
            out = asyncio.run(workflow_events("run1", max_events=2))
        assert out["ok"] is True
        assert len(out["events"]) == 2
        # Stopped before the terminal frame -> state stays None (no run.* seen).
        assert out["state"] is None

    def test_terminal_stops_before_max_events(self):
        """The follower stops at a terminal state before exhausting ``max_events``."""
        stream = _stream_resp(
            _event_frame(1, "step.completed", "s1", "completed"),
            _event_frame(2, "run.failed", None, "failed"),
        )
        with patch("cli_agent_orchestrator.mcp_server.server.requests.get", return_value=stream):
            out = asyncio.run(workflow_events("run1", max_events=100))
        assert out["ok"] is True
        assert out["state"] == "failed"
        assert len(out["events"]) == 2

    def test_after_seq_forwarded_on_wire(self):
        """RS-1: a supplied ``after_seq`` is placed on the request params for exact
        resume."""
        stream = _stream_resp(_event_frame(6, "run.completed", None, "completed"))
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get", return_value=stream
        ) as get:
            out = asyncio.run(workflow_events("run1", after_seq=5))
        assert out["ok"] is True
        assert get.call_args.kwargs["params"]["after_seq"] == 5

    def test_after_seq_omitted_not_on_wire(self):
        """With no ``after_seq`` the params carry no cursor (read from the start)."""
        stream = _stream_resp(_event_frame(1, "run.completed", None, "completed"))
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get", return_value=stream
        ) as get:
            asyncio.run(workflow_events("run1"))
        assert "after_seq" not in get.call_args.kwargs["params"]


class TestWorkflowEventsNeverRaises:
    def test_server_error_envelope_no_raise(self):
        stream = _stream_resp(status_code=404)
        stream.json.return_value = {"detail": "unknown run 'ghost'"}
        with patch("cli_agent_orchestrator.mcp_server.server.requests.get", return_value=stream):
            out = asyncio.run(workflow_events("ghost"))
        assert out["ok"] is False
        assert "unknown run" in out["error"]

    def test_transport_error_on_open_envelope_no_raise(self):
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get",
            side_effect=requests.ConnectionError("down"),
        ):
            out = asyncio.run(workflow_events("run1"))
        assert out["ok"] is False
        assert "could not reach cao-server" in out["error"]

    def test_mid_stream_read_error_envelope_no_raise_keeps_partial(self):
        """A read failure MID-stream returns an envelope (never raises) and keeps the
        frames drained before the failure."""
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        resp.close.return_value = None

        def _raise_after_one():
            yield from _sse_lines(_event_frame(1, "step.completed", "s1", "completed"))
            raise requests.exceptions.ChunkedEncodingError("truncated")

        resp.iter_lines.return_value = _raise_after_one()
        with patch("cli_agent_orchestrator.mcp_server.server.requests.get", return_value=resp):
            out = asyncio.run(workflow_events("run1"))
        assert out["ok"] is False
        assert "stream read failed" in out["error"]
        assert len(out["events"]) == 1


def test_mcp_server_stays_http_only_boundary():
    """FR-7.4 / C-2: workflow_events reaches the run over HTTP only — it must not
    import #504's event read DAL. The dedicated AST guard
    (test_http_only_boundary) enforces clients.database/tmux repo-wide; here we
    assert the tool's own source references no engine/journal/DAL symbol."""
    import cli_agent_orchestrator.mcp_server.server as mod

    src = __import__("inspect").getsource(mod.workflow_events)
    for forbidden in ("workflow_journal", "workflow_service", "event_log", "clients.database"):
        assert forbidden not in src, f"MCP follower must not reference {forbidden}"
