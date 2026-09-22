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
    GapFrame,
    HeartbeatFrame,
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

    @staticmethod
    def _connected(registry, *runtime_ids):
        """Register a live channel per id, as a real hello would."""

        async def send_text(_text):
            pass

        return [registry.register(rid, send_text) for rid in runtime_ids]

    def test_remote_terminal_ids_spans_every_runtime(self):
        """The whole bound set, not one runtime's — what ``list_sessions`` needs.

        A session's agents can be placed on different executors, so an answer
        derived from a single runtime (or from ``list_runtimes`` without
        flattening) would list part of a session and silently drop the rest.
        """
        registry = RuntimeChannelRegistry()
        self._connected(registry, "worker-1", "worker-2")
        registry.bind_terminal("t-a", "worker-1")
        registry.bind_terminal("t-b", "worker-2")

        assert sorted(registry.remote_terminal_ids()) == ["t-a", "t-b"]

        registry.unbind_terminal("t-a")
        assert registry.remote_terminal_ids() == ["t-b"]

    def test_remote_terminal_ids_drops_a_disconnected_runtimes_terminals(self):
        """Enumeration is a liveness question; routing state is not.

        ``unregister`` deliberately leaves the binding in place so a command for
        the terminal fails with "runtime not connected" instead of looking like
        an unknown terminal. Reporting that same binding here kept ``GET
        /sessions`` listing a dead pod's sessions as active forever (Copilot
        review on #802, finding 13).
        """
        registry = RuntimeChannelRegistry()
        conn_a, _ = self._connected(registry, "worker-1", "worker-2")
        registry.bind_terminal("t-a", "worker-1")
        registry.bind_terminal("t-b", "worker-2")

        registry.unregister("worker-1", conn_a)

        assert registry.remote_terminal_ids() == ["t-b"]
        # Routing still knows where t-a was, which is what turns a command for
        # it into an explicit failure rather than a missing terminal.
        assert registry.runtime_for_terminal("t-a") == "worker-1"
        assert registry.is_remote("t-a") is True

    def test_remote_terminal_ids_is_a_snapshot_not_a_view(self):
        """Callers iterate it while awaiting a DB read.

        Returning the live key view instead would raise "dictionary changed size
        during iteration" at a caller the moment a channel disconnected mid-read
        — a fault in one runtime breaking an unrelated listing.
        """
        registry = RuntimeChannelRegistry()
        self._connected(registry, "worker-1", "worker-2")
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

    def test_a_gapframe_for_an_unowned_terminal_is_dropped(self, channel_client):
        """A GapFrame advances the watermark and reports a loss, so it needs the
        same ownership fence as the other frames: a runtime must not be able to
        forge a gap for a terminal it does not own (Copilot follow-up on #802)."""
        from cli_agent_orchestrator.runtime_channel.protocol import GapFrame
        from cli_agent_orchestrator.runtime_channel.registry import runtime_registry

        runtime_registry.bind_terminal(TID, "worker-1")
        runtime_registry.record_position(TID, "capture", 500)
        try:
            headers = {"Host": "localhost", "X-CAO-Runtime-Token": "test-runtime-token"}
            with channel_client.websocket_connect("/runtime/channel", headers=headers) as ws:
                ws.send_text(_hello(runtime_id="worker-2"))
                decode_frame(ws.receive_text())
                ws.send_text(
                    encode_frame(
                        GapFrame(
                            terminal_id=TID,
                            stream=StreamName.CAPTURE,
                            generation=0,
                            from_pos=0,
                            to_pos=999,
                        )
                    )
                )
                # Sync so the gap frame is processed before we assert.
                ws.send_text(
                    encode_frame(
                        CommandResultFrame(
                            op_id="sync-gap", terminal_id=TID, outcome=CommandOutcome.OK
                        )
                    )
                )
                assert decode_frame(ws.receive_text()).op_id == "sync-gap"
            # The forged gap neither moved the watermark nor stole routing.
            assert runtime_registry.resume_position(TID, "capture") == 500
            assert runtime_registry.runtime_for_terminal(TID) == "worker-1"
        finally:
            runtime_registry.unbind_terminal(TID)

    def test_a_hello_claiming_anothers_terminal_is_dropped_from_resume(self, channel_client):
        """The reproduced hijack (guojing1217 on #802): a second runtime's hello
        names a terminal launched on the first. The server must refuse the claim,
        leave routing on the real owner, and not hand back its stream position."""
        from cli_agent_orchestrator.runtime_channel.registry import runtime_registry

        # worker-1 owns TID (as creation would have bound it).
        runtime_registry.bind_terminal(TID, "worker-1")
        runtime_registry.record_position(TID, "capture", 7166)
        try:
            with channel_client.websocket_connect(
                "/runtime/channel",
                headers={"Host": "localhost", "X-CAO-Runtime-Token": "test-runtime-token"},
            ) as ws:
                ws.send_text(
                    _hello(
                        runtime_id="worker-2",
                        streams=[
                            StreamPosition(
                                terminal_id=TID,
                                stream=StreamName.CAPTURE,
                                generation=1,
                                end_pos=0,
                            )
                        ],
                    )
                )
                reply = decode_frame(ws.receive_text())
                assert isinstance(reply, HelloFrame)
                # No resume entry: worker-2 is never told TID's position.
                assert [r for r in reply.resume if r.terminal_id == TID] == []
            # Routing never moved off the real owner.
            assert runtime_registry.runtime_for_terminal(TID) == "worker-1"
        finally:
            runtime_registry.unbind_terminal(TID)

    def test_a_redelivered_launch_result_reconciles_an_orphaned_terminal(
        self, channel_client, monkeypatch
    ):
        """A LAUNCH result the old server never acked would otherwise orphan a
        live terminal after a restart (guojing1217 on #802): the runtime keeps
        running it, but the restarted server has no row or binding. It is
        persisted and bound before the ack, not dropped."""
        import cli_agent_orchestrator.runtime_channel.api as rc_api
        from cli_agent_orchestrator.runtime_channel.registry import runtime_registry

        orphan = "beef9999"
        created = {}
        monkeypatch.setattr(rc_api, "get_terminal_metadata", lambda tid: None)
        monkeypatch.setattr(
            rc_api, "db_create_terminal", lambda *a, **k: created.update({"args": a, "kwargs": k})
        )
        headers = {"Host": "localhost", "X-CAO-Runtime-Token": "test-runtime-token"}
        try:
            with channel_client.websocket_connect("/runtime/channel", headers=headers) as ws:
                ws.send_text(_hello())
                decode_frame(ws.receive_text())
                ws.send_text(
                    encode_frame(
                        CommandResultFrame(
                            op_id="launch-op-lost",
                            terminal_id=orphan,
                            outcome=CommandOutcome.OK,
                            payload={
                                "terminal": {
                                    "id": orphan,
                                    "session_name": "cao-beef9999",
                                    "name": "developer-beef",
                                    "provider": "kiro_cli",
                                    "agent_profile": "developer",
                                    "status": "idle",
                                }
                            },
                        )
                    )
                )
                ack = decode_frame(ws.receive_text())
                assert isinstance(ack, AckFrame) and ack.op_id == "launch-op-lost"
                assert created["args"][0] == orphan  # persisted
                assert created["kwargs"]["metadata"] == {"runtime_id": "worker-1"}
                assert runtime_registry.runtime_for_terminal(orphan) == "worker-1"  # bound
        finally:
            runtime_registry.unbind_terminal(orphan)

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

    def test_a_superseded_channel_stops_being_read(self, channel_client):
        """A reconnect under the same runtime id retires the channel it replaced.

        A pod that reconnects before the server noticed the old socket leaves two
        frame loops running for one runtime id. The registry holds only the new
        connection, but the old loop kept consuming: its stale output was
        republished as live and advanced the resume watermark, so the replacement's
        genuine output was then skipped as already-seen. The protocol carries a
        `generation` field meant to fence this, but nothing has ever advanced it,
        so identity of the registered connection is the check that holds today
        (Copilot review on #802, finding 3).
        """
        from cli_agent_orchestrator.runtime_channel.registry import runtime_registry
        from cli_agent_orchestrator.services.event_bus import bus

        received = []
        original_publish = bus.publish
        bus.publish = lambda topic, data: received.append((topic, data))
        headers = {"Host": "localhost", "X-CAO-Runtime-Token": "test-runtime-token"}
        try:
            with channel_client.websocket_connect("/runtime/channel", headers=headers) as old:
                old.send_text(_hello())
                decode_frame(old.receive_text())
                first = runtime_registry.get_runtime("worker-1")

                with channel_client.websocket_connect("/runtime/channel", headers=headers) as new:
                    new.send_text(_hello())
                    decode_frame(new.receive_text())
                    assert runtime_registry.get_runtime("worker-1") is not first

                    # The retired channel speaks anyway.
                    old.send_text(
                        encode_frame(
                            StreamFrame(
                                terminal_id=TID,
                                stream=StreamName.CAPTURE,
                                generation=0,
                                pos=0,
                                data=base64.b64encode(b"stale output").decode(),
                            )
                        )
                    )
                    # The live channel's output is what reaches subscribers.
                    new.send_text(
                        encode_frame(
                            StreamFrame(
                                terminal_id=TID,
                                stream=StreamName.CAPTURE,
                                generation=0,
                                pos=0,
                                data=base64.b64encode(b"live output").decode(),
                            )
                        )
                    )
                    new.send_text(
                        encode_frame(
                            CommandResultFrame(
                                op_id="sync-op", terminal_id=TID, outcome=CommandOutcome.OK
                            )
                        )
                    )
                    ack = decode_frame(new.receive_text())
                    assert isinstance(ack, AckFrame) and ack.op_id == "sync-op"
        finally:
            bus.publish = original_publish

        topics = [(t, d) for t, d in received if t == f"terminal.{TID}.output"]
        assert (f"terminal.{TID}.output", {"data": "live output"}) in topics
        assert (f"terminal.{TID}.output", {"data": "stale output"}) not in topics
        # And the watermark tracks only the live channel, so nothing it sends next
        # is mistaken for output already consumed.
        assert runtime_registry.resume_position(TID, "capture") == len(b"live output")
        runtime_registry.unbind_terminal(TID)


