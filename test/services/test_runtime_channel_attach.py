"""Interactive attach relay across the runtime channel (#776 slice in #745).

Bridge side: ATTACH_OPEN spawns the backend's attach client in a runtime-local
PTY and pumps its bytes up as the ``attach`` stream; ATTACH_DATA/RESIZE act on
that PTY; ATTACH_CLOSE (and TEARDOWN) end it. Server side: the browser/native
WS endpoint relays the same client protocol over the channel instead of
spawning a local PTY.
"""

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.runtime_channel.bridge import Bridge
from cli_agent_orchestrator.runtime_channel.protocol import (
    CommandFrame,
    CommandOutcome,
    CommandResultFrame,
    CommandType,
    StreamName,
)
from cli_agent_orchestrator.runtime_channel.registry import runtime_registry


def _cmd(op_id, ctype, terminal_id, payload=None):
    return CommandFrame(op_id=op_id, type=ctype, terminal_id=terminal_id, payload=payload or {})


class _FakeBackend:
    def prepare_web_attach(self, session_name, window_name):
        # `cat` echoes PTY input back out — a stand-in interactive client
        # with no tmux dependency.
        return ["cat"]


@pytest.mark.asyncio
async def test_bridge_attach_round_trip_streams_pty_bytes():
    bridge = Bridge("ws://unused", "worker-x", "tok")
    sent = []

    async def fake_send(frame):
        sent.append(frame)

    with (
        patch.object(bridge, "_send", side_effect=fake_send),
        patch(
            "cli_agent_orchestrator.backends.registry.get_backend",
            return_value=_FakeBackend(),
        ),
        patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            return_value={"tmux_session": "s", "tmux_window": "w"},
        ),
    ):
        outcome, payload, _ = await bridge._execute(
            _cmd("op1", CommandType.ATTACH_OPEN, "abcd1234", {"rows": 24, "cols": 80})
        )
        assert outcome == CommandOutcome.OK and payload["opened"] is True

        outcome, payload, _ = await bridge._execute(
            _cmd(
                "op2",
                CommandType.ATTACH_DATA,
                "abcd1234",
                {"data": base64.b64encode(b"hello-attach\n").decode()},
            )
        )
        assert payload["written"] is True

        # The PTY echoes; the pump task must stream it up as `attach` frames.
        for _ in range(100):
            if any(
                getattr(f, "stream", None) == StreamName.ATTACH
                and "hello-attach" in base64.b64decode(f.data).decode(errors="replace")
                for f in sent
                if getattr(f, "data", "")
            ):
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail(f"echo never streamed; frames: {sent!r}")

        outcome, payload, _ = await bridge._execute(
            _cmd("op3", CommandType.RESIZE, "abcd1234", {"rows": 40, "cols": 120})
        )
        assert payload["resized"] is True

        outcome, payload, _ = await bridge._execute(
            _cmd("op4", CommandType.ATTACH_CLOSE, "abcd1234")
        )
        assert payload["closed"] is True
        assert "abcd1234" not in bridge._attach


@pytest.mark.asyncio
async def test_bridge_attach_open_fails_cleanly_without_terminal():
    bridge = Bridge("ws://unused", "worker-x", "tok")
    with patch(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        return_value=None,
    ):
        outcome, payload, _ = await bridge._execute(
            _cmd("op1", CommandType.ATTACH_OPEN, "deadbeef")
        )
    assert outcome == CommandOutcome.FAILED
    assert payload["opened"] is False


class _FakeWebSocket:
    """Minimal server-side WS double for relay_remote_attach."""

    def __init__(self, incoming):
        self._incoming = list(incoming)
        self.sent_bytes = []
        self.closed = None

    async def receive_text(self):
        if not self._incoming:
            from starlette.websockets import WebSocketDisconnect

            raise WebSocketDisconnect(1000)
        await asyncio.sleep(0.01)
        return self._incoming.pop(0)

    async def send_bytes(self, data):
        self.sent_bytes.append(data)

    async def close(self, code=1000, reason=""):
        if self.closed is None:
            self.closed = (code, reason)


