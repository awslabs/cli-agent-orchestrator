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
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import inbox_service, wake_receipts
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.services.inbox_service import InboxService


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(wake_receipts, "WAKE_RECEIPT_DIR", tmp_path)
    return tmp_path


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
    async def test_a_transition_out_of_idle_confirms(self, store, bus_on_loop, monkeypatch):
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
        )
        svc = InboxService()
        svc._loop = bus_on_loop
        task = asyncio.create_task(svc._watch_wake("term-1", "1202", _future_deadline()))
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
        self, store, bus_on_loop, monkeypatch
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
            )
        svc = InboxService()
        svc._loop = bus_on_loop
        tasks = [
            asyncio.create_task(svc._watch_wake("term-1", mid, _future_deadline()))
            for mid in ("1202", "1207")
        ]
        await asyncio.sleep(0)
        bus.publish("terminal.term-1.status", {"status": TerminalStatus.PROCESSING.value})
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2.0)
        # Each message confirms independently, exactly once.
        assert wake_receipts.get("term-1", "1202")["state"] == wake_receipts.WAKE_CONFIRMED
        assert wake_receipts.get("term-1", "1207")["state"] == wake_receipts.WAKE_CONFIRMED


class TestOneNudge:
    @pytest.mark.asyncio
    async def test_no_transition_nudges_once_then_unconfirms(self, store, bus_on_loop, monkeypatch):
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
        )
        svc = InboxService()
        svc._loop = bus_on_loop
        await asyncio.wait_for(svc._watch_wake("term-1", "1202", _past_deadline()), timeout=2.0)
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
