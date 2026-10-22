"""Tests for the event-driven InboxService."""

import asyncio
import textwrap
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from cli_agent_orchestrator.backends.base import TerminalNotFoundError
from cli_agent_orchestrator.constants import INBOX_RECONCILE_GRACE_SECONDS, INBOX_RECONCILE_INTERVAL
from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.event_bus import EventBus
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.status_monitor import (
    STALE_PROCESSING_CAPTURE_INTERVAL_S,
    StatusMonitor,
)


class TestCopilotQuietInboxRecovery:
    @pytest.mark.asyncio
    async def test_scheduled_confirmation_reaches_inbox_subscriber(self):
        from types import SimpleNamespace

        from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider

        loop = asyncio.get_running_loop()
        clock = {"now": 100.0}
        timers = []
        sm = StatusMonitor()
        sm._loop = SimpleNamespace(
            call_soon_threadsafe=lambda fn, *args: fn(*args),
            call_later=lambda delay, fn, *args: timers.append((delay, fn, args)) or MagicMock(),
            create_task=loop.create_task,
        )
        sm._apply_detection("term-1", TerminalStatus.PROCESSING)
        sm._buffers["term-1"] = "cursor-addressed redraw"
        sm._buffer_changed_at["term-1"] = 0.0
        provider = CopilotCliProvider("term-1", "s", "w")
        backend = MagicMock()
        backend.supports_event_inbox.return_value = False
        backend.get_native_status.return_value = None
        backend.get_history.return_value = (
            "╻▄▄▄▄▄▄▄▄\n┃\n╹▀▀▀▀▀▀▀▀\n"
            "← open sidebar · Autopilot · Allow All · / commands · tab next tab"
        )
        event_bus = EventBus()
        event_bus.set_loop(loop)
        delivered = asyncio.Event()
        message = _make_message()

        with (
            patch(
                "cli_agent_orchestrator.services.status_monitor.time",
                SimpleNamespace(monotonic=lambda: clock["now"]),
            ),
            patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
            patch(
                "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
                return_value=provider,
            ),
            patch("cli_agent_orchestrator.services.status_monitor.bus", event_bus),
            patch("cli_agent_orchestrator.services.inbox_service.bus", event_bus),
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor", sm),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_pending_messages",
                return_value=[message],
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.list_pending_receiver_ids_older_than",
                return_value=["term-1"],
            ),
            patch("cli_agent_orchestrator.services.inbox_service.update_message_status") as update,
            patch(
                "cli_agent_orchestrator.services.inbox_service.terminal_service.send_input"
            ) as send,
        ):
            send.side_effect = lambda *args: loop.call_soon_threadsafe(delivered.set)
            svc = InboxService()
            subscriber = asyncio.create_task(svc.run())
            try:
                await asyncio.sleep(0)
                svc.reconcile_orphaned_messages()
                send.assert_not_called()
                delay, confirm, args = timers[-1]
                clock["now"] = sm._pending_stale_capture["term-1"][1] + delay
                confirm(*args)
                await asyncio.wait_for(delivered.wait(), timeout=2)
                send.assert_called_once_with("term-1", "hello")
                update.assert_called_once_with(1, MessageStatus.DELIVERED)
            finally:
                subscriber.cancel()
                await asyncio.gather(subscriber, return_exceptions=True)
                event_bus.set_loop(None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("layout", ["prompt", "box"])
    @pytest.mark.parametrize(
        "interruption",
        [
            None,
            "approval",
            "busy",
            "input",
            "output",
            "clear",
            "reset",
            "capture_input",
            "capture_output",
            "rate_limit",
        ],
    )
    async def test_slow_reconciliation_confirms_idle_without_an_external_poller(
        self, layout, interruption
    ):
        from types import SimpleNamespace

        from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider

        clock = {"now": 100.0}
        timers = []
        loop = SimpleNamespace(
            call_soon_threadsafe=lambda fn, *args: fn(*args),
            call_later=lambda delay, fn, *args: timers.append((delay, fn, args)) or MagicMock(),
            create_task=asyncio.create_task,
        )
        sm = StatusMonitor()
        sm._loop = loop
        sm._apply_detection("term-1", TerminalStatus.PROCESSING)
        sm._buffers["term-1"] = "cursor-addressed redraw without logical prompt rows"
        sm._buffer_changed_at["term-1"] = 0.0
        provider = CopilotCliProvider("term-1", "s", "w")
        backend = MagicMock()
        backend.supports_event_inbox.return_value = False
        backend.get_native_status.return_value = None
        composer = "❯\n" if layout == "prompt" else "╻▄▄▄▄▄▄▄▄\n┃\n╹▀▀▀▀▀▀▀▀\n"
        ready = composer + "← open sidebar · Autopilot · Allow All · / commands · tab next tab"
        backend.get_history.return_value = ready
        message = _make_message()
        with (
            patch(
                "cli_agent_orchestrator.services.status_monitor.time",
                SimpleNamespace(monotonic=lambda: clock["now"]),
            ),
            patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
            patch(
                "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
                return_value=provider,
            ),
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor", sm),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_pending_messages",
                return_value=[message],
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.list_pending_receiver_ids_older_than",
                return_value=["term-1"],
            ),
            patch("cli_agent_orchestrator.services.inbox_service.update_message_status") as update,
            patch(
                "cli_agent_orchestrator.services.inbox_service.terminal_service.send_input"
            ) as send,
            patch("cli_agent_orchestrator.services.status_monitor.bus.publish") as publish,
        ):
            svc = InboxService()
            # Reconciliation polls arrive after the confirmation TTL expires.
            svc.reconcile_orphaned_messages()
            send.assert_not_called()
            assert timers, "a quiet ready candidate needs a follow-up within its TTL"
            delay, confirm, args = timers[-1]
            assert delay == STALE_PROCESSING_CAPTURE_INTERVAL_S
            assert delay < INBOX_RECONCILE_INTERVAL
            clock["now"] = sm._pending_stale_capture["term-1"][1] + delay
            if interruption == "approval":
                backend.get_history.return_value = "Allow tool execution? [y/n]\n" + ready
            elif interruption == "busy":
                backend.get_history.return_value = "∙ Thinking (Esc to cancel)\n" + ready
            elif interruption == "input":
                sm.notify_input_sent("term-1")
            elif interruption == "output":
                with patch.object(sm, "_schedule_raw_detection"):
                    sm._process_chunk("term-1", "\x1b[2;1H")
            elif interruption == "clear":
                sm.clear_terminal("term-1")
            elif interruption == "reset":
                sm.reset_buffer("term-1")
            elif interruption in ("capture_input", "capture_output"):

                def straddling_capture(*args, **kwargs):
                    if interruption == "capture_input":
                        sm.notify_input_sent("term-1")
                    else:
                        with patch.object(sm, "_schedule_raw_detection"):
                            sm._process_chunk("term-1", "\x1b[2;1H")
                    return ready

                backend.get_history.side_effect = straddling_capture
            elif interruption == "rate_limit":
                # A concurrent poll used the slot just before the timer fired.
                sm._last_stale_capture_check["term-1"] = clock["now"] - 0.1
            confirm(*args)
            if sm._detect_tasks:
                await asyncio.gather(*list(sm._detect_tasks))
            if interruption == "rate_limit":
                assert sm._last_status["term-1"] == TerminalStatus.PROCESSING
                delay, confirm, args = timers[-1]
                clock["now"] += delay
                confirm(*args)
                await asyncio.gather(*list(sm._detect_tasks))
            if interruption in (None, "rate_limit"):
                assert sm._last_status["term-1"] == TerminalStatus.IDLE
                publish.assert_called_with("terminal.term-1.status", {"status": "idle"})
                svc.deliver_pending("term-1")
                send.assert_called_once_with("term-1", "hello")
                update.assert_called_once_with(1, MessageStatus.DELIVERED)
            else:
                assert sm._last_status.get("term-1") != TerminalStatus.IDLE
                send.assert_not_called()
                update.assert_not_called()


