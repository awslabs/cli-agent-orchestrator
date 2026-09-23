"""Tearing down a terminal whose runtime pod was replaced (#745).

An executor's tmux server and local row die with its pod; the central row on the
server's volume does not. So a teardown can arrive for an id the runtime has
never heard of, and the bridge's answer decides whether the caller can make
progress: "nothing here" means the goal state already holds, while "I could not
finish" must keep blocking, because a session that may still be alive cannot be
declared gone.

Conflating the two is what wedged a scheduled flow after its executor restarted —
every later run recycled first, read the failure as a cleanup that might have
left an agent running, and deferred forever.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from cli_agent_orchestrator.runtime_channel.api import remote_delete_terminal
from cli_agent_orchestrator.runtime_channel.bridge import Bridge
from cli_agent_orchestrator.runtime_channel.protocol import (
    CommandFrame,
    CommandOutcome,
    CommandResultFrame,
    CommandType,
)

TERMINAL = "abcd1234"


def _teardown(terminal_id=TERMINAL):
    return CommandFrame(op_id="op1", type=CommandType.TEARDOWN, terminal_id=terminal_id, payload={})


def _bridge_with(deleted, metadata):
    """The two reads the TEARDOWN branch makes, in one place."""
    return patch.multiple(
        "cli_agent_orchestrator.services.terminal_service",
        delete_terminal=MagicMock(return_value=deleted),
        get_terminal_metadata=MagicMock(return_value=metadata),
    )


class TestWhatTheRuntimeReports:
    @pytest.mark.asyncio
    async def test_a_terminal_the_runtime_never_had_is_reported_absent_not_failed(self):
        """The replaced-pod case: no row, no session, nothing to kill."""
        bridge = Bridge("ws://unused", "worker-x", "tok")
        with _bridge_with(deleted=False, metadata=None):
            outcome, payload, terminal_id = await bridge._execute(_teardown())

        assert outcome is CommandOutcome.OK
        assert payload == {"deleted": False, "absent": True}
        assert terminal_id == TERMINAL

    @pytest.mark.asyncio
    async def test_a_deferred_cleanup_is_still_a_failure(self):
        """The row is still here, so the session may be too.

        This is the case the `absent` branch must not swallow: reporting OK would
        let a caller drop the central row and launch a second agent beside a live
        one.
        """
        bridge = Bridge("ws://unused", "worker-x", "tok")
        with _bridge_with(deleted=False, metadata={"tmux_session": "cao-flow-x"}):
            outcome, payload, _ = await bridge._execute(_teardown())

        assert outcome is CommandOutcome.FAILED
        assert payload == {"deleted": False}
        assert "absent" not in payload

    @pytest.mark.asyncio
    async def test_an_ordinary_teardown_is_unchanged(self):
        bridge = Bridge("ws://unused", "worker-x", "tok")
        with _bridge_with(deleted=True, metadata={"tmux_session": "cao-flow-x"}):
            outcome, payload, _ = await bridge._execute(_teardown())

        assert outcome is CommandOutcome.OK
        assert payload == {"deleted": True}


def _result(payload, outcome=CommandOutcome.OK):
    return CommandResultFrame(op_id="op1", terminal_id=TERMINAL, outcome=outcome, payload=payload)


class TestWhatTheServerDoesWithIt:
    @pytest.mark.asyncio
    async def test_absent_settles_the_central_row_and_routing(self, monkeypatch):
        """Otherwise the id is untearable for good: the only runtime that could
        confirm it says it does not have it."""
        db_delete = MagicMock(return_value=True)
        registry = MagicMock()
        monkeypatch.setattr(
            "cli_agent_orchestrator.runtime_channel.api.db_delete_terminal", db_delete
        )
        monkeypatch.setattr("cli_agent_orchestrator.runtime_channel.api.runtime_registry", registry)
        monkeypatch.setattr(
            "cli_agent_orchestrator.runtime_channel.api.remote_terminal_command",
            AsyncMock(return_value=_result({"deleted": False, "absent": True})),
        )

        assert await remote_delete_terminal(TERMINAL) is True
        db_delete.assert_called_once_with(TERMINAL)
        registry.unbind_terminal.assert_called_once_with(TERMINAL, deleted=True)

    @pytest.mark.asyncio
    async def test_an_unknown_outcome_leaves_the_row_alone(self, monkeypatch):
        """A timeout arrives as an exception, not as a payload to interpret."""
        db_delete = MagicMock()
        registry = MagicMock()
        monkeypatch.setattr(
            "cli_agent_orchestrator.runtime_channel.api.db_delete_terminal", db_delete
        )
        monkeypatch.setattr("cli_agent_orchestrator.runtime_channel.api.runtime_registry", registry)
        monkeypatch.setattr(
            "cli_agent_orchestrator.runtime_channel.api.remote_terminal_command",
            AsyncMock(side_effect=HTTPException(status_code=504, detail="outcome unknown")),
        )

        with pytest.raises(HTTPException):
            await remote_delete_terminal(TERMINAL)
        db_delete.assert_not_called()
        registry.unbind_terminal.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_plain_refusal_keeps_the_row(self, monkeypatch):
        """`deleted: False` with no `absent` is the deferred case, end to end."""
        db_delete = MagicMock()
        monkeypatch.setattr(
            "cli_agent_orchestrator.runtime_channel.api.db_delete_terminal", db_delete
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.runtime_channel.api.remote_terminal_command",
            AsyncMock(return_value=_result({"deleted": False})),
        )

        assert await remote_delete_terminal(TERMINAL) is False
        db_delete.assert_not_called()
