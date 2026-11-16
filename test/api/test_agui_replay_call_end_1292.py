"""A reconnect must not lose a tool call's end (#1292).

Each ambient stream is a fresh ``AmbientRun``. A client that holds a tool call open,
loses its stream and reconnects with ``?since=`` or ``Last-Event-ID`` gets a replay
that starts after the call's ``assign``/``handoff`` record. When the receiver's
``completion`` falls in that gap, the replay must still close the call: the run
synthesizes the ``TOOL_CALL_START`` it never saw (same id, from the record in the
log) and then the ``TOOL_CALL_END``, so a stock verifier stays valid.

The same file covers the second half of #1292: the wire tool-call name is the plain
orchestration value (``assign``), not the enum's repr (``OrchestrationType.ASSIGN``).

Every conformant stream a test produces is checked by the Python pre-check and,
when ``CAO_AGUI_CAPTURE_DIR`` is set, written there so the pinned ``@ag-ui/client``
``verifyEvents`` (``verify-ambient.mjs``) gets the final word.
"""

from __future__ import annotations

import json
from test._support import agui_ambient_records as R
from test._support.agui_stream_harness import install
from test.api.test_agui_stream_conformance import (  # noqa: F401  (autouse fixture)
    _assign,
    _conformant,
    _get,
    _isolate_stop_and_incarnations,
    _known_terminals,
    _terminal_launch,
)
from typing import Any, Dict, List

import pytest

from cli_agent_orchestrator.models.inbox import OrchestrationType

S = R.SESSION
T_SENDER = "aaaa1111"
T_RECEIVER = "bbbb2222"

LAUNCH_SESSION = R.SESSION_LAUNCH  # evt-01, 10:00:00
LAUNCH_SENDER = R.TERMINAL_A_LAUNCH  # evt-02, 10:00:01
LAUNCH_RECEIVER = R.TERMINAL_B_LAUNCH  # evt-03, 10:00:02
ASSIGN = _assign("call-1", S, T_SENDER, T_RECEIVER, "2026-11-04T10:00:03.000000+00:00")
HANDOFF = dict(R.HANDOFF, id="call-1")  # a handoff to the same receiver
COMPLETION = R.TERMINAL_B_KILL  # evt-12, 10:00:11

AFTER_ASSIGN = "2026-11-04T10:00:05Z"
BEFORE_ASSIGN = "2026-11-04T10:00:00.5Z"
AFTER_ASSIGN_BEFORE_SECOND = "2026-11-04T10:00:03.5Z"


