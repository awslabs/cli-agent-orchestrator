"""Tests for the event-driven InboxService."""

import asyncio
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.inbox_service import InboxService


def _make_message(id=1, receiver_id="term-1", message="hello", status=MessageStatus.PENDING):
    return InboxMessage(
        id=id,
        sender_id="sender-1",
        receiver_id=receiver_id,
        message=message,
        status=status,
        created_at=datetime.now(),
    )


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

        mock_update.assert_called_once_with(1, MessageStatus.FAILED)


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

        svc.deliver_pending.assert_called_once_with("abc123")

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

        svc.deliver_pending.assert_called_once_with("xyz789")

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
