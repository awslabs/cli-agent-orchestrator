"""The server reports a same-generation forward jump as lost bytes (#745).

The StreamFrame handler advanced the consumed watermark to ``pos + len`` with no
check that ``pos`` was where the last frame ended. A same-generation jump — the
shape the reconnect gate loss produced on the wire — moved the watermark over a
hole with no trace. The server now reports ``[recorded, frame.pos)`` as a loss to
output consumers before processing the frame, with the exclusions that keep it
from firing on legitimate discontinuities.
"""

import base64

import pytest

from cli_agent_orchestrator.runtime_channel.protocol import (
    PROTOCOL_VERSION,
    AckFrame,
    CommandOutcome,
    CommandResultFrame,
    HelloFrame,
    StreamFrame,
    StreamName,
    decode_frame,
    encode_frame,
)
from cli_agent_orchestrator.runtime_channel.registry import RuntimeChannelRegistry

TID = "abcd1234"
CAP = StreamName.CAPTURE.value


class TestTheRegistryDetectsAForwardJump:
    def test_a_same_generation_forward_jump_is_reported(self):
        reg = RuntimeChannelRegistry()
        reg.record_position(TID, CAP, 4, generation=0)
        assert reg.stream_forward_jump(TID, CAP, 0, 8) == (4, 8)

    def test_the_first_frame_with_no_recorded_position_is_not_a_jump(self):
        reg = RuntimeChannelRegistry()
        # A generation is established at hello, but no byte position yet.
        reg.record_position(TID, CAP, 0, generation=0)
        assert reg.stream_forward_jump(TID, CAP, 0, 8) is None

    def test_a_higher_generation_is_not_a_jump(self):
        reg = RuntimeChannelRegistry()
        reg.record_position(TID, CAP, 4, generation=0)
        assert reg.stream_forward_jump(TID, CAP, 1, 8) is None

    def test_a_position_at_or_behind_the_watermark_is_not_a_jump(self):
        reg = RuntimeChannelRegistry()
        reg.record_position(TID, CAP, 4, generation=0)
        assert reg.stream_forward_jump(TID, CAP, 0, 4) is None
        assert reg.stream_forward_jump(TID, CAP, 0, 2) is None

    def test_an_unbounded_gap_suppresses_the_jump_until_the_generation_advances(self):
        reg = RuntimeChannelRegistry()
        reg.record_position(TID, CAP, 4, generation=0)
        reg.note_unbounded_gap(TID, CAP, 0)
        # The runtime said it cannot bound the loss, so a higher same-generation
        # position is the expected discontinuity, not a jump.
        assert reg.stream_forward_jump(TID, CAP, 0, 8) is None
        # Once the generation advances the suppression is cleared.
        reg.record_position(TID, CAP, 8, generation=1)
        assert reg.stream_forward_jump(TID, CAP, 1, 20) == (8, 20)


@pytest.fixture()
def channel_client(monkeypatch):
    monkeypatch.setenv("CAO_RUNTIME_TOKEN", "test-runtime-token")
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api.main import app

    return TestClient(app, base_url="http://localhost")


def _hello(runtime_id="worker-1"):
    return encode_frame(HelloFrame(protocol_version=PROTOCOL_VERSION, runtime_id=runtime_id))


class TestTheServerRepublishesTheJumpAsALoss:
    HEADERS = {"Host": "localhost", "X-CAO-Runtime-Token": "test-runtime-token"}

    @pytest.fixture(autouse=True)
    def _clean(self):
        from cli_agent_orchestrator.runtime_channel.registry import runtime_registry

        runtime_registry.unbind_terminal(TID)
        yield
        runtime_registry.unbind_terminal(TID)

    def test_a_same_generation_jump_becomes_a_gap_marker(self, channel_client):
        from cli_agent_orchestrator.services.event_bus import bus

        received = []
        original_publish = bus.publish
        original_deliver = bus.deliver_with_loss_markers
        bus.publish = lambda topic, data: received.append((topic, data))
        bus.deliver_with_loss_markers = lambda topic, data, *, lost=None: (
            received.append((topic, data)),
            0,
        )[1]
        try:
            with channel_client.websocket_connect("/runtime/channel", headers=self.HEADERS) as ws:
                ws.send_text(_hello())
                decode_frame(ws.receive_text())
                # First frame establishes the watermark at 4.
                ws.send_text(
                    encode_frame(
                        StreamFrame(
                            terminal_id=TID,
                            stream=StreamName.CAPTURE,
                            generation=0,
                            pos=0,
                            data=base64.b64encode(b"0123").decode(),
                        )
                    )
                )
                # A same-generation jump to pos 10 leaves [4,10) unaccounted for.
                ws.send_text(
                    encode_frame(
                        StreamFrame(
                            terminal_id=TID,
                            stream=StreamName.CAPTURE,
                            generation=0,
                            pos=10,
                            data=base64.b64encode(b"XYZ").decode(),
                        )
                    )
                )
                ws.send_text(
                    encode_frame(
                        CommandResultFrame(op_id="sync", terminal_id=TID, outcome=CommandOutcome.OK)
                    )
                )
                assert decode_frame(ws.receive_text()).op_id == "sync"
        finally:
            bus.publish = original_publish
            bus.deliver_with_loss_markers = original_deliver

        markers = [d for t, d in received if t == f"terminal.{TID}.output" and "gap" in d]
        assert {"from_pos": 4, "to_pos": 10} in [m["gap"] for m in markers]

    def test_an_attach_stream_jump_is_not_reported_as_a_capture_loss(self, channel_client):
        """The attach stream restarts its positions per attach; the detector is
        scoped to CAPTURE and must not fire for it."""
        from cli_agent_orchestrator.services.event_bus import bus

        received = []
        original_publish = bus.publish
        original_deliver = bus.deliver_with_loss_markers
        bus.publish = lambda topic, data: received.append((topic, data))
        bus.deliver_with_loss_markers = lambda topic, data, *, lost=None: (
            received.append((topic, data)),
            0,
        )[1]
        try:
            with channel_client.websocket_connect("/runtime/channel", headers=self.HEADERS) as ws:
                ws.send_text(_hello())
                decode_frame(ws.receive_text())
                ws.send_text(
                    encode_frame(
                        StreamFrame(
                            terminal_id=TID,
                            stream=StreamName.ATTACH,
                            generation=0,
                            pos=500,
                            data=base64.b64encode(b"attach bytes").decode(),
                        )
                    )
                )
                ws.send_text(
                    encode_frame(
                        CommandResultFrame(op_id="sync", terminal_id=TID, outcome=CommandOutcome.OK)
                    )
                )
                assert decode_frame(ws.receive_text()).op_id == "sync"
        finally:
            bus.publish = original_publish
            bus.deliver_with_loss_markers = original_deliver

        assert [d for t, d in received if t == f"terminal.{TID}.output"] == []
