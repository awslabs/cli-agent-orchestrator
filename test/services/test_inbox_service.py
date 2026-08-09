"""Tests for the event-driven InboxService."""

import asyncio
import contextlib
import os
import threading
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from cli_agent_orchestrator.backends.base import TerminalNotFoundError
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.constants import INBOX_RECONCILE_GRACE_SECONDS
from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import inbox_service
from cli_agent_orchestrator.services.control_input_contract import (
    ACCEPTED,
    AMBIGUOUS,
    REFUSED,
    contains_bracketed_paste_sentinel,
)
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.managed_launch import ManagedLaunchConflict


def _make_message(id=1, receiver_id="term-1", message="hello", status=MessageStatus.PENDING):
    return InboxMessage(
        id=id,
        sender_id="sender-1",
        receiver_id=receiver_id,
        message=message,
        status=status,
        created_at=datetime.now(),
    )


@pytest.fixture(autouse=True)
def _isolated_wake_store(monkeypatch, tmp_path):
    """Wake-receipt sidecars are written by ``deliver_pending`` now; keep them
    out of the host's state root during tests."""
    from cli_agent_orchestrator.services import wake_receipts

    monkeypatch.setattr(wake_receipts, "WAKE_RECEIPT_DIR", tmp_path / "wake-receipts")


class TestDeliverPending:
    """Tests for InboxService.deliver_pending()."""

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivers_message_when_idle(self, mock_get, mock_monitor, mock_term_svc, mock_update):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE

        svc = InboxService()
        svc.deliver_pending("term-1")

        mock_term_svc.send_input.assert_called_once_with("term-1", "hello")
        mock_update.assert_called_once_with(1, MessageStatus.DELIVERED)

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivers_message_when_completed(
        self, mock_get, mock_monitor, mock_term_svc, mock_update
    ):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.COMPLETED

        svc = InboxService()
        svc.deliver_pending("term-1")

        mock_term_svc.send_input.assert_called_once_with("term-1", "hello")
        mock_update.assert_called_once_with(1, MessageStatus.DELIVERED)

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_skips_when_no_pending_messages(
        self, mock_get, mock_monitor, mock_term_svc, mock_update
    ):
        mock_get.return_value = []

        svc = InboxService()
        svc.deliver_pending("term-1")

        mock_term_svc.send_input.assert_not_called()
        mock_update.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_skips_when_processing(self, mock_get, mock_monitor, mock_term_svc, mock_update):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.PROCESSING

        svc = InboxService()
        svc.deliver_pending("term-1")

        mock_term_svc.send_input.assert_not_called()
        mock_update.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_skips_when_unknown(self, mock_get, mock_monitor, mock_term_svc, mock_update):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.UNKNOWN

        svc = InboxService()
        svc.deliver_pending("term-1")

        mock_term_svc.send_input.assert_not_called()
        mock_update.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivers_multiple_messages_concatenated(
        self, mock_get, mock_monitor, mock_term_svc, mock_update
    ):
        msgs = [_make_message(id=1, message="hello"), _make_message(id=2, message="world")]
        mock_get.return_value = msgs
        mock_monitor.get_status.return_value = TerminalStatus.IDLE

        svc = InboxService()
        svc.deliver_pending("term-1", num_messages=2)

        mock_get.assert_called_once_with("term-1", limit=2)
        mock_term_svc.send_input.assert_called_once_with("term-1", "hello\nworld")
        assert mock_update.call_count == 2

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivers_all_when_num_messages_zero(
        self, mock_get, mock_monitor, mock_term_svc, mock_update
    ):
        msgs = [_make_message(id=i, message=f"msg{i}") for i in range(3)]
        mock_get.return_value = msgs
        mock_monitor.get_status.return_value = TerminalStatus.IDLE

        svc = InboxService()
        svc.deliver_pending("term-1", num_messages=0)

        mock_get.assert_called_once_with("term-1", limit=100)
        mock_term_svc.send_input.assert_called_once_with("term-1", "msg0\nmsg1\nmsg2")
        assert mock_update.call_count == 3

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_marks_failed_on_send_error(self, mock_get, mock_monitor, mock_term_svc, mock_update):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_term_svc.send_input.side_effect = RuntimeError("tmux error")

        svc = InboxService()
        svc.deliver_pending("term-1")

        # Status is set to DELIVERED before send_input (#164), then reset to
        # FAILED when the send raises.
        mock_update.assert_has_calls(
            [
                call(1, MessageStatus.DELIVERED),
                call(1, MessageStatus.FAILED),
            ]
        )
        assert mock_update.call_count == 2

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_marks_delivered_before_send_input(
        self, mock_get, mock_monitor, mock_term_svc, mock_update
    ):
        """Regression for the double-delivery race (#164).

        send_input()'s output flows back through the FIFO/StatusMonitor pipeline
        and can re-emit a status event that re-enters deliver_pending. The
        message must already be DELIVERED by then, so the status update has to
        happen before send_input is called.
        """
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE

        order = []
        mock_update.side_effect = lambda *args, **kwargs: order.append(("update", args))
        mock_term_svc.send_input.side_effect = lambda *args, **kwargs: order.append(("send", args))

        svc = InboxService()
        svc.deliver_pending("term-1")

        assert order[0] == ("update", (1, MessageStatus.DELIVERED))
        assert order[1][0] == "send"

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_resolution_failure_leaves_message_pending(
        self, mock_get, mock_monitor, mock_term_svc, mock_update
    ):
        """A TerminalNotFoundError during send leaves the message PENDING, not FAILED.

        Pane resolution can transiently fail (e.g. herdr pane not yet resolvable).
        Status is optimistically set DELIVERED before send (to close the
        re-entrancy race), so on a resolution failure it must be reset to PENDING
        for a later retry — never left DELIVERED or marked FAILED.
        """
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_term_svc.send_input.side_effect = TerminalNotFoundError("s:w")

        svc = InboxService()
        svc.deliver_pending("term-1")

        # Final status is PENDING (reset after the optimistic DELIVERED), never FAILED.
        assert mock_update.call_args_list[-1] == call(1, MessageStatus.PENDING)
        assert call(1, MessageStatus.FAILED) not in mock_update.call_args_list

    def test_two_concurrent_callers_send_one_pending_message_once(
        self, isolated_memory_db, monkeypatch
    ):
        database.create_terminal(
            "term-race",
            "cao-race",
            "worker",
            "codex",
        )
        message = database.create_inbox_message("sender-1", "term-race", "callback-once")
        real_get_pending = database.get_pending_messages
        both_selected = threading.Barrier(2)

        def synchronized_get(terminal_id, limit=1):
            selected = real_get_pending(terminal_id, limit=limit)
            both_selected.wait(timeout=5)
            return selected

        sends = []
        send_lock = threading.Lock()

        def record_send(terminal_id, text):
            with send_lock:
                sends.append((terminal_id, text))

        monkeypatch.setattr(inbox_service, "get_pending_messages", synchronized_get)
        monkeypatch.setattr(
            inbox_service.managed_launch,
            "managed_control_identity",
            lambda _terminal_id: None,
        )
        monkeypatch.setattr(
            inbox_service.status_monitor,
            "get_status",
            lambda _terminal_id: TerminalStatus.IDLE,
        )
        monkeypatch.setattr(inbox_service.terminal_service, "send_input", record_send)
        monkeypatch.setattr(
            InboxService,
            "_prepare_wake_confirmation",
            lambda *args, **kwargs: None,
        )

        service = InboxService()
        errors = []

        def deliver():
            try:
                service.deliver_pending("term-race")
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        callers = [threading.Thread(target=deliver) for _ in range(2)]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(timeout=5)

        assert not errors
        assert all(not caller.is_alive() for caller in callers)
        assert sends == [("term-race", "callback-once")]
        stored = database.get_inbox_messages("term-race", limit=10)
        assert [(row.id, row.status) for row in stored] == [(message.id, MessageStatus.DELIVERED)]

    def test_stale_callback_recovery_cannot_starve_next_exact_row(self, monkeypatch):
        common = {
            "sender_id": "supervisor",
            "receiver_id": "worker",
            "status": MessageStatus.PENDING,
            "created_at": datetime.now(),
            "sender_generation": "supervisor-generation",
            "expected_receiver_generation": "worker-generation",
            "expected_provider_session_id": "provider-session",
            "expected_execution_mode": "acp",
            "expected_provider": "codex",
        }
        stale = InboxMessage(
            id=1,
            message="stale",
            callback_recovery_key="recovery-stale",
            **common,
        )
        current = InboxMessage(
            id=2,
            message="current",
            callback_recovery_key="recovery-current",
            **common,
        )
        monkeypatch.setattr(
            inbox_service, "get_pending_messages", lambda *_args, **_kwargs: [stale, current]
        )
        monkeypatch.setattr(inbox_service, "is_message_pending", lambda _id: True)
        monkeypatch.setattr(
            inbox_service.managed_launch,
            "managed_control_identity",
            lambda _terminal: {"execution_mode": "acp"},
        )
        checks = iter((False, True))
        monkeypatch.setattr(
            inbox_service.callback_recovery,
            "current_delivery_binding_matches",
            lambda _message: next(checks),
        )
        ambiguous = []
        monkeypatch.setattr(
            inbox_service.callback_recovery,
            "turn_receipt",
            lambda _key: None,
        )
        monkeypatch.setattr(
            inbox_service.callback_recovery,
            "mark_delivery_ambiguous",
            lambda key, **kwargs: ambiguous.append((key, kwargs["reason_code"])),
        )
        bridged = []
        monkeypatch.setattr(
            inbox_service.managed_launch,
            "deliver_inbox_via_bridge",
            lambda terminal, **kwargs: bridged.append((terminal, kwargs)) or True,
        )
        updates = []
        monkeypatch.setattr(
            inbox_service,
            "update_message_status",
            lambda message_id, status: updates.append((message_id, status)) or True,
        )

        # Production calls use the default one-message budget. The scanner
        # must park the held first row and still reach the later valid row.
        InboxService().deliver_pending("worker")

        assert ambiguous == [
            (
                "recovery-stale",
                "source-generation-replaced-manual-resolution-required",
            )
        ]
        assert [item[1]["recovery_operation_key"] for item in bridged] == ["recovery-current"]
        assert updates == [(2, MessageStatus.DELIVERED)]


