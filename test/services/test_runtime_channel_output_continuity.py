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
from cli_agent_orchestrator.runtime_channel.replay_buffer import GapInfo, ReplayBuffer
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
        items = buf.replay_from(0)
        assert [i for i in items if not isinstance(i, GapInfo)] == [(0, b"abc"), (103, b"xyz")]

    def test_a_replay_re_reports_an_interior_hole(self):
        """The hole is reported again on reconnect, in stream order.

        ``append_at`` returns the gap once, and the live path turns it into a
        GapFrame — but a chunk is dropped precisely when things are going wrong,
        and if the channel is down at that moment that frame is never sent. The
        consumer then resumes from the end of the last chunk it DID receive,
        which is inside the retained window, so a gap keyed only on
        ``window_start`` reported nothing and the higher-positioned chunk was
        accepted as if it followed contiguously (Copilot review on #802).
        """
        buf = ReplayBuffer(max_bytes=1024)
        buf.append_at(0, b"abc")
        buf.append_at(103, b"xyz")  # the live GapFrame for [3,103) never landed

        assert buf.replay_from(3) == [GapInfo(from_pos=3, to_pos=103), (103, b"xyz")]

    def test_a_gap_precedes_the_bytes_that_follow_it(self):
        """Order is load-bearing: a consumer must not advance past undelivered bytes.

        Every gap first and the bytes afterwards would let a reconnect that dies
        mid-replay leave the consumer's watermark past chunks it never received —
        the same silent loss, one layer up.
        """
        buf = ReplayBuffer(max_bytes=1024)
        buf.append_at(0, b"aa")  # [0,2)
        buf.append_at(10, b"bb")  # [10,12) — hole [2,10)
        buf.append_at(30, b"cc")  # [30,32) — hole [12,30)

        assert buf.replay_from(2) == [
            GapInfo(from_pos=2, to_pos=10),
            (10, b"bb"),
            GapInfo(from_pos=12, to_pos=30),
            (30, b"cc"),
        ]

    def test_a_mid_chunk_resume_after_a_hole_still_trims(self):
        buf = ReplayBuffer(max_bytes=1024)
        buf.append_at(0, b"abc")
        buf.append_at(10, b"defgh")

        assert buf.replay_from(11) == [(11, b"efgh")]

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
    async def test_a_restarted_producer_offset_advances_the_generation(self, wired):
        """A re-armed reader restarts its offset at 0. Without a generation
        transition the bridge appended those bytes contiguously and the server
        saw one spliced stream; the frames must instead carry a new generation
        with positions numbered from 0 (Copilot review on #802)."""
        bridge, sent = wired
        await self._forward(
            bridge,
            [
                self._event("first stream", 0),
                # The reader was stopped and re-armed: offset counter back to 0.
                self._event("second", 0),
            ],
        )

        streams = [f for f in sent if isinstance(f, StreamFrame)]
        assert [f.generation for f in streams] == [0, 1], "the restart is a new generation"
        assert [f.pos for f in streams] == [0, 0], "the new stream is numbered from 0"
        assert base64.b64decode(streams[-1].data) == b"second"
        # No gap is invented for the restart: nothing was lost, the stream ended.
        assert [f for f in sent if isinstance(f, GapFrame)] == []

    @pytest.mark.asyncio
    async def test_the_watermark_after_a_restart_describes_only_the_new_stream(self, wired):
        bridge, _ = wired
        await self._forward(bridge, [self._event("first stream", 0), self._event("second", 0)])
        positions = bridge._stream_positions()
        assert [p.end_pos for p in positions] == [len(b"second")]
        assert [p.generation for p in positions] == [1]

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


