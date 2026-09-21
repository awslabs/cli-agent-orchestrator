"""Output that the bus dropped is reported, not hidden (#745, review finding 3).

``ReplayBuffer``'s whole purpose is that a position is assigned "before any
queue, socket or reconnect can lose the chunk", so a later loss is *reportable*.
The bridge broke that by numbering chunks on the far side of the event bus,
which is bounded and drops on a full queue: whatever the bus threw away, the
positions it produced were still contiguous. The server saw a shortened
transcript with a watermark claiming it was whole, every hello and heartbeat
re-asserted that claim, and a GapFrame was impossible by construction.

The producer now stamps each output event with its offset in the terminal's byte
stream, so a missing event is arithmetic on the consuming side.
"""

import asyncio
import base64
import os
import threading
import time
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.runtime_channel.bridge import Bridge
from cli_agent_orchestrator.runtime_channel.protocol import GapFrame, StreamFrame
from cli_agent_orchestrator.runtime_channel.replay_buffer import ReplayBuffer
from cli_agent_orchestrator.services.fifo_reader import FifoManager

TID = "aaaa1111"


class TestProducerAssignedPositions:
    def test_a_contiguous_chunk_is_an_ordinary_append(self):
        buf = ReplayBuffer(max_bytes=1024)
        gap, pos = buf.append_at(0, b"abc")
        assert (gap, pos) == (None, 0)
        gap, pos = buf.append_at(3, b"def")
        assert (gap, pos) == (None, 3)
        assert buf.end_pos == 6

    def test_a_missing_chunk_is_a_gap_with_its_extent(self):
        buf = ReplayBuffer(max_bytes=1024)
        buf.append_at(0, b"abc")
        gap, pos = buf.append_at(103, b"xyz")
        assert gap is not None
        assert (gap.from_pos, gap.to_pos) == (3, 103)
        assert pos == 103
        assert buf.end_pos == 106

    def test_the_lost_bytes_are_not_invented(self):
        """A hole stays a hole: no padding may masquerade as terminal output."""
        buf = ReplayBuffer(max_bytes=1024)
        buf.append_at(0, b"abc")
        buf.append_at(103, b"xyz")
        _, chunks = buf.replay_from(0)
        assert [(p, c) for p, c in chunks] == [(0, b"abc"), (103, b"xyz")]

    def test_a_watermark_that_counts_the_loss_is_the_point(self):
        """Contrast with arrival-order numbering, which is what the bug was."""
        producer = ReplayBuffer(max_bytes=1024)
        consumer = ReplayBuffer(max_bytes=1024)
        offsets = []
        for chunk in (b"one", b"two-dropped", b"three"):
            offsets.append(producer.append(chunk))
        for offset, chunk in zip(offsets, (b"one", None, b"three")):
            if chunk is not None:
                consumer.append_at(offset, chunk)
        assert consumer.end_pos == producer.end_pos

    def test_an_offset_behind_the_watermark_appends_contiguously(self):
        """A producer that restarted its count must not rewrite the stream."""
        buf = ReplayBuffer(max_bytes=1024)
        buf.append_at(0, b"abcdef")
        gap, pos = buf.append_at(0, b"ghi")
        assert gap is None
        assert pos == 6, "the stale offset is ignored, and the caller can see that"
        assert buf.end_pos == 9

    def test_an_empty_chunk_after_a_gap_still_advances_the_watermark(self):
        buf = ReplayBuffer(max_bytes=1024)
        buf.append_at(0, b"abc")
        gap, _ = buf.append_at(50, b"")
        assert gap is not None and (gap.from_pos, gap.to_pos) == (3, 50)
        assert buf.end_pos == 50


