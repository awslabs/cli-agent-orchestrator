"""Status survives a server restart, because the hello says what it is (#745).

Found in live validation on EKS, not by a test. The sequence: an agent finished
a task on a bridge runtime, `cao-server-0` was deleted and recreated, routing
was correctly rebuilt from the hello snapshot — and `GET /terminals/{id}` then
answered `status: "unknown"`. Nothing was broken; status is only ever *pushed*
on change, so the restarted server had never seen a frame for a terminal that
had stopped moving. For an idle agent that is not "unknown until the next
poll", it is unknown indefinitely.

The runtime knew the answer the whole time. So the hello snapshot — already the
mechanism by which terminal→runtime routing is recovered without persisting
channel state — now carries status too, and the server seeds its cache from it.

The deliberate non-behaviours, each with a test below: a runtime makes no claim
rather than claiming UNKNOWN; the server accepts a status only for a terminal
that same hello bound to that same runtime (a hello cannot speak for another
runtime's terminals); and a disconnected runtime still reports UNKNOWN, because
seeding a cache must not resurrect a stale verdict as current.
"""

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.runtime_channel.bridge import Bridge
from cli_agent_orchestrator.runtime_channel.protocol import (
    PROTOCOL_VERSION,
    HelloFrame,
    StreamName,
    StreamPosition,
    decode_frame,
    encode_frame,
)
from cli_agent_orchestrator.runtime_channel.registry import RuntimeChannelRegistry

TID = "abcd1234"
OTHER = "beef5678"


@pytest.fixture()
def channel_client(monkeypatch):
    """TestClient wired to the real app with a runtime token configured."""
    monkeypatch.setenv("CAO_RUNTIME_TOKEN", "test-runtime-token")
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api.main import app

    # base_url host must be in ALLOWED_HOSTS: TrustedHostMiddleware runs on the
    # WebSocket scope too. No lifespan — only the WS endpoint and registry here.
    return TestClient(app, base_url="http://localhost")


@pytest.fixture(autouse=True)
def clean_registry():
    """The endpoint uses the process-wide registry; don't leak bindings."""
    from cli_agent_orchestrator.runtime_channel.registry import runtime_registry

    for terminal_id in (TID, OTHER):
        runtime_registry.unbind_terminal(terminal_id)
    yield
    for terminal_id in (TID, OTHER):
        runtime_registry.unbind_terminal(terminal_id)


def _position(terminal_id, end_pos=10):
    return StreamPosition(
        terminal_id=terminal_id,
        stream=StreamName.CAPTURE,
        generation=0,
        end_pos=end_pos,
    )


class TestHelloCarriesStatus:
    """Bridge side: the runtime states what it knows."""

    @pytest.mark.asyncio
    async def test_bridge_reports_status_for_each_live_terminal(self, monkeypatch):
        bridge = Bridge("ws://server/runtime/channel", "worker-1", "tok")
        bridge._buffer_for(TID)
        bridge._buffer_for(OTHER)

        from cli_agent_orchestrator.runtime_channel import bridge as bridge_mod

        known = {TID: TerminalStatus.PROCESSING, OTHER: TerminalStatus.COMPLETED}
        monkeypatch.setattr(
            bridge_mod.status_monitor, "get_status", lambda tid: known[tid], raising=False
        )

        assert bridge._terminal_statuses() == known

    @pytest.mark.asyncio
    async def test_unknown_is_omitted_rather_than_asserted(self, monkeypatch):
        """Absent means "no claim". Sending UNKNOWN would be the runtime
        overwriting the server's own default with the same non-answer, and
        would make a later real report indistinguishable from a retraction."""
        bridge = Bridge("ws://server/runtime/channel", "worker-1", "tok")
        bridge._buffer_for(TID)
        bridge._buffer_for(OTHER)

        from cli_agent_orchestrator.runtime_channel import bridge as bridge_mod

        monkeypatch.setattr(
            bridge_mod.status_monitor,
            "get_status",
            lambda tid: TerminalStatus.UNKNOWN if tid == TID else TerminalStatus.COMPLETED,
            raising=False,
        )

        assert bridge._terminal_statuses() == {OTHER: TerminalStatus.COMPLETED}

    @pytest.mark.asyncio
    async def test_a_failing_status_read_does_not_fail_the_hello(self, monkeypatch):
        """Reconnecting matters more than reporting status. A monitor that
        raises must cost that terminal its status line, not the channel."""
        bridge = Bridge("ws://server/runtime/channel", "worker-1", "tok")
        bridge._buffer_for(TID)
        bridge._buffer_for(OTHER)

        from cli_agent_orchestrator.runtime_channel import bridge as bridge_mod

        def explode(tid):
            if tid == TID:
                raise RuntimeError("tmux socket gone")
            return TerminalStatus.COMPLETED

        monkeypatch.setattr(bridge_mod.status_monitor, "get_status", explode, raising=False)

        assert bridge._terminal_statuses() == {OTHER: TerminalStatus.COMPLETED}

    @pytest.mark.asyncio
    async def test_the_hello_frame_on_the_wire_carries_them(self, monkeypatch):
        """End of the bridge path: not just the helper, the frame it sends."""
        bridge = Bridge("ws://server/runtime/channel", "worker-1", "tok")
        bridge._buffer_for(TID)

        from cli_agent_orchestrator.runtime_channel import bridge as bridge_mod

        monkeypatch.setattr(
            bridge_mod.status_monitor,
            "get_status",
            lambda tid: TerminalStatus.COMPLETED,
            raising=False,
        )

        sent = []

        class _WS:
            async def send(self, raw):
                sent.append(decode_frame(raw))

            async def recv(self):
                return encode_frame(
                    HelloFrame(protocol_version=PROTOCOL_VERSION, runtime_id="server")
                )

            def __aiter__(self):
                async def gen():
                    return
                    yield

                return gen()

        await bridge._serve(_WS())

        hello = next(f for f in sent if isinstance(f, HelloFrame))
        assert hello.statuses == {TID: TerminalStatus.COMPLETED}


