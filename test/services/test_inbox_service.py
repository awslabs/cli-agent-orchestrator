"""Tests for the event-driven InboxService."""

import asyncio
import textwrap
import threading
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from cli_agent_orchestrator.backends.base import TerminalNotFoundError
from cli_agent_orchestrator.constants import (
    INBOX_RECONCILE_GRACE_SECONDS,
    INBOX_RECONCILE_INTERVAL,
    INBOX_SWEEP_FANOUT,
)
from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import (
    TerminalInputBlockedError,
    TerminalInputBlockedReason,
    TerminalStatus,
)
from cli_agent_orchestrator.services.event_bus import EventBus
from cli_agent_orchestrator.services.inbox_service import DELIVERY_DEFERRED, InboxService
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
                "cli_agent_orchestrator.services.inbox_service.terminal_service.dispatch_input"
            ) as send,
            # This test is about the quiet-ready scheduling mechanism in
            # StatusMonitor, not inbox submit confirmation (#78): the mocked
            # StatusMonitor here never earns a real post-dispatch output
            # generation, so confirmation would always burn the full timeout.
            patch("cli_agent_orchestrator.services.inbox_service.INBOX_SUBMIT_CONFIRMATION", False),
        ):
            send.side_effect = lambda *args, **kwargs: loop.call_soon_threadsafe(delivered.set)
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
                send.assert_called_once_with(
                    "term-1", "hello", orchestration_type=OrchestrationType.SEND_MESSAGE
                )
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
                "cli_agent_orchestrator.services.inbox_service.terminal_service.dispatch_input"
            ) as send,
            patch("cli_agent_orchestrator.services.status_monitor.bus.publish") as publish,
            # This test is about the quiet-ready scheduling mechanism in
            # StatusMonitor, not inbox submit confirmation (#78): the mocked
            # StatusMonitor here never earns a real post-dispatch output
            # generation, so confirmation would always burn the full timeout.
            patch("cli_agent_orchestrator.services.inbox_service.INBOX_SUBMIT_CONFIRMATION", False),
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
                send.assert_called_once_with(
                    "term-1", "hello", orchestration_type=OrchestrationType.SEND_MESSAGE
                )
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


class TestInboxSubmitConfirmation:
    """Issue #78: an inbox message whose submitting Enter was swallowed.

    ``send_input`` pastes, sleeps a fixed submit delay, then sends Enter. When
    the receiving TUI swallows that Enter the message sits unsubmitted in the
    composer: nothing raises, the receiver still reads ready, and the inbox has
    already marked the message DELIVERED — so before this fix no path ever
    retried and the sender waited forever.
    """

    @staticmethod
    def _fast_confirm():
        """Shrink the confirmation budget so these tests don't sleep for real."""
        return (
            patch(
                "cli_agent_orchestrator.services.inbox_service.INBOX_SUBMIT_CONFIRM_TIMEOUT", 0.0
            ),
            patch("cli_agent_orchestrator.services.inbox_service.INBOX_SUBMIT_MAX_RESUBMITS", 1),
        )

    def test_swallowed_enter_is_recovered_with_a_bare_enter(self):
        """No pickup after the send → the real recovery path sends a bare Enter."""
        from types import SimpleNamespace

        from cli_agent_orchestrator.services import terminal_service
        from cli_agent_orchestrator.services.status_monitor import StatusObservation

        # The receiver never leaves IDLE and never earns a post-boundary stamp:
        # exactly what a swallowed Enter looks like from the monitor's side.
        sm = MagicMock()
        sm.get_status.return_value = TerminalStatus.IDLE
        sm.output_generation.return_value = 7
        sm.status_observation.return_value = StatusObservation(TerminalStatus.IDLE, 7)

        enters = []
        timeout_patch, resubmit_patch = self._fast_confirm()
        with (
            timeout_patch,
            resubmit_patch,
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor", sm),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_pending_messages",
                return_value=[_make_message()],
            ),
            patch("cli_agent_orchestrator.services.inbox_service.update_message_status"),
            patch(
                "cli_agent_orchestrator.services.inbox_service.terminal_service.dispatch_input",
                return_value=7,
            ),
            # Exercise the real decision helper, stubbing only its two sinks: the
            # pane render it reads and the key it sends.
            patch.object(terminal_service, "_message_in_composer", return_value=True),
            patch.object(
                terminal_service,
                "send_special_key",
                side_effect=lambda tid, key: enters.append((tid, key)),
            ),
            patch.object(
                terminal_service.provider_manager,
                "get_provider",
                return_value=SimpleNamespace(supports_direct_status_probe=False),
            ),
            patch.object(terminal_service, "status_monitor", sm),
        ):
            InboxService().deliver_pending("term-1")

        assert enters == [("term-1", "Enter")]

    def test_confirmed_pickup_sends_no_recovery(self):
        """A receiver that picks the message up must not be poked again."""
        from cli_agent_orchestrator.services.status_monitor import StatusObservation

        sm = MagicMock()
        sm.get_status.return_value = TerminalStatus.IDLE
        sm.output_generation.return_value = 7
        # PROCESSING earned at a generation past the delivery boundary: real
        # evidence that this delivery was accepted.
        sm.status_observation.return_value = StatusObservation(TerminalStatus.PROCESSING, 8)

        timeout_patch, resubmit_patch = self._fast_confirm()
        with (
            timeout_patch,
            resubmit_patch,
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor", sm),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_pending_messages",
                return_value=[_make_message()],
            ),
            patch("cli_agent_orchestrator.services.inbox_service.update_message_status"),
            patch(
                "cli_agent_orchestrator.services.inbox_service.terminal_service.dispatch_input",
                return_value=7,
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.terminal_service"
                ".redeliver_dropped_message_with_boundary"
            ) as redeliver,
        ):
            InboxService().deliver_pending("term-1")

        redeliver.assert_not_called()

    def test_stale_completed_from_a_previous_turn_is_not_pickup_evidence(self):
        """A COMPLETED latched before the send must not confirm this delivery."""
        from cli_agent_orchestrator.services.status_monitor import StatusObservation

        sm = MagicMock()
        sm.get_status.return_value = TerminalStatus.COMPLETED
        sm.output_generation.return_value = 7
        # Right status, pre-boundary stamp — the PR #566 trap.
        sm.status_observation.return_value = StatusObservation(TerminalStatus.COMPLETED, 5)

        timeout_patch, resubmit_patch = self._fast_confirm()
        with (
            timeout_patch,
            resubmit_patch,
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor", sm),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_pending_messages",
                return_value=[_make_message()],
            ),
            patch("cli_agent_orchestrator.services.inbox_service.update_message_status"),
            patch(
                "cli_agent_orchestrator.services.inbox_service.terminal_service.dispatch_input",
                return_value=7,
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.terminal_service"
                ".redeliver_dropped_message_with_boundary",
                return_value=(False, None),
            ) as redeliver,
        ):
            InboxService().deliver_pending("term-1")

        redeliver.assert_called_once()

    def test_eager_delivery_skips_confirmation(self):
        """Eager delivery (#251) targets a PROCESSING receiver; "left ready" proves nothing."""
        from types import SimpleNamespace

        sm = MagicMock()
        sm.get_status.return_value = TerminalStatus.PROCESSING
        sm.output_generation.return_value = 7

        timeout_patch, resubmit_patch = self._fast_confirm()
        with (
            timeout_patch,
            resubmit_patch,
            patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", True),
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor", sm),
            patch(
                "cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
                return_value=SimpleNamespace(accepts_input_while_processing=True),
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_pending_messages",
                return_value=[_make_message()],
            ),
            patch("cli_agent_orchestrator.services.inbox_service.update_message_status"),
            patch("cli_agent_orchestrator.services.inbox_service.terminal_service.dispatch_input"),
            patch(
                "cli_agent_orchestrator.services.inbox_service.terminal_service"
                ".redeliver_dropped_message_with_boundary"
            ) as redeliver,
        ):
            InboxService().deliver_pending("term-1")

        sm.status_observation.assert_not_called()
        redeliver.assert_not_called()

    def test_boundary_comes_from_dispatch_not_a_pre_send_sample(self):
        """Codex review regression (#78): a boundary sampled via a bare
        output_generation() call BEFORE send_input can be exceeded by output
        that landed in the window before send_input's provider work, arm, and
        buffer clear -- output that has nothing to do with this delivery --
        falsely confirming a swallowed Enter. The boundary must instead come
        from dispatch_input's return value, sampled after the arm and clear.
        """
        from cli_agent_orchestrator.services.status_monitor import StatusObservation

        sm = MagicMock()
        sm.get_status.return_value = TerminalStatus.IDLE
        # If the boundary were (wrongly) taken from a bare pre-send
        # output_generation() call, it would see this stale, lower value.
        sm.output_generation.return_value = 5
        # The real dispatch boundary (dispatch_input's return, sampled after
        # arm + clear) already absorbed this generation -- it is pre-dispatch
        # output, not progress from THIS delivery's Enter.
        sm.status_observation.return_value = StatusObservation(TerminalStatus.PROCESSING, 8)

        timeout_patch, resubmit_patch = self._fast_confirm()
        with (
            timeout_patch,
            resubmit_patch,
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor", sm),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_pending_messages",
                return_value=[_make_message()],
            ),
            patch("cli_agent_orchestrator.services.inbox_service.update_message_status"),
            patch(
                "cli_agent_orchestrator.services.inbox_service.terminal_service.dispatch_input",
                return_value=8,
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.terminal_service"
                ".redeliver_dropped_message_with_boundary",
                return_value=(False, None),
            ) as redeliver,
        ):
            InboxService().deliver_pending("term-1")

        # Confirmed only if the boundary were (wrongly) 5: 8 > 5. The fix uses
        # dispatch_input's returned boundary of 8, so 8 > 8 is False and
        # recovery must run.
        redeliver.assert_called_once()
        sm.output_generation.assert_not_called()

    def test_event_inbox_backend_skips_confirmation(self):
        """Event-inbox backends (herdr) push delivery synchronously inline on
        their own asyncio event loop thread (herdr_inbox_service.py's
        _event_loop -> _deliver, with no asyncio.to_thread offload -- see the
        TODO at herdr_inbox_service.py:1276-1277). Running the blocking
        confirmation wait there would stall that session's whole event loop
        for up to the confirm timeout on every delivery. Confirmation must be
        skipped entirely for these backends until that offload exists.
        """
        from cli_agent_orchestrator.services.status_monitor import StatusObservation

        sm = MagicMock()
        sm.get_status.return_value = TerminalStatus.IDLE
        # No pickup evidence at all -- a tmux backend would read this as a
        # dropped Enter and run recovery.
        sm.status_observation.return_value = StatusObservation(TerminalStatus.IDLE, 0)

        backend = MagicMock()
        backend.supports_event_inbox.return_value = True

        with (
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor", sm),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_backend", return_value=backend
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_pending_messages",
                return_value=[_make_message()],
            ),
            patch("cli_agent_orchestrator.services.inbox_service.update_message_status"),
            patch(
                "cli_agent_orchestrator.services.inbox_service.terminal_service.dispatch_input",
                return_value=0,
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.terminal_service"
                ".redeliver_dropped_message_with_boundary"
            ) as redeliver,
        ):
            InboxService().deliver_pending("term-1")

        sm.status_observation.assert_not_called()
        redeliver.assert_not_called()


