"""Tests for the durable unmanaged wake-receipt sidecar.

The sidecar is the truth and the idempotency boundary for the wake watcher:
one record per ``(terminal_id, message_id)``, written atomically, and the
state machine that makes at-most-one-watcher and at-most-one-nudge fall out
of "does a record already exist for this key?".
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services import wake_receipts


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(wake_receipts, "WAKE_RECEIPT_DIR", tmp_path)
    return tmp_path


def _open(store, terminal_id="term-1", message_id="1202", **kwargs):
    fields = {
        "native_session_id": None,
        "delivered_at": "2026-07-26T12:00:00+00:00",
        "deadline_at": "2026-07-26T12:00:45+00:00",
    }
    fields.update(kwargs)
    return wake_receipts.ensure_watching(terminal_id, message_id, **fields)


class TestSchemaAndDefaults:
    def test_a_watching_record_has_the_pinned_shape(self, store):
        _open(store, native_session_id=None)
        record = wake_receipts.get("term-1", "1202")
        assert record["schema"] == "cao-unmanaged-wake-receipt-v1"
        assert record["source"] == "status-transition"
        assert record["state"] == wake_receipts.WATCHING
        assert record["message_id"] == "1202"
        assert record["terminal_id"] == "term-1"
        # A v1 terminal exposes no native session: explicit null, never absent.
        assert record["native_session_id"] is None
        assert record["nudge_intent_at"] is None
        assert record["nudge_sent_at"] is None
        assert record["observed"] is None

    def test_a_v2_native_session_is_recorded_not_inferred(self, store):
        _open(store, native_session_id="kimi-sess-9")
        assert wake_receipts.get("term-1", "1202")["native_session_id"] == "kimi-sess-9"

    def test_an_absent_message_answers_none(self, store):
        assert wake_receipts.get("term-1", "never-sent") is None


class TestIdempotentOpen:
    def test_a_second_open_for_the_same_key_is_a_noop(self, store):
        first = _open(store, native_session_id="s-1")
        second = _open(store, native_session_id="s-different")
        # The first writer's facts stand; a re-arrival does not overwrite.
        assert second["native_session_id"] == "s-1"
        assert wake_receipts.get("term-1", "1202")["native_session_id"] == "s-1"
        assert first["delivered_at"] == second["delivered_at"]


class TestStateMachine:
    def test_a_transition_confirms_and_is_terminal(self, store):
        _open(store)
        wake_receipts.record_wake_confirmed(
            "term-1",
            "1202",
            observed={
                "event": "status-transition",
                "from_status": "idle",
                "to_status": "processing",
            },
        )
        record = wake_receipts.get("term-1", "1202")
        assert record["state"] == wake_receipts.WAKE_CONFIRMED
        assert record["observed"]["to_status"] == "processing"
        # A later unconfirmed cannot reopen a confirmed record.
        wake_receipts.record_wake_unconfirmed("term-1", "1202", note="late")
        assert wake_receipts.get("term-1", "1202")["state"] == wake_receipts.WAKE_CONFIRMED

    def test_a_confirmed_record_outranks_a_concurrent_unconfirmed(self, store):
        _open(store)
        wake_receipts.record_wake_confirmed("term-1", "1202", observed={"to_status": "processing"})
        wake_receipts.record_wake_unconfirmed("term-1", "1202", note="race")
        assert wake_receipts.get("term-1", "1202")["state"] == wake_receipts.WAKE_CONFIRMED

    def test_nudge_intent_and_sent_are_each_recorded_once(self, store):
        _open(store)
        wake_receipts.record_nudge_intent("term-1", "1202", at="t-1")
        wake_receipts.record_nudge_intent("term-1", "1202", at="t-2")  # idempotent
        wake_receipts.record_nudge_sent("term-1", "1202", at="t-3")
        wake_receipts.record_nudge_sent("term-1", "1202", at="t-4")  # idempotent
        record = wake_receipts.get("term-1", "1202")
        assert record["nudge_intent_at"] == "t-1"
        assert record["nudge_sent_at"] == "t-3"


class TestIterRecords:
    def test_iter_records_yields_every_stored_message(self, store):
        _open(store, terminal_id="term-1", message_id="m-1")
        _open(store, terminal_id="term-2", message_id="m-2")
        keys = {(t, m) for t, m, _ in wake_receipts.iter_records()}
        assert keys == {("term-1", "m-1"), ("term-2", "m-2")}

    def test_iter_records_skips_non_watching_for_finalized(self, store):
        _open(store, terminal_id="term-1", message_id="m-1")
        wake_receipts.record_wake_confirmed("term-1", "m-1", observed={"to_status": "processing"})
        # Only watching records are candidates for startup re-arm.
        watching = [
            (t, m)
            for t, m, r in wake_receipts.iter_records()
            if r["state"] == wake_receipts.WATCHING
        ]
        assert watching == []
