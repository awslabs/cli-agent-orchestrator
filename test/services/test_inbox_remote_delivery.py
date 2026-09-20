"""A delegated result reaches a supervisor whose pane is in another pod (#745).

Observed on EKS before this existed: an elastic worker finished, called
complete_assignment, and the answer arrived at the central server —

    POST /terminals/ef38cd1c/inbox/messages?...&message=757 200 OK
    ERROR Failed to deliver message 1 to ef38cd1c: Session 'cao-2b412d60' not found

The server had queued the message correctly and then typed it into its OWN tmux,
where the supervisor's session does not exist. The message was marked FAILED and
the supervisor never learned the answer, so delegation looked like it worked
(the lease settled) while the result was dropped.

Two halves, tested here: readiness for a remote receiver comes from the runtime
that owns it, and the send is routed there. The routing itself now lives in
``terminal_service.send_input``, so every synchronous sender on the server gets
it; what the inbox adds is classifying a disconnected runtime as retryable.
"""

import asyncio
import threading
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.runtime_channel.protocol import (
    CommandOutcome,
    CommandResultFrame,
    CommandType,
)
from cli_agent_orchestrator.runtime_channel.registry import (
    RuntimeChannelRegistry,
    RuntimeUnavailableError,
)
from cli_agent_orchestrator.services.inbox_service import InboxService

REMOTE = "ef38cd1c"


def _message(id=1, receiver_id=REMOTE, message="757", sender_id="15c1e8e0"):
    return InboxMessage(
        id=id,
        sender_id=sender_id,
        receiver_id=receiver_id,
        message=message,
        status=MessageStatus.PENDING,
        created_at=datetime.now(),
    )


def _ok(success=True):
    return CommandResultFrame(op_id="op-1", outcome=CommandOutcome.OK, payload={"success": success})


@pytest.fixture
def wiring():
    """Patch the module's collaborators, with the terminal already remote."""
    with (
        patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages") as pending,
        patch("cli_agent_orchestrator.services.inbox_service.update_message_status") as update,
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service") as sender,
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.runtime_registry") as registry,
    ):
        pending.return_value = [_message()]
        registry.is_remote.return_value = True
        registry.get_status.return_value = TerminalStatus.IDLE
        sender.send_input.return_value = True
        yield MagicMock(
            pending=pending, update=update, sender=sender, monitor=monitor, registry=registry
        )


class TestARemoteReceiverIsDeliveredTo:
    def test_the_send_carries_the_sender_so_attribution_survives_the_hop(self, wiring):
        InboxService().deliver_pending(REMOTE)

        assert wiring.sender.send_input.call_args.args == (REMOTE, "757")
        assert wiring.sender.send_input.call_args.kwargs == {
            "sender_id": "15c1e8e0",
            "orchestration_type": OrchestrationType.SEND_MESSAGE,
        }

    def test_it_is_marked_delivered(self, wiring):
        InboxService().deliver_pending(REMOTE)

        assert wiring.update.call_args_list[0].args == (1, MessageStatus.DELIVERED)
        assert MessageStatus.FAILED not in [c.args[1] for c in wiring.update.call_args_list]

    def test_readiness_comes_from_the_runtime_not_the_local_detector(self, wiring):
        """The local monitor would probe a tmux socket this process does not own;
        for a remote terminal its answer is about the wrong machine."""
        InboxService().deliver_pending(REMOTE)

        wiring.monitor.get_status.assert_not_called()
        wiring.registry.get_status.assert_called_once_with(REMOTE)

    def test_a_runtime_reporting_unknown_is_not_typed_into(self, wiring):
        """UNKNOWN is what a disconnected runtime reports. Holding the message is
        the point: it stays PENDING and the reconcile sweep retries it."""
        wiring.registry.get_status.return_value = TerminalStatus.UNKNOWN

        InboxService().deliver_pending(REMOTE)

        wiring.sender.send_input.assert_not_called()
        wiring.update.assert_not_called()