GAP_TID = "beefcafe"


class TestABoundedGapIsConsumed:
    """Reported loss has to move the watermark, or the channel never catches up.

    Only StreamFrames advanced the resume position. A runtime whose replay window
    evicted a chunk larger than the window itself answers the resume request with a
    gap and no bytes, so the position stayed where it was — and the next reconnect
    asked for the same already-lost range, got the same gap, and so on. The stream
    could never reach live output again. A *bounded* gap is the runtime stating
    exactly which bytes are gone, which is enough to move past them; an unbounded
    one (``to_pos is None``) is it saying it cannot tell, so that stays put
    (Copilot review on #802).
    """

    HEADERS = {"Host": "localhost", "X-CAO-Runtime-Token": "test-runtime-token"}

    @staticmethod
    def _gap(from_pos, to_pos):
        return encode_frame(
            GapFrame(
                terminal_id=GAP_TID,
                stream=StreamName.CAPTURE,
                generation=0,
                from_pos=from_pos,
                to_pos=to_pos,
            )
        )

    @staticmethod
    def _sync(ws, op_id):
        """Round-trip an ack so the frames sent before it have been handled.

        The endpoint processes frames in order on one task, so an ack for a result
        sent afterwards proves the gap ahead of it was consumed — without sleeping.
        """
        ws.send_text(
            encode_frame(
                CommandResultFrame(op_id=op_id, terminal_id=GAP_TID, outcome=CommandOutcome.OK)
            )
        )
        ack = decode_frame(ws.receive_text())
        assert isinstance(ack, AckFrame) and ack.op_id == op_id

    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        from cli_agent_orchestrator.runtime_channel.registry import runtime_registry

        runtime_registry.unbind_terminal(GAP_TID)
        yield
        runtime_registry.unbind_terminal(GAP_TID)

    def test_a_bounded_gap_advances_the_resume_position(self, channel_client):
        from cli_agent_orchestrator.runtime_channel.registry import runtime_registry

        with channel_client.websocket_connect("/runtime/channel", headers=self.HEADERS) as ws:
            ws.send_text(_hello())
            decode_frame(ws.receive_text())
            ws.send_text(self._gap(0, 64))
            self._sync(ws, "sync-gap")

            assert runtime_registry.resume_position(GAP_TID, "capture") == 64

    def test_a_reconnect_after_a_bounded_gap_asks_for_live_bytes_not_the_lost_range(
        self, channel_client
    ):
        """The loop this closes, end to end.

        The second hello advertises the same position it did the first time — the
        runtime's own watermark is unchanged by the loss. What must change is the
        server's answer: it has to ask for bytes after the gap, because asking for
        the lost ones again is what spun forever.
        """
        with channel_client.websocket_connect("/runtime/channel", headers=self.HEADERS) as ws:
            ws.send_text(_hello())
            decode_frame(ws.receive_text())
            ws.send_text(self._gap(0, 64))
            self._sync(ws, "sync-gap")

        with channel_client.websocket_connect("/runtime/channel", headers=self.HEADERS) as ws:
            ws.send_text(
                _hello(
                    streams=[
                        StreamPosition(
                            terminal_id=GAP_TID,
                            stream=StreamName.CAPTURE,
                            generation=0,
                            end_pos=200,
                        )
                    ]
                )
            )
            reply = decode_frame(ws.receive_text())

        assert reply.resume[0].terminal_id == GAP_TID
        assert reply.resume[0].end_pos == 64, "the reconnect asked for the lost range again"

    def test_bytes_after_a_consumed_gap_still_land_contiguously(self, channel_client):
        """Consuming the gap must not make the runtime's next output look stale."""
        from cli_agent_orchestrator.runtime_channel.registry import runtime_registry

        with channel_client.websocket_connect("/runtime/channel", headers=self.HEADERS) as ws:
            ws.send_text(_hello())
            decode_frame(ws.receive_text())
            ws.send_text(self._gap(0, 64))
            ws.send_text(
                encode_frame(
                    StreamFrame(
                        terminal_id=GAP_TID,
                        stream=StreamName.CAPTURE,
                        generation=0,
                        pos=64,
                        data=base64.b64encode(b"after the loss").decode(),
                    )
                )
            )
            self._sync(ws, "sync-after")

            assert runtime_registry.resume_position(GAP_TID, "capture") == 64 + len(
                b"after the loss"
            )

    def test_an_unbounded_gap_leaves_the_position_alone(self, channel_client):
        """``to_pos is None`` is "I cannot tell you what was lost".

        Advancing on that would skip past bytes that may still arrive, and there is
        no value to advance *to*. A lost generation is recovered by the generation
        path instead, so this arm must stay where it was.
        """
        from cli_agent_orchestrator.runtime_channel.registry import runtime_registry

        with channel_client.websocket_connect("/runtime/channel", headers=self.HEADERS) as ws:
            ws.send_text(_hello())
            decode_frame(ws.receive_text())
            ws.send_text(
                encode_frame(
                    StreamFrame(
                        terminal_id=GAP_TID,
                        stream=StreamName.CAPTURE,
                        generation=0,
                        pos=0,
                        data=base64.b64encode(b"12345").decode(),
                    )
                )
            )
            ws.send_text(self._gap(5, None))
            self._sync(ws, "sync-unbounded")

            assert runtime_registry.resume_position(GAP_TID, "capture") == 5

    def test_a_gap_behind_the_watermark_does_not_rewind_it(self, channel_client):
        """A retained gap re-sent after the stream moved on must not undo progress.

        ``record_position`` only ever moves forward, and the gap arm goes through it
        for that reason: a late or duplicated gap frame cannot reopen a range the
        server has already consumed and cause the bytes after it to be replayed.
        """
        from cli_agent_orchestrator.runtime_channel.registry import runtime_registry

        with channel_client.websocket_connect("/runtime/channel", headers=self.HEADERS) as ws:
            ws.send_text(_hello())
            decode_frame(ws.receive_text())
            ws.send_text(
                encode_frame(
                    StreamFrame(
                        terminal_id=GAP_TID,
                        stream=StreamName.CAPTURE,
                        generation=0,
                        pos=0,
                        data=base64.b64encode(b"0123456789").decode(),
                    )
                )
            )
            ws.send_text(self._gap(0, 4))
            self._sync(ws, "sync-stale-gap")

            assert runtime_registry.resume_position(GAP_TID, "capture") == 10

    def test_the_loss_is_still_reported_to_subscribers(self, channel_client):
        """Consuming a gap is bookkeeping, not suppression.

        The subscriber-visible marker is the only signal that a transcript has a
        hole in it, so the fix must not have traded a silent hole for a spinning
        one.
        """
        from cli_agent_orchestrator.services.event_bus import bus

        received = []
        original_publish = bus.publish
        bus.publish = lambda topic, data: received.append((topic, data))
        try:
            with channel_client.websocket_connect("/runtime/channel", headers=self.HEADERS) as ws:
                ws.send_text(_hello())
                decode_frame(ws.receive_text())
                ws.send_text(self._gap(0, 64))
                self._sync(ws, "sync-published")
        finally:
            bus.publish = original_publish

        assert (
            f"terminal.{GAP_TID}.output",
            {"data": "", "gap": {"from_pos": 0, "to_pos": 64}},
        ) in received