class TestAStreamRestartStartsANewGeneration:
    """A re-armed reader is a NEW stream, not a rewind of the old one.

    ``fifo_reader`` resets its producer offset to 0 when a reader is stopped and
    re-armed, but the bridge kept the same buffer at generation 0, and
    ``append_at`` treats an offset behind the watermark as bytes to append
    contiguously. That spliced a new stream onto the old one, so every resume
    position and replay afterwards described a transcript that never existed
    (Copilot review on #802). ``begin_generation`` is the transition that makes
    the restart visible.
    """

    def test_begin_generation_restarts_numbering_and_drops_the_old_window(self):
        buf = ReplayBuffer(max_bytes=1024)
        buf.append_at(0, b"old stream")
        assert buf.end_pos == 10
        assert buf.generation == 0

        assert buf.begin_generation() == 1
        assert buf.generation == 1
        # Numbering starts over, and the previous stream's bytes are not
        # replayable under the new generation.
        assert buf.end_pos == 0
        assert buf.window_start == 0
        assert buf.replay_from(0) == []

    def test_the_new_streams_bytes_are_numbered_from_zero(self):
        buf = ReplayBuffer(max_bytes=1024)
        buf.append_at(0, b"old stream")
        buf.begin_generation()

        gap, pos = buf.append_at(0, b"new")
        assert gap is None, "position 0 of a new generation is not a hole"
        assert pos == 0
        assert buf.replay_from(0) == [(0, b"new")]

    def test_without_the_transition_a_restart_would_splice(self):
        """Pins WHY the transition is needed: append_at alone appends
        contiguously, which is exactly the splice being prevented."""
        buf = ReplayBuffer(max_bytes=1024)
        buf.append_at(0, b"old stream")

        gap, pos = buf.append_at(0, b"new")  # no generation transition
        assert gap is None
        assert pos == 10, "the stale offset is ignored and the bytes are spliced"


