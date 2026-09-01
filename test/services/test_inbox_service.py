"""Tests for the event-driven InboxService."""

import asyncio
import threading
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

import cli_agent_orchestrator.services.inbox_service as inbox_service_module
from cli_agent_orchestrator.backends.base import TerminalNotFoundError
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import InboxModel
from cli_agent_orchestrator.constants import INBOX_RECONCILE_GRACE_SECONDS
from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.inbox_service import InboxService


@pytest.fixture(autouse=True)
def _reset_dispatch_active():
    """_dispatch_active (#709 coalescing) is module-level state keyed by
    terminal_id and persists until a later status event clears it. Tests
    across this file reuse the same terminal ids (e.g. "term-1", "t1"), so
    without a reset a dispatch marked active by one test would make an
    unrelated later test's deliver_pending call coalesce instead of send."""
    inbox_service_module._dispatch_active.clear()
    yield
    inbox_service_module._dispatch_active.clear()


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
    @patch("cli_agent_orchestrator.services.inbox_service.claim_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivers_message_when_idle(
        self, mock_get, mock_monitor, mock_term_svc, mock_claim, mock_update
    ):
        mock_get.return_value = [_make_message()]
        mock_claim.return_value = [_make_message(status=MessageStatus.DELIVERED)]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_monitor.get_status_generation.return_value = 0

        svc = InboxService()
        svc.deliver_pending("term-1")

        mock_claim.assert_called_once_with("term-1", limit=1)
        mock_term_svc.send_input.assert_called_once_with("term-1", "hello")
        mock_update.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.claim_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivers_message_when_completed(
        self, mock_get, mock_monitor, mock_term_svc, mock_claim, mock_update
    ):
        mock_get.return_value = [_make_message()]
        mock_claim.return_value = [_make_message(status=MessageStatus.DELIVERED)]
        mock_monitor.get_status.return_value = TerminalStatus.COMPLETED
        mock_monitor.get_status_generation.return_value = 0

        svc = InboxService()
        svc.deliver_pending("term-1")

        mock_claim.assert_called_once_with("term-1", limit=1)
        mock_term_svc.send_input.assert_called_once_with("term-1", "hello")
        mock_update.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.claim_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_skips_when_no_pending_messages(
        self, mock_get, mock_monitor, mock_term_svc, mock_claim
    ):
        mock_get.return_value = []

        svc = InboxService()
        svc.deliver_pending("term-1")

        mock_term_svc.send_input.assert_not_called()
        mock_claim.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.claim_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_skips_when_processing(self, mock_get, mock_monitor, mock_term_svc, mock_claim):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.PROCESSING

        svc = InboxService()
        svc.deliver_pending("term-1")

        mock_term_svc.send_input.assert_not_called()
        mock_claim.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.claim_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_skips_when_unknown(self, mock_get, mock_monitor, mock_term_svc, mock_claim):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.UNKNOWN

        svc = InboxService()
        svc.deliver_pending("term-1")

        mock_term_svc.send_input.assert_not_called()
        mock_claim.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.claim_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivers_multiple_messages_concatenated(
        self, mock_get, mock_monitor, mock_term_svc, mock_claim, mock_update
    ):
        msgs = [_make_message(id=1, message="hello"), _make_message(id=2, message="world")]
        mock_get.return_value = msgs
        mock_claim.return_value = [
            _make_message(id=1, message="hello", status=MessageStatus.DELIVERED),
            _make_message(id=2, message="world", status=MessageStatus.DELIVERED),
        ]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_monitor.get_status_generation.return_value = 0

        svc = InboxService()
        svc.deliver_pending("term-1", num_messages=2)

        mock_get.assert_called_once_with("term-1", limit=2)
        mock_claim.assert_called_once_with("term-1", limit=2)
        mock_term_svc.send_input.assert_called_once_with("term-1", "hello\nworld")
        mock_update.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.claim_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivers_all_when_num_messages_zero(
        self, mock_get, mock_monitor, mock_term_svc, mock_claim, mock_update
    ):
        msgs = [_make_message(id=i, message=f"msg{i}") for i in range(3)]
        mock_get.return_value = msgs
        mock_claim.return_value = [
            _make_message(id=i, message=f"msg{i}", status=MessageStatus.DELIVERED) for i in range(3)
        ]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_monitor.get_status_generation.return_value = 0

        svc = InboxService()
        svc.deliver_pending("term-1", num_messages=0)

        mock_get.assert_called_once_with("term-1", limit=100)
        mock_claim.assert_called_once_with("term-1", limit=100)
        mock_term_svc.send_input.assert_called_once_with("term-1", "msg0\nmsg1\nmsg2")
        mock_update.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.claim_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_marks_failed_on_send_error(
        self, mock_get, mock_monitor, mock_term_svc, mock_claim, mock_update
    ):
        mock_get.return_value = [_make_message()]
        mock_claim.return_value = [_make_message(status=MessageStatus.DELIVERED)]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_term_svc.send_input.side_effect = RuntimeError("tmux error")

        svc = InboxService()
        svc.deliver_pending("term-1")

        # The claim already moved the message to DELIVERED atomically (#406);
        # only the post-failure reset to FAILED goes through update_message_status.
        mock_update.assert_called_once_with(1, MessageStatus.FAILED)

    @patch("cli_agent_orchestrator.services.inbox_service.claim_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_claims_before_send_input(self, mock_get, mock_monitor, mock_term_svc, mock_claim):
        """Regression for the double-delivery race (#164, #406).

        send_input()'s output flows back through the FIFO/StatusMonitor pipeline
        and can re-emit a status event that re-enters deliver_pending from a
        second thread while this call is still in flight. The message must
        already be claimed (moved out of PENDING) by then, so the claim has to
        happen before send_input is called.
        """
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_monitor.get_status_generation.return_value = 0

        order = []

        def _claim(*args, **kwargs):
            order.append(("claim", args))
            return [_make_message(status=MessageStatus.DELIVERED)]

        mock_claim.side_effect = _claim
        mock_term_svc.send_input.side_effect = lambda *args, **kwargs: order.append(("send", args))

        svc = InboxService()
        svc.deliver_pending("term-1")

        assert order[0] == ("claim", ("term-1",))
        assert order[1][0] == "send"

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.claim_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_resolution_failure_leaves_message_pending(
        self, mock_get, mock_monitor, mock_term_svc, mock_claim, mock_update
    ):
        """A TerminalNotFoundError during send leaves the message PENDING, not FAILED.

        Pane resolution can transiently fail (e.g. herdr pane not yet resolvable).
        The claim already moved the message to DELIVERED atomically (to close the
        re-entrancy race), so on a resolution failure it must be reset to PENDING
        for a later retry — never left DELIVERED or marked FAILED.
        """
        mock_get.return_value = [_make_message()]
        mock_claim.return_value = [_make_message(status=MessageStatus.DELIVERED)]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_term_svc.send_input.side_effect = TerminalNotFoundError("s:w")

        svc = InboxService()
        svc.deliver_pending("term-1")

        # Only the reset to PENDING goes through update_message_status; never FAILED.
        mock_update.assert_called_once_with(1, MessageStatus.PENDING)


class TestConcurrentDeliverySerialization:
    """The atomic claim (#406) stops two callers from claiming the same row,
    but not from claiming two DIFFERENT rows and then both calling
    send_input at once, interleaving their tmux paste/delay/Enter sequences
    (reviewer-reproduced race on #709, first pass).

    The per-terminal lock added for that fixes the byte-level interleaving,
    but a queued contender still ran the full delivery the instant it got the
    lock: status_monitor.get_status() keeps returning the cached IDLE from
    before the first send until the real pipeline observes actual terminal
    output, so the second caller saw the same stale ready value and dispatched
    its own message into a terminal that had not started on the first one yet
    (reviewer-reproduced second pass: two IDLE checks, paste:first, enter:first,
    paste:second, enter:second, both rows DELIVERED in one IDLE cycle). The
    tests below cover the coalescing fix for that.
    """

    def test_two_concurrent_deliveries_to_same_terminal_never_overlap(self, isolated_memory_db):
        with database.SessionLocal() as seed:
            seed.add_all(
                [
                    InboxModel(
                        sender_id="s",
                        receiver_id="term-1",
                        message=f"m{i}",
                        status=MessageStatus.PENDING.value,
                        created_at=datetime.now(),
                    )
                    for i in range(2)
                ]
            )
            seed.commit()

        intervals = []
        intervals_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def fake_send_input(terminal_id, message, **kwargs):
            start = time.monotonic()
            time.sleep(0.05)
            end = time.monotonic()
            with intervals_lock:
                intervals.append((start, end))
            return True

        def worker():
            barrier.wait()
            InboxService().deliver_pending("term-1")

        with (
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as mock_monitor,
            patch("cli_agent_orchestrator.services.inbox_service.terminal_service") as mock_term,
        ):
            mock_monitor.get_status.return_value = TerminalStatus.IDLE
            mock_monitor.get_status_generation.return_value = 0
            mock_term.send_input.side_effect = fake_send_input

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # Exactly one dispatch: the queued contender coalesces instead of
        # sending into the same still-cached IDLE status the first caller
        # already used (#709, second review pass). The prior version of this
        # test asserted len(intervals) == 2, which is exactly the bug the
        # reviewer flagged: it codified "both messages get sent" as correct.
        assert len(intervals) == 1, f"expected exactly one dispatch, got {intervals}"

        with database.SessionLocal() as check:
            statuses = sorted(
                m.status for m in check.query(InboxModel).filter_by(receiver_id="term-1").all()
            )
        # One row delivered, the other left PENDING for a later ready event
        # rather than being claimed and sent alongside it.
        assert statuses == sorted([MessageStatus.DELIVERED.value, MessageStatus.PENDING.value])

    def test_coalesced_message_delivers_on_next_status_event(self, isolated_memory_db):
        """The row left PENDING by coalescing is not stuck forever: once the
        real pipeline reports the next status event for the terminal,
        InboxService.run() clears the busy marker (see TestRun) and a later
        IDLE-triggered deliver_pending call sends the coalesced message."""
        with database.SessionLocal() as seed:
            seed.add_all(
                [
                    InboxModel(
                        sender_id="s",
                        receiver_id="term-1",
                        message=f"m{i}",
                        status=MessageStatus.PENDING.value,
                        created_at=datetime.now(),
                    )
                    for i in range(2)
                ]
            )
            seed.commit()

        with (
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as mock_monitor,
            patch("cli_agent_orchestrator.services.inbox_service.terminal_service") as mock_term,
        ):
            mock_monitor.get_status.return_value = TerminalStatus.IDLE
            mock_monitor.get_status_generation.return_value = 0
            svc = InboxService()

            svc.deliver_pending("term-1")
            assert mock_term.send_input.call_count == 1

            # Still within the same stale-IDLE window: coalesces, does not
            # send a second message.
            svc.deliver_pending("term-1")
            assert mock_term.send_input.call_count == 1

            # A later status event for the terminal (any value: run() clears
            # the marker before deciding whether to deliver) proves the real
            # pipeline moved past the cached status the first send used. Its
            # generation (1) is newer than the one recorded at dispatch (0).
            inbox_service_module._clear_dispatch_active("term-1", 1)

            svc.deliver_pending("term-1")
            assert mock_term.send_input.call_count == 2

        with database.SessionLocal() as check:
            statuses = sorted(
                m.status for m in check.query(InboxModel).filter_by(receiver_id="term-1").all()
            )
        assert statuses == sorted([MessageStatus.DELIVERED.value, MessageStatus.DELIVERED.value])

    def test_dispatch_marker_survives_elapsed_time_with_no_transition(self):
        """Reviewer-reproduced fifth-round finding on #709: a provider is not
        contractually bound to emit output or a PROCESSING transition inside
        any fixed window, so elapsed time alone must never authorize another
        send. Only a genuinely newer generation (a real transition) may clear
        the marker; simply waiting must not."""
        inbox_service_module._mark_dispatch_active("term-1")
        assert inbox_service_module._is_dispatch_active("term-1") is True
        time.sleep(0.2)
        assert inbox_service_module._is_dispatch_active("term-1") is True

    def test_two_deliveries_with_no_status_event_still_coalesce_past_the_old_window(
        self, isolated_memory_db
    ):
        """Reviewer-reproduced fifth-round finding on #709: with the cached
        status and generation unchanged the whole time (no status event at
        all, matching a slow/silent provider start), a second deliver_pending
        call must still coalesce even once real time has moved well past what
        used to be the five-second coalescing window. Mirrors
        test_coalesced_message_delivers_on_next_status_event but never clears
        the marker, so a second real send here means the guard authorized
        itself on elapsed time alone."""
        with database.SessionLocal() as seed:
            seed.add_all(
                [
                    InboxModel(
                        sender_id="s",
                        receiver_id="term-1",
                        message=f"m{i}",
                        status=MessageStatus.PENDING.value,
                        created_at=datetime.now(),
                    )
                    for i in range(2)
                ]
            )
            seed.commit()

        with (
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as mock_monitor,
            patch("cli_agent_orchestrator.services.inbox_service.terminal_service") as mock_term,
        ):
            mock_monitor.get_status.return_value = TerminalStatus.IDLE
            mock_monitor.get_status_generation.return_value = 0
            svc = InboxService()

            svc.deliver_pending("term-1")
            assert mock_term.send_input.call_count == 1

            time.sleep(0.2)

            svc.deliver_pending("term-1")
            assert mock_term.send_input.call_count == 1

        with database.SessionLocal() as check:
            statuses = sorted(
                m.status for m in check.query(InboxModel).filter_by(receiver_id="term-1").all()
            )
        assert statuses == sorted([MessageStatus.DELIVERED.value, MessageStatus.PENDING.value])

    def test_completion_inside_send_input_does_not_starve_the_next_message(
        self, isolated_memory_db
    ):
        """Reviewer-reproduced sixth-round finding on #709: send_input's own
        notify_input_sent(assume_processing=True) applies one PROCESSING
        transition synchronously before the tmux paste, so a single generation
        bump between the pre- and post-send_input reads is expected and the
        marker is meant to survive it. If a genuine completion transition ALSO
        lands inside send_input's own blocking window (e.g. real output
        arriving during the tmux submit-delay path for a very fast turn), two
        bumps happen before deliver_pending ever gets to snapshot a baseline,
        and marking busy against that already-final generation would wait for
        a transition that already happened and will not repeat, coalescing
        every future message to this terminal forever. Confirmed this fails
        without the fix: the second message is left PENDING with no future
        event able to release it."""
        with database.SessionLocal() as seed:
            seed.add_all(
                [
                    InboxModel(
                        sender_id="s",
                        receiver_id="term-1",
                        message=f"m{i}",
                        status=MessageStatus.PENDING.value,
                        created_at=datetime.now(),
                    )
                    for i in range(2)
                ]
            )
            seed.commit()

        with (
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as mock_monitor,
            patch("cli_agent_orchestrator.services.inbox_service.terminal_service") as mock_term,
        ):
            mock_monitor.get_status.return_value = TerminalStatus.IDLE
            # First delivery: pre-dispatch read is 0; by the time send_input
            # returns, TWO real transitions already landed (its own PROCESSING
            # bump plus a genuine completion) so the post-dispatch read is 2.
            # Second delivery: pre=2, post=3 (one ordinary PROCESSING bump),
            # the normal case, so this one does re-arm the marker; it just
            # must not block the send that gets it there.
            mock_monitor.get_status_generation.side_effect = [0, 2, 2, 3]
            svc = InboxService()

            svc.deliver_pending("term-1")
            assert mock_term.send_input.call_count == 1
            assert inbox_service_module._is_dispatch_active("term-1") is False

            svc.deliver_pending("term-1")
            assert mock_term.send_input.call_count == 2

        with database.SessionLocal() as check:
            statuses = sorted(
                m.status for m in check.query(InboxModel).filter_by(receiver_id="term-1").all()
            )
        assert statuses == sorted([MessageStatus.DELIVERED.value, MessageStatus.DELIVERED.value])


class TestEagerInboxDelivery:
    """Tests for eager inbox delivery (CAO_EAGER_INBOX_DELIVERY).

    Covers the relaxed status gate in deliver_pending() that allows PROCESSING
    and WAITING_USER_ANSWER delivery when the env var is enabled and the
    provider declares accepts_input_while_processing=True.
    """

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.claim_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_idle_status_always_works(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_claim, mock_update
    ):
        """IDLE delivers regardless of env var or provider capability."""
        mock_get.return_value = [_make_message()]
        mock_claim.return_value = [_make_message(status=MessageStatus.DELIVERED)]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        provider = MagicMock()
        provider.accepts_input_while_processing = False
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", False):
            svc = InboxService()
            svc.deliver_pending("t1")

        mock_term_svc.send_input.assert_called_once()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.claim_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_completed_status_always_works(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_claim, mock_update
    ):
        """COMPLETED delivers regardless of env var or provider capability."""
        mock_get.return_value = [_make_message()]
        mock_claim.return_value = [_make_message(status=MessageStatus.DELIVERED)]
        mock_monitor.get_status.return_value = TerminalStatus.COMPLETED
        provider = MagicMock()
        provider.accepts_input_while_processing = False
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", False):
            svc = InboxService()
            svc.deliver_pending("t1")

        mock_term_svc.send_input.assert_called_once()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.claim_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_processing_with_eager_enabled_and_capable_provider(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_claim, mock_update
    ):
        """PROCESSING + eager ON + capable provider -> delivers."""
        mock_get.return_value = [_make_message()]
        mock_claim.return_value = [_make_message(status=MessageStatus.DELIVERED)]
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
    @patch("cli_agent_orchestrator.services.inbox_service.claim_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_waiting_user_answer_with_eager_enabled_and_capable_provider(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_claim, mock_update
    ):
        """WAITING_USER_ANSWER + eager ON + capable provider -> delivers."""
        mock_get.return_value = [_make_message()]
        mock_claim.return_value = [_make_message(status=MessageStatus.DELIVERED)]
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
                "data": {"status": TerminalStatus.IDLE.value, "generation": 1},
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
                "data": {"status": TerminalStatus.COMPLETED.value, "generation": 1},
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
                "data": {"status": TerminalStatus.PROCESSING.value, "generation": 1},
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
    async def test_processing_event_clears_dispatch_active(self):
        """A PROCESSING event does not trigger delivery, but it is exactly the
        real pipeline catching up on a prior dispatch (#709): it must still
        clear the busy marker so the terminal is not coalescing against a
        cached status that has already moved on, as long as it postdates
        the dispatch. status_monitor is patched (not the real singleton,
        which other test modules also exercise against this same terminal
        id) so the dispatch-time generation is pinned to a known value."""
        with patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as mock_monitor:
            mock_monitor.get_status_generation.return_value = 0
            inbox_service_module._mark_dispatch_active("abc123")

        svc = InboxService()
        svc.deliver_pending = MagicMock()

        queue = asyncio.Queue()
        await queue.put(
            {
                "topic": "terminal.abc123.status",
                "data": {"status": TerminalStatus.PROCESSING.value, "generation": 1},
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

        assert inbox_service_module._is_dispatch_active("abc123") is False

    @pytest.mark.asyncio
    async def test_stale_queued_event_does_not_clear_dispatch_active(self):
        """Reviewer-reproduced third-round finding on #709: a status event can
        already be sitting in run()'s queue when a dispatch happens (the
        immediate API, OpenCode poller, and reconcile paths all call
        deliver_pending() outside this consumer). If run() then clears the
        marker on that stale, already-superseded event, a queued contender
        reads the still-cached ready status and dispatches into the same
        cycle, the exact bug the busy marker exists to prevent. The event's
        generation (1) does not postdate the dispatch's own generation
        snapshot (1, i.e. taken after that same transition already landed),
        so the marker must survive."""
        with patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as mock_monitor:
            # The transition that produced the queued event has already
            # happened by the time deliver_pending dispatches and marks busy,
            # so get_status_generation reflects it (1), not the pre-transition
            # value (0) run() has not consumed yet.
            mock_monitor.get_status_generation.return_value = 1
            inbox_service_module._mark_dispatch_active("abc123")

        svc = InboxService()
        svc.deliver_pending = MagicMock()

        queue = asyncio.Queue()
        # The event already queued before the dispatch above.
        await queue.put(
            {
                "topic": "terminal.abc123.status",
                "data": {"status": TerminalStatus.IDLE.value, "generation": 1},
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

        # The marker must still be active: the queued event did not postdate
        # the dispatch, so clearing it here would let a concurrent contender
        # read the still-stale cached status.
        assert inbox_service_module._is_dispatch_active("abc123") is True

    @pytest.mark.asyncio
    async def test_genuinely_newer_event_still_clears_dispatch_active(self):
        """The mirror of the stale-event case: once a REAL post-dispatch
        transition arrives (generation strictly greater than the one
        recorded at dispatch time), the marker must still clear: the fix
        for the stale-event bug must not regress into never clearing at
        all."""
        with patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as mock_monitor:
            mock_monitor.get_status_generation.return_value = 1
            inbox_service_module._mark_dispatch_active("abc123")

        svc = InboxService()
        svc.deliver_pending = MagicMock()

        queue = asyncio.Queue()
        await queue.put(
            {
                "topic": "terminal.abc123.status",
                "data": {"status": TerminalStatus.IDLE.value, "generation": 2},
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

        assert inbox_service_module._is_dispatch_active("abc123") is False

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
                "data": {"status": TerminalStatus.IDLE.value, "generation": 1},
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
                "data": {"status": TerminalStatus.IDLE.value, "generation": 1},
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
