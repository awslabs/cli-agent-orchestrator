"""Overflow-handling for the SSE fan-out bus (services/sse_bus.py).

Reproduces the reviewer's must-fix #3 residue (PR #436, ``fanhongy``): a bounded
subscriber queue that fills silently drops the event *and* leaves the subscriber
open, so the client sees a gap with no signal to backfill. The fix turns overflow
into an explicit gap signal — the overflowing subscriber is marked ``overflowed``
and an ``OVERFLOW_SENTINEL`` is enqueued so the drain loop terminates the stream;
the client then reconnects (``Last-Event-ID``) and replays the dropped record.

These tests assert the NEW contract:
* an overflow-close subscriber is marked ``overflowed`` when its queue fills;
* ``drain`` yields the events buffered *before* the gap and then closes (returns)
  rather than blocking forever with the gap unsignalled;
* the events delivered before the gap are an unbroken *prefix* of the stream
  (the OLDEST buffered events are kept, the newest is dropped), so a reconnect
  replaying ``after_id(last_delivered)`` recovers every event with NO loss —
  the residual bug fanhongy flagged on PR #436 (drop-oldest left a hole before
  the client's ``Last-Event-ID`` that replay could never fill);
* a plain subscriber (``overflow_close=False``, the ``/events`` MCP-Apps path)
  keeps the legacy drop-on-slow behaviour and never closes on overflow.
"""

import asyncio

import pytest

from cli_agent_orchestrator.services import sse_bus as sse_bus_module
from cli_agent_orchestrator.services.event_log_service import EventLog
from cli_agent_orchestrator.services.sse_bus import OVERFLOW_SENTINEL, SseBus


async def _settle() -> None:
    """Let publish()'s call_soon_threadsafe _deliver callbacks run to completion."""

    for _ in range(4):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_overflow_marks_and_closes(monkeypatch) -> None:
    """Fill an overflow-close subscriber past cap → it is marked overflowed and
    ``drain`` delivers the pre-gap events then closes (no silent, open gap)."""

    # Small cap so the queue fills in a few publishes.
    monkeypatch.setattr(sse_bus_module, "SSE_MAX_QUEUE_SIZE", 3)

    bus = SseBus()
    sub = bus.register(overflow_close=True)

    # Publish well past capacity. publish() is non-blocking; _deliver runs on
    # this loop via call_soon_threadsafe, so yield to let them all fire.
    for i in range(sse_bus_module.SSE_MAX_QUEUE_SIZE + 5):
        bus.publish({"id": f"e{i}", "kind": "launch"})
    await _settle()

    # The overflow is now an explicit, inspectable signal — not a silent drop.
    assert sub.overflowed is True

    # Drain terminates instead of blocking forever: it yields the events buffered
    # before the gap, consumes the sentinel, and returns (StopAsyncIteration).
    received = []
    async for event in bus.drain(sub):
        received.append(event)

    # The sentinel itself is never surfaced to the caller.
    assert OVERFLOW_SENTINEL not in received
    # The delivered events are an unbroken PREFIX starting at the very first
    # event (e0, e1, …). The OLDEST buffered events are preserved and only the
    # newest buffered event is dropped to make room for the sentinel — so a
    # reconnect replaying after the last delivered id recovers everything with
    # no hole. (Drop-oldest would have dropped e0, leaving a gap BEFORE the
    # client's Last-Event-ID that replay could never fill — fanhongy's finding.)
    received_ids = [event["id"] for event in received]
    assert received_ids == [f"e{i}" for i in range(len(received_ids))]
    assert received_ids[0] == "e0"  # oldest event kept, not evicted
    # And the stream actually CLOSED (the async-for completed) rather than
    # hanging with the gap unsignalled — that is the whole point of the fix.

    bus.unregister(sub)
    assert bus.subscriber_count == 0


@pytest.mark.asyncio
async def test_overflow_recovery_replays_every_event(monkeypatch) -> None:
    """Overflow + reconnect-replay loses NO event (fanhongy's must-fix, PR #436).

    Reproduces the reviewer's exact scenario: a cap-3 queue fed e0..e3. On
    overflow the delivered events must be a contiguous prefix so that
    ``delivered + EventLog.after_id(delivered[-1]) == published`` — i.e. every
    published event is either delivered before the gap or replayed after the
    reconnect, none dropped. Before the fix (drop-oldest) e0 went missing:
    delivered=[e1, e2], replayed=[e3], observed lost e0.
    """

    monkeypatch.setattr(sse_bus_module, "SSE_MAX_QUEUE_SIZE", 3)

    bus = SseBus()
    log = EventLog()
    sub = bus.register(overflow_close=True)

    published: list[str] = []
    for i in range(4):
        event = log.append("launch", f"t{i}", None, {})
        event["id"] = f"e{i}"  # deterministic ids for the assertion
        published.append(event["id"])
        bus.publish(event)
    await _settle()

    # First connection: drain the pre-gap prefix until the stream closes.
    delivered = [event["id"] async for event in bus.drain(sub)]
    # Reconnect: replay everything after the last id the client actually saw.
    replayed = [event["id"] for event in log.after_id(delivered[-1])]
    observed = delivered + replayed

    missing = sorted(set(published) - set(observed))
    assert observed == published, f"overflow recovery lost {missing}"

    bus.unregister(sub)


@pytest.mark.asyncio
async def test_plain_subscriber_preserves_drop_on_slow(monkeypatch) -> None:
    """A default (``overflow_close=False``) subscriber — the ``/events`` MCP-Apps
    path — keeps legacy drop-on-slow: overflow neither marks nor closes it."""

    monkeypatch.setattr(sse_bus_module, "SSE_MAX_QUEUE_SIZE", 3)

    bus = SseBus()
    sub = bus.register()  # overflow_close defaults to False

    for i in range(sse_bus_module.SSE_MAX_QUEUE_SIZE + 5):
        bus.publish({"id": f"e{i}"})
    await _settle()

    assert sub.overflowed is False
    # No sentinel was enqueued; the queue holds only real (dropped-tail) events.
    drained = []
    while not sub.queue.empty():
        drained.append(sub.queue.get_nowait())
    assert OVERFLOW_SENTINEL not in drained
    assert len(drained) == sse_bus_module.SSE_MAX_QUEUE_SIZE  # bounded, no growth

    bus.unregister(sub)