class TestStatusSurvivesRestart:
    """Server side: a fresh registry answers from the snapshot, not UNKNOWN."""

    def test_a_reconnect_restores_status_for_a_quiescent_terminal(self, channel_client):
        from cli_agent_orchestrator.runtime_channel.registry import runtime_registry

        with channel_client.websocket_connect(
            "/runtime/channel",
            headers={"Host": "localhost", "X-CAO-Runtime-Token": "test-runtime-token"},
        ) as ws:
            ws.send_text(
                encode_frame(
                    HelloFrame(
                        protocol_version=PROTOCOL_VERSION,
                        runtime_id="worker-status",
                        streams=[_position(TID)],
                        statuses={TID: TerminalStatus.COMPLETED},
                    )
                )
            )
            assert isinstance(decode_frame(ws.receive_text()), HelloFrame)
            # This is the live-validation failure, as an assertion: before the
            # fix this read UNKNOWN even though the runtime had just said
            # otherwise in the frame the server used to rebuild routing.
            assert runtime_registry.is_remote(TID)
            assert runtime_registry.get_status(TID) == TerminalStatus.COMPLETED

    def test_a_hello_cannot_set_status_for_another_runtimes_terminal(self, channel_client):
        """Possession of the shared token is possession of a channel, not of
        every terminal. A status for a terminal this hello did not bind is
        dropped — otherwise one runtime could report on another's work."""
        from cli_agent_orchestrator.runtime_channel.registry import runtime_registry

        async def send_text(text):
            pass

        # A live channel for the owner, so get_status returns its cached
        # verdict rather than the disconnected-runtime UNKNOWN.
        owner = runtime_registry.register("worker-owner", send_text)
        runtime_registry.bind_terminal(OTHER, "worker-owner")
        runtime_registry.set_status(OTHER, TerminalStatus.PROCESSING)

        with channel_client.websocket_connect(
            "/runtime/channel",
            headers={"Host": "localhost", "X-CAO-Runtime-Token": "test-runtime-token"},
        ) as ws:
            ws.send_text(
                encode_frame(
                    HelloFrame(
                        protocol_version=PROTOCOL_VERSION,
                        runtime_id="worker-liar",
                        streams=[_position(TID)],
                        statuses={
                            TID: TerminalStatus.COMPLETED,
                            OTHER: TerminalStatus.COMPLETED,
                        },
                    )
                )
            )
            assert isinstance(decode_frame(ws.receive_text()), HelloFrame)
            assert runtime_registry.get_status(TID) == TerminalStatus.COMPLETED
            # Still owned by, and still reported as, the other runtime's: the
            # foreign "COMPLETED" did not overwrite the owner's PROCESSING.
            assert runtime_registry.runtime_for_terminal(OTHER) == "worker-owner"
            assert runtime_registry.get_status(OTHER) == TerminalStatus.PROCESSING

        runtime_registry.unregister("worker-owner", owner)

    def test_seeding_does_not_make_a_dead_runtime_look_alive(self):
        """The UNKNOWN-on-disconnect posture outranks a seeded value: a cache
        entry is a report from a live channel, never a substitute for one."""
        registry = RuntimeChannelRegistry()

        async def send_text(text):
            pass

        conn = registry.register("worker-1", send_text)
        registry.bind_terminal(TID, "worker-1")
        registry.set_status(TID, TerminalStatus.COMPLETED)
        assert registry.get_status(TID) == TerminalStatus.COMPLETED

        registry.unregister("worker-1", conn)
        assert registry.get_status(TID) == TerminalStatus.UNKNOWN

    def test_a_hello_without_statuses_is_still_valid(self, channel_client):
        """Compatibility with a runtime that reports nothing: routing is
        rebuilt exactly as before and status falls back to UNKNOWN."""
        from cli_agent_orchestrator.runtime_channel.registry import runtime_registry

        with channel_client.websocket_connect(
            "/runtime/channel",
            headers={"Host": "localhost", "X-CAO-Runtime-Token": "test-runtime-token"},
        ) as ws:
            ws.send_text(
                encode_frame(
                    HelloFrame(
                        protocol_version=PROTOCOL_VERSION,
                        runtime_id="worker-quiet",
                        streams=[_position(TID)],
                    )
                )
            )
            assert isinstance(decode_frame(ws.receive_text()), HelloFrame)
            assert runtime_registry.is_remote(TID)
            assert runtime_registry.get_status(TID) == TerminalStatus.UNKNOWN
