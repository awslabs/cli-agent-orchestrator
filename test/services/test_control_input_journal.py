"""Tests for the durable control-input request journal.

The journal exists to make one sentence true after a response is lost:
"ask by request id and you will be told what actually happened."  These
tests are organised around the ways that sentence can become false —
duplicate writes, a rebound request id, a dead owner, a comfortable
answer recorded without evidence — rather than around the API surface.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
import threading

import pytest

from cli_agent_orchestrator.services import control_input_journal as journal_module
from cli_agent_orchestrator.services.control_input_contract import (
    ACCEPTED,
    AMBIGUOUS,
    REASON_OWNER_LOST,
    REASON_PANE_BUSY,
    REASON_RESPONSE_LOST,
    REASON_WRITE_INCOMPLETE,
    REFUSED,
    control_input_request_sha256,
)
from cli_agent_orchestrator.services.control_input_journal import (
    DELIVERED,
    INTENT,
    LEGAL_TRANSITIONS,
    STATE_AMBIGUOUS,
    STATE_REFUSED,
    TERMINAL_STATES,
    WRITING,
    ControlInputBinding,
    ControlInputJournal,
    ControlInputJournalError,
    ControlInputNotFound,
    ControlInputRebound,
    ControlInputTransitionRefused,
    outcome_for_state,
)

REQ = "req-6f1b9c2d"
TERMINAL = "term-a1b2c3d4"
PANE = "%17"
WINDOW = "@3"
PANE_PID = 4242


def _sha(text="/model opus", pane_id=PANE, generation="gen-1"):
    return control_input_request_sha256(
        request_id=REQ,
        terminal_id=TERMINAL,
        pane_id=pane_id,
        window_id=WINDOW,
        pane_pid=PANE_PID,
        generation=generation,
        text=text,
        submit=True,
    )


def _binding(**overrides):
    fields = {
        "request_id": REQ,
        "terminal_id": TERMINAL,
        "pane_id": PANE,
        "window_id": WINDOW,
        "pane_pid": PANE_PID,
        "generation": "gen-1",
        "request_sha256": _sha(),
    }
    fields.update(overrides)
    return ControlInputBinding(**fields)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "recovery" / "control-input-journal.db"


@pytest.fixture
def journal(db_path):
    return ControlInputJournal(db_path)


def _dead_pid():
    """A pid that has certainly exited, for owner-loss tests."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    return child.pid


class TestStateMachineShape:
    def test_a_claimed_write_can_never_be_recorded_as_refused(self):
        """The absent edge is the contract: refused means zero bytes."""
        assert (WRITING, STATE_REFUSED) not in LEGAL_TRANSITIONS

    def test_transitions_are_exactly_the_documented_five(self):
        assert LEGAL_TRANSITIONS == {
            (None, INTENT),
            (INTENT, STATE_REFUSED),
            (INTENT, WRITING),
            (WRITING, DELIVERED),
            (WRITING, STATE_AMBIGUOUS),
        }

    def test_in_flight_states_license_no_outcome(self):
        """'In flight' is truthful; inventing an outcome for it is not."""
        assert outcome_for_state(INTENT) is None
        assert outcome_for_state(WRITING) is None

    def test_terminal_states_map_onto_wire_outcomes(self):
        assert outcome_for_state(DELIVERED) == ACCEPTED
        assert outcome_for_state(STATE_REFUSED) == REFUSED
        assert outcome_for_state(STATE_AMBIGUOUS) == AMBIGUOUS
        assert TERMINAL_STATES == {DELIVERED, STATE_REFUSED, STATE_AMBIGUOUS}


class TestStorage:
    def test_db_is_owner_only(self, journal, db_path):
        assert stat.S_IMODE(os.stat(db_path).st_mode) == 0o600

    def test_schema_version_is_recorded(self, journal, db_path):
        conn = sqlite3.connect(str(db_path))
        try:
            rows = dict(conn.execute("SELECT k, v FROM journal_meta"))
        finally:
            conn.close()
        assert rows["journal_schema_version"] == "1"
        assert rows["db_uuid"]

    def test_event_history_is_append_only(self, journal, db_path):
        journal.open_intent(_binding())
        conn = sqlite3.connect(str(db_path))
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE control_input_event SET to_state='delivered'")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM control_input_event")
        finally:
            conn.close()

    def test_concurrent_first_open_does_not_lose_a_journal(self, db_path):
        """Several processes may start at once; creation is idempotent."""
        handles = []
        errors = []
        start = threading.Barrier(8)

        def build():
            start.wait(timeout=30)
            try:
                handles.append(ControlInputJournal(db_path))
            except ControlInputJournalError as exc:  # pragma: no cover - failure path
                errors.append(exc)

        workers = [threading.Thread(target=build) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=60)
        assert errors == []
        assert len(handles) == 8


