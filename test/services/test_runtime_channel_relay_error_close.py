"""A mid-session relay failure closes the client with an error code (Augusto).

The OPEN failure already closed with 4010, but once the relay was running a
runtime that went away or a keystroke that timed out fell into the generic
``websocket.close()`` — a normal 1000 — so the client could not tell a crash
from a clean detach. A real PTY EOF must still close normally.
"""

import asyncio

import pytest

from cli_agent_orchestrator.runtime_channel.protocol import (
    CommandOutcome,
    CommandResultFrame,
    CommandType,
)
from cli_agent_orchestrator.runtime_channel.registry import (
    RuntimeUnavailableError,
    runtime_registry,
)


class _FakeWebSocket:
    def __init__(self, incoming):
        self._incoming = list(incoming)
        self.sent_bytes = []
        self.closed = None

    async def receive_text(self):
        if not self._incoming:
            await asyncio.sleep(3600)
        await asyncio.sleep(0.01)
        return self._incoming.pop(0)

    async def send_bytes(self, data):
        self.sent_bytes.append(data)

    async def close(self, code=1000, reason=""):
        if self.closed is None:
            self.closed = (code, reason)


@pytest.mark.asyncio
async def test_midsession_timeout_closes_with_error_code_4011():
    from cli_agent_orchestrator.runtime_channel.api import relay_remote_attach

    async def fake_send_terminal_command(terminal_id, ctype, payload, timeout):
        if ctype == CommandType.ATTACH_OPEN:
            return CommandResultFrame(
                op_id="x",
                outcome=CommandOutcome.OK,
                payload={"opened": True},
                terminal_id=terminal_id,
            )
        if ctype == CommandType.ATTACH_DATA:
            raise TimeoutError("runtime did not answer the keystroke")
        return CommandResultFrame(
            op_id="x", outcome=CommandOutcome.OK, payload={}, terminal_id=terminal_id
        )

    ws = _FakeWebSocket(['{"type": "input", "data": "ls\\r"}'])
    from unittest.mock import patch

    with patch.object(
        runtime_registry, "send_terminal_command", side_effect=fake_send_terminal_command
    ):
        await relay_remote_attach(ws, "ea0e0001")

    assert ws.closed is not None
    assert ws.closed[0] == 4011, f"expected an error close, got {ws.closed}"


@pytest.mark.asyncio
async def test_midsession_runtime_unavailable_closes_with_error_code_4011():
    from cli_agent_orchestrator.runtime_channel.api import relay_remote_attach

    async def fake_send_terminal_command(terminal_id, ctype, payload, timeout):
        if ctype == CommandType.ATTACH_OPEN:
            return CommandResultFrame(
                op_id="x",
                outcome=CommandOutcome.OK,
                payload={"opened": True},
                terminal_id=terminal_id,
            )
        if ctype == CommandType.ATTACH_DATA:
            raise RuntimeUnavailableError("runtime 'w' is not connected")
        return CommandResultFrame(
            op_id="x", outcome=CommandOutcome.OK, payload={}, terminal_id=terminal_id
        )

    ws = _FakeWebSocket(['{"type": "input", "data": "x"}'])
    from unittest.mock import patch

    with patch.object(
        runtime_registry, "send_terminal_command", side_effect=fake_send_terminal_command
    ):
        await relay_remote_attach(ws, "ea0e0002")

    assert ws.closed is not None and ws.closed[0] == 4011


@pytest.mark.asyncio
async def test_a_normal_pty_eof_still_closes_cleanly():
    """A runtime PTY EOF ends _downstream with no error, so the close is 1000."""
    from cli_agent_orchestrator.runtime_channel.api import relay_remote_attach

    async def fake_send_terminal_command(terminal_id, ctype, payload, timeout):
        if ctype == CommandType.ATTACH_OPEN:
            # Immediately EOF the bound sink: the PTY ended.
            runtime_registry.deliver_attach(terminal_id, None)
        return CommandResultFrame(
            op_id="x", outcome=CommandOutcome.OK, payload={"opened": True}, terminal_id=terminal_id
        )

    ws = _FakeWebSocket([])
    from unittest.mock import patch

    with patch.object(
        runtime_registry, "send_terminal_command", side_effect=fake_send_terminal_command
    ):
        await relay_remote_attach(ws, "ea0e0003")

    assert ws.closed is not None and ws.closed[0] == 1000
