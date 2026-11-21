"""Tests for delegation_callback_service."""

import asyncio
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    close_delegation_edge,
    close_edges_replied_by,
    create_delegation_edge,
    find_open_edge,
    list_open_edges_into,
    list_open_edges_out_of,
    mark_assign_edges_delivered,
)
from cli_agent_orchestrator.models.delegation import (
    DelegationEdge,
    EdgeReason,
    EdgeSource,
    EdgeState,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import delegation_callback_service as svc
from cli_agent_orchestrator.services.inbox_service import InboxService

MOD = "cli_agent_orchestrator.services.delegation_callback_service"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)
    with patch("cli_agent_orchestrator.clients.database.SessionLocal", session):
        yield


class TestMessages:
    def test_auto_forward_header(self):
        text = svc.build_auto_forward_message("w1", "developer", EdgeReason.NO_CALLBACK, "result")
        assert text.startswith("[CAO auto-forward] Worker w1 (profile developer)")
        assert "Reason: did not call send_message" in text
        assert "may be partial" in text
        assert 'send_message(receiver_id="w1"' in text
        assert 'delete_terminal("w1")' in text
        assert text.endswith("---\nresult")

    def test_worker_ended_message(self):
        text = svc.build_worker_ended_message("w1", "developer", "was deleted", None)
        assert text.startswith("[CAO worker ended] Worker w1 (profile developer) was deleted")
        assert "(last response could not be read)" in text
        assert "may still be running" not in text
        assert "delete_terminal" not in text

    def test_error_notice_says_the_worker_may_still_be_running(self):
        text = svc.build_worker_ended_message(
            "w1", "developer", "reached ERROR", "partial", may_be_running=True
        )
        assert text.startswith("[CAO worker ended] Worker w1 (profile developer) reached ERROR")
        assert "The worker may still be running" in text
        assert 'When done with it: delete_terminal("w1")' in text
        assert text.endswith("---\npartial")


class TestSendMessageRules:
    def test_reply_marks_owed_edge_in_flight_and_leaves_it_open(self, db):
        """Spec scenario "Reply stored but not yet delivered" (rule 1, design D1)."""
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        svc.on_send_message(sender_id="w1", receiver_id="sup", message_id=1, expect_reply=False)
        (edge,) = list_open_edges_into("w1")
        assert (edge.state, edge.reply_message_id) == (EdgeState.OPEN, 1)

    def test_later_reply_replaces_the_recorded_message(self, db):
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        svc.on_send_message(sender_id="w1", receiver_id="sup", message_id=1, expect_reply=False)
        svc.on_send_message(sender_id="w1", receiver_id="sup", message_id=2, expect_reply=False)
        (edge,) = list_open_edges_into("w1")
        assert edge.reply_message_id == 2

    def test_reply_with_expect_reply_opens_no_reverse_edge(self, db):
        """Rule 1 wins: a reply never also opens w1 -> sup."""
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        svc.on_send_message(sender_id="w1", receiver_id="sup", message_id=1, expect_reply=True)
        assert find_open_edge("w1", "sup") is None
        assert list_open_edges_into("w1")[0].reply_message_id == 1

    @patch(f"{MOD}.get_terminal_metadata", return_value={"caller_id": "sup"})
    def test_caller_follow_up_to_own_child_opens_edge(self, _meta, db):
        svc.on_send_message(sender_id="sup", receiver_id="w1", message_id=7, expect_reply=False)
        edge = find_open_edge("sup", "w1")
        assert edge is not None and edge.message_id == 7 and edge.source == EdgeSource.SEND_MESSAGE

    @patch(f"{MOD}.get_terminal_metadata", return_value={"caller_id": "sup"})
    def test_follow_up_does_not_duplicate_open_edge(self, _meta, db):
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        svc.on_send_message(sender_id="sup", receiver_id="w1", message_id=8, expect_reply=False)
        assert len(list_open_edges_into("w1")) == 1

    @patch(f"{MOD}.get_terminal_metadata", return_value={"caller_id": "someone-else"})
    def test_unrelated_terminal_without_expect_reply(self, _meta, db):
        svc.on_send_message(sender_id="w1", receiver_id="w3", message_id=9, expect_reply=False)
        assert find_open_edge("w1", "w3") is None

    @patch(f"{MOD}.get_terminal_metadata", return_value={"caller_id": "someone-else"})
    def test_unrelated_terminal_with_expect_reply(self, _meta, db):
        svc.on_send_message(sender_id="w1", receiver_id="w3", message_id=9, expect_reply=True)
        assert find_open_edge("w1", "w3") is not None


def _armed(caller, worker):
    create_delegation_edge(caller, worker, EdgeSource.ASSIGN)
    mark_assign_edges_delivered(worker)


@pytest.fixture
def env():
    """Patch the I/O the evaluator touches; return the inbox capture list.

    The claim stays real (the database close); only the queued message is
    captured instead of written.
    """
    sent = []

    def claim_and_record(edge_id, state, sender_id, receiver_id, message, *, is_system_notice):
        assert is_system_notice is True, "every message queued with a closing edge is a notice"
        if not close_delegation_edge(edge_id, state):
            return False
        sent.append((sender_id, receiver_id, message))
        return True

    with (
        patch(f"{MOD}.status_monitor") as sm,
        patch(f"{MOD}.terminal_service") as ts,
        patch(f"{MOD}.get_pending_messages", return_value=[]) as pending,
        patch(f"{MOD}.get_terminal_metadata", return_value={"agent_profile": "developer"}),
        patch(f"{MOD}.close_delegation_edge_with_message", side_effect=claim_and_record),
        patch(f"{MOD}.inbox_service") as inbox,
    ):
        sm.get_status.return_value = TerminalStatus.IDLE
        sm.started_on_last_input.return_value = True
        ts.get_output.return_value = "final answer"
        yield {"sent": sent, "status": sm, "pending": pending, "ts": ts, "inbox": inbox}


