"""Tests for the unmanaged wake-confirmation watcher (scoped cond-0072).

The watcher is owned by the InboxService event loop and watches a parked
unmanaged receiver for a transition out of IDLE after a paste.  It records a
durable wake receipt, nudges at most once, and never re-nudges across
restart or reconcile.  These tests drive the bus directly and assert on the
durable sidecar (the truth) rather than on in-memory state.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import inbox_service, wake_receipts
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.services.inbox_service import InboxService


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(wake_receipts, "WAKE_RECEIPT_DIR", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def terminal_identity(monkeypatch):
    def identity(terminal_id):
        return {
            "id": terminal_id,
            "provider": "codex",
            "session_name": "cao-test",
            "pane_id": "%1",
            "window_id": "@1",
        }

    monkeypatch.setattr(inbox_service.terminal_service, "get_terminal", identity)
    monkeypatch.setattr(
        inbox_service.status_monitor, "get_status", lambda _tid: TerminalStatus.IDLE
    )
    return identity("term-1")


@pytest_asyncio.fixture
async def bus_on_loop():
    """Bind the module event bus to the running test loop, then restore it."""
    loop = asyncio.get_running_loop()
    bus.set_loop(loop)
    yield loop
    bus.set_loop(None)


def _future_deadline(seconds: float = 10.0) -> str:
    return wake_receipts.deadline_iso(wake_receipts.utcnow(), seconds)


def _past_deadline() -> str:
    return wake_receipts.deadline_iso(wake_receipts.utcnow(), -1.0)


class TestWakeConfirmed:
    @pytest.mark.asyncio
    async def test_a_transition_out_of_idle_confirms(
        self, store, bus_on_loop, monkeypatch, terminal_identity
    ):
        monkeypatch.setattr(
            inbox_service.status_monitor, "get_status", lambda tid: TerminalStatus.IDLE
        )
        nudge = MagicMock()
        monkeypatch.setattr(inbox_service.terminal_service, "send_special_key", nudge)
        wake_receipts.ensure_watching(
            "term-1",
            "1202",
            native_session_id=None,
            delivered_at=wake_receipts.utcnow(),
            deadline_at=_future_deadline(),
            delivery_identity=terminal_identity,
        )
        svc = InboxService()
        svc._loop = bus_on_loop
        task = asyncio.create_task(
            svc._watch_wake(
                "term-1",
                "1202",
                _future_deadline(),
                delivery_identity=terminal_identity,
            )
        )
        await asyncio.sleep(0)  # let the watcher subscribe
        bus.publish("terminal.term-1.status", {"status": TerminalStatus.PROCESSING.value})
        await asyncio.wait_for(task, timeout=2.0)
        record = wake_receipts.get("term-1", "1202")
        assert record["state"] == wake_receipts.WAKE_CONFIRMED
        assert record["observed"]["to_status"] == TerminalStatus.PROCESSING.value
        # A wake needs no nudge.
        assert nudge.call_count == 0

    @pytest.mark.asyncio
    async def test_a_transition_is_bound_to_the_exact_message_id(
        self, store, bus_on_loop, monkeypatch, terminal_identity
    ):
        monkeypatch.setattr(
            inbox_service.status_monitor, "get_status", lambda tid: TerminalStatus.IDLE
        )
        monkeypatch.setattr(inbox_service.terminal_service, "send_special_key", MagicMock())
        for mid in ("1202", "1207"):
            wake_receipts.ensure_watching(
                "term-1",
                mid,
                native_session_id=None,
                delivered_at=wake_receipts.utcnow(),
                deadline_at=_future_deadline(),
                delivery_identity=terminal_identity,
            )
        svc = InboxService()
        svc._loop = bus_on_loop
        tasks = [
            asyncio.create_task(
                svc._watch_wake(
                    "term-1",
                    mid,
                    _future_deadline(),
                    delivery_identity=terminal_identity,
                )
            )
            for mid in ("1202", "1207")
        ]
        await asyncio.sleep(0)
        bus.publish("terminal.term-1.status", {"status": TerminalStatus.PROCESSING.value})
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2.0)
        # Each message confirms independently, exactly once.
        assert wake_receipts.get("term-1", "1202")["state"] == wake_receipts.WAKE_CONFIRMED
        assert wake_receipts.get("term-1", "1207")["state"] == wake_receipts.WAKE_CONFIRMED

    @pytest.mark.asyncio
    async def test_a_transition_from_a_replaced_terminal_does_not_confirm(
        self, store, bus_on_loop, monkeypatch, terminal_identity
    ):
        current_identity = dict(terminal_identity)
        monkeypatch.setattr(
            inbox_service.terminal_service,
            "get_terminal",
            lambda _terminal_id: dict(current_identity),
        )
        wake_receipts.ensure_watching(
            "term-1",
            "1208",
            native_session_id=None,
            delivered_at=wake_receipts.utcnow(),
            deadline_at=_future_deadline(),
            delivery_identity=terminal_identity,
        )
        svc = InboxService()
        svc._loop = bus_on_loop
        task = asyncio.create_task(
            svc._watch_wake(
                "term-1",
                "1208",
                _future_deadline(),
                delivery_identity=terminal_identity,
            )
        )
        await asyncio.sleep(0)
        current_identity["pane_id"] = "%99"
        bus.publish("terminal.term-1.status", {"status": TerminalStatus.PROCESSING.value})

        await asyncio.wait_for(task, timeout=2.0)

        record = wake_receipts.get("term-1", "1208")
        assert record["state"] == wake_receipts.WAKE_UNCONFIRMED
        assert "identity changed" in record["note"]


class TestOneNudge:
    @pytest.mark.asyncio
    async def test_no_transition_nudges_once_then_unconfirms(
        self, store, bus_on_loop, monkeypatch, terminal_identity
    ):
        monkeypatch.setattr(
            inbox_service.status_monitor, "get_status", lambda tid: TerminalStatus.IDLE
        )
        nudge = MagicMock()
        monkeypatch.setattr(inbox_service.terminal_service, "send_special_key", nudge)
        # Collapse the post-nudge window so the test does not wait in real time.
        monkeypatch.setattr(inbox_service, "WAKE_NUDGE_WINDOW_SECONDS", 0.0)
        wake_receipts.ensure_watching(
            "term-1",
            "1202",
            native_session_id=None,
            delivered_at=wake_receipts.utcnow(),
            deadline_at=_past_deadline(),
            delivery_identity=terminal_identity,
            baseline_status=TerminalStatus.IDLE.value,
        )
        svc = InboxService()
        svc._loop = bus_on_loop
        await asyncio.wait_for(
            svc._watch_wake(
                "term-1",
                "1202",
                _past_deadline(),
                baseline_status=TerminalStatus.IDLE.value,
                delivery_identity=terminal_identity,
            ),
            timeout=2.0,
        )
        record = wake_receipts.get("term-1", "1202")
        assert record["state"] == wake_receipts.WAKE_UNCONFIRMED
        # Exactly one bare Enter, never re-pasting text.
        assert nudge.call_count == 1
        assert nudge.call_args.args == ("term-1", "Enter")
        assert record["nudge_intent_at"] is not None
        assert record["nudge_sent_at"] is not None


class TestEnsureIsIdempotent:
    def test_two_ensures_open_one_record(self, store, monkeypatch):
        svc = InboxService()
        monkeypatch.setattr(
            inbox_service.managed_launch, "managed_control_identity", lambda tid: None
        )
        svc._ensure_wake_confirmation("term-1", "1202")
        svc._ensure_wake_confirmation("term-1", "1202")
        files = list(store.glob("*.json"))
        non_lock = [p for p in files if not p.name.endswith(".lock")]
        assert len(non_lock) == 1

    def test_an_absent_loop_still_writes_the_watching_sidecar(self, store, monkeypatch):
        # A sync caller before run() starts: no watcher is armed, but the
        # durable ``watching`` record is the truth a later startup will load.
        svc = InboxService()  # _loop is None
        monkeypatch.setattr(
            inbox_service.managed_launch, "managed_control_identity", lambda tid: None
        )
        svc._ensure_wake_confirmation("term-1", "1202")
        record = wake_receipts.get("term-1", "1202")
        assert record is not None
        assert record["state"] == wake_receipts.WATCHING

    def test_concurrent_ensures_from_two_threads_open_one_record(self, store, monkeypatch):
        svc = InboxService()
        monkeypatch.setattr(
            inbox_service.managed_launch, "managed_control_identity", lambda tid: None
        )
        barrier = threading.Barrier(2)

        def go():
            barrier.wait()
            svc._ensure_wake_confirmation("term-1", "1202")

        t1 = threading.Thread(target=go)
        t2 = threading.Thread(target=go)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        non_lock = [p for p in store.glob("*.json") if not p.name.endswith(".lock")]
        assert len(non_lock) == 1


class TestDeliveryOrdering:
    def test_observer_setup_failure_cannot_leave_an_unsent_message_delivered(
        self, store, monkeypatch
    ):
        message = InboxMessage(
            id=9200,
            sender_id="sender",
            receiver_id="term-1",
            message="not-sent",
            status=MessageStatus.PENDING,
            created_at=datetime.now(),
        )
        statuses = []
        send = MagicMock()
        monkeypatch.setattr(inbox_service, "get_pending_messages", lambda _tid, limit=1: [message])
        monkeypatch.setattr(
            inbox_service, "update_message_status", lambda mid, status: statuses.append(status)
        )
        monkeypatch.setattr(
            inbox_service.managed_launch, "managed_control_identity", lambda _tid: None
        )
        monkeypatch.setattr(
            inbox_service.managed_launch,
            "deliver_inbox_via_bridge",
            lambda *_args, **_kwargs: False,
        )
        monkeypatch.setattr(inbox_service.terminal_service, "send_input", send)
        service = InboxService()
        monkeypatch.setattr(
            service,
            "_prepare_wake_confirmation",
            MagicMock(side_effect=RuntimeError("observer setup failed")),
        )

        service.deliver_pending("term-1")

        send.assert_not_called()
        assert statuses == [MessageStatus.DELIVERED, MessageStatus.FAILED]

    def test_post_send_receipt_failure_does_not_license_duplicate_delivery(
        self, store, monkeypatch
    ):
        message = InboxMessage(
            id=9203,
            sender_id="sender",
            receiver_id="term-1",
            message="sent-once",
            status=MessageStatus.PENDING,
            created_at=datetime.now(),
        )
        statuses = []
        send = MagicMock()
        monkeypatch.setattr(inbox_service, "get_pending_messages", lambda _tid, limit=1: [message])
        monkeypatch.setattr(
            inbox_service, "update_message_status", lambda mid, status: statuses.append(status)
        )
        monkeypatch.setattr(
            inbox_service.managed_launch, "managed_control_identity", lambda _tid: None
        )
        monkeypatch.setattr(
            inbox_service.managed_launch,
            "deliver_inbox_via_bridge",
            lambda *_args, **_kwargs: False,
        )
        monkeypatch.setattr(inbox_service.terminal_service, "send_input", send)
        service = InboxService()
        monkeypatch.setattr(
            service,
            "_commit_wake_confirmation",
            MagicMock(side_effect=RuntimeError("receipt persistence failed")),
        )

        service.deliver_pending("term-1")

        send.assert_called_once_with("term-1", "sent-once")
        assert statuses == [MessageStatus.DELIVERED]

    @pytest.mark.asyncio
    async def test_transition_inside_send_is_observed_without_a_later_nudge(
        self, store, bus_on_loop, monkeypatch
    ):
        terminal_id = "term-1"
        message = InboxMessage(
            id=9201,
            sender_id="sender",
            receiver_id=terminal_id,
            message="callback",
            status=MessageStatus.PENDING,
            created_at=datetime.now(),
        )
        current = {"status": TerminalStatus.IDLE}
        nudges = MagicMock()

        monkeypatch.setattr(
            inbox_service.status_monitor,
            "get_status",
            lambda _tid: current["status"],
        )
        monkeypatch.setattr(inbox_service, "get_pending_messages", lambda _tid, limit=1: [message])
        monkeypatch.setattr(inbox_service, "update_message_status", lambda *_args: None)
        monkeypatch.setattr(
            inbox_service.managed_launch, "managed_control_identity", lambda _tid: None
        )
        monkeypatch.setattr(
            inbox_service.managed_launch,
            "deliver_inbox_via_bridge",
            lambda *_args, **_kwargs: False,
        )

        def send_input(_terminal_id, _text):
            current["status"] = TerminalStatus.PROCESSING
            bus.publish(
                f"terminal.{terminal_id}.status",
                {"status": TerminalStatus.PROCESSING.value},
            )

        monkeypatch.setattr(inbox_service.terminal_service, "send_input", send_input)
        monkeypatch.setattr(inbox_service.terminal_service, "send_special_key", nudges)
        monkeypatch.setattr(inbox_service, "WAKE_CONFIRMATION_SECONDS", 0.05)
        monkeypatch.setattr(inbox_service, "WAKE_NUDGE_WINDOW_SECONDS", 0.0)

        service = InboxService()
        service._loop = bus_on_loop
        await asyncio.to_thread(service.deliver_pending, terminal_id)
        current["status"] = TerminalStatus.IDLE
        bus.publish(
            f"terminal.{terminal_id}.status",
            {"status": TerminalStatus.IDLE.value},
        )
        await asyncio.sleep(0.08)

        receipt = wake_receipts.get(terminal_id, "9201")
        assert receipt["state"] == wake_receipts.WAKE_CONFIRMED
        assert receipt["observed"]["to_status"] == TerminalStatus.PROCESSING.value
        nudges.assert_not_called()

    @pytest.mark.asyncio
    async def test_eager_processing_delivery_never_arms_or_nudges(
        self, store, bus_on_loop, monkeypatch
    ):
        message = InboxMessage(
            id=9202,
            sender_id="sender",
            receiver_id="term-1",
            message="follow-up",
            status=MessageStatus.PENDING,
            created_at=datetime.now(),
        )
        monkeypatch.setattr(
            inbox_service.status_monitor,
            "get_status",
            lambda _tid: TerminalStatus.PROCESSING,
        )
        monkeypatch.setattr(inbox_service, "get_pending_messages", lambda _tid, limit=1: [message])
        monkeypatch.setattr(inbox_service, "update_message_status", lambda *_args: None)
        monkeypatch.setattr(
            inbox_service.managed_launch, "managed_control_identity", lambda _tid: None
        )
        monkeypatch.setattr(
            inbox_service.managed_launch,
            "deliver_inbox_via_bridge",
            lambda *_args, **_kwargs: False,
        )
        provider = MagicMock(accepts_input_while_processing=True)
        monkeypatch.setattr(inbox_service.provider_manager, "get_provider", lambda _tid: provider)
        monkeypatch.setattr(inbox_service, "EAGER_INBOX_DELIVERY", True)
        send = MagicMock()
        nudge = MagicMock()
        monkeypatch.setattr(inbox_service.terminal_service, "send_input", send)
        monkeypatch.setattr(inbox_service.terminal_service, "send_special_key", nudge)

        service = InboxService()
        service._loop = bus_on_loop
        await asyncio.to_thread(service.deliver_pending, "term-1")
        await asyncio.sleep(0.02)

        send.assert_called_once()
        nudge.assert_not_called()
        assert wake_receipts.get("term-1", "9202") is None


class TestRestartNeverReNudges:
    def test_a_past_deadline_watching_record_finalizes_unconfirmed_without_nudging(
        self, store, monkeypatch
    ):
        nudge = MagicMock()
        monkeypatch.setattr(inbox_service.terminal_service, "send_special_key", nudge)
        # A record left ``watching`` by a process that died mid-nudge-decision,
        # now past its deadline.  No loop is needed: a past-deadline record is
        # finalized without arming a watcher at all.
        wake_receipts.ensure_watching(
            "term-1",
            "1202",
            native_session_id=None,
            delivered_at=wake_receipts.utcnow(),
            deadline_at=_past_deadline(),
        )
        svc = InboxService()
        svc._load_wake_confirmations()
        assert wake_receipts.get("term-1", "1202")["state"] == wake_receipts.WAKE_UNCONFIRMED
        # The in-flight nudge decision did not survive; fail closed, no nudge.
        assert nudge.call_count == 0

    @pytest.mark.asyncio
    async def test_a_record_with_nudge_intent_is_never_re_nudged_on_reload(
        self, store, bus_on_loop, monkeypatch
    ):
        nudge = MagicMock()
        monkeypatch.setattr(inbox_service.terminal_service, "send_special_key", nudge)
        monkeypatch.setattr(
            inbox_service.status_monitor, "get_status", lambda tid: TerminalStatus.IDLE
        )
        monkeypatch.setattr(inbox_service, "WAKE_NUDGE_WINDOW_SECONDS", 0.0)
        # A near deadline so the re-armed watcher expires within the test, and
        # a recorded intent from a prior incarnation that crashed mid-nudge.
        wake_receipts.ensure_watching(
            "term-1",
            "1202",
            native_session_id=None,
            delivered_at=wake_receipts.utcnow(),
            deadline_at=_future_deadline(0.2),
        )
        wake_receipts.record_nudge_intent("term-1", "1202", at=wake_receipts.utcnow())
        svc = InboxService()
        svc._loop = bus_on_loop
        svc._load_wake_confirmations()
        # Let the re-armed (observation-only) watcher reach its deadline.
        await asyncio.sleep(0.45)
        assert nudge.call_count == 0
        assert wake_receipts.get("term-1", "1202")["state"] == wake_receipts.WAKE_UNCONFIRMED

    @pytest.mark.asyncio
    async def test_a_future_record_without_prior_intent_is_observation_only_after_restart(
        self, store, bus_on_loop, monkeypatch
    ):
        nudge = MagicMock()
        monkeypatch.setattr(inbox_service.terminal_service, "send_special_key", nudge)
        monkeypatch.setattr(
            inbox_service.status_monitor, "get_status", lambda tid: TerminalStatus.IDLE
        )
        wake_receipts.ensure_watching(
            "term-1",
            "1203",
            native_session_id=None,
            delivered_at=wake_receipts.utcnow(),
            deadline_at=_future_deadline(0.2),
        )
        svc = InboxService()
        svc._loop = bus_on_loop

        svc._load_wake_confirmations()
        await asyncio.sleep(0.45)

        record = wake_receipts.get("term-1", "1203")
        assert nudge.call_count == 0
        assert record["state"] == wake_receipts.WAKE_UNCONFIRMED
        assert record["nudge_intent_at"] is None
        assert "observation-only" in record["note"]


class TestManagedPathUnchanged:
    def test_a_managed_paste_does_not_open_a_wake_receipt(self, store, monkeypatch):
        # The managed bridge records its own provider-native ack; the wake
        # receipt is for the unmanaged paste path only.
        from datetime import datetime

        from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus

        msg = InboxMessage(
            id=1,
            sender_id="s",
            receiver_id="term-1",
            message="hi",
            status=MessageStatus.PENDING,
            created_at=datetime.now(),
        )
        monkeypatch.setattr(inbox_service, "get_pending_messages", lambda tid, limit=1: [msg])
        monkeypatch.setattr(
            inbox_service.managed_launch,
            "managed_control_identity",
            lambda tid: {"generation": "g-1"},
        )
        monkeypatch.setattr(
            inbox_service.managed_launch, "deliver_inbox_via_bridge", lambda *a, **k: True
        )
        monkeypatch.setattr(inbox_service, "update_message_status", lambda *a, **k: None)
        svc = InboxService()
        svc.deliver_pending("term-1")
        assert wake_receipts.get("term-1", "1") is None
