"""Every path that drops an attach client wakes it with EOF.

The live attach client is stored with its epoch. A reconnect whose hello no longer
claims a terminal, a runtime disconnect, and a teardown must each hand that client
its EOF (``None``) so the relay unwinds instead of waiting forever.
"""

import asyncio

import pytest

from cli_agent_orchestrator.runtime_channel.registry import RuntimeChannelRegistry


async def _noop_send(_text):
    return None


@pytest.mark.asyncio
async def test_a_hello_that_drops_an_attached_terminal_eofs_its_client():
    registry = RuntimeChannelRegistry()
    registry.register("rt-1", _noop_send)
    registry.bind_terminal("t1", "rt-1")
    sink: asyncio.Queue = asyncio.Queue()
    registry.bind_attach("t1", sink, registry.next_attach_epoch("t1"))

    stale = registry.reconcile_hello("rt-1", advertised=[])

    assert stale == ["t1"]
    assert sink.get_nowait() is None


@pytest.mark.asyncio
async def test_a_disconnect_eofs_the_attach_client():
    registry = RuntimeChannelRegistry()
    conn = registry.register("rt-1", _noop_send)
    registry.bind_terminal("t1", "rt-1")
    sink: asyncio.Queue = asyncio.Queue()
    registry.bind_attach("t1", sink, registry.next_attach_epoch("t1"))

    registry.unregister("rt-1", conn)

    assert sink.get_nowait() is None


@pytest.mark.asyncio
async def test_a_teardown_eofs_the_attach_client():
    registry = RuntimeChannelRegistry()
    registry.register("rt-1", _noop_send)
    registry.bind_terminal("t1", "rt-1")
    sink: asyncio.Queue = asyncio.Queue()
    registry.bind_attach("t1", sink, registry.next_attach_epoch("t1"))

    registry.unbind_terminal("t1", deleted=True)

    assert sink.get_nowait() is None