class TestEvaluateCompletion:
    def test_forwards_idle_worker(self, db, env):
        _armed("sup", "w1")
        assert svc.evaluate_completion("w1") == 1
        sender, receiver, text = env["sent"][0]
        assert (sender, receiver) == ("w1", "sup")
        assert text.endswith("final answer")
        assert list_open_edges_into("w1") == []

    def test_unarmed_edge_not_forwarded(self, db, env):
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        assert svc.evaluate_completion("w1") == 0

    def test_waiting_on_sub_worker_not_forwarded(self, db, env):
        _armed("sup", "w1")
        create_delegation_edge("w1", "w2", EdgeSource.ASSIGN)
        assert svc.evaluate_completion("w1") == 0
        assert env["sent"] == []

    def test_caller_held_while_its_sub_workers_reply_is_in_flight(self, db, env):
        """Spec scenario "Caller idle while its worker's reply is queued"."""
        _armed("sup", "w1")
        _armed("w1", "w2")
        svc.on_send_message(sender_id="w2", receiver_id="w1", message_id=3, expect_reply=False)
        assert svc.evaluate_completion("w1") == 0
        assert env["sent"] == []

    def test_forwarded_after_sub_worker_reply_is_delivered(self, db, env):
        _armed("sup", "w1")
        create_delegation_edge("w1", "w2", EdgeSource.ASSIGN)
        svc.on_send_message(sender_id="w2", receiver_id="w1", message_id=3, expect_reply=False)
        close_edges_replied_by([3])
        assert svc.evaluate_completion("w1") == 1

    def test_worker_with_reply_in_flight_not_forwarded(self, db, env):
        """Spec scenario "Worker idle after replying" (design D2)."""
        _armed("sup", "w1")
        svc.on_send_message(sender_id="w1", receiver_id="sup", message_id=4, expect_reply=False)
        assert svc.evaluate_completion("w1") == 0
        assert env["sent"] == []
        (edge,) = list_open_edges_into("w1")
        assert edge.reply_message_id == 4, "the evaluator must not claim the edge either"

    def test_worker_forwarded_to_its_other_caller_while_one_reply_is_in_flight(self, db, env):
        """Only the edge whose reply is in flight is held back."""
        _armed("sup", "w1")
        _armed("other", "w1")
        svc.on_send_message(sender_id="w1", receiver_id="sup", message_id=4, expect_reply=False)
        assert svc.evaluate_completion("w1") == 1
        assert [(s, r) for s, r, _ in env["sent"]] == [("w1", "other")]
        assert find_open_edge("sup", "w1").reply_message_id == 4

    def test_pending_inbox_blocks(self, db, env):
        _armed("sup", "w1")
        env["pending"].return_value = [object()]
        assert svc.evaluate_completion("w1") == 0

    def test_processing_blocks(self, db, env):
        _armed("sup", "w1")
        env["status"].get_status.return_value = TerminalStatus.PROCESSING
        assert svc.evaluate_completion("w1") == 0

    def test_second_evaluation_does_not_double_forward(self, db, env):
        _armed("sup", "w1")
        svc.evaluate_completion("w1")
        assert svc.evaluate_completion("w1") == 0
        assert len(env["sent"]) == 1

    def test_not_started_on_last_input_blocks(self, db, env):
        _armed("sup", "w1")
        env["status"].started_on_last_input.return_value = False
        env["status"].last_input_at.return_value = svc._monotonic()
        assert svc.evaluate_completion("w1") == 0
        assert env["sent"] == []

    def test_not_started_past_the_pickup_timeout_forwards_with_its_header(
        self, db, env, monkeypatch
    ):
        _armed("sup", "w1")
        env["status"].started_on_last_input.return_value = False
        env["status"].last_input_at.return_value = 100.0
        monkeypatch.setattr(
            svc, "_monotonic", lambda: 100.0 + svc.DELEGATION_PICKUP_TIMEOUT_SECONDS
        )
        assert svc.evaluate_completion("w1") == 1
        text = env["sent"][0][2]
        assert text.startswith(
            "[CAO auto-forward] Worker w1 (profile developer) showed no sign of work on the "
            "last message delivered to it."
        )
        assert f"no work was seen {int(svc.DELEGATION_PICKUP_TIMEOUT_SECONDS)}s" in text
        assert "It can be from an earlier turn." in text
        assert "Check its terminal before you send the message again" in text
        assert "Resend" not in text
        assert text.endswith("---\nfinal answer")

    def test_delivery_in_flight_blocks(self, db, env):
        _armed("sup", "w1")
        env["inbox"].hold_delivery_if_idle.return_value.__enter__.return_value = False
        assert svc.evaluate_completion("w1") == 0
        env["status"].get_status.assert_not_called()

    def test_claim_lost_after_listing_does_not_forward(self, db, env):
        """Two evaluators list the same edge before either claims it: one forwards.

        The first evaluator's listing is returned only after a second
        evaluation of the same terminal has run to completion, so both hold the
        edge when they reach the claim.
        """
        _armed("sup", "w1")
        real_list = svc.list_open_edges_into
        inner = {}

        def list_then_let_another_evaluator_run(terminal_id, **kwargs):
            edges = real_list(terminal_id, **kwargs)
            if "forwarded" not in inner:
                inner["forwarded"] = None  # set first, so the nested call does not recurse
                inner["forwarded"] = svc.evaluate_completion(terminal_id)
            return edges

        with patch(f"{MOD}.list_open_edges_into", side_effect=list_then_let_another_evaluator_run):
            outer = svc.evaluate_completion("w1")

        assert (inner["forwarded"], outer) == (1, 0)
        assert len(env["sent"]) == 1

    def test_unreadable_output_still_forwards(self, db, env):
        _armed("sup", "w1")
        env["ts"].get_output.side_effect = RuntimeError("pane gone")
        assert svc.evaluate_completion("w1") == 1
        assert "(last response could not be read)" in env["sent"][0][2]


class TestWorkerEnded:
    def test_deleted_worker_notifies_caller(self, db, env):
        _armed("sup", "w1")
        svc.handle_worker_ended("w1", "was deleted")
        sender, receiver, text = env["sent"][0]
        assert (sender, receiver) == ("w1", "sup")
        assert text.startswith("[CAO worker ended] Worker w1")
        assert list_open_edges_into("w1") == []

    def test_outbound_edges_closed_silently(self, db, env):
        create_delegation_edge("w1", "w2", EdgeSource.ASSIGN)
        svc.handle_worker_ended("w1", "reached ERROR")
        assert env["sent"] == []
        assert list_open_edges_out_of("w1") == []

    def test_reply_in_flight_gets_no_worker_ended_notice(self, db, env):
        """Spec scenario "Worker deleted with its reply queued" (design D8)."""
        _armed("sup", "w1")
        _armed("other", "w1")
        svc.on_send_message(sender_id="w1", receiver_id="sup", message_id=5, expect_reply=False)
        svc.handle_worker_ended("w1", "was deleted")
        assert [(s, r) for s, r, _ in env["sent"]] == [("w1", "other")]
        (edge,) = list_open_edges_into("w1")
        assert (edge.caller_id, edge.reply_message_id) == ("sup", 5)


class TestSweep:
    def test_sweep_forwards_missed_worker(self, db, env):
        _armed("sup", "w1")
        svc.sweep()
        assert len(env["sent"]) == 1

    def test_cycle_logged_not_forwarded(self, db, env, caplog):
        _armed("w1", "w2")
        _armed("w2", "w1")
        svc.sweep()
        assert env["sent"] == []
        assert "delegation cycle" in caplog.text


