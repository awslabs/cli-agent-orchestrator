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
    REASON_OWNER_LOST_BEFORE_WRITE,
    REASON_OWNER_LOST_MID_WRITE,
    REASON_PANE_BUSY,
    REASON_RESPONSE_LOST,
    REASON_WRITE_INCOMPLETE,
    REFUSED,
    control_input_request_digest,
)
from cli_agent_orchestrator.services.control_input_journal import (
    CONTROL_INPUT_JOURNAL_SCHEMA_VERSION,
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


def _sha(text="/model opus", generation="gen-1"):
    """The digest over the wire request both sides can compute.

    The physical target — pane, window, pid — is deliberately absent: it
    is resolved and re-verified by the server, and a client cannot
    reproduce a digest over facts it has never been told.  The binding
    columns below carry it instead, which is why a pane change is still
    a rebind without touching this digest.
    """
    return control_input_request_digest(
        control_id=REQ,
        text=text,
        enter=True,
        expected_identity={"terminal_id": TERMINAL, "terminal_generation": generation},
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

    def test_transitions_are_exactly_the_documented_six(self):
        assert LEGAL_TRANSITIONS == {
            (None, INTENT),
            (INTENT, STATE_REFUSED),
            (INTENT, WRITING),
            (WRITING, DELIVERED),
            (WRITING, STATE_AMBIGUOUS),
            (STATE_REFUSED, INTENT),
        }

    def test_only_a_refusal_may_leave_a_terminal_state(self):
        """Re-arming is licensed by proof, not by convenience.

        A refused record proves zero bytes, so re-attempting it cannot
        duplicate a write.  Neither other terminal state can make that
        proof, so neither gets an edge out.
        """
        leaving = {edge for edge in LEGAL_TRANSITIONS if edge[0] in TERMINAL_STATES}
        assert leaving == {(STATE_REFUSED, INTENT)}

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
        assert rows["journal_schema_version"] == str(CONTROL_INPUT_JOURNAL_SCHEMA_VERSION)
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
            {"pane_id": "%18"},
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

    def test_the_digest_binds_payload_and_declared_identity(self):
        """Same id, different control or different terminal, different digest."""
        assert _sha(text="/model opus") != _sha(text="/model haiku")
        assert _sha(generation="gen-1") != _sha(generation="gen-2")
        assert _sha() == _sha()

    def test_the_physical_target_is_bound_without_being_digested(self, journal):
        """A moved pane is a rebind even though the digest never saw it.

        The client digests what it declared; the server owns the pane,
        window, and pid it resolved.  Enforcing the physical target
        through the binding columns rather than the preimage is what lets
        both sides compute the same digest without the client having to
        know tmux facts it was never told.
        """
        journal.open_intent(_binding())
        with pytest.raises(ControlInputRebound):
            journal.open_intent(_binding(pane_id="%18"))
        assert journal.get(REQ).pane_id == PANE

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


class TestReasonOutcomeBinding:
    """A reason may never be filed under an outcome it cannot support.

    The state machine already stops a claimed write from being recorded
    as refused.  That protects the fork's own call sites and nothing
    else: a reason code travels on the wire, and a caller that keys off
    the reason rather than the state can still be handed
    ``refused``/``response-lost`` by some other implementation.  Binding
    the two in the contract and checking the binding here closes the gap
    at the layer the wire actually shares.
    """

    @pytest.mark.parametrize("reason", [REASON_RESPONSE_LOST, REASON_WRITE_INCOMPLETE])
    def test_post_attempt_uncertainty_cannot_be_recorded_as_a_refusal(self, journal, reason):
        """Refused licenses a re-send; these reasons cannot prove it is safe."""
        journal.open_intent(_binding())
        with pytest.raises(ControlInputTransitionRefused) as excinfo:
            journal.mark_refused(REQ, reason_code=reason)
        assert "duplicate" in str(excinfo.value)
        assert journal.get(REQ).state == INTENT

    def test_owner_loss_mid_write_cannot_be_recorded_as_a_refusal(self, journal):
        journal.open_intent(_binding())
        with pytest.raises(ControlInputTransitionRefused):
            journal.mark_refused(REQ, reason_code=REASON_OWNER_LOST_MID_WRITE)

    def test_a_pre_write_reason_cannot_be_recorded_as_ambiguous(self, journal):
        """Understating what is known strands a request that never ran."""
        journal.open_intent(_binding())
        journal.claim_write(REQ)
        with pytest.raises(ControlInputTransitionRefused):
            journal.mark_ambiguous(REQ, reason_code=REASON_PANE_BUSY)

    def test_an_unknown_reason_is_refused_rather_than_stored(self, journal):
        """A free-text reason is a vocabulary the other side cannot read."""
        journal.open_intent(_binding())
        with pytest.raises(ValueError):
            journal.mark_refused(REQ, reason_code="something-went-wrong")
        assert journal.get(REQ).state == INTENT

    def test_the_bound_reasons_still_record_normally(self, journal):
        journal.open_intent(_binding())
        journal.claim_write(REQ)
        record = journal.mark_ambiguous(REQ, reason_code=REASON_RESPONSE_LOST)
        assert record.outcome == AMBIGUOUS
        assert record.reason_code == REASON_RESPONSE_LOST


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


class TestRefusalIsReattemptable:
    """A refusal that could not be acted on would not be a refusal.

    ``refused`` is the one outcome that licenses sending the control
    again.  If the record then answered every re-arrival with the stored
    refusal, that licence would be void and a transient cause — a busy
    pane above all — would be permanent for that control id.
    """

    def test_a_refused_record_is_rearmed_by_an_identical_re_arrival(self, journal):
        journal.open_intent(_binding())
        journal.mark_refused(REQ, reason_code=REASON_PANE_BUSY)
        assert journal.get(REQ).outcome == REFUSED

        record = journal.open_intent(_binding())
        assert record.state == INTENT
        assert record.outcome is None
        # The stale refusal is cleared from the live row: it describes an
        # attempt that is over, and a re-armed request that still carried
        # it would look like it had already failed.
        assert record.reason_code is None
        assert journal.claim_write(REQ).granted

    def test_re_arming_preserves_the_refusal_in_the_event_log(self, journal):
        """The append-only log is what keeps re-arming honest."""
        journal.open_intent(_binding())
        journal.mark_refused(REQ, reason_code=REASON_PANE_BUSY)
        journal.open_intent(_binding())

        events = journal.get(REQ).events
        assert [e["to_state"] for e in events] == [INTENT, STATE_REFUSED, INTENT]
        assert events[1]["reason_code"] == REASON_PANE_BUSY

    def test_a_delivered_record_is_never_rearmed(self, journal):
        """Delivered cannot prove zero bytes, so it stays terminal."""
        journal.open_intent(_binding())
        journal.claim_write(REQ)
        journal.mark_delivered(REQ, chunks_sent=1, enter_attempted=True)

        record = journal.open_intent(_binding())
        assert record.state == DELIVERED
        assert not journal.claim_write(REQ).granted

    def test_an_ambiguous_record_is_never_rearmed(self, journal):
        journal.open_intent(_binding())
        journal.claim_write(REQ)
        journal.mark_ambiguous(REQ, reason_code=REASON_WRITE_INCOMPLETE, chunks_sent=1)

        record = journal.open_intent(_binding())
        assert record.state == STATE_AMBIGUOUS
        assert not journal.claim_write(REQ).granted

    def test_re_arming_requires_a_byte_identical_binding(self, journal):
        """Re-arming re-attempts one control; it does not free the id."""
        journal.open_intent(_binding())
        journal.mark_refused(REQ, reason_code=REASON_PANE_BUSY)

        with pytest.raises(ControlInputRebound):
            journal.open_intent(_binding(request_sha256=_sha(text="/clear")))
        with pytest.raises(ControlInputRebound):
            journal.open_intent(_binding(pane_id="%99"))
        assert journal.get(REQ).state == STATE_REFUSED

    def test_a_rearmed_record_is_owned_by_the_reattempting_server(self, journal, db_path):
        """Otherwise a sweep would treat the live re-attempt as stranded."""
        journal.open_intent(_binding())
        journal.mark_refused(REQ, reason_code=REASON_PANE_BUSY)

        retry_server = ControlInputJournal(db_path)
        retry_server.open_intent(_binding())
        assert retry_server.get(REQ).owner_token == retry_server.owner_token
        assert retry_server.sweep_stranded(owner_alive=lambda pid: False) == []


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
        assert record.reason_code == REASON_OWNER_LOST_BEFORE_WRITE

    def test_stranded_write_resolves_to_ambiguous(self, db_path):
        """The owner had the right to write; nothing durable says it did not."""
        dead = ControlInputJournal(db_path, owner_pid=999_001, owner_token="prev")
        dead.open_intent(_binding())
        dead.claim_write(REQ)

        sweeper = ControlInputJournal(db_path)
        sweeper.sweep_stranded(owner_alive=lambda pid: False)
        record = sweeper.get(REQ)
        assert record.outcome == AMBIGUOUS
        assert record.reason_code == REASON_OWNER_LOST_MID_WRITE

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


SOCKET = "/private/tmp/tmux-501/cao.sock"
OTHER_SOCKET = "/private/tmp/tmux-501/somebody-elses.sock"


def _v1_row(db_path, state, *, chunks_sent=None):
    """A journal written before ``server_socket_path`` existed.

    Built at the real v1 shape — the column is genuinely absent, not
    present-and-null — so opening it exercises the additive migration
    rather than a hand-made imitation of its result.  Only then does the
    row look like what an upgraded operator actually has: a request that
    was bound, and possibly already delivered, by a binary that had no
    concept of which tmux server it was talking to.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
            CREATE TABLE control_input_request (
              request_id TEXT PRIMARY KEY, terminal_id TEXT NOT NULL,
              pane_id TEXT NOT NULL, window_id TEXT NOT NULL,
              pane_pid INTEGER NOT NULL, generation TEXT,
              request_sha256 TEXT NOT NULL, state TEXT NOT NULL,
              reason_code TEXT, chunks_sent INTEGER, enter_attempted INTEGER,
              owner_pid INTEGER NOT NULL, owner_token TEXT NOT NULL,
              opened_at TEXT NOT NULL, updated_at TEXT NOT NULL
            ) WITHOUT ROWID;
            """)
        conn.execute(
            "INSERT INTO control_input_request VALUES " "(?,?,?,?,?,?,?,?,NULL,?,NULL,?,?,?,?)",
            (
                REQ,
                TERMINAL,
                PANE,
                WINDOW,
                PANE_PID,
                "gen-1",
                _sha(),
                state,
                chunks_sent,
                os.getpid(),
                "v1-owner",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    assert "server_socket_path" not in _columns(db_path), "the v1 fixture is not v1"
    return ControlInputJournal(db_path)


def _columns(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(control_input_request)")}
    finally:
        conn.close()


def _stored_socket(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT server_socket_path FROM control_input_request WHERE request_id=?",
            (REQ,),
        ).fetchone()[0]
    finally:
        conn.close()


class TestMigratedRowsAreNotRebindings:
    """A row from before the socket existed must still be the same request.

    The upgrade is the dangerous moment.  Every stored row suddenly has a
    null in a column that now participates in identity, while the clients
    re-arriving against those rows have started stating a real value.  If
    that difference reads as "a different control wearing this id", the
    journal stops recognising its own delivered requests — and the one
    thing it exists to prevent, a second write for a control the operator
    authorised once, becomes reachable through the front door.
    """

    def test_a_delivered_v1_row_replays_instead_of_rebinding(self, db_path):
        """The reported P1, at the state where it costs the most.

        The request was really delivered.  A retry after a lost response
        must be told exactly that, because the alternative — a rebound
        error the caller reads as "this id is free, send it again" — is a
        duplicate write of a control that already landed.
        """
        book = _v1_row(db_path, DELIVERED, chunks_sent=1)
        record = book.open_intent(_binding(server_socket_path=SOCKET))

        assert record.state == DELIVERED
        assert record.outcome == ACCEPTED
        assert record.chunks_sent == 1
        # Nothing was re-armed: a delivered row has no path back to an
        # attemptable state, so the retry cannot become a second write.
        assert book.get(REQ).state == DELIVERED

    def test_a_delivered_v1_row_does_not_adopt_the_socket_it_never_used(self, db_path):
        """The write already happened, against a server nobody recorded.

        Stamping the socket the *retry* names would convert "unknown" into
        a stored fact, and every later comparison would trust it as though
        the original delivery had been verified against it.
        """
        book = _v1_row(db_path, DELIVERED, chunks_sent=1)
        book.open_intent(_binding(server_socket_path=SOCKET))
        assert _stored_socket(db_path) is None

    def test_an_ambiguous_v1_row_neither_rebinds_nor_adopts(self, db_path):
        """Ambiguous means a write may have reached the pane."""
        book = _v1_row(db_path, STATE_AMBIGUOUS)
        record = book.open_intent(_binding(server_socket_path=SOCKET))
        assert record.state == STATE_AMBIGUOUS
        assert record.outcome == AMBIGUOUS
        assert _stored_socket(db_path) is None

    def test_a_pre_write_v1_row_adopts_the_socket_it_is_about_to_use(self, db_path):
        """Before any byte, the socket describes the write to come.

        Recording it here is what stops the row being permanently
        unqualified — after this, the exact comparison below applies.
        """
        book = _v1_row(db_path, INTENT)
        assert book.open_intent(_binding(server_socket_path=SOCKET)).state == INTENT
        assert _stored_socket(db_path) == SOCKET

        with pytest.raises(ControlInputRebound, match="different pane"):
            book.open_intent(_binding(server_socket_path=OTHER_SOCKET))

    def test_a_refused_v1_row_is_still_re_armed_and_adopts(self, db_path):
        """Refused promises a re-attempt; the promise survives the upgrade."""
        book = _v1_row(db_path, STATE_REFUSED)
        assert book.open_intent(_binding(server_socket_path=SOCKET)).state == INTENT
        assert _stored_socket(db_path) == SOCKET

    def test_the_relaxation_does_not_swallow_a_real_rebinding(self, db_path):
        """Only the socket is forgiven, and only when it was never stored."""
        book = _v1_row(db_path, DELIVERED, chunks_sent=1)
        for override in (
            {"pane_id": "%99"},
            {"window_id": "@9"},
            {"pane_pid": 5151},
            {"generation": "gen-2"},
            {"request_sha256": _sha(text="/clear")},
        ):
            with pytest.raises(ControlInputRebound):
                book.open_intent(_binding(server_socket_path=SOCKET, **override))


class TestAStoredSocketStillBindsExactly:
    """The column earns its place only if a present value is enforced."""

    def test_the_same_pane_id_on_another_server_is_a_different_pane(self, journal):
        journal.open_intent(_binding(server_socket_path=SOCKET))
        with pytest.raises(ControlInputRebound, match="different pane"):
            journal.open_intent(_binding(server_socket_path=OTHER_SOCKET))

    def test_a_re_arrival_that_states_no_server_cannot_downgrade_the_binding(self, journal):
        """Silence is not a match for a recorded server.

        Accepting it would make "stop stating the socket" a way to reach
        any pane sharing the id, which is the rebinding the column exists
        to catch, reached by omission instead of by collision.
        """
        journal.open_intent(_binding(server_socket_path=SOCKET))
        with pytest.raises(ControlInputRebound, match="different pane"):
            journal.open_intent(_binding())

    def test_an_identical_re_arrival_on_the_same_server_is_still_a_retry(self, journal):
        first = journal.open_intent(_binding(server_socket_path=SOCKET))
        again = journal.open_intent(_binding(server_socket_path=SOCKET))
        assert (first.state, again.state) == (INTENT, INTENT)
        assert [e["to_state"] for e in journal.get(REQ).events] == [INTENT]


# --- Schema v4: the provider-visible submission observation ------------------

V4_COLUMNS = ("submission_observed", "submission_evidence_ref")


def _v3_row(db_path, state, *, chunks_sent=None, enter_attempted=None):
    """A journal written before the submission observation existed.

    Built at the real v3 shape — chord columns present, both v4 columns
    genuinely absent, and ``journal_meta`` stamped ``3`` — so opening it
    exercises the additive v4 migration and its pre-migration snapshot
    rather than a hand-made imitation of their result.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
            CREATE TABLE journal_meta (
              k TEXT PRIMARY KEY, v TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE control_input_request (
              request_id TEXT PRIMARY KEY, terminal_id TEXT NOT NULL,
              pane_id TEXT NOT NULL, window_id TEXT NOT NULL,
              pane_pid INTEGER NOT NULL, server_socket_path TEXT,
              generation TEXT, request_sha256 TEXT NOT NULL,
              state TEXT NOT NULL, reason_code TEXT,
              chunks_sent INTEGER, enter_attempted INTEGER,
              chord TEXT, chord_attempted INTEGER, chord_sent INTEGER,
              owner_pid INTEGER NOT NULL, owner_token TEXT NOT NULL,
              opened_at TEXT NOT NULL, updated_at TEXT NOT NULL
            ) WITHOUT ROWID;
            INSERT INTO journal_meta(k,v) VALUES ('journal_schema_version', '3');
            """)
        conn.execute(
            "INSERT INTO control_input_request VALUES "
            "(?,?,?,?,?,?,?,?,?,NULL,?,?,NULL,NULL,NULL,?,?,?,?)",
            (
                REQ,
                TERMINAL,
                PANE,
                WINDOW,
                PANE_PID,
                SOCKET,
                "gen-1",
                _sha(),
                state,
                chunks_sent,
                enter_attempted,
                os.getpid(),
                "v3-owner",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    assert not (set(V4_COLUMNS) & _columns(db_path)), "the v3 fixture is not v3"
    return ControlInputJournal(db_path)


def _snapshot_path(db_path):
    return db_path.with_name(db_path.name + journal_module.V4_MIGRATION_SNAPSHOT_SUFFIX)


def _meta_value(db_path, key):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT v FROM journal_meta WHERE k=?", (key,)).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


class TestV4Migration:
    """The v3 -> v4 upgrade: additive columns, snapshot first, idempotent."""

    def test_a_fresh_journal_is_born_v4_and_takes_no_snapshot(self, db_path):
        ControlInputJournal(db_path)
        assert set(V4_COLUMNS) <= _columns(db_path)
        assert _meta_value(db_path, "journal_schema_version") == str(
            CONTROL_INPUT_JOURNAL_SCHEMA_VERSION
        )
        # Nothing migrated, so nothing was preserved: a snapshot of a
        # journal that never had a pre-v4 shape would be evidence of a
        # migration that never happened.
        assert not _snapshot_path(db_path).exists()

    def test_a_v3_journal_gains_the_columns_and_is_restamped(self, db_path):
        _v3_row(db_path, DELIVERED, chunks_sent=1, enter_attempted=1)
        assert set(V4_COLUMNS) <= _columns(db_path)
        assert _meta_value(db_path, "journal_schema_version") == str(
            CONTROL_INPUT_JOURNAL_SCHEMA_VERSION
        )

    def test_the_migration_snapshots_the_pre_v4_journal_before_altering(self, db_path):
        _v3_row(db_path, DELIVERED, chunks_sent=1, enter_attempted=1)
        snapshot = _snapshot_path(db_path)
        assert snapshot.exists()
        # The snapshot is the journal as it was *before* the ALTERs: v3
        # shape, v3 stamp, the sealed row intact.  Anything newer in it
        # would make it useless as pre-migration evidence.
        assert not (set(V4_COLUMNS) & _columns(snapshot))
        assert _meta_value(snapshot, "journal_schema_version") == "3"
        conn = sqlite3.connect(snapshot)
        try:
            row = conn.execute(
                "SELECT state, chunks_sent, enter_attempted FROM control_input_request "
                "WHERE request_id=?",
                (REQ,),
            ).fetchone()
        finally:
            conn.close()
        assert row == (DELIVERED, 1, 1)

    def test_an_existing_snapshot_is_never_overwritten(self, db_path):
        """The first copy is the evidence; a later open must not replace it."""
        db_path.parent.mkdir(parents=True, exist_ok=True)
        sentinel = _snapshot_path(db_path)
        sentinel.write_bytes(b"operator-preserved pre-v4 snapshot")
        journal = _v3_row(db_path, DELIVERED)
        assert journal.get(REQ).state == DELIVERED
        assert sentinel.read_bytes() == b"operator-preserved pre-v4 snapshot"

    def test_reopening_a_migrated_journal_takes_no_second_snapshot(self, db_path):
        _v3_row(db_path, DELIVERED)
        snapshot = _snapshot_path(db_path)
        before = snapshot.stat().st_mtime_ns
        # Second and later opens see the v4 columns already present, so
        # the migration path — and with it the snapshot — is not re-run.
        ControlInputJournal(db_path)
        ControlInputJournal(db_path)
        assert snapshot.stat().st_mtime_ns == before

    def test_a_delivered_pre_v4_row_replays_with_a_typed_null_observation(self, db_path):
        """A sealed pre-v4 row never grows an observation it never recorded.

        NULL is the honest projection — "observation not recorded" — and
        is distinct from ``unknown``, which is a recorded observation that
        could not be classified.  Backfilling either way would fabricate
        evidence about a write nobody watched.
        """
        book = _v3_row(db_path, DELIVERED, chunks_sent=1, enter_attempted=1)
        record = book.open_intent(_binding(server_socket_path=SOCKET))
        assert record.state == DELIVERED
        assert record.outcome == ACCEPTED
        assert record.submission_observed is None
        assert record.submission_evidence_ref is None
        assert record.as_dict()["submission_observed"] is None
        assert [e["to_state"] for e in record.events] == []

    def test_an_ambiguous_pre_v4_row_replays_typed_null_and_never_re_arms(self, db_path):
        book = _v3_row(db_path, STATE_AMBIGUOUS)
        record = book.open_intent(_binding(server_socket_path=SOCKET))
        assert record.state == STATE_AMBIGUOUS
        assert record.outcome == AMBIGUOUS
        assert record.submission_observed is None
        # An ambiguous record is terminal: the identical retry changed
        # nothing, so a second Enter can never be smuggled in as a replay.
        assert book.get(REQ).state == STATE_AMBIGUOUS
        assert [e["to_state"] for e in book.get(REQ).events] == []


class TestSubmissionObservationStorage:
    """The v4 columns store and replay the observation verbatim."""

    def test_a_delivered_record_stores_the_observation_and_its_evidence(self, journal):
        journal.open_intent(_binding())
        journal.claim_write(REQ)
        record = journal.mark_delivered(
            REQ,
            chunks_sent=1,
            enter_attempted=True,
            submission_observed="submitted",
            submission_evidence_ref="capture:%17:2026-07-27T00:00:01Z:abc123",
        )
        assert record.submission_observed == "submitted"
        assert record.submission_evidence_ref == "capture:%17:2026-07-27T00:00:01Z:abc123"
        assert journal.get(REQ).submission_observed == "submitted"

    def test_an_ambiguous_record_stores_an_unsubmitted_observation(self, journal):
        journal.open_intent(_binding())
        journal.claim_write(REQ)
        record = journal.mark_ambiguous(
            REQ,
            reason_code="submission-unproven",
            chunks_sent=1,
            enter_attempted=True,
            submission_observed="unsubmitted",
            submission_evidence_ref="capture:%17:2026-07-27T00:00:02Z:def456",
        )
        assert record.outcome == AMBIGUOUS
        assert record.submission_observed == "unsubmitted"

    def test_a_value_outside_the_closed_vocabulary_is_refused(self, journal):
        journal.open_intent(_binding())
        journal.claim_write(REQ)
        with pytest.raises(ControlInputJournalError, match="unknown submission observation"):
            journal.mark_delivered(REQ, chunks_sent=1, submission_observed="completed")

    def test_the_observation_survives_a_terminal_replay_verbatim(self, journal):
        """A6a: a same-id retry returns the stored observation, with no new event."""
        journal.open_intent(_binding())
        journal.claim_write(REQ)
        journal.mark_delivered(
            REQ,
            chunks_sent=1,
            enter_attempted=True,
            submission_observed="submitted",
            submission_evidence_ref="capture:%17:2026-07-27T00:00:03Z:789",
        )
        replayed = journal.open_intent(_binding())
        assert replayed.state == DELIVERED
        assert replayed.submission_observed == "submitted"
        assert replayed.submission_evidence_ref == "capture:%17:2026-07-27T00:00:03Z:789"
        # Terminal replay appends nothing: the event log is exactly the
        # first attempt's history.
        assert [e["to_state"] for e in replayed.events] == [INTENT, WRITING, DELIVERED]

    def test_a_refused_record_rearms_and_then_replays_its_new_observation(self, journal):
        """A6b: refused -> intent reattempt, then terminal replay of the new state."""
        journal.open_intent(_binding())
        journal.mark_refused(REQ, reason_code=REASON_PANE_BUSY)
        rearmed = journal.open_intent(_binding())
        assert rearmed.state == INTENT
        assert rearmed.submission_observed is None
        journal.claim_write(REQ)
        journal.mark_delivered(
            REQ,
            chunks_sent=1,
            enter_attempted=True,
            submission_observed="submitted",
            submission_evidence_ref="capture:%17:2026-07-27T00:00:04Z:aaa",
        )
        replayed = journal.open_intent(_binding())
        assert replayed.state == DELIVERED
        assert replayed.submission_observed == "submitted"
        assert [e["to_state"] for e in replayed.events] == [
            INTENT,
            STATE_REFUSED,
            INTENT,
            WRITING,
            DELIVERED,
        ]

    def test_a_barrier_refusal_carries_no_observation(self, journal):
        """A refusal is decided before any byte; there is nothing to observe."""
        journal.open_intent(_binding())
        record = journal.mark_refused(REQ, reason_code=REASON_PANE_BUSY)
        assert record.submission_observed is None
        assert record.submission_evidence_ref is None


# --- Schema v5: structured sequence events and per-event outcomes -------------

SEQUENCE = [
    {"type": "text", "text": "make it so, + \\"},
    {"type": "key", "key": "Enter"},
    {"type": "key", "key": "Escape"},
    {"type": "chord", "chord": "C-s"},
]


def _tables(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _v5_snapshot_path(db_path):
    return db_path.with_name(db_path.name + journal_module.V5_MIGRATION_SNAPSHOT_SUFFIX)


def _v4_row(db_path, state, *, chunks_sent=None, enter_attempted=None):
    """A journal written at the v4 shape — observation columns present, the
    sequence table genuinely absent, and ``journal_meta`` stamped ``4`` —
    so opening it exercises the v5 migration and its snapshot for real.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
            CREATE TABLE journal_meta (
              k TEXT PRIMARY KEY, v TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE control_input_request (
              request_id TEXT PRIMARY KEY, terminal_id TEXT NOT NULL,
              pane_id TEXT NOT NULL, window_id TEXT NOT NULL,
              pane_pid INTEGER NOT NULL, server_socket_path TEXT,
              generation TEXT, request_sha256 TEXT NOT NULL,
              state TEXT NOT NULL, reason_code TEXT,
              chunks_sent INTEGER, enter_attempted INTEGER,
              chord TEXT, chord_attempted INTEGER, chord_sent INTEGER,
              submission_observed TEXT, submission_evidence_ref TEXT,
              owner_pid INTEGER NOT NULL, owner_token TEXT NOT NULL,
              opened_at TEXT NOT NULL, updated_at TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE control_input_event (
              event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
              request_id TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL,
              reason_code TEXT, evidence_digest TEXT, at TEXT NOT NULL
            );
            INSERT INTO journal_meta(k,v) VALUES ('journal_schema_version', '4');
            """)
        conn.execute(
            "INSERT INTO control_input_request VALUES "
            "(?,?,?,?,?,?,?,?,?,NULL,?,?,NULL,NULL,NULL,NULL,NULL,?,?,?,?)",
            (
                REQ,
                TERMINAL,
                PANE,
                WINDOW,
                PANE_PID,
                SOCKET,
                "gen-1",
                _sha(),
                state,
                chunks_sent,
                enter_attempted,
                os.getpid(),
                "v4-owner",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    assert "control_input_sequence_event" not in _tables(db_path), "the v4 fixture is not v4"
    return ControlInputJournal(db_path)


class TestV5Migration:
    """The v4 -> v5 upgrade: a new structured table, snapshot first, idempotent."""

    def test_a_fresh_journal_is_born_v5_and_takes_no_snapshot(self, db_path):
        ControlInputJournal(db_path)
        assert "control_input_sequence_event" in _tables(db_path)
        assert _meta_value(db_path, "journal_schema_version") == "5"
        assert not _v5_snapshot_path(db_path).exists()
        assert not _snapshot_path(db_path).exists()

    def test_a_v4_journal_gains_the_table_and_is_restamped(self, db_path):
        _v4_row(db_path, DELIVERED, chunks_sent=1, enter_attempted=1)
        assert "control_input_sequence_event" in _tables(db_path)
        assert _meta_value(db_path, "journal_schema_version") == "5"

    def test_the_migration_snapshots_the_pre_v5_journal_before_creating(self, db_path):
        _v4_row(db_path, DELIVERED, chunks_sent=1, enter_attempted=1)
        snapshot = _v5_snapshot_path(db_path)
        assert snapshot.exists()
        # The snapshot is the journal as it was before the table existed:
        # v4 shape, v4 stamp, the sealed row intact.
        assert "control_input_sequence_event" not in _tables(snapshot)
        assert _meta_value(snapshot, "journal_schema_version") == "4"
        conn = sqlite3.connect(snapshot)
        try:
            row = conn.execute(
                "SELECT state, chunks_sent, enter_attempted FROM control_input_request "
                "WHERE request_id=?",
                (REQ,),
            ).fetchone()
        finally:
            conn.close()
        assert row == (DELIVERED, 1, 1)

    def test_an_existing_v5_snapshot_is_never_overwritten(self, db_path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        sentinel = _v5_snapshot_path(db_path)
        sentinel.write_bytes(b"operator-preserved pre-v5 snapshot")
        journal = _v4_row(db_path, DELIVERED)
        assert journal.get(REQ).state == DELIVERED
        assert sentinel.read_bytes() == b"operator-preserved pre-v5 snapshot"

    def test_reopening_a_migrated_journal_takes_no_second_v5_snapshot(self, db_path):
        _v4_row(db_path, DELIVERED)
        snapshot = _v5_snapshot_path(db_path)
        before = snapshot.stat().st_mtime_ns
        ControlInputJournal(db_path)
        ControlInputJournal(db_path)
        assert snapshot.stat().st_mtime_ns == before

    def test_a_v3_journal_migrates_through_both_steps(self, db_path):
        _v3_row(db_path, DELIVERED, chunks_sent=1, enter_attempted=1)
        assert set(V4_COLUMNS) <= _columns(db_path)
        assert "control_input_sequence_event" in _tables(db_path)
        assert _meta_value(db_path, "journal_schema_version") == "5"
        assert _snapshot_path(db_path).exists()
        assert _v5_snapshot_path(db_path).exists()

    def test_a_pre_v5_row_carries_no_events(self, db_path):
        """Sealed v1/v2 requests have no events, and none are invented."""
        book = _v4_row(db_path, DELIVERED, chunks_sent=1, enter_attempted=1)
        record = book.open_intent(_binding(server_socket_path=SOCKET))
        assert record.state == DELIVERED
        assert record.sequence_events is None
        assert record.as_dict()["sequence_events"] is None


class TestSequenceEventStorage:
    """One sequence is one request row; its events are stored structured."""

    def test_intent_stores_the_ordered_events_as_typed_rows(self, journal):
        record = journal.open_intent(_binding(), sequence_events=SEQUENCE)
        assert record.sequence_events is not None
        assert [(e["ordinal"], e["type"], e["outcome"]) for e in record.sequence_events] == [
            (0, "text", None),
            (1, "key", None),
            (2, "key", None),
            (3, "chord", None),
        ]
        # Structured columns, never an escaped string: the payloads are
        # queryable per event, byte for byte as requested.
        assert record.sequence_events[0]["text"] == "make it so, + \\"
        assert record.sequence_events[1]["key"] == "Enter"
        assert record.sequence_events[2]["key"] == "Escape"
        assert record.sequence_events[3]["chord"] == "C-s"
        conn = sqlite3.connect(journal._path)
        try:
            rows = conn.execute(
                "SELECT ordinal, type, text, key, chord, outcome "
                "FROM control_input_sequence_event WHERE request_id=? ORDER BY ordinal",
                (REQ,),
            ).fetchall()
        finally:
            conn.close()
        assert rows == [
            (0, "text", "make it so, + \\", None, None, None),
            (1, "key", None, "Enter", None, None),
            (2, "key", None, "Escape", None, None),
            (3, "chord", None, None, "C-s", None),
        ]

    def test_an_identical_re_arrival_never_duplicates_the_events(self, journal):
        journal.open_intent(_binding(), sequence_events=SEQUENCE)
        again = journal.open_intent(_binding(), sequence_events=SEQUENCE)
        assert again.sequence_events is not None
        assert len(again.sequence_events) == len(SEQUENCE)
        assert [e["to_state"] for e in again.events] == [INTENT]

    def test_delivery_records_every_event_sent_and_replays_verbatim(self, journal):
        journal.open_intent(_binding(), sequence_events=SEQUENCE)
        journal.claim_write(REQ)
        outcomes = [(index, "sent") for index in range(len(SEQUENCE))]
        record = journal.mark_delivered(
            REQ, chunks_sent=1, enter_attempted=True, sequence_event_outcomes=outcomes
        )
        assert [e["outcome"] for e in record.sequence_events] == ["sent"] * 4
        # The terminal stored-row replay: a fresh read returns the stored
        # per-event results exactly, with nothing recomputed.
        replayed = journal.get(REQ)
        assert [dict(e) for e in replayed.sequence_events] == [
            dict(e) for e in record.sequence_events
        ]
        assert replayed.as_dict()["sequence_events"][1] == {
            "ordinal": 1,
            "type": "key",
            "key": "Enter",
            "outcome": "sent",
        }

    def test_ambiguity_records_the_event_boundary(self, journal):
        journal.open_intent(_binding(), sequence_events=SEQUENCE)
        journal.claim_write(REQ)
        record = journal.mark_ambiguous(
            REQ,
            reason_code=REASON_WRITE_INCOMPLETE,
            chunks_sent=1,
            sequence_event_outcomes=[(0, "sent"), (1, "attempted"), (2, "skipped"), (3, "skipped")],
        )
        assert [e["outcome"] for e in record.sequence_events] == [
            "sent",
            "attempted",
            "skipped",
            "skipped",
        ]
        # An ambiguous record never gains new outcomes on a re-arrival.
        again = journal.open_intent(_binding(), sequence_events=SEQUENCE)
        assert [e["outcome"] for e in again.sequence_events] == [
            "sent",
            "attempted",
            "skipped",
            "skipped",
        ]

    def test_refusal_marks_every_event_refused_and_rearm_clears_them(self, journal):
        journal.open_intent(_binding(), sequence_events=SEQUENCE)
        record = journal.mark_refused(
            REQ,
            reason_code=REASON_PANE_BUSY,
            sequence_event_outcomes=[(index, "refused") for index in range(len(SEQUENCE))],
        )
        assert [e["outcome"] for e in record.sequence_events] == ["refused"] * 4
        # The refused -> intent re-arm clears the old attempt's evidence:
        # NULL is "no outcome recorded yet", not a carried-forward refusal.
        rearmed = journal.open_intent(_binding(), sequence_events=SEQUENCE)
        assert rearmed.state == INTENT
        assert [e["outcome"] for e in rearmed.sequence_events] == [None] * 4
        # The re-attempt then delivers and replays its new outcomes.
        journal.claim_write(REQ)
        delivered = journal.mark_delivered(
            REQ,
            chunks_sent=1,
            enter_attempted=True,
            sequence_event_outcomes=[(index, "sent") for index in range(len(SEQUENCE))],
        )
        assert [e["outcome"] for e in delivered.sequence_events] == ["sent"] * 4

    def test_an_unknown_event_outcome_is_refused_with_zero_mutation(self, journal):
        journal.open_intent(_binding(), sequence_events=SEQUENCE)
        journal.claim_write(REQ)
        with pytest.raises(ControlInputJournalError):
            journal.mark_delivered(REQ, sequence_event_outcomes=[(0, "maybe")])
        record = journal.get(REQ)
        assert record.state == WRITING
        assert [e["outcome"] for e in record.sequence_events] == [None] * 4

    def test_malformed_sequence_events_are_refused_at_intent(self, journal):
        with pytest.raises(ValueError):
            journal.open_intent(_binding(), sequence_events=[{"type": "text", "text": ""}])
        assert journal.find(REQ) is None

    def test_v1_and_v2_requests_have_no_sequence_events(self, journal):
        journal.open_intent(_binding())
        journal.claim_write(REQ)
        record = journal.mark_delivered(REQ, chunks_sent=1, enter_attempted=True)
        assert record.sequence_events is None

    def test_a_swept_record_keeps_unrecorded_outcomes_null(self, journal, db_path):
        """The crash window: a claimed sequence whose owner died records no
        per-event outcomes, and the sweep never invents a boundary."""
        journal.open_intent(_binding(), sequence_events=SEQUENCE)
        journal.claim_write(REQ)
        # A different owner instance sweeps with every owner provably dead.
        resolved = ControlInputJournal(db_path).sweep_stranded(owner_alive=lambda pid: False)
        assert [record.state for record in resolved] == [STATE_AMBIGUOUS]
        record = journal.get(REQ)
        assert record.state == STATE_AMBIGUOUS
        assert [e["outcome"] for e in record.sequence_events] == [None] * 4