class TestHerdrAppInboxSubmitConfirmation:
    """herdr-app: herdr-core submits 200 ms after the paste, whatever the provider asks.

    herdr-core's terminal.send waits a fixed PASTE_GAP (200 ms; a template's
    paste_gap_ms is the only override, input.rs) before Enter, and CAO cannot
    ask for more per send. Claude Code asks for 2.0 s because a too-early
    Enter is swallowed. Inbox deliveries on herdr-app run off the event loop
    (HerdrAppInboxService uses asyncio.to_thread), so they run the submit
    confirmation and resend the Enter. The herdr TUI backend still skips it.
    """

    @pytest.fixture
    def herdr_app(self, monkeypatch):
        from test.backends.fake_hc import FakeHc

        from cli_agent_orchestrator.backends.herdr_app_backend import HerdrAppBackend

        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.mcp_resolution.cao_server_env", lambda: {}
        )
        return HerdrAppBackend("cao", client=FakeHc())

    def _deliver(self, backend, sm, provider, order=None):
        from cli_agent_orchestrator.services import terminal_service

        enters = []
        boundary = MagicMock(return_value=0)
        if order is not None:
            sm.get_status.side_effect = lambda tid: order.append("status") or sm._status
            boundary.side_effect = lambda *a, **k: order.append("dispatch") or 0
        with (
            patch(
                "cli_agent_orchestrator.services.inbox_service.INBOX_SUBMIT_CONFIRM_TIMEOUT", 0.0
            ),
            patch("cli_agent_orchestrator.services.inbox_service.INBOX_SUBMIT_MAX_RESUBMITS", 1),
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor", sm),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_backend", return_value=backend
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_pending_messages",
                return_value=[_make_message()],
            ),
            patch("cli_agent_orchestrator.services.inbox_service.update_message_status"),
            patch(
                "cli_agent_orchestrator.services.inbox_service.terminal_service.dispatch_input",
                boundary,
            ),
            # The real recovery helper; only the composer read and the key sink are stubbed.
            patch.object(terminal_service, "_message_in_composer", return_value=True),
            patch.object(
                terminal_service,
                "send_special_key",
                side_effect=lambda tid, key: enters.append((tid, key)),
            ),
            patch.object(terminal_service.provider_manager, "get_provider", return_value=provider),
            patch.object(terminal_service, "status_monitor", sm),
        ):
            InboxService().deliver_pending("term-1")
        return enters

    @staticmethod
    def _monitor(status, started=False):
        from cli_agent_orchestrator.services.status_monitor import StatusObservation

        sm = MagicMock()
        sm._status = status
        sm.get_status.return_value = status
        sm.output_generation.return_value = 0
        # Event-inbox backends report no output generation.
        sm.status_observation.return_value = StatusObservation(status, None)
        sm.started_on_last_input.return_value = started
        return sm

    @staticmethod
    def _claude_code():
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        provider = ClaudeCodeProvider("term-1", "cao-a", "w1")
        assert provider.paste_submit_delay == 2.0  # herdr-core gives it 0.2 s
        return provider

    def test_swallowed_enter_is_resent_for_claude_code(self, herdr_app):
        """COMPLETED before the send, COMPLETED after, no native activity: Enter again."""
        from cli_agent_orchestrator.services import terminal_service

        assert herdr_app.supports_event_inbox() and herdr_app.event_inbox_confirms_submit()
        sm = self._monitor(TerminalStatus.COMPLETED)
        with patch.object(terminal_service, "_worker_is_started_direct", return_value=False):
            enters = self._deliver(herdr_app, sm, self._claude_code())
        assert enters == [("term-1", "Enter")]

    def test_pre_dispatch_status_is_read_before_the_paste(self, herdr_app):
        order = []
        sm = self._monitor(TerminalStatus.IDLE)
        provider = MagicMock(supports_direct_status_probe=False)
        self._deliver(herdr_app, sm, provider, order=order)
        # The gate's read, then the transition baseline, then the paste.
        assert order[:3] == ["status", "status", "dispatch"]

    @pytest.mark.parametrize(
        "status, started",
        [
            (TerminalStatus.PROCESSING, False),  # working now
            (TerminalStatus.COMPLETED, True),  # a short turn already over: core said working
        ],
    )
    def test_pickup_sends_no_recovery(self, herdr_app, status, started):
        sm = self._monitor(status, started=started)
        sm.get_status.side_effect = [TerminalStatus.COMPLETED, TerminalStatus.COMPLETED]
        enters = self._deliver(herdr_app, sm, MagicMock(supports_direct_status_probe=False))
        assert enters == []

    def test_transition_from_pre_dispatch_status_is_pickup(self, herdr_app):
        """IDLE before the send and COMPLETED after: the turn started and ended."""
        sm = self._monitor(TerminalStatus.COMPLETED)
        sm.get_status.side_effect = [TerminalStatus.IDLE, TerminalStatus.IDLE]
        enters = self._deliver(herdr_app, sm, MagicMock(supports_direct_status_probe=False))
        assert enters == []

    def test_herdr_tui_keeps_skipping_confirmation(self):
        from cli_agent_orchestrator.backends.base import TerminalBackend
        from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend
        from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend

        # Its inbox service delivers on the event loop, where the wait would block.
        assert (
            HerdrBackend.event_inbox_confirms_submit is TerminalBackend.event_inbox_confirms_submit
        )
        assert (
            TmuxBackend.event_inbox_confirms_submit is TerminalBackend.event_inbox_confirms_submit
        )
        assert TerminalBackend.event_inbox_confirms_submit(MagicMock()) is False


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

        def dispatch_input(_terminal_id, message, **_kwargs):
            delivered_texts.append(message)
            status_monitor.notify_input_sent("term-1")
            provider.notify_status_buffer_reset(len(delivered_texts))
            provider.mark_input_received()
            provider.record_dispatched_message(message)
            status_monitor._apply_detection("term-1", TerminalStatus.PROCESSING)
            loop.call_soon_threadsafe(sends.put_nowait, message)
            return len(delivered_texts)

        service = InboxService()
        with (
            # This test is about empty-turn ack ORDERING, not submit confirmation
            # (#78). Its StatusMonitor runs on a frozen fake clock and produces no
            # output chunks, so no post-boundary status stamp can ever be earned
            # and every delivery would pay the full confirmation timeout.
            patch("cli_agent_orchestrator.services.inbox_service.INBOX_SUBMIT_CONFIRMATION", False),
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
                "cli_agent_orchestrator.services.inbox_service.terminal_service.dispatch_input",
                side_effect=dispatch_input,
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

    @pytest.fixture(autouse=True)
    def _no_submit_confirmation(self):
        """These tests mock status_monitor bare, with no status_observation()
        configured, so the default-on submit confirmation (#78) would poll a
        mock that never satisfies pickup and burn the real 8s timeout on every
        test here. None of these tests are about that confirmation path.
        """
        with patch(
            "cli_agent_orchestrator.services.inbox_service.INBOX_SUBMIT_CONFIRMATION", False
        ):
            yield

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_empty_inbox_returns_zero(self, mock_get, mock_monitor, mock_term_svc, mock_update):
        mock_get.return_value = []
        assert InboxService().deliver_pending("term-1") == 0
        mock_term_svc.dispatch_input.assert_not_called()
        mock_update.assert_not_called()

    @pytest.mark.parametrize(
        "reason, expected_status, expected_result",
        [
            (
                TerminalInputBlockedReason.WAITING_USER_ANSWER,
                MessageStatus.PENDING,
                DELIVERY_DEFERRED,
            ),
            (TerminalInputBlockedReason.IDENTITY_PENDING, MessageStatus.PENDING, DELIVERY_DEFERRED),
            (TerminalInputBlockedReason.PROVIDER_ERROR, MessageStatus.FAILED, 0),
        ],
    )
    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_refused_send_return_contract(
        self,
        mock_get,
        mock_monitor,
        mock_term_svc,
        mock_update,
        reason,
        expected_status,
        expected_result,
    ):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_term_svc.dispatch_input.side_effect = TerminalInputBlockedError(
            "Input refused", reason=reason
        )
        assert InboxService().deliver_pending("term-1") == expected_result
        assert mock_update.call_args_list == [
            call(1, MessageStatus.DELIVERED),
            call(1, expected_status),
        ]

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivered_count_survives_transient_refusal(
        self, mock_get, mock_monitor, mock_term_svc, mock_update
    ):
        first = _make_message(id=1)
        second = _make_message(id=2)
        second.sender_id = "sender-2"
        mock_get.return_value = [first, second]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_term_svc.dispatch_input.side_effect = [None, TerminalNotFoundError("s:w")]
        assert InboxService().deliver_pending("term-1", num_messages=0) == 1
        assert mock_term_svc.dispatch_input.call_count == 2
        assert mock_update.call_args_list == [
            call(1, MessageStatus.DELIVERED),
            call(2, MessageStatus.DELIVERED),
            call(2, MessageStatus.PENDING),
        ]

    @patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", False)
    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_locked_delivery_returns_deferred_when_gate_closed(
        self, mock_get, mock_monitor, mock_term_svc, mock_update
    ):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.PROCESSING

        assert InboxService()._deliver_pending_locked("term-1", 1, None) == DELIVERY_DEFERRED

        mock_term_svc.dispatch_input.assert_not_called()
        mock_update.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivers_message_when_idle(self, mock_get, mock_monitor, mock_term_svc, mock_update):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE

        svc = InboxService()
        svc.deliver_pending("term-1")

        mock_term_svc.dispatch_input.assert_called_once_with(
            "term-1", "hello", orchestration_type=OrchestrationType.SEND_MESSAGE
        )
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

        mock_term_svc.dispatch_input.assert_called_once_with(
            "term-1", "hello", orchestration_type=OrchestrationType.SEND_MESSAGE
        )
        mock_update.assert_called_once_with(1, MessageStatus.DELIVERED)

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_codex_background_completion_releases_pending_message(
        self, mock_get, mock_monitor, mock_term_svc, mock_update
    ):
        from cli_agent_orchestrator.providers.codex import CodexProvider

        provider = CodexProvider("term-1", "session", "window")
        provider._codex_version = (0, 169, 0)
        provider._ends_turns_with_footer = True
        chrome = [
            "",
            "1 background terminal running · /ps to view · /stop to close",
            "",
            "",
            "› Ask Codex to do anything",
            "",
            "gpt-6 max · /tmp/project",
            "← for agents · ? for shortcuts · ⚠ 1 warning · f2 to view",
        ]
        mock_get.return_value = [_make_message()]
        svc = InboxService()
        mock_monitor.get_status.return_value = provider.get_status_from_screen(
            ["› 1+1", "• 2", *chrome]
        )
        svc.deliver_pending("term-1")
        mock_term_svc.dispatch_input.assert_not_called()
        mock_update.assert_not_called()

        mock_monitor.get_status.return_value = provider.get_status_from_screen(
            ["› 1+1", "• 2", "Worked for 1s • 3:43 AM", *chrome]
        )
        svc.deliver_pending("term-1")
        mock_term_svc.dispatch_input.assert_called_once_with(
            "term-1", "hello", orchestration_type=OrchestrationType.SEND_MESSAGE
        )
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

        mock_term_svc.dispatch_input.assert_not_called()
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

        mock_term_svc.dispatch_input.assert_not_called()
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

        mock_term_svc.dispatch_input.assert_not_called()
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
        mock_term_svc.dispatch_input.assert_called_once_with(
            "term-1", "hello\nworld", orchestration_type=OrchestrationType.SEND_MESSAGE
        )
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
        mock_term_svc.dispatch_input.assert_called_once_with(
            "term-1", "msg0\nmsg1\nmsg2", orchestration_type=OrchestrationType.SEND_MESSAGE
        )
        assert mock_update.call_count == 3

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_marks_failed_on_send_error(self, mock_get, mock_monitor, mock_term_svc, mock_update):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_term_svc.dispatch_input.side_effect = RuntimeError("tmux error")

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
        mock_term_svc.dispatch_input.side_effect = lambda *args, **kwargs: order.append(
            ("send", args)
        )

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
        mock_term_svc.dispatch_input.side_effect = TerminalNotFoundError("s:w")

        svc = InboxService()
        assert svc.deliver_pending("term-1") == DELIVERY_DEFERRED

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

        assert mock_term_svc.dispatch_input.call_count == 1
        assert store["status"] == MessageStatus.DELIVERED


class TestEagerInboxDelivery:
    """Tests for eager inbox delivery (CAO_EAGER_INBOX_DELIVERY).

    Covers the relaxed status gate in deliver_pending() that allows PROCESSING
    and WAITING_USER_ANSWER delivery when the env var is enabled and the
    provider declares accepts_input_while_processing=True.
    """

    @pytest.fixture(autouse=True)
    def _no_submit_confirmation(self):
        """The IDLE/COMPLETED cases here go through the non-eager confirmation
        path with status_monitor mocked bare, so without this they would burn
        the real 8s timeout. See TestDeliverPending's fixture of the same name.
        """
        with patch(
            "cli_agent_orchestrator.services.inbox_service.INBOX_SUBMIT_CONFIRMATION", False
        ):
            yield

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

        mock_term_svc.dispatch_input.assert_called_once()

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

        mock_term_svc.dispatch_input.assert_called_once()

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

        mock_term_svc.dispatch_input.assert_called_once()

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

        mock_term_svc.dispatch_input.assert_not_called()

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

        mock_term_svc.dispatch_input.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_waiting_user_answer_holds_for_dialog_blocking_provider(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_update
    ):
        """WAITING_USER_ANSWER + eager ON + dialog-blocking provider -> holds (#896).

        Claude Code's WAITING_USER_ANSWER means an AskUserQuestion/approval
        dialog is open; its Ink Select widget consumes pasted text as the answer
        and the trailing Enter submits the highlighted option. Eagerly
        delivering there loses the message AND answers a question a human was
        supposed to answer, so the message must stay PENDING for the IDLE /
        COMPLETED consumer (or the reconcile sweep) to pick up later.
        """
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.WAITING_USER_ANSWER
        provider = MagicMock()
        provider.accepts_input_while_processing = True
        provider.blocks_orchestrated_input_while_waiting_user_answer = True
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", True):
            svc = InboxService()
            svc.deliver_pending("t1")

        mock_term_svc.dispatch_input.assert_not_called()
        # Held, not consumed: the optimistic DELIVERED mark must not be written.
        mock_update.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_waiting_user_answer_delivers_for_non_blocking_provider(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_update
    ):
        """WAITING_USER_ANSWER + eager ON + provider that does NOT block -> delivers.

        The gate is the provider's own
        ``blocks_orchestrated_input_while_waiting_user_answer`` capability, not a
        blanket ban: a TUI that keeps a separate input box alive next to its
        prompt can still take the message mid-dialog.
        """
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.WAITING_USER_ANSWER
        provider = MagicMock()
        provider.accepts_input_while_processing = True
        provider.blocks_orchestrated_input_while_waiting_user_answer = False
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", True):
            svc = InboxService()
            svc.deliver_pending("t1")

        mock_term_svc.dispatch_input.assert_called_once()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_held_dialog_message_delivers_once_the_terminal_is_idle(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_update
    ):
        """A message held during WAITING_USER_ANSWER is delivered on the next attempt."""
        mock_get.return_value = [_make_message()]
        provider = MagicMock()
        provider.accepts_input_while_processing = True
        provider.blocks_orchestrated_input_while_waiting_user_answer = True
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", True):
            svc = InboxService()
            mock_monitor.get_status.return_value = TerminalStatus.WAITING_USER_ANSWER
            svc.deliver_pending("t1")
            mock_term_svc.dispatch_input.assert_not_called()

            # Human answered the dialog; the terminal finished its turn.
            mock_monitor.get_status.return_value = TerminalStatus.IDLE
            svc.deliver_pending("t1")

        mock_term_svc.dispatch_input.assert_called_once()

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

        mock_term_svc.dispatch_input.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_dispatch_input_blocked_dialog_refusal_stays_pending_without_resampling_status(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_update
    ):
        """dispatch_input's dialog guard refuses mid-send -> PENDING, decided from
        the exception's own reason rather than a second status read (#896).

        At the merge base there was no dedicated handler at all: the generic
        ``except Exception`` marked the batch FAILED. An intermediate revision of
        this fix added a handler that re-sampled status_monitor.get_status() to
        decide PENDING vs FAILED. On herdr that call shells out to the herdr CLI
        and returns UNKNOWN on any error, which would misclassify this exact,
        provably-transient refusal as FAILED and strand the message permanently
        -- the outcome the handler exists to prevent. Call count asserted at 1
        proves the decision does not depend on a second, unreliable read.
        """
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_term_svc.dispatch_input.side_effect = TerminalInputBlockedError(
            "Terminal t1 is waiting for a user answer.",
            reason=TerminalInputBlockedReason.WAITING_USER_ANSWER,
        )

        svc = InboxService()
        svc.deliver_pending("t1")

        assert mock_monitor.get_status.call_count == 1
        mock_update.assert_called_with(1, MessageStatus.PENDING)

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_dispatch_input_blocked_provider_error_refusal_still_fails(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_update
    ):
        """dispatch_input's dead-provider guard refuses mid-send -> FAILED (terminal,
        no sweep can ever deliver it), decided from the exception's reason."""
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_term_svc.dispatch_input.side_effect = TerminalInputBlockedError(
            "Terminal t1 provider is in ERROR state.",
            reason=TerminalInputBlockedReason.PROVIDER_ERROR,
        )

        svc = InboxService()
        svc.deliver_pending("t1")

        mock_update.assert_called_with(1, MessageStatus.FAILED)


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
            call("receiver-1", registry=None, source="opencode-poll"),
            call("receiver-2", registry=None, source="opencode-poll"),
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

        svc.deliver_pending.assert_called_once_with(
            "receiver-1", registry=None, source="reconcile-sweep"
        )

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
            call("receiver-1", registry=None, source="reconcile-sweep"),
            call("receiver-2", registry=None, source="reconcile-sweep"),
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


class TestDeliveryIsolation:
    """PR #1004 review, P2-2: one unconfirmed delivery must not stall the rest.

    The submit confirmation (#78) can hold a terminal's delivery lock for a
    full confirmation cycle. That wait belongs to the one terminal that swallowed
    its Enter; the status-event consumer, the sweeps and the inbox POST must not
    sit behind it.
    """

    @staticmethod
    def _idle_event(terminal_id):
        return {"topic": f"terminal.{terminal_id}.status", "data": {"status": "idle"}}

    @pytest.mark.asyncio
    async def test_one_stalled_delivery_does_not_delay_another_terminal(self):
        """term-A's delivery blocks (unconfirmed submit); term-B is still delivered."""
        import threading
        import time

        release_a = threading.Event()
        delivered = {}

        def deliver(terminal_id, registry=None, source=None):
            delivered[terminal_id] = time.monotonic()
            if terminal_id == "term-A":
                release_a.wait(5)

        svc = InboxService()
        svc.deliver_pending = MagicMock(side_effect=deliver)
        queue = asyncio.Queue()
        await queue.put(self._idle_event("term-A"))
        await queue.put(self._idle_event("term-B"))

        with patch("cli_agent_orchestrator.services.inbox_service.bus") as mock_bus:
            mock_bus.subscribe.return_value = queue
            task = asyncio.create_task(svc.run())
            try:
                deadline = time.monotonic() + 2.0
                while "term-B" not in delivered and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                # B was dispatched while A was still blocked inside its delivery.
                assert "term-B" in delivered
                assert not release_a.is_set()
            finally:
                release_a.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_events_for_an_in_flight_terminal_coalesce_into_one_recheck(self):
        """Three IDLE events while term-A's delivery is in flight: one re-check,
        run after the first finishes (same ordering the serial loop gave), not a
        thread per event queued behind the lock."""
        import threading
        import time

        release = threading.Event()
        calls = []
        started = threading.Event()

        def deliver(terminal_id, registry=None, source=None):
            calls.append(time.monotonic())
            if len(calls) == 1:
                started.set()
                release.wait(5)

        svc = InboxService()
        svc.deliver_pending = MagicMock(side_effect=deliver)
        queue = asyncio.Queue()
        await queue.put(self._idle_event("term-A"))

        with patch("cli_agent_orchestrator.services.inbox_service.bus") as mock_bus:
            mock_bus.subscribe.return_value = queue
            task = asyncio.create_task(svc.run())
            try:
                await asyncio.to_thread(started.wait, 2)
                for _ in range(3):
                    await queue.put(self._idle_event("term-A"))
                await asyncio.sleep(0.1)
                assert len(calls) == 1, "a re-check ran while the first delivery held the lock"
                release.set()
                deadline = time.monotonic() + 2.0
                while len(calls) < 2 and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.1)
                assert len(calls) == 2
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_delivery_error_is_logged_and_the_loop_keeps_consuming(self):
        svc = InboxService()
        svc.deliver_pending = MagicMock(side_effect=[RuntimeError("boom"), None])
        queue = asyncio.Queue()
        await queue.put(self._idle_event("term-A"))
        await queue.put(self._idle_event("term-B"))

        with (
            patch("cli_agent_orchestrator.services.inbox_service.bus") as mock_bus,
            patch("cli_agent_orchestrator.services.inbox_service.logger") as mock_logger,
        ):
            mock_bus.subscribe.return_value = queue
            task = asyncio.create_task(svc.run())
            await asyncio.sleep(0.2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert svc.deliver_pending.call_count == 2
        assert any("boom" in str(c) for c in mock_logger.error.call_args_list)

    def test_after_dispatch_fires_after_the_paste_and_before_confirmation(self):
        order = []
        sm = MagicMock()
        sm.get_status.return_value = TerminalStatus.IDLE

        svc = InboxService()
        with (
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor", sm),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_pending_messages",
                return_value=[_make_message()],
            ),
            patch("cli_agent_orchestrator.services.inbox_service.update_message_status"),
            patch(
                "cli_agent_orchestrator.services.inbox_service.terminal_service.dispatch_input",
                side_effect=lambda *a, **k: order.append("paste") or 7,
            ),
            patch("cli_agent_orchestrator.services.inbox_service.get_backend") as backend,
            patch.object(
                svc, "_confirm_submitted", side_effect=lambda *a, **k: order.append("confirm")
            ),
        ):
            backend.return_value.supports_event_inbox.return_value = False
            svc.deliver_pending("term-1", after_dispatch=lambda: order.append("after_dispatch"))

        assert order == ["paste", "after_dispatch", "confirm"]

    def test_after_dispatch_fires_once_even_when_nothing_is_pending(self):
        fired = []
        with patch(
            "cli_agent_orchestrator.services.inbox_service.get_pending_messages", return_value=[]
        ):
            InboxService().deliver_pending("term-1", after_dispatch=lambda: fired.append(1))
        assert fired == [1]

    def test_after_dispatch_fires_once_when_delivery_raises(self):
        fired = []
        with patch(
            "cli_agent_orchestrator.services.inbox_service.get_pending_messages",
            side_effect=RuntimeError("db down"),
        ):
            with pytest.raises(RuntimeError):
                InboxService().deliver_pending("term-1", after_dispatch=lambda: fired.append(1))
        assert fired == [1]

    @pytest.mark.parametrize(
        "sweep, lister",
        [
            (
                "reconcile_orphaned_messages",
                "cli_agent_orchestrator.services.inbox_service.list_pending_receiver_ids_older_than",
            ),
            (
                "poll_opencode_pending_messages",
                "cli_agent_orchestrator.services.inbox_service.list_pending_receiver_ids_by_provider",
            ),
        ],
    )
    def test_sweep_does_not_wait_on_a_stalled_receiver_before_the_next(self, sweep, lister):
        """The sweep fans out: term-B is delivered while term-A is still blocked."""
        import threading
        import time

        release_a = threading.Event()
        delivered = {}

        def deliver(terminal_id, registry=None, source=None):
            delivered[terminal_id] = time.monotonic()
            if terminal_id == "term-A":
                release_a.wait(5)

        svc = InboxService()
        svc.deliver_pending = MagicMock(side_effect=deliver)

        def run_sweep():
            with (
                patch(lister, return_value=["term-A", "term-B"]),
                patch(
                    "cli_agent_orchestrator.services.inbox_service.fail_orphaned_pending_messages",
                    return_value=[],
                ),
            ):
                getattr(svc, sweep)()

        worker = threading.Thread(target=run_sweep)
        worker.start()
        try:
            deadline = time.monotonic() + 2.0
            while "term-B" not in delivered and time.monotonic() < deadline:
                time.sleep(0.01)
            assert "term-B" in delivered
            assert not release_a.is_set()
        finally:
            release_a.set()
            worker.join(5)
        assert svc.deliver_pending.call_count == 2

    def test_sweep_survives_one_receiver_failing_in_the_pool(self):
        svc = InboxService()
        svc.deliver_pending = MagicMock(side_effect=[RuntimeError("boom"), None])
        with (
            patch(
                "cli_agent_orchestrator.services.inbox_service.list_pending_receiver_ids_older_than",
                return_value=["term-A", "term-B"],
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.fail_orphaned_pending_messages",
                return_value=[],
            ),
        ):
            svc.reconcile_orphaned_messages()
        assert svc.deliver_pending.call_count == 2


