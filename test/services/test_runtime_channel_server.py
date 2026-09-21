"""Server-side runtime channel: auth, registration, routing, republish (#745)."""

import asyncio
import base64

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.runtime_channel.protocol import (
    PROTOCOL_VERSION,
    AckFrame,
    CommandFrame,
    CommandOutcome,
    CommandResultFrame,
    CommandType,
    EventFrame,
    EventType,
    HelloFrame,
    StreamFrame,
    StreamName,
    StreamPosition,
    decode_frame,
    encode_frame,
)
from cli_agent_orchestrator.runtime_channel.registry import (
    RuntimeChannelRegistry,
    RuntimeUnavailableError,
)

TID = "abcd1234"


class TestRuntimeRegistry:
    @pytest.mark.asyncio
    async def test_command_roundtrip(self):
        registry = RuntimeChannelRegistry()
        sent = []

        async def send_text(text):
            sent.append(decode_frame(text))

        conn = registry.register("worker-1", send_text)
        registry.bind_terminal(TID, "worker-1")

        async def respond():
            while not sent:
                await asyncio.sleep(0.01)
            cmd = sent[0]
            conn.resolve(
                CommandResultFrame(
                    op_id=cmd.op_id,
                    terminal_id=TID,
                    outcome=CommandOutcome.OK,
                    payload={"success": True},
                )
            )

        responder = asyncio.ensure_future(respond())
        result = await registry.send_terminal_command(
            TID, CommandType.INPUT, {"message": "hi"}, timeout=2
        )
        await responder
        assert result.outcome == CommandOutcome.OK
        assert sent[0].type == CommandType.INPUT
        assert sent[0].terminal_id == TID

    @pytest.mark.asyncio
    async def test_unbound_terminal_raises(self):
        registry = RuntimeChannelRegistry()
        with pytest.raises(RuntimeUnavailableError):
            await registry.send_terminal_command(TID, CommandType.INPUT, {})

    @pytest.mark.asyncio
    async def test_disconnected_runtime_raises_and_status_unknown(self):
        registry = RuntimeChannelRegistry()

        async def send_text(text):
            pass

        conn = registry.register("worker-1", send_text)
        registry.bind_terminal(TID, "worker-1")
        registry.set_status(TID, TerminalStatus.PROCESSING)
        assert registry.get_status(TID) == TerminalStatus.PROCESSING

        registry.unregister("worker-1", conn)
        with pytest.raises(RuntimeUnavailableError):
            await registry.send_terminal_command(TID, CommandType.INPUT, {})
        # No live channel: the server must report UNKNOWN, not the stale
        # last report (#745 failure posture).
        assert registry.get_status(TID) == TerminalStatus.UNKNOWN

    @pytest.mark.asyncio
    async def test_reconnect_fails_pending_of_old_channel(self):
        registry = RuntimeChannelRegistry()

        async def send_text(text):
            pass

        registry.register("worker-1", send_text)
        registry.bind_terminal(TID, "worker-1")
        pending = asyncio.ensure_future(
            registry.send_terminal_command(TID, CommandType.INPUT, {}, timeout=5)
        )
        await asyncio.sleep(0.01)
        registry.register("worker-1", send_text)  # reconnect supersedes
        with pytest.raises(RuntimeUnavailableError):
            await pending

    def test_remote_terminal_ids_spans_every_runtime(self):
        """The whole bound set, not one runtime's — what ``list_sessions`` needs.

        A session's agents can be placed on different executors, so an answer
        derived from a single runtime (or from ``list_runtimes`` without
        flattening) would list part of a session and silently drop the rest.
        """
        registry = RuntimeChannelRegistry()
        registry.bind_terminal("t-a", "worker-1")
        registry.bind_terminal("t-b", "worker-2")

        assert sorted(registry.remote_terminal_ids()) == ["t-a", "t-b"]

        registry.unbind_terminal("t-a")
        assert registry.remote_terminal_ids() == ["t-b"]

    def test_remote_terminal_ids_is_a_snapshot_not_a_view(self):
        """Callers iterate it while awaiting a DB read.

        Returning the live key view instead would raise "dictionary changed size
        during iteration" at a caller the moment a channel disconnected mid-read
        — a fault in one runtime breaking an unrelated listing.
        """
        registry = RuntimeChannelRegistry()
        registry.bind_terminal("t-a", "worker-1")

        ids = registry.remote_terminal_ids()
        for _ in ids:
            registry.bind_terminal("t-b", "worker-2")  # a channel connects mid-iteration

        assert ids == ["t-a"]

    def test_positions_monotonic(self):
        registry = RuntimeChannelRegistry()
        registry.record_position(TID, "capture", 10)
        registry.record_position(TID, "capture", 5)  # stale, ignored
        assert registry.resume_position(TID, "capture") == 10


