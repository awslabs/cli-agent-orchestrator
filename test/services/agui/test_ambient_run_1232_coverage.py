"""The ambient-stream and run-plane paths #1232's patch left untested.

Each test names the ``services/agui`` line(s) it reaches, numbered on ``main``
at the time of writing. No test starts a cao-server or touches tmux: ``AmbientRun`` is driven directly, and
``ambient_stream`` runs against an in-memory bus whose waits are bounded by the
test's own ``max_seconds`` and keep-alive settings.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from test._support import agui_ambient_records as R
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional

import pytest

from cli_agent_orchestrator.services.agui import ambient_run
from cli_agent_orchestrator.services.agui import run_plane as rp
from cli_agent_orchestrator.services.agui.attribution import (
    subagent_run_id_for_session,
    subagent_run_id_for_terminal,
)

S = R.SESSION


@pytest.fixture(autouse=True)
def _clear_stop() -> Iterator[None]:
    ambient_run.reset_stop()
    yield
    ambient_run.reset_stop()


def _run(**kwargs: Any) -> ambient_run.AmbientRun:
    kwargs.setdefault("incarnation_of", lambda name: None)
    return ambient_run.AmbientRun("t", "r", **kwargs)


def _dump(items: List[Any]) -> List[Dict[str, Any]]:
    return [e.model_dump(by_alias=True, exclude_none=True) for _, e in items]


def _feed(run: ambient_run.AmbientRun, *records: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for record in records:
        out.extend(_dump(run.feed(record)))
    return out


def _started(events: List[Dict[str, Any]]) -> List[str]:
    return [e["subagentRunId"] for e in events if e["type"] == "SUBAGENT_STARTED"]


# --- _epoch_ms (ambient_run.py:153-167) ------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),  # :153-154
        (datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc), 1793786400000),  # :156-157
        ("2026-11-04T10:00:00Z", 1793786400000),  # :160-161
        ("2026-11-04T10:00:00", 1793786400000),  # naive is UTC, :163-164
        ("not-a-time", None),  # :166-167
    ],
    ids=["none", "datetime", "z-suffix", "naive", "unparseable"],
)
def test_epoch_ms_reads_every_timestamp_form(value: Any, expected: Optional[int]) -> None:
    assert ambient_run._epoch_ms(value) == expected


# --- AmbientRun ------------------------------------------------------------------


def test_delta_needs_ops_and_a_baseline() -> None:
    """ambient_run.py:246-248: no ops, or no baseline yet, sends nothing."""
    run = _run(state_baseline_known=False)
    op = {"op": "add", "path": "/counts/sessions", "value": 1}
    assert run.delta([]) == []
    assert run.delta([op]) == []  # no STATE_SNAPSHOT yet, and the client sent state
    run.snapshot({"sessions": [], "terminals": []})
    (delta,) = _dump(run.delta([op]))
    assert delta == {"type": "STATE_DELTA", "delta": [op]}


def test_a_closed_run_maps_nothing() -> None:
    """ambient_run.py:275-276."""
    run = _run()
    run.start()
    run.close_cancelled()
    assert run.feed(R.SESSION_LAUNCH) == []


def test_a_record_with_no_id_and_no_incarnation_gets_a_per_run_session_id() -> None:
    """ambient_run.py:439-443: the counter keeps a relaunch's id distinct."""
    launch = {k: v for k, v in R.SESSION_LAUNCH.items() if k != "id"}
    kill = {k: v for k, v in R.SESSION_KILL.items() if k != "id"}
    events = _feed(_run(), launch, kill, launch)
    assert _started(events) == [
        subagent_run_id_for_session(f"r:{S}:1"),
        subagent_run_id_for_session(f"r:{S}:2"),
    ]


def test_an_event_with_no_terminal_belongs_to_its_session() -> None:
    """ambient_run.py:504, the no-terminal side of ``_owner_for``."""
    run = _run(incarnation_of=lambda name: "inc-1")
    file_mod = dict(R.FILE_MOD, id="fm-1", terminal_id=None)
    events = _feed(run, R.SESSION_LAUNCH, file_mod)
    (delta,) = [e for e in events if e["type"] == "STATE_DELTA"]
    assert delta["subagentRunId"] == subagent_run_id_for_session("inc-1")


