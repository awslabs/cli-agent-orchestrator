"""Unit tests for the #1292 reconnect fix, driving ``AmbientRun`` and the record mapper directly.

The route-level tests are in ``test/api/test_agui_replay_call_end_1292.py``; these reach the
branches a route cannot easily set up: no history, a broken history, a record that cannot be
mapped, a call the run already holds, the sender's side of a call, and a reassigned worker.

A fed record is also in the history list, as it is in the real event log, and the lookup
reads the history up to (not including) the record being fed.
"""

from __future__ import annotations

import json
from test._support import agui_ambient_records as R
from typing import Any, Dict, Iterable, List, Optional

import pytest

from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.services.agui import ambient_run
from cli_agent_orchestrator.services.agui.base import BoundedSeen
from cli_agent_orchestrator.services.agui_stream import to_agui_event

A, B, C = "aaaa1111", "bbbb2222", "cccc3333"


def _call(
    rid: str, receiver: Optional[str], sender: str = A, kind: str = "assign"
) -> Dict[str, Any]:
    detail: Dict[str, Any] = {"sender": sender, "orchestration_type": kind}
    if receiver is not None:
        detail["receiver"] = receiver
    return dict(R.HANDOFF, id=rid, terminal_id=sender, detail=detail)


def _done(rid: str, terminal: str, kind: str = "completion") -> Dict[str, Any]:
    base = R.TERMINAL_B_KILL if kind == "completion" else R.TERMINAL_ERROR
    return dict(base, id=rid, terminal_id=terminal)


CALL = _call("call-1", B)
CALL2 = _call("call-2", B)
KILL_B = R.TERMINAL_B_KILL  # evt-12, terminal bbbb2222
SENDER_DONE = _done("a-done", A)


def _run(history: Optional[Iterable[Dict[str, Any]]], **kwargs: Any) -> ambient_run.AmbientRun:
    return ambient_run.AmbientRun(
        "t",
        "r",
        incarnation_of=lambda name: None,
        call_history=None if history is None else (lambda: list(history)),
        **kwargs,
    )