@pytest.mark.asyncio
async def test_server_relay_forwards_both_directions():
    from cli_agent_orchestrator.runtime_channel.api import relay_remote_attach

    commands = []

    async def fake_send_terminal_command(terminal_id, ctype, payload, timeout):
        commands.append((ctype, payload))
        if ctype == CommandType.ATTACH_OPEN:
            # Simulate the runtime pushing one screenful right after open.
            runtime_registry.deliver_attach(terminal_id, b"\x1b[2Jwelcome")
        return CommandResultFrame(
            op_id="x", outcome=CommandOutcome.OK, payload={"opened": True}, terminal_id=terminal_id
        )

    ws = _FakeWebSocket(
        [
            '{"type": "resize", "rows": 40, "cols": 132}',
            '{"type": "input", "data": "ls\\r"}',
        ]
    )
    with patch.object(
        runtime_registry, "send_terminal_command", side_effect=fake_send_terminal_command
    ):
        await relay_remote_attach(ws, "abcd1234")

    types = [c[0] for c in commands]
    assert types[0] == CommandType.ATTACH_OPEN
    assert CommandType.RESIZE in types
    assert CommandType.ATTACH_DATA in types
    assert types[-1] == CommandType.ATTACH_CLOSE
    data_payload = next(p for t, p in commands if t == CommandType.ATTACH_DATA)
    assert base64.b64decode(data_payload["data"]) == b"ls\r"
    assert b"welcome" in b"".join(ws.sent_bytes)


@pytest.mark.asyncio
async def test_a_second_client_displaces_the_first_without_killing_the_pty():
    """Two attaches to one terminal: the loser must not close the winner's PTY.

    One live attach sink exists per terminal, so a second client takes the
    binding over. The displaced relay used to send ATTACH_CLOSE on its way out,
    which reached the runtime AFTER the replacement's ATTACH_OPEN and tore down
    the PTY the new client was already reading — a blank screen for a session
    that is fine (Copilot review on #802, finding 4).
    """
    from cli_agent_orchestrator.runtime_channel.api import relay_remote_attach

    closes = []
    first_open = asyncio.Event()

    async def fake_send_terminal_command(terminal_id, ctype, payload, timeout):
        if ctype == CommandType.ATTACH_OPEN:
            first_open.set()
        if ctype == CommandType.ATTACH_CLOSE:
            closes.append(terminal_id)
        return CommandResultFrame(
            op_id="x", outcome=CommandOutcome.OK, payload={"opened": True}, terminal_id=terminal_id
        )

    # The first client never sends anything and stays up until it is displaced.
    class _Idle(_FakeWebSocket):
        async def receive_text(self):
            await asyncio.sleep(3600)

    first = _Idle([])
    with patch.object(
        runtime_registry, "send_terminal_command", side_effect=fake_send_terminal_command
    ):
        first_relay = asyncio.create_task(relay_remote_attach(first, "abcd1234"))
        await asyncio.wait_for(first_open.wait(), timeout=2.0)

        # A second client arrives and takes the binding; the first's downstream
        # reader sees the None sentinel and unwinds.
        second_sink: asyncio.Queue = asyncio.Queue()
        assert runtime_registry.bind_attach("abcd1234", second_sink) is True
        await asyncio.wait_for(first_relay, timeout=2.0)

        # The displaced relay no longer owns the attach, so it sent no close.
        assert closes == []
        # The replacement still owns it, and releasing it does close the PTY.
        assert runtime_registry.unbind_attach("abcd1234", second_sink) is True


@pytest.mark.asyncio
async def test_releasing_a_sink_that_was_already_displaced_is_a_no_op():
    sink_a: asyncio.Queue = asyncio.Queue()
    sink_b: asyncio.Queue = asyncio.Queue()
    assert runtime_registry.bind_attach("eeee1111", sink_a) is False  # nothing displaced
    assert runtime_registry.bind_attach("eeee1111", sink_b) is True  # displaced A
    assert sink_a.get_nowait() is None  # A was told its stream ended
    assert runtime_registry.unbind_attach("eeee1111", sink_a) is False
    assert runtime_registry.unbind_attach("eeee1111", sink_b) is True
    assert runtime_registry.unbind_attach("eeee1111", sink_b) is False


@pytest.mark.asyncio
async def test_server_relay_reports_unconnected_runtime_as_4010():
    from cli_agent_orchestrator.runtime_channel.api import relay_remote_attach
    from cli_agent_orchestrator.runtime_channel.registry import RuntimeUnavailableError

    async def refuse(*args, **kwargs):
        raise RuntimeUnavailableError("runtime 'gone' is not connected")

    ws = _FakeWebSocket([])
    with patch.object(runtime_registry, "send_terminal_command", side_effect=refuse):
        await relay_remote_attach(ws, "abcd1234")
    assert ws.closed is not None and ws.closed[0] == 4010
