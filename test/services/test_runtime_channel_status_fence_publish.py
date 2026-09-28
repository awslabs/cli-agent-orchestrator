"""Only a status the registry accepted is published to the bus (#745, #6).

``set_status`` fences a stale-generation report, but the EventFrame handler
published ``frame.status`` to the bus regardless, so the polling cache and the
event consumers disagreed — a rejected WAITING reached ``ApprovalBridge`` while
the cache still read the live PROCESSING. ``set_status`` now returns whether it
applied the report, and the handler publishes only then.
"""

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.runtime_channel.protocol import (
    PROTOCOL_VERSION,
    CommandOutcome,
    CommandResultFrame,
    EventFrame,
    EventType,
    HelloFrame,
    StreamName,
    StreamPosition,
    decode_frame,
    encode_frame,
)
from cli_agent_orchestrator.runtime_channel.registry import RuntimeChannelRegistry

TID = "abcd1234"
CAP = StreamName.CAPTURE.value


class TestSetStatusReportsAcceptance:
    def test_it_returns_true_when_applied(self):
        reg = RuntimeChannelRegistry()

        async def send_text(_):
            return None

        conn = reg.register("worker-1", send_text)
        reg.bind_terminal(TID, "worker-1")
        assert reg.set_status(TID, TerminalStatus.PROCESSING, conn=conn, generation=7) is True
        assert reg.get_status(TID) == TerminalStatus.PROCESSING

    def test_it_returns_false_and_changes_nothing_for_a_stale_generation(self):
        reg = RuntimeChannelRegistry()

        async def send_text(_):
            return None

        conn = reg.register("worker-1", send_text)
        reg.bind_terminal(TID, "worker-1")
        reg.set_status(TID, TerminalStatus.PROCESSING, conn=conn, generation=7)
        assert (
            reg.set_status(TID, TerminalStatus.WAITING_USER_ANSWER, conn=conn, generation=6)
            is False
        )
        assert reg.get_status(TID) == TerminalStatus.PROCESSING


@pytest.fixture()
def channel_client(monkeypatch):
    monkeypatch.setenv("CAO_RUNTIME_TOKEN", "test-runtime-token")
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api.main import app

    return TestClient(app, base_url="http://localhost")


def _hello_with(runtime_id="worker-1"):
    return encode_frame(
        HelloFrame(
            protocol_version=PROTOCOL_VERSION,
            runtime_id=runtime_id,
            streams=[
                StreamPosition(terminal_id=TID, stream=StreamName.CAPTURE, generation=0, end_pos=0)
            ],
        )
    )


class TestAFencedStatusIsNotPublished:
    HEADERS = {"Host": "localhost", "X-CAO-Runtime-Token": "test-runtime-token"}

    @pytest.fixture(autouse=True)
    def _clean(self):
        from cli_agent_orchestrator.runtime_channel.registry import runtime_registry

        runtime_registry.unbind_terminal(TID)
        yield
        runtime_registry.unbind_terminal(TID)

    def test_a_stale_status_is_neither_applied_nor_published(self, channel_client):
        from cli_agent_orchestrator.runtime_channel.registry import runtime_registry
        from cli_agent_orchestrator.services.event_bus import bus

        published = []
        original = bus.publish
        bus.publish = lambda topic, data: published.append((topic, data))
        try:
            with channel_client.websocket_connect("/runtime/channel", headers=self.HEADERS) as ws:
                ws.send_text(_hello_with())
                decode_frame(ws.receive_text())
                ws.send_text(
                    encode_frame(
                        EventFrame(
                            terminal_id=TID,
                            generation=7,
                            type=EventType.STATUS,
                            status=TerminalStatus.PROCESSING,
                        )
                    )
                )
                # A delayed report from an older generation.
                ws.send_text(
                    encode_frame(
                        EventFrame(
                            terminal_id=TID,
                            generation=6,
                            type=EventType.STATUS,
                            status=TerminalStatus.WAITING_USER_ANSWER,
                        )
                    )
                )
                ws.send_text(
                    encode_frame(
                        CommandResultFrame(op_id="sync", terminal_id=TID, outcome=CommandOutcome.OK)
                    )
                )
                assert decode_frame(ws.receive_text()).op_id == "sync"
                assert runtime_registry.get_status(TID) == TerminalStatus.PROCESSING
        finally:
            bus.publish = original

        status_events = [d for t, d in published if t == f"terminal.{TID}.status"]
        assert status_events == [{"status": "processing"}], status_events
