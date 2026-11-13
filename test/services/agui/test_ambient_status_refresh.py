"""A status change alone refreshes the ambient fleet snapshot (a STATE_DELTA).

The stream used to rebuild its snapshot only when a lifecycle record arrived (a launch, a
kill, a message), so a worker that went from ``unknown`` to ``idle`` showed ``unknown`` for
up to a heartbeat. The stream now also listens to the in-process status topic
(``terminal.<id>.status``, published by the status monitor only when the applied status
CHANGES) and refreshes, coalesced to at most once per ``STATUS_REFRESH_MIN_SECONDS`` with
the trailing change always delivered. It adds no publisher, no thread and no timer: the
stream's own wait wakes for it, and the stream exists only when ``CAO_AGUI_ENABLED`` is on.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, List

import pytest

from cli_agent_orchestrator.services import event_bus
from cli_agent_orchestrator.services.agui import ambient_run
from cli_agent_orchestrator.services.ui_state_service import diff_snapshot


class _Sub:
    overflowed = False


class _QuietBus:
    """Never yields a fleet record: only status events can move the stream."""

    async def drain(self, sub: Any) -> AsyncIterator[Dict[str, Any]]:
        await asyncio.Event().wait()
        return
        yield {}


def _encode(sse_id: Any, event: Any) -> Dict[str, Any]:
    return event.model_dump(by_alias=True, exclude_none=True)


class _Fleet:
    """A fleet whose terminal ``t1`` has a status the test changes."""

    def __init__(self) -> None:
        self.status: Any = None
        self.calls = 0

    def __call__(self) -> Dict[str, Any]:
        self.calls += 1
        return {
            "sessions": [],
            "terminals": [{"id": "t1", "session_name": "s", "status": self.status}],
        }


def _stream(fleet: _Fleet, *, max_seconds: float = 6) -> Any:
    run = ambient_run.AmbientRun("t", "r", incarnation_of=lambda name: None)
    return ambient_run.ambient_stream(
        run=run,
        encode=_encode,
        is_sse=False,
        replay=[],
        fleet_snapshot=fleet,
        diff=diff_snapshot,
        bus=_QuietBus(),
        sub=_Sub(),
        keepalive_seconds=5.0,
        max_seconds=max_seconds,
    )


async def _next(stream: Any, kind: str, timeout: float = 3.0) -> Dict[str, Any]:
    async def go() -> Dict[str, Any]:
        async for frame in stream:
            if frame.get("type") == kind:
                return frame
        raise AssertionError(f"stream ended without {kind}")

    return await asyncio.wait_for(go(), timeout)


def _publish(terminal: str, status: str) -> None:
    event_bus.bus.publish(f"terminal.{terminal}.status", {"status": status})


@pytest.fixture(autouse=True)
def _loop_bound_bus(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ambient_run, "STATUS_REFRESH_MIN_SECONDS", 0.3, raising=False)
    yield
    event_bus.bus.set_loop(None)


def _wildcards() -> int:
    return sum(len(queues) for _, queues in event_bus.bus._wildcard.values())


@pytest.mark.asyncio
async def test_a_status_change_alone_yields_a_state_delta_with_the_new_status() -> None:
    event_bus.bus.set_loop(asyncio.get_running_loop())
    before = _wildcards()
    fleet = _Fleet()
    stream = _stream(fleet)
    await _next(stream, "STATE_SNAPSHOT")
    fleet.status = "idle"
    _publish("t1", "idle")
    delta = await _next(stream, "STATE_DELTA")
    (op,) = delta["delta"]
    assert op["path"] == "/terminals" and op["value"][0]["status"] == "idle"
    assert _wildcards() == before + len(ambient_run._STATUS_TOPICS), "subscribed while open"
    await stream.aclose()
    assert _wildcards() == before, "unsubscribed when the consumer closes the stream"


@pytest.mark.asyncio
async def test_rapid_changes_coalesce_and_the_last_one_is_delivered() -> None:
    event_bus.bus.set_loop(asyncio.get_running_loop())
    fleet = _Fleet()
    stream = _stream(fleet)
    await _next(stream, "STATE_SNAPSHOT")
    before = fleet.calls
    for i in range(60):
        fleet.status = "processing" if i % 2 == 0 else "idle"
        _publish("t1", fleet.status)
    fleet.status = "completed"
    _publish("t1", "completed")
    seen: List[str] = []
    async for frame in stream:
        if frame.get("type") == "STATE_DELTA":
            seen.append(frame["delta"][0]["value"][0]["status"])
            if seen[-1] == "completed":
                break
    assert seen[-1] == "completed", "the trailing change must reach the client"
    assert fleet.calls - before <= 3, "61 changes must not cause 61 snapshots"
    await stream.aclose()


@pytest.mark.asyncio
async def test_no_status_event_means_no_extra_snapshot() -> None:
    event_bus.bus.set_loop(asyncio.get_running_loop())
    fleet = _Fleet()
    stream = _stream(fleet, max_seconds=1)
    frames = [f async for f in stream]
    assert fleet.calls == 1, "only the initial snapshot"
    assert [f["type"] for f in frames if f["type"] == "STATE_DELTA"] == []


@pytest.mark.asyncio
async def test_the_subscription_is_removed_when_the_stream_ends() -> None:
    event_bus.bus.set_loop(asyncio.get_running_loop())
    before = _wildcards()
    stream = _stream(_Fleet(), max_seconds=1)
    first = await _next(stream, "RUN_STARTED")
    assert first["type"] == "RUN_STARTED"
    assert _wildcards() == before + len(ambient_run._STATUS_TOPICS)
    async for _ in stream:
        pass
    assert _wildcards() == before


@pytest.mark.asyncio
async def test_a_bus_that_cannot_subscribe_does_not_break_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("bus down")

    monkeypatch.setattr(event_bus.bus, "subscribe", boom)
    fleet = _Fleet()
    frames = [f async for f in _stream(fleet, max_seconds=1)]
    assert any(f["type"] == "STATE_SNAPSHOT" for f in frames)
    assert frames[-1]["type"] in ("RUN_FINISHED", "RUN_ERROR")


def test_flag_unset_means_no_stream_and_no_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api.main import app

    monkeypatch.delenv("CAO_AGUI_ENABLED", raising=False)
    monkeypatch.delenv("CAO_MCP_APPS_ENABLED", raising=False)
    before = _wildcards()
    assert TestClient(app, base_url="http://localhost").get("/agui/v1/stream").status_code == 404
    assert _wildcards() == before


@pytest.mark.asyncio
async def test_changes_further_apart_than_the_gap_are_each_delivered() -> None:
    event_bus.bus.set_loop(asyncio.get_running_loop())
    fleet = _Fleet()
    stream = _stream(fleet, max_seconds=8)
    await _next(stream, "STATE_SNAPSHOT")
    seen: List[str] = []
    for status in ("idle", "processing", "completed"):
        fleet.status = status
        _publish("t1", status)
        frame = await _next(stream, "STATE_DELTA", timeout=4.0)
        seen.append(frame["delta"][0]["value"][0]["status"])
    assert seen == ["idle", "processing", "completed"]
    await stream.aclose()


@pytest.mark.asyncio
async def test_a_failing_unsubscribe_does_not_break_the_end_of_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("bus down")

    real_unsubscribe = event_bus.bus.unsubscribe
    real_subscribe = event_bus.bus.subscribe
    queues: List[Any] = []

    def recording_subscribe(pattern: str) -> Any:
        queue = real_subscribe(pattern)
        queues.append((pattern, queue))
        return queue

    monkeypatch.setattr(event_bus.bus, "subscribe", recording_subscribe)
    monkeypatch.setattr(event_bus.bus, "unsubscribe", boom)
    frames = [f async for f in _stream(_Fleet(), max_seconds=1)]
    assert frames[-1]["type"] in ("RUN_FINISHED", "RUN_ERROR")
    # The stream could not remove its queue: remove exactly that one, so a leak of any
    # other queue would still show in the baseline checks of the other tests.
    assert len(queues) == len(ambient_run._STATUS_TOPICS)
    for pattern, queue in queues:
        real_unsubscribe(pattern, queue)


# --- the subscription is airtight: nothing between subscribing and the try/finally leaks it --


@pytest.mark.asyncio
async def test_a_setup_step_that_raises_leaves_no_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`bus.drain(sub)` is evaluated before the stream's try block; if it raises, the status
    subscription must not have been taken (or must be released)."""
    event_bus.bus.set_loop(asyncio.get_running_loop())
    before = _wildcards()

    class _BrokenBus:
        def drain(self, sub: Any) -> Any:
            raise RuntimeError("drain cannot start")

    run = ambient_run.AmbientRun("t", "r", incarnation_of=lambda name: None)
    stream = ambient_run.ambient_stream(
        run=run,
        encode=_encode,
        is_sse=False,
        replay=[],
        fleet_snapshot=_Fleet(),
        diff=diff_snapshot,
        bus=_BrokenBus(),
        sub=_Sub(),
        keepalive_seconds=5.0,
        max_seconds=1,
    )
    with pytest.raises(RuntimeError, match="drain cannot start"):
        async for _ in stream:
            pass
    assert _wildcards() == before