class TestEagerInboxDelivery:
    """Tests for eager inbox delivery (CAO_EAGER_INBOX_DELIVERY).

    Covers the relaxed status gate in deliver_pending() that allows PROCESSING
    and WAITING_USER_ANSWER delivery when the env var is enabled and the
    provider declares accepts_input_while_processing=True.
    """

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_idle_status_always_works(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_update
    ):
        """IDLE delivers regardless of env var or provider capability."""
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        provider = MagicMock()
        provider.accepts_input_while_processing = False
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", False):
            svc = InboxService()
            svc.deliver_pending("t1")

        mock_term_svc.send_input.assert_called_once()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_completed_status_always_works(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_update
    ):
        """COMPLETED delivers regardless of env var or provider capability."""
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.COMPLETED
        provider = MagicMock()
        provider.accepts_input_while_processing = False
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", False):
            svc = InboxService()
            svc.deliver_pending("t1")

        mock_term_svc.send_input.assert_called_once()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_processing_with_eager_enabled_and_capable_provider(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_update
    ):
        """PROCESSING + eager ON + capable provider -> delivers."""
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.PROCESSING
        provider = MagicMock()
        provider.accepts_input_while_processing = True
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", True):
            svc = InboxService()
            svc.deliver_pending("t1")

        mock_term_svc.send_input.assert_called_once()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_processing_with_eager_enabled_and_non_capable_provider(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_update
    ):
        """PROCESSING + eager ON + non-capable provider -> skips."""
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.PROCESSING
        provider = MagicMock()
        provider.accepts_input_while_processing = False
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", True):
            svc = InboxService()
            svc.deliver_pending("t1")

        mock_term_svc.send_input.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_processing_with_eager_disabled(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_update
    ):
        """PROCESSING + eager OFF -> skips even for capable provider."""
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.PROCESSING
        provider = MagicMock()
        provider.accepts_input_while_processing = True
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", False):
            svc = InboxService()
            svc.deliver_pending("t1")

        mock_term_svc.send_input.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_waiting_user_answer_with_eager_enabled_and_capable_provider(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_update
    ):
        """WAITING_USER_ANSWER + eager ON + capable provider -> delivers."""
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.WAITING_USER_ANSWER
        provider = MagicMock()
        provider.accepts_input_while_processing = True
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", True):
            svc = InboxService()
            svc.deliver_pending("t1")

        mock_term_svc.send_input.assert_called_once()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_error_status_never_delivers(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_update
    ):
        """ERROR -> never delivers regardless of flags."""
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.ERROR
        provider = MagicMock()
        provider.accepts_input_while_processing = True
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", True):
            svc = InboxService()
            svc.deliver_pending("t1")

        mock_term_svc.send_input.assert_not_called()


class TestPollOpenCodePendingMessages:
    """Tests for the OpenCode inbox poller."""

    @patch("cli_agent_orchestrator.services.inbox_service.list_pending_receiver_ids_by_provider")
    def test_polls_pending_opencode_receivers(self, mock_list_receivers):
        """Test poller attempts delivery for each pending OpenCode receiver."""
        mock_list_receivers.return_value = ["receiver-1", "receiver-2"]

        svc = InboxService()
        svc.deliver_pending = MagicMock()
        svc.poll_opencode_pending_messages()

        mock_list_receivers.assert_called_once_with("opencode_cli")
        assert svc.deliver_pending.call_args_list == [
            call("receiver-1", registry=None),
            call("receiver-2", registry=None),
        ]

    @patch("cli_agent_orchestrator.services.inbox_service.list_pending_receiver_ids_by_provider")
    def test_survives_per_receiver_failure(self, mock_list_receivers):
        """Test one failed receiver does not stop the poll loop."""
        mock_list_receivers.return_value = ["receiver-1", "receiver-2"]

        svc = InboxService()
        svc.deliver_pending = MagicMock(side_effect=[Exception("tmux busy"), None])
        svc.poll_opencode_pending_messages()

        assert svc.deliver_pending.call_count == 2


class TestReconcileOrphanedMessages:
    """Tests for the provider-agnostic inbox reconciliation sweep (issue #131)."""

    @patch("cli_agent_orchestrator.services.inbox_service.list_pending_receiver_ids_older_than")
    def test_reconciles_stale_receivers(self, mock_list_receivers):
        """Sweep attempts delivery for each receiver with an orphaned message."""
        mock_list_receivers.return_value = ["receiver-1", "receiver-2"]

        svc = InboxService()
        svc.deliver_pending = MagicMock()
        svc.reconcile_orphaned_messages()

        mock_list_receivers.assert_called_once_with(INBOX_RECONCILE_GRACE_SECONDS)
        assert svc.deliver_pending.call_args_list == [
            call("receiver-1", registry=None),
            call("receiver-2", registry=None),
        ]

    @patch("cli_agent_orchestrator.services.inbox_service.list_pending_receiver_ids_older_than")
    def test_survives_per_receiver_failure(self, mock_list_receivers):
        """One failed receiver does not stop the sweep."""
        mock_list_receivers.return_value = ["receiver-1", "receiver-2"]

        svc = InboxService()
        svc.deliver_pending = MagicMock(side_effect=[Exception("tmux busy"), None])
        svc.reconcile_orphaned_messages()

        assert svc.deliver_pending.call_count == 2