def test_an_error_for_an_unseen_terminal_announces_it_once() -> None:
    """ambient_run.py:567-571: announce, then error; a finished one is never reopened."""
    run = _run()
    first = _feed(run, R.SESSION_LAUNCH, R.TERMINAL_ERROR)
    tid = subagent_run_id_for_terminal("bbbb2222")
    assert [e["type"] for e in first if e.get("subagentRunId") == tid] == [
        "SUBAGENT_STARTED",
        "SUBAGENT_ERROR",
    ]
    assert run.feed(dict(R.TERMINAL_ERROR, id="evt-10b")) == []


def test_a_kill_for_an_unseen_session_announces_it_then_closes_it() -> None:
    """ambient_run.py:595-596."""
    events = _feed(_run(incarnation_of=lambda name: "inc-1"), R.SESSION_KILL)
    sid = subagent_run_id_for_session("inc-1")
    assert [(e["type"], e["subagentRunId"]) for e in events] == [
        ("SUBAGENT_STARTED", sid),
        ("SUBAGENT_FINISHED", sid),
    ]


def test_killing_one_session_leaves_another_sessions_terminal_open() -> None:
    """ambient_run.py:598, the other-session side of the close loop."""
    other = "cao-other"
    run = _run(incarnation_of=lambda name: f"inc-{name}")
    events = _feed(
        run,
        R.SESSION_LAUNCH,
        R.TERMINAL_A_LAUNCH,
        dict(R.SESSION_LAUNCH, id="o-1", session_name=other),
        dict(R.TERMINAL_B_LAUNCH, id="o-2", session_name=other),
        R.SESSION_KILL,
    )
    finished = {e["subagentRunId"] for e in events if e["type"] == "SUBAGENT_FINISHED"}
    assert subagent_run_id_for_terminal("aaaa1111") in finished
    assert subagent_run_id_for_terminal("bbbb2222") not in finished
    assert subagent_run_id_for_session(f"inc-{other}") not in finished


def test_the_snapshot_announces_live_sessions_and_terminals_once() -> None:
    """ambient_run.py:636-644."""
    run = _run(incarnation_of=lambda name: f"inc-{name}")
    _feed(run, R.SESSION_LAUNCH, R.TERMINAL_A_LAUNCH)  # already open: not announced again
    snapshot = {
        "sessions": [{"name": S}, {"name": "cao-live"}, {"id": "cao-by-id"}, {}],
        "terminals": [
            {"id": "aaaa1111", "session_name": S},
            {"id": "cccc3333", "session_name": "cao-live", "agent_profile": "rev"},
            {"id": "", "session_name": "cao-live"},
        ],
    }
    events = _dump(run.snapshot(snapshot))
    assert events[0]["type"] == "STATE_SNAPSHOT"
    started = [e for e in events if e["type"] == "SUBAGENT_STARTED"]
    assert [e["subagentRunId"] for e in started] == [
        subagent_run_id_for_session("inc-cao-live"),
        subagent_run_id_for_session("inc-cao-by-id"),
        subagent_run_id_for_terminal("cccc3333"),
    ]
    assert started[-1]["name"] == "rev"
    assert started[-1]["parentSubagentRunId"] == subagent_run_id_for_session("inc-cao-live")


def test_a_call_with_no_receiver_opens_once_and_closes_with_the_run() -> None:
    """ambient_run.py:670-671 (same id again), 673 (no receiver), 688 (no receiver to clear)."""
    run = _run()
    assign = dict(
        R.HANDOFF,
        id="call-1",
        detail={"sender": "aaaa1111", "orchestration_type": "assign"},
    )
    events = _feed(run, R.SESSION_LAUNCH, R.TERMINAL_A_LAUNCH, assign, assign)
    assert [e["toolCallId"] for e in events if e["type"] == "TOOL_CALL_START"] == ["call-1"]
    closed = _dump(run.close_cancelled())
    (end,) = [e for e in closed if e["type"] == "TOOL_CALL_END"]
    assert end["toolCallId"] == "call-1" and end["metadata"]["cao"]["closedBy"] == "stream_closed"


