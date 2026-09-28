"""Attach displacement is fenced by a per-terminal epoch.

These wire the REAL ``Bridge`` to the REAL ``registry.deliver_attach`` — the
existing attach tests patch ``send_terminal_command``, so they never exercised
the bridge's ``_attach_open`` -> ``_attach_close`` EOF path that reached the
NEW client's sink and tore it down.

The fix stamps every ATTACH StreamFrame with the attach epoch as its
``generation``; the server binds each sink with its epoch and drops a frame
whose epoch is not the bound one; the bridge closes a displaced PTY WITHOUT an
EOF and ignores a command whose epoch is not the current PTY's.
"""

import asyncio
import base64

import pytest

from cli_agent_orchestrator.runtime_channel.bridge import Bridge
from cli_agent_orchestrator.runtime_channel.protocol import (
    CommandType,
    StreamName,
)
from cli_agent_orchestrator.runtime_channel.registry import RuntimeChannelRegistry


def _cmd(op_id, ctype, terminal_id, payload=None):
    from cli_agent_orchestrator.runtime_channel.protocol import CommandFrame

    return CommandFrame(op_id=op_id, type=ctype, terminal_id=terminal_id, payload=payload or {})


class _FakeBackend:
    def prepare_web_attach(self, session_name, window_name):
        return ["cat"]  # echoes stdin; no tmux dependency


def _wire(registry):
    """A _send that mirrors api.py's ATTACH StreamFrame handler onto the
    registry, epoch (=generation) and all."""

    async def route(frame):
        if getattr(frame, "stream", None) == StreamName.ATTACH:
            raw = base64.b64decode(frame.data) if frame.data else b""
            registry.deliver_attach(frame.terminal_id, raw or None, frame.generation)

    return route


def _patches(bridge, registry):
    from unittest.mock import patch

    return (
        patch.object(bridge, "_send", side_effect=_wire(registry)),
        patch(
            "cli_agent_orchestrator.backends.registry.get_backend",
            return_value=_FakeBackend(),
        ),
        patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            return_value={"tmux_session": "s", "tmux_window": "w"},
        ),
    )


@pytest.mark.asyncio
async def test_next_attach_epoch_is_strictly_increasing_per_terminal():
    registry = RuntimeChannelRegistry()
    assert registry.next_attach_epoch("t1") == 1
    assert registry.next_attach_epoch("t1") == 2
    assert registry.next_attach_epoch("t1") == 3
    # Independent per terminal.
    assert registry.next_attach_epoch("t2") == 1


def test_deliver_attach_drops_a_stale_epoch():
    registry = RuntimeChannelRegistry()
    sink: asyncio.Queue = asyncio.Queue()
    registry.bind_attach("abcd1234", sink, 2)
    # A frame stamped with the OLD epoch is dropped, not delivered.
    assert registry.deliver_attach("abcd1234", b"stale", 1) is False
    assert sink.empty()
    # The bound epoch is delivered.
    assert registry.deliver_attach("abcd1234", b"fresh", 2) is True
    assert sink.get_nowait() == b"fresh"


@pytest.mark.asyncio
async def test_a_second_attach_does_not_eof_the_new_clients_sink():
    """The displaced PTY's EOF must never reach the replacement client."""
    registry = RuntimeChannelRegistry()
    tid = "abcd1234"
    bridge = Bridge("ws://unused", "worker-x", "tok")
    p_send, p_backend, p_meta = _patches(bridge, registry)
    with p_send, p_backend, p_meta:
        epoch_a = registry.next_attach_epoch(tid)
        sink_a: asyncio.Queue = asyncio.Queue()
        assert registry.bind_attach(tid, sink_a, epoch_a) is False
        _, payload, _ = await bridge._execute(
            _cmd("A", CommandType.ATTACH_OPEN, tid, {"rows": 24, "cols": 80, "epoch": epoch_a})
        )
        assert payload["opened"] is True

        # Client B binds FIRST (EOFs sink_a), then its ATTACH_OPEN replaces the
        # PTY. The old PTY must be closed with no EOF, and any late epoch-A EOF
        # must be dropped by the registry.
        epoch_b = registry.next_attach_epoch(tid)
        sink_b: asyncio.Queue = asyncio.Queue()
        assert registry.bind_attach(tid, sink_b, epoch_b) is True
        assert sink_a.get_nowait() is None  # A was told its stream ended

        _, payload, _ = await bridge._execute(
            _cmd("B", CommandType.ATTACH_OPEN, tid, {"rows": 24, "cols": 80, "epoch": epoch_b})
        )
        assert payload["opened"] is True

        await asyncio.sleep(0.05)  # let any stray EOF frame route
        with pytest.raises(asyncio.QueueEmpty):
            sink_b.get_nowait()
        # Cleanup.
        await bridge._attach_close(tid)