class TestInboxRecoveryNeverDuplicates:
    """PR #1004 review, P2-3: the inbox recovery must never deliver a message twice.

    These drive the REAL ``deliver_pending`` -> ``dispatch_input`` -> recovery
    helper with real provider classes. Only the DB, the pane and the status
    monitor are fakes. The pane models #78: the first Enter is swallowed and the
    pasted text stays in the composer, so a full re-send would join two copies
    and the next Enter would submit both.
    """

    FIXTURES = Path(__file__).resolve().parents[1] / "providers" / "fixtures"
    MESSAGE = "Run the migration once."

    class _Pane:
        def __init__(self, screen):
            self.screen = screen
            self.composer, self.submitted, self.enters = "", [], 0

        def send_keys(self, session, window, text, enter_count=1, **kw):
            self.composer += text
            self.press_enter()

        def press_enter(self):
            self.enters += 1
            if self.enters == 1:
                return  # Enter swallowed: the text stays in the composer (#78)
            self.submitted.append(self.composer)
            self.composer = ""

        def get_history(self, *a, **k):
            return self.screen

        def supports_event_inbox(self):
            return False

    @staticmethod
    def _make_provider(cls):
        import inspect

        params = list(inspect.signature(cls.__init__).parameters.values())[1:]
        args = ["term-1", "s", "w"]
        for extra in params[3:]:
            if extra.default is inspect.Parameter.empty and extra.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.POSITIONAL_ONLY,
            ):
                args.append("developer")
        return cls(*args)

    def _deliver(self, provider, provider_type, screen, caplog=None):
        import sys

        from cli_agent_orchestrator.services import terminal_service as ts
        from cli_agent_orchestrator.services.status_monitor import StatusObservation

        pane = self._Pane(screen)
        message = _make_message(message=self.MESSAGE)
        sm = MagicMock()
        sm.get_status.return_value = TerminalStatus.IDLE
        sm.output_generation.return_value = 7
        sm.probe_execution_evidence.return_value = False
        # A submit that reaches the agent earns a post-boundary PROCESSING stamp;
        # until then the receiver reads IDLE at the dispatch generation.
        sm.status_observation.side_effect = lambda tid: (
            StatusObservation(TerminalStatus.PROCESSING, 100)
            if pane.submitted
            else StatusObservation(TerminalStatus.IDLE, 7)
        )
        events = []
        provider_module = sys.modules[type(provider).__module__]
        with (
            patch(
                "cli_agent_orchestrator.services.inbox_service.INBOX_SUBMIT_CONFIRM_TIMEOUT", 0.0
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_pending_messages",
                return_value=[message],
            ),
            patch("cli_agent_orchestrator.services.inbox_service.update_message_status"),
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor", sm),
            patch("cli_agent_orchestrator.services.inbox_service.get_backend", return_value=pane),
            patch.object(ts, "status_monitor", sm),
            patch.object(ts, "get_backend", return_value=pane),
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={"tmux_session": "s", "tmux_window": "w", "provider": provider_type},
            ),
            patch.object(ts.provider_manager, "get_provider", return_value=provider),
            patch.object(ts, "inject_memory_context", side_effect=lambda m, *a: m),
            patch.object(ts, "update_last_active"),
            patch.object(ts, "send_special_key", side_effect=lambda tid, key: pane.press_enter()),
            patch.object(
                ts,
                "dispatch_plugin_event",
                side_effect=lambda reg, name, ev: events.append(name),
            ),
            patch.object(provider_module, "get_backend", return_value=pane, create=True),
        ):
            InboxService().deliver_pending("term-1", registry=MagicMock())
        return pane, events

    @pytest.mark.parametrize(
        "provider_type, fixture",
        [
            ("opencode_cli", "opencode_cli_idle_splash.txt"),
            ("mcode", "minimax_code_idle.txt"),
        ],
    )
    def test_probe_capable_provider_without_a_composer_reader_gets_no_second_paste(
        self, provider_type, fixture, caplog
    ):
        """OpenCode and MiniMax: a probe, no extract_current_composer. The
        helper used to treat "not started" as "paste dropped" and re-pasted the
        whole message on top of the first copy."""
        import logging

        from cli_agent_orchestrator.providers.manager import ProviderManager

        provider = self._make_provider(ProviderManager.provider_class(provider_type))
        screen = (self.FIXTURES / fixture).read_text()
        with caplog.at_level(logging.WARNING):
            pane, events = self._deliver(provider, provider_type, screen)

        assert pane.submitted == [], "nothing may be submitted twice or on top of the first paste"
        assert pane.composer == self.MESSAGE, "the single paste stays where it was"
        assert events.count("post_send_message") == 1
        assert "composer cannot be read" in caplog.text

    @pytest.mark.parametrize("provider_type", [t.value for t in ProviderType])
    def test_no_provider_receives_an_inbox_message_twice_in_one_prompt(self, provider_type):
        """The class, enumerated: for every registered provider, a swallowed
        Enter followed by the recovery never yields two copies of the message
        in one submission and never fires post_send_message twice."""
        from cli_agent_orchestrator.providers.manager import ProviderManager

        provider = self._make_provider(ProviderManager.provider_class(provider_type))
        pane, events = self._deliver(provider, provider_type, "")

        for submitted in pane.submitted:
            assert submitted.count(self.MESSAGE) <= 1, f"{provider_type} got the message twice"
        assert pane.composer.count(self.MESSAGE) <= 1, f"{provider_type} composer holds two copies"
        assert events.count("post_send_message") == 1, f"{provider_type} fired two plugin events"

    def test_opencode_retained_completion_marker_is_not_pickup_evidence(self, caplog):
        """The previous turn's ``▣ … · Ns`` marker is still on screen. The
        probe reads COMPLETED, but that completion is not this delivery's."""
        import logging

        from cli_agent_orchestrator.providers.opencode_cli import OpenCodeCliProvider

        provider = OpenCodeCliProvider("term-1", "s", "w")
        screen = (self.FIXTURES / "opencode_cli_completed.txt").read_text()
        assert provider.get_status(screen) == TerminalStatus.COMPLETED, "fixture precondition"
        with caplog.at_level(logging.WARNING):
            pane, events = self._deliver(provider, "opencode_cli", screen)

        assert pane.submitted == []
        assert pane.composer == self.MESSAGE
        # Before the fix the probe's COMPLETED ended the recovery as "confirmed"
        # with no log at all. Now the helper gets past the probe and records
        # that it could not recover.
        assert "composer cannot be read" in caplog.text, "a silent false confirmation is the bug"


