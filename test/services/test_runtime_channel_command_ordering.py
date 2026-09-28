"""Commands for one terminal run in arrival order (#745, Augusto nit).

``_serve`` dispatched every CommandFrame with a fresh ``create_task``, so two
INPUTs for the same terminal raced on the thread pool and could reach the pane
out of order (a TEARDOWN could even overtake an earlier INPUT). Terminal-scoped
commands for one terminal are now serialized — each awaits the previous — while
different terminals and runtime-scoped commands (LAUNCH/RUN_SCRIPT) stay
concurrent.
"""

import asyncio
import time
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.runtime_channel.bridge import Bridge
from cli_agent_orchestrator.runtime_channel.protocol import CommandFrame, CommandType

TID = "abcd1234"
TID_B = "beef5678"


def _bridge():
    bridge = Bridge("ws://server/runtime/channel", "worker-1", "tok")

    async def _send(frame):
        return None

    bridge._send = _send
    return bridge


def _input(op_id, terminal_id, message):
    return CommandFrame(
        op_id=op_id, terminal_id=terminal_id, type=CommandType.INPUT, payload={"message": message}
    )


@pytest.mark.asyncio
async def test_two_inputs_to_one_terminal_reach_send_input_in_order():
    from cli_agent_orchestrator.services import terminal_service

    bridge = _bridge()
    order = []

    def fake_send_input(terminal_id, message, sender_id=None, orchestration_type=None):
        if message == "a":
            time.sleep(0.2)  # the first call is slow; b must still wait for it
        order.append(message)
        return True

    with patch.object(terminal_service, "send_input", fake_send_input):
        bridge._dispatch_command(_input("1", TID, "a"))
        bridge._dispatch_command(_input("2", TID, "b"))
        await asyncio.wait_for(bridge._terminal_chains[TID], timeout=5)

    assert order == ["a", "b"]


@pytest.mark.asyncio
async def test_a_slow_command_on_one_terminal_does_not_delay_another():
    from cli_agent_orchestrator.services import terminal_service

    bridge = _bridge()
    order = []

    def fake_send_input(terminal_id, message, sender_id=None, orchestration_type=None):
        if terminal_id == TID:
            time.sleep(0.3)  # the slow terminal
        order.append(terminal_id)
        return True

    with patch.object(terminal_service, "send_input", fake_send_input):
        bridge._dispatch_command(_input("1", TID, "slow"))
        bridge._dispatch_command(_input("2", TID_B, "fast"))
        await asyncio.wait_for(bridge._terminal_chains[TID_B], timeout=5)
        # The other terminal finished while TID was still sleeping.
        assert order == [TID_B]
        await asyncio.wait_for(bridge._terminal_chains[TID], timeout=5)

    assert order == [TID_B, TID]


@pytest.mark.asyncio
async def test_runtime_scoped_commands_are_not_serialized():
    bridge = _bridge()
    started = []

    async def fake_handle(frame):
        started.append(frame.op_id)
        await asyncio.sleep(0.05)

    bridge._handle_command = fake_handle
    # RUN_SCRIPT is runtime-scoped (no terminal_id) and must not be chained.
    bridge._dispatch_command(
        CommandFrame(op_id="s1", terminal_id=None, type=CommandType.RUN_SCRIPT, payload={})
    )
    bridge._dispatch_command(
        CommandFrame(op_id="s2", terminal_id=None, type=CommandType.RUN_SCRIPT, payload={})
    )
    assert bridge._terminal_chains == {}
    await asyncio.sleep(0.1)
    assert set(started) == {"s1", "s2"}


@pytest.mark.asyncio
async def test_a_finished_chain_is_cleaned_up():
    from cli_agent_orchestrator.services import terminal_service

    bridge = _bridge()
    with patch.object(terminal_service, "send_input", lambda *a, **k: True):
        bridge._dispatch_command(_input("1", TID, "a"))
        await asyncio.wait_for(bridge._terminal_chains[TID], timeout=5)
        # The done callback runs after the task completes.
        await asyncio.sleep(0)
    assert TID not in bridge._terminal_chains