class TestRun:
    """Tests for InboxService.run() event loop."""

    @pytest.mark.asyncio
    async def test_processes_idle_status_event(self):
        svc = InboxService()
        svc.deliver_pending = MagicMock()

        queue = asyncio.Queue()
        await queue.put(
            {
                "topic": "terminal.abc123.status",
                "data": {"status": TerminalStatus.IDLE.value},
            }
        )

        with patch("cli_agent_orchestrator.services.inbox_service.bus") as mock_bus:
            mock_bus.subscribe.return_value = queue

            # Run one iteration then cancel
            async def run_one():
                task = asyncio.create_task(svc.run())
                await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            await run_one()

        svc.deliver_pending.assert_called_once_with("abc123", registry=None)

    @pytest.mark.asyncio
    async def test_processes_completed_status_event(self):
        svc = InboxService()
        svc.deliver_pending = MagicMock()

        queue = asyncio.Queue()
        await queue.put(
            {
                "topic": "terminal.xyz789.status",
                "data": {"status": TerminalStatus.COMPLETED.value},
            }
        )

        with patch("cli_agent_orchestrator.services.inbox_service.bus") as mock_bus:
            mock_bus.subscribe.return_value = queue

            task = asyncio.create_task(svc.run())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        svc.deliver_pending.assert_called_once_with("xyz789", registry=None)

    @pytest.mark.asyncio
    async def test_ignores_processing_status_event(self):
        svc = InboxService()
        svc.deliver_pending = MagicMock()

        queue = asyncio.Queue()
        await queue.put(
            {
                "topic": "terminal.abc123.status",
                "data": {"status": TerminalStatus.PROCESSING.value},
            }
        )

        with patch("cli_agent_orchestrator.services.inbox_service.bus") as mock_bus:
            mock_bus.subscribe.return_value = queue

            task = asyncio.create_task(svc.run())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        svc.deliver_pending.assert_not_called()

    @pytest.mark.asyncio
    async def test_threads_registry_to_delivery(self):
        """run(registry) threads the plugin registry to deliver_pending so
        status-driven deliveries fire PostSendMessageEvent hooks with the same
        attribution as the immediate and OpenCode-poller paths (PR #273 review).
        """
        svc = InboxService()
        svc.deliver_pending = MagicMock()
        registry = MagicMock()

        queue = asyncio.Queue()
        await queue.put(
            {
                "topic": "terminal.abc123.status",
                "data": {"status": TerminalStatus.IDLE.value},
            }
        )

        with patch("cli_agent_orchestrator.services.inbox_service.bus") as mock_bus:
            mock_bus.subscribe.return_value = queue

            task = asyncio.create_task(svc.run(registry))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        svc.deliver_pending.assert_called_once_with("abc123", registry=registry)

    @pytest.mark.asyncio
    async def test_offloads_delivery_to_thread(self):
        """Delivery is offloaded via asyncio.to_thread so the consumer loop keeps
        yielding to the event loop and never blocks StatusMonitor/LogWriter on
        deliver_pending's synchronous DB + tmux I/O (PR #273 review; see the
        threading discipline note in docs/event-driven-architecture.md).
        """
        svc = InboxService()
        svc.deliver_pending = MagicMock()

        queue = asyncio.Queue()
        await queue.put(
            {
                "topic": "terminal.abc123.status",
                "data": {"status": TerminalStatus.IDLE.value},
            }
        )

        with (
            patch("cli_agent_orchestrator.services.inbox_service.bus") as mock_bus,
            patch(
                "cli_agent_orchestrator.services.inbox_service.asyncio.to_thread",
                new_callable=AsyncMock,
            ) as mock_to_thread,
        ):
            mock_bus.subscribe.return_value = queue

            task = asyncio.create_task(svc.run())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        mock_to_thread.assert_awaited_once_with(svc.deliver_pending, "abc123", registry=None)


class TestManagedBridgeDelivery:
    """P1-7 (final conformance §20.2f): a receiver with a live managed
    provider session gets exact provider-native turn delivery (and its
    acknowledgement); anything else uses the ordinary path with NO ack
    inferred from paste."""

    @patch("cli_agent_orchestrator.services.inbox_service.is_message_pending", return_value=True)
    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service.managed_launch")
    def test_managed_receiver_delivers_via_bridge_never_paste(
        self, mock_managed, mock_get, mock_monitor, mock_term_svc, mock_update, mock_pending
    ):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_managed.deliver_inbox_via_bridge.return_value = True

        svc = InboxService()
        svc.deliver_pending("term-1")

        mock_pending.assert_called_once_with(1)
        mock_managed.deliver_inbox_via_bridge.assert_called_once_with(
            "term-1", message_id=1, message="hello", sender_id="sender-1"
        )
        mock_term_svc.send_input.assert_not_called()
        mock_update.assert_called_once_with(1, MessageStatus.DELIVERED)

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service.managed_launch")
    def test_bridge_unavailable_falls_back_to_paste_without_ack(
        self, mock_managed, mock_get, mock_monitor, mock_term_svc, mock_update
    ):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_managed.deliver_inbox_via_bridge.return_value = False
        mock_managed.managed_control_identity.return_value = None

        svc = InboxService()
        svc.deliver_pending("term-1")

        mock_term_svc.send_input.assert_called_once_with("term-1", "hello")
        mock_update.assert_called_once_with(1, MessageStatus.DELIVERED)

    @patch("cli_agent_orchestrator.services.inbox_service.is_message_pending", return_value=True)
    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service.managed_launch")
    def test_managed_bridge_unavailable_preserves_pending_never_pastes(
        self, mock_managed, mock_get, mock_monitor, mock_term_svc, mock_update, mock_pending
    ):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_managed.deliver_inbox_via_bridge.return_value = False
        mock_managed.managed_control_identity.return_value = {
            "terminal_id": "term-1",
            "generation": "generation-1",
            "state": "ready",
            "controllable": True,
        }

        svc = InboxService()
        svc.deliver_pending("term-1")

        mock_pending.assert_called_once_with(1)
        mock_term_svc.send_input.assert_not_called()
        mock_update.assert_not_called()

    def test_two_service_instances_enter_the_managed_bridge_once(
        self, isolated_memory_db, monkeypatch
    ):
        database.create_terminal("term-managed-race", "cao-race", "worker", "codex")
        message = database.create_inbox_message(
            "sender-1",
            "term-managed-race",
            "managed-callback-once",
        )
        real_get_pending = database.get_pending_messages
        both_selected = threading.Barrier(2)

        def synchronized_get(terminal_id, limit=1):
            selected = real_get_pending(terminal_id, limit=limit)
            both_selected.wait(timeout=5)
            return selected

        bridge_calls = []
        bridge_lock = threading.Lock()

        def bridge(*args, **kwargs):
            with bridge_lock:
                bridge_calls.append((args, kwargs))
            return True

        monkeypatch.setattr(inbox_service, "get_pending_messages", synchronized_get)
        monkeypatch.setattr(
            inbox_service.managed_launch,
            "managed_control_identity",
            lambda _terminal_id: {"state": "admitted"},
        )
        monkeypatch.setattr(inbox_service.managed_launch, "deliver_inbox_via_bridge", bridge)

        services = [InboxService(), InboxService()]
        errors = []

        def deliver(service):
            try:
                service.deliver_pending("term-managed-race")
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        callers = [threading.Thread(target=deliver, args=(service,)) for service in services]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(timeout=5)

        assert not errors
        assert all(not caller.is_alive() for caller in callers)
        assert len(bridge_calls) == 1
        stored = database.get_inbox_messages("term-managed-race", limit=10)
        assert [(row.id, row.status) for row in stored] == [(message.id, MessageStatus.DELIVERED)]

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX process semantics")
    @pytest.mark.parametrize("crash_after_ack", [False, True])
    def test_managed_process_death_reconciles_one_provider_effect(
        self, isolated_memory_db, monkeypatch, tmp_path, crash_after_ack
    ):
        database.create_terminal("term-managed-crash", "cao-crash", "worker", "codex")
        message = database.create_inbox_message(
            "sender-1",
            "term-managed-crash",
            "survive-process-death",
        )
        ack_path = tmp_path / "provider-ack"
        effects_path = tmp_path / "provider-effects"
        monkeypatch.setattr(
            inbox_service.managed_launch,
            "managed_control_identity",
            lambda _terminal_id: {"state": "admitted"},
        )

        def crashing_bridge(*args, **kwargs):
            if crash_after_ack:
                effects_path.write_text("effect\n", encoding="utf-8")
                ack_path.write_text("ack\n", encoding="utf-8")
            os._exit(77)

        monkeypatch.setattr(
            inbox_service.managed_launch,
            "deliver_inbox_via_bridge",
            crashing_bridge,
        )
        child = os.fork()
        if child == 0:
            InboxService().deliver_pending("term-managed-crash")
            os._exit(78)
        _, wait_status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(wait_status) == 77
        assert database.get_inbox_messages("term-managed-crash", limit=1)[0].status == (
            MessageStatus.PENDING
        )

        def reconciling_bridge(*args, **kwargs):
            if not ack_path.exists():
                effects_path.write_text("effect\n", encoding="utf-8")
                ack_path.write_text("ack\n", encoding="utf-8")
            return True

        monkeypatch.setattr(
            inbox_service.managed_launch,
            "deliver_inbox_via_bridge",
            reconciling_bridge,
        )
        InboxService().deliver_pending("term-managed-crash")

        assert effects_path.read_text(encoding="utf-8").splitlines() == ["effect"]
        assert database.get_inbox_messages("term-managed-crash", limit=1)[0].status == (
            MessageStatus.DELIVERED
        )
        assert message.id > 0

    def test_managed_refusal_and_exception_stay_pending_until_retry(
        self, isolated_memory_db, monkeypatch
    ):
        database.create_terminal("term-managed-retry", "cao-retry", "worker", "codex")
        first = database.create_inbox_message("sender-1", "term-managed-retry", "refused")
        second = database.create_inbox_message("sender-1", "term-managed-retry", "exception")
        monkeypatch.setattr(
            inbox_service.managed_launch,
            "managed_control_identity",
            lambda _terminal_id: {"state": "admitted"},
        )

        monkeypatch.setattr(
            inbox_service.managed_launch,
            "deliver_inbox_via_bridge",
            lambda *args, **kwargs: False,
        )
        InboxService().deliver_pending("term-managed-retry")
        assert database.get_inbox_messages("term-managed-retry", limit=2)[0].status == (
            MessageStatus.PENDING
        )

        monkeypatch.setattr(
            inbox_service.managed_launch,
            "deliver_inbox_via_bridge",
            lambda *args, **kwargs: True,
        )
        InboxService().deliver_pending("term-managed-retry")
        stored = database.get_inbox_messages("term-managed-retry", limit=2)
        assert [(row.id, row.status) for row in stored] == [
            (first.id, MessageStatus.DELIVERED),
            (second.id, MessageStatus.PENDING),
        ]

        def bridge_error(*args, **kwargs):
            raise RuntimeError("bridge failed before a usable result")

        monkeypatch.setattr(
            inbox_service.managed_launch,
            "deliver_inbox_via_bridge",
            bridge_error,
        )
        InboxService().deliver_pending("term-managed-retry")
        assert database.get_inbox_messages("term-managed-retry", limit=2)[1].status == (
            MessageStatus.PENDING
        )

        monkeypatch.setattr(
            inbox_service.managed_launch,
            "deliver_inbox_via_bridge",
            lambda *args, **kwargs: True,
        )
        InboxService().deliver_pending("term-managed-retry")
        assert database.get_inbox_messages("term-managed-retry", limit=2)[1].status == (
            MessageStatus.DELIVERED
        )

    def test_managed_lock_timeout_leaves_pending_for_a_later_cycle(
        self, isolated_memory_db, monkeypatch
    ):
        database.create_terminal("term-managed-busy", "cao-busy", "worker", "codex")
        message = database.create_inbox_message("sender-1", "term-managed-busy", "later")
        monkeypatch.setattr(
            inbox_service.managed_launch,
            "managed_control_identity",
            lambda _terminal_id: {"state": "admitted"},
        )
        bridge = MagicMock(return_value=True)
        monkeypatch.setattr(
            inbox_service.managed_launch,
            "deliver_inbox_via_bridge",
            bridge,
        )
        monkeypatch.setattr(inbox_service, "MANAGED_DELIVERY_LOCK_TIMEOUT_SECONDS", 0.02)

        lock = InboxService._managed_delivery_lock(message.id)
        lock.acquire()
        try:
            InboxService().deliver_pending("term-managed-busy")
        finally:
            lock.release()

        bridge.assert_not_called()
        assert database.get_inbox_messages("term-managed-busy", limit=1)[0].status == (
            MessageStatus.PENDING
        )

        InboxService().deliver_pending("term-managed-busy")
        bridge.assert_called_once()
        assert database.get_inbox_messages("term-managed-busy", limit=1)[0].status == (
            MessageStatus.DELIVERED
        )