class TestSwallowedEnterRecoveryOnRealComposers:
    """PR #1004 review, P2-1: the #78 pile-up for claude_code and kiro_cli.

    Same real-code harness as TestInboxRecoveryNeverDuplicates, but the pane
    renders the REAL capture of each TUI holding the unsubmitted MSG1 (see
    fixtures/COMPOSER_FIXTURES.md) while the paste sits in its composer. With a
    composer reader the recovery sends the bare Enter, MSG1 is submitted on its
    own, and MSG2 lands in an empty composer -- instead of the {MSG1}{MSG2}
    result the issue reports.
    """

    FIXTURES = TestInboxRecoveryNeverDuplicates.FIXTURES
    MSG1 = "MSG1: refactor module A so the retry policy lives in one place"
    MSG2 = "MSG2: write tests for module B"

    class _Pane(TestInboxRecoveryNeverDuplicates._Pane):
        """Shows the captured 'MSG1 unsubmitted' frame while MSG1 is in the composer."""

        def __init__(self, idle_screen, msg1_screen, msg1):
            super().__init__(idle_screen)
            self.msg1_screen, self.msg1 = msg1_screen, msg1

        def get_history(self, *a, **k):
            return self.msg1_screen if self.composer == self.msg1 else self.screen

    @pytest.mark.parametrize(
        "provider_type, idle_fixture, composer_fixture",
        [
            (
                "claude_code",
                "claude_code_865_startup_idle.txt",
                "claude_code_composer_single_line.txt",
            ),
            (
                "kiro_cli",
                "kiro_cli_tui_2_25_idle_output.txt",
                "kiro_cli_tui_composer_single_line.txt",
            ),
        ],
    )
    def test_two_messages_arrive_as_two_prompts_not_one_pile_up(
        self, provider_type, idle_fixture, composer_fixture
    ):
        import sys

        from cli_agent_orchestrator.providers.manager import ProviderManager
        from cli_agent_orchestrator.services import terminal_service as ts
        from cli_agent_orchestrator.services.status_monitor import StatusObservation
        from cli_agent_orchestrator.utils.text import strip_terminal_escapes

        provider = TestInboxRecoveryNeverDuplicates._make_provider(
            ProviderManager.provider_class(provider_type)
        )
        pane = self._Pane(
            strip_terminal_escapes((self.FIXTURES / idle_fixture).read_text()),
            (self.FIXTURES / composer_fixture).read_text(),
            self.MSG1,
        )
        queue = [_make_message(1, message=self.MSG1), _make_message(2, message=self.MSG2)]
        sm = MagicMock()
        sm.get_status.return_value = TerminalStatus.IDLE
        sm.output_generation.return_value = 7
        sm.probe_execution_evidence.return_value = False
        sm.status_observation.side_effect = lambda tid: (
            StatusObservation(TerminalStatus.PROCESSING, 100)
            if pane.submitted
            else StatusObservation(TerminalStatus.IDLE, 7)
        )

        def pending(tid, limit=1):
            return queue[:limit]

        def mark(message_id, status):
            if status == MessageStatus.DELIVERED:
                queue[:] = [m for m in queue if m.id != message_id]

        provider_module = sys.modules[type(provider).__module__]
        with (
            patch(
                "cli_agent_orchestrator.services.inbox_service.INBOX_SUBMIT_CONFIRM_TIMEOUT", 0.0
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_pending_messages",
                side_effect=pending,
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.update_message_status",
                side_effect=mark,
            ),
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor", sm),
            patch("cli_agent_orchestrator.services.inbox_service.get_backend", return_value=pane),
            patch.object(ts, "status_monitor", sm),
            patch.object(ts, "get_backend", return_value=pane),
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={"tmux_session": "s", "tmux_window": "w", "provider": provider_type},
            ),
            patch.object(ts.provider_manager, "get_provider", return_value=provider),
            patch.object(ts, "inject_memory_context", side_effect=lambda m, *a: m),
            patch.object(ts, "update_last_active"),
            patch.object(ts, "send_special_key", side_effect=lambda tid, key: pane.press_enter()),
            patch.object(ts, "dispatch_plugin_event"),
            patch.object(provider_module, "get_backend", return_value=pane, create=True),
        ):
            svc = InboxService()
            svc.deliver_pending("term-1", registry=MagicMock())  # MSG1, Enter swallowed
            # The reconcile sweep / next status event delivers MSG2 later.
            sm.status_observation.side_effect = lambda tid: (
                StatusObservation(TerminalStatus.PROCESSING, 200)
                if len(pane.submitted) == 2
                else StatusObservation(TerminalStatus.IDLE, 7)
            )
            # MSG2's own Enter lands (only MSG1's frame is captured for real).
            svc.deliver_pending("term-1", registry=MagicMock())  # MSG2

        assert pane.submitted == [self.MSG1, self.MSG2], "each message must reach the agent alone"
        assert pane.composer == ""