def _events(run: ambient_run.AmbientRun, record: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [e.model_dump(by_alias=True) for _, e in run.feed(record)]


def _types(run: ambient_run.AmbientRun, record: Dict[str, Any]) -> List[str]:
    return [e["type"] for e in _events(run, record)]


def _calls(events: List[Dict[str, Any]]) -> List[tuple]:
    out = []
    for e in events:
        if e["type"] == "TOOL_CALL_START":
            out.append(("START", e["toolCallId"]))
        elif e["type"] == "TOOL_CALL_END":
            out.append(("END", e["toolCallId"], e["metadata"]["cao"]["closedBy"]))
    return out


# --- no history, a broken history, an unmappable record ----------------------------------


def test_without_a_history_the_completion_closes_only_what_the_run_knows(
    caplog: pytest.LogCaptureFixture,
) -> None:
    run = _run(None)
    assert "TOOL_CALL_START" not in _types(run, KILL_B)
    assert "call history lookup failed" not in caplog.text  # no lookup was even attempted


def test_a_broken_history_still_ends_the_call_the_run_holds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken() -> Iterable[Dict[str, Any]]:
        raise RuntimeError("ring unavailable")

    run = ambient_run.AmbientRun("t", "r", incarnation_of=lambda name: None, call_history=broken)
    run.feed(CALL)  # live: the run opens call-1 itself
    events = _events(run, KILL_B)
    assert _calls(events) == [("END", "call-1", "completion")]
    assert events[-1]["type"] == "SUBAGENT_FINISHED"
    assert "call history lookup failed" in caplog.text


def test_a_record_that_cannot_be_mapped_costs_neither_the_held_call_nor_the_close(
    caplog: pytest.LogCaptureFixture,
) -> None:
    held = _call("call-held", C)
    history = [CALL, held, KILL_B]
    run = _run(history)
    run.feed(held)  # the run holds call-held (to C); call-1 (to B) is only in the log
    original = run._map

    def boom(record: Dict[str, Any]) -> List[Any]:
        if record is CALL:
            raise RuntimeError("cannot map")
        return original(record)

    run._map = boom  # type: ignore[method-assign]
    events = _events(run, KILL_B)  # B closes: call-1 cannot be announced
    assert _calls(events) == []
    assert events[-1]["type"] == "SUBAGENT_FINISHED"
    assert "could not map a call record" in caplog.text
    run._map = original  # type: ignore[method-assign]
    assert _calls(_events(run, _done("c-done", C))) == [("END", "call-held", "completion")]


# --- the receiver's close ------------------------------------------------------------------


def test_a_completion_announces_then_ends_the_call_it_never_saw() -> None:
    run = _run([CALL, KILL_B])
    events = _events(run, KILL_B)
    assert _calls(events) == [("START", "call-1"), ("END", "call-1", "completion")]
    assert events[-1]["type"] == "SUBAGENT_FINISHED"


def test_a_call_the_run_already_opened_and_ended_is_not_announced_again() -> None:
    run = _run([CALL, SENDER_DONE, KILL_B], terminal_known=lambda tid: tid in (A, B))
    run.feed(CALL)
    assert _calls(_events(run, SENDER_DONE)) == [("END", "call-1", "completion")]
    assert _calls(_events(run, KILL_B)) == []


def test_a_call_still_open_on_the_run_is_not_announced_again() -> None:
    run = _run([CALL, KILL_B])
    run.feed(CALL)
    assert _calls(_events(run, KILL_B)) == [("END", "call-1", "completion")]


def test_a_call_the_seen_set_forgot_is_not_announced_while_it_is_still_open() -> None:
    # The seen set is bounded and evicts; a call that is still open on the run must not be
    # ended as superseded, started again and ended again.
    run = _run([CALL, KILL_B])
    run.feed(CALL)
    run._seen_calls = BoundedSeen()
    assert _calls(_events(run, KILL_B)) == [("END", "call-1", "completion")]


def test_the_lookup_stops_at_the_record_being_replayed() -> None:
    # A call made AFTER the completion is not what the completion closed.
    after = _call("call-late", B)
    run = _run([CALL, KILL_B, after])
    assert _calls(_events(run, KILL_B)) == [("START", "call-1"), ("END", "call-1", "completion")]


def test_a_record_without_an_id_scans_the_whole_history() -> None:
    run = _run([CALL])
    done = {k: v for k, v in KILL_B.items() if k != "id"}
    assert _calls(_events(run, done)) == [("START", "call-1"), ("END", "call-1", "completion")]


def test_the_newest_call_to_a_receiver_is_the_one_announced() -> None:
    # call-1 was superseded by call-2 in the log; only call-2 is open when B closes.
    run = _run([CALL, CALL2, KILL_B])
    assert _calls(_events(run, KILL_B)) == [("START", "call-2"), ("END", "call-2", "completion")]


def test_an_a2a_delegation_is_found_too() -> None:
    a2a = dict(R.A2A, id="a2a-1")
    run = _run([a2a, KILL_B])
    (start,) = [e for e in _events(run, KILL_B) if e["type"] == "TOOL_CALL_START"]
    assert (start["toolCallId"], start["toolCallName"]) == ("a2a-1", "a2a_delegation")


def test_a_message_that_is_not_a_tool_call_is_not_announced() -> None:
    run = _run([R.SEND_MESSAGE, KILL_B])
    assert "TOOL_CALL_START" not in _types(run, KILL_B)


def test_a_later_message_to_the_receiver_does_not_hide_the_call() -> None:
    message = dict(R.SEND_MESSAGE, id="msg-1")
    run = _run([CALL, message, KILL_B])
    assert _calls(_events(run, KILL_B))[0] == ("START", "call-1")


def test_another_terminals_completion_does_not_close_the_receivers_call() -> None:
    run = _run([CALL, _done("c-done", C), KILL_B])
    assert _calls(_events(run, KILL_B)) == [("START", "call-1"), ("END", "call-1", "completion")]


def test_an_error_of_the_receiver_closes_the_call_too() -> None:
    err = _done("b-err", B, "error")
    run = _run([CALL, err])
    assert _calls(_events(run, err)) == [("START", "call-1"), ("END", "call-1", "completion")]


# --- the sender's close (the live run ends a call at its owner's close too) -----------------


def test_the_senders_completion_closes_a_call_whose_receiver_is_still_running() -> None:
    # Trace 1: assign A->B before the cursor, A's completion in the gap, B still running.
    run = _run([CALL, SENDER_DONE])
    events = _events(run, SENDER_DONE)
    assert _calls(events) == [("START", "call-1"), ("END", "call-1", "completion")]
    (end,) = [e for e in events if e["type"] == "TOOL_CALL_END"]
    assert end["subagentRunId"] == f"terminal:{A}"  # the owner is the sender
    assert events[-1]["type"] == "SUBAGENT_FINISHED"


def test_the_senders_error_closes_the_call_too() -> None:
    err = _done("a-err", A, "error")
    run = _run([CALL, err])
    assert _calls(_events(run, err)) == [("START", "call-1"), ("END", "call-1", "completion")]


def test_the_receiver_closing_after_the_senders_close_does_not_re_announce_the_call() -> None:
    # Trace 2: A closes in the gap (ending the call), then B closes in the gap.
    run = _run([CALL, SENDER_DONE, KILL_B])
    assert _calls(_events(run, SENDER_DONE)) == [
        ("START", "call-1"),
        ("END", "call-1", "completion"),
    ]
    assert _calls(_events(run, KILL_B)) == []


def test_a_replay_that_starts_after_the_senders_close_announces_nothing_at_the_receivers() -> None:
    # The sender's completion is before the cursor: the client saw the END live.
    run = _run([CALL, SENDER_DONE, KILL_B])
    assert _calls(_events(run, KILL_B)) == []


def test_the_sender_of_a_call_without_a_receiver_closes_it() -> None:
    nameless = _call("call-r", None)
    run = _run([nameless, SENDER_DONE])
    assert _calls(_events(run, SENDER_DONE)) == [
        ("START", "call-r"),
        ("END", "call-r", "completion"),
    ]


def test_a_senders_close_leaves_the_calls_of_other_senders_alone() -> None:
    other = _call("call-c", B, sender=C)
    run = _run([other, SENDER_DONE])
    assert _calls(_events(run, SENDER_DONE)) == []


def test_the_senders_calls_are_announced_oldest_first_and_only_the_open_ones() -> None:
    # A called B (call-1), C (call-2) and B again (call-3, which superseded call-1).
    c1, c2, c3 = _call("call-1", B), _call("call-2", C), _call("call-3", B)
    run = _run([c1, c2, c3, SENDER_DONE])
    assert _calls(_events(run, SENDER_DONE)) == [
        ("START", "call-2"),
        ("START", "call-3"),
        ("END", "call-2", "completion"),
        ("END", "call-3", "completion"),
    ]


def test_two_calls_without_a_receiver_are_both_recovered() -> None:
    r1, r2 = _call("call-r1", None), _call("call-r2", None)
    run = _run([r1, r2, SENDER_DONE])
    assert [c[:2] for c in _calls(_events(run, SENDER_DONE)) if c[0] == "START"] == [
        ("START", "call-r1"),
        ("START", "call-r2"),
    ]


def test_a_call_record_without_a_sender_in_its_detail_is_the_senders_by_its_terminal() -> None:
    bare = dict(CALL, detail={"receiver": B, "orchestration_type": "assign"})  # terminal_id is A
    run = _run([bare, SENDER_DONE])
    assert _calls(_events(run, SENDER_DONE)) == [
        ("START", "call-1"),
        ("END", "call-1", "completion"),
    ]


# --- a reassigned worker ---------------------------------------------------------------------


def test_a_second_call_to_the_worker_ends_the_first_the_run_never_saw() -> None:
    # F2: call-1 before the cursor, call-2 and the worker's completion in the gap.
    run = _run([CALL, CALL2, KILL_B])
    assert _calls(_events(run, CALL2)) == [
        ("START", "call-1"),
        ("END", "call-1", "superseded"),
        ("START", "call-2"),
    ]
    assert _calls(_events(run, KILL_B)) == [("END", "call-2", "completion")]


def test_a_second_call_the_run_saw_the_first_of_does_not_announce_it_again() -> None:
    run = _run([CALL, CALL2])
    assert _calls(_events(run, CALL)) == [("START", "call-1")]
    assert _calls(_events(run, CALL2)) == [("END", "call-1", "superseded"), ("START", "call-2")]


def test_only_the_call_open_just_before_the_second_one_is_recovered() -> None:
    # call-0 was superseded by call-1 in the log: the client has seen that END already.
    first = _call("call-0", B)
    run = _run([first, CALL, CALL2])
    assert _calls(_events(run, CALL2)) == [
        ("START", "call-1"),
        ("END", "call-1", "superseded"),
        ("START", "call-2"),
    ]


def test_a_call_to_a_worker_with_nothing_open_announces_nothing_else() -> None:
    run = _run([CALL])
    assert _calls(_events(run, CALL)) == [("START", "call-1")]


def test_a_call_the_run_opened_and_ended_itself_is_not_announced_again_from_a_log_that_lacks_the_end() -> (
    None
):
    # The run opened call-1 and ended it (superseded by call-2); the log it reads back
    # does not hold call-2, so it still shows call-1 as open. The run knows better.
    run = _run([CALL, KILL_B])
    run.feed(CALL)
    run.feed(CALL2)
    assert _calls(_events(run, KILL_B)) == [("END", "call-2", "completion")]


def test_the_log_is_not_walked_for_a_call_to_a_worker_that_already_holds_one() -> None:
    walks: List[int] = []

    def history() -> List[Dict[str, Any]]:
        walks.append(1)
        return [CALL, CALL2]

    run = ambient_run.AmbientRun("t", "r", incarnation_of=lambda name: None, call_history=history)
    run.feed(CALL)  # nothing held on B yet: one walk
    assert len(walks) == 1
    run.feed(CALL2)  # B holds call-1: no walk, call-1 is ended as superseded
    assert len(walks) == 1


def test_a_recovery_does_not_switch_off_the_next_calls_lookup() -> None:
    # B's close recovers call-x; afterwards a second call to C must still recover call-y.
    call_x, call_y, call_z = _call("call-x", B), _call("call-y", C), _call("call-z", C)
    run = _run([call_x, call_y, KILL_B, call_z])
    assert _calls(_events(run, KILL_B)) == [("START", "call-x"), ("END", "call-x", "completion")]
    assert _calls(_events(run, call_z)) == [
        ("START", "call-y"),
        ("END", "call-y", "superseded"),
        ("START", "call-z"),
    ]


# --- the wire name -----------------------------------------------------------------------


@pytest.mark.parametrize("member", [OrchestrationType.ASSIGN, OrchestrationType.HANDOFF])
def test_the_wire_name_of_an_enum_member_is_its_value(member: OrchestrationType) -> None:
    record = dict(
        R.HANDOFF,
        id="n-1",
        detail={"sender": A, "receiver": B, "orchestration_type": member},
    )
    run = _run(None)
    (start,) = [e for e in _events(run, record) if e["type"] == "TOOL_CALL_START"]
    assert start["toolCallName"] == member.value
    assert start["metadata"]["cao"]["orchestrationType"] == member.value
    assert type(start["toolCallName"]) is str


@pytest.mark.parametrize("member", [OrchestrationType.ASSIGN, OrchestrationType.HANDOFF])
def test_the_mapper_hands_on_the_plain_value_not_the_member(member: OrchestrationType) -> None:
    record = dict(R.HANDOFF, detail={"sender": A, "receiver": B, "orchestration_type": member})
    plain = dict(record, detail=dict(record["detail"], orchestration_type=member.value))
    _, data = to_agui_event(record)
    assert data["tool_call_name"] == member.value
    assert type(data["tool_call_name"]) is str
    assert str(data["tool_call_name"]) == member.value
    # What the cao-legacy framing serializes is byte-for-byte what a plain string gives.
    assert json.dumps(data, sort_keys=True) == json.dumps(to_agui_event(plain)[1], sort_keys=True)
