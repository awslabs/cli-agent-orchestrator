"""Bridge-side handshake and reconnect recovery (#745).

Two claims of the boundary contract had implementations but no test:

- A PROTOCOL_VERSION mismatch is rejected at hello, on BOTH sides, before any
  command is accepted. Every other test builds its hello from the same
  ``PROTOCOL_VERSION`` constant, so the rejection arm never ran.
- What survives a dropped channel is the unacked-result map and the replay
  buffer, so a reconnect re-delivers retained results and resumes the stream
  from the server's position (or says explicitly that bytes were lost). The
  server side and the pure buffer were tested; the bridge's own resume arm,
  which is what actually has to re-send, was not.
"""

import asyncio
import base64

import pytest

from cli_agent_orchestrator.runtime_channel.bridge import Bridge
from cli_agent_orchestrator.runtime_channel.protocol import (
    PROTOCOL_VERSION,
    CommandFrame,
    CommandOutcome,
    CommandResultFrame,
    CommandType,
    GapFrame,
    HelloFrame,
    StreamFrame,
    StreamName,
    StreamPosition,
    decode_frame,
    encode_frame,
)

TID = "abcd1234"


class _FakeWS:
    """The bridge's view of a channel: send, one recv for the server hello,
    then async iteration over whatever the server sends next."""

    def __init__(self, server_hello, inbound=()):
        self.sent = []
        self._server_hello = server_hello
        self._inbound = list(inbound)

    async def send(self, raw):
        self.sent.append(decode_frame(raw))

    async def recv(self):
        return encode_frame(self._server_hello)

    def __aiter__(self):
        async def gen():
            for item in self._inbound:
                yield item

        return gen()

    def frames_of(self, cls):
        return [f for f in self.sent if isinstance(f, cls)]


def _bridge():
    return Bridge("ws://server/runtime/channel", "worker-1", "tok")


def _retained():
    return CommandResultFrame(
        op_id="op-1",
        terminal_id=TID,
        outcome=CommandOutcome.OK,
        payload={"retained": True},
    )


class TestProtocolVersionGate:
    @pytest.mark.asyncio
    async def test_bridge_refuses_a_server_on_another_version(self):
        bridge = _bridge()
        # A command is queued behind the hello: if the gate leaks, it runs.
        command = encode_frame(
            CommandFrame(op_id="op-9", terminal_id=TID, type=CommandType.INPUT, payload={})
        )
        ws = _FakeWS(
            HelloFrame(protocol_version=PROTOCOL_VERSION + 1, runtime_id="server"),
            inbound=[command],
        )
        # Bounded: if the gate leaks, the queued command runs for real, and an
        # unbounded await would hang the suite rather than failing it.
        with pytest.raises(ValueError, match="protocol version mismatch"):
            await asyncio.wait_for(bridge._serve(ws), timeout=10)
        assert ws.frames_of(CommandResultFrame) == [], "no command may be executed after a mismatch"

    @pytest.mark.asyncio
    async def test_bridge_refuses_a_non_hello_answer(self):
        bridge = _bridge()
        ws = _FakeWS(CommandResultFrame(op_id="x", outcome=CommandOutcome.OK, payload={}))
        with pytest.raises(ValueError, match="did not answer hello with hello"):
            await bridge._serve(ws)

    def test_server_closes_a_mismatched_hello_before_registering(self, monkeypatch):
        monkeypatch.setenv("CAO_RUNTIME_TOKEN", "test-runtime-token")
        from fastapi.testclient import TestClient

        from cli_agent_orchestrator.api.main import app
        from cli_agent_orchestrator.runtime_channel.registry import runtime_registry

        client = TestClient(app, base_url="http://localhost")
        with client.websocket_connect(
            "/runtime/channel",
            headers={"Host": "localhost", "X-CAO-Runtime-Token": "test-runtime-token"},
        ) as ws:
            ws.send_text(
                encode_frame(
                    HelloFrame(
                        protocol_version=PROTOCOL_VERSION + 1,
                        runtime_id="worker-mismatch",
                        streams=[],
                    )
                )
            )
            # The server answers with its own version so the runtime can log what
            # it disagreed with, then closes.
            reply = decode_frame(ws.receive_text())
            assert reply.protocol_version == PROTOCOL_VERSION
            # Asserted BEFORE reading again, deliberately: a regressed gate
            # registers the runtime and then waits for commands, so THIS is what
            # fails fast. Reading first would block forever on an open socket.
            assert "worker-mismatch" not in runtime_registry.list_runtimes()
            # Already queued by the close above, so this does not block.
            closed = ws.receive()
        assert closed["type"] == "websocket.close"
        assert closed["code"] == 1002