class TestConcurrentRuntimes:
    """Two runtimes connected at once (#745 acceptance: N workers execute
    concurrently with input, output, results and cancellation routed to the
    CORRECT runtime).

    Every other test here registers a single runtime, which cannot fail the way
    a shared server actually fails: with one connection, a registry that ignored
    runtime_id entirely would still pass. These fix the routing.
    """

    T1 = "aaaa1111"
    T2 = "bbbb2222"

    @staticmethod
    def _two_runtimes():
        registry = RuntimeChannelRegistry()
        sent = {"worker-1": [], "worker-2": []}

        def sender(runtime_id):
            async def send_text(text):
                sent[runtime_id].append(decode_frame(text))

            return send_text

        conns = {r: registry.register(r, sender(r)) for r in sent}
        registry.bind_terminal(TestConcurrentRuntimes.T1, "worker-1")
        registry.bind_terminal(TestConcurrentRuntimes.T2, "worker-2")
        return registry, conns, sent

    @pytest.mark.asyncio
    async def test_input_reaches_only_the_owning_runtime(self):
        registry, conns, sent = self._two_runtimes()

        async def respond(runtime_id, terminal_id, marker):
            while not sent[runtime_id]:
                await asyncio.sleep(0.01)
            conns[runtime_id].resolve(
                CommandResultFrame(
                    op_id=sent[runtime_id][0].op_id,
                    terminal_id=terminal_id,
                    outcome=CommandOutcome.OK,
                    payload={"marker": marker},
                )
            )

        # Both in flight at once: each result must come back to its own caller.
        r1, r2, _, _ = await asyncio.gather(
            registry.send_terminal_command(self.T1, CommandType.INPUT, {"message": "one"}, 2),
            registry.send_terminal_command(self.T2, CommandType.INPUT, {"message": "two"}, 2),
            respond("worker-1", self.T1, "from-1"),
            respond("worker-2", self.T2, "from-2"),
        )

        assert r1.payload["marker"] == "from-1"
        assert r2.payload["marker"] == "from-2"
        # And neither runtime saw the other's terminal or message.
        assert [f.terminal_id for f in sent["worker-1"]] == [self.T1]
        assert [f.terminal_id for f in sent["worker-2"]] == [self.T2]
        assert sent["worker-1"][0].payload == {"message": "one"}
        assert sent["worker-2"][0].payload == {"message": "two"}

    @pytest.mark.asyncio
    async def test_cancellation_is_routed_by_terminal_not_broadcast(self):
        registry, conns, sent = self._two_runtimes()

        async def respond():
            while not sent["worker-2"]:
                await asyncio.sleep(0.01)
            conns["worker-2"].resolve(
                CommandResultFrame(
                    op_id=sent["worker-2"][0].op_id,
                    terminal_id=self.T2,
                    outcome=CommandOutcome.OK,
                    payload={},
                )
            )

        responder = asyncio.ensure_future(respond())
        await registry.send_terminal_command(self.T2, CommandType.CANCEL, {}, 2)
        await responder
        assert sent["worker-1"] == [], "a cancel for worker-2 must not reach worker-1"

    @pytest.mark.asyncio
    async def test_one_runtime_dying_does_not_disturb_the_other(self):
        registry, conns, sent = self._two_runtimes()
        registry.set_status(self.T1, TerminalStatus.PROCESSING)
        registry.set_status(self.T2, TerminalStatus.PROCESSING)

        registry.unregister("worker-1", conns["worker-1"])

        # The dead worker's terminal is explicitly unavailable and its status
        # UNKNOWN rather than the stale last report...
        with pytest.raises(RuntimeUnavailableError):
            await registry.send_terminal_command(self.T1, CommandType.INPUT, {})
        assert registry.get_status(self.T1) == TerminalStatus.UNKNOWN
        # ...while the survivor keeps both its status and its channel.
        assert registry.get_status(self.T2) == TerminalStatus.PROCESSING
        assert "worker-2" in registry.list_runtimes()
        assert "worker-1" not in registry.list_runtimes()

        async def respond():
            while not sent["worker-2"]:
                await asyncio.sleep(0.01)
            conns["worker-2"].resolve(
                CommandResultFrame(
                    op_id=sent["worker-2"][0].op_id,
                    terminal_id=self.T2,
                    outcome=CommandOutcome.OK,
                    payload={"alive": True},
                )
            )

        responder = asyncio.ensure_future(respond())
        result = await registry.send_terminal_command(self.T2, CommandType.INPUT, {}, 2)
        await responder
        assert result.payload == {"alive": True}

    def test_stream_positions_are_per_terminal(self):
        registry, _, _ = self._two_runtimes()
        registry.record_position(self.T1, "capture", 99)
        # Fencing is per terminal: one busy worker must not advance another's
        # resume point and silently skip output on reconnect.
        assert registry.resume_position(self.T2, "capture") == 0
        assert registry.resume_position(self.T1, "capture") == 99