# --- ambient_stream (ambient_run.py:779-865) -----------------------------------


class _Sub:
    overflowed = False


class _ListBus:
    """Yields its records, then ends (the stream closes with ``source_ended``)."""

    def __init__(self, records: List[Dict[str, Any]]) -> None:
        self._records = records

    async def drain(self, sub: Any) -> AsyncIterator[Dict[str, Any]]:
        for record in self._records:
            yield record


class _QuietBus:
    """Never yields: every wait times out, so only keep-alives and the deadline end it."""

    async def drain(self, sub: Any) -> AsyncIterator[Dict[str, Any]]:
        await asyncio.Event().wait()
        return
        yield {}


def _encode(sse_id: Optional[str], event: Any) -> Dict[str, Any]:
    return event.model_dump(by_alias=True, exclude_none=True)


async def _collect(stream: AsyncIterator[Any]) -> List[Any]:
    return [frame async for frame in stream]


def _stream(**overrides: Any) -> AsyncIterator[Any]:
    kwargs: Dict[str, Any] = dict(
        run=_run(),
        encode=_encode,
        is_sse=True,
        replay=[],
        fleet_snapshot=lambda: {"sessions": [], "terminals": []},
        diff=lambda prev, curr: None,
        bus=_ListBus([]),
        sub=_Sub(),
        keepalive_seconds=5.0,
        max_seconds=5,
    )
    kwargs.update(overrides)
    return ambient_run.ambient_stream(**kwargs)


@pytest.mark.asyncio
async def test_the_stream_survives_snapshot_and_diff_failures(caplog) -> None:
    """ambient_run.py:779, 788-790, 832-843.

    A replay record with no id; the first snapshot fails, so the first live record
    sends the snapshot instead of a delta; then a delta; then a failing diff.
    """
    snapshots = iter([RuntimeError("no fleet"), {"n": 1}, {"n": 2}, {"n": 3}])

    def fleet_snapshot() -> Dict[str, Any]:
        value = next(snapshots)
        if isinstance(value, Exception):
            raise value
        return value

    diffs = iter([[{"op": "replace", "path": "/n", "value": 2}], RuntimeError("no diff")])

    def diff(prev: Dict[str, Any], curr: Dict[str, Any]) -> Any:
        value = next(diffs)
        if isinstance(value, Exception):
            raise value
        return value

    replay_record = {k: v for k, v in R.OTHER.items() if k != "id"}
    live = [dict(R.OTHER, id=f"live-{n}") for n in range(3)]
    caplog.set_level("WARNING", logger=ambient_run.logger.name)
    frames = await _collect(
        _stream(
            replay=[replay_record], fleet_snapshot=fleet_snapshot, diff=diff, bus=_ListBus(live)
        )
    )
    types = [f["type"] for f in frames]
    assert types == [
        "RUN_STARTED",
        "RAW",  # the id-less replay record
        "RAW",
        "STATE_SNAPSHOT",  # first live record: no prior snapshot, so a snapshot
        "RAW",
        "STATE_DELTA",
        "RAW",  # the diff failed: no frame, and the stream goes on
        "RUN_FINISHED",
    ]
    assert frames[3]["snapshot"] == {"n": 1}
    assert frames[-1]["metadata"]["cao"]["endReason"] == "source_ended"
    messages = [r.getMessage() for r in caplog.records]
    assert "ambient_run: initial STATE_SNAPSHOT failed" in messages
    assert "ambient_run: STATE_DELTA computation failed" in messages


@pytest.mark.asyncio
async def test_an_idle_stream_keeps_alive_until_its_deadline() -> None:
    """ambient_run.py:802, 845, 848-849."""
    frames = await asyncio.wait_for(
        _collect(_stream(bus=_QuietBus(), keepalive_seconds=0.02, max_seconds=0.3)), timeout=10
    )
    assert frames[0]["type"] == "RUN_STARTED"
    keepalives = [f for f in frames if f == ":keep-alive\n\n"]
    assert len(keepalives) >= 2
    assert frames[-1]["type"] == "RUN_FINISHED"
    assert frames[-1]["metadata"]["cao"]["endReason"] == "max_seconds"