class TestReconnectRecovery:
    @pytest.mark.asyncio
    async def test_unacked_results_are_redelivered_after_the_handshake(self):
        bridge = _bridge()
        bridge._unacked["op-1"] = _retained()
        ws = _FakeWS(HelloFrame(protocol_version=PROTOCOL_VERSION, runtime_id="server"))

        await bridge._serve(ws)

        results = ws.frames_of(CommandResultFrame)
        assert [r.op_id for r in results] == ["op-1"]
        assert results[0].payload == {"retained": True}
        # Still retained: only the server's ack clears it, so a channel that
        # drops again re-delivers rather than losing the outcome.
        assert "op-1" in bridge._unacked

    @pytest.mark.asyncio
    async def test_stream_resumes_from_the_servers_position_not_from_zero(self):
        bridge = _bridge()
        buf = bridge._buffer_for(TID)
        buf.append(b"already-consumed")
        resume_at = buf.end_pos
        buf.append(b"missed-this")
        ws = _FakeWS(
            HelloFrame(
                protocol_version=PROTOCOL_VERSION,
                runtime_id="server",
                resume=[
                    StreamPosition(
                        terminal_id=TID,
                        stream=StreamName.CAPTURE,
                        generation=buf.generation,
                        end_pos=resume_at,
                    )
                ],
            )
        )

        await bridge._serve(ws)

        replayed = ws.frames_of(StreamFrame)
        assert [base64.b64decode(f.data) for f in replayed] == [b"missed-this"]
        assert ws.frames_of(GapFrame) == [], "nothing was evicted, so nothing was lost"

    @pytest.mark.asyncio
    async def test_eviction_is_reported_as_an_explicit_gap(self):
        bridge = _bridge()
        from cli_agent_orchestrator.runtime_channel.replay_buffer import ReplayBuffer

        # A window too small to hold what the server still needs: the bytes are
        # genuinely gone, and silence here would read as "nothing happened".
        bridge._buffers[TID] = ReplayBuffer(max_bytes=8)
        buf = bridge._buffers[TID]
        buf.append(b"aaaaaaaa")
        buf.append(b"bbbbbbbb")
        ws = _FakeWS(
            HelloFrame(
                protocol_version=PROTOCOL_VERSION,
                runtime_id="server",
                resume=[
                    StreamPosition(
                        terminal_id=TID,
                        stream=StreamName.CAPTURE,
                        generation=buf.generation,
                        end_pos=0,
                    )
                ],
            )
        )

        await bridge._serve(ws)

        gaps = ws.frames_of(GapFrame)
        assert len(gaps) == 1
        assert gaps[0].terminal_id == TID
        assert gaps[0].from_pos == 0 and gaps[0].to_pos > 0

    @pytest.mark.asyncio
    async def test_hello_advertises_current_positions(self):
        bridge = _bridge()
        bridge._buffer_for(TID).append(b"12345")
        ws = _FakeWS(HelloFrame(protocol_version=PROTOCOL_VERSION, runtime_id="server"))

        await bridge._serve(ws)

        hello = ws.frames_of(HelloFrame)[0]
        assert hello.runtime_id == "worker-1"
        advertised = {(s.terminal_id, s.end_pos) for s in hello.streams}
        assert (TID, 5) in advertised

    @pytest.mark.asyncio
    async def test_a_terminal_the_server_does_not_know_is_not_replayed(self):
        bridge = _bridge()
        ws = _FakeWS(
            HelloFrame(
                protocol_version=PROTOCOL_VERSION,
                runtime_id="server",
                resume=[
                    StreamPosition(
                        terminal_id="ffff9999",
                        stream=StreamName.CAPTURE,
                        generation=0,
                        end_pos=0,
                    )
                ],
            )
        )

        await asyncio.wait_for(bridge._serve(ws), timeout=5)

        assert ws.frames_of(StreamFrame) == []
        assert ws.frames_of(GapFrame) == []