@pytest.mark.asyncio
async def test_a_stop_waiter_that_raises_leaves_no_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> Any:
        raise RuntimeError("no waiter")

    monkeypatch.setattr(ambient_run, "_register_stop_waiter", boom)
    event_bus.bus.set_loop(asyncio.get_running_loop())
    before = _wildcards()
    with pytest.raises(RuntimeError, match="no waiter"):
        async for _ in _stream(_Fleet(), max_seconds=1):
            pass
    assert _wildcards() == before


@pytest.mark.asyncio
async def test_a_failure_inside_the_run_still_unsubscribes(monkeypatch: pytest.MonkeyPatch) -> None:
    event_bus.bus.set_loop(asyncio.get_running_loop())
    before = _wildcards()
    run = ambient_run.AmbientRun("t", "r", incarnation_of=lambda name: None)
    monkeypatch.setattr(run, "start", lambda: (_ for _ in ()).throw(RuntimeError("start failed")))
    stream = ambient_run.ambient_stream(
        run=run,
        encode=_encode,
        is_sse=False,
        replay=[],
        fleet_snapshot=_Fleet(),
        diff=diff_snapshot,
        bus=_QuietBus(),
        sub=_Sub(),
        keepalive_seconds=5.0,
        max_seconds=1,
    )
    frames = [f async for f in stream]
    assert frames and frames[-1]["type"] == "RUN_ERROR"
    assert _wildcards() == before


