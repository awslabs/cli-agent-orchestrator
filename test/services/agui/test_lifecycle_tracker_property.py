"""Hypothesis property tests for ToolCallLifecycleTracker.

P6: lifecycle well-formedness -- closers are a subset of opens, at most 1 END per id.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from cli_agent_orchestrator.services.agui.lifecycle_tracker import ToolCallLifecycleTracker
from cli_agent_orchestrator.services.agui_stream import (
    AGUI_TOOL_CALL_END,
    AGUI_TOOL_CALL_RESULT,
    AGUI_TOOL_CALL_START,
    to_agui_event,
)


def _handoff_record(receiver: str, orch_type: str, rid: str) -> dict:
    return {
        "id": rid,
        "kind": "handoff",
        "terminal_id": receiver,
        "session_name": "s",
        "timestamp": "2026-07-04T00:00:00+00:00",
        "detail": {
            "sender": "orchestrator",
            "receiver": receiver,
            "orchestration_type": orch_type,
        },
    }


def _a2a_record(receiver: str, rid: str) -> dict:
    return {
        "id": rid,
        "kind": "a2a_delegation",
        "terminal_id": "t-src",
        "session_name": "s",
        "timestamp": "2026-07-04T00:00:00+00:00",
        "detail": {
            "sender": "orchestrator",
            "receiver": receiver,
            "orchestration_type": "a2a_send",
        },
    }


def _completion_record(terminal_id: str, rid: str) -> dict:
    return {
        "id": rid,
        "kind": "completion",
        "terminal_id": terminal_id,
        "session_name": "s",
        "timestamp": "2026-07-04T00:01:00+00:00",
        "detail": {"event_type": "post_kill_terminal", "agent_name": "worker"},
    }


# Strategy: generate a sequence of open/close events with varying receivers
receiver_st = st.sampled_from([f"r{i}" for i in range(8)])
orch_type_st = st.sampled_from(["handoff", "assign", "send_message"])

event_st = st.one_of(
    st.tuples(st.just("open_handoff"), receiver_st, orch_type_st),
    st.tuples(st.just("open_a2a"), receiver_st, st.just("a2a_send")),
    st.tuples(st.just("close"), receiver_st, st.just("")),
)

sequence_st = st.lists(event_st, min_size=0, max_size=50)


@given(sequence=sequence_st)
@settings(max_examples=200)
def test_lifecycle_wellformedness(sequence) -> None:
    """For any input sequence, TOOL_CALL_END frames form a subset of opened IDs,
    with at most one END per tool_call_id."""
    tracker = ToolCallLifecycleTracker()
    opened_ids: set = set()
    ended_ids: list = []

    for idx, (action, receiver, orch_type) in enumerate(sequence):
        rid = f"evt-{idx}"
        if action == "open_handoff":
            record = _handoff_record(receiver, orch_type, rid)
        elif action == "open_a2a":
            record = _a2a_record(receiver, rid)
        else:  # close
            record = _completion_record(receiver, rid)

        mapped = to_agui_event(record)
        frames = tracker.feed(record, mapped)

        for ftype, fdata in frames:
            if ftype == AGUI_TOOL_CALL_START:
                opened_ids.add(fdata.get("tool_call_id"))
            elif ftype == AGUI_TOOL_CALL_END:
                ended_ids.append(fdata.get("tool_call_id"))

    # Also close remaining
    for ftype, fdata in tracker.close_all():
        if ftype == AGUI_TOOL_CALL_END:
            ended_ids.append(fdata.get("tool_call_id"))

    # Property 1: closers are a subset of opens
    assert set(ended_ids).issubset(opened_ids)

    # Property 2: at most one END per tool_call_id
    assert len(ended_ids) == len(set(ended_ids))


@given(sequence=sequence_st)
@settings(max_examples=200)
def test_result_only_for_a2a(sequence) -> None:
    """TOOL_CALL_RESULT is only synthesized for a2a_delegation opens, never for handoff opens."""
    tracker = ToolCallLifecycleTracker()
    # Track which tool_call_ids originated from a2a_delegation
    a2a_ids: set = set()
    result_ids: list = []

    for idx, (action, receiver, orch_type) in enumerate(sequence):
        rid = f"evt-{idx}"
        if action == "open_handoff":
            record = _handoff_record(receiver, orch_type, rid)
        elif action == "open_a2a":
            record = _a2a_record(receiver, rid)
        else:
            record = _completion_record(receiver, rid)

        mapped = to_agui_event(record)
        frames = tracker.feed(record, mapped)

        for ftype, fdata in frames:
            if ftype == AGUI_TOOL_CALL_START and record.get("kind") == "a2a_delegation":
                a2a_ids.add(fdata.get("tool_call_id"))
            elif ftype == AGUI_TOOL_CALL_RESULT:
                result_ids.append(fdata.get("tool_call_id"))

    for ftype, fdata in tracker.close_all():
        if ftype == AGUI_TOOL_CALL_RESULT:
            result_ids.append(fdata.get("tool_call_id"))

    # Every RESULT must belong to an a2a_delegation open
    assert set(result_ids).issubset(a2a_ids)