class TestCodexNoStatusSubmitRecovery:
    def test_contiguous_status_path_does_not_submit_unrelated_draft(self):
        from cli_agent_orchestrator.providers.codex import CodexProvider
        from cli_agent_orchestrator.services import terminal_service as ts

        message = "/tmp/project"
        provider = CodexProvider("term-1", "s", "w")
        pane = MagicMock()
        pane.get_history.return_value = (
            "› pending unrelated task\n"
            "  gpt-6 max · /tmp/project                       ⚠ 1 warning · f2 to view\n"
            "  ? for shortcuts\n"
        )
        with (
            patch.object(ts, "get_backend", return_value=pane),
            patch.object(
                ts, "get_terminal_metadata", return_value={"tmux_session": "s", "tmux_window": "w"}
            ),
            patch.object(ts.provider_manager, "get_provider", return_value=provider),
            patch.object(ts, "send_special_key") as send_key,
            patch.object(ts, "dispatch_input") as dispatch,
        ):
            assert ts._message_in_composer("term-1", message) is None
            assert ts.redeliver_dropped_message_with_boundary(
                "term-1",
                message,
                1,
                provider=provider,
                full_resend_requires_composer_evidence=True,
            ) == (False, None)
        send_key.assert_not_called()
        dispatch.assert_not_called()

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("pending unrelated task", True),
            ("unrelated task gpt-6 max", None),
            ("missing content", False),
        ],
    )
    def test_match_only_in_certain_composer_text(self, message, expected):
        from cli_agent_orchestrator.providers.codex import CodexProvider
        from cli_agent_orchestrator.services import terminal_service as ts

        pane = MagicMock()
        pane.get_history.return_value = (
            "› pending unrelated task\n" "  gpt-6 max · /tmp/project\n" "  ? for shortcuts\n"
        )
        with (
            patch.object(ts, "get_backend", return_value=pane),
            patch.object(
                ts, "get_terminal_metadata", return_value={"tmux_session": "s", "tmux_window": "w"}
            ),
            patch.object(
                ts.provider_manager, "get_provider", return_value=CodexProvider("term-1", "s", "w")
            ),
        ):
            assert ts._message_in_composer("term-1", message) is expected

    @pytest.mark.parametrize("path", ["/tmp/x", "~/x"])
    @pytest.mark.parametrize("execution_observed", [False, True])
    @pytest.mark.parametrize("multiline", [False, True])
    @pytest.mark.parametrize("prefix", ["and report", "and  report", "gpt-custom", "codex"])
    def test_swallowed_enter_recovers_without_repeating_paste(
        self, path, execution_observed, multiline, prefix
    ):
        from cli_agent_orchestrator.providers.codex import CodexProvider
        from cli_agent_orchestrator.services import terminal_service as ts
        from cli_agent_orchestrator.services.status_monitor import StatusObservation

        message = f"check foo\n  {prefix} {path}" if multiline else f"check foo · {path} and report"
        screen = (
            "› Earlier request\n"
            "Previous response\n"
            "  Worked for 2s • 7:46 AM\n"
            f"› {message}\n\n"
            "  ? for shortcuts\n"
        )
        provider = CodexProvider("term-1", "s", "w")
        pane = MagicMock()
        pane.get_history.return_value = screen
        sm = MagicMock()
        sm.get_status.return_value = TerminalStatus.IDLE
        sm.output_generation.return_value = 7
        entered = []
        sm.status_observation.side_effect = lambda tid: (
            StatusObservation(TerminalStatus.PROCESSING, 8)
            if entered and execution_observed
            else StatusObservation(TerminalStatus.IDLE, 7)
        )
        with (
            patch(
                "cli_agent_orchestrator.services.inbox_service.INBOX_SUBMIT_CONFIRM_TIMEOUT", 0.0
            ),
            patch("cli_agent_orchestrator.services.inbox_service.INBOX_SUBMIT_MAX_RESUBMITS", 1),
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor", sm),
            patch.object(ts, "status_monitor", sm),
            patch.object(ts, "get_backend", return_value=pane),
            patch.object(
                ts, "get_terminal_metadata", return_value={"tmux_session": "s", "tmux_window": "w"}
            ),
            patch.object(ts.provider_manager, "get_provider", return_value=provider),
            patch.object(ts, "dispatch_input", return_value=7) as dispatch,
            patch.object(
                ts, "send_special_key", side_effect=lambda tid, key: entered.append((tid, key))
            ),
        ):
            # The initial dispatch succeeds, but its Enter is absorbed by the TUI.
            boundary = dispatch("term-1", message)
            assert ts._message_in_composer("term-1", message) is True
            confirmed = InboxService()._confirm_submitted(
                "term-1", message, boundary, registry=None, sender_id="sender-1"
            )

        dispatch.assert_called_once_with("term-1", message)
        assert entered == [("term-1", "Enter")]
        assert confirmed is execution_observed

    def test_previous_message_in_history_is_not_in_empty_composer(self):
        from cli_agent_orchestrator.providers.codex import CodexProvider
        from cli_agent_orchestrator.services import terminal_service as ts

        message = "check foo · /tmp/x and report"
        pane = MagicMock()
        pane.get_history.return_value = (
            f"› {message}\nPrevious response\n  Worked for 2s • 7:46 AM\n"
            "› Ask Codex to do anything\n\n  ? for shortcuts\n"
        )
        with (
            patch.object(ts, "get_backend", return_value=pane),
            patch.object(
                ts, "get_terminal_metadata", return_value={"tmux_session": "s", "tmux_window": "w"}
            ),
            patch.object(
                ts.provider_manager, "get_provider", return_value=CodexProvider("term-1", "s", "w")
            ),
        ):
            assert ts._message_in_composer("term-1", message) is False


