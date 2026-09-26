"""Runtime channel frame contract and replay buffer (#745/#776 slice 1)."""

import base64

import pytest
from pydantic import ValidationError

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.runtime_channel import (
    AckFrame,
    CommandFrame,
    CommandOutcome,
    CommandResultFrame,
    CommandType,
    EventFrame,
    EventType,
    GapFrame,
    GapInfo,
    HeartbeatFrame,
    HelloFrame,
    ReplayBuffer,
    StreamFrame,
    StreamName,
    StreamPosition,
    decode_frame,
    encode_frame,
)

TID = "abcd1234"


class TestFrameCodec:
    def test_command_roundtrip(self):
        frame = CommandFrame(
            op_id="op-1", terminal_id=TID, type=CommandType.INPUT, payload={"keys": "hi"}
        )
        decoded = decode_frame(encode_frame(frame))
        assert isinstance(decoded, CommandFrame)
        assert decoded == frame

    def test_all_kinds_roundtrip(self):
        frames = [
            CommandResultFrame(op_id="op-1", terminal_id=TID, outcome=CommandOutcome.OK),
            AckFrame(op_id="op-1"),
            EventFrame(
                terminal_id=TID,
                generation=2,
                type=EventType.STATUS,
                status=TerminalStatus.COMPLETED,
            ),
            StreamFrame(
                terminal_id=TID,
                stream=StreamName.CAPTURE,
                generation=1,
                pos=100,
                data=base64.b64encode(b"hello").decode(),
            ),
            GapFrame(
                terminal_id=TID, stream=StreamName.CAPTURE, generation=1, from_pos=5, to_pos=10
            ),
            HelloFrame(
                protocol_version=1,
                runtime_id="worker-1",
                streams=[
                    StreamPosition(
                        terminal_id=TID, stream=StreamName.ATTACH, generation=0, end_pos=0
                    )
                ],
            ),
            HeartbeatFrame(streams=[]),
        ]
        for frame in frames:
            assert decode_frame(encode_frame(frame)) == frame

    def test_stream_end_pos(self):
        frame = StreamFrame(
            terminal_id=TID,
            stream=StreamName.CAPTURE,
            generation=0,
            pos=10,
            data=base64.b64encode(b"12345").decode(),
        )
        assert frame.end_pos == 15

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValidationError):
            decode_frame('{"kind": "totally-new", "op_id": "x"}')

    def test_extra_fields_rejected(self):
        with pytest.raises(ValidationError):
            decode_frame('{"kind": "ack", "op_id": "x", "smuggled": true}')

    def test_unbounded_gap_allowed(self):
        # to_pos=None means the runtime cannot bound the loss (lost generation).
        frame = GapFrame(terminal_id=TID, stream=StreamName.ATTACH, generation=3, from_pos=7)
        decoded = decode_frame(encode_frame(frame))
        assert isinstance(decoded, GapFrame)
        assert decoded.to_pos is None

    def test_invalid_terminal_id_rejected(self):
        with pytest.raises(ValidationError):
            CommandFrame(op_id="op", terminal_id="not-hex!", type=CommandType.LAUNCH)


class TestReplayBuffer:
    def test_positions_are_monotonic_and_content_preserved(self):
        buf = ReplayBuffer(max_bytes=100)
        assert buf.append(b"abc") == 0
        assert buf.append(b"defg") == 3
        assert buf.end_pos == 7
        items = buf.replay_from(0)
        assert not any(isinstance(i, GapInfo) for i in items)
        assert b"".join(c for _, c in items) == b"abcdefg"

    def test_replay_from_mid_chunk_trims(self):
        buf = ReplayBuffer(max_bytes=100)
        buf.append(b"abcdef")
        assert buf.replay_from(2) == [(2, b"cdef")]

    def test_replay_at_watermark_is_empty(self):
        buf = ReplayBuffer(max_bytes=100)
        buf.append(b"abc")
        assert buf.replay_from(3) == []

    def test_eviction_produces_explicit_gap(self):
        buf = ReplayBuffer(max_bytes=6)
        buf.append(b"aaa")  # [0,3)
        buf.append(b"bbb")  # [3,6)
        buf.append(b"ccc")  # [6,9) — evicts "aaa"
        assert buf.window_start == 3
        items = buf.replay_from(0)
        assert items[0] == GapInfo(from_pos=0, to_pos=3)
        assert b"".join(c for _, c in items[1:]) == b"bbbccc"

    def test_gap_bounded_to_window_start_not_resume_chunk(self):
        buf = ReplayBuffer(max_bytes=4)
        buf.append(b"aaaa")  # [0,4)
        buf.append(b"bb")  # [4,6) — evicts "aaaa"
        assert buf.replay_from(1) == [GapInfo(from_pos=1, to_pos=4), (4, b"bb")]

    def test_oversized_chunk_still_advances_watermark(self):
        buf = ReplayBuffer(max_bytes=4)
        assert buf.append(b"abcdefgh") == 0
        assert buf.end_pos == 8
        # Whole-chunk eviction dropped it; the loss is visible, not silent.
        assert buf.replay_from(0) == [GapInfo(from_pos=0, to_pos=8)]

    def test_resume_past_watermark_is_protocol_violation(self):
        buf = ReplayBuffer(max_bytes=10)
        buf.append(b"ab")
        with pytest.raises(ValueError):
            buf.replay_from(3)

    def test_empty_append_is_noop(self):
        buf = ReplayBuffer(max_bytes=10)
        assert buf.append(b"") == 0
        assert buf.end_pos == 0
        assert buf.replay_from(0) == []

    def test_generation_is_carried(self):
        assert ReplayBuffer(max_bytes=1, generation=5).generation == 5
