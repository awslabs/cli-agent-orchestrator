"""A failed LAUNCH is cleaned up OFF the channel's reader loop (#802).

The first version of this awaited the TEARDOWN result inside the websocket
handler's only frame reader — and `RuntimeConnection.resolve`, the sole thing that
completes a `send_command` future, is called from that same loop. So it waited for
a frame only the suspended reader could deliver: the channel stalled for the full
TEARDOWN_TIMEOUT, the wait timed out, the ack was withheld, and the next reconnect
repeated it. The success branch was unreachable by construction.

The cleanup now runs as its own task and the ack is decided from the journal's
`state` column, which is what lets the retry converge. Found by reviewing my own
fix; it had no test coverage at all.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.runtime_channel.protocol import (
    CommandOutcome,
    CommandResultFrame,
)

# A private id. An earlier version used "abcd1234", which other channel test files
# also use — and the cleanup path calls unbind_terminal(deleted=True), TOMBSTONING
# it in the process-wide registry, so eight unrelated tests failed in a full-suite
# run while passing in isolation.
TID = "fade0001"


@pytest.fixture(autouse=True)
def _no_leaked_tasks():
    """The task set is module state; a leak between tests makes assertions lie."""
    import cli_agent_orchestrator.runtime_channel.api as rc

    rc._failed_launch_cleanups.clear()
    yield
    rc._failed_launch_cleanups.clear()


def _failed_launch_frame():
    return CommandResultFrame(
        op_id="op-failed-launch",
        terminal_id=TID,
        outcome=CommandOutcome.FAILED,
        payload={"terminal": {"id": TID}, "error": "provider would not start"},
    )


def _record(state="dispatched"):
    return {
        "op_id": "op-failed-launch",
        "command_type": "launch",
        "runtime_id": "worker-1",
        "terminal_id": None,
        "owner": "alice",
        "run_id": None,
        "step_id": None,
        "engine": None,
        "state": state,
    }


class TestTheAckIsDecidedWithoutBlockingTheReader:
    @pytest.mark.asyncio
    async def test_an_already_settled_entry_acks_immediately(self):
        """This is what makes the retry converge instead of looping forever."""
        import cli_agent_orchestrator.runtime_channel.api as rc

        safe = await rc._confirm_failed_launch_left_nothing(
            _failed_launch_frame(), TID, "worker-1", _record(state="settled")
        )
        assert safe is True

    @pytest.mark.asyncio
    async def test_an_unsettled_entry_withholds_the_ack_and_schedules_cleanup(self):
        import cli_agent_orchestrator.runtime_channel.api as rc

        conn = MagicMock()
        conn.send_command = AsyncMock(
            return_value=CommandResultFrame(
                op_id="td",
                terminal_id=TID,
                outcome=CommandOutcome.OK,
                payload={"deleted": True},
            )
        )
        with (
            patch.object(rc.runtime_registry, "get_runtime", return_value=conn),
            patch.object(rc, "get_terminal_metadata", return_value=None),
            patch.object(rc, "settle_dispatch") as settle,
            patch.object(rc.runtime_registry, "unbind_terminal"),
        ):
            safe = await rc._confirm_failed_launch_left_nothing(
                _failed_launch_frame(), TID, "worker-1", _record()
            )
            assert safe is False, "the ack must wait for confirmed cleanup"
            # The task is scheduled, not awaited inline — that is the whole fix.
            assert rc._failed_launch_cleanups, "no cleanup task was retained"
            await asyncio.gather(*list(rc._failed_launch_cleanups))
            conn.send_command.assert_awaited()
            settle.assert_called_once_with("op-failed-launch")

    @pytest.mark.asyncio
    async def test_the_task_reference_is_held_then_released(self):
        """A discarded handle can be collected mid-flight; the repo's BG-1 rule."""
        import cli_agent_orchestrator.runtime_channel.api as rc

        conn = MagicMock()
        conn.send_command = AsyncMock(
            return_value=CommandResultFrame(
                op_id="td", terminal_id=TID, outcome=CommandOutcome.OK, payload={"absent": True}
            )
        )
        with (
            patch.object(rc.runtime_registry, "get_runtime", return_value=conn),
            patch.object(rc, "get_terminal_metadata", return_value=None),
            patch.object(rc, "settle_dispatch"),
            patch.object(rc.runtime_registry, "unbind_terminal"),
        ):
            await rc._confirm_failed_launch_left_nothing(
                _failed_launch_frame(), TID, "worker-1", _record()
            )
            held = list(rc._failed_launch_cleanups)
            assert len(held) == 1
            await asyncio.gather(*held)
        # Released by the done callback, so the set cannot grow without bound.
        assert not rc._failed_launch_cleanups

    @pytest.mark.asyncio
    async def test_a_gone_runtime_withholds_the_ack_without_scheduling(self):
        import cli_agent_orchestrator.runtime_channel.api as rc

        with patch.object(rc.runtime_registry, "get_runtime", return_value=None):
            safe = await rc._confirm_failed_launch_left_nothing(
                _failed_launch_frame(), TID, "worker-1", _record()
            )
        assert safe is False
        assert not rc._failed_launch_cleanups

    @pytest.mark.asyncio
    async def test_an_unconfirmed_teardown_does_not_settle(self):
        """Not settling is what keeps the result retained for another attempt."""
        import cli_agent_orchestrator.runtime_channel.api as rc

        conn = MagicMock()
        conn.send_command = AsyncMock(
            return_value=CommandResultFrame(
                op_id="td",
                terminal_id=TID,
                outcome=CommandOutcome.OK,
                payload={"deleted": False},  # neither deleted nor absent
            )
        )
        with (
            patch.object(rc.runtime_registry, "get_runtime", return_value=conn),
            patch.object(rc, "settle_dispatch") as settle,
            patch.object(rc.runtime_registry, "unbind_terminal"),
        ):
            await rc._cleanup_failed_launch("op-failed-launch", TID, "worker-1")
            settle.assert_not_called()
