"""Tests for the delivery journal (T-CB-2 fork side, honest at-most-once)."""

from __future__ import annotations

import os
import stat
import threading

import pytest

from cli_agent_orchestrator.services.delivery_journal import (
    DeliveryJournal,
    DeliveryNotFound,
    DeliveryTransitionRefused,
)

OB = "obgen-7c2e4a1b"
CB = "0f8fad5a-1c87-4d3e-9b96-1b6b2c8e5f10"
REQ = "9fe77e88ae62dad23a73aacaf34784624f532cfa8f3d66762bd1bfdc3254c5f6"


@pytest.fixture
def journal(tmp_path):
    return DeliveryJournal(tmp_path / "recovery" / "delivery-journal.db")


def test_full_happy_path(journal):
    record = journal.open_intent(OB, CB, REQ)
    assert record["state"] == "accepted"
    journal.mark_terminal_queued(OB, CB)
    journal.mark_submitted(OB, CB, evidence_digest="e" * 64)
    journal.mark_submit_acked(OB, CB)
    final = journal.mark_consumer_acked(OB, CB, evidence_digest="f" * 64)
    assert final["state"] == "consumer-acked"
    states = [(e["from_state"], e["to_state"]) for e in final["events"]]
    assert states == [
        (None, "accepted"),
        ("accepted", "terminal_queued"),
        ("terminal_queued", "submitted"),
        ("submitted", "submit-acked"),
        ("submit-acked", "consumer-acked"),
    ]


def test_db_is_owner_only(journal, tmp_path):
    mode = stat.S_IMODE(os.stat(tmp_path / "recovery" / "delivery-journal.db").st_mode)
    assert mode == 0o600


def test_intent_persisted_before_io(journal):
    # open_intent before any provider milestone is the intent-before-I/O
    # guarantee; a crash after accept leaves a readable intent.
    journal.open_intent(OB, CB, REQ)
    assert journal.get(OB, CB)["state"] == "accepted"


def test_submit_ambiguous_is_terminal_no_blind_replay(journal):
    journal.open_intent(OB, CB, REQ)
    journal.mark_terminal_queued(OB, CB)
    journal.mark_submitted(OB, CB)
    record = journal.mark_submit_ambiguous(OB, CB, evidence_digest="d" * 64)
    assert record["state"] == "submit-ambiguous"
    assert journal.is_ambiguous_preserved(OB, CB)
    # No automated transition exists out of the ambiguous window —
    # resolution is consumer/human reconciliation, never a second submit.
    with pytest.raises(DeliveryTransitionRefused):
        journal.mark_submitted(OB, CB)
    with pytest.raises(DeliveryTransitionRefused):
        journal.mark_submit_acked(OB, CB)
    with pytest.raises(DeliveryTransitionRefused):
        journal.mark_consumer_acked(OB, CB)


def test_provider_accept_then_bridge_death_window(journal):
    # The T-CB-2 shape: submitted, then the receipt is lost — the honest
    # state is ambiguous-preserved, and retrying submission is refused.
    journal.open_intent(OB, CB, REQ)
    journal.mark_terminal_queued(OB, CB)
    journal.mark_submitted(OB, CB)
    journal.mark_submit_ambiguous(OB, CB)
    record = journal.get(OB, CB)
    assert record["state"] == "submit-ambiguous"
    assert [e["to_state"] for e in record["events"]][-1] == "submit-ambiguous"


def test_certain_pre_submit_refusal_can_retry_same_message(journal):
    journal.open_intent(OB, CB, REQ)
    journal.mark_terminal_queued(OB, CB)
    refused = journal.mark_submit_refused(OB, CB)
    assert refused["state"] == "submit-refused"

    queued = journal.mark_terminal_queued(OB, CB)
    assert queued["state"] == "terminal_queued"
    journal.mark_submitted(OB, CB)
    assert journal.mark_submit_acked(OB, CB)["state"] == "submit-acked"


def test_idempotent_rearrival_records_nothing_twice(journal):
    journal.open_intent(OB, CB, REQ)
    journal.mark_terminal_queued(OB, CB)
    again = journal.mark_terminal_queued(OB, CB)
    assert again["state"] == "terminal_queued"
    assert len(again["events"]) == 2  # open + one queued transition only


def test_illegal_and_regressive_transitions_refused(journal):
    journal.open_intent(OB, CB, REQ)
    with pytest.raises(DeliveryTransitionRefused):
        journal.mark_submitted(OB, CB)  # skips terminal_queued
    with pytest.raises(DeliveryTransitionRefused):
        journal.mark_consumer_acked(OB, CB)
    journal.mark_terminal_queued(OB, CB)
    with pytest.raises(DeliveryTransitionRefused):
        journal.open_intent(OB, CB, REQ)  # regression back to accepted


def test_same_logical_id_different_request_refused(journal):
    journal.open_intent(OB, CB, REQ)
    with pytest.raises(DeliveryTransitionRefused):
        journal.mark_terminal_queued(OB, CB)
        journal._transition(OB, CB, to_state="terminal_queued", request_sha256="0" * 64)


def test_unknown_record_lookup(journal):
    with pytest.raises(DeliveryNotFound):
        journal.get(OB, CB)
    assert not journal.is_ambiguous_preserved(OB, CB)


def test_crash_between_transitions_converges(tmp_path):
    # Each transition is one transaction; a kill between them leaves the
    # journal at the last committed milestone, and re-driving continues.
    journal = DeliveryJournal(tmp_path / "j.db")
    journal.open_intent(OB, CB, REQ)
    journal.mark_terminal_queued(OB, CB)
    # simulated kill: new handle, same file
    restarted = DeliveryJournal(tmp_path / "j.db")
    assert restarted.get(OB, CB)["state"] == "terminal_queued"
    restarted.mark_submitted(OB, CB)
    assert restarted.get(OB, CB)["state"] == "submitted"


def test_concurrent_open_intent_serializes(tmp_path):
    errors = []

    def open_it():
        try:
            DeliveryJournal(tmp_path / "c.db").open_intent(OB, CB, REQ)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=open_it) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    record = DeliveryJournal(tmp_path / "c.db").get(OB, CB)
    assert record["state"] == "accepted"
    assert len(record["events"]) == 1  # exactly one opening transition