class TestConsumer:
    def test_idle_event_evaluates_and_error_event_ends(self):
        registry = object()
        events = [
            {"topic": "terminal.w1.status", "data": {"status": "idle"}},
            {"topic": "terminal.w2.status", "data": {"status": "error"}},
        ]

        class FakeQueue:
            async def get(self):
                if events:
                    return events.pop(0)
                raise asyncio.CancelledError

        with (
            patch(f"{MOD}.bus") as bus,
            patch(f"{MOD}.evaluate_completion") as ev,
            patch(f"{MOD}.handle_worker_ended") as ended,
        ):
            bus.subscribe.return_value = FakeQueue()
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(svc.run(registry))
        ev.assert_called_once_with("w1", registry)
        ended.assert_called_once_with("w2", "reached ERROR", registry, may_be_running=True)


class TestDeliveryDoesNotBlock:
    """A forward or notice is queued with its edge close, then delivered on its own
    thread: no caller waits for a receiver's submit confirmation (PR #1247, review 3)."""

    @pytest.fixture
    def slow_inbox(self, db):
        """Deliveries to "sup1" block until released (at most 3 s); others finish at once.

        ``finished`` is set when the "sup1" delivery returns. A caller that
        waited for it would see it set; one that did not, returns first.
        """
        release, finished = threading.Event(), threading.Event()
        started = {}

        def deliver(receiver_id, *_args, **_kwargs):
            started.setdefault(receiver_id, threading.Event()).set()
            if receiver_id == "sup1":
                release.wait(3)
                finished.set()

        service = InboxService()
        with (
            patch(f"{MOD}.inbox_service", service),
            patch.object(service, "deliver_pending", side_effect=deliver),
            patch(f"{MOD}.status_monitor") as sm,
            patch(f"{MOD}.terminal_service") as ts,
            patch(f"{MOD}.get_terminal_metadata", return_value={"agent_profile": "developer"}),
        ):
            sm.get_status.return_value = TerminalStatus.IDLE
            sm.started_on_last_input.return_value = True
            ts.get_output.return_value = "final answer"
            try:
                yield finished, started
            finally:
                release.set()

    @staticmethod
    def _started(started, receiver_id):
        return started.setdefault(receiver_id, threading.Event()).wait(5)

    def test_worker_ended_returns_before_the_caller_picks_up(self, slow_inbox):
        finished, started = slow_inbox
        _armed("sup1", "w1")

        svc.handle_worker_ended("w1", "was deleted")

        assert self._started(started, "sup1") is True
        assert finished.is_set() is False  # returned while the delivery still waits
        assert list_open_edges_into("w1") == []

    def test_slow_forward_does_not_delay_another_callers_forward(self, slow_inbox):
        finished, started = slow_inbox
        _armed("sup1", "w1")
        _armed("sup2", "w2")

        assert svc.evaluate_completion("w1") == 1
        assert self._started(started, "sup1") is True
        assert svc.evaluate_completion("w2") == 1

        assert self._started(started, "sup2") is True
        assert finished.is_set() is False


class TestRegistryReachesDeliveries:
    """Forwards and notices fire PostSendMessageEvent, as an inbox POST does."""

    def test_forward_is_delivered_with_the_registry(self, db, env):
        registry = object()
        _armed("sup", "w1")
        svc.evaluate_completion("w1", registry)
        env["inbox"].deliver_pending_in_background.assert_called_once_with("sup", registry=registry)

    def test_sweep_passes_the_registry(self, db, env):
        registry = object()
        _armed("sup", "w1")
        svc.sweep(registry)
        env["inbox"].deliver_pending_in_background.assert_called_once_with("sup", registry=registry)

    def test_notice_is_delivered_with_the_registry(self, db, env):
        registry = object()
        _armed("sup", "w1")
        svc.handle_worker_ended("w1", "was deleted", registry)
        env["inbox"].deliver_pending_in_background.assert_called_once_with("sup", registry=registry)


class TestWaitingOnUndeliveredEdge:
    """An outbound edge that is never delivered holds the forward back; say so once."""

    @pytest.fixture(autouse=True)
    def _fresh_warnings(self, monkeypatch):
        monkeypatch.setattr(svc, "_warned_undelivered", set())

    @staticmethod
    def _edge(edge_id, age_seconds, delivered=False):
        now = datetime.now(timezone.utc)
        return DelegationEdge(
            id=edge_id,
            caller_id="sup",
            worker_id=f"w{edge_id}",
            source=EdgeSource.ASSIGN,
            state=EdgeState.OPEN,
            reason=EdgeReason.NO_CALLBACK,
            created_at=now - timedelta(seconds=age_seconds),
            delivered_at=now if delivered else None,
        )

    def test_old_undelivered_edge_is_logged_once(self, caplog):
        old = self._edge(1, svc.DELEGATION_PICKUP_TIMEOUT_SECONDS + 5)
        with caplog.at_level(logging.WARNING, logger=svc.logger.name):
            svc._warn_if_waiting_on_undelivered("sup", [old])
            svc._warn_if_waiting_on_undelivered("sup", [old])
        assert caplog.text.count("whose task has not been delivered") == 1
        assert "sup is not forwarded while it waits on w1" in caplog.text

    def test_young_or_delivered_edges_are_not_logged(self, caplog):
        young = self._edge(1, 5)
        armed = self._edge(2, svc.DELEGATION_PICKUP_TIMEOUT_SECONDS + 5, delivered=True)
        with caplog.at_level(logging.WARNING, logger=svc.logger.name):
            svc._warn_if_waiting_on_undelivered("sup", [young, armed])
        assert "whose task has not been delivered" not in caplog.text

    def test_evaluation_still_holds_the_forward_back(self, db, env, caplog):
        _armed("sup", "w1")
        create_delegation_edge("w1", "w2", EdgeSource.ASSIGN)
        with patch(f"{MOD}._warn_if_waiting_on_undelivered") as warn:
            assert svc.evaluate_completion("w1") == 0
        (outbound,) = warn.call_args.args[1]
        assert (warn.call_args.args[0], outbound.worker_id) == ("w1", "w2")


# --- reply-delivery-failure phase B: notices and reply settlement over a real database ---

C, S, W = "cccc0001", "ssss0002", "wwww0003"
GONE = "gggg0009"  # a terminal with no row: ended


def _msg(id=7, sender=W, receiver=S, text="final reply", is_system_notice=False):
    from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus

    return InboxMessage(
        id=id,
        sender_id=sender,
        receiver_id=receiver,
        message=text,
        status=MessageStatus.FAILED,
        created_at=datetime.now(timezone.utc),
        is_system_notice=is_system_notice,
    )