class TestInboxRecoveryCoverageByProvider:
    """Which providers can recover a swallowed inbox submit, enumerated.

    The recovery needs either a composer reader (bare Enter) or a direct probe
    plus a composer reader (full re-send). A provider with neither gets no
    recovery: the drop is logged and the message stays in its composer until
    the next delivery piles on top (#78). That is a known limitation per
    provider, not an accident, so adding a provider makes its author pick a
    side here instead of silently joining the no-recovery set.
    """

    # Providers that currently have NO inbox recovery. Remove an entry when the
    # provider gains extract_current_composer; the test fails if one is missing
    # or stale.
    NO_RECOVERY = {
        "antigravity_cli",
        "copilot_cli",
        "cursor_cli",
        "grok_cli",
        "hermes",
        "kimi_cli",
        # Qwen has no composer reader, so a swallowed submit cannot be retried
        # safely without risking duplicate input.
        "qwen_cli",
        "omp",
        "mock_cli",
        # Probe but no composer reader: the probe can confirm a working agent
        # but nothing can be re-sent safely (P2-3), so a swallowed Enter is only
        # logged.
        "opencode_cli",
        "mcode",
    }

    def test_every_provider_either_reads_its_composer_or_is_listed(self):
        from cli_agent_orchestrator.providers.base import BaseProvider
        from cli_agent_orchestrator.providers.manager import ProviderManager

        without_reader = set()
        for provider_type in ProviderType:
            cls = ProviderManager.provider_class(provider_type.value)
            if cls.extract_current_composer is BaseProvider.extract_current_composer:
                without_reader.add(provider_type.value)
        assert without_reader == self.NO_RECOVERY, (
            "Providers without extract_current_composer changed. If one gained a reader, "
            "remove it from NO_RECOVERY; if a new provider has none, add it with a note."
        )

    def test_the_issue_78_reporters_have_a_composer_reader(self):
        assert "claude_code" not in self.NO_RECOVERY
        assert "kiro_cli" not in self.NO_RECOVERY


class TestDialogGuardCoversSubmitRecovery:
    """#896's dialog guard must hold on #1004's recovery path too.

    The merge of #1004 into this branch put a second machine-originated submit
    into the delivery path: ``redeliver_dropped_message_with_boundary``'s bare
    Enter, which submits whatever is already in the composer and therefore never
    reaches ``dispatch_input`` -- where #896's guard lives. An Enter into an open
    choice widget is worse than a paste: it submits the highlighted option, so an
    inbox message's recovery would answer a question reserved for a human, with
    no text involved at all.

    These tests drive both features on one delivery: #1004's swallowed-Enter
    recovery against a receiver that #896 says must not be written to.
    """

    MESSAGE = "MSG1: refactor module A so the retry policy lives in one place"

    #: The providers where the two features can meet at all: they block
    #: orchestrated input while waiting on a user answer (so #896 applies) AND
    #: they can read their own composer (so #1004's bare-Enter branch is
    #: reachable). Pinned because the guard below only matters for this set, and
    #: a provider joining it should come with that fact in view.
    OVERLAP = {"claude_code", "codex"}

    def test_overlap_set_is_still_these_two_providers(self):
        """Enumerated, not assumed: which providers both features apply to."""
        from cli_agent_orchestrator.providers.base import BaseProvider
        from cli_agent_orchestrator.providers.manager import ProviderManager

        overlap = set()
        for provider_type in ProviderType:
            cls = ProviderManager.provider_class(provider_type.value)
            provider = TestInboxRecoveryNeverDuplicates._make_provider(cls)
            reads_composer = (
                cls.extract_current_composer is not BaseProvider.extract_current_composer
            )
            if provider.blocks_orchestrated_input_while_waiting_user_answer and reads_composer:
                overlap.add(provider_type.value)
        assert overlap == self.OVERLAP, (
            "The set of providers that both block dialogs and read their composer "
            "changed. A provider joining it becomes subject to the bare-Enter dialog "
            "guard below; confirm that is what you want, then update OVERLAP."
        )

    @pytest.mark.parametrize("provider_type", sorted(OVERLAP))
    def test_real_dialog_captures_hide_the_composer(self, provider_type):
        """Why this is latent rather than live today.

        On every real dialog capture in test/providers/fixtures the prompt
        replaces the composer box, so ``extract_current_composer`` returns None
        and the inbox path (``full_resend_requires_composer_evidence=True``)
        sends nothing at all. That is what keeps the unguarded bare Enter out of
        reach -- a property of these two extractors, not an invariant, which is
        exactly why the guard is checked explicitly rather than inherited.
        """
        from cli_agent_orchestrator.providers.manager import ProviderManager

        provider = TestInboxRecoveryNeverDuplicates._make_provider(
            ProviderManager.provider_class(provider_type)
        )
        fixtures = sorted(TestInboxRecoveryNeverDuplicates.FIXTURES.glob(f"{provider_type}*"))
        dialogs = [
            path
            for path in fixtures
            if provider.get_status(path.read_text()) == TerminalStatus.WAITING_USER_ANSWER
        ]
        if not dialogs:
            pytest.skip(f"no WAITING_USER_ANSWER capture for {provider_type}")
        for path in dialogs:
            assert provider.extract_current_composer(path.read_text()) is None, (
                f"{path.name} now exposes a composer while a dialog is open; the "
                "bare-Enter guard in redeliver_dropped_message_with_boundary is what "
                "keeps that from answering the dialog"
            )

    def _deliver_with_dialog_open(self, blocks_dialog, composer_text):
        """One inbox delivery whose Enter was swallowed, with a dialog now open.

        The receiver reads IDLE when the paste is dispatched (so #896's guard in
        ``dispatch_input`` lets the delivery through) and WAITING_USER_ANSWER
        afterwards, with a composer that still shows the message -- the
        forward-looking shape the real extractors do not produce today. That is
        #1004's bare-Enter case landing on #896's forbidden state.
        """
        from cli_agent_orchestrator.services import terminal_service as ts
        from cli_agent_orchestrator.services.status_monitor import StatusObservation

        statuses = [TerminalStatus.IDLE]
        enters: list[str] = []
        pastes: list[str] = []

        provider = MagicMock()
        provider.blocks_orchestrated_input_while_waiting_user_answer = blocks_dialog
        provider.supports_direct_status_probe = False
        provider.execution_evidence_ambiguous = False
        provider.paste_enter_count = 1
        provider.extract_current_composer.return_value = composer_text

        sm = MagicMock()
        sm.get_status.side_effect = lambda _tid: statuses[-1]
        sm.output_generation.return_value = 7
        # Never post-boundary: the delivery is never confirmed, so the recovery
        # always runs. The dialog opens as soon as the paste lands.
        sm.status_observation.side_effect = lambda _tid: StatusObservation(statuses[-1], 7)

        def fake_send_keys(*_a, **_k):
            pastes.append("paste")
            # The dialog is up from the moment the paste lands: the Enter was
            # swallowed, so nothing of ours was submitted.
            statuses.append(TerminalStatus.WAITING_USER_ANSWER)

        backend = MagicMock()
        backend.supports_event_inbox.return_value = False
        backend.send_keys.side_effect = fake_send_keys

        with (
            patch(
                "cli_agent_orchestrator.services.inbox_service.INBOX_SUBMIT_CONFIRM_TIMEOUT", 0.0
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_pending_messages",
                return_value=[_make_message(message=self.MESSAGE)],
            ),
            patch("cli_agent_orchestrator.services.inbox_service.update_message_status"),
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor", sm),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_backend",
                return_value=backend,
            ),
            patch.object(ts, "get_backend", return_value=backend),
            patch.object(ts, "status_monitor", sm),
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={
                    "tmux_session": "s",
                    "tmux_window": "w",
                    "provider": "claude_code",
                },
            ),
            patch.object(ts.provider_manager, "get_provider", return_value=provider),
            patch.object(ts, "inject_memory_context", side_effect=lambda m, *a: m),
            patch.object(ts, "update_last_active"),
            patch.object(ts, "send_special_key", side_effect=lambda _tid, key: enters.append(key)),
            patch.object(ts, "_capture_current_composer_region", return_value=composer_text),
        ):
            InboxService().deliver_pending("term-1")
        return pastes, enters

    def test_bare_enter_recovery_never_answers_an_open_dialog(self):
        """The guard: dialog-blocking provider + open dialog -> no Enter at all.

        Without the check in ``redeliver_dropped_message_with_boundary`` the
        recovery reads the message in the composer, sends a bare Enter, and that
        Enter submits whichever option the dialog has highlighted -- #896's harm,
        reached through #1004's recovery instead of through the original paste.
        """
        pastes, enters = self._deliver_with_dialog_open(
            blocks_dialog=True, composer_text=self.MESSAGE
        )

        assert pastes == ["paste"], "the original delivery still goes out while IDLE"
        assert enters == [], "no Enter may be sent into an open dialog"

    def test_bare_enter_recovery_still_runs_for_a_non_blocking_provider(self):
        """The guard is the provider's capability, not a ban on the state.

        A provider that does not declare
        ``blocks_orchestrated_input_while_waiting_user_answer`` keeps #1004's
        recovery in WAITING_USER_ANSWER: its TUI has a live input box beside the
        prompt, so the Enter submits the message rather than the dialog. Pins
        that the fix did not disable the recovery wholesale.
        """
        pastes, enters = self._deliver_with_dialog_open(
            blocks_dialog=False, composer_text=self.MESSAGE
        )

        assert pastes == ["paste"]
        assert enters, "a non-blocking provider must still get its swallowed Enter re-sent"


