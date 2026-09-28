"""frozen_memory survives the remote INPUT hop unchanged (haofeif #8).

``send_input(..., frozen_memory=...)`` freezes a workflow's memory context so
the terminal is fed exactly that block — including the empty string, which
means "an intentionally empty context, do not look one up live". The remote
path dropped the field entirely, collapsing None / "" / a real block into the
same None, so a frozen empty context silently became a live memory lookup on
the runtime. These tests pin the value byte-for-byte at each hop.
"""

import asyncio
import base64

import pytest

from cli_agent_orchestrator.runtime_channel.protocol import (
    CommandFrame,
    CommandOutcome,
    CommandResultFrame,
    CommandType,
)

FROZEN_VALUES = [None, "", "FROZEN BLOCK"]


@pytest.mark.parametrize("frozen", FROZEN_VALUES)
def test_send_input_remote_branch_forwards_frozen_memory(frozen):
    """send_input's remote arm must pass frozen_memory to _send_input_remote."""
    from cli_agent_orchestrator.runtime_channel.registry import runtime_registry
    from cli_agent_orchestrator.services import terminal_service

    seen = {}

    def _capture(terminal_id, message, sender_id, orchestration_type, frozen_memory):
        seen["frozen_memory"] = frozen_memory
        seen["present"] = True
        return True

    orig_is_remote = runtime_registry.is_remote
    orig_meta = terminal_service.get_terminal_metadata
    orig_remote = terminal_service._send_input_remote
    runtime_registry.is_remote = lambda tid: True
    terminal_service.get_terminal_metadata = lambda tid: {"id": tid}
    terminal_service._send_input_remote = _capture
    try:
        result = terminal_service.send_input("abcd1234", "hi", frozen_memory=frozen)
    finally:
        runtime_registry.is_remote = orig_is_remote
        terminal_service.get_terminal_metadata = orig_meta
        terminal_service._send_input_remote = orig_remote

    assert result is True
    assert seen.get("present") is True
    # None, "" and a block must remain distinct — not collapsed to None.
    assert seen["frozen_memory"] == frozen
    assert type(seen["frozen_memory"]) is type(frozen)


@pytest.mark.parametrize("frozen", FROZEN_VALUES)
def test_send_input_remote_puts_frozen_memory_in_the_payload(frozen):
    """_send_input_remote must carry frozen_memory in the INPUT command payload."""
    from cli_agent_orchestrator.runtime_channel.registry import runtime_registry
    from cli_agent_orchestrator.services import terminal_service

    seen = {}

    def _capture(terminal_id, ctype, payload, timeout):
        seen["payload"] = payload
        return CommandResultFrame(
            op_id="x", outcome=CommandOutcome.OK, payload={"success": True}, terminal_id=terminal_id
        )

    orig = runtime_registry.send_terminal_command_blocking
    runtime_registry.send_terminal_command_blocking = _capture
    try:
        terminal_service._send_input_remote(
            "abcd1234", "hi", sender_id=None, orchestration_type=None, frozen_memory=frozen
        )
    finally:
        runtime_registry.send_terminal_command_blocking = orig

    assert "frozen_memory" in seen["payload"], "the INPUT payload dropped frozen_memory"
    assert seen["payload"]["frozen_memory"] == frozen


@pytest.mark.parametrize("frozen", FROZEN_VALUES)
@pytest.mark.asyncio
async def test_bridge_input_arm_delivers_frozen_memory_to_send_input(frozen):
    """The bridge's INPUT arm must forward frozen_memory into send_input()."""
    from cli_agent_orchestrator.runtime_channel.bridge import Bridge
    from cli_agent_orchestrator.services import terminal_service

    seen = {}

    def _capture(terminal_id, message, sender_id=None, orchestration_type=None, frozen_memory=None):
        seen["frozen_memory"] = frozen_memory
        seen["present"] = True
        return True

    bridge = Bridge("ws://unused", "worker-x", "tok")
    orig = terminal_service.send_input
    terminal_service.send_input = _capture
    try:
        payload = {"message": "hi", "sender_id": None, "orchestration_type": None}
        # A None frozen block is still an explicit value on v3, so it is always
        # present in the payload; "" and a block likewise.
        payload["frozen_memory"] = frozen
        outcome, result, _ = await bridge._execute(
            CommandFrame(
                op_id="op1", type=CommandType.INPUT, terminal_id="abcd1234", payload=payload
            )
        )
    finally:
        terminal_service.send_input = orig

    assert outcome == CommandOutcome.OK
    assert seen.get("present") is True
    assert seen["frozen_memory"] == frozen