@pytest.fixture
def live(isolated_memory_db, monkeypatch):
    """Terminals C -> S -> W with real rows, a real inbox and edge rules; the pane is mocked.

    ``delivered`` records every ``_deliver`` the service starts; nothing is
    delivered from it, so notices stay readable as PENDING rows.
    """
    from unittest.mock import MagicMock

    from cli_agent_orchestrator.clients.database import create_terminal
    from cli_agent_orchestrator.services import inbox_service as inbox_module

    create_terminal(C, "cao-s", "c", "claude_code", "conductor")
    create_terminal(S, "cao-s", "s", "claude_code", "supervisor", caller_id=C)
    create_terminal(W, "cao-s", "w", "claude_code", "forge", caller_id=S)

    monitor = MagicMock()
    monitor.get_status.return_value = TerminalStatus.IDLE
    monitor.started_on_last_input.return_value = True
    monkeypatch.setattr(svc, "status_monitor", monitor)
    monkeypatch.setattr(inbox_module, "status_monitor", monitor)
    monkeypatch.setattr(inbox_module, "INBOX_SUBMIT_CONFIRMATION", False)
    screen = MagicMock()
    screen.get_output.side_effect = lambda terminal_id, _mode: f"{terminal_id} SCREEN"
    monkeypatch.setattr(svc, "terminal_service", screen)
    paste = MagicMock()
    paste.dispatch_input.return_value = 0
    monkeypatch.setattr(inbox_module, "terminal_service", paste)
    delivered = []
    monkeypatch.setattr(svc, "_deliver", lambda r, s, registry: delivered.append((r, s, registry)))
    return MagicMock(monitor=monitor, paste=paste, delivered=delivered)


def _send(sender_id, receiver_id, text, expect_reply=False):
    """The inbox POST's bookkeeping: store the message and apply the edge rules."""
    from cli_agent_orchestrator.clients.database import create_inbox_message

    message = create_inbox_message(sender_id, receiver_id, text)
    svc.on_send_message(sender_id, receiver_id, message.id, expect_reply=expect_reply)
    return message.id


def _attempt(receiver_id, report_to_sender=None):
    outcomes = []
    InboxService().deliver_pending(
        receiver_id,
        on_outcome=lambda *a: outcomes.append(a),
        report_to_sender=report_to_sender,
    )
    return outcomes


def _inbox(receiver_id):
    from cli_agent_orchestrator.clients.database import get_inbox_messages

    return get_inbox_messages(receiver_id, limit=50)


def _failure_notices(receiver_id):
    return [m for m in _inbox(receiver_id) if m.message.startswith("[CAO delivery failed]")]


def _not_sent(text):
    from cli_agent_orchestrator.backends import base

    return base.InputNotSentError(text)


class TestMessagesFailed:
    """inbox-delivery "Queued delivery failures are retried and reported": the sender notice."""

    def test_queued_final_failure_tells_the_sender_once(self, live):
        # Act
        svc.handle_messages_failed(
            S, [_msg(id=7)], "InputNotSentError: lost terminal.read", uncertain=False
        )

        # Assert
        (notice,) = _failure_notices(W)
        assert notice.sender_id == S
        assert S in notice.message, "names the receiver"
        assert "7" in notice.message, "names the message id"
        assert "lost terminal.read" in notice.message, "names the error"
        assert "may have" not in notice.message, "nothing was typed, so it did not arrive"
        assert live.delivered == [(W, S, None)]

    def test_uncertain_notice_says_the_text_may_have_arrived(self, live):
        svc.handle_messages_failed(S, [_msg(id=7)], "lost terminal.send", uncertain=True)
        (notice,) = _failure_notices(W)
        assert "may have" in notice.message
        assert "lost terminal.send" in notice.message

    def test_message_reported_to_its_sender_gets_no_notice(self, live):
        svc.handle_messages_failed(S, [_msg(id=7)], "lost", uncertain=False, reported_ids={7})
        assert _inbox(W) == []
        assert live.delivered == []

    def test_only_the_unreported_message_of_a_batch_gets_a_notice(self, live):
        svc.handle_messages_failed(
            S,
            [_msg(id=7, text="first"), _msg(id=8, text="second")],
            "lost",
            uncertain=False,
            reported_ids={7},
        )
        (notice,) = _failure_notices(W)
        assert "8" in notice.message and "7" not in notice.message.replace(S, "")

    @pytest.mark.parametrize(
        "text",
        [
            "[CAO delivery failed] Your message 3 to terminal x was not delivered",
            "[CAO auto-forward] Worker x (profile y) ...",
            "[CAO worker ended] Worker x ...",
        ],
        ids=["delivery-failed", "auto-forward", "worker-ended"],
    )
    def test_failed_cao_notice_never_causes_another_notice(self, live, text):
        """Plan note 12: two terminals cannot trade failure notices."""
        svc.handle_messages_failed(
            S, [_msg(id=7, text=text, is_system_notice=True)], "lost", uncertain=False
        )
        assert _inbox(W) == []

    def test_sender_that_ended_gets_no_notice(self, live):
        svc.handle_messages_failed(S, [_msg(id=7, sender=GONE)], "lost", uncertain=False)
        assert _inbox(GONE) == []
        assert live.delivered == []

    def test_message_that_opened_an_edge_closes_it_and_its_sender_is_told_once(self, live):
        """handle_delivery_failed still runs; the caller of that edge is the sender."""
        # Arrange: S follows up with its own child W, which opens S -> W.
        message_id = _send(S, W, "follow-up")
        assert find_open_edge(S, W).message_id == message_id

        # Act
        svc.handle_messages_failed(
            W,
            [_msg(id=message_id, sender=S, receiver=W, text="follow-up")],
            "lost",
            uncertain=False,
        )

        # Assert
        assert find_open_edge(S, W) is None
        (notice,) = _failure_notices(S)  # one notice, not the edge's plus the sender's
        assert re.search(rf"\b{message_id}\b", notice.message), "names the message id"

    def test_reported_message_that_opened_an_edge_closes_it_silently(self, live):
        """Plan note 12: the sender already has the error from its own send_message."""
        message_id = _send(S, W, "follow-up")
        svc.handle_messages_failed(
            W,
            [_msg(id=message_id, sender=S, receiver=W, text="follow-up")],
            "lost",
            uncertain=False,
            reported_ids={message_id},
        )
        assert find_open_edge(S, W) is None
        assert _failure_notices(S) == []

    def test_registry_reaches_the_notice_delivery(self, live):
        registry = object()
        svc.handle_messages_failed(S, [_msg(id=7)], "lost", uncertain=False, registry=registry)
        assert live.delivered == [(W, S, registry)]


