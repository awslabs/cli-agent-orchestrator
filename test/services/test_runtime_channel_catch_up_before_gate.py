"""The reconnect catch-up finishes before the send gate opens (#745).

While ``_ready`` is shut, ``_forward_output`` keeps appending to the replay
buffer, ``_forward_status`` learns of a status change, and a finishing command
adds a result to ``_unacked`` -- but ``_send`` drops every one of those frames,
and the one-shot resume replay ran before they existed. The bytes were stuck in
the buffer, the status was retained nowhere, and the result waited for the next
reconnect. This drives ``_serve`` against a websocket that can be paused mid
replay so those three updates can be produced while the gate is still shut, and
asserts the catch-up delivers all of them before ``_ready`` is set.
"""

import asyncio
import base64

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.runtime_channel.bridge import Bridge
from cli_agent_orchestrator.runtime_channel.protocol import (
    PROTOCOL_VERSION,
    CommandOutcome,
    CommandResultFrame,
    EventFrame,
    EventType,
    GapFrame,
    HelloFrame,
    StreamFrame,
    StreamName,
    StreamPosition,
    decode_frame,
    encode_frame,
)

TID = "abcd1234"


class _PausableWS:
    """Blocks its first CAPTURE StreamFrame send so updates can be produced
    while ``_serve`` is still gated (``_ready`` not yet set)."""

    def __init__(self, server_hello):
        self.sent = []
        self._server_hello = server_hello
        self.pause = asyncio.Event()
        self.paused_once = asyncio.Event()
        self._armed = False

    async def send(self, raw):
        frame = decode_frame(raw)
        self.sent.append(frame)
        if isinstance(frame, StreamFrame) and not self._armed:
            self._armed = True
            self.paused_once.set()
            await self.pause.wait()

    async def recv(self):
        return encode_frame(self._server_hello)

    def __aiter__(self):
        async def gen():
            if False:
                yield None

        return gen()

    def frames_of(self, cls):
        return [f for f in self.sent if isinstance(f, cls)]


def _bridge():
    return Bridge("ws://server/runtime/channel", "worker-1", "tok")


def _hello_resuming_from_zero():
    return HelloFrame(
        protocol_version=PROTOCOL_VERSION,
        runtime_id="server",
        resume=[
            StreamPosition(
                terminal_id=TID,
                stream=StreamName.CAPTURE,
                generation=0,
                end_pos=0,  # server behind: forces a replay of [0,4)
            )
        ],
    )


@pytest.mark.asyncio
async def test_output_status_and_result_during_replay_are_caught_up_before_ready():
    bridge = _bridge()
    buf = bridge._buffer_for(TID)
    buf.append(b"AAAA")  # end_pos == 4
    ws = _PausableWS(_hello_resuming_from_zero())

    serve = asyncio.ensure_future(bridge._serve(ws))
    await asyncio.wait_for(ws.paused_once.wait(), timeout=5)
    assert not bridge._ready.is_set(), "gate must still be shut mid-replay"

    # Updates produced DURING the gate, exactly as the live tasks do.
    buf.append_at(4, b"BBBB")  # _forward_output appends after the snapshot
    bridge._pending_status[TID] = TerminalStatus.COMPLETED  # _forward_status retains
    result = CommandResultFrame(
        op_id="op-finish", terminal_id=TID, outcome=CommandOutcome.OK, payload={"done": True}
    )
    bridge._unacked["op-finish"] = result  # _handle_command retains after redelivery

    ws.pause.set()
    await asyncio.wait_for(serve, timeout=5)
    assert bridge._ready.is_set()

    # The catch-up delivered all three before opening the gate.
    caps = [f for f in ws.frames_of(StreamFrame) if f.stream == StreamName.CAPTURE]
    bbbb = [f for f in caps if base64.b64decode(f.data) == b"BBBB"]
    assert len(bbbb) == 1, "output appended during the gate was never caught up"
    # Contiguous with the [0,4) replay: no hole, no duplicate.
    assert bbbb[0].pos == 4 and bbbb[0].generation == 0
    assert [base64.b64decode(f.data) for f in caps] == [b"AAAA", b"BBBB"]

    statuses = [f for f in ws.frames_of(EventFrame) if f.type == EventType.STATUS]
    assert [s.status for s in statuses] == [TerminalStatus.COMPLETED]

    results = [f for f in ws.frames_of(CommandResultFrame) if f.op_id == "op-finish"]
    assert len(results) == 1


@pytest.mark.asyncio
async def test_a_generation_change_during_the_gate_gaps_the_old_generation():
    bridge = _bridge()
    buf = bridge._buffer_for(TID)
    buf.append(b"AAAA")  # gen 0, end 4
    ws = _PausableWS(_hello_resuming_from_zero())

    serve = asyncio.ensure_future(bridge._serve(ws))
    await asyncio.wait_for(ws.paused_once.wait(), timeout=5)

    # Old generation grows past what the replay sent, then the reader re-arms.
    buf.append_at(4, b"CCCC")  # gen 0 end 8; only [0,4) was sent
    buf.begin_generation()  # gen 1, old generation's end was 8
    buf.append_at(0, b"BB")  # new stream numbered from 0

    ws.pause.set()
    await asyncio.wait_for(serve, timeout=5)

    gaps = [f for f in ws.frames_of(GapFrame) if f.stream == StreamName.CAPTURE]
    assert len(gaps) == 1, f"expected one old-generation gap, got {gaps}"
    assert (gaps[0].from_pos, gaps[0].to_pos, gaps[0].generation) == (4, 8, 0)

    caps = [f for f in ws.frames_of(StreamFrame) if f.stream == StreamName.CAPTURE]
    new_gen = [f for f in caps if f.generation == 1]
    assert [f.pos for f in new_gen] == [0]
    assert base64.b64decode(new_gen[0].data) == b"BB"
    # The old-generation gap precedes the new generation's bytes.
    assert ws.sent.index(gaps[0]) < ws.sent.index(new_gen[0])


@pytest.mark.asyncio
async def test_forward_status_retains_the_latest_status_while_gated():
    """``_forward_status`` records the status even when the gate drops the send."""
    from unittest.mock import patch

    from cli_agent_orchestrator.services.event_bus import bus

    bridge = _bridge()
    assert not bridge._ready.is_set()
    queue: asyncio.Queue = asyncio.Queue()
    with patch.object(bus, "subscribe", return_value=queue), patch.object(bus, "unsubscribe"):
        task = asyncio.ensure_future(bridge._forward_status())
        queue.put_nowait(
            {"topic": f"terminal.{TID}.status", "data": {"status": TerminalStatus.PROCESSING.value}}
        )
        for _ in range(200):
            if queue.empty():
                break
            await asyncio.sleep(0.005)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert bridge._pending_status.get(TID) == TerminalStatus.PROCESSING