@pytest.mark.asyncio
async def test_a_stale_attach_close_does_not_close_the_replacement_pty():
    """The first relay's ATTACH_CLOSE, run after B's ATTACH_OPEN, is ignored."""
    registry = RuntimeChannelRegistry()
    tid = "abcd1234"
    bridge = Bridge("ws://unused", "worker-x", "tok")
    p_send, p_backend, p_meta = _patches(bridge, registry)
    with p_send, p_backend, p_meta:
        epoch_a = registry.next_attach_epoch(tid)
        await bridge._execute(_cmd("A", CommandType.ATTACH_OPEN, tid, {"epoch": epoch_a}))
        epoch_b = registry.next_attach_epoch(tid)
        await bridge._execute(_cmd("B", CommandType.ATTACH_OPEN, tid, {"epoch": epoch_b}))
        assert tid in bridge._attach

        # A's teardown, delivered late, names epoch A — the current PTY is B.
        _, payload, _ = await bridge._execute(
            _cmd("A-close", CommandType.ATTACH_CLOSE, tid, {"epoch": epoch_a})
        )
        assert payload["closed"] is False
        assert tid in bridge._attach, "the replacement PTY was torn down by a stale close"
        await bridge._attach_close(tid)


@pytest.mark.asyncio
async def test_a_sole_clients_close_still_eofs_it():
    registry = RuntimeChannelRegistry()
    tid = "beef0002"
    bridge = Bridge("ws://unused", "worker-x", "tok")
    p_send, p_backend, p_meta = _patches(bridge, registry)
    with p_send, p_backend, p_meta:
        epoch = registry.next_attach_epoch(tid)
        sink: asyncio.Queue = asyncio.Queue()
        registry.bind_attach(tid, sink, epoch)
        await bridge._execute(_cmd("A", CommandType.ATTACH_OPEN, tid, {"epoch": epoch}))
        _, payload, _ = await bridge._execute(
            _cmd("C", CommandType.ATTACH_CLOSE, tid, {"epoch": epoch})
        )
        assert payload["closed"] is True
        assert sink.get_nowait() is None  # the sole client is EOF'd, as intended


@pytest.mark.asyncio
async def test_a_teardown_still_eofs_the_client():
    from unittest.mock import patch

    registry = RuntimeChannelRegistry()
    tid = "beef0003"
    bridge = Bridge("ws://unused", "worker-x", "tok")
    p_send, p_backend, p_meta = _patches(bridge, registry)
    with (
        p_send,
        p_backend,
        p_meta,
        patch(
            "cli_agent_orchestrator.services.terminal_service.delete_terminal",
            return_value=True,
        ),
    ):
        epoch = registry.next_attach_epoch(tid)
        sink: asyncio.Queue = asyncio.Queue()
        registry.bind_attach(tid, sink, epoch)
        await bridge._execute(_cmd("A", CommandType.ATTACH_OPEN, tid, {"epoch": epoch}))
        # TEARDOWN closes whatever PTY is current and EOFs its client.
        await bridge._execute(_cmd("T", CommandType.TEARDOWN, tid))
        assert sink.get_nowait() is None