class TestSystemNoticeMark:
    """The loop guard reads the mark in the row, never the text of the message.

    Each notice CAO queues is marked as one. A peer message that only starts
    with ``[CAO `` is not, so its failure is still told to its sender.
    """

    PEER_TEXTS = [
        "[CAO worker ended] Worker x (profile y) ended before sending you a result.",
        "[CAO auto-forward] Worker x (profile y) went idle without calling send_message.",
        "[CAO delivery failed] Your message 3 to terminal x was not delivered",
        "[CAO Handoff] Supervisor terminal ID: x.",
    ]

    def test_the_sender_notice_is_marked(self, live):
        svc.handle_messages_failed(S, [_msg(id=7)], "lost", uncertain=False)
        (notice,) = _failure_notices(W)
        assert notice.is_system_notice is True

    def test_the_edge_notice_is_marked(self, live):
        message_id = _send(S, W, "follow-up")
        svc.handle_messages_failed(
            W,
            [_msg(id=message_id, sender=S, receiver=W, text="follow-up")],
            "lost",
            uncertain=False,
        )
        (notice,) = _failure_notices(S)
        assert notice.is_system_notice is True

    def test_the_reply_lost_notice_is_marked(self, live):
        from cli_agent_orchestrator.clients.database import delete_terminal

        _armed(S, W)
        _send(W, S, "final reply text")
        svc.handle_worker_ended(W, "was deleted")
        delete_terminal(W, end_reason="deleted")
        live.paste.dispatch_input.side_effect = RuntimeError("herdr-core lost terminal.send")

        _attempt(S)

        (notice,) = _failure_notices(S)
        assert "final reply text" in notice.message
        assert notice.is_system_notice is True

    def test_the_task_not_delivered_notice_is_marked(self, live):
        create_delegation_edge(S, W, EdgeSource.ASSIGN)
        assert svc.handle_task_never_delivered(W) == 1
        (notice,) = _failure_notices(S)
        assert notice.is_system_notice is True

    def test_the_worker_ended_notice_is_marked(self, live):
        _armed(S, W)
        svc.handle_worker_ended(W, "was deleted")
        (notice,) = [m for m in _inbox(S) if m.message.startswith("[CAO worker ended]")]
        assert notice.is_system_notice is True

    def test_the_auto_forward_notice_is_marked(self, live):
        _armed(S, W)
        assert svc.evaluate_completion(W) == 1
        (notice,) = [m for m in _inbox(S) if m.message.startswith("[CAO auto-forward]")]
        assert notice.is_system_notice is True

    @pytest.mark.parametrize("text", PEER_TEXTS)
    def test_a_peer_message_that_starts_with_cao_still_gets_its_notice(self, live, text):
        svc.handle_messages_failed(S, [_msg(id=7, text=text)], "lost", uncertain=False)
        (notice,) = _failure_notices(W)
        assert "7" in notice.message, "names the message id"

    @pytest.mark.parametrize("text", PEER_TEXTS)
    def test_a_forwarded_notice_text_that_fails_in_the_queue_is_reported_to_its_sender(
        self, live, text
    ):
        from cli_agent_orchestrator.constants import INBOX_MAX_DELIVERY_ATTEMPTS

        message_id = _send(W, C, text)
        live.paste.dispatch_input.side_effect = [
            _not_sent(f"lost terminal.read on attempt {n}")
            for n in range(1, INBOX_MAX_DELIVERY_ATTEMPTS + 1)
        ]

        for _ in range(INBOX_MAX_DELIVERY_ATTEMPTS):
            _attempt(C)

        (notice,) = _failure_notices(W)
        assert str(message_id) in notice.message

    def test_a_notice_that_fails_in_the_queue_is_not_reported_back(self, live):
        from cli_agent_orchestrator.constants import INBOX_MAX_DELIVERY_ATTEMPTS

        svc.handle_messages_failed(S, [_msg(id=7)], "lost", uncertain=False)
        (notice,) = _failure_notices(W)  # S -> W, queued PENDING by the service
        live.paste.dispatch_input.side_effect = [
            _not_sent(f"lost terminal.read on attempt {n}")
            for n in range(1, INBOX_MAX_DELIVERY_ATTEMPTS + 1)
        ]

        for _ in range(INBOX_MAX_DELIVERY_ATTEMPTS):
            _attempt(W)

        assert [m.status.value for m in _inbox(W) if m.id == notice.id] == ["failed"]
        assert _inbox(S) == [], "S is not told that its notice to W failed"


class TestDeferredInitNoticeMark:
    """The notice a worker's failed deferred init queues for its caller is a CAO notice too."""

    def _notify(self, text="Worker wwww0003 failed to initialize: trust prompt"):
        from cli_agent_orchestrator.services import terminal_service as real_terminal_service

        real_terminal_service._notify_caller_of_deferred_failure(
            W, text, registry=None, delete_worker=False
        )

    def test_the_notice_is_stored_marked(self, live):
        self._notify()

        (notice,) = _inbox(S)
        assert (notice.sender_id, notice.message.startswith("Worker wwww0003")) == (W, True)
        assert notice.is_system_notice is True

    def test_a_notice_that_fails_for_good_gets_the_worker_no_failure_notice(self, live):
        from cli_agent_orchestrator.constants import INBOX_MAX_DELIVERY_ATTEMPTS

        self._notify()
        (notice,) = _inbox(S)
        live.paste.dispatch_input.side_effect = [
            _not_sent(f"lost terminal.read on attempt {n}")
            for n in range(1, INBOX_MAX_DELIVERY_ATTEMPTS + 1)
        ]

        for _ in range(INBOX_MAX_DELIVERY_ATTEMPTS):
            _attempt(S)

        assert [m.status.value for m in _inbox(S) if m.id == notice.id] == ["failed"]
        assert _failure_notices(W) == []
        assert _inbox(W) == [], "the worker did not write that notice, and is told nothing"


class TestQueuedFailureNotices:
    """The same spec requirement through the real deliver_pending, with no edges involved.

    W -> C opens no edge: C is not waiting on W, and C was not created by W.
    """

    def test_failure_on_every_attempt_sends_one_notice_with_the_last_error(self, live):
        """Spec scenario "Failure on every attempt"."""
        from cli_agent_orchestrator.constants import INBOX_MAX_DELIVERY_ATTEMPTS

        message_id = _send(W, C, "status update")
        live.paste.dispatch_input.side_effect = [
            _not_sent(f"lost terminal.read on attempt {n}")
            for n in range(1, INBOX_MAX_DELIVERY_ATTEMPTS + 1)
        ]
        for _ in range(INBOX_MAX_DELIVERY_ATTEMPTS - 1):
            _attempt(C)
        assert _failure_notices(W) == [], "no notice while retries remain"

        _attempt(C)

        (notice,) = _failure_notices(W)
        assert str(message_id) in notice.message
        assert f"attempt {INBOX_MAX_DELIVERY_ATTEMPTS}" in notice.message
        assert "may have" not in notice.message

    def test_uncertain_queued_failure_notice_says_it_may_have_arrived(self, live):
        """Spec scenario "Uncertain queued failure"."""
        _send(W, C, "status update")
        live.paste.dispatch_input.side_effect = RuntimeError("herdr-core lost terminal.send")
        _attempt(C)
        (notice,) = _failure_notices(W)
        assert "may have" in notice.message
        assert live.paste.dispatch_input.call_count == 1

    def test_transient_failure_then_success_sends_no_notice(self, live):
        """Spec scenario "Transient failure, then success"."""
        _send(W, C, "status update")
        live.paste.dispatch_input.side_effect = [_not_sent("lost terminal.read"), 0]
        _attempt(C)
        _attempt(C)
        assert _failure_notices(W) == []
        assert live.paste.dispatch_input.call_count == 2

    def test_reported_failure_sends_no_notice(self, live):
        """Spec "Immediate delivery fails before typing": the POST carries the error instead."""
        message_id = _send(W, C, "status update")
        live.paste.dispatch_input.side_effect = _not_sent("lost terminal.read")
        _attempt(C, report_to_sender=message_id)
        assert _failure_notices(W) == []