class TestOffsetOrderMatchesPublishOrder:
    """Two threads publish here; a reordered pair now costs a generation.

    ``_publish_output`` assigned the offset under the lock and published outside
    it, so the reader thread and the watchdog's rearm replay could take offsets 0
    and N and publish N first. Since a backwards offset is a GENERATION RESTART
    on the bridge, that reordering would splice a new stream onto the old one
    although nothing restarted (Copilot review on #802).
    """

    def test_concurrent_publishers_never_emit_a_backwards_offset(self, tmp_path, monkeypatch):
        import threading

        monkeypatch.setattr("cli_agent_orchestrator.services.fifo_reader.FIFO_DIR", tmp_path)
        manager = FifoManager()
        seen: list = []
        seen_lock = threading.Lock()

        def record(topic, payload):
            # Capture publish ORDER, which is what the consumer sees.
            with seen_lock:
                seen.append(payload["offset"])

        with patch("cli_agent_orchestrator.services.fifo_reader.bus.publish", side_effect=record):

            def hammer():
                for _ in range(150):
                    manager._publish_output(TID, "abcd")

            threads = [threading.Thread(target=hammer) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert seen == sorted(seen), "a later offset was published before an earlier one"
        assert len(seen) == 600


class TestLossIsReportedToTheSubscriberThatLostIt:
    """Per-subscriber loss markers, because aggregates are wrong for somebody.

    ``publish`` is fire-and-forget, so the channel handler could not see a refused
    put at all. Making it visible was only half the problem: the bus is a shared
    bounded fanout, so holding the watermark back duplicates output for the
    subscribers that DID accept, and broadcasting a gap marker both misinforms
    those subscribers and can be dropped by the very queue that is full.

    So the marker is owed to the specific queue that refused and delivered on the
    next put it accepts (Copilot reviews on #802).
    """

    @staticmethod
    def _bus_with(*queues):
        import asyncio

        from cli_agent_orchestrator.services.event_bus import EventBus

        bus = EventBus()
        bus._loop = asyncio.new_event_loop()
        with bus._lock:
            bus._exact["terminal.t1.output"] = list(queues)
        return bus

    def test_the_refusing_queue_is_owed_a_marker_and_gets_it_when_it_drains(self):
        import asyncio

        full: asyncio.Queue = asyncio.Queue(maxsize=1)
        bus = self._bus_with(full)

        assert (
            bus.deliver_with_loss_markers(
                "terminal.t1.output", {"data": "first"}, lost={"from_pos": 0, "to_pos": 5}
            )
            == 0
        )
        # Queue is full now; this delivery is refused and a marker is owed.
        assert (
            bus.deliver_with_loss_markers(
                "terminal.t1.output", {"data": "second"}, lost={"from_pos": 5, "to_pos": 11}
            )
            == 1
        )

        full.get_nowait()  # the consumer drains one event

        # Next delivery: the owed marker arrives FIRST, then the new payload.
        bus.deliver_with_loss_markers(
            "terminal.t1.output", {"data": "third"}, lost={"from_pos": 11, "to_pos": 16}
        )
        marker = full.get_nowait()
        assert marker["data"]["gap"] == {"from_pos": 5, "to_pos": 11}
        assert marker["data"]["data"] == ""

    def test_a_healthy_subscriber_is_never_told_it_lost_anything(self):
        """The bug in broadcasting: it misinforms the subscribers that were fine."""
        import asyncio

        full: asyncio.Queue = asyncio.Queue(maxsize=1)
        healthy: asyncio.Queue = asyncio.Queue()
        bus = self._bus_with(full, healthy)

        bus.deliver_with_loss_markers(
            "terminal.t1.output", {"data": "a"}, lost={"from_pos": 0, "to_pos": 1}
        )
        bus.deliver_with_loss_markers(
            "terminal.t1.output", {"data": "b"}, lost={"from_pos": 1, "to_pos": 2}
        )

        seen = []
        while not healthy.empty():
            seen.append(healthy.get_nowait())
        assert all("gap" not in e["data"] for e in seen), f"healthy subscriber got a gap: {seen}"
        assert [e["data"]["data"] for e in seen] == ["a", "b"]

    def test_consecutive_drops_widen_one_range_rather_than_skipping_the_first(self):
        """The two-consecutive-drops bug: the earlier loss must not be forgotten."""
        import asyncio

        full: asyncio.Queue = asyncio.Queue(maxsize=1)
        bus = self._bus_with(full)

        bus.deliver_with_loss_markers(
            "terminal.t1.output", {"data": "fills it"}, lost={"from_pos": 0, "to_pos": 8}
        )
        bus.deliver_with_loss_markers(
            "terminal.t1.output", {"data": "lost-1"}, lost={"from_pos": 8, "to_pos": 14}
        )
        bus.deliver_with_loss_markers(
            "terminal.t1.output", {"data": "lost-2"}, lost={"from_pos": 14, "to_pos": 20}
        )

        full.get_nowait()
        bus.deliver_with_loss_markers(
            "terminal.t1.output", {"data": "ok"}, lost={"from_pos": 20, "to_pos": 22}
        )
        marker = full.get_nowait()
        # BOTH dropped ranges, as one range — not just the later one.
        assert marker["data"]["gap"] == {"from_pos": 8, "to_pos": 20}

    def test_the_marker_is_not_re_delivered_once_taken(self):
        """Delivered once, then forgotten — not re-sent on every later event.

        Sized so the flushed marker AND the payload both fit once the consumer
        drains; with a maxsize of 1 the marker itself fills the queue, the payload
        is then legitimately refused, and a NEW range is owed — which is correct
        behaviour, not a re-delivery, and made the first version of this test
        assert the wrong thing.
        """
        import asyncio

        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        bus = self._bus_with(q)

        # Fill it, then lose a range.
        bus.deliver_with_loss_markers(
            "terminal.t1.output", {"data": "a"}, lost={"from_pos": 0, "to_pos": 1}
        )
        bus.deliver_with_loss_markers(
            "terminal.t1.output", {"data": "b"}, lost={"from_pos": 1, "to_pos": 2}
        )
        assert (
            bus.deliver_with_loss_markers(
                "terminal.t1.output", {"data": "lost"}, lost={"from_pos": 2, "to_pos": 6}
            )
            == 1
        )

        # Drain fully, so the marker and the next payload both have room.
        while not q.empty():
            q.get_nowait()

        bus.deliver_with_loss_markers(
            "terminal.t1.output", {"data": "c"}, lost={"from_pos": 6, "to_pos": 7}
        )
        drained = []
        while not q.empty():
            drained.append(q.get_nowait())
        gaps = [e for e in drained if "gap" in e["data"]]
        assert len(gaps) == 1, f"expected exactly one marker, got {drained}"
        assert gaps[0]["data"]["gap"] == {"from_pos": 2, "to_pos": 6}

        # Nothing further is owed, so a later event carries no marker.
        bus.deliver_with_loss_markers(
            "terminal.t1.output", {"data": "d"}, lost={"from_pos": 7, "to_pos": 8}
        )
        rest = []
        while not q.empty():
            rest.append(q.get_nowait())
        assert all("gap" not in e["data"] for e in rest), f"marker re-delivered: {rest}"

    def test_unsubscribing_drops_the_owed_marker(self):
        """An owed entry must not outlive its subscriber.

        It holds a reference to the queue, so leaving it behind pins a dead
        subscriber's queue forever; and the map is keyed by ``id(queue)``, which
        CPython reuses after collection, so a stale entry could hand a future queue
        at the same address a gap it never suffered.
        """
        import asyncio

        from cli_agent_orchestrator.services.event_bus import EventBus

        bus = EventBus()
        bus._loop = asyncio.new_event_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        with bus._lock:
            bus._exact["terminal.t1.output"] = [q]

        bus.deliver_with_loss_markers(
            "terminal.t1.output", {"data": "a"}, lost={"from_pos": 0, "to_pos": 1}
        )
        assert (
            bus.deliver_with_loss_markers(
                "terminal.t1.output", {"data": "b"}, lost={"from_pos": 1, "to_pos": 2}
            )
            == 1
        )
        assert len(bus._owed_loss) == 1

        bus.unsubscribe("terminal.t1.output", q)
        assert bus._owed_loss == {}, "an owed marker outlived its subscriber"


class TestADroppedChunkStaysReplayable:
    """A subscriber that could not take the bytes must not advance the watermark.

    ``publish`` is fire-and-forget over ``call_soon_threadsafe``, so a full
    LogWriter/AG-UI/inbox queue became a log line and nothing else: the channel
    handler could not know a subscriber had refused the bytes.

    ``deliver_now`` reports the count. The handler uses it to REPORT the loss as a
    gap, not to hold the watermark back — the bus is shared, so a replay would
    re-send those bytes to the subscribers that accepted the first copy and
    duplicate their output (Copilot follow-up on #802). What these tests pin is
    the reporting primitive: the count is accurate per refusing subscriber.
    """

    def test_a_full_subscriber_queue_is_reported_not_replayed(self):
        import asyncio

        from cli_agent_orchestrator.services.event_bus import EventBus

        bus = EventBus()
        bus._loop = asyncio.new_event_loop()
        q = asyncio.Queue(maxsize=1)
        with bus._lock:
            bus._exact["terminal.t1.output"] = [q]

        assert bus.deliver_now("terminal.t1.output", {"data": "first"}) == 0
        # Queue is now full; the next delivery is refused and REPORTED.
        assert bus.deliver_now("terminal.t1.output", {"data": "second"}) == 1

    def test_delivery_to_every_subscriber_reports_zero(self):
        import asyncio

        from cli_agent_orchestrator.services.event_bus import EventBus

        bus = EventBus()
        bus._loop = asyncio.new_event_loop()
        with bus._lock:
            bus._exact["terminal.t1.output"] = [asyncio.Queue(), asyncio.Queue()]

        assert bus.deliver_now("terminal.t1.output", {"data": "x"}) == 0

    def test_each_refusing_subscriber_is_counted(self):
        """Two full queues are two drops, not one — the caller logs the number."""
        import asyncio

        from cli_agent_orchestrator.services.event_bus import EventBus

        bus = EventBus()
        bus._loop = asyncio.new_event_loop()
        full_a, full_b = asyncio.Queue(maxsize=1), asyncio.Queue(maxsize=1)
        full_a.put_nowait({"pre": "filled"})
        full_b.put_nowait({"pre": "filled"})
        with bus._lock:
            bus._exact["terminal.t1.output"] = [full_a, full_b]

        assert bus.deliver_now("terminal.t1.output", {"data": "x"}) == 2