# --- keep-alive is not starved by status events that produce no frame ----------------------


@pytest.mark.asyncio
async def test_status_events_that_produce_no_delta_do_not_starve_the_keep_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A status event for a terminal the snapshot does not show changes nothing, so no frame
    goes out; if such events recur faster than the keep-alive interval the wait never times
    out, and an idle-timeout proxy would cut the connection. A keep-alive must still go out."""
    monkeypatch.setattr(ambient_run, "STATUS_REFRESH_MIN_SECONDS", 0.05)
    event_bus.bus.set_loop(asyncio.get_running_loop())
    fleet = _Fleet()
    run = ambient_run.AmbientRun("t", "r", incarnation_of=lambda name: None)
    stream = ambient_run.ambient_stream(
        run=run,
        encode=_encode,
        is_sse=True,
        replay=[],
        fleet_snapshot=fleet,
        diff=diff_snapshot,
        bus=_QuietBus(),
        sub=_Sub(),
        keepalive_seconds=0.3,
        max_seconds=3,
    )

    async def chatter() -> None:
        for _ in range(60):
            _publish("not-in-the-snapshot", "processing")
            await asyncio.sleep(0.04)

    task = asyncio.ensure_future(chatter())
    keepalives = 0
    deltas = 0
    async for frame in stream:
        if isinstance(frame, str) and frame.startswith(":keep-alive"):
            keepalives += 1
            if keepalives >= 2:
                break
        elif isinstance(frame, dict) and frame.get("type") == "STATE_DELTA":
            deltas += 1
    task.cancel()
    await stream.aclose()
    assert deltas == 0, "the snapshot never changed"
    assert keepalives >= 2, "status chatter starved the keep-alive"


# --- the final change must not be lost (found by the evidence recorder) ------------------
#
# The recorder saw the stream left at ``unknown`` while ``GET /terminals/<id>`` said
# ``completed``. Root cause: the snapshot reports a terminal's status through the same mask
# ``GET /terminals/<id>`` uses, which shows ``unknown`` instead of idle/completed while the
# terminal's first message is not yet dispatched. The mask lifts when the pending mark is
# cleared, which is NOT a status change, so no ``terminal.<id>.status`` event followed and the
# stream stayed at the masked value. Clearing the mark now announces itself on the bus, and
# the stream refreshes on that too.


def _live_fleet() -> Any:
    """A fleet whose statuses come through the real live-status path (monitor + mask)."""
    from cli_agent_orchestrator.services.terminal_service import with_live_status

    def fleet() -> Dict[str, Any]:
        rows = [{"id": "t1", "session_name": "s"}]
        return {"sessions": [], "terminals": with_live_status(rows)}

    return fleet


@pytest.fixture
def _monitor_and_pending(monkeypatch: pytest.MonkeyPatch):
    from cli_agent_orchestrator.models.terminal import TerminalStatus
    from cli_agent_orchestrator.services import terminal_service
    from cli_agent_orchestrator.services.status_monitor import status_monitor

    monkeypatch.setattr(status_monitor, "_last_status", {"t1": TerminalStatus.COMPLETED})
    monkeypatch.setattr(terminal_service, "_pending_initial_delivery", {"t1"})
    return terminal_service


@pytest.mark.asyncio
async def test_lifting_the_first_message_mask_refreshes_the_stream(
    _monitor_and_pending: Any,
) -> None:
    terminal_service = _monitor_and_pending
    event_bus.bus.set_loop(asyncio.get_running_loop())
    run = ambient_run.AmbientRun("t", "r", incarnation_of=lambda name: None)
    stream = ambient_run.ambient_stream(
        run=run,
        encode=_encode,
        is_sse=False,
        replay=[],
        fleet_snapshot=_live_fleet(),
        diff=diff_snapshot,
        bus=_QuietBus(),
        sub=_Sub(),
        keepalive_seconds=5.0,
        max_seconds=6,
    )
    snapshot = await _next(stream, "STATE_SNAPSHOT")
    assert snapshot["snapshot"]["terminals"][0]["status"] == "unknown", "masked while pending"
    terminal_service._clear_initial_delivery_pending("t1")  # the mask lifts; no status event
    delta = await _next(stream, "STATE_DELTA")
    assert delta["delta"][0]["value"][0]["status"] == "completed"
    await stream.aclose()


def test_clearing_the_mark_twice_announces_once() -> None:
    from cli_agent_orchestrator.services import terminal_service

    seen: List[Any] = []
    original = event_bus.bus.publish
    event_bus.bus.publish = lambda topic, data: seen.append((topic, data))  # type: ignore[method-assign]
    try:
        terminal_service._pending_initial_delivery.add("tz")
        terminal_service._clear_initial_delivery_pending("tz")
        terminal_service._clear_initial_delivery_pending("tz")
        terminal_service._clear_initial_delivery_pending("never-pending")
    finally:
        event_bus.bus.publish = original  # type: ignore[method-assign]
    assert seen == [("terminal.tz.delivery", {"pending": False})]


# --- the coalescing gap never drops the trailing change -------------------------------------


@pytest.mark.asyncio
async def test_three_changes_inside_the_gap_end_on_the_last_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ambient_run, "STATUS_REFRESH_MIN_SECONDS", 0.5)
    event_bus.bus.set_loop(asyncio.get_running_loop())
    fleet = _Fleet()
    stream = _stream(fleet)
    await _next(stream, "STATE_SNAPSHOT")
    fleet.status = "processing"
    _publish("t1", "processing")
    first = await _next(stream, "STATE_DELTA")  # leading edge may be immediate or after the gap
    assert first["delta"][0]["value"][0]["status"] == "processing"
    for status in ("idle", "processing", "completed"):  # all inside the gap after that refresh
        fleet.status = status
        _publish("t1", status)
        await asyncio.sleep(0.02)
    last = await _next(stream, "STATE_DELTA", timeout=2.0)
    assert last["delta"][0]["value"][0]["status"] == "completed"
    await stream.aclose()


@pytest.mark.asyncio
async def test_a_change_that_lands_while_the_snapshot_is_being_built_is_not_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ambient_run, "STATUS_REFRESH_MIN_SECONDS", 0.3)
    event_bus.bus.set_loop(asyncio.get_running_loop())
    fleet = _Fleet()
    stream_calls = {"n": 0}
    inner = fleet.__call__

    def racing() -> Dict[str, Any]:
        snap = inner()
        stream_calls["n"] += 1
        if stream_calls["n"] == 2:  # the first refresh after the initial snapshot
            fleet.status = "completed"  # changes while this build is running...
            _publish("t1", "completed")  # ...and announces itself after it read the old one
        return snap

    stream = _stream(racing)  # type: ignore[arg-type]
    await _next(stream, "STATE_SNAPSHOT")
    fleet.status = "processing"
    _publish("t1", "processing")
    seen: List[str] = []
    async for frame in stream:
        if frame.get("type") == "STATE_DELTA":
            seen.append(frame["delta"][0]["value"][0]["status"])
            if seen[-1] == "completed":
                break
    assert seen[-1] == "completed", seen
    await stream.aclose()


def test_a_bus_that_cannot_publish_never_affects_the_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli_agent_orchestrator.services import terminal_service

    calls: List[Any] = []

    def boom(*args: Any, **_k: Any) -> None:
        calls.append(args)
        raise RuntimeError("bus down")

    monkeypatch.setattr(event_bus.bus, "publish", boom)
    monkeypatch.setattr(terminal_service, "_pending_initial_delivery", {"tq"})
    terminal_service._clear_initial_delivery_pending("tq")
    assert calls == [("terminal.tq.delivery", {"pending": False})], "it must try to publish, once"
    assert "tq" not in terminal_service._pending_initial_delivery


@pytest.mark.asyncio
async def test_a_second_subscription_that_fails_still_releases_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first topic subscribes, the second raises: the stream runs on the one topic it has,
    and its queue is released at the end (no leak)."""
    event_bus.bus.set_loop(asyncio.get_running_loop())
    before = _wildcards()
    real_subscribe = event_bus.bus.subscribe
    taken: List[str] = []

    def flaky(pattern: str) -> Any:
        if taken:
            raise RuntimeError("second subscribe fails")
        taken.append(pattern)
        return real_subscribe(pattern)

    monkeypatch.setattr(event_bus.bus, "subscribe", flaky)
    fleet = _Fleet()
    stream = _stream(fleet)
    await _next(stream, "STATE_SNAPSHOT")
    assert taken == [ambient_run._STATUS_TOPICS[0]]
    assert _wildcards() == before + 1, "only the first topic is held"
    fleet.status = "idle"
    _publish("t1", "idle")  # the first topic is terminal.*.status: the stream still reacts
    delta = await _next(stream, "STATE_DELTA")
    assert delta["delta"][0]["value"][0]["status"] == "idle"
    await stream.aclose()
    assert _wildcards() == before, "the first queue is released although the second failed"