HB_TID = "cafed00d"


class TestAHeartbeatWatermarkTheServerIsBehind:
    """The heartbeat carries end positions so a lost FINAL chunk is detectable.

    The handler bound routing and discarded the positions, so nothing acted on the
    one signal that carries them: output that never arrived, with no later output
    coming to reveal it, went unnoticed indefinitely (Copilot review on #802).

    The reviewed suggestion — persist the advertised ``end_pos`` as the resume
    position — is what these tests rule out. The recorded position means "bytes the
    server has received"; those bytes are still in the runtime's replay buffer, so
    the stale watermark is precisely what makes the next reconnect replay them.
    Adopting the advertised position would mark unreceived output as received and
    discard the only path that recovers it.
    """

    HEADERS = {"Host": "localhost", "X-CAO-Runtime-Token": "test-runtime-token"}

    @staticmethod
    def _heartbeat(end_pos, generation=0):
        return encode_frame(
            HeartbeatFrame(
                streams=[
                    StreamPosition(
                        terminal_id=HB_TID,
                        stream=StreamName.CAPTURE,
                        generation=generation,
                        end_pos=end_pos,
                    )
                ]
            )
        )

    @staticmethod
    def _sync(ws, op_id):
        """Ordered round-trip, so the heartbeats before it have been handled."""
        ws.send_text(
            encode_frame(
                CommandResultFrame(op_id=op_id, terminal_id=HB_TID, outcome=CommandOutcome.OK)
            )
        )
        ack = decode_frame(ws.receive_text())
        assert isinstance(ack, AckFrame) and ack.op_id == op_id

    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        from cli_agent_orchestrator.runtime_channel.registry import runtime_registry

        runtime_registry.unbind_terminal(HB_TID)
        yield
        runtime_registry.unbind_terminal(HB_TID)

    def test_a_heartbeat_never_advances_the_resume_position(self, channel_client):
        """The bytes are unreceived, not lost: the watermark must stay truthful."""
        from cli_agent_orchestrator.runtime_channel.registry import runtime_registry

        with channel_client.websocket_connect("/runtime/channel", headers=self.HEADERS) as ws:
            ws.send_text(_hello())
            decode_frame(ws.receive_text())
            ws.send_text(self._heartbeat(500))
            ws.send_text(self._heartbeat(500))
            self._sync(ws, "sync-hb")

            assert runtime_registry.resume_position(HB_TID, "capture") == 0

    def test_a_reconnect_still_asks_for_the_bytes_that_never_arrived(self, channel_client):
        """The recovery the advertised position would have thrown away."""
        with channel_client.websocket_connect("/runtime/channel", headers=self.HEADERS) as ws:
            ws.send_text(_hello())
            decode_frame(ws.receive_text())
            ws.send_text(
                encode_frame(
                    StreamFrame(
                        terminal_id=HB_TID,
                        stream=StreamName.CAPTURE,
                        generation=0,
                        pos=0,
                        data=base64.b64encode(b"first chunk").decode(),
                    )
                )
            )
            ws.send_text(self._heartbeat(500))
            ws.send_text(self._heartbeat(500))
            self._sync(ws, "sync-hb")

        with channel_client.websocket_connect("/runtime/channel", headers=self.HEADERS) as ws:
            ws.send_text(
                _hello(
                    streams=[
                        StreamPosition(
                            terminal_id=HB_TID,
                            stream=StreamName.CAPTURE,
                            generation=0,
                            end_pos=500,
                        )
                    ]
                )
            )
            reply = decode_frame(ws.receive_text())

        assert reply.resume[0].end_pos == len(b"first chunk"), (
            "the reconnect must resume where the server's received bytes end, "
            "so the runtime replays what never arrived"
        )

    def test_a_persistent_shortfall_is_reported(self, channel_client, caplog):
        """Two heartbeats with no progress: nobody would otherwise learn of it."""
        import logging

        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.runtime_channel.api"):
            with channel_client.websocket_connect("/runtime/channel", headers=self.HEADERS) as ws:
                ws.send_text(_hello())
                decode_frame(ws.receive_text())
                ws.send_text(self._heartbeat(500))
                ws.send_text(self._heartbeat(500))
                self._sync(ws, "sync-hb")

        assert any(
            HB_TID in record.message and "500" in record.message
            for record in caplog.records
            if record.levelno >= logging.WARNING
        ), f"no warning named the shortfall: {[r.message for r in caplog.records]}"

    def test_one_heartbeat_alone_says_nothing(self, channel_client, caplog):
        """A reconnect's replay is a stream of chunks and a heartbeat can land
        between them, so a single observation is not evidence of loss. A warning
        that fires during ordinary recovery is one operators learn to ignore."""
        import logging

        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.runtime_channel.api"):
            with channel_client.websocket_connect("/runtime/channel", headers=self.HEADERS) as ws:
                ws.send_text(_hello())
                decode_frame(ws.receive_text())
                ws.send_text(self._heartbeat(500))
                self._sync(ws, "sync-hb")

        assert not [
            r
            for r in caplog.records
            if HB_TID in r.message and r.name == "cli_agent_orchestrator.runtime_channel.api"
        ]

    def test_output_arriving_between_heartbeats_clears_the_suspicion(self, channel_client, caplog):
        """Progress is the discriminator: bytes landed, so nothing was lost."""
        import logging

        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.runtime_channel.api"):
            with channel_client.websocket_connect("/runtime/channel", headers=self.HEADERS) as ws:
                ws.send_text(_hello())
                decode_frame(ws.receive_text())
                ws.send_text(self._heartbeat(11))
                ws.send_text(
                    encode_frame(
                        StreamFrame(
                            terminal_id=HB_TID,
                            stream=StreamName.CAPTURE,
                            generation=0,
                            pos=0,
                            data=base64.b64encode(b"first chunk").decode(),
                        )
                    )
                )
                ws.send_text(self._heartbeat(11))
                self._sync(ws, "sync-hb")

        assert not [
            r
            for r in caplog.records
            if HB_TID in r.message and r.name == "cli_agent_orchestrator.runtime_channel.api"
        ]

    def test_a_heartbeat_the_server_is_level_with_is_silent(self, channel_client, caplog):
        """The steady state, which is every heartbeat on a healthy channel."""
        import logging

        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.runtime_channel.api"):
            with channel_client.websocket_connect("/runtime/channel", headers=self.HEADERS) as ws:
                ws.send_text(_hello())
                decode_frame(ws.receive_text())
                ws.send_text(self._heartbeat(0))
                ws.send_text(self._heartbeat(0))
                self._sync(ws, "sync-hb")

        assert not [
            r
            for r in caplog.records
            if HB_TID in r.message and r.name == "cli_agent_orchestrator.runtime_channel.api"
        ]

    def test_a_heartbeat_still_binds_routing(self, channel_client):
        """The behaviour that was already there has to survive the addition."""
        from cli_agent_orchestrator.runtime_channel.registry import runtime_registry

        with channel_client.websocket_connect("/runtime/channel", headers=self.HEADERS) as ws:
            ws.send_text(_hello())
            decode_frame(ws.receive_text())
            ws.send_text(self._heartbeat(500))
            self._sync(ws, "sync-hb")

            assert runtime_registry.runtime_for_terminal(HB_TID) == "worker-1"