@pytest.mark.asyncio
async def test_an_idle_non_sse_stream_sends_no_keep_alive() -> None:
    """ambient_run.py:848, the non-SSE side."""
    frames = await asyncio.wait_for(
        _collect(_stream(bus=_QuietBus(), is_sse=False, keepalive_seconds=0.02, max_seconds=0.2)),
        timeout=10,
    )
    assert ":keep-alive\n\n" not in frames
    assert frames[-1]["metadata"]["cao"]["endReason"] == "max_seconds"


@pytest.mark.asyncio
async def test_a_client_disconnect_propagates_and_sends_no_closer() -> None:
    """ambient_run.py:859-860: a cancelled stream re-raises and writes no RUN_FINISHED."""
    got: List[Any] = []

    async def consume() -> None:
        async for frame in _stream(bus=_QuietBus(), keepalive_seconds=5.0, max_seconds=30):
            got.append(frame)

    task = asyncio.ensure_future(consume())
    for _ in range(200):  # wait until the stream is parked in its live wait
        if any(isinstance(f, dict) and f["type"] == "STATE_SNAPSHOT" for f in got):
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)
    assert [f["type"] for f in got] == ["RUN_STARTED", "STATE_SNAPSHOT"]


@pytest.mark.asyncio
async def test_an_unexpected_error_ends_the_run_with_run_error(caplog) -> None:
    """ambient_run.py:861-865."""

    def replay() -> Iterator[Dict[str, Any]]:
        yield R.SESSION_LAUNCH
        raise RuntimeError("replay broke")

    caplog.set_level("WARNING", logger=ambient_run.logger.name)
    frames = await _collect(_stream(replay=replay()))
    assert [f["type"] for f in frames] == ["RUN_STARTED", "SUBAGENT_STARTED", "RUN_ERROR"]
    assert frames[-1]["code"] == ambient_run.RUN_ERROR_INTERNAL
    assert "ambient_run: stream failed" in [r.getMessage() for r in caplog.records]


@pytest.mark.asyncio
async def test_an_error_after_the_run_closed_adds_no_second_closer(caplog) -> None:
    """ambient_run.py:863, the closed side: RUN_FINISHED was already built, so no RUN_ERROR."""

    def encode(sse_id: Optional[str], event: Any) -> Dict[str, Any]:
        frame = _encode(sse_id, event)
        if frame["type"] == "RUN_FINISHED":
            raise RuntimeError("the client went away mid-write")
        return frame

    caplog.set_level("WARNING", logger=ambient_run.logger.name)
    frames = await _collect(_stream(encode=encode))
    assert [f["type"] for f in frames] == ["RUN_STARTED", "STATE_SNAPSHOT"]
    assert "ambient_run: stream failed" in [r.getMessage() for r in caplog.records]


# --- run_plane_stream (run_plane.py:496) ---------------------------------------


@pytest.mark.asyncio
async def test_a_closer_that_cannot_be_translated_is_skipped(monkeypatch) -> None:
    """run_plane.py:496: ``_translate_live_frame`` returns None for an unmappable frame."""
    real = rp._translate_live_frame
    skipped: List[str] = []

    def translate(agui_type: str, data: Dict[str, Any], *args: Any) -> Optional[str]:
        if agui_type == "TOOL_CALL_END":
            skipped.append(agui_type)
            return None
        return real(agui_type, data, *args)

    monkeypatch.setattr(rp, "_translate_live_frame", translate)

    async def bus() -> AsyncIterator[Dict[str, Any]]:
        yield {
            "kind": "handoff",
            "terminal_id": "r1",
            "id": "o1",
            "timestamp": "2026-07-04T00:00:00Z",
            "detail": {"sender": "s", "receiver": "r1", "orchestration_type": "handoff"},
        }

    run_input = {
        "threadId": "t",
        "runId": "r",
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    frames = await _collect(rp.run_plane_stream(input_data=run_input, bus_subscribe_fn=bus))
    joined = "".join(frames)
    assert skipped == ["TOOL_CALL_END"]  # the open call's closer, from tracker.close_all()
    assert "TOOL_CALL_START" in joined and "TOOL_CALL_END" not in joined
    assert '"RUN_FINISHED"' in joined
