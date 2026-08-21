"""Unit tests for the U10 SSE frame parser (issue #505).

``parse_sse_frames`` + ``SseFrame`` are the pure, consumer-only heart of the live
follower (both the CLI verb and the MCP tool build on them). These tests cover the
parser's leniency (malformed lines skipped, never crash), the render-not-infer
terminal semantics (a step's ``state`` is not the run's), and the resume-cursor
``seq`` accessor — without any HTTP.
"""

from __future__ import annotations

import json

from cli_agent_orchestrator.utils.workflow_events import (
    GAP_EVENT_TYPE,
    SseFrame,
    parse_sse_frames,
)


def _lines(*blocks: str):
    out: list[str] = []
    for block in blocks:
        out.extend(block.split("\n"))
    return out


def _ev(seq, event_type, step_id, state):
    data = {
        "seq": seq,
        "run_id": "r",
        "event_type": event_type,
        "step_id": step_id,
        "state": state,
        "ts": "t",
    }
    return f"event: {event_type}\ndata: {json.dumps(data)}\nid: {seq}\n"


# ---------------------------------------------------------------------------
# Frame parsing
# ---------------------------------------------------------------------------
def test_parses_normal_and_gap_frames_in_order():
    frames = list(
        parse_sse_frames(
            _lines(
                _ev(1, "step.completed", "s1", "completed"),
                "event: gap\ndata: "
                + json.dumps(
                    {"after_seq": 1, "before_seq": 3, "missing_count": 1, "reason": "append_failed"}
                )
                + "\n",
                _ev(3, "run.completed", None, "completed"),
            )
        )
    )
    assert [f.event for f in frames] == ["step.completed", GAP_EVENT_TYPE, "run.completed"]
    assert frames[0].seq() == 1
    assert frames[1].is_gap and frames[1].seq() is None  # gap frame has no id:
    assert frames[2].is_terminal and frames[2].terminal_state == "completed"


def test_comment_and_malformed_lines_are_skipped():
    """A heartbeat comment (``:``) and a colon-less line are ignored, never crash."""
    frames = list(
        parse_sse_frames(
            [":keep-alive", "garbage-no-colon", *_ev(5, "run.failed", None, "failed").split("\n")]
        )
    )
    assert len(frames) == 1
    assert frames[0].event == "run.failed"
    assert frames[0].terminal_state == "failed"


def test_none_lines_are_ignored():
    frames = list(parse_sse_frames([None, *_ev(1, "run.completed", None, "completed").split("\n")]))
    assert len(frames) == 1


def test_unparseable_data_frame_is_dropped():
    """A data: payload that is not JSON is dropped (never crashes the follow)."""
    frames = list(parse_sse_frames(["event: step.completed", "data: {not json", ""]))
    assert frames == []


def test_multiline_data_is_concatenated():
    # Split the JSON across two data: lines at a token boundary — SSE concatenates
    # them with \n, and json.loads tolerates a newline as inter-token whitespace.
    frames = list(
        parse_sse_frames(
            ["event: step.completed", 'data: {"seq": 1,', 'data:  "step_id": "s1"}', "id: 1", ""]
        )
    )
    assert len(frames) == 1
    assert frames[0].data["step_id"] == "s1"
    assert frames[0].seq() == 1


def test_stream_ending_without_blank_line_still_dispatches():
    frames = list(
        parse_sse_frames(
            ["event: run.completed", 'data: {"seq": 9, "state": "completed"}', "id: 9"]
        )
    )
    assert len(frames) == 1
    assert frames[0].seq() == 9


def test_non_dict_json_data_wrapped_under_raw():
    frames = list(parse_sse_frames(["event: x", "data: 42", ""]))
    assert frames[0].data == {"_raw": 42}


# ---------------------------------------------------------------------------
# SseFrame semantics (render-not-infer terminal + cursor)
# ---------------------------------------------------------------------------
def test_step_completed_is_not_run_terminal():
    """A step.completed frame carries state=completed (the STEP's), which must NOT
    read as a terminal RUN state."""
    frame = SseFrame(event="step.completed", data={"step_id": "s1", "state": "completed"}, id="2")
    assert frame.is_terminal is False
    assert frame.terminal_state is None


def test_run_projection_frame_terminal_by_state():
    """A run-projection frame (step_id None) with a terminal state settles the run."""
    frame = SseFrame(event="run.updated", data={"step_id": None, "state": "cancelled"}, id="7")
    assert frame.terminal_state == "cancelled"


def test_run_event_terminal_regardless_of_state_field():
    """A run.* terminal event is authoritative even if its state field were absent."""
    frame = SseFrame(event="run.failed", data={"step_id": None}, id="8")
    assert frame.terminal_state == "failed"


def test_gap_frame_never_terminal():
    frame = SseFrame(event="gap", data={"after_seq": 1, "before_seq": 3}, id=None)
    assert frame.is_gap is True
    assert frame.is_terminal is False
    assert frame.terminal_state is None


def test_seq_non_integer_id_is_none():
    assert SseFrame(event="x", data={}, id="not-an-int").seq() is None
    assert SseFrame(event="x", data={}, id=None).seq() is None