class TestDelegationEdgeArming:
    """A successful delivery arms the send_message edges its messages opened."""

    @pytest.fixture(autouse=True)
    def _no_submit_confirmation(self):
        with patch(
            "cli_agent_orchestrator.services.inbox_service.INBOX_SUBMIT_CONFIRMATION", False
        ):
            yield

    @patch("cli_agent_orchestrator.services.inbox_service.mark_message_edges_delivered")
    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_successful_delivery_arms_message_edges(
        self, mock_get, mock_monitor, mock_term_svc, mock_update, mock_arm
    ):
        mock_get.return_value = [_make_message(id=5), _make_message(id=6)]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE

        InboxService().deliver_pending("term-1", num_messages=0)

        mock_arm.assert_called_once_with([5, 6])

    @patch("cli_agent_orchestrator.services.inbox_service.mark_message_edges_delivered")
    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_failed_delivery_does_not_arm_edges(
        self, mock_get, mock_monitor, mock_term_svc, mock_update, mock_arm
    ):
        mock_get.return_value = [_make_message(id=5)]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_term_svc.dispatch_input.side_effect = RuntimeError("tmux error")

        InboxService().deliver_pending("term-1")

        mock_arm.assert_not_called()

    @patch(
        "cli_agent_orchestrator.services.inbox_service.mark_message_edges_delivered",
        side_effect=RuntimeError("database unavailable"),
    )
    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_arming_failure_leaves_delivered_batch_delivered(
        self, mock_get, mock_monitor, mock_term_svc, mock_update, _mock_arm
    ):
        mock_get.return_value = [_make_message(id=5)]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE

        InboxService().deliver_pending("term-1")

        mock_term_svc.dispatch_input.assert_called_once()
        statuses = [c.args[1] for c in mock_update.call_args_list]
        assert MessageStatus.FAILED not in statuses


class TestDelegationEdgeArmingAfterConfirmation:
    """Edges are armed after the submit confirmation, whatever its outcome."""

    @pytest.fixture
    def delivery(self):
        order = []
        with (
            patch("cli_agent_orchestrator.services.inbox_service.INBOX_SUBMIT_CONFIRMATION", True),
            patch(
                "cli_agent_orchestrator.services.inbox_service.mark_message_edges_delivered",
                side_effect=lambda ids: order.append(("arm", ids)),
            ),
            patch("cli_agent_orchestrator.services.inbox_service.update_message_status"),
            patch("cli_agent_orchestrator.services.inbox_service.terminal_service"),
            patch("cli_agent_orchestrator.services.inbox_service.get_backend") as backend,
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_pending_messages",
                return_value=[_make_message(id=5)],
            ),
        ):
            backend.return_value.supports_event_inbox.return_value = False
            monitor.get_status.return_value = TerminalStatus.IDLE
            yield order

    @pytest.mark.parametrize("confirmed", [True, False])
    def test_armed_after_the_confirmation_returns(self, delivery, confirmed):
        def confirm(*_args, **_kwargs):
            delivery.append(("confirm", None))
            return confirmed

        with patch.object(InboxService, "_confirm_submitted", side_effect=confirm):
            InboxService().deliver_pending("term-1")

        assert delivery == [("confirm", None), ("arm", [5])]


class TestHoldDeliveryIfIdle:
    def test_held_when_no_delivery_runs(self):
        service = InboxService()
        with service.hold_delivery_if_idle("term-1") as held:
            assert held is True
            assert service._delivery_lock("term-1").locked() is True
        assert service._delivery_lock("term-1").locked() is False

    def test_not_held_while_a_delivery_runs(self):
        service = InboxService()
        seen = []

        def dispatch(terminal_id, *_args, **_kwargs):
            with service.hold_delivery_if_idle(terminal_id) as held:
                seen.append(held)
            return 0

        with (
            patch("cli_agent_orchestrator.services.inbox_service.INBOX_SUBMIT_CONFIRMATION", False),
            patch("cli_agent_orchestrator.services.inbox_service.mark_message_edges_delivered"),
            patch("cli_agent_orchestrator.services.inbox_service.update_message_status"),
            patch("cli_agent_orchestrator.services.inbox_service.terminal_service") as ts,
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_pending_messages",
                return_value=[_make_message(id=5)],
            ),
        ):
            monitor.get_status.return_value = TerminalStatus.IDLE
            ts.dispatch_input.side_effect = dispatch
            service.deliver_pending("term-1")

        assert seen == [False]
        assert service._delivery_lock("term-1").locked() is False


class TestDeliverPendingInBackground:
    def test_runs_on_the_shared_pool_with_the_registry(self):
        service = InboxService()
        registry = MagicMock()
        seen = []

        def deliver(terminal_id, registry=None, source=None):
            seen.append((terminal_id, registry, threading.current_thread().name))

        with patch.object(service, "deliver_pending", side_effect=deliver):
            service.deliver_pending_in_background("term-1", registry=registry).result(5)
            service.deliver_pending_in_background("term-2").result(5)

        assert [(t, r) for t, r, _ in seen] == [("term-1", registry), ("term-2", None)]
        assert all(name.startswith("cao-deliver") for _, _, name in seen)
        assert service._background_pool._max_workers == INBOX_SWEEP_FANOUT

    def test_failure_is_logged_not_raised(self, caplog):
        service = InboxService()
        with patch.object(service, "deliver_pending", side_effect=RuntimeError("pane gone")):
            service.deliver_pending_in_background("term-1").result(5)

        assert "Background delivery to term-1 failed: pane gone" in caplog.text


class TestShortReason:
    def test_names_the_exception_on_one_line(self):
        from cli_agent_orchestrator.services.inbox_service import _short_reason

        assert _short_reason(RuntimeError("tmux\n  error")) == "RuntimeError: tmux error"
        assert _short_reason(RuntimeError()) == "RuntimeError"

    def test_long_reasons_are_cut(self):
        from cli_agent_orchestrator.services.inbox_service import _short_reason

        reason = _short_reason(RuntimeError("x" * 500))
        assert len(reason) == 200 and reason.endswith("...")


class TestDeliveryOutcome:
    """reply-delivery-failure D3/D5: deliver_pending reports what it did with each message.

    ``on_outcome(message_id, outcome, error)`` is called at most once per message
    the call handles; a message left alone gets no call. ``report_to_sender``
    names the message whose outcome goes back to its sender (the inbox POST),
    which the server never retries.
    """

    @pytest.fixture(autouse=True)
    def _seams(self):
        with (
            patch("cli_agent_orchestrator.services.inbox_service.INBOX_SUBMIT_CONFIRMATION", False),
            patch("cli_agent_orchestrator.services.inbox_service.mark_message_edges_delivered"),
            patch(
                "cli_agent_orchestrator.services.inbox_service.close_edges_replied_by",
                create=True,
            ) as settle,
            patch("cli_agent_orchestrator.services.inbox_service.update_message_status") as update,
            patch(
                "cli_agent_orchestrator.services.inbox_service.record_delivery_failure",
                create=True,
                return_value=1,
            ) as record,
            patch("cli_agent_orchestrator.services.inbox_service.terminal_service") as term,
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
            patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages") as pending,
            patch(
                "cli_agent_orchestrator.services.delegation_callback_service.handle_messages_failed",
                create=True,  # defined in plan Task 4
            ) as failed,
            patch(
                "cli_agent_orchestrator.services.delegation_callback_service.handle_delivery_failed"
            ),
        ):
            monitor.get_status.return_value = TerminalStatus.IDLE
            pending.return_value = [_make_message(id=7)]
            term.dispatch_input.return_value = 0
            self.settle, self.update, self.record = settle, update, record
            self.term, self.monitor, self.pending, self.failed = term, monitor, pending, failed
            yield

    def _deliver(self, **kwargs):
        outcomes = []
        InboxService().deliver_pending(
            "term-1", on_outcome=lambda *args: outcomes.append(args), **kwargs
        )
        return outcomes

    @staticmethod
    def _outcome():
        from cli_agent_orchestrator.models.inbox import DeliveryOutcome

        return DeliveryOutcome

    @staticmethod
    def _not_sent(text):
        from cli_agent_orchestrator.backends import base

        return base.InputNotSentError(text)

    def _statuses(self, message_id):
        return [c.args[1] for c in self.update.call_args_list if c.args[0] == message_id]

    # --- delivered ---

    def test_delivered(self):
        assert self._deliver() == [(7, self._outcome().DELIVERED, None)]
        self.settle.assert_called_once_with([7])

    def test_each_message_of_a_batch_is_reported_once(self):
        self.pending.return_value = [_make_message(id=7), _make_message(id=8)]
        outcomes = self._deliver(num_messages=0)
        delivered = self._outcome().DELIVERED
        assert outcomes == [(7, delivered, None), (8, delivered, None)]
        self.settle.assert_called_once_with([7, 8])

    @pytest.mark.parametrize("step", ["_confirm_submitted", "_wait_for_pickup"])
    def test_exception_after_the_paste_keeps_the_message_delivered(self, step):
        """F4: the text was typed, so a failure in the confirmation fails nothing."""
        with (
            patch("cli_agent_orchestrator.services.inbox_service.INBOX_SUBMIT_CONFIRMATION", True),
            patch("cli_agent_orchestrator.services.inbox_service.get_backend") as backend,
            patch.object(InboxService, step, side_effect=RuntimeError("monitor gone")),
        ):
            backend.return_value.supports_event_inbox.return_value = False
            outcomes = self._deliver(report_to_sender=7)
        assert outcomes == [(7, self._outcome().DELIVERED, None)]
        assert self._statuses(7) == [MessageStatus.DELIVERED]
        self.settle.assert_called_once_with([7])
        self.record.assert_not_called()
        self.failed.assert_not_called()

    def test_reply_settlement_failure_leaves_the_message_delivered(self):
        self.settle.side_effect = RuntimeError("database unavailable")
        assert self._deliver() == [(7, self._outcome().DELIVERED, None)]
        assert self._statuses(7) == [MessageStatus.DELIVERED]

    def test_outcome_callback_error_does_not_change_the_delivery(self):
        def broken(*_args):
            raise RuntimeError("caller gone")

        InboxService().deliver_pending("term-1", on_outcome=broken)
        assert self._statuses(7) == [MessageStatus.DELIVERED]
        self.settle.assert_called_once_with([7])

    # --- failed for the sender that waits on the POST ---

    def test_reported_message_failed_before_typing_is_failed_without_retry(self):
        """Spec scenario "Immediate delivery fails before typing"."""
        self.term.dispatch_input.side_effect = self._not_sent("lost terminal.read")
        outcomes = self._deliver(report_to_sender=7)
        assert [(o[0], o[1]) for o in outcomes] == [(7, self._outcome().FAILED)]
        assert "lost terminal.read" in outcomes[0][2]
        assert MessageStatus.PENDING not in self._statuses(7), "the sender owns the retry"
        assert self._statuses(7)[-1] == MessageStatus.FAILED
        self.settle.assert_not_called()
        assert self.failed.call_args.kwargs["reported_ids"] == {7}
        assert self.failed.call_args.kwargs["uncertain"] is False

    def test_failed_attempt_records_its_error(self):
        self.term.dispatch_input.side_effect = self._not_sent("lost terminal.read")
        self._deliver(report_to_sender=7)
        [record] = self.record.call_args_list
        assert record.args[0] == 7
        assert "lost terminal.read" in record.args[1]

    def test_failure_while_sending_is_uncertain(self):
        self.term.dispatch_input.side_effect = RuntimeError("lost terminal.send")
        outcomes = self._deliver(report_to_sender=7)
        assert [(o[0], o[1]) for o in outcomes] == [(7, self._outcome().UNCERTAIN)]
        assert "lost terminal.send" in outcomes[0][2]
        assert self._statuses(7)[-1] == MessageStatus.FAILED
        assert self.failed.call_args.kwargs["uncertain"] is True

    @staticmethod
    def _missing_row():
        """What dispatch_input raises when the receiver's terminal row is gone.

        Taken from the real dispatch_input, so the test follows whatever type
        it uses to keep POST /terminals/{id}/input answering 404.
        """
        from cli_agent_orchestrator.services import terminal_service as real

        with patch.object(real, "get_terminal_metadata", return_value=None):
            try:
                real.dispatch_input("term-1", "hello")
            except Exception as exc:  # noqa: BLE001 - captured for the fake
                return exc
        raise AssertionError("dispatch_input accepted a terminal with no row")

    def test_missing_terminal_row_is_failed(self):
        """F5: nothing can have been typed into a terminal that has no row."""
        self.term.dispatch_input.side_effect = self._missing_row()
        outcomes = self._deliver(report_to_sender=7)
        assert [(o[0], o[1]) for o in outcomes] == [(7, self._outcome().FAILED)]
        assert self._statuses(7)[-1] == MessageStatus.FAILED

    def test_missing_terminal_row_is_not_retried_when_queued(self):
        self.term.dispatch_input.side_effect = self._missing_row()
        outcomes = self._deliver()
        assert [o[1] for o in outcomes] == [self._outcome().FAILED]
        assert MessageStatus.PENDING not in self._statuses(7)

    def test_each_sender_run_reports_its_own_outcome(self):
        first = _make_message(id=7).model_copy(update={"sender_id": "w1"})
        second = _make_message(id=8).model_copy(update={"sender_id": "w2"})
        self.pending.return_value = [first, second]
        self.term.dispatch_input.side_effect = [self._not_sent("lost terminal.read"), 0]
        outcomes = self._deliver(num_messages=0, report_to_sender=7)
        assert [(o[0], o[1]) for o in outcomes] == [
            (7, self._outcome().FAILED),
            (8, self._outcome().DELIVERED),
        ]
        self.settle.assert_called_once_with([8])

    # --- refused ---

    def test_permanent_refusal_is_failed_without_retry(self):
        """Plan note 9: a provider in ERROR can never take the message."""
        from cli_agent_orchestrator.models.terminal import ProviderExitedInputBlockedError

        self.term.dispatch_input.side_effect = ProviderExitedInputBlockedError(
            "provider exited", reason=TerminalInputBlockedReason.PROVIDER_ERROR
        )
        outcomes = self._deliver()
        assert [(o[0], o[1]) for o in outcomes] == [(7, self._outcome().FAILED)]
        assert MessageStatus.PENDING not in self._statuses(7)
        assert self.failed.call_args.kwargs["uncertain"] is False

    # --- left alone: no outcome ---

    def test_receiver_not_ready_reports_nothing(self):
        """Spec scenario "Receiver busy": the POST answers queued."""
        self.monitor.get_status.return_value = TerminalStatus.PROCESSING
        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", False):
            assert self._deliver(report_to_sender=7) == []
        self.term.dispatch_input.assert_not_called()

    def test_busy_claude_receiver_restored_after_restart_gets_the_message_eagerly(self):
        """A Claude Code terminal that was running when cao-server restarted has
        its provider rebuilt on demand. With eager delivery on, a message to it
        while PROCESSING is pasted at once rather than left queued."""
        from cli_agent_orchestrator.providers.manager import ProviderManager

        self.monitor.get_status.return_value = TerminalStatus.PROCESSING
        with (
            patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", True),
            patch(
                "cli_agent_orchestrator.services.inbox_service.provider_manager",
                ProviderManager(),
            ),
            patch(
                "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
                return_value={
                    "provider": ProviderType.CLAUDE_CODE.value,
                    "tmux_session": "s1",
                    "tmux_window": "w1",
                    "agent_profile": None,
                    "shell_command": None,
                },
            ),
        ):
            outcomes = self._deliver(report_to_sender=7)

        assert outcomes == [(7, self._outcome().DELIVERED, None)]
        self.term.dispatch_input.assert_called_once()

    def test_older_message_ahead_reports_only_its_own_outcome(self):
        """Spec scenario "Older messages ahead": the POST's own message gets no outcome."""
        self.pending.return_value = [_make_message(id=3)]
        assert self._deliver(report_to_sender=7) == [(3, self._outcome().DELIVERED, None)]

    def test_unresolved_pane_reports_nothing_and_counts_no_attempt(self):
        """Plan note 8: still reset to PENDING without counting."""
        self.term.dispatch_input.side_effect = TerminalNotFoundError("term-1", "no pane yet")
        assert self._deliver(report_to_sender=7) == []
        assert self._statuses(7)[-1] == MessageStatus.PENDING
        self.record.assert_not_called()

    def test_transient_refusal_reports_nothing_and_counts_no_attempt(self):
        self.term.dispatch_input.side_effect = TerminalInputBlockedError(
            "dialog up", reason=TerminalInputBlockedReason.WAITING_USER_ANSWER
        )
        assert self._deliver(report_to_sender=7) == []
        assert self._statuses(7)[-1] == MessageStatus.PENDING
        self.record.assert_not_called()