class TestIntentBeforeIO:
    def test_intent_is_durable_before_any_write(self, journal):
        record = journal.open_intent(_binding())
        assert record.state == INTENT
        assert record.outcome is None
        assert not record.is_terminal
        assert [(e["from_state"], e["to_state"]) for e in record.events] == [(None, INTENT)]

    def test_identical_re_arrival_is_idempotent(self, journal):
        """A client retrying a lost HTTP request must not open a second request."""
        journal.open_intent(_binding())
        record = journal.open_intent(_binding())
        assert record.state == INTENT
        assert len(record.events) == 1

    @pytest.mark.parametrize(
        "override",
        [
            {"request_sha256": _sha(text="/model haiku")},
            {"pane_id": "%18", "request_sha256": _sha(pane_id="%18")},
            {"terminal_id": "term-other"},
            {"window_id": "@9"},
            {"pane_pid": 9999},
            {"generation": "gen-2", "request_sha256": _sha(generation="gen-2")},
        ],
    )
    def test_a_rebound_request_id_is_refused(self, journal, override):
        """One id, one control, one target — a borrowed id is not a retry."""
        journal.open_intent(_binding())
        with pytest.raises(ControlInputRebound):
            journal.open_intent(_binding(**override))
        assert journal.get(REQ).state == INTENT
        assert len(journal.get(REQ).events) == 1

    def test_the_digest_binds_target_as_well_as_payload(self):
        """Same text, different pane, must not share a digest."""
        assert _sha(pane_id="%17") != _sha(pane_id="%18")
        assert _sha(text="/model opus") != _sha(text="/model haiku")
        assert _sha(generation="gen-1") != _sha(generation="gen-2")
        assert _sha() == _sha()

    @pytest.mark.parametrize(
        "override",
        [
            {"request_id": ""},
            {"pane_id": "42"},
            {"pane_id": "%4a"},
            {"window_id": "3"},
            {"pane_pid": 0},
            {"pane_pid": -1},
            {"request_sha256": ""},
        ],
    )
    def test_an_unusable_binding_is_refused_at_construction(self, override):
        with pytest.raises(ValueError):
            _binding(**override)


class TestAtMostOnce:
    def test_the_claim_is_granted_once(self, journal):
        journal.open_intent(_binding())
        first = journal.claim_write(REQ)
        second = journal.claim_write(REQ)
        assert first.granted
        assert not second.granted
        assert second.record.state == WRITING

    def test_concurrent_claims_produce_exactly_one_writer(self, db_path):
        """The property duplicate delivery would violate, under contention."""
        ControlInputJournal(db_path).open_intent(_binding())
        claimants = 16
        start = threading.Barrier(claimants)
        granted = []
        bookkeeping = threading.Lock()

        def claim():
            # A separate handle per thread: separate owner tokens, the
            # way separate processes would contend.
            handle = ControlInputJournal(db_path)
            start.wait(timeout=30)
            result = handle.claim_write(REQ)
            if result.granted:
                with bookkeeping:
                    granted.append(handle.owner_token)

        workers = [threading.Thread(target=claim) for _ in range(claimants)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=60)
            assert not worker.is_alive()

        assert len(granted) == 1
        transitions = [
            event
            for event in ControlInputJournal(db_path).event_log()
            if (event["from_state"], event["to_state"]) == (INTENT, WRITING)
        ]
        assert len(transitions) == 1

    def test_a_refused_request_can_never_be_claimed(self, journal):
        journal.open_intent(_binding())
        journal.mark_refused(REQ, reason_code=REASON_PANE_BUSY)
        assert not journal.claim_write(REQ).granted

    def test_a_delivered_request_can_never_be_claimed_again(self, journal):
        journal.open_intent(_binding())
        journal.claim_write(REQ)
        journal.mark_delivered(REQ, chunks_sent=1)
        claim = journal.claim_write(REQ)
        assert not claim.granted
        assert claim.record.outcome == ACCEPTED

    def test_claiming_an_unknown_request_is_an_error_not_a_grant(self, journal):
        with pytest.raises(ControlInputNotFound):
            journal.claim_write("req-never-opened")