class TestQueuedEdgeOpeningFailureNotice:
    """Ruling: the edge notice is the only notice, and it carries the message id.

    C -> S opens an edge (S's recorded caller is C), so a failure of that message
    is reported through ``handle_delivery_failed``'s notice to C.
    """

    def test_failing_on_every_attempt_sends_one_notice_with_the_message_id(self, live):
        # Arrange
        from cli_agent_orchestrator.constants import INBOX_MAX_DELIVERY_ATTEMPTS

        message_id = _send(C, S, "follow-up")
        assert find_open_edge(C, S).message_id == message_id
        live.paste.dispatch_input.side_effect = _not_sent("herdr-core lost terminal.read")

        # Act
        for _ in range(INBOX_MAX_DELIVERY_ATTEMPTS):
            _attempt(S)

        # Assert
        (notice,) = _failure_notices(C)
        assert notice.sender_id == S
        assert S in notice.message, "names the receiver"
        assert re.search(rf"\b{message_id}\b", notice.message), "names the message id"
        assert "lost terminal.read" in notice.message, "names the error"
        assert "may have arrived" not in notice.message
        assert find_open_edge(C, S) is None

    def test_no_notice_while_retries_remain(self, live):
        from cli_agent_orchestrator.constants import INBOX_MAX_DELIVERY_ATTEMPTS

        _send(C, S, "follow-up")
        live.paste.dispatch_input.side_effect = _not_sent("herdr-core lost terminal.read")
        for _ in range(INBOX_MAX_DELIVERY_ATTEMPTS - 1):
            _attempt(S)
        assert _failure_notices(C) == []
        assert find_open_edge(C, S) is not None, "the edge waits for the retry"

    def test_uncertain_failure_notice_says_the_text_may_have_arrived(self, live):
        message_id = _send(C, S, "follow-up")
        live.paste.dispatch_input.side_effect = RuntimeError("herdr-core lost terminal.send")

        _attempt(S)

        (notice,) = _failure_notices(C)
        assert re.search(rf"\b{message_id}\b", notice.message)
        assert "lost terminal.send" in notice.message
        assert "may have arrived" in notice.message
        assert find_open_edge(C, S) is None


class TestReplySettlement:
    """delegation-callback: a reply ends the wait once it is delivered, not when stored."""

    def _assign(self, caller, worker):
        _armed(caller, worker)

    def test_delivered_reply_closes_the_edge_as_replied(self, live):
        """Spec scenario "Worker replies to its caller"."""
        from cli_agent_orchestrator.clients.database import find_any_edge

        self._assign(S, W)
        _send(W, S, "final reply")
        assert find_open_edge(S, W) is not None, "still open while only stored"

        _attempt(S)

        assert find_any_edge(S, W).state == EdgeState.REPLIED
        assert svc.evaluate_completion(W) == 0, "W is not auto-forwarded on its next IDLE"
        assert live.delivered == []

    def test_delivery_of_a_replaced_reply_keeps_the_edge_open_until_the_latest(self, live):
        from cli_agent_orchestrator.clients.database import find_any_edge

        self._assign(S, W)
        _send(W, S, "first reply")
        latest = _send(W, S, "second reply")

        _attempt(S)  # one message per attempt, oldest first: the replaced reply

        assert find_open_edge(S, W).reply_message_id == latest
        _attempt(S)
        assert find_any_edge(S, W).state == EdgeState.REPLIED

    def test_worker_deleted_with_reply_queued_closes_as_replied_on_delivery(self, live):
        """Spec scenario "Worker deleted with its reply queued"."""
        from cli_agent_orchestrator.clients.database import find_any_edge

        self._assign(S, W)
        _send(W, S, "final reply")

        svc.handle_worker_ended(W, "was deleted")

        assert [m for m in _inbox(S) if m.message.startswith("[CAO worker ended]")] == []
        assert find_open_edge(S, W) is not None
        _attempt(S)
        assert find_any_edge(S, W).state == EdgeState.REPLIED

    def test_queued_reply_failing_for_good_reopens_the_wait_and_tells_only_the_sender(self, live):
        """Spec "A reply that fails for good reopens its edge" (sender alive, queued)."""
        self._assign(C, S)
        self._assign(S, W)
        _send(W, S, "final reply")
        live.paste.dispatch_input.side_effect = RuntimeError("herdr-core lost terminal.send")

        _attempt(S)

        edge = find_open_edge(S, W)
        assert edge is not None and edge.reply_message_id is None
        assert [m.message for m in _inbox(S)] == ["final reply"], "S received nothing new"
        assert len(_failure_notices(W)) == 1
        assert _inbox(C) == []

    def test_reply_failing_after_its_sender_ended_tells_the_caller_with_the_reply(self, live):
        """Spec scenario "Reply fails after its sender ended"."""
        from cli_agent_orchestrator.clients.database import delete_terminal

        self._assign(S, W)
        _send(W, S, "final reply text")
        svc.handle_worker_ended(W, "was deleted")
        delete_terminal(W, end_reason="deleted")
        live.paste.dispatch_input.side_effect = RuntimeError("herdr-core lost terminal.send")

        _attempt(S)

        (notice,) = _failure_notices(S)
        assert notice.sender_id == W
        assert W in notice.message, "names the worker"
        assert "final reply text" in notice.message, "carries the stored reply"
        assert find_open_edge(S, W) is None
        assert (S, W, None) in live.delivered


class TestIncident20261117:
    """C -> S -> W: W's reply failed in mark_input_received, before anything was typed."""

    def test_failed_reply_keeps_the_caller_waiting_and_unforwarded(self, live, monkeypatch):
        # Arrange
        self_ids = {"reply": None}
        _armed(C, S)
        _armed(S, W)
        self_ids["reply"] = _send(W, S, "forge's final reply")
        live.paste.dispatch_input.side_effect = _not_sent(
            "TerminalBackendError: herdr-core lost terminal.read: sent nothing for 10.0s"
        )

        # Act: the POST's immediate attempt, reported back to W.
        outcomes = _attempt(S, report_to_sender=self_ids["reply"])

        # Assert: W was told through the POST; S is waiting on W again.
        assert [o[1].value for o in outcomes] == ["failed"]
        edge = find_open_edge(S, W)
        assert edge is not None and edge.reply_message_id is None
        assert [m.status.value for m in _inbox(S)] == ["failed"], "S has received nothing"
        assert _failure_notices(W) == []

        # Past the pickup timeout, with the failed attempt as S's last input,
        # S is still not auto-forwarded to C.
        live.monitor.started_on_last_input.return_value = False
        live.monitor.last_input_at.return_value = 100.0
        monkeypatch.setattr(
            svc, "_monotonic", lambda: 100.0 + svc.DELEGATION_PICKUP_TIMEOUT_SECONDS + 1
        )
        assert svc.evaluate_completion(S) == 0
        svc.sweep()
        assert _inbox(C) == []
        assert [d for d in live.delivered if d[0] == C] == []