class TestTheProducerStampsTheOffset:
    def test_offsets_are_cumulative_encoded_bytes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli_agent_orchestrator.services.fifo_reader.FIFO_DIR", tmp_path)
        manager = FifoManager()
        published = []
        with patch(
            "cli_agent_orchestrator.services.fifo_reader.bus.publish",
            side_effect=lambda topic, payload: published.append(payload),
        ):
            manager._publish_output(TID, "abc")
            manager._publish_output(TID, "dé")  # two chars, three UTF-8 bytes
            manager._publish_output(TID, "f")

        assert [p["offset"] for p in published] == [0, 3, 6]
        assert [p["data"] for p in published] == ["abc", "dé", "f"]

    def test_streams_are_counted_per_terminal(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli_agent_orchestrator.services.fifo_reader.FIFO_DIR", tmp_path)
        manager = FifoManager()
        published = []
        with patch(
            "cli_agent_orchestrator.services.fifo_reader.bus.publish",
            side_effect=lambda topic, payload: published.append((topic, payload["offset"])),
        ):
            manager._publish_output(TID, "abcd")
            manager._publish_output("bbbb2222", "x")
            manager._publish_output(TID, "e")

        assert published == [
            (f"terminal.{TID}.output", 0),
            ("terminal.bbbb2222.output", 0),
            (f"terminal.{TID}.output", 4),
        ]

    def test_the_rearm_replay_shares_the_counter(self, tmp_path, monkeypatch):
        """A re-armed pipe's replay is published bytes like any other.

        Publishing it around the counter would leave the producer's offsets and a
        remote replay buffer's watermark permanently disagreeing, and every
        subsequent chunk would be read as a backward jump.
        """
        monkeypatch.setattr("cli_agent_orchestrator.services.fifo_reader.FIFO_DIR", tmp_path)
        manager = FifoManager()
        manager._pane_probe[TID] = lambda: "pane"
        manager._rearm[TID] = lambda: None
        published = []
        with patch(
            "cli_agent_orchestrator.services.fifo_reader.bus.publish",
            side_effect=lambda topic, payload: published.append(payload),
        ):
            manager._publish_output(TID, "abcd")
            manager._rearm_stalled_pipe(TID, "pane\nlines", lambda: None, cold_start=False)
            manager._publish_output(TID, "z")

        assert [p["offset"] for p in published] == [0, 4, 4 + len(published[1]["data"])]

    def test_a_real_reader_thread_publishes_increasing_offsets(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli_agent_orchestrator.services.fifo_reader.FIFO_DIR", tmp_path)
        fifo_path = tmp_path / "term-offsets.fifo"
        os.mkfifo(fifo_path)
        manager = FifoManager()
        stop_flag = threading.Event()
        published = []
        with patch(
            "cli_agent_orchestrator.services.fifo_reader.bus.publish",
            side_effect=lambda topic, payload: published.append(payload),
        ):
            reader = threading.Thread(
                target=manager._reader_loop,
                args=("term-offsets", fifo_path, stop_flag),
                daemon=True,
            )
            reader.start()
            time.sleep(0.1)
            wfd = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
            try:
                for _ in range(3):
                    os.write(wfd, b"hello")
                    time.sleep(0.15)  # longer than the coalescing window
            finally:
                os.close(wfd)
            time.sleep(0.2)
            stop_flag.set()
            reader.join(timeout=2.0)

        assert len(published) >= 2, published
        running = 0
        for payload in published:
            assert payload["offset"] == running
            running += len(payload["data"].encode())

    def test_teardown_forgets_the_counter(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli_agent_orchestrator.services.fifo_reader.FIFO_DIR", tmp_path)
        manager = FifoManager()
        with patch("cli_agent_orchestrator.services.fifo_reader.bus.publish"):
            manager._publish_output(TID, "abcd")
        manager.stop_reader(TID)
        assert TID not in manager._published_bytes


class TestTheBridgeReportsWhatTheBusDropped:
    """The two halves joined: a skipped event becomes a GapFrame on the wire."""

    @pytest.fixture()
    def wired(self):
        bridge = Bridge("ws://unused", "worker-x", "tok")
        sent = []

        async def _send(frame):
            sent.append(frame)

        bridge._send = _send
        return bridge, sent

    async def _forward(self, bridge, events):
        """Feed events straight into the forwarder's queue.

        A dropped event is, from the subscriber's side, an event that simply
        never arrives — so the drop is modelled by not delivering it, which is
        exactly what ``EventBus._dispatch`` does on a full queue.
        """
        from cli_agent_orchestrator.services.event_bus import bus

        queue: asyncio.Queue = asyncio.Queue()
        with patch.object(bus, "subscribe", return_value=queue), patch.object(bus, "unsubscribe"):
            task = asyncio.ensure_future(bridge._forward_output())
            for event in events:
                queue.put_nowait(event)
            for _ in range(200):
                if queue.empty():
                    break
                await asyncio.sleep(0.005)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @staticmethod
    def _event(text, offset):
        return {"topic": f"terminal.{TID}.output", "data": {"data": text, "offset": offset}}

    @pytest.mark.asyncio
    async def test_a_dropped_event_produces_a_gap_then_the_bytes(self, wired):
        bridge, sent = wired
        await self._forward(
            bridge,
            [self._event("one", 0), self._event("three", 3 + len("two-dropped"))],
        )

        gaps = [f for f in sent if isinstance(f, GapFrame)]
        streams = [f for f in sent if isinstance(f, StreamFrame)]
        assert len(gaps) == 1
        assert (gaps[0].from_pos, gaps[0].to_pos) == (3, 14)
        assert gaps[0].terminal_id == TID
        assert [f.pos for f in streams] == [0, 14]
        assert base64.b64decode(streams[-1].data) == b"three"
        # The gap is reported BEFORE the bytes that follow it, so a consumer
        # never renders them as if they continued the previous line.
        assert sent.index(gaps[0]) < sent.index(streams[-1])

    @pytest.mark.asyncio
    async def test_the_advertised_watermark_counts_the_lost_bytes(self, wired):
        """What made the old behaviour silent: the hello/heartbeat snapshot.

        With arrival-order numbering the watermark was the number of bytes that
        happened to arrive, and a server resuming from it asked for exactly the
        position the runtime would next produce — no gap, no replay, and a
        transcript missing a chunk in the middle with nothing to show for it.
        """
        bridge, _ = wired
        await self._forward(bridge, [self._event("one", 0), self._event("three", 14)])
        positions = bridge._stream_positions()
        assert [p.end_pos for p in positions] == [19]

    @pytest.mark.asyncio
    async def test_an_unbroken_stream_reports_no_gap(self, wired):
        bridge, sent = wired
        await self._forward(
            bridge, [self._event("one", 0), self._event("two", 3), self._event("three", 6)]
        )
        assert [f for f in sent if isinstance(f, GapFrame)] == []
        assert [f.pos for f in sent] == [0, 3, 6]

    @pytest.mark.asyncio
    async def test_an_offsetless_publisher_keeps_working(self, wired):
        """Only the FIFO reader stamps offsets; anything else must still stream."""
        bridge, sent = wired
        await self._forward(
            bridge,
            [
                {"topic": f"terminal.{TID}.output", "data": {"data": "one"}},
                {"topic": f"terminal.{TID}.output", "data": {"data": "two"}},
            ],
        )
        assert [f for f in sent if isinstance(f, GapFrame)] == []
        assert [f.pos for f in sent] == [0, 3]