class TestOutcomes:
    def test_delivered_is_accepted(self, journal):
        journal.open_intent(_binding())
        journal.claim_write(REQ)
        record = journal.mark_delivered(REQ, chunks_sent=2)
        assert record.state == DELIVERED
        assert record.outcome == ACCEPTED
        assert record.chunks_sent == 2
        assert record.is_terminal

    def test_refused_before_the_claim_is_refused(self, journal):
        journal.open_intent(_binding())
        record = journal.mark_refused(REQ, reason_code=REASON_PANE_BUSY)
        assert record.outcome == REFUSED
        assert record.reason_code == REASON_PANE_BUSY

    def test_a_claimed_write_cannot_be_downgraded_to_refused(self, journal):
        """The honesty property, enforced at runtime and not just in the docs."""
        journal.open_intent(_binding())
        journal.claim_write(REQ)
        with pytest.raises(ControlInputTransitionRefused):
            journal.mark_refused(REQ, reason_code=REASON_PANE_BUSY)
        assert journal.get(REQ).state == WRITING

    def test_a_partial_write_is_ambiguous_with_its_evidence(self, journal):
        journal.open_intent(_binding())
        journal.claim_write(REQ)
        record = journal.mark_ambiguous(
            REQ,
            reason_code=REASON_WRITE_INCOMPLETE,
            chunks_sent=1,
            enter_attempted=False,
        )
        assert record.outcome == AMBIGUOUS
        assert record.chunks_sent == 1
        assert record.enter_attempted is False

    def test_ambiguous_is_terminal_and_never_upgraded(self, journal):
        """A later observation cannot prove these bytes were this control's."""
        journal.open_intent(_binding())
        journal.claim_write(REQ)
        journal.mark_ambiguous(REQ, reason_code=REASON_RESPONSE_LOST)
        with pytest.raises(ControlInputTransitionRefused):
            journal.mark_delivered(REQ)
        assert journal.get(REQ).outcome == AMBIGUOUS

    def test_delivered_is_terminal(self, journal):
        journal.open_intent(_binding())
        journal.claim_write(REQ)
        journal.mark_delivered(REQ)
        with pytest.raises(ControlInputTransitionRefused):
            journal.mark_ambiguous(REQ, reason_code=REASON_RESPONSE_LOST)

    def test_recording_the_same_milestone_twice_records_it_once(self, journal):
        """A crash between the effect and the journal write is a re-arrival."""
        journal.open_intent(_binding())
        journal.claim_write(REQ)
        journal.mark_delivered(REQ, chunks_sent=3)
        record = journal.mark_delivered(REQ)
        assert [e["to_state"] for e in record.events] == [INTENT, WRITING, DELIVERED]
        # The re-arrival must not erase the evidence the first one carried.
        assert record.chunks_sent == 3

    def test_marking_an_unknown_request_is_refused(self, journal):
        with pytest.raises(ControlInputNotFound):
            journal.mark_delivered("req-never-opened")

    def test_the_full_history_is_readable(self, journal):
        journal.open_intent(_binding())
        journal.claim_write(REQ)
        record = journal.mark_delivered(REQ, chunks_sent=1)
        assert [(e["from_state"], e["to_state"]) for e in record.events] == [
            (None, INTENT),
            (INTENT, WRITING),
            (WRITING, DELIVERED),
        ]
        assert record.as_dict()["outcome"] == ACCEPTED


class TestResponseLoss:
    def test_a_lost_response_is_resolved_by_request_id(self, journal, db_path):
        """The whole point: ask, do not guess and re-send."""
        journal.open_intent(_binding())
        journal.claim_write(REQ)
        journal.mark_delivered(REQ, chunks_sent=1)

        # The client saw nothing and asks again; a fresh server incarnation
        # answers from the durable record.
        reader = ControlInputJournal(db_path)
        record = reader.get(REQ)
        assert record.outcome == ACCEPTED
        assert not reader.claim_write(REQ).granted

    def test_an_unknown_request_id_is_distinguishable_from_a_finished_one(self, journal):
        journal.open_intent(_binding())
        assert journal.find(REQ) is not None
        assert journal.find("req-never-opened") is None

    def test_a_client_retry_after_a_lost_response_reuses_the_record(self, journal, db_path):
        """Re-POSTing the identical request must not produce a second write."""
        journal.open_intent(_binding())
        journal.claim_write(REQ)
        journal.mark_delivered(REQ, chunks_sent=1)

        retry_server = ControlInputJournal(db_path)
        record = retry_server.open_intent(_binding())
        assert record.outcome == ACCEPTED
        assert not retry_server.claim_write(REQ).granted
        assert [e["to_state"] for e in retry_server.get(REQ).events] == [
            INTENT,
            WRITING,
            DELIVERED,
        ]