def _calls(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [e for e in events if e["type"] in ("TOOL_CALL_START", "TOOL_CALL_END")]


def _gap_history(call: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [LAUNCH_SESSION, LAUNCH_SENDER, LAUNCH_RECEIVER, call, COMPLETION]


@pytest.mark.parametrize("call", [ASSIGN, HANDOFF], ids=["assign", "handoff"])
def test_a_completion_in_the_gap_closes_the_call_on_a_since_reconnect(
    monkeypatch: pytest.MonkeyPatch, call: Dict[str, Any]
) -> None:
    _known_terminals(monkeypatch, T_SENDER, T_RECEIVER)
    install(monkeypatch, history=_gap_history(call))
    _, body = _get({"since": AFTER_ASSIGN})
    events = _conformant(f"1292-since-gap-{call['detail']['orchestration_type']}", body)

    start, end = _calls(events)
    kind = call["detail"]["orchestration_type"]
    assert (start["type"], start["toolCallId"], start["toolCallName"]) == (
        "TOOL_CALL_START",
        "call-1",
        kind,
    )
    assert start["metadata"]["cao"]["orchestrationType"] == kind
    assert start["metadata"]["cao"]["sender"] == T_SENDER
    assert start["metadata"]["cao"]["receiver"] == T_RECEIVER
    assert (end["type"], end["toolCallId"]) == ("TOOL_CALL_END", "call-1")
    assert end["metadata"]["cao"]["closedBy"] == "completion"
    # The call closes before the receiver's invocation does, and both frames carry
    # the owner the live path would have given them.
    types = [e["type"] for e in events]
    assert types.index("TOOL_CALL_END") < len(types) - 1 - types[::-1].index("SUBAGENT_FINISHED")
    assert start["subagentRunId"] == end["subagentRunId"] == f"terminal:{T_SENDER}"


def test_a_completion_in_the_gap_closes_the_call_on_a_last_event_id_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _known_terminals(monkeypatch, T_SENDER, T_RECEIVER)
    install(monkeypatch, history=_gap_history(ASSIGN))
    # The last frame the client read was the assign's own: the replay is the completion.
    _, body = _get(headers={"Last-Event-ID": "call-1"})
    events = _conformant("1292-last-event-id-gap", body)
    start, end = _calls(events)
    assert (start["toolCallId"], end["toolCallId"]) == ("call-1", "call-1")
    assert end["metadata"]["cao"]["closedBy"] == "completion"


def test_the_synthesized_frames_belong_to_the_completion_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The announced START and the END ride in the completion's sub-frame ids
    # (<record id>.<n>), so a Last-Event-ID from any of them replays the completion whole.
    _known_terminals(monkeypatch, T_SENDER, T_RECEIVER)
    install(monkeypatch, history=_gap_history(ASSIGN))
    _, body = _get({"since": AFTER_ASSIGN})
    ids_by_type: Dict[str, List[str]] = {}
    sse_id = None
    for line in body.splitlines():
        if line.startswith("id:"):
            sse_id = line[3:].strip()
        elif line.startswith("data:"):
            ids_by_type.setdefault(json.loads(line[5:])["type"], []).append(sse_id or "")
            sse_id = None
    done = COMPLETION["id"]
    for kind in ("TOOL_CALL_START", "TOOL_CALL_END"):
        (frame_id,) = ids_by_type[kind]
        assert frame_id.startswith(done + "."), (kind, frame_id)
    assert ids_by_type["SUBAGENT_FINISHED"][0] == done  # the record's own frame comes last


@pytest.mark.parametrize(
    "params", [{}, {"since": BEFORE_ASSIGN}], ids=["no-since", "since-before-the-assign"]
)
def test_control_a_replay_that_holds_the_assign_starts_and_ends_the_call_once(
    monkeypatch: pytest.MonkeyPatch, params: Dict[str, Any]
) -> None:
    _known_terminals(monkeypatch, T_SENDER, T_RECEIVER)
    install(monkeypatch, history=_gap_history(ASSIGN), live=[])
    _, body = _get(params)
    events = _conformant(f"1292-control-{'-'.join(params) or 'plain'}", body)
    if not params:  # no cursor: nothing is replayed, so nothing to close
        assert _calls(events) == []
        return
    start, end = _calls(events)
    assert [start["type"], end["type"]] == ["TOOL_CALL_START", "TOOL_CALL_END"]
    assert end["metadata"]["cao"]["closedBy"] == "completion"


def test_a_call_to_another_terminal_is_not_closed_by_this_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = "cccc3333"
    _known_terminals(monkeypatch, T_SENDER, T_RECEIVER, other)
    elsewhere = _assign("call-2", S, T_SENDER, other, "2026-11-04T10:00:04.000000+00:00")
    install(
        monkeypatch,
        history=[LAUNCH_SESSION, LAUNCH_SENDER, LAUNCH_RECEIVER, elsewhere, COMPLETION],
    )
    _, body = _get({"since": AFTER_ASSIGN})
    events = _conformant("1292-other-receiver", body)
    assert _calls(events) == []


@pytest.mark.parametrize("earlier", ["completion", "error"])
def test_a_call_an_earlier_record_already_closed_is_not_replayed_as_open(
    monkeypatch: pytest.MonkeyPatch, earlier: str
) -> None:
    # assign -> the terminal's completion or error (the call is closed) -> a later close
    # of the same terminal. The replay starts after the assign and holds only the later
    # one; the earlier record already closed the call, so it must not be resurrected.
    _known_terminals(monkeypatch, T_SENDER, T_RECEIVER)
    base = COMPLETION if earlier == "completion" else R.TERMINAL_ERROR
    early = dict(base, id="early-1", timestamp="2026-11-04T10:00:04.000000+00:00")
    late = dict(R.TERMINAL_ERROR, id="err-1", timestamp="2026-11-04T10:00:09.000000+00:00")
    install(
        monkeypatch,
        history=[LAUNCH_SESSION, LAUNCH_SENDER, LAUNCH_RECEIVER, ASSIGN, early, late],
    )
    _, body = _get({"since": "2026-11-04T10:00:08Z"})
    events = _conformant(f"1292-closed-before-the-window-{earlier}", body)
    assert _calls(events) == []


def test_a_failing_history_lookup_still_ends_the_call_the_replay_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _known_terminals(monkeypatch, T_SENDER, T_RECEIVER)
    install(monkeypatch, history=_gap_history(ASSIGN))
    seen = {"whole": 0}

    class Broken:
        def history(self, since=None, **_: Any):  # type: ignore[no-untyped-def]
            if since is None:
                seen["whole"] += 1
                raise RuntimeError("ring unavailable")
            return [ASSIGN, COMPLETION]  # the replay holds the call and its end

        def after_id(self, _id: str):  # type: ignore[no-untyped-def]
            return []

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.event_log_service.get_event_log", lambda: Broken()
    )
    _, body = _get({"since": BEFORE_ASSIGN})
    events = _conformant("1292-lookup-failure", body)
    assert seen["whole"] >= 1  # the lookup was tried, and failed
    ends = [e for e in events if e["type"] == "TOOL_CALL_END"]
    assert [e["toolCallId"] for e in ends] == ["call-1"]  # the real END survived
    assert ends[0]["metadata"]["cao"]["closedBy"] == "completion"
    assert events[-1]["type"] == "RUN_FINISHED"


def test_a_live_completion_on_a_run_that_never_saw_the_call_closes_it_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No cursor: nothing is replayed. The connection opens after the assign, then the
    # receiver completes on the live stream. The run never saw the call, so the live
    # close announces and ends it, as the replayed one does.
    _known_terminals(monkeypatch, T_SENDER, T_RECEIVER)
    install(
        monkeypatch,
        history=[LAUNCH_SESSION, LAUNCH_SENDER, LAUNCH_RECEIVER, ASSIGN],
        live=[COMPLETION],
    )
    _, body = _get()
    events = _conformant("1292-live-completion", body)
    start, end = _calls(events)
    assert (start["type"], start["toolCallId"]) == ("TOOL_CALL_START", "call-1")
    assert (end["type"], end["toolCallId"]) == ("TOOL_CALL_END", "call-1")
    assert end["metadata"]["cao"]["closedBy"] == "completion"


@pytest.mark.parametrize("closing", ["completion", "error"])
def test_the_senders_close_in_the_gap_ends_the_call_while_the_receiver_runs(
    monkeypatch: pytest.MonkeyPatch, closing: str
) -> None:
    _known_terminals(monkeypatch, T_SENDER, T_RECEIVER)
    base = COMPLETION if closing == "completion" else R.TERMINAL_ERROR
    sender_closes = dict(base, id="a-close", terminal_id=T_SENDER)
    install(
        monkeypatch,
        history=[LAUNCH_SESSION, LAUNCH_SENDER, LAUNCH_RECEIVER, ASSIGN, sender_closes],
    )
    _, body = _get({"since": AFTER_ASSIGN})
    events = _conformant(f"1292-sender-closes-{closing}", body)
    start, end = _calls(events)
    assert (start["toolCallId"], end["toolCallId"]) == ("call-1", "call-1")
    assert start["subagentRunId"] == end["subagentRunId"] == f"terminal:{T_SENDER}"


def test_the_receiver_closing_after_the_senders_close_ends_the_call_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _known_terminals(monkeypatch, T_SENDER, T_RECEIVER)
    sender_closes = dict(COMPLETION, id="a-close", terminal_id=T_SENDER)
    receiver_closes = dict(COMPLETION, id="b-close", timestamp="2026-11-04T10:00:12.000000+00:00")
    install(
        monkeypatch,
        history=[
            LAUNCH_SESSION,
            LAUNCH_SENDER,
            LAUNCH_RECEIVER,
            ASSIGN,
            sender_closes,
            receiver_closes,
        ],
    )
    _, body = _get({"since": AFTER_ASSIGN})
    events = _conformant("1292-sender-then-receiver", body)
    assert [e["type"] for e in _calls(events)] == ["TOOL_CALL_START", "TOOL_CALL_END"]


def test_a_second_call_to_the_worker_ends_the_first_one_the_replay_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _known_terminals(monkeypatch, T_SENDER, T_RECEIVER)
    second = _assign("call-2", S, T_SENDER, T_RECEIVER, "2026-11-04T10:00:04.000000+00:00")
    install(
        monkeypatch,
        history=[LAUNCH_SESSION, LAUNCH_SENDER, LAUNCH_RECEIVER, ASSIGN, second, COMPLETION],
    )
    _, body = _get({"since": AFTER_ASSIGN_BEFORE_SECOND})
    events = _conformant("1292-reassigned-worker", body)
    shape = [
        (e["type"], e["toolCallId"], e["metadata"]["cao"].get("closedBy")) for e in _calls(events)
    ]
    assert shape == [
        ("TOOL_CALL_START", "call-1", None),
        ("TOOL_CALL_END", "call-1", "superseded"),
        ("TOOL_CALL_START", "call-2", None),
        ("TOOL_CALL_END", "call-2", "completion"),
    ]


@pytest.mark.parametrize("closing", ["completion", "error"])
def test_an_error_or_completion_both_close_the_call_on_a_reconnect(
    monkeypatch: pytest.MonkeyPatch, closing: str
) -> None:
    _known_terminals(monkeypatch, T_SENDER, T_RECEIVER)
    base = COMPLETION if closing == "completion" else R.TERMINAL_ERROR
    install(
        monkeypatch,
        history=[
            LAUNCH_SESSION,
            LAUNCH_SENDER,
            LAUNCH_RECEIVER,
            ASSIGN,
            dict(base, id="b-close", timestamp="2026-11-04T10:00:11.000000+00:00"),
        ],
    )
    _, body = _get({"since": AFTER_ASSIGN})
    events = _conformant(f"1292-receiver-{closing}", body)
    assert [e["type"] for e in _calls(events)] == ["TOOL_CALL_START", "TOOL_CALL_END"]


def test_the_cao_legacy_framing_is_the_same_bytes_for_a_member_and_a_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def body_for(kind: Any) -> str:
        member = dict(ASSIGN, id="leg-1", detail=dict(ASSIGN["detail"], orchestration_type=kind))
        install(monkeypatch, history=[member])
        _, text = _get({"since": "2026-11-04T09:59:59Z", "framing": "cao-legacy"})
        return text

    as_member = body_for(OrchestrationType.ASSIGN)
    assert as_member == body_for("assign")
    assert '"assign"' in as_member and "OrchestrationType" not in as_member


# --- the name on the wire -----------------------------------------------------------------


@pytest.mark.parametrize("member", list(OrchestrationType)[1:], ids=lambda m: m.value)
def test_the_wire_name_is_the_plain_value_for_an_enum_member_and_a_string(
    monkeypatch: pytest.MonkeyPatch, member: OrchestrationType
) -> None:
    _known_terminals(monkeypatch, T_SENDER, T_RECEIVER)
    as_member = _assign("n-1", S, T_SENDER, T_RECEIVER, "2026-11-04T10:00:03.000000+00:00")
    as_member["detail"] = dict(as_member["detail"], orchestration_type=member)
    as_string = dict(as_member, id="n-2")
    as_string["detail"] = dict(as_member["detail"], orchestration_type=member.value)
    # Receivers differ so the second call does not supersede the first.
    as_string["detail"]["receiver"] = "dddd4444"
    _known_terminals(monkeypatch, T_SENDER, T_RECEIVER, "dddd4444")
    install(
        monkeypatch,
        history=[
            LAUNCH_SESSION,
            LAUNCH_SENDER,
            LAUNCH_RECEIVER,
            _terminal_launch("n-0", S, "dddd4444", "rev", "2026-11-04T10:00:02.500000+00:00"),
            as_member,
            as_string,
        ],
    )
    _, body = _get({"since": "2026-11-04T09:59:59Z"})
    events = _conformant(f"1292-name-{member.value}", body)
    starts = [e for e in events if e["type"] == "TOOL_CALL_START"]
    assert [s["toolCallName"] for s in starts] == [member.value, member.value]
    assert [s["metadata"]["cao"]["orchestrationType"] for s in starts] == [member.value] * 2