@pytest.mark.asyncio
async def test_a_status_change_and_a_fleet_record_in_the_same_wake_are_both_handled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The status refresh must not swallow a fleet record that is ready in the same wake."""
    from test._support import agui_ambient_records as records

    monkeypatch.setattr(ambient_run, "STATUS_REFRESH_MIN_SECONDS", 0.2)
    event_bus.bus.set_loop(asyncio.get_running_loop())
    go = asyncio.Event()

    class _GatedBus:
        async def drain(self, sub: Any) -> AsyncIterator[Dict[str, Any]]:
            await go.wait()
            yield records.PRIMITIVE_RECORDS[0]
            await asyncio.Event().wait()

    fleet = _Fleet()
    run = ambient_run.AmbientRun("t", "r", incarnation_of=lambda name: None)
    stream = ambient_run.ambient_stream(
        run=run,
        encode=_encode,
        is_sse=False,
        replay=[],
        fleet_snapshot=fleet,
        diff=diff_snapshot,
        bus=_GatedBus(),
        sub=_Sub(),
        keepalive_seconds=5.0,
        max_seconds=6,
    )
    await _next(stream, "STATE_SNAPSHOT")
    fleet.status = "processing"
    _publish("t1", "processing")
    first = await _next(stream, "STATE_DELTA")
    assert first["delta"][0]["value"][0]["status"] == "processing"
    # The consumer holds the stream here (it is suspended at that yield) while the gap passes,
    # a second status change arrives and a fleet record becomes ready: the next pass wakes with
    # BOTH done, refreshes for the status, and must still go on to handle the record.
    await asyncio.sleep(0.4)
    fleet.status = "idle"
    _publish("t1", "idle")
    go.set()
    await asyncio.sleep(0.05)
    delta = await _next(stream, "STATE_DELTA")
    assert delta["delta"][0]["value"][0]["status"] == "idle"
    # Resume the stream past the delta: the record that was ready in the same wake is handled
    # next (its frames follow), instead of being skipped.
    following = await asyncio.wait_for(stream.__anext__(), 3.0)
    assert following["type"] != "RUN_ERROR"
    await stream.aclose()