class TestCrashWindow:
    def test_a_crash_before_the_claim_survives_as_intent(self, db_path):
        """Nothing was written, and the record says so without guessing."""
        first = ControlInputJournal(db_path)
        first.open_intent(_binding())
        del first

        recovered = ControlInputJournal(db_path)
        record = recovered.get(REQ)
        assert record.state == INTENT
        assert record.outcome is None

    def test_a_crash_after_the_claim_survives_as_writing(self, db_path):
        """The claim commits before the first byte, so this is the evidence."""
        first = ControlInputJournal(db_path)
        first.open_intent(_binding())
        first.claim_write(REQ)
        del first

        recovered = ControlInputJournal(db_path)
        assert recovered.get(REQ).state == WRITING
        assert recovered.get(REQ).outcome is None

    def test_stranded_intent_resolves_to_refused(self, db_path):
        """A record that never reached 'writing' proves zero bytes."""
        dead = ControlInputJournal(db_path, owner_pid=999_001, owner_token="prev")
        dead.open_intent(_binding())

        sweeper = ControlInputJournal(db_path)
        resolved = sweeper.sweep_stranded(owner_alive=lambda pid: False)
        assert [r.request_id for r in resolved] == [REQ]
        record = sweeper.get(REQ)
        assert record.outcome == REFUSED
        assert record.reason_code == REASON_OWNER_LOST

    def test_stranded_write_resolves_to_ambiguous(self, db_path):
        """The owner had the right to write; nothing durable says it did not."""
        dead = ControlInputJournal(db_path, owner_pid=999_001, owner_token="prev")
        dead.open_intent(_binding())
        dead.claim_write(REQ)

        sweeper = ControlInputJournal(db_path)
        sweeper.sweep_stranded(owner_alive=lambda pid: False)
        record = sweeper.get(REQ)
        assert record.outcome == AMBIGUOUS
        assert record.reason_code == REASON_OWNER_LOST

    def test_a_genuinely_dead_owner_is_detected_without_injection(self, db_path):
        """The default liveness probe, not just the test double."""
        pid = _dead_pid()
        if journal_module._pid_is_alive(pid):  # pragma: no cover - pid reuse
            pytest.skip("pid was recycled before the assertion")
        dead = ControlInputJournal(db_path, owner_pid=pid, owner_token="prev")
        dead.open_intent(_binding())
        dead.claim_write(REQ)

        resolved = ControlInputJournal(db_path).sweep_stranded()
        assert [r.outcome for r in resolved] == [AMBIGUOUS]

    def test_a_live_owner_is_never_swept(self, db_path):
        """Resolving a live owner's record out from under it invents an answer."""
        live = ControlInputJournal(db_path, owner_pid=os.getpid(), owner_token="other-live")
        live.open_intent(_binding())
        live.claim_write(REQ)

        sweeper = ControlInputJournal(db_path)
        assert sweeper.sweep_stranded() == []
        assert sweeper.get(REQ).state == WRITING

    def test_a_journal_never_sweeps_its_own_in_flight_request(self, journal):
        journal.open_intent(_binding())
        journal.claim_write(REQ)
        assert journal.sweep_stranded(owner_alive=lambda pid: False) == []
        assert journal.get(REQ).state == WRITING

    def test_sweeping_twice_resolves_once(self, db_path):
        dead = ControlInputJournal(db_path, owner_pid=999_001, owner_token="prev")
        dead.open_intent(_binding())
        dead.claim_write(REQ)

        sweeper = ControlInputJournal(db_path)
        assert len(sweeper.sweep_stranded(owner_alive=lambda pid: False)) == 1
        assert sweeper.sweep_stranded(owner_alive=lambda pid: False) == []
        assert [e["to_state"] for e in sweeper.get(REQ).events] == [
            INTENT,
            WRITING,
            STATE_AMBIGUOUS,
        ]

    def test_a_terminal_record_is_not_re_resolved_by_a_sweep(self, db_path):
        dead = ControlInputJournal(db_path, owner_pid=999_001, owner_token="prev")
        dead.open_intent(_binding())
        dead.claim_write(REQ)
        dead.mark_delivered(REQ, chunks_sent=1)

        sweeper = ControlInputJournal(db_path)
        assert sweeper.sweep_stranded(owner_alive=lambda pid: False) == []
        assert sweeper.get(REQ).outcome == ACCEPTED

    def test_in_flight_lists_only_unresolved_requests(self, journal):
        journal.open_intent(_binding())
        assert [r.request_id for r in journal.in_flight()] == [REQ]
        journal.mark_refused(REQ, reason_code=REASON_PANE_BUSY)
        assert journal.in_flight() == []
