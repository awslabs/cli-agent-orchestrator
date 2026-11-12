"""The lossy publish path of the SSE bus (#1158, design point A).

The AG-UI stream subscribes with ``overflow_close=True``: a full queue ends the
stream with ``RUN_ERROR cao.stream_overflow``. Off-list ``emit_ui`` refusal
records are published with ``publish_lossy``, which keeps the upper half of
every queue for real events, so a refusal flood against a slow consumer can
never fill the queue and end the stream.
"""

import asyncio

import pytest

from cli_agent_orchestrator.services import sse_bus as sse_bus_module
from cli_agent_orchestrator.services.sse_bus import OVERFLOW_SENTINEL, SseBus


async def _settle() -> None:
    for _ in range(4):
        await asyncio.sleep(0)


def _drain_now(sub) -> list:
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


@pytest.mark.asyncio
async def test_a_refusal_flood_against_a_slow_consumer_leaves_room_for_every_real_event() -> None:
    """10,000 lossy records, interleaved with the real events the reserve is for,
    and a consumer that drains nothing until the end: every real event arrives,
    the subscriber never overflows, and no sentinel is queued."""
    bus = SseBus()
    sub = bus.register(overflow_close=True)
    reserve = sse_bus_module.SSE_MAX_QUEUE_SIZE // 2
    real = [f"real-{i}" for i in range(reserve)]
    step = 10_000 // reserve
    for i in range(10_000):
        bus.publish_lossy({"id": f"refusal-{i}", "kind": "other"})
        if i % step == 0 and i // step < reserve:
            bus.publish({"id": real[i // step], "kind": "launch"})
        if i % 100 == 0:
            await _settle()
    await _settle()
    assert sub.overflowed is False
    items = _drain_now(sub)
    assert OVERFLOW_SENTINEL not in items
    ids = [e["id"] for e in items]
    assert [i for i in ids if i.startswith("real-")] == real
    assert sum(1 for i in ids if i.startswith("refusal-")) <= reserve


@pytest.mark.asyncio
async def test_a_lossy_record_never_takes_a_queue_past_half() -> None:
    bus = SseBus()
    sub = bus.register(overflow_close=True)
    for i in range(sse_bus_module.SSE_MAX_QUEUE_SIZE * 2):
        bus.publish_lossy({"id": f"r{i}"})
    await _settle()
    assert sub.queue.qsize() == sse_bus_module.SSE_MAX_QUEUE_SIZE // 2
    assert sub.overflowed is False


@pytest.mark.asyncio
async def test_a_lossy_record_is_skipped_once_real_events_pass_the_mark() -> None:
    """Real events alone may fill the queue; a lossy one then adds nothing and
    the next real event still meets the ordinary overflow path, unchanged."""
    bus = SseBus()
    sub = bus.register(overflow_close=True)
    half = sse_bus_module.SSE_MAX_QUEUE_SIZE // 2
    for i in range(half):
        bus.publish({"id": f"e{i}"})
    bus.publish_lossy({"id": "lossy"})
    await _settle()
    assert [e["id"] for e in _drain_now(sub)] == [f"e{i}" for i in range(half)]


@pytest.mark.asyncio
async def test_a_lossy_record_is_not_delivered_to_an_overflowed_subscriber() -> None:
    bus = SseBus()
    sub = bus.register(overflow_close=True)
    sub.overflowed = True
    bus.publish_lossy({"id": "lossy"})
    await _settle()
    assert sub.queue.empty()


@pytest.mark.asyncio
async def test_a_drop_on_slow_subscriber_gets_lossy_records_below_the_mark() -> None:
    bus = SseBus()
    sub = bus.register()
    bus.publish_lossy({"id": "lossy"})
    await _settle()
    assert [e["id"] for e in _drain_now(sub)] == ["lossy"]


def test_a_lossy_publish_to_a_closed_loop_does_not_raise() -> None:
    bus = SseBus()
    loop = asyncio.new_event_loop()
    try:
        sub = loop.run_until_complete(_register(bus))
    finally:
        loop.close()
    bus.publish_lossy({"id": "lossy"})  # the closed loop raises RuntimeError inside
    assert sub.queue.empty()


async def _register(bus: SseBus):
    return bus.register(overflow_close=True)


async def _stalled_with_lossy(n_lossy: int):
    bus = SseBus()
    sub = bus.register(overflow_close=True)
    for i in range(n_lossy):
        bus.publish_lossy({"id": f"refusal-{i}"})
    await _settle()
    assert sub.queue.qsize() == n_lossy
    return bus, sub


@pytest.mark.asyncio
async def test_queued_lossy_records_give_way_to_a_full_queue_of_real_events() -> None:
    """FIX 1 (#1158 review): refusals queued while a consumer stalls must not
    shorten the real events' headroom. 127 lossy records, then a full queue's
    worth of real events: every real event is kept, in order, and nothing
    overflows; each lossy record gave way to one."""
    bus, sub = await _stalled_with_lossy(127)
    real = [f"real-{i}" for i in range(sse_bus_module.SSE_MAX_QUEUE_SIZE)]
    for rid in real:
        bus.publish({"id": rid})
    await _settle()
    assert sub.overflowed is False
    items = _drain_now(sub)
    assert OVERFLOW_SENTINEL not in items
    assert [e["id"] for e in items] == real


@pytest.mark.asyncio
async def test_127_lossy_then_129_real_deliver_every_real_event_in_order() -> None:
    bus, sub = await _stalled_with_lossy(127)
    real = [f"real-{i}" for i in range(129)]
    for rid in real:
        bus.publish({"id": rid})
    await _settle()
    items = _drain_now(sub)
    assert sub.overflowed is False and OVERFLOW_SENTINEL not in items
    assert [e["id"] for e in items if e["id"].startswith("real-")] == real


@pytest.mark.asyncio
async def test_the_newest_lossy_record_is_the_one_dropped() -> None:
    bus, sub = await _stalled_with_lossy(2)  # refusal-0, refusal-1
    for i in range(sse_bus_module.SSE_MAX_QUEUE_SIZE - 1):
        bus.publish({"id": f"real-{i}"})
    await _settle()
    assert [e["id"] for e in _drain_now(sub)][:1] == ["refusal-0"]


@pytest.mark.asyncio
async def test_once_no_lossy_record_is_left_a_real_event_overflows_as_before() -> None:
    """Real events alone still overflow exactly as before: the 257th, after all
    lossy records gave way, marks the gap and queues the sentinel."""
    bus, sub = await _stalled_with_lossy(127)
    for i in range(sse_bus_module.SSE_MAX_QUEUE_SIZE + 1):
        bus.publish({"id": f"real-{i}"})
    await _settle()
    assert sub.overflowed is True
    items = _drain_now(sub)
    assert items[-1] is OVERFLOW_SENTINEL
    assert not [e for e in items[:-1] if e["id"].startswith("refusal-")]


@pytest.mark.asyncio
async def test_draining_a_lossy_record_forgets_it() -> None:
    bus, sub = await _stalled_with_lossy(1)
    gen = bus.drain(sub)
    assert (await gen.__anext__())["id"] == "refusal-0"
    assert sub.lossy == set()
    await gen.aclose()


@pytest.mark.asyncio
async def test_a_stale_lossy_tag_falls_through_to_the_ordinary_overflow() -> None:
    """#1158 review: were `lossy` ever to hold an id with no queued match, the drop
    must not raise inside the delivery callback (that would lose the real event
    silently): it takes the ordinary overflow path, as if no tag were there."""
    bus = SseBus()
    sub = bus.register(overflow_close=True)
    sub.lossy.add(id(object()))  # a stale tag: nothing queued has this id
    for i in range(sse_bus_module.SSE_MAX_QUEUE_SIZE):
        bus.publish({"id": f"real-{i}"})
    loop = asyncio.get_running_loop()
    errors = []
    loop.set_exception_handler(lambda _loop, ctx: errors.append(ctx))
    try:
        bus.publish({"id": "one-more"})
        await _settle()
    finally:
        loop.set_exception_handler(None)
    assert errors == []
    assert sub.overflowed is True
    assert sub.lossy == set()
    items = _drain_now(sub)
    assert items[-1] is OVERFLOW_SENTINEL
    assert [e["id"] for e in items[:-1]] == [
        f"real-{i}" for i in range(sse_bus_module.SSE_MAX_QUEUE_SIZE - 1)
    ]