def _make_message(id=1, receiver_id="term-1", message="hello", status=MessageStatus.PENDING):
    return InboxMessage(
        id=id,
        sender_id="sender-1",
        receiver_id=receiver_id,
        message=message,
        status=status,
        created_at=datetime.now(),
    )


class TestCodexEmptyTurnInboxIntegration:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("word_wrapped", [False, True])
    async def test_ack_empty_turn_notice_two_empty_turn_notice_three_deliver_once_in_order(
        self, word_wrapped
    ):
        """A real Codex completion verdict wakes inbox delivery for each notice."""
        from cli_agent_orchestrator.providers.codex import CodexProvider

        loop = asyncio.get_running_loop()
        event_bus = EventBus()
        event_bus.set_loop(loop)
        status_monitor = StatusMonitor()
        status_monitor._last_status["term-1"] = TerminalStatus.IDLE
        provider = CodexProvider("term-1", "session", "window")
        provider._codex_version = (0, 162, 1)
        provider._ends_turns_with_footer = True
        messages = [
            _make_message(1, message="ACK: agente disponible"),
            _make_message(2, message="Segundo aviso: sigo disponible"),
            _make_message(3, message="Tercer aviso: sin tareas pendientes"),
        ]
        message_states = {message.id: MessageStatus.PENDING for message in messages}
        delivered_texts = []
        current_capture = {"text": "› Previous task\n› Ask Codex to do anything\n"}
        sends = asyncio.Queue()

        def completed_capture(message):
            echo = (
                textwrap.fill(message, width=20, initial_indent="› ", subsequent_indent="  ")
                if word_wrapped
                else f"› {message}"
            )
            return (
                f"{echo}\n"
                "  Worked for <1s • 7:46 AM\n"
                "› Ask Codex to do anything\n"
                "  gpt-6 max · /tmp/project                       ⚠ 1 warning · f2 to view"
            )

        def pending(_receiver_id, limit=1):
            return [
                message
                for message in messages
                if message_states[message.id] == MessageStatus.PENDING
            ][:limit]

        def update(message_id, status):
            message_states[message_id] = status

        backend = MagicMock()
        backend.get_history.side_effect = lambda *args, **kwargs: current_capture["text"]

        def send_input(_terminal_id, message):
            delivered_texts.append(message)
            status_monitor.notify_input_sent("term-1")
            provider.notify_status_buffer_reset(len(delivered_texts))
            provider.mark_input_received()
            provider.record_dispatched_message(message)
            status_monitor._apply_detection("term-1", TerminalStatus.PROCESSING)
            loop.call_soon_threadsafe(sends.put_nowait, message)

        service = InboxService()
        with (
            patch("cli_agent_orchestrator.services.inbox_service.bus", event_bus),
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor", status_monitor),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_pending_messages",
                side_effect=pending,
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.update_message_status",
                side_effect=update,
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.terminal_service.send_input",
                side_effect=send_input,
            ),
            patch("cli_agent_orchestrator.services.status_monitor.bus", event_bus),
            patch(
                "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
                return_value=provider,
            ),
            patch("cli_agent_orchestrator.providers.codex.get_backend", return_value=backend),
            patch(
                "cli_agent_orchestrator.services.inbox_service.list_pending_receiver_ids_older_than"
            ) as recovery_poll,
        ):
            subscriber = asyncio.create_task(service.run())
            try:
                await asyncio.sleep(0)  # Let InboxService subscribe to status events.
                await asyncio.to_thread(service.deliver_pending, "term-1")
                first = await asyncio.wait_for(sends.get(), timeout=2)
                assert first == messages[0].message

                for expected in messages[1:]:
                    capture = completed_capture(delivered_texts[-1])
                    current_capture["text"] = capture
                    detected = provider.get_status_from_screen(capture.splitlines())
                    assert detected == TerminalStatus.COMPLETED
                    status_monitor._apply_detection("term-1", detected)

                    delivered = await asyncio.wait_for(sends.get(), timeout=2)
                    assert delivered == expected.message

                final_capture = completed_capture(delivered_texts[-1])
                current_capture["text"] = final_capture
                assert (
                    provider.get_status_from_screen(final_capture.splitlines())
                    == TerminalStatus.COMPLETED
                )

                assert delivered_texts == [message.message for message in messages]
                assert message_states == {
                    message.id: MessageStatus.DELIVERED for message in messages
                }
                recovery_poll.assert_not_called()
            finally:
                subscriber.cancel()
                await asyncio.gather(subscriber, return_exceptions=True)
                event_bus.set_loop(None)


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

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_concurrent_delivery_for_one_terminal_sends_exactly_once(
        self, mock_get, mock_monitor, mock_term_svc, mock_update
    ):
        """deliver_pending is read(PENDING) -> status check -> mark DELIVERED -> send,
        with no atomic claim at the DB layer. Its callers genuinely overlap on worker
        threads (the status-event consumer, the immediate POST path, the OpenCode poller,
        the reconcile sweep), and two calls both reading the same oldest row before
        either marks it would execute one agent task twice. The per-terminal lock must
        serialize the whole sequence: with the mark artificially slowed to hold the
        read->mark window open, two racing threads must produce exactly one send."""
        import threading
        import time

        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        message = _make_message()
        store = {"status": MessageStatus.PENDING}
        store_guard = threading.Lock()

        def read_pending(terminal_id, limit=1):
            with store_guard:
                return [message] if store["status"] == MessageStatus.PENDING else []

        def slow_mark(message_id, status):
            # Hold the read->mark window open long enough that an unserialized second
            # thread would re-read the row as PENDING.
            time.sleep(0.15)
            with store_guard:
                store["status"] = status

        mock_get.side_effect = read_pending
        mock_update.side_effect = slow_mark

        svc = InboxService()
        start = threading.Barrier(2)

        def race():
            start.wait(timeout=5)
            svc.deliver_pending("term-1")

        threads = [threading.Thread(target=race) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert mock_term_svc.send_input.call_count == 1
        assert store["status"] == MessageStatus.DELIVERED


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

    @patch("cli_agent_orchestrator.services.inbox_service.fail_orphaned_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service.list_pending_receiver_ids_older_than")
    def test_sweep_fails_and_logs_messages_whose_receiver_row_is_gone(
        self, mock_list_receivers, mock_fail_orphans, caplog
    ):
        """An undeliverable message stops claiming PENDING and says so (#708)."""
        mock_list_receivers.return_value = []
        mock_fail_orphans.return_value = ["msg-1", "msg-2"]

        svc = InboxService()
        svc.deliver_pending = MagicMock()
        with caplog.at_level("WARNING", logger="cli_agent_orchestrator.services.inbox_service"):
            svc.reconcile_orphaned_messages()

        mock_fail_orphans.assert_called_once_with(INBOX_RECONCILE_GRACE_SECONDS)
        svc.deliver_pending.assert_not_called()
        assert "msg-1, msg-2" in caplog.text
        assert "receiver terminal no longer exists" in caplog.text

    @patch("cli_agent_orchestrator.services.inbox_service.fail_orphaned_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service.list_pending_receiver_ids_older_than")
    def test_sweep_still_delivers_when_the_orphan_pass_fails(
        self, mock_list_receivers, mock_fail_orphans
    ):
        """The orphan pass is bookkeeping; it must never block delivery."""
        mock_list_receivers.return_value = ["receiver-1"]
        mock_fail_orphans.side_effect = RuntimeError("db locked")

        svc = InboxService()
        svc.deliver_pending = MagicMock()
        svc.reconcile_orphaned_messages()

        svc.deliver_pending.assert_called_once_with("receiver-1", registry=None)

    @patch(
        "cli_agent_orchestrator.services.inbox_service.fail_orphaned_pending_messages",
        return_value=[],
    )
    @patch("cli_agent_orchestrator.services.inbox_service.list_pending_receiver_ids_older_than")
    def test_reconciles_stale_receivers(self, mock_list_receivers, mock_fail_orphans):
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

    @patch(
        "cli_agent_orchestrator.services.inbox_service.fail_orphaned_pending_messages",
        return_value=[],
    )
    @patch("cli_agent_orchestrator.services.inbox_service.list_pending_receiver_ids_older_than")
    def test_survives_per_receiver_failure(self, mock_list_receivers, mock_fail_orphans):
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