# --- review round 1 ---


def _set_status(message_id, status_value):
    from cli_agent_orchestrator.clients.database import update_message_status
    from cli_agent_orchestrator.models.inbox import MessageStatus

    update_message_status(message_id, MessageStatus(status_value))


def _edge_state(caller, worker):
    from cli_agent_orchestrator.clients.database import find_any_edge

    return find_any_edge(caller, worker)


class TestStaleReplyMark:
    """F1, spec "Stale reply mark after a restart": the sweep settles marks nothing else will.

    W stays PROCESSING so the sweep's ordinary evaluation never forwards it:
    only the settling of the mark is observed.
    """

    @pytest.fixture(autouse=True)
    def _worker_busy(self, live):
        live.monitor.get_status.return_value = TerminalStatus.PROCESSING

    def test_restart_after_the_paste_closes_the_edge_as_replied(self, live):
        # Arrange: the reply was typed (DELIVERED), the server died before settling.
        _armed(S, W)
        reply = _send(W, S, "final reply")
        _set_status(reply, "delivered")

        # Act
        svc.sweep()

        # Assert
        assert _edge_state(S, W).state == EdgeState.REPLIED

    def test_settle_that_raised_is_settled_by_the_sweep(self, live, monkeypatch):
        from cli_agent_orchestrator.services import inbox_service as inbox_module

        _armed(S, W)
        reply = _send(W, S, "final reply")
        live.monitor.get_status.side_effect = lambda t: (
            TerminalStatus.IDLE if t == S else TerminalStatus.PROCESSING
        )
        with patch.object(
            inbox_module, "close_edges_replied_by", side_effect=RuntimeError("database locked")
        ):
            _attempt(S)
        assert [m.status.value for m in _inbox(S)] == ["delivered"]
        assert find_open_edge(S, W).reply_message_id == reply, "the settle failed"

        svc.sweep()

        assert _edge_state(S, W).state == EdgeState.REPLIED

    def test_reply_delivered_before_its_mark_closes_the_edge_at_once(self, live):
        """The concurrent race: the delivery settled before on_send_message marked."""
        from cli_agent_orchestrator.clients.database import create_inbox_message

        _armed(S, W)
        reply = create_inbox_message(W, S, "final reply").id
        live.monitor.get_status.side_effect = lambda t: (
            TerminalStatus.IDLE if t == S else TerminalStatus.PROCESSING
        )
        _attempt(S)  # typed and settled; no edge carried the mark yet
        assert [m.status.value for m in _inbox(S)] == ["delivered"]

        svc.on_send_message(sender_id=W, receiver_id=S, message_id=reply, expect_reply=False)

        assert _edge_state(S, W).state == EdgeState.REPLIED

    def test_failed_reply_mark_is_cleared_without_a_notice(self, live):
        _armed(S, W)
        reply = _send(W, S, "final reply")
        _set_status(reply, "failed")

        svc.sweep()

        edge = find_open_edge(S, W)
        assert edge is not None and edge.reply_message_id is None
        assert _failure_notices(S) == []
        assert [m.message for m in _inbox(S)] == ["final reply"], "S received nothing"

    def test_mark_whose_message_row_is_gone_is_cleared(self, live):
        from cli_agent_orchestrator.clients import database

        _armed(S, W)
        edge = find_open_edge(S, W)
        database.mark_reply_in_flight(edge.id, 999_999)

        svc.sweep()

        after = find_open_edge(S, W)
        assert after is not None and after.reply_message_id is None
        assert _inbox(S) == []

    @pytest.mark.parametrize("mark", ["failed", "missing_row"])
    def test_stale_mark_of_an_ended_worker_tells_the_caller_and_closes(self, live, mark):
        """Spec "Reply fails after its sender ended", reached through the sweep."""
        from cli_agent_orchestrator.clients import database

        # Arrange: W replied, then ended; the reply's outcome was never settled.
        _armed(S, W)
        if mark == "failed":
            reply = _send(W, S, "final reply text")
            _set_status(reply, "failed")
        else:
            database.mark_reply_in_flight(find_open_edge(S, W).id, 999_999)
        svc.handle_worker_ended(W, "was deleted")
        database.delete_terminal(W, end_reason="deleted")

        # Act
        svc.sweep()

        # Assert
        (notice,) = _failure_notices(S)
        assert W in notice.message, "names the worker"
        if mark == "failed":
            assert "final reply text" in notice.message, "carries the stored reply"
        assert find_open_edge(S, W) is None
        assert (S, W, None) in live.delivered, "the notice delivery was started"
        assert [m for m in _inbox(S) if m.message.startswith("[CAO worker ended]")] == []

    def test_pending_reply_keeps_its_mark(self, live):
        """A reply still queued is in flight, not stale."""
        _armed(S, W)
        reply = _send(W, S, "final reply")

        svc.sweep()

        assert find_open_edge(S, W).reply_message_id == reply


class TestClaimSkipsAReplyInFlight:
    """F3: a reply marked between the listing and the claim makes the claim fail."""

    def _mark_after_listing(self, monkeypatch):
        """List as usual, then let W's reply to S be marked before the claim."""
        real_list = svc.list_open_edges_into
        state = {"marked": None}

        def list_then_reply(terminal_id, **kwargs):
            edges = real_list(terminal_id, **kwargs)
            if state["marked"] is None and terminal_id == W:
                state["marked"] = -1  # on_send_message lists too: mark only once
                state["marked"] = _send(W, S, "final reply")
            return edges

        monkeypatch.setattr(svc, "list_open_edges_into", list_then_reply)
        return state

    def test_no_auto_forward(self, live, monkeypatch):
        _armed(S, W)
        state = self._mark_after_listing(monkeypatch)

        forwarded = svc.evaluate_completion(W)

        assert forwarded == 0
        assert [m.message for m in _inbox(S)] == ["final reply"], "no [CAO auto-forward]"
        assert find_open_edge(S, W).reply_message_id == state["marked"]
        assert live.delivered == []

    def test_no_worker_ended_notice(self, live, monkeypatch):
        _armed(S, W)
        state = self._mark_after_listing(monkeypatch)

        svc.handle_worker_ended(W, "was deleted")

        assert [m.message for m in _inbox(S)] == ["final reply"], "no [CAO worker ended]"
        assert find_open_edge(S, W).reply_message_id == state["marked"]
        assert live.delivered == []