class TestReconnectBackoff:
    """What the retry delay is allowed to reset on (#745).

    Found on the cluster: two executors left on an older ``PROTOCOL_VERSION``
    retried once a second for hours. The backoff was reset as soon as
    ``websockets.connect`` returned, and a version mismatch is raised after that
    point, so the growth could never happen for the one failure that is
    permanent. The pod is correctly never Ready either way — the cost is log
    volume against a server that has already refused it.
    """

    @staticmethod
    async def _delays_over(monkeypatch, tmp_path, server_hello, attempts):
        """Run the reconnect loop for ``attempts`` connections; return the
        delays it waited between them."""
        import cli_agent_orchestrator.runtime_channel.bridge as bridge_mod

        # Private marker path: this drives the real run loop, which announces
        # readiness on an established channel.
        monkeypatch.setenv(bridge_mod.READY_FILE_ENV, str(tmp_path / "bridge-connected"))
        # Scaled down so the assertions are about the shape of the growth, not
        # about waiting for it.
        monkeypatch.setattr(bridge_mod, "RECONNECT_BACKOFF_INITIAL", 0.01)
        monkeypatch.setattr(bridge_mod, "RECONNECT_BACKOFF_MAX", 10.0)

        bridge = _bridge()
        connects = []
        delays = []

        class _Conn:
            async def __aenter__(self):
                return _FakeWS(server_hello)

            async def __aexit__(self, *exc):
                return False

        def fake_connect(*a, **k):
            connects.append(True)
            if len(connects) >= attempts:
                # Last attempt: the loop exits after this iteration instead of
                # reconnecting forever.
                bridge._stop.set()
            return _Conn()

        monkeypatch.setattr(bridge_mod.websockets, "connect", fake_connect)
        real_wait_for = asyncio.wait_for

        async def recording_wait_for(awaitable, timeout=None):
            delays.append(timeout)
            return await real_wait_for(awaitable, timeout=timeout)

        monkeypatch.setattr(asyncio, "wait_for", recording_wait_for)
        await real_wait_for(bridge.run(), timeout=10)
        assert len(connects) == attempts
        return delays

    @pytest.mark.asyncio
    async def test_a_channel_that_never_gets_past_hello_backs_off(self, monkeypatch, tmp_path):
        delays = await self._delays_over(
            monkeypatch,
            tmp_path,
            HelloFrame(protocol_version=PROTOCOL_VERSION + 1, runtime_id="server"),
            attempts=4,
        )

        assert delays == [0.01, 0.02, 0.04, 0.08], "each refused hello must wait longer"

    @pytest.mark.asyncio
    async def test_a_channel_that_worked_starts_over(self, monkeypatch, tmp_path):
        """The reset still has to happen for the case it was written for.

        A runtime whose server restarted, or whose network blinked, established
        a channel before losing it — that one must come back promptly rather
        than inheriting a delay from an earlier outage.
        """
        delays = await self._delays_over(
            monkeypatch,
            tmp_path,
            HelloFrame(protocol_version=PROTOCOL_VERSION, runtime_id="server"),
            attempts=4,
        )

        assert delays == [0.01, 0.01, 0.01, 0.01], "a working channel resets the backoff"
