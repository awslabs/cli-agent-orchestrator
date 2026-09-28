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


class TestWorkflowEventsBounds:
    """issue #505 review: the socket is closed on EVERY path (FD-1) and the call is
    bounded in WALL-CLOCK, not only in events (TB-1)."""

    def test_non_200_closes_the_streamed_socket(self):
        """FD-1: the non-200 early return must close the streamed response.

        The request is made with ``stream=True``, so the connection stays open until
        it is explicitly closed or fully drained. The error arm returned without
        closing — leaking the socket/FD — while the success path correctly used
        ``try``/``finally``.

        MUTATION PROOF: revert the arm to a bare
        ``return {"ok": False, "error": detail}`` (no ``finally: response.close()``)
        and this goes RED.
        """
        stream = _stream_resp(status_code=503)
        stream.json.return_value = {"detail": "unavailable"}
        with patch("cli_agent_orchestrator.mcp_server.server.requests.get", return_value=stream):
            out = asyncio.run(workflow_events("run1"))
        assert out["ok"] is False
        stream.close.assert_called_once()

    def test_transport_error_while_reading_detail_still_closes(self):
        """FD-1 corner: even if reading the error body itself raises, the socket is
        still closed — the close sits in a ``finally``, not after the read."""
        stream = _stream_resp(status_code=500)
        stream.json.side_effect = ValueError("not json")
        stream.text = "boom"
        with patch("cli_agent_orchestrator.mcp_server.server.requests.get", return_value=stream):
            out = asyncio.run(workflow_events("run1"))
        assert out["ok"] is False
        stream.close.assert_called_once()

    def test_heartbeat_only_stream_is_bounded_by_wall_clock(self):
        """TB-1: a heartbeat-only stream terminates on the WALL-CLOCK bound.

        This is the unbounded case the tool's own "BOUNDED" docstring did not cover.
        SSE ``:keep-alive`` comment lines are skipped inside ``parse_sse_frames``, so
        they yield NO frame: they never increment ``len(events)`` toward
        ``max_events`` and never carry a terminal ``event:`` type — while still being
        traffic that resets the socket read timeout. So NEITHER pre-existing bound
        could ever be reached and the loop blocked forever.

        The deadline is monkeypatched to 0 so the bound trips immediately without a
        real sleep.

        MUTATION PROOF: remove the ``_deadline_bounded`` wrapper and this test fails
        on ``consumed`` (the drive runs to the end of the heartbeat supply) and on
        ``timed_out``.

        FAILS RATHER THAN HANGS (PR #525 review): the heartbeat supply is
        large-but-FINITE, not infinite. It was previously a ``while True`` generator,
        which meant a regression of the wall-clock bound HUNG this test — surfacing as
        a whole-job CI timeout with no attribution, so it read as flaky infrastructure
        instead of as this assertion catching the bug it exists to catch. A finite
        supply makes the regression terminate and fail a named assertion instead.

        Note an ``asyncio.wait_for`` wrapper does NOT work here and was rejected:
        ``workflow_events``' frame loop contains no ``await``, so it never yields to
        the event loop and a ``wait_for`` timeout can never fire (verified directly —
        a synchronous spin under ``wait_for(timeout=1.0)`` hangs indefinitely).
        Bounding the DATA is the only thing that bounds a synchronous consumer.
        """
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        resp.close.return_value = None

        # Enough lines that the bound is plainly what stops the drive, few enough that
        # exhausting them (the regression path) takes well under a second.
        supply = 50_000
        consumed = 0

        def _bounded_heartbeats():
            nonlocal consumed
            for _ in range(supply):
                consumed += 1
                yield ":keep-alive"

        resp.iter_lines.return_value = _bounded_heartbeats()
        with patch("cli_agent_orchestrator.mcp_server.server.WORKFLOW_EVENTS_MCP_MAX_SECONDS", 0.0):
            with patch("cli_agent_orchestrator.mcp_server.server.requests.get", return_value=resp):
                out = asyncio.run(workflow_events("run1"))
        # It RETURNED (the property under test) with an explicit timed_out marker.
        assert out["ok"] is True
        assert out["timed_out"] is True
        assert out["events"] == []
        assert out["state"] is None
        resp.close.assert_called_once()
        # The DEADLINE stopped the drive, not the end of the supply. Without this the
        # test would pass identically on a build with no wall-clock bound at all.
        assert consumed < supply, (
            f"the drive consumed all {supply} heartbeats, so it was ended by the supply "
            "running out rather than by the wall-clock bound"
        )

    def test_normal_terminal_stream_is_not_marked_timed_out(self):
        """TB-1 must not over-fire: a stream that reaches a terminal frame within the
        window reports ``timed_out: False``, so the caller can tell "the run ended"
        from "my window closed" and know whether to resume via ``after_seq``.
        """
        stream = _stream_resp(
            _event_frame(1, "step.completed", "s1", "completed"),
            _event_frame(2, "run.completed", None, "completed"),
        )
        with patch("cli_agent_orchestrator.mcp_server.server.requests.get", return_value=stream):
            out = asyncio.run(workflow_events("run1"))
        assert out["ok"] is True
        assert out["timed_out"] is False
        assert out["state"] == "completed"