class TestManagedV2InboxDelivery:
    """Ordinary inbox delivery to managed-v2 native workers.

    A live managed-v2 receiver can now persist an ordinary inbox row; delivery
    still resolves the cross-vintage managed identity and routes through the
    provider-native bridge — never the pane (no paste, no Ctrl-S/bracketed-paste
    sentinel). Unavailable delivery stays PENDING and reconciles exactly once;
    a server bounce cannot duplicate a delivered message.
    """

    def _seed_live_v2_terminal(self, terminal_id, provider, generation):
        with database.SessionLocal() as db:
            db.add(
                database.ManagedLaunchV2TerminalModel(
                    id=terminal_id,
                    tmux_session="cao-v2",
                    tmux_window="worker",
                    provider=provider,
                    generation=generation,
                    protocol_vintage="v2",
                    v2_lifecycle_state="live",
                )
            )
            db.commit()

    @staticmethod
    def _managed_identity(terminal_id, provider, generation):
        return {
            "reservation_id": f"rsv-{terminal_id}",
            "terminal_id": terminal_id,
            "generation": generation,
            "provider": provider,
            "state": "admitted",
            "controllable": True,
            "vintage": "v2",
        }

    def _install_bridge_fakes(self, monkeypatch, terminal_id, provider, generation, bridge):
        monkeypatch.setattr(
            inbox_service.managed_launch,
            "managed_control_identity",
            lambda tid: self._managed_identity(tid, provider, generation),
        )
        monkeypatch.setattr(inbox_service.managed_launch, "deliver_inbox_via_bridge", bridge)
        send_input = MagicMock()
        monkeypatch.setattr(inbox_service.terminal_service, "send_input", send_input)
        return send_input

    def test_live_v2_kimi_receiver_delivers_via_native_bridge(
        self, isolated_memory_db, monkeypatch
    ):
        terminal_id = "v2kimi01"
        generation = "gen-kimi-1"
        self._seed_live_v2_terminal(terminal_id, "kimi_cli", generation)
        message = database.create_inbox_message("supervisor-1", terminal_id, "ordinary ping")
        bridge_calls = []

        def bridge(*args, **kwargs):
            bridge_calls.append((args, kwargs))
            return True

        send_input = self._install_bridge_fakes(
            monkeypatch, terminal_id, "kimi_cli", generation, bridge
        )

        InboxService().deliver_pending(terminal_id)

        assert bridge_calls == [
            (
                (terminal_id,),
                {
                    "message_id": message.id,
                    "message": "ordinary ping",
                    "sender_id": "supervisor-1",
                },
            )
        ]
        stored = database.get_inbox_messages(terminal_id, limit=10)
        assert [(row.id, row.status) for row in stored] == [(message.id, MessageStatus.DELIVERED)]
        # No paste and no Ctrl-S/bracketed-paste sentinel ever touches the pane:
        # the managed bridge speaks the provider-native protocol instead.
        send_input.assert_not_called()

    def test_delivery_refuses_identity_that_becomes_cross_vintage_ambiguous(
        self, isolated_memory_db, monkeypatch
    ):
        terminal_id = "dualrace"
        generation = "gen-dual-race"
        database.create_terminal(terminal_id, "cao-v1", "worker", "kimi_cli")
        message = database.create_inbox_message("supervisor-1", terminal_id, "legacy target")

        now = datetime.now().isoformat()
        with database.SessionLocal() as db:
            db.add(
                database.ManagedLaunchV2ReservationModel(
                    reservation_id="rsv-dual-race",
                    terminal_id=terminal_id,
                    generation=generation,
                    protocol_vintage="v2",
                    session_name="cao-v2",
                    provider="kimi_cli",
                    agent_profile="reviewer",
                    caller_id="supervisor-1",
                    working_directory="/tmp",
                    trusted_project_root=None,
                    obligation_generation="obligation-dual-race",
                    task_id="dual-race",
                    run_id="run-dual-race",
                    launch_nonce_digest="0" * 64,
                    state="admitted",
                    request_json="{}",
                    binding_json="{}",
                    execution_mode="acp",
                    execution_mode_source="launch",
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                database.ManagedLaunchV2TerminalModel(
                    id=terminal_id,
                    tmux_session="cao-v2",
                    tmux_window="worker",
                    provider="kimi_cli",
                    generation=generation,
                    protocol_vintage="v2",
                    v2_lifecycle_state="live",
                )
            )
            db.commit()

        bridge = MagicMock()
        monkeypatch.setattr(inbox_service.managed_launch, "deliver_inbox_via_bridge", bridge)

        with pytest.raises(ManagedLaunchConflict, match="ambiguous"):
            InboxService().deliver_pending(terminal_id)

        bridge.assert_not_called()
        stored = database.get_inbox_messages(terminal_id, limit=1)[0]
        assert (stored.id, stored.status) == (message.id, MessageStatus.PENDING)

    def test_live_v2_claude_receiver_delivers_via_native_bridge(
        self, isolated_memory_db, monkeypatch
    ):
        """Provider-neutral: the same path serves managed-v2 Claude identity
        metadata — nothing in create/route branches on the provider string."""
        terminal_id = "v2claude"
        generation = "gen-claude-1"
        self._seed_live_v2_terminal(terminal_id, "claude_code", generation)
        message = database.create_inbox_message("supervisor-1", terminal_id, "ordinary ping")
        bridge_calls = []

        def bridge(*args, **kwargs):
            bridge_calls.append((args, kwargs))
            return True

        send_input = self._install_bridge_fakes(
            monkeypatch, terminal_id, "claude_code", generation, bridge
        )

        InboxService().deliver_pending(terminal_id)

        assert bridge_calls == [
            (
                (terminal_id,),
                {
                    "message_id": message.id,
                    "message": "ordinary ping",
                    "sender_id": "supervisor-1",
                },
            )
        ]
        stored = database.get_inbox_messages(terminal_id, limit=10)
        assert [(row.id, row.status) for row in stored] == [(message.id, MessageStatus.DELIVERED)]
        send_input.assert_not_called()

    def test_bridge_unavailable_stays_pending_then_reconcile_delivers_once(
        self, isolated_memory_db, monkeypatch
    ):
        terminal_id = "v2busy01"
        generation = "gen-busy-1"
        self._seed_live_v2_terminal(terminal_id, "kimi_cli", generation)
        message = database.create_inbox_message("supervisor-1", terminal_id, "queued follow-up")
        bridge_calls = []

        def refusing_bridge(*args, **kwargs):
            bridge_calls.append((args, kwargs))
            return False

        send_input = self._install_bridge_fakes(
            monkeypatch, terminal_id, "kimi_cli", generation, refusing_bridge
        )

        # Busy/unavailable: the row stays PENDING and the pane is never touched.
        InboxService().deliver_pending(terminal_id)
        assert database.get_inbox_messages(terminal_id, limit=1)[0].status == (
            MessageStatus.PENDING
        )
        send_input.assert_not_called()

        # Age the row past the reconcile grace window so the sweep adopts it.
        with database.SessionLocal() as db:
            db.query(database.InboxModel).filter(database.InboxModel.id == message.id).update(
                {
                    database.InboxModel.created_at: datetime.now()
                    - timedelta(seconds=INBOX_RECONCILE_GRACE_SECONDS + 60)
                }
            )
            db.commit()

        bridge_calls.clear()

        def accepting_bridge(*args, **kwargs):
            bridge_calls.append((args, kwargs))
            return True

        monkeypatch.setattr(
            inbox_service.managed_launch, "deliver_inbox_via_bridge", accepting_bridge
        )

        InboxService().reconcile_orphaned_messages()
        assert len(bridge_calls) == 1
        assert database.get_inbox_messages(terminal_id, limit=1)[0].status == (
            MessageStatus.DELIVERED
        )

        # A second sweep over the terminalized row is a no-op.
        InboxService().reconcile_orphaned_messages()
        assert len(bridge_calls) == 1
        send_input.assert_not_called()

    def test_server_bounce_retry_cannot_duplicate_delivered_message(
        self, isolated_memory_db, monkeypatch
    ):
        terminal_id = "v2bounce"
        generation = "gen-bounce-1"
        self._seed_live_v2_terminal(terminal_id, "claude_code", generation)
        message = database.create_inbox_message("supervisor-1", terminal_id, "exactly once")
        bridge_calls = []

        def bridge(*args, **kwargs):
            bridge_calls.append((args, kwargs))
            return True

        send_input = self._install_bridge_fakes(
            monkeypatch, terminal_id, "claude_code", generation, bridge
        )

        # First server incarnation delivers.
        InboxService().deliver_pending(terminal_id)
        assert len(bridge_calls) == 1

        # Server bounce: fresh service instances over the same durable rows.
        # Neither a retried deliver pass nor the reconcile sweep may re-enter
        # the bridge for the already-terminalized exact message id.
        bounced = InboxService()
        bounced.deliver_pending(terminal_id)
        bounced.reconcile_orphaned_messages()
        InboxService().deliver_pending(terminal_id)

        assert len(bridge_calls) == 1
        stored = database.get_inbox_messages(terminal_id, limit=10)
        assert [(row.id, row.status) for row in stored] == [(message.id, MessageStatus.DELIVERED)]
        send_input.assert_not_called()

    def test_cross_vintage_ambiguous_identity_produces_zero_effects(
        self, isolated_memory_db, monkeypatch
    ):
        """Reservation rows in BOTH vintages for one id: the delivery-time
        identity resolver refuses (ManagedLaunchConflict) before any effect."""
        terminal_id = "v2ambg01"
        database.create_terminal(terminal_id, "cao-amb", "worker", "codex")
        message = database.create_inbox_message("supervisor-1", terminal_id, "hello")
        now = datetime.now().isoformat()
        with database.SessionLocal() as db:
            db.add(
                database.ManagedLaunchReservationModel(
                    reservation_id="rsv-v1-amb",
                    terminal_id=terminal_id,
                    generation="gen-v1-amb",
                    session_name="cao-amb",
                    provider="codex",
                    agent_profile="worker",
                    caller_id="test",
                    working_directory="/tmp",
                    state="admitted",
                    request_json="{}",
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                database.ManagedLaunchV2ReservationModel(
                    reservation_id="rsv-v2-amb",
                    terminal_id=terminal_id,
                    generation="gen-v2-amb",
                    session_name="cao-amb",
                    provider="codex",
                    agent_profile="worker",
                    caller_id="test",
                    working_directory="/tmp",
                    obligation_generation="og-1",
                    run_id="run-1",
                    launch_nonce_digest="digest-1",
                    state="admitted",
                    request_json="{}",
                    created_at=now,
                    updated_at=now,
                )
            )
            db.commit()

        bridge = MagicMock(return_value=True)
        monkeypatch.setattr(inbox_service.managed_launch, "deliver_inbox_via_bridge", bridge)
        send_input = MagicMock()
        monkeypatch.setattr(inbox_service.terminal_service, "send_input", send_input)

        with pytest.raises(ManagedLaunchConflict, match="ambiguous"):
            InboxService().deliver_pending(terminal_id)

        bridge.assert_not_called()
        send_input.assert_not_called()
        assert database.get_inbox_messages(terminal_id, limit=1)[0].status == (
            MessageStatus.PENDING
        )
        assert database.get_inbox_messages(terminal_id, limit=1)[0].id == message.id

    def test_stale_generation_conflict_produces_zero_effects(self, isolated_memory_db, monkeypatch):
        """A wrong/current-generation mismatch surfaces as a conflict at
        identity resolution; delivery refuses before any provider effect."""
        terminal_id = "v2genmis"
        generation = "gen-current-1"
        self._seed_live_v2_terminal(terminal_id, "kimi_cli", generation)
        database.create_inbox_message("supervisor-1", terminal_id, "hello")

        def stale_identity(tid):
            raise ManagedLaunchConflict("stale managed terminal generation")

        monkeypatch.setattr(
            inbox_service.managed_launch, "managed_control_identity", stale_identity
        )
        bridge = MagicMock(return_value=True)
        monkeypatch.setattr(inbox_service.managed_launch, "deliver_inbox_via_bridge", bridge)
        send_input = MagicMock()
        monkeypatch.setattr(inbox_service.terminal_service, "send_input", send_input)

        with pytest.raises(ManagedLaunchConflict, match="stale managed terminal generation"):
            InboxService().deliver_pending(terminal_id)

        bridge.assert_not_called()
        send_input.assert_not_called()
        assert database.get_inbox_messages(terminal_id, limit=1)[0].status == (
            MessageStatus.PENDING
        )


def test_ordinary_callback_completion_never_uses_generic_pane_writer(monkeypatch):
    message = InboxMessage(
        id=71,
        sender_id="worker-1",
        receiver_id="supervisor-1",
        message="callback complete",
        status=MessageStatus.PENDING,
        created_at=datetime.now(),
        callback_completion_key="operation-71",
        expected_receiver_generation="supervisor-generation-1",
    )
    claimed = []
    ambiguous = []
    committed = []
    send_input = MagicMock()
    monkeypatch.setattr(
        InboxService,
        "_callback_completion_delivery_claim",
        staticmethod(lambda _messages: contextlib.nullcontext()),
    )
    monkeypatch.setattr(
        inbox_service.callback_recovery,
        "claim_callback_effect",
        lambda *args: claimed.append(args),
    )
    monkeypatch.setattr(
        inbox_service.callback_recovery,
        "mark_callback_effect_ambiguous",
        lambda *args: ambiguous.append(args),
    )
    monkeypatch.setattr(
        inbox_service.callback_recovery,
        "commit_callback_effect",
        lambda *args: committed.append(args),
    )
    monkeypatch.setattr(inbox_service.terminal_service, "send_input", send_input)
    monkeypatch.setattr(
        inbox_service.status_monitor, "get_status", lambda _terminal: TerminalStatus.IDLE
    )

    InboxService()._deliver_callback_completions_via_pane(
        "supervisor-1", [message], registry=None, native_managed=False, managed_identity=None
    )

    assert claimed == [("operation-71", 71)]
    assert ambiguous == [("operation-71",)]
    assert committed == []
    send_input.assert_not_called()


class TestNativeManagedV2InboxDelivery:
    """Ordinary inbox delivery to a native-TUI managed v2 receiver.

    A native-TUI generation has no ACP bridge process, so the managed branch
    dispatches on the reservation's execution mode: ACP generations keep the
    bridge path, native generations fall through to the ordinary idle-gated
    machinery with the pane send performed by the generation-bound native
    text delivery (one exact control per message id, literal bytes only).
    """

    def _seed_live_v2_terminal(self, terminal_id, provider, generation):
        with database.SessionLocal() as db:
            db.add(
                database.ManagedLaunchV2TerminalModel(
                    id=terminal_id,
                    tmux_session="cao-v2",
                    tmux_window="worker",
                    provider=provider,
                    generation=generation,
                    protocol_vintage="v2",
                    v2_lifecycle_state="live",
                )
            )
            db.commit()

    @staticmethod
    def _native_identity(terminal_id, provider, generation):
        return {
            "reservation_id": f"rsv-{terminal_id}",
            "terminal_id": terminal_id,
            "generation": generation,
            "provider": provider,
            "state": "admitted",
            "controllable": True,
            "vintage": "v2",
            "execution_mode": "native_tui",
        }

    def _install_fakes(self, monkeypatch, identity, control, *, status=TerminalStatus.IDLE):
        monkeypatch.setattr(
            inbox_service.managed_launch, "managed_control_identity", lambda tid: identity
        )
        bridge = MagicMock(return_value=True)
        monkeypatch.setattr(inbox_service.managed_launch, "deliver_inbox_via_bridge", bridge)
        send_input = MagicMock()
        monkeypatch.setattr(inbox_service.terminal_service, "send_input", send_input)
        monkeypatch.setattr(
            inbox_service.control_input_service, "deliver_native_inbox_payload", control
        )
        monkeypatch.setattr(inbox_service.status_monitor, "get_status", lambda tid: status)
        return bridge, send_input

    @staticmethod
    def _result(outcome, reason="ok"):
        return SimpleNamespace(outcome=outcome, reason_code=reason)

    def test_idle_native_receiver_delivers_once_via_native_text_path(
        self, isolated_memory_db, monkeypatch
    ):
        terminal_id = "ntv00001"
        generation = "gen-native-1"
        self._seed_live_v2_terminal(terminal_id, "kimi_cli", generation)
        message = database.create_inbox_message("supervisor-1", terminal_id, "ordinary ping")
        control_calls = []

        def control(tid, **kwargs):
            control_calls.append((tid, kwargs))
            return self._result(ACCEPTED)

        bridge, send_input = self._install_fakes(
            monkeypatch, self._native_identity(terminal_id, "kimi_cli", generation), control
        )

        InboxService().deliver_pending(terminal_id)

        assert control_calls == [
            (
                terminal_id,
                {
                    "text": "ordinary ping",
                    "expected_identity": {
                        "terminal_id": terminal_id,
                        "terminal_generation": generation,
                    },
                },
            )
        ]
        bridge.assert_not_called()
        send_input.assert_not_called()
        assert database.get_inbox_messages(terminal_id, limit=1)[0].status == (
            MessageStatus.DELIVERED
        )

    def test_busy_native_receiver_stays_queued_then_delivers_once_at_idle(
        self, isolated_memory_db, monkeypatch
    ):
        terminal_id = "ntv00002"
        generation = "gen-native-2"
        self._seed_live_v2_terminal(terminal_id, "kimi_cli", generation)
        database.create_inbox_message("supervisor-1", terminal_id, "queued follow-up")
        control_calls = []

        def control(tid, **kwargs):
            control_calls.append((tid, kwargs))
            # First pass observes a mid-turn receiver (zero-byte refusal);
            # the later pass observes IDLE.
            if len(control_calls) == 1:
                return self._result(REFUSED, "pane-busy")
            return self._result(ACCEPTED)

        identity = self._native_identity(terminal_id, "kimi_cli", generation)
        monkeypatch.setattr(
            inbox_service.managed_launch, "managed_control_identity", lambda tid: identity
        )
        bridge = MagicMock(return_value=True)
        monkeypatch.setattr(inbox_service.managed_launch, "deliver_inbox_via_bridge", bridge)
        send_input = MagicMock()
        monkeypatch.setattr(inbox_service.terminal_service, "send_input", send_input)
        monkeypatch.setattr(
            inbox_service.control_input_service, "deliver_native_inbox_payload", control
        )
        # Busy: the provider-native observation reports a mid-turn receiver,
        # which the payload path refuses with zero bytes — the row stays
        # queued and nothing is typed mid-turn.  (The FIFO status below is
        # irrelevant on the native path; the observation is the gate.)
        monkeypatch.setattr(
            inbox_service.status_monitor,
            "get_status",
            lambda tid: TerminalStatus.PROCESSING,
        )

        InboxService().deliver_pending(terminal_id)

        assert len(control_calls) == 1
        bridge.assert_not_called()
        send_input.assert_not_called()
        assert database.get_inbox_messages(terminal_id, limit=1)[0].status == (
            MessageStatus.PENDING
        )

        # Idle (a later pass observes IDLE): exactly one accepted delivery,
        # still no paste and no bridge.
        InboxService().deliver_pending(terminal_id)

        assert len(control_calls) == 2
        bridge.assert_not_called()
        send_input.assert_not_called()
        assert database.get_inbox_messages(terminal_id, limit=1)[0].status == (
            MessageStatus.DELIVERED
        )

    def test_bounce_never_redelivers_a_terminalized_row(self, isolated_memory_db, monkeypatch):
        terminal_id = "ntv00003"
        generation = "gen-native-3"
        self._seed_live_v2_terminal(terminal_id, "kimi_cli", generation)
        message = database.create_inbox_message("supervisor-1", terminal_id, "exactly once")
        control_calls = []

        def control(tid, **kwargs):
            control_calls.append((tid, kwargs))
            return self._result(ACCEPTED)

        self._install_fakes(
            monkeypatch, self._native_identity(terminal_id, "kimi_cli", generation), control
        )

        InboxService().deliver_pending(terminal_id)
        assert len(control_calls) == 1
        assert database.get_inbox_messages(terminal_id, limit=1)[0].status == (
            MessageStatus.DELIVERED
        )

        # Server bounce: fresh service instances over the same durable rows.
        bounced = InboxService()
        bounced.deliver_pending(terminal_id)
        bounced.reconcile_orphaned_messages()
        InboxService().deliver_pending(terminal_id)

        assert len(control_calls) == 1
        stored = database.get_inbox_messages(terminal_id, limit=10)
        assert [(row.id, row.status) for row in stored] == [(message.id, MessageStatus.DELIVERED)]

    def test_pre_claim_crash_yields_exactly_one_later_delivery(
        self, isolated_memory_db, monkeypatch
    ):
        terminal_id = "ntv00004"
        generation = "gen-native-4"
        self._seed_live_v2_terminal(terminal_id, "kimi_cli", generation)
        database.create_inbox_message("supervisor-1", terminal_id, "adopt me once")
        control_calls = []

        def control(tid, **kwargs):
            control_calls.append((tid, kwargs))
            return self._result(ACCEPTED)

        self._install_fakes(
            monkeypatch, self._native_identity(terminal_id, "kimi_cli", generation), control
        )

        # The row was never claimed (the first server died before any delivery
        # pass). A fresh incarnation adopts it and delivers exactly once.
        bounced = InboxService()
        bounced.deliver_pending(terminal_id)
        assert len(control_calls) == 1
        bounced.deliver_pending(terminal_id)
        bounced.reconcile_orphaned_messages()
        assert len(control_calls) == 1
        assert database.get_inbox_messages(terminal_id, limit=1)[0].status == (
            MessageStatus.DELIVERED
        )

    def test_native_send_refused_resets_pending_and_retries_without_duplicate(
        self, isolated_memory_db, monkeypatch
    ):
        terminal_id = "ntv00005"
        generation = "gen-native-5"
        self._seed_live_v2_terminal(terminal_id, "kimi_cli", generation)
        message = database.create_inbox_message("supervisor-1", terminal_id, "retry me")
        control_calls = []

        def control(tid, **kwargs):
            control_calls.append((tid, kwargs))
            if len(control_calls) == 1:
                return self._result(REFUSED, "pane-busy")
            return self._result(ACCEPTED)

        bridge, send_input = self._install_fakes(
            monkeypatch, self._native_identity(terminal_id, "kimi_cli", generation), control
        )

        # First pass: the typed refusal proves zero bytes, so the row resets
        # to PENDING (never FAILED, never duplicated).
        InboxService().deliver_pending(terminal_id)
        assert len(control_calls) == 1
        assert database.get_inbox_messages(terminal_id, limit=1)[0].status == (
            MessageStatus.PENDING
        )
        bridge.assert_not_called()
        send_input.assert_not_called()

        # Second pass: the exact control id is retried and accepted once.
        InboxService().deliver_pending(terminal_id)
        assert len(control_calls) == 2
        assert control_calls[1][1]["text"] == "retry me"
        assert database.get_inbox_messages(terminal_id, limit=1)[0].status == (
            MessageStatus.DELIVERED
        )

    def test_native_generation_fence_terminalizes_the_claimed_inbox_row(
        self, isolated_memory_db, monkeypatch
    ):
        terminal_id = "ntv0005f"
        generation = "gen-native-fenced"
        self._seed_live_v2_terminal(terminal_id, "kimi_cli", generation)
        database.create_inbox_message("supervisor-1", terminal_id, "never retarget me")
        control_calls = []

        def control(tid, **kwargs):
            control_calls.append((tid, kwargs))
            return self._result(REFUSED, "generation-fenced")

        self._install_fakes(
            monkeypatch, self._native_identity(terminal_id, "kimi_cli", generation), control
        )

        InboxService().deliver_pending(terminal_id)
        InboxService().deliver_pending(terminal_id)

        assert len(control_calls) == 1
        assert database.get_inbox_messages(terminal_id, limit=1)[0].status == MessageStatus.FAILED

        # Terminalized: no third pass has any effect.
        InboxService().deliver_pending(terminal_id)
        assert len(control_calls) == 1

    def test_stale_native_head_does_not_starve_the_current_generation_row(
        self, isolated_memory_db, monkeypatch
    ):
        """Default one-message delivery scans through failed G1 work to valid G2."""
        terminal_id = "ntv0005e"
        old_generation = "gen-native-old"
        generation = "gen-native-current"
        self._seed_live_v2_terminal(terminal_id, "kimi_cli", generation)
        stale = database.create_inbox_message("supervisor-1", terminal_id, "old G1")
        current = database.create_inbox_message("supervisor-1", terminal_id, "current G2")
        with database.SessionLocal() as db:
            db.query(database.InboxModel).filter_by(id=stale.id).update(
                {"expected_receiver_generation": old_generation}
            )
            db.commit()
        control_calls = []

        def control(tid, **kwargs):
            control_calls.append((tid, kwargs))
            return self._result(ACCEPTED)

        self._install_fakes(
            monkeypatch, self._native_identity(terminal_id, "kimi_cli", generation), control
        )

        InboxService().deliver_pending(terminal_id)

        assert control_calls == [
            (
                terminal_id,
                {
                    "text": "current G2",
                    "expected_identity": {
                        "terminal_id": terminal_id,
                        "terminal_generation": generation,
                    },
                },
            )
        ]
        with database.SessionLocal() as db:
            statuses = {
                row.id: MessageStatus(row.status)
                for row in db.query(database.InboxModel).filter_by(receiver_id=terminal_id).all()
            }
        assert statuses == {stale.id: MessageStatus.FAILED, current.id: MessageStatus.DELIVERED}

    def test_pre_m3_generationless_g1_row_cannot_retarget_g2_after_park(
        self, isolated_memory_db, monkeypatch, tmp_path
    ):
        """Crash-equivalent old rows remain visible terminal history, never G2 input."""
        from cli_agent_orchestrator import constants
        from cli_agent_orchestrator.services import generation_fence

        companion = tmp_path / "companion"
        monkeypatch.setattr(constants, "COMPANION_DIR", companion)
        terminal_id = "ntv000g1"
        g1 = "gen-native-g1"
        g2 = "gen-native-g2"
        self._seed_live_v2_terminal(terminal_id, "kimi_cli", g1)
        # Deliberately bypass the current creation helper: this models a
        # pre-M3 row from before exact receiver-generation binding existed.
        with database.SessionLocal() as db:
            row = database.InboxModel(
                sender_id="supervisor-1",
                receiver_id=terminal_id,
                message="old G1 work",
                status=MessageStatus.PENDING.value,
                expected_receiver_generation=None,
            )
            db.add(row)
            db.commit()
            row_id = row.id
            terminal = (
                db.query(database.ManagedLaunchV2TerminalModel).filter_by(id=terminal_id).one()
            )
            terminal.generation = g2  # G1 parked; G2 is now the live receiver.
            db.commit()
        parked = generation_fence.install_park(
            companion,
            request={
                "schema": generation_fence.PARK_REQUEST_SCHEMA,
                "operation_id": str(uuid.uuid4()),
                "reservation_id": str(uuid.uuid4()),
                "terminal_id": terminal_id,
                "terminal_generation": g1,
                "logical_task_id": "task-g1",
                "retained_round": 0,
                "obligation_generation": "obligation-g1",
                "attempt_id": str(uuid.uuid4()),
                "report_sha256": "a" * 64,
            },
            fencing_token_id="token-g1",
        )
        assert parked["outcome"] == generation_fence.OUTCOME_FENCED
        calls = []

        def control(*args, **kwargs):
            calls.append((args, kwargs))
            return self._result(ACCEPTED)

        self._install_fakes(
            monkeypatch, self._native_identity(terminal_id, "kimi_cli", g2), control
        )
        InboxService().deliver_pending(terminal_id)
        InboxService().reconcile_orphaned_messages()

        stored = next(
            row for row in database.get_inbox_messages(terminal_id, limit=10) if row.id == row_id
        )
        assert calls == []
        assert stored is not None and stored.status == MessageStatus.FAILED

    def test_back_to_back_sender_runs_leave_the_guarded_run_pending(
        self, isolated_memory_db, monkeypatch
    ):
        terminal_id = "ntv00010"
        generation = "gen-native-10"
        self._seed_live_v2_terminal(terminal_id, "kimi_cli", generation)
        first = database.create_inbox_message("sender-a", terminal_id, "first turn")
        second = database.create_inbox_message("sender-b", terminal_id, "guard this turn")
        control_calls = []

        def control(tid, **kwargs):
            control_calls.append((tid, kwargs))
            if len(control_calls) == 1:
                return self._result(ACCEPTED)
            return self._result(REFUSED, "pane-busy")

        self._install_fakes(
            monkeypatch, self._native_identity(terminal_id, "kimi_cli", generation), control
        )

        InboxService().deliver_pending(terminal_id, num_messages=0)

        stored = {row.id: row.status for row in database.get_inbox_messages(terminal_id, limit=10)}
        assert stored[first.id] == MessageStatus.DELIVERED
        assert stored[second.id] == MessageStatus.PENDING
        assert [call[1]["text"] for call in control_calls] == [
            "first turn",
            "guard this turn",
        ]

    def test_native_send_ambiguous_terminalizes_failed_without_replay(
        self, isolated_memory_db, monkeypatch
    ):
        terminal_id = "ntv00006"
        generation = "gen-native-6"
        self._seed_live_v2_terminal(terminal_id, "kimi_cli", generation)
        database.create_inbox_message("supervisor-1", terminal_id, "do not replay")
        control_calls = []

        def control(tid, **kwargs):
            control_calls.append((tid, kwargs))
            return self._result(AMBIGUOUS, "response-lost")

        self._install_fakes(
            monkeypatch, self._native_identity(terminal_id, "kimi_cli", generation), control
        )

        InboxService().deliver_pending(terminal_id)
        assert len(control_calls) == 1
        # Ambiguous is not a zero-byte proof: the row terminalizes under the
        # existing hard-failure semantics and is never retyped blindly.
        assert database.get_inbox_messages(terminal_id, limit=1)[0].status == (MessageStatus.FAILED)

        InboxService().deliver_pending(terminal_id)
        InboxService().reconcile_orphaned_messages()
        assert len(control_calls) == 1

    def test_acp_execution_mode_still_uses_the_bridge(self, isolated_memory_db, monkeypatch):
        terminal_id = "ntv00007"
        self._seed_live_v2_terminal(terminal_id, "kimi_cli", "gen-acp-1")
        identity = self._native_identity(terminal_id, "kimi_cli", "gen-acp-1")
        identity["execution_mode"] = "acp"
        message = database.create_inbox_message("supervisor-1", terminal_id, "bridge me")
        bridge_calls = []

        def bridge(tid, **kwargs):
            bridge_calls.append((tid, kwargs))
            return True

        control = MagicMock(return_value=self._result(ACCEPTED))
        monkeypatch.setattr(
            inbox_service.managed_launch, "managed_control_identity", lambda tid: identity
        )
        monkeypatch.setattr(inbox_service.managed_launch, "deliver_inbox_via_bridge", bridge)
        monkeypatch.setattr(
            inbox_service.control_input_service, "deliver_native_inbox_payload", control
        )
        send_input = MagicMock()
        monkeypatch.setattr(inbox_service.terminal_service, "send_input", send_input)

        InboxService().deliver_pending(terminal_id)

        assert bridge_calls == [
            (
                terminal_id,
                {
                    "message_id": message.id,
                    "message": "bridge me",
                    "sender_id": "supervisor-1",
                },
            )
        ]
        control.assert_not_called()
        send_input.assert_not_called()
        assert database.get_inbox_messages(terminal_id, limit=1)[0].status == (
            MessageStatus.DELIVERED
        )

    def test_legacy_null_execution_mode_keeps_bridge_and_preserve_guard(
        self, isolated_memory_db, monkeypatch
    ):
        terminal_id = "ntv00008"
        self._seed_live_v2_terminal(terminal_id, "kimi_cli", "gen-legacy-1")
        identity = self._native_identity(terminal_id, "kimi_cli", "gen-legacy-1")
        # A reservation row written before the mode contract carries NULL,
        # which must read as legacy ACP — the projection then carries no mode.
        identity["execution_mode"] = None
        database.create_inbox_message("supervisor-1", terminal_id, "preserve me")
        bridge = MagicMock(return_value=False)
        monkeypatch.setattr(
            inbox_service.managed_launch, "managed_control_identity", lambda tid: identity
        )
        monkeypatch.setattr(inbox_service.managed_launch, "deliver_inbox_via_bridge", bridge)
        control = MagicMock(return_value=self._result(ACCEPTED))
        monkeypatch.setattr(
            inbox_service.control_input_service, "deliver_native_inbox_payload", control
        )
        send_input = MagicMock()
        monkeypatch.setattr(inbox_service.terminal_service, "send_input", send_input)

        InboxService().deliver_pending(terminal_id)

        # Bridge attempted and unavailable: the preserve guard parks the row;
        # neither the native text path nor the unmanaged paste runs.
        bridge.assert_called_once()
        control.assert_not_called()
        send_input.assert_not_called()
        assert database.get_inbox_messages(terminal_id, limit=1)[0].status == (
            MessageStatus.PENDING
        )

    def test_native_send_carries_no_bracketed_paste_sentinels(
        self, isolated_memory_db, monkeypatch
    ):
        terminal_id = "ntv00009"
        generation = "gen-native-9"
        self._seed_live_v2_terminal(terminal_id, "kimi_cli", generation)
        database.create_inbox_message(
            "supervisor-1", terminal_id, "line one\nline two, bracket free"
        )
        control_calls = []

        def control(tid, **kwargs):
            control_calls.append((tid, kwargs))
            return self._result(ACCEPTED)

        _, send_input = self._install_fakes(
            monkeypatch, self._native_identity(terminal_id, "kimi_cli", generation), control
        )

        InboxService().deliver_pending(terminal_id)

        assert len(control_calls) == 1
        # Multiline is a proven composer encoding: the payload reaches the
        # native path verbatim, with no framing bytes introduced anywhere.
        sent_text = control_calls[0][1]["text"]
        assert sent_text == "line one\nline two, bracket free"
        assert contains_bracketed_paste_sentinel(sent_text) is False
        assert "\x1b[" not in sent_text
        # The bracket-framing paste path is never used for a managed receiver.
        send_input.assert_not_called()

    def test_two_same_sender_messages_submit_as_one_payload(self, isolated_memory_db, monkeypatch):
        terminal_id = "ntv00011"
        generation = "gen-native-11"
        self._seed_live_v2_terminal(terminal_id, "kimi_cli", generation)
        first = database.create_inbox_message("supervisor-1", terminal_id, "first half")
        second = database.create_inbox_message("supervisor-1", terminal_id, "second half")
        control_calls = []

        def control(tid, **kwargs):
            control_calls.append((tid, kwargs))
            return self._result(ACCEPTED)

        _, send_input = self._install_fakes(
            monkeypatch, self._native_identity(terminal_id, "kimi_cli", generation), control
        )

        # num_messages=0 drains all pending: the same-sender run must be
        # LF-joined and submitted ONCE — a second message is never typed
        # after the first turn has already started.
        InboxService().deliver_pending(terminal_id, num_messages=0)

        assert control_calls == [
            (
                terminal_id,
                {
                    "text": "first half\nsecond half",
                    "expected_identity": {
                        "terminal_id": terminal_id,
                        "terminal_generation": generation,
                    },
                },
            )
        ]
        send_input.assert_not_called()
        stored = database.get_inbox_messages(terminal_id, limit=10)
        assert [(row.id, row.status) for row in stored] == [
            (first.id, MessageStatus.DELIVERED),
            (second.id, MessageStatus.DELIVERED),
        ]

    def test_multi_kb_multiline_payload_delivered_once_verbatim(
        self, isolated_memory_db, monkeypatch
    ):
        terminal_id = "ntv00010"
        generation = "gen-native-10"
        self._seed_live_v2_terminal(terminal_id, "kimi_cli", generation)
        # ≥4 KB of ordinary multiline agent prose — the common case the
        # public single-line control shape would refuse.
        paragraph = "ordinary agent report line with detail and numbers 0123456789\n"
        payload = paragraph * 70  # ~4.9 KB, 70 embedded newlines
        assert len(payload.encode("utf-8")) >= 4096
        database.create_inbox_message("supervisor-1", terminal_id, payload)
        control_calls = []

        def control(tid, **kwargs):
            control_calls.append((tid, kwargs))
            return self._result(ACCEPTED)

        _, send_input = self._install_fakes(
            monkeypatch, self._native_identity(terminal_id, "kimi_cli", generation), control
        )

        InboxService().deliver_pending(terminal_id)

        assert len(control_calls) == 1
        assert control_calls[0][1]["text"] == payload
        send_input.assert_not_called()
        stored = database.get_inbox_messages(terminal_id, limit=1)
        assert stored[0].status == MessageStatus.DELIVERED

        # Terminalized once: a second pass and the reconcile sweep re-type nothing.
        InboxService().deliver_pending(terminal_id)
        InboxService().reconcile_orphaned_messages()
        assert len(control_calls) == 1

    def test_native_path_never_consults_fifo_status(self, isolated_memory_db, monkeypatch):
        terminal_id = "ntv00012"
        generation = "gen-native-12"
        self._seed_live_v2_terminal(terminal_id, "kimi_cli", generation)
        database.create_inbox_message("supervisor-1", terminal_id, "fifo-free delivery")
        control_calls = []

        def control(tid, **kwargs):
            control_calls.append((tid, kwargs))
            return self._result(ACCEPTED)

        bridge, send_input = self._install_fakes(
            monkeypatch, self._native_identity(terminal_id, "kimi_cli", generation), control
        )
        # A native pane is never FIFO-classified, so the native path must not
        # ask the FIFO monitor at all: the idle proof is the provider-native
        # observation under the payload write's own lease (faked above).
        get_status = MagicMock(side_effect=AssertionError("FIFO consulted on native path"))
        monkeypatch.setattr(inbox_service.status_monitor, "get_status", get_status)

        InboxService().deliver_pending(terminal_id)

        assert len(control_calls) == 1
        get_status.assert_not_called()
        bridge.assert_not_called()
        send_input.assert_not_called()
        assert database.get_inbox_messages(terminal_id, limit=1)[0].status == (
            MessageStatus.DELIVERED
        )