@pytest.fixture()
def channel_client(monkeypatch, tmp_path):
    """TestClient wired to the real app with a runtime token configured."""
    monkeypatch.setenv("CAO_RUNTIME_TOKEN", "test-runtime-token")
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api.main import app

    # base_url host must be in ALLOWED_HOSTS (TrustedHostMiddleware runs on
    # the WebSocket scope too). No lifespan: these tests exercise the WS
    # endpoint and registry only.
    return TestClient(app, base_url="http://localhost")


def _hello(runtime_id="worker-1", streams=()):
    return encode_frame(
        HelloFrame(protocol_version=PROTOCOL_VERSION, runtime_id=runtime_id, streams=list(streams))
    )


class TestChannelEndpoint:
    def test_missing_token_rejected(self, channel_client):
        with pytest.raises(Exception):
            with channel_client.websocket_connect(
                "/runtime/channel", headers={"Host": "localhost"}
            ):
                pass

    def test_wrong_token_rejected(self, channel_client):
        with pytest.raises(Exception):
            with channel_client.websocket_connect(
                "/runtime/channel", headers={"Host": "localhost", "X-CAO-Runtime-Token": "wrong"}
            ):
                pass

    def test_no_token_configured_fails_closed(self, monkeypatch):
        monkeypatch.delenv("CAO_RUNTIME_TOKEN", raising=False)
        from fastapi.testclient import TestClient

        from cli_agent_orchestrator.api.main import app

        client = TestClient(app)
        with pytest.raises(Exception):
            with client.websocket_connect(
                "/runtime/channel", headers={"Host": "localhost", "X-CAO-Runtime-Token": ""}
            ):
                pass

    def test_hello_registers_and_rebinds(self, channel_client):
        from cli_agent_orchestrator.runtime_channel.registry import runtime_registry

        with channel_client.websocket_connect(
            "/runtime/channel",
            headers={"Host": "localhost", "X-CAO-Runtime-Token": "test-runtime-token"},
        ) as ws:
            ws.send_text(
                _hello(
                    streams=[
                        StreamPosition(
                            terminal_id=TID,
                            stream=StreamName.CAPTURE,
                            generation=0,
                            end_pos=42,
                        )
                    ]
                )
            )
            reply = decode_frame(ws.receive_text())
            assert isinstance(reply, HelloFrame)
            assert reply.protocol_version == PROTOCOL_VERSION
            # Server answers with its consumed resume position (0: fresh).
            assert reply.resume[0].terminal_id == TID
            assert reply.resume[0].end_pos == 0
            assert runtime_registry.is_remote(TID)
            assert runtime_registry.runtime_for_terminal(TID) == "worker-1"
        runtime_registry.unbind_terminal(TID)

    def test_stream_and_status_republish_to_bus(self, channel_client):
        from cli_agent_orchestrator.runtime_channel.registry import runtime_registry
        from cli_agent_orchestrator.services.event_bus import bus

        received = []
        bus_loop = None

        # The TestClient runs the app in its own event loop; capture publishes
        # by patching is heavier than just recording what publish is given.
        original_publish = bus.publish
        bus.publish = lambda topic, data: received.append((topic, data))
        try:
            with channel_client.websocket_connect(
                "/runtime/channel",
                headers={"Host": "localhost", "X-CAO-Runtime-Token": "test-runtime-token"},
            ) as ws:
                ws.send_text(_hello())
                decode_frame(ws.receive_text())
                ws.send_text(
                    encode_frame(
                        StreamFrame(
                            terminal_id=TID,
                            stream=StreamName.CAPTURE,
                            generation=0,
                            pos=0,
                            data=base64.b64encode("hello from worker".encode()).decode(),
                        )
                    )
                )
                ws.send_text(
                    encode_frame(
                        EventFrame(
                            terminal_id=TID,
                            generation=0,
                            type=EventType.STATUS,
                            status=TerminalStatus.COMPLETED,
                        )
                    )
                )
                # A command result for an op nobody sent must be acked anyway
                # (retained result from before a server restart).
                ws.send_text(
                    encode_frame(
                        CommandResultFrame(
                            op_id="stale-op",
                            terminal_id=TID,
                            outcome=CommandOutcome.OK,
                        )
                    )
                )
                ack = decode_frame(ws.receive_text())
                assert isinstance(ack, AckFrame) and ack.op_id == "stale-op"
                # While the channel is live, the worker-reported status is
                # authoritative; after disconnect it becomes UNKNOWN.
                assert runtime_registry.get_status(TID) == TerminalStatus.COMPLETED
        finally:
            bus.publish = original_publish

        assert (f"terminal.{TID}.output", {"data": "hello from worker"}) in received
        assert (f"terminal.{TID}.status", {"status": "completed"}) in received
        assert runtime_registry.get_status(TID) == TerminalStatus.UNKNOWN
        assert runtime_registry.resume_position(TID, "capture") == len(b"hello from worker")
        runtime_registry.unbind_terminal(TID)