class TestPlainClosesSkipAReplyInFlight:
    """F3 residual: closes that filter only on OPEN must also skip an edge marked since listing."""

    @staticmethod
    def _worker_ends_while_s_waits_and_w_replies(monkeypatch):
        """Drive handle_worker_ended(W) into close_open_edges_for_terminal's window.

        handle_worker_ended's own listing runs first and sees no edge into W, so
        no notice is claimed. Then S -> W is opened (an assign that landed in
        between) and W's reply to S is stored; close_open_edges_for_terminal lists
        S -> W unmarked, and the mark lands before its close.
        """
        from cli_agent_orchestrator.clients import database
        from cli_agent_orchestrator.clients.database import create_inbox_message

        state = {"reply": None}
        real_svc_list = svc.list_open_edges_into
        real_db_list = database.list_open_edges_into

        def svc_list(terminal_id, **kwargs):
            edges = real_svc_list(terminal_id, **kwargs)
            if terminal_id == W and state["reply"] is None:
                state["reply"] = -1
                _armed(S, W)
                state["reply"] = create_inbox_message(W, S, "final reply text").id
            return edges

        def db_list(terminal_id, **kwargs):
            edges = real_db_list(terminal_id, **kwargs)
            if terminal_id == W and kwargs.get("exclude_reply_in_flight"):
                for edge in edges:
                    if edge.caller_id == S:
                        database.mark_reply_in_flight(edge.id, state["reply"])
            return edges

        monkeypatch.setattr(svc, "list_open_edges_into", svc_list)
        monkeypatch.setattr(database, "list_open_edges_into", db_list)
        state["restore"] = lambda: (
            monkeypatch.setattr(svc, "list_open_edges_into", real_svc_list),
            monkeypatch.setattr(database, "list_open_edges_into", real_db_list),
        )
        return state

    def test_worker_end_close_leaves_a_newly_marked_edge_open(self, live, monkeypatch):
        """(a) close_open_edges_for_terminal via handle_worker_ended."""
        state = self._worker_ends_while_s_waits_and_w_replies(monkeypatch)

        svc.handle_worker_ended(W, "was deleted")

        edge = find_open_edge(S, W)
        assert edge is not None, "the close must skip an edge whose reply is in flight"
        assert edge.reply_message_id == state["reply"]
        assert [m for m in _inbox(S) if m.message.startswith("[CAO ")] == []

    def test_reported_message_close_leaves_a_newly_marked_edge_open(self, live, monkeypatch):
        """(b) handle_messages_failed's close of an edge a reported message opened."""
        follow_up = _send(S, W, "follow-up")  # W's recorded caller is S: opens S -> W
        real_list = svc.list_open_message_edges
        state = {"reply": None}

        def list_then_reply(message_ids):
            edges = real_list(message_ids)
            if follow_up in message_ids and edges and state["reply"] is None:
                state["reply"] = -1
                state["reply"] = _send(W, S, "answer")  # marks S -> W
            return edges

        monkeypatch.setattr(svc, "list_open_message_edges", list_then_reply)

        svc.handle_messages_failed(
            W,
            [_msg(id=follow_up, sender=S, receiver=W, text="follow-up")],
            "lost",
            uncertain=False,
            reported_ids={follow_up},
        )

        edge = find_open_edge(S, W)
        assert edge is not None, "the close must skip an edge whose reply is in flight"
        assert edge.reply_message_id == state["reply"]
        assert _failure_notices(S) == []

    def test_reply_that_then_fails_after_the_worker_ended_reaches_the_caller(
        self, live, monkeypatch
    ):
        """(c) after (a): the reply fails for good, W is gone, and the sweep tells S."""
        from cli_agent_orchestrator.clients import database

        state = self._worker_ends_while_s_waits_and_w_replies(monkeypatch)
        svc.handle_worker_ended(W, "was deleted")
        state["restore"]()  # the window is over; the listings run unwrapped from here
        database.delete_terminal(W, end_reason="deleted")
        _set_status(state["reply"], "failed")

        svc.sweep()

        (notice,) = _failure_notices(S)
        assert W in notice.message
        assert "final reply text" in notice.message
        assert find_open_edge(S, W) is None


class TestExceptionAfterThePaste:
    """F4: a failure in the confirmation leaves a typed message DELIVERED and settled."""

    @pytest.fixture
    def confirmation_raises(self, live, monkeypatch):
        from unittest.mock import MagicMock

        from cli_agent_orchestrator.services import inbox_service as inbox_module

        backend = MagicMock()
        backend.supports_event_inbox.return_value = False
        monkeypatch.setattr(inbox_module, "INBOX_SUBMIT_CONFIRMATION", True)
        monkeypatch.setattr(inbox_module, "get_backend", lambda: backend)
        monkeypatch.setattr(
            InboxService,
            "_confirm_submitted",
            lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("status monitor gone")),
        )

    def test_reply_closes_as_replied_and_nobody_is_told(self, live, confirmation_raises):
        _armed(S, W)
        reply = _send(W, S, "final reply")

        outcomes = _attempt(S, report_to_sender=reply)

        assert [(o[0], o[1].value) for o in outcomes] == [(reply, "delivered")]
        assert [m.status.value for m in _inbox(S)] == ["delivered"]
        assert _edge_state(S, W).state == EdgeState.REPLIED
        assert _failure_notices(W) == []

    def test_edge_the_message_opened_is_armed(self, live, confirmation_raises):
        message_id = _send(C, S, "follow-up")  # S's recorded caller is C: opens C -> S

        _attempt(S)

        edge = find_open_edge(C, S)
        assert edge is not None and edge.message_id == message_id
        assert edge.delivered_at is not None, "armed"
        assert [m.status.value for m in _inbox(S)] == ["delivered"]
        assert _failure_notices(C) == []


class TestPermanentRefusalOfAQueuedMessage:
    """Spec scenario "Permanent refusal of a queued message"."""

    def test_failed_at_once_and_the_sender_told_once(self, live):
        from cli_agent_orchestrator.models.terminal import (
            ProviderExitedInputBlockedError,
            TerminalInputBlockedReason,
        )

        message_id = _send(W, C, "status")  # W -> C opens no edge
        live.paste.dispatch_input.side_effect = ProviderExitedInputBlockedError(
            "provider exited", reason=TerminalInputBlockedReason.PROVIDER_ERROR
        )

        _attempt(C)
        _attempt(C)

        assert [m.status.value for m in _inbox(C)] == ["failed"]
        assert live.paste.dispatch_input.call_count == 1, "never retried"
        (notice,) = _failure_notices(W)
        assert re.search(rf"\b{message_id}\b", notice.message)
