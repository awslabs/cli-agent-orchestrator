"""A dropped FINAL output event becomes a reported gap (#745, review finding 5).

``_stream_positions`` advertised only ``buf.end_pos`` — bytes the bridge
dequeued. When the bounded bus drops the LAST event, the producer's tail never
enters the buffer and no later event triggers a gap, so the server was told the
stream was complete short of the producer's real end. The producer now publishes
through ``publish_with_loss_markers``, which owes the refusing queue a marker;
the bridge collects it with ``flush_owed_to`` whenever its queue drains, records
the hole, and reports a GapFrame — so the advertised watermark reaches the
producer's true end.
"""

import asyncio
import re

import pytest

from cli_agent_orchestrator.runtime_channel.bridge import Bridge
from cli_agent_orchestrator.runtime_channel.protocol import GapFrame, StreamName

TID = "aaaa1111"


class TestTheBusOwesAndFlushesAMarker:
    @staticmethod
    def _bus_with(queue):
        from cli_agent_orchestrator.services.event_bus import EventBus

        bus = EventBus()
        bus._loop = asyncio.new_event_loop()
        with bus._lock:
            bus._exact["terminal.t1.output"] = [queue]
        return bus

    def test_publish_with_loss_markers_owes_a_refused_queue(self):
        full = asyncio.Queue(maxsize=1)
        bus = self._bus_with(full)
        # First delivery fills the queue via the direct dispatch path.
        assert bus._dispatch("terminal.t1.output", {"data": "a"}) == 0
        # The second is refused and owed to this queue.
        assert (
            bus._dispatch("terminal.t1.output", {"data": "b"}, lost={"from_pos": 1, "to_pos": 2})
            == 1
        )

        full.get_nowait()  # consumer drains
        assert bus.flush_owed_to(full) == 0  # the owed marker is delivered
        marker = full.get_nowait()
        assert marker["data"] == {"data": "", "gap": {"from_pos": 1, "to_pos": 2}}

    def test_flush_owed_to_reports_a_still_full_queue(self):
        full = asyncio.Queue(maxsize=1)
        bus = self._bus_with(full)
        bus._dispatch("terminal.t1.output", {"data": "a"})
        bus._dispatch("terminal.t1.output", {"data": "b"}, lost={"from_pos": 1, "to_pos": 2})
        # Queue is still full, so the marker cannot be delivered and stays owed.
        assert bus.flush_owed_to(full) == 1


class TestADroppedFinalEventIsReportedAsATailGap:
    @pytest.mark.asyncio
    async def test_the_bridge_reports_the_dropped_tail_and_advertises_the_true_end(self):
        from cli_agent_orchestrator.services.event_bus import bus

        bus.set_loop(asyncio.get_running_loop())
        bridge = Bridge("ws://unused", "worker-x", "tok")
        sent = []

        async def _send(frame):
            sent.append(frame)

        bridge._send = _send

        topic = f"terminal.{TID}.output"
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        regex = r"terminal\.[^.]+\.output"
        with bus._lock:
            prev = bus._wildcard.get(regex)
            bus._wildcard[regex] = (re.compile(f"^{regex}$"), [queue])
        try:
            # The first event is delivered; the tail event is refused (queue
            # full) and owed as a [4, 8) marker.
            bus._dispatch(topic, {"data": "AAAA", "offset": 0, "epoch": 0})
            dropped = bus._dispatch(
                topic,
                {"data": "BBBB", "offset": 4, "epoch": 0},
                lost={"from_pos": 4, "to_pos": 8, "generation": 0},
            )
            assert dropped == 1

            with patch_subscribe(bus, queue):
                task = asyncio.ensure_future(bridge._forward_output())
                for _ in range(200):
                    if any(isinstance(f, GapFrame) for f in sent):
                        break
                    await asyncio.sleep(0.005)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        finally:
            with bus._lock:
                if prev is not None:
                    bus._wildcard[regex] = prev
                else:
                    bus._wildcard.pop(regex, None)
                for k in [k for k in bus._owed_loss if k[0] == id(queue)]:
                    bus._owed_loss.pop(k, None)
            bus.set_loop(None)

        gaps = [f for f in sent if isinstance(f, GapFrame) and f.stream == StreamName.CAPTURE]
        assert [(g.from_pos, g.to_pos, g.generation) for g in gaps] == [(4, 8, 0)]
        # The advertised watermark now reaches the producer's true end (8), not
        # the 4 bytes the bridge actually dequeued.
        positions = bridge._stream_positions()
        assert [(p.terminal_id, p.end_pos) for p in positions if p.terminal_id == TID] == [(TID, 8)]


class _patch_subscribe:
    """Make the bridge subscribe to a pre-registered queue and no-op unsubscribe."""

    def __init__(self, bus, queue):
        self._bus = bus
        self._queue = queue
        self._orig_sub = bus.subscribe
        self._orig_unsub = bus.unsubscribe

    def __enter__(self):
        self._bus.subscribe = lambda pattern: self._queue
        self._bus.unsubscribe = lambda pattern, q: None
        return self

    def __exit__(self, *exc):
        self._bus.subscribe = self._orig_sub
        self._bus.unsubscribe = self._orig_unsub
        return False


def patch_subscribe(bus, queue):
    return _patch_subscribe(bus, queue)