class TestWorkflowEventsAbsentRoute:
    """CD-1: a 404 must distinguish an unknown RUN from an absent events ROUTE (the
    route ships with issue #504), and hand the agent an actionable alternative."""

    def test_absent_route_flagged_as_events_unavailable(self):
        """A readable run + a 404 from the events route means the ROUTE is missing.
        The envelope carries a machine-readable ``events_unavailable`` discriminator
        so an agent can branch without parsing prose.

        MUTATION PROOF: drop the ``_classify_events_404`` call and this goes RED.
        """
        stream = _stream_resp(status_code=404)
        stream.json.return_value = {"detail": "Not Found"}
        snapshot = MagicMock()
        snapshot.status_code = 200
        snapshot.json.return_value = {"run_id": "live-1", "state": "running"}

        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get",
            side_effect=[stream, snapshot],
        ):
            out = asyncio.run(workflow_events("live-1"))
        assert out["ok"] is False
        assert out["events_unavailable"] is True
        assert "no event stream" in out["error"]
        assert "workflow_status" in out["error"]
        # FD-1 still holds on this path: the streamed socket is closed.
        stream.close.assert_called_once()

    def test_unknown_run_is_not_flagged_unavailable(self):
        """A 404 on BOTH means the run is genuinely unknown — no capability claim,
        and no ``events_unavailable`` key for the agent to mis-branch on."""
        stream = _stream_resp(status_code=404)
        stream.json.return_value = {"detail": "unknown run 'ghost'"}
        snapshot = MagicMock()
        snapshot.status_code = 404
        snapshot.json.return_value = {"detail": "unknown run 'ghost'"}

        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get",
            side_effect=[stream, snapshot],
        ):
            out = asyncio.run(workflow_events("ghost"))
        assert out["ok"] is False
        assert "events_unavailable" not in out
        assert "unknown run" in out["error"]

    def test_probe_failure_keeps_original_detail(self):
        """Conservatism: a failed probe must not let the tool assert a capability it
        could not verify — the original detail survives unchanged."""
        stream = _stream_resp(status_code=404)
        stream.json.return_value = {"detail": "unknown run 'maybe'"}

        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get",
            side_effect=[stream, requests.ConnectionError("probe down")],
        ):
            out = asyncio.run(workflow_events("maybe"))
        assert out["ok"] is False
        assert "events_unavailable" not in out
        assert "unknown run" in out["error"]


def test_mcp_server_stays_http_only_boundary():
    """FR-7.4 / C-2: workflow_events reaches the run over HTTP only — it must not
    import #504's event read DAL. The dedicated AST guard
    (test_http_only_boundary) enforces clients.database/tmux repo-wide; here we
    assert the tool's own source references no engine/journal/DAL symbol."""
    import cli_agent_orchestrator.mcp_server.server as mod

    src = __import__("inspect").getsource(mod.workflow_events)
    for forbidden in ("workflow_journal", "workflow_service", "event_log", "clients.database"):
        assert forbidden not in src, f"MCP follower must not reference {forbidden}"