class TestFailureIsClassifiedByWhetherRetryCanHelp:
    def test_a_disconnected_runtime_leaves_the_message_pending(self, wiring):
        """A reconnecting runtime is transient — the answer is not lost, it is
        early. FAILED here would silently discard a completed worker's result."""
        wiring.sender.send_input.side_effect = RuntimeUnavailableError(
            "runtime cao-supervisor-0 for terminal ef38cd1c is not connected"
        )

        InboxService().deliver_pending(REMOTE)

        statuses = [c.args[1] for c in wiring.update.call_args_list]
        assert statuses == [MessageStatus.DELIVERED, MessageStatus.PENDING]

    def test_a_runtime_that_refused_the_input_marks_it_failed(self, wiring):
        wiring.sender.send_input.return_value = False

        InboxService().deliver_pending(REMOTE)

        assert wiring.update.call_args_list[-1].args == (1, MessageStatus.FAILED)


class TestTheLocalPathIsUnchanged:
    def test_a_local_receiver_keeps_the_plain_two_argument_send(self, wiring):
        wiring.registry.is_remote.return_value = False
        wiring.monitor.get_status.return_value = TerminalStatus.IDLE

        InboxService().deliver_pending(REMOTE)

        wiring.sender.send_input.assert_called_once_with(REMOTE, "757")
        wiring.registry.get_status.assert_not_called()


class TestTheBlockingHandoffOntoTheChannelLoop:
    """The channel's command futures belong to the server's event loop, and the
    senders that need this run on worker threads (asyncio.to_thread). These are
    the rules that make crossing that boundary safe."""

    def test_a_worker_thread_reaches_the_loop_that_owns_the_channel(self):
        registry = RuntimeChannelRegistry()
        seen = {}

        async def serve():
            registry.register("cao-supervisor-0", lambda _text: asyncio.sleep(0))
            registry.bind_terminal(REMOTE, "cao-supervisor-0")

            async def fake_send(terminal_id, command_type, payload, timeout=60.0):
                seen["loop"] = asyncio.get_running_loop()
                seen["payload"] = payload
                return _ok()

            registry.send_terminal_command = fake_send
            result = await asyncio.to_thread(
                registry.send_terminal_command_blocking,
                REMOTE,
                CommandType.INPUT,
                {"message": "757"},
                5.0,
            )
            assert result.outcome == CommandOutcome.OK
            assert seen["loop"] is asyncio.get_running_loop()
            assert seen["payload"]["message"] == "757"

        asyncio.run(serve())

    def test_calling_it_on_the_loop_thread_is_refused_not_deadlocked(self):
        """Future.result() on the loop thread would block the loop that has to
        deliver the frame — it would hang for the whole timeout and then lie."""
        registry = RuntimeChannelRegistry()

        async def serve():
            registry.register("cao-supervisor-0", lambda _text: asyncio.sleep(0))
            registry.bind_terminal(REMOTE, "cao-supervisor-0")
            with pytest.raises(RuntimeError, match="channel loop"):
                registry.send_terminal_command_blocking(
                    REMOTE, CommandType.INPUT, {"message": "757"}, 5.0
                )

        asyncio.run(serve())

    def test_with_no_channel_ever_connected_it_reports_unavailable(self):
        """A purely local server never registers a runtime, so there is no loop
        to hand off to. That must read as transient-unavailable, not as a crash
        inside the caller."""
        registry = RuntimeChannelRegistry()
        with pytest.raises(RuntimeUnavailableError):
            registry.send_terminal_command_blocking(
                REMOTE, CommandType.INPUT, {"message": "757"}, 5.0
            )

    def test_a_closed_loop_reports_unavailable_too(self):
        """The loop is captured at register() and can be outlived on shutdown."""
        registry = RuntimeChannelRegistry()
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()
        registry._loop = loop

        with pytest.raises(RuntimeUnavailableError):
            registry.send_terminal_command_blocking(
                REMOTE, CommandType.INPUT, {"message": "757"}, 5.0
            )
