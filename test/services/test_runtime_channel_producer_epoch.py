"""A producer epoch, not a backward offset, drives the stream generation (#745).

The FIFO reader resets its offset to 0 when re-armed, and the bridge inferred a
restart from ``offset < end_pos`` — a heuristic a dropped head defeats: if the
new stream's first events are lost by the bounded bus, the first delivered offset
is at or past the old watermark and no generation is begun, so the two streams
splice. The producer now stamps every output event with a per-terminal epoch,
bumped whenever a reader is (re)created or stopped, and the bridge begins a new
buffer generation when the epoch changes.
"""

import asyncio
import base64
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.runtime_channel.bridge import Bridge
from cli_agent_orchestrator.runtime_channel.protocol import GapFrame, StreamFrame
from cli_agent_orchestrator.services.fifo_reader import FifoManager

TID = "aaaa1111"


class TestTheProducerStampsAnEpoch:
    def test_publish_output_carries_the_current_epoch(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli_agent_orchestrator.services.fifo_reader.FIFO_DIR", tmp_path)
        manager = FifoManager()
        published = []
        with patch(
            "cli_agent_orchestrator.services.fifo_reader.bus.publish",
            side_effect=lambda topic, payload: published.append(payload),
        ):
            manager._publish_output(TID, "abc")
        assert "epoch" in published[0]

    def test_creating_a_reader_bumps_the_epoch_and_resets_the_counter(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli_agent_orchestrator.services.fifo_reader.FIFO_DIR", tmp_path)
        manager = FifoManager()
        with patch("cli_agent_orchestrator.services.fifo_reader.bus.publish"):
            manager._publish_output(TID, "abcd")  # advances the byte counter
        before = manager._epochs.get(TID, 0)
        manager.create_reader(TID)
        try:
            assert manager._epochs[TID] == before + 1
            assert manager._published_bytes.get(TID, 0) == 0
        finally:
            manager.stop_reader(TID)

    def test_stopping_a_reader_bumps_the_epoch(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli_agent_orchestrator.services.fifo_reader.FIFO_DIR", tmp_path)
        manager = FifoManager()
        manager.create_reader(TID)
        after_create = manager._epochs[TID]
        manager.stop_reader(TID)
        assert manager._epochs[TID] == after_create + 1

    def test_a_re_created_reader_delivers_a_higher_epoch(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli_agent_orchestrator.services.fifo_reader.FIFO_DIR", tmp_path)
        manager = FifoManager()
        published = []
        with patch(
            "cli_agent_orchestrator.services.fifo_reader.bus.publish",
            side_effect=lambda topic, payload: published.append(payload["epoch"]),
        ):
            manager.create_reader(TID)
            manager._publish_output(TID, "old")
            manager.stop_reader(TID)
            manager.create_reader(TID)
            manager._publish_output(TID, "new")
            manager.stop_reader(TID)
        assert published[0] < published[1], published


class TestTheBridgeBeginsAGenerationOnAnEpochChange:
    @pytest.fixture()
    def wired(self):
        bridge = Bridge("ws://unused", "worker-x", "tok")
        sent = []

        async def _send(frame):
            sent.append(frame)

        bridge._send = _send
        return bridge, sent

    async def _forward(self, bridge, events):
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
    def _event(text, offset, epoch):
        return {
            "topic": f"terminal.{TID}.output",
            "data": {"data": text, "offset": offset, "epoch": epoch},
        }

    @pytest.mark.asyncio
    async def test_a_restart_whose_head_is_dropped_still_begins_a_generation(self, wired):
        bridge, sent = wired
        await self._forward(
            bridge,
            [
                self._event("0123456789", 0, 0),  # old stream, epoch 0
                # Re-armed: epoch 1. Its first event (offset 0) was dropped, so
                # the first DELIVERED event is at offset 10 — which the old
                # offset heuristic reads as a continuation.
                self._event("second", 10, 1),
            ],
        )
        streams = [f for f in sent if isinstance(f, StreamFrame)]
        assert [f.generation for f in streams] == [0, 1], "the epoch change begins generation 1"
        new_gen = [f for f in streams if f.generation == 1]
        assert new_gen[0].pos == 10
        assert base64.b64decode(new_gen[0].data) == b"second"
        # The new stream's dropped head is reported as a gap in the new generation.
        gaps = [f for f in sent if isinstance(f, GapFrame)]
        assert [(g.from_pos, g.to_pos, g.generation) for g in gaps] == [(0, 10, 1)]

    @pytest.mark.asyncio
    async def test_the_first_epoch_seen_is_only_recorded(self, wired):
        bridge, sent = wired
        await self._forward(bridge, [self._event("one", 0, 5), self._event("two", 3, 5)])
        streams = [f for f in sent if isinstance(f, StreamFrame)]
        assert [f.generation for f in streams] == [0, 0], "a steady epoch stays one generation"
        assert [f.pos for f in streams] == [0, 3]
