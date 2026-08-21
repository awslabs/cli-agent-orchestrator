"""Unit tests for the SSE fan-out bus (services/sse_bus.py).

Covers drop-on-slow back-pressure, non-blocking publish, multi-subscriber
isolation, subscriber registration/removal, and the singleton accessor.
"""

import asyncio

import pytest

from cli_agent_orchestrator.services import sse_bus as sse_bus_module
from cli_agent_orchestrator.services.sse_bus import SseBus, get_bus


async def _start(bus: SseBus):
    """Subscribe and advance the generator to its first ``await queue.get()``.

    Returns the generator and the in-flight ``__anext__`` task so the caller can
    consume one event (``await task``) or cancel for cleanup.
    """

    gen = bus.subscribe()
    task = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0)  # let the generator register its queue
    return gen, task


async def _stop(gen, task) -> None:
    """Cancel the in-flight read and let the generator's finally remove the sub."""

    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, StopAsyncIteration):
        pass
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_publish_delivers_to_subscriber() -> None:
    """A subscribed iframe receives published events."""

    bus = SseBus()
    gen, task = await _start(bus)

    bus.publish({"id": "1", "kind": "launch"})
    received = await task
    assert received == {"id": "1", "kind": "launch"}

    await gen.aclose()


@pytest.mark.asyncio
async def test_full_queue_drops_without_blocking_producer() -> None:
    """A full subscriber queue drops events; publish returns immediately."""

    bus = SseBus()
    gen, task = await _start(bus)

    # Fill well past the bounded capacity. None of these should raise or block
    # even though the subscriber never drains its queue.
    for i in range(sse_bus_module.SSE_MAX_QUEUE_SIZE + 50):
        bus.publish({"id": str(i)})

    assert bus.subscriber_count == 1
    await _stop(gen, task)


@pytest.mark.asyncio
async def test_slow_subscriber_does_not_starve_healthy_subscriber() -> None:
    """A stalled subscriber's full queue must not stop delivery to others."""

    bus = SseBus()

    # Slow subscriber: registered but never drained; fill its queue.
    slow_gen, slow_task = await _start(bus)
    for i in range(sse_bus_module.SSE_MAX_QUEUE_SIZE + 5):
        bus.publish({"id": f"pre-{i}"})

    # Healthy subscriber joins now with an empty queue.
    healthy_gen, healthy_task = await _start(bus)

    bus.publish({"id": "healthy-event"})
    received = await asyncio.wait_for(healthy_task, timeout=1.0)
    assert received == {"id": "healthy-event"}

    await _stop(slow_gen, slow_task)
    await healthy_gen.aclose()


@pytest.mark.asyncio
async def test_disconnect_removes_subscriber_from_active_set() -> None:
    """Closing the generator removes the subscriber's queue."""

    bus = SseBus()
    gen, task = await _start(bus)
    assert bus.subscriber_count == 1

    await _stop(gen, task)
    assert bus.subscriber_count == 0


@pytest.mark.asyncio
async def test_publish_from_other_thread_delivers() -> None:
    """publish() invoked off the event loop still reaches the subscriber.

    asyncio.Queue is not thread-safe, so the bus must marshal the put onto the
    subscriber's own loop via call_soon_threadsafe — exercised here by
    publishing from a separate OS thread (mirrors a plugin hook under
    asyncio.run).
    """

    import threading

    bus = SseBus()
    gen, task = await _start(bus)

    t = threading.Thread(target=lambda: bus.publish({"id": "from-thread"}))
    t.start()
    t.join()

    received = await asyncio.wait_for(task, timeout=1.0)
    assert received == {"id": "from-thread"}

    await gen.aclose()


def test_publish_with_no_subscribers_is_noop() -> None:
    """Publishing to an empty bus does not raise."""

    bus = SseBus()
    bus.publish({"id": "x"})
    assert bus.subscriber_count == 0


def test_get_bus_returns_singleton() -> None:
    assert get_bus() is get_bus()


def test_dead_loop_subscriber_is_dropped() -> None:
    """A subscriber whose loop is closed is unregistered on publish.

    When ``loop.call_soon_threadsafe`` raises ``RuntimeError`` (closed/closing
    loop) the bus must drop that subscriber from ``_subs`` rather than leaving a
    stale entry that is retried — and re-logged — on every subsequent publish
    (Copilot C3). Exercised with a stub loop so no real event loop is needed.
    """

    bus = SseBus()

    class _DeadLoop:
        def call_soon_threadsafe(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("Event loop is closed")

    queue: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=8)
    sub = sse_bus_module._Subscriber(queue, _DeadLoop(), False)  # type: ignore[arg-type]
    with bus._lock:
        bus._subs.append(sub)
    assert bus.subscriber_count == 1

    # publish must not raise, and must drop the dead subscriber.
    bus.publish({"id": "1", "kind": "launch"})
    assert bus.subscriber_count == 0

    # A second publish is a clean no-op — the stale entry is gone.
    bus.publish({"id": "2", "kind": "launch"})
    assert bus.subscriber_count == 0


@pytest.mark.asyncio
async def test_subscribe_finally_tolerates_already_removed_entry():
    """If the entry was already removed (e.g. by publish's dead-loop cleanup),
    the generator's finally must swallow the ValueError rather than raise."""
    bus = SseBus()
    gen, task = await _start(bus)
    with bus._lock:
        bus._subs.clear()  # simulate the entry having been removed already
    await _stop(gen, task)  # finally runs remove() -> ValueError -> pass
    assert bus.subscriber_count == 0


def test_reset_bus_drops_singleton():
    first = get_bus()
    sse_bus_module.reset_bus()
    assert get_bus() is not first
