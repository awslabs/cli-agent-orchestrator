"""Tests for delegation_callback_service."""

import asyncio
import logging
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    close_delegation_edge,
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

    def test_auto_forward_no_tool_reason(self):
        text = svc.build_auto_forward_message("w1", "p", EdgeReason.NO_SEND_MESSAGE_TOOL, "x")
        assert "Reason: has no send_message tool (cao-mcp-server is not in its profile)" in text

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
    def test_reply_closes_owed_edge(self, db):
        create_delegation_edge("sup", "w1", EdgeSource.ASSIGN)
        svc.on_send_message(sender_id="w1", receiver_id="sup", message_id=1, expect_reply=False)
        assert list_open_edges_into("w1") == []

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

    def claim_and_record(edge_id, state, sender_id, receiver_id, message):
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

    def test_forwarded_after_sub_worker_replies(self, db, env):
        _armed("sup", "w1")
        create_delegation_edge("w1", "w2", EdgeSource.ASSIGN)
        svc.on_send_message(sender_id="w2", receiver_id="w1", message_id=3, expect_reply=False)
        assert svc.evaluate_completion("w1") == 1

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

        def list_then_let_another_evaluator_run(terminal_id, armed_only=False):
            edges = real_list(terminal_id, armed_only=armed_only)
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