class TestQueuedRetry:
    """Spec "Queued delivery failures are retried and reported" (plan Task 3 ``_fail_batch``).

    A real in-memory database holds the message, so the tests read back its
    status, ``delivery_attempts`` and ``last_error``. Only the dispatch, the
    receiver status and the failure notice (Task 4) are replaced.
    """

    @pytest.fixture(autouse=True)
    def _seams(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from cli_agent_orchestrator.clients.database import (
            Base,
            create_inbox_message,
            create_terminal,
        )

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        with (
            patch("cli_agent_orchestrator.clients.database.SessionLocal", sessionmaker(engine)),
            patch("cli_agent_orchestrator.services.inbox_service.INBOX_SUBMIT_CONFIRMATION", False),
            patch("cli_agent_orchestrator.services.inbox_service.terminal_service") as term,
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
            patch(
                "cli_agent_orchestrator.services.delegation_callback_service.handle_messages_failed",
                create=True,  # defined in plan Task 4
            ),
            patch(
                "cli_agent_orchestrator.services.delegation_callback_service.handle_delivery_failed"
            ),
        ):
            monitor.get_status.return_value = TerminalStatus.IDLE
            term.dispatch_input.return_value = 0
            create_terminal("term-1", "cao-s", "sup", "claude_code")
            self.message_id = create_inbox_message("sender-1", "term-1", "queued reply").id
            self.term = term
            yield
        engine.dispose()

    @staticmethod
    def _outcome():
        from cli_agent_orchestrator.models.inbox import DeliveryOutcome

        return DeliveryOutcome

    @staticmethod
    def _max_attempts():
        from cli_agent_orchestrator import constants

        return constants.INBOX_MAX_DELIVERY_ATTEMPTS

    @staticmethod
    def _not_sent(text):
        from cli_agent_orchestrator.backends import base

        return base.InputNotSentError(text)

    def _attempt(self):
        """One queued delivery (no report_to_sender); returns this call's outcomes."""
        outcomes = []
        InboxService().deliver_pending("term-1", on_outcome=lambda *a: outcomes.append(a))
        return outcomes

    def _stored(self):
        from cli_agent_orchestrator.clients.database import get_inbox_messages

        [message] = get_inbox_messages("term-1", limit=10)
        return message

    def _fail_before_typing(self, attempts):
        self.term.dispatch_input.side_effect = [
            self._not_sent(f"lost terminal.read on attempt {n}") for n in range(1, attempts + 1)
        ]
        return [self._attempt() for _ in range(attempts)]

    def test_failure_before_typing_resets_to_pending_and_records_the_attempt(self):
        # Act
        [outcomes] = self._fail_before_typing(1)

        # Assert
        assert [(o[0], o[1]) for o in outcomes] == [(self.message_id, self._outcome().RETRYING)]
        stored = self._stored()
        assert stored.status == MessageStatus.PENDING
        assert stored.delivery_attempts == 1
        assert "lost terminal.read on attempt 1" in stored.last_error

    def test_attempt_before_the_last_is_still_retried(self):
        # Act
        runs = self._fail_before_typing(self._max_attempts() - 1)

        # Assert
        retrying = self._outcome().RETRYING
        assert [[o[1] for o in run] for run in runs] == [[retrying]] * (self._max_attempts() - 1)
        stored = self._stored()
        assert stored.status == MessageStatus.PENDING
        assert stored.delivery_attempts == self._max_attempts() - 1
        assert f"attempt {self._max_attempts() - 1}" in stored.last_error

    def test_last_attempt_fails_for_good(self):
        # Act
        runs = self._fail_before_typing(self._max_attempts())

        # Assert
        assert [(o[0], o[1]) for o in runs[-1]] == [(self.message_id, self._outcome().FAILED)]
        stored = self._stored()
        assert stored.status == MessageStatus.FAILED
        assert stored.delivery_attempts == self._max_attempts()
        assert f"attempt {self._max_attempts()}" in stored.last_error, "keeps the last error"

    def test_failed_message_is_not_attempted_again(self):
        self._fail_before_typing(self._max_attempts())
        self.term.dispatch_input.side_effect = None
        assert self._attempt() == []
        assert self.term.dispatch_input.call_count == self._max_attempts()

    def test_uncertain_failure_fails_at_once_without_retry(self):
        # Arrange
        self.term.dispatch_input.side_effect = RuntimeError("herdr-core lost terminal.send")

        # Act
        outcomes = self._attempt()
        later = self._attempt()

        # Assert
        assert [(o[0], o[1]) for o in outcomes] == [(self.message_id, self._outcome().UNCERTAIN)]
        assert self._stored().status == MessageStatus.FAILED
        assert later == [], "an uncertain message is never typed a second time"
        assert self.term.dispatch_input.call_count == 1

    def test_transient_failure_then_success_delivers_once(self):
        self.term.dispatch_input.side_effect = [self._not_sent("lost terminal.read"), 0]
        first, second = self._attempt(), self._attempt()
        assert [o[1] for o in first + second] == [
            self._outcome().RETRYING,
            self._outcome().DELIVERED,
        ]
        stored = self._stored()
        assert stored.status == MessageStatus.DELIVERED
        assert stored.delivery_attempts == 1, "counts the failed attempt only"
        assert self._attempt() == [], "delivered once"
        assert self.term.dispatch_input.call_count == 2
