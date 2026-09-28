"""Hello/heartbeat liveness is an explicit set, not buffer existence (#745).

``_stream_positions`` and ``_terminal_statuses`` enumerated ``_buffers``, so a
terminal live in this runtime but without an in-memory buffer — a process that
kept its durable rows across an in-memory reset, a pane not created through this
LAUNCH handler — was omitted from the hello, and a restarted server stayed
UNKNOWN and never rebound it. The bridge now maintains an explicit live set,
added at LAUNCH and removed at TEARDOWN, and advertises from it.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.runtime_channel.bridge import Bridge
from cli_agent_orchestrator.runtime_channel.protocol import CommandFrame, CommandType

TID = "abcd1234"


def _bridge():
    return Bridge("ws://server/runtime/channel", "worker-1", "tok")


def test_a_live_terminal_with_no_buffer_is_advertised(monkeypatch):
    bridge = _bridge()
    bridge._live_terminals.add(TID)
    assert TID not in bridge._buffers, "precondition: no buffer for it yet"

    from cli_agent_orchestrator.runtime_channel import bridge as bridge_mod

    monkeypatch.setattr(
        bridge_mod.status_monitor,
        "get_status",
        lambda tid: TerminalStatus.PROCESSING,
        raising=False,
    )

    positions = bridge._stream_positions()
    assert [(p.terminal_id, p.end_pos) for p in positions] == [(TID, 0)]
    assert bridge._terminal_statuses() == {TID: TerminalStatus.PROCESSING}


@pytest.mark.asyncio
async def test_launch_adds_the_terminal_to_the_live_set():
    from cli_agent_orchestrator.services import terminal_service

    bridge = _bridge()
    launched = SimpleNamespace(
        id="beef0001",
        name="agent-0",
        provider="kiro_cli",
        session_name="cao-1234",
        agent_profile="developer",
        allowed_tools=None,
        shell_command=None,
        status="initializing",
    )
    with patch.object(terminal_service, "create_terminal", AsyncMock(return_value=launched)):
        await bridge._execute(
            CommandFrame(
                op_id="op-launch",
                type=CommandType.LAUNCH,
                terminal_id=None,
                payload={"provider": "kiro_cli", "agent_profile": "developer"},
            )
        )
    assert "beef0001" in bridge._live_terminals


@pytest.mark.asyncio
async def test_teardown_removes_the_terminal_from_the_live_set():
    from cli_agent_orchestrator.services import terminal_service

    bridge = _bridge()
    bridge._live_terminals.add(TID)
    bridge._buffer_for(TID)
    with patch.object(terminal_service, "delete_terminal", return_value=True):
        await bridge._execute(
            CommandFrame(
                op_id="op-teardown",
                type=CommandType.TEARDOWN,
                terminal_id=TID,
                payload={},
            )
        )
    assert TID not in bridge._live_terminals


@pytest.mark.asyncio
async def test_teardown_of_an_absent_terminal_also_removes_it():
    from cli_agent_orchestrator.services import terminal_service

    bridge = _bridge()
    bridge._live_terminals.add(TID)
    with (
        patch.object(terminal_service, "delete_terminal", return_value=False),
        patch.object(terminal_service, "get_terminal_metadata", return_value=None),
    ):
        outcome, payload, _ = await bridge._execute(
            CommandFrame(
                op_id="op-teardown",
                type=CommandType.TEARDOWN,
                terminal_id=TID,
                payload={},
            )
        )
    assert payload.get("absent") is True
    assert TID not in bridge._live_terminals
