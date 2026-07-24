"""Tests for the identity-bound control-input delivery path.

The property under test is one sentence: the exact characters the
operator wrote reached exactly one pane, exactly once, or the caller was
told truthfully that they did not.  These tests are organised around the
ways that sentence can quietly become false — framing bytes reaching a
composer, a control landing in a pane that was replaced, two writers
interleaving, a lost response answered by a second write — rather than
around the shape of the API.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from contextlib import contextmanager

import pytest

from cli_agent_orchestrator.clients.tmux import TmuxLiteralSendError
from cli_agent_orchestrator.services import control_input_service as service
from cli_agent_orchestrator.services.control_input_contract import (
    ACCEPTED,
    AMBIGUOUS,
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START,
    BRACKETED_PASTE_START_C1,
    CONTROL_INPUT_PROTOCOL,
    REASON_CONTROL_ROUTE_ABSENT,
    REASON_IDENTITY_MISMATCH,
    REASON_ILLEGAL_CONTROL_BYTES,
    REASON_LINEAGE_UNPROVEN,
    REASON_MANAGED_ACP_PANE,
    REASON_MULTILINE_REJECTED,
    REASON_OWNER_LOST_BEFORE_WRITE,
    REASON_OWNER_LOST_MID_WRITE,
    REASON_PANE_BUSY,
    REASON_PANE_DEAD,
    REASON_PROTOCOL_MISMATCH,
    REASON_REQUEST_REBOUND,
    REASON_STALE_GENERATION,
    REASON_UNKNOWN_TERMINAL,
    REASON_WRITE_INCOMPLETE,
    REFUSED,
    UNSUPPORTED,
    contains_bracketed_paste_sentinel,
    control_input_request_digest,
)
from cli_agent_orchestrator.services.control_input_journal import (
    DELIVERED,
    STATE_AMBIGUOUS,
    STATE_REFUSED,
    ControlInputBinding,
    ControlInputJournal,
)
from cli_agent_orchestrator.services.pane_input_arbiter import (
    pane_input_lease,
    reset_pane_input_arbiter,
)

TERMINAL = "a1b2c3d4"
CONTROL = "ctl-6f1b9c2d"
PANE = "%17"
WINDOW = "@3"
PANE_PID = 4242
GENERATION = "gen-7"
TEXT = "/compact"


class FakePaneIdentity:
    """Stands in for tmux's observed pane facts."""

    def __init__(self, *, pane_id=PANE, window_id=WINDOW, pane_pid=PANE_PID, dead=False):
        self.pane_id = pane_id
        self.window_id = window_id
        self.pane_pid = pane_pid
        self.session_name = "cao"
        self.window_name = "worker-1"
        self.bracketed_paste_proven = False
        self.dead = dead


class FakeTmux:
    """A tmux client that records every write, and offers no other way to write.

    Deliberately missing ``send_keys``, ``paste_buffer`` and friends: if
    the delivery path ever grew a fallback, these tests would fail with
    an AttributeError rather than silently exercising the fallback.
    """

    def __init__(self, identities=None, *, write_error=None, on_write=None):
        if identities is None:
            identities = [FakePaneIdentity()]
        self._identities = list(identities)
        self._write_error = write_error
        self._on_write = on_write
        self.writes = []
        self.identity_reads = 0

    def pane_control_identity(self, *, pane_id=None, session_name=None, window_name=None):
        self.identity_reads += 1
        if len(self._identities) > 1:
            return self._identities.pop(0)
        return self._identities[0]

    def send_literal_line(self, pane_id, text, submit=True):
        if self._on_write is not None:
            self._on_write()
        if self._write_error is not None:
            raise self._write_error
        self.writes.append({"pane_id": pane_id, "text": text, "submit": submit})
        return 1


def _metadata(**overrides):
    fields = {
        "pane_id": PANE,
        "generation": GENERATION,
        "provider": "claude-code",
        "tmux_session": "cao",
    }
    fields.update(overrides)
    return fields


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    """Pane locks and the journal both follow the state root, never the host's."""
    monkeypatch.setattr("cli_agent_orchestrator.constants.CAO_HOME_DIR", str(tmp_path / "state"))
    reset_pane_input_arbiter()
    service.reset_control_input_journal()
    yield
    reset_pane_input_arbiter()
    service.reset_control_input_journal()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "journal" / "control-input.sqlite3"


@pytest.fixture
def journal(db_path):
    return ControlInputJournal(db_path)


@pytest.fixture
def tmux(monkeypatch):
    client = FakeTmux()
    monkeypatch.setattr(service, "_tmux_client", lambda: client)
    monkeypatch.setattr(service, "_terminal_metadata", lambda terminal_id: _metadata())
    monkeypatch.setattr(service, "_managed_identity", lambda terminal_id: None)
    return client


def _deliver(journal, **overrides):
    kwargs = {"control_id": CONTROL, "text": TEXT, "enter": True}
    kwargs.update(overrides)
    return service.deliver_control_input(TERMINAL, journal=journal, **kwargs)


@contextmanager
def _pane_held_elsewhere(pane_id=PANE):
    """Hold the pane lease from another thread.

    It has to be another thread: the lease is non-reentrant by design, so
    holding it on this one would produce a reentry error rather than the
    busy refusal under test.
    """
    acquired, release = threading.Event(), threading.Event()
    failure = []

    def hold():
        try:
            with pane_input_lease(pane_id, holder="other-writer", timeout=0.0):
                acquired.set()
                release.wait(10)
        except Exception as exc:  # pragma: no cover - surfaced by the assert below
            failure.append(exc)
            acquired.set()

    worker = threading.Thread(target=hold, daemon=True)
    worker.start()
    assert acquired.wait(10), "the holding thread never took the lease"
    assert not failure, failure
    try:
        yield
    finally:
        release.set()
        worker.join(10)


def _dead_pid():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    return child.pid


class TestPayloadScreening:
    """Nothing that can synthesise its own framing or submit early gets typed."""

    @pytest.mark.parametrize(
        "text",
        [
            f"{BRACKETED_PASTE_START}/compact",
            f"/compact{BRACKETED_PASTE_END}",
            f"{BRACKETED_PASTE_START_C1}/compact",
        ],
    )
    def test_paste_framing_is_refused_not_stripped(self, tmux, journal, text):
        """Stripping would turn the payload's remainder into keystrokes."""
        result = _deliver(journal, text=text)
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_ILLEGAL_CONTROL_BYTES
        assert tmux.writes == []

    def test_the_c1_spelling_is_screened_like_the_esc_spelling(self, tmux, journal):
        """U+009B is ESC [ to a terminal in 8-bit mode; a screen with a
        known bypass is not a screen."""
        result = _deliver(journal, text=f"x{BRACKETED_PASTE_START_C1}y")
        assert result.reason_code == REASON_ILLEGAL_CONTROL_BYTES

    @pytest.mark.parametrize("text", ["/compact\nrm -rf /", "/compact\r"])
    def test_an_embedded_line_break_is_refused(self, tmux, journal, text):
        """It would submit at a point the caller did not choose."""
        result = _deliver(journal, text=text)
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_MULTILINE_REJECTED
        assert tmux.writes == []

    def test_other_control_characters_are_refused(self, tmux, journal):
        result = _deliver(journal, text="/compact\x03")
        assert result.reason_code == REASON_ILLEGAL_CONTROL_BYTES
        assert tmux.writes == []

    def test_a_refused_payload_opens_no_journal_record(self, tmux, journal):
        """Screening precedes the intent, so nothing durable is created."""
        _deliver(journal, text=f"{BRACKETED_PASTE_START}x")
        assert journal.find(CONTROL) is None

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"control_id": "has spaces"},
            {"control_id": ""},
            {"text": ""},
            {"text": "x" * (service.MAX_TEXT_BYTES + 1)},
            {"enter": "yes"},
            {"request_digest": "not-a-digest"},
        ],
    )
    def test_a_malformed_request_carries_no_typed_outcome(self, tmux, journal, kwargs):
        """Reason codes exist to tell apart failures a caller must act on
        differently; a malformed request has one action regardless."""
        with pytest.raises(service.ControlInputRequestInvalid):
            _deliver(journal, **kwargs)
        assert tmux.writes == []

    def test_the_byte_bound_is_on_utf8_not_characters(self, tmux, journal):
        """Both sides must mean the same thing by 'too long'."""
        text = "é" * (service.MAX_TEXT_BYTES // 2)
        assert len(text) < service.MAX_TEXT_BYTES < len(text.encode("utf-8")) + 1
        with pytest.raises(service.ControlInputRequestInvalid):
            _deliver(journal, text=text + "éé")


class TestIdentityBinding:
    """A control aimed at a terminal that has been replaced is refused."""

    def test_an_unknown_terminal_is_a_typed_refusal_not_a_404(self, tmux, journal, monkeypatch):
        """A 404 would be indistinguishable from 'this server has no
        control route', which demands the opposite action."""
        monkeypatch.setattr(service, "_terminal_metadata", lambda terminal_id: None)
        result = _deliver(journal)
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_UNKNOWN_TERMINAL
        assert result.http_status == 200

    def test_a_stale_generation_is_refused(self, tmux, journal):
        result = _deliver(journal, expected_identity={"terminal_generation": "gen-1"})
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_STALE_GENERATION
        assert tmux.writes == []

    def test_the_live_generation_is_accepted(self, tmux, journal):
        result = _deliver(journal, expected_identity={"terminal_generation": GENERATION})
        assert result.outcome == ACCEPTED

    def test_a_wrong_pane_birth_id_is_refused(self, tmux, journal):
        result = _deliver(journal, expected_identity={"pane_birth_id": "%99"})
        assert result.reason_code == REASON_IDENTITY_MISMATCH
        assert tmux.writes == []

    def test_a_wrong_provider_is_refused(self, tmux, journal):
        result = _deliver(journal, expected_identity={"provider": "codex"})
        assert result.reason_code == REASON_IDENTITY_MISMATCH

    def test_a_wrong_session_name_is_refused(self, tmux, journal):
        result = _deliver(journal, expected_identity={"session_name": "other"})
        assert result.reason_code == REASON_IDENTITY_MISMATCH

    def test_a_wrong_terminal_id_is_refused(self, tmux, journal):
        result = _deliver(journal, expected_identity={"terminal_id": "deadbeef"})
        assert result.reason_code == REASON_IDENTITY_MISMATCH

    @pytest.mark.parametrize(
        "field, value",
        [("terminal_incarnation", "inc-1"), ("provider_process_id", 991)],
    )
    def test_an_unprovable_expectation_fails_closed(self, tmux, journal, field, value):
        """Accepting an expectation nobody checked is how a caller comes
        to believe it bound to something it did not."""
        result = _deliver(journal, expected_identity={field: value})
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_LINEAGE_UNPROVEN
        assert tmux.writes == []

    def test_the_provider_process_id_is_never_aliased_to_the_pane_pid(self, tmux, journal):
        """pane_pid is the pane's root process; the provider is a
        descendant, so equating them would be a fabricated binding."""
        resolved = service.resolve_control_identity(TERMINAL)
        assert resolved.pane_pid == PANE_PID
        assert resolved.provider_process_id is None
        result = _deliver(journal, expected_identity={"provider_process_id": PANE_PID})
        assert result.reason_code == REASON_LINEAGE_UNPROVEN

    def test_a_managed_pane_is_refused_rather_than_typed_into(self, tmux, journal, monkeypatch):
        """Its pane runs a bridge process, not a composer."""
        monkeypatch.setattr(
            service, "_managed_identity", lambda terminal_id: {"generation": GENERATION}
        )
        result = _deliver(journal)
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_MANAGED_ACP_PANE
        assert tmux.writes == []

    def test_a_terminal_with_no_recorded_pane_is_refused(self, tmux, journal, monkeypatch):
        """A control bound to a mutable window name is bound to nothing."""
        monkeypatch.setattr(service, "_terminal_metadata", lambda tid: _metadata(pane_id=None))
        result = _deliver(journal)
        assert result.reason_code == REASON_LINEAGE_UNPROVEN

    def test_a_pane_that_is_gone_is_distinguished_from_one_never_recorded(
        self, monkeypatch, journal
    ):
        """Different facts, different refusals: a caller acts differently."""
        gone = FakeTmux(identities=[None])
        monkeypatch.setattr(service, "_tmux_client", lambda: gone)
        monkeypatch.setattr(service, "_terminal_metadata", lambda tid: _metadata())
        monkeypatch.setattr(service, "_managed_identity", lambda tid: None)
        result = _deliver(journal)
        assert result.reason_code == REASON_PANE_DEAD

    def test_a_dead_pane_is_refused(self, monkeypatch, journal):
        dead = FakeTmux(identities=[FakePaneIdentity(dead=True)])
        monkeypatch.setattr(service, "_tmux_client", lambda: dead)
        monkeypatch.setattr(service, "_terminal_metadata", lambda tid: _metadata())
        monkeypatch.setattr(service, "_managed_identity", lambda tid: None)
        result = _deliver(journal)
        assert result.reason_code == REASON_PANE_DEAD
        assert dead.writes == []

    def test_a_non_tmux_backend_is_unsupported_not_refused(self, monkeypatch, journal):
        """A refusal invites a re-attempt that could never succeed here."""
        monkeypatch.setattr(service, "_tmux_client", lambda: None)
        monkeypatch.setattr(service, "_terminal_metadata", lambda tid: _metadata())
        monkeypatch.setattr(service, "_managed_identity", lambda tid: None)
        result = _deliver(journal)
        assert result.outcome == UNSUPPORTED
        assert result.reason_code == REASON_CONTROL_ROUTE_ABSENT
        assert not result.as_response()["reattemptable"]


class TestReverificationUnderTheLease:
    def test_a_pane_replaced_after_resolution_is_caught_before_the_write(
        self, monkeypatch, journal
    ):
        """The gap between 'checked' and 'wrote' is where a control lands
        in a stranger's composer; the lease is what closes it."""
        replaced = FakeTmux(
            identities=[FakePaneIdentity(), FakePaneIdentity(pane_pid=PANE_PID + 1)]
        )
        monkeypatch.setattr(service, "_tmux_client", lambda: replaced)
        monkeypatch.setattr(service, "_terminal_metadata", lambda tid: _metadata())
        monkeypatch.setattr(service, "_managed_identity", lambda tid: None)

        result = _deliver(journal)
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_IDENTITY_MISMATCH
        assert replaced.writes == []
        assert journal.get(CONTROL).state == STATE_REFUSED

    def test_a_pane_that_dies_under_the_lease_is_refused(self, monkeypatch, journal):
        dying = FakeTmux(identities=[FakePaneIdentity(), None])
        monkeypatch.setattr(service, "_tmux_client", lambda: dying)
        monkeypatch.setattr(service, "_terminal_metadata", lambda tid: _metadata())
        monkeypatch.setattr(service, "_managed_identity", lambda tid: None)

        result = _deliver(journal)
        assert result.reason_code == REASON_PANE_DEAD
        assert dying.writes == []

    def test_the_window_is_re_verified_too(self, monkeypatch, journal):
        moved = FakeTmux(identities=[FakePaneIdentity(), FakePaneIdentity(window_id="@9")])
        monkeypatch.setattr(service, "_tmux_client", lambda: moved)
        monkeypatch.setattr(service, "_terminal_metadata", lambda tid: _metadata())
        monkeypatch.setattr(service, "_managed_identity", lambda tid: None)

        result = _deliver(journal)
        assert result.reason_code == REASON_IDENTITY_MISMATCH
        assert moved.writes == []


class TestArbitration:
    def test_a_busy_pane_is_refused_rather_than_queued(self, tmux, journal):
        """A refusal is the honest answer that permits a retry; blocking
        would convert it into an unbounded request."""
        with _pane_held_elsewhere():
            result = _deliver(journal)
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_PANE_BUSY
        assert tmux.writes == []

    def test_a_busy_refusal_is_durable_and_then_re_attemptable(self, tmux, journal):
        """'reattemptable: true' has to actually be true, or the pane
        being busy for one instant would be permanent for that control."""
        with _pane_held_elsewhere():
            first = _deliver(journal)
        assert first.reason_code == REASON_PANE_BUSY
        assert journal.get(CONTROL).state == STATE_REFUSED

        second = _deliver(journal)
        assert second.outcome == ACCEPTED
        assert len(tmux.writes) == 1
        assert [event["to_state"] for event in journal.get(CONTROL).events][:2] == [
            "intent",
            STATE_REFUSED,
        ]

    def test_the_lease_is_released_after_a_successful_write(self, tmux, journal):
        assert _deliver(journal).outcome == ACCEPTED
        result = _deliver(journal, control_id="ctl-second", text="/clear")
        assert result.outcome == ACCEPTED


class TestDelivery:
    def test_the_text_is_typed_literally_with_one_explicit_enter(self, tmux, journal):
        result = _deliver(journal)
        assert result.outcome == ACCEPTED
        assert result.text_sent and result.enter_sent
        assert tmux.writes == [{"pane_id": PANE, "text": TEXT, "submit": True}]

    def test_nothing_written_carries_paste_framing(self, tmux, journal):
        """The leakage this lane exists to remove is structurally absent
        rather than conditionally avoided."""
        _deliver(journal)
        for write in tmux.writes:
            assert not contains_bracketed_paste_sentinel(write["text"])
            assert "\x1b" not in write["text"] and "\x9b" not in write["text"]

    def test_enter_is_stated_not_inferred(self, tmux, journal):
        result = _deliver(journal, enter=False)
        assert result.outcome == ACCEPTED
        assert result.text_sent and not result.enter_sent
        assert tmux.writes[0]["submit"] is False

    def test_delivery_is_recorded_with_the_enter_it_actually_sent(self, tmux, journal):
        """A replayed record must answer whether the provider already
        started acting on the control."""
        _deliver(journal, enter=False)
        record = journal.get(CONTROL)
        assert record.state == DELIVERED
        assert record.enter_attempted is False
        assert record.chunks_sent == 1

    def test_the_resolved_identity_is_echoed_back(self, tmux, journal):
        """A caller cannot declare a pane birth id it was never told."""
        payload = _deliver(journal).as_response()
        identity = payload["resolved_identity"]
        assert identity["terminal_id"] == TERMINAL
        assert identity["pane_birth_id"] == PANE
        assert identity["terminal_generation"] == GENERATION
        assert identity["pane"] == {
            "pane_id": PANE,
            "window_id": WINDOW,
            "pane_pid": PANE_PID,
            "dead": False,
        }

    def test_accepted_is_not_reattemptable(self, tmux, journal):
        assert not _deliver(journal).as_response()["reattemptable"]


class TestTheRequestDigest:
    def test_the_server_digest_matches_the_shared_contract(self, tmux, journal):
        expected = control_input_request_digest(
            control_id=CONTROL,
            text=TEXT,
            enter=True,
            expected_identity={"terminal_generation": GENERATION},
        )
        result = _deliver(journal, expected_identity={"terminal_generation": GENERATION})
        assert result.request_digest == expected

    def test_a_matching_caller_digest_is_accepted(self, tmux, journal):
        digest = control_input_request_digest(
            control_id=CONTROL, text=TEXT, enter=True, expected_identity=None
        )
        assert _deliver(journal, request_digest=digest).outcome == ACCEPTED

    def test_a_mismatched_caller_digest_is_refused_before_any_write(self, tmux, journal):
        """The control the caller authorised is not the one that arrived."""
        digest = control_input_request_digest(
            control_id=CONTROL, text="/clear", enter=True, expected_identity=None
        )
        result = _deliver(journal, request_digest=digest)
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_REQUEST_REBOUND
        assert tmux.writes == []
        assert journal.find(CONTROL) is None


class TestAtMostOnce:
    def test_an_identical_retry_after_delivery_does_not_write_twice(self, tmux, journal):
        """The whole point of the journal: ask, do not re-send."""
        first = _deliver(journal)
        second = _deliver(journal)
        assert first.outcome == second.outcome == ACCEPTED
        assert len(tmux.writes) == 1
        assert second.state == DELIVERED

    def test_a_reused_control_id_with_different_text_is_refused(self, tmux, journal):
        _deliver(journal)
        result = _deliver(journal, text="/clear")
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_REQUEST_REBOUND
        assert len(tmux.writes) == 1

    def test_a_second_writer_never_writes_while_a_claim_is_held(self, tmux, journal, db_path):
        """A caller holding a refused claim must not write even when the
        record looks abandoned: that owner may be mid-write right now."""
        other = ControlInputJournal(db_path)
        other.open_intent(
            ControlInputBinding(
                request_id=CONTROL,
                terminal_id=TERMINAL,
                pane_id=PANE,
                window_id=WINDOW,
                pane_pid=PANE_PID,
                generation=GENERATION,
                request_sha256=control_input_request_digest(
                    control_id=CONTROL, text=TEXT, enter=True, expected_identity=None
                ),
            )
        )
        other.claim_write(CONTROL)

        result = _deliver(journal)
        assert result.outcome is None
        assert result.as_response()["in_flight"] is True
        assert tmux.writes == []


class TestWriteFailure:
    def test_a_partial_write_is_ambiguous_not_refused(self, monkeypatch, journal):
        """Recording post-attempt uncertainty as a refusal would license
        a caller to re-send bytes that may already have landed."""
        failing = FakeTmux(
            write_error=TmuxLiteralSendError("boom", chunks_sent=1, enter_attempted=True)
        )
        monkeypatch.setattr(service, "_tmux_client", lambda: failing)
        monkeypatch.setattr(service, "_terminal_metadata", lambda tid: _metadata())
        monkeypatch.setattr(service, "_managed_identity", lambda tid: None)

        result = _deliver(journal)
        assert result.outcome == AMBIGUOUS
        assert result.reason_code == REASON_WRITE_INCOMPLETE
        assert result.chunks_sent == 1
        assert result.enter_attempted is True
        assert not result.text_sent and not result.enter_sent
        assert not result.as_response()["reattemptable"]
        assert journal.get(CONTROL).state == STATE_AMBIGUOUS

    def test_an_ambiguous_request_is_never_re_driven(self, monkeypatch, journal):
        failing = FakeTmux(
            write_error=TmuxLiteralSendError("boom", chunks_sent=1, enter_attempted=False)
        )
        monkeypatch.setattr(service, "_tmux_client", lambda: failing)
        monkeypatch.setattr(service, "_terminal_metadata", lambda tid: _metadata())
        monkeypatch.setattr(service, "_managed_identity", lambda tid: None)
        _deliver(journal)

        healthy = FakeTmux()
        monkeypatch.setattr(service, "_tmux_client", lambda: healthy)
        again = _deliver(journal)
        assert again.outcome == AMBIGUOUS
        assert healthy.writes == []


class TestResponseLoss:
    def test_a_delivered_control_answers_from_the_record(self, tmux, journal):
        _deliver(journal)
        answer = service.lookup_control_input(CONTROL, journal=journal)
        assert answer.outcome == ACCEPTED
        assert answer.terminal_id == TERMINAL
        assert answer.enter_sent is True

    def test_an_unknown_control_id_proves_nothing_was_written(self, journal):
        """The intent commits before the first byte, so the absence of a
        record is positive proof rather than an optimistic default."""
        answer = service.lookup_control_input("ctl-never-sent", journal=journal)
        assert answer.outcome == REFUSED
        assert answer.reason_code == REASON_OWNER_LOST_BEFORE_WRITE
        assert answer.as_response()["reattemptable"] is True

    def test_a_refusal_is_answered_as_a_refusal(self, tmux, journal):
        with _pane_held_elsewhere():
            _deliver(journal)
        answer = service.lookup_control_input(CONTROL, journal=journal)
        assert answer.outcome == REFUSED
        assert answer.reason_code == REASON_PANE_BUSY

    def test_a_malformed_control_id_is_rejected_rather_than_looked_up(self, journal):
        with pytest.raises(service.ControlInputRequestInvalid):
            service.lookup_control_input("not a control id", journal=journal)

    def test_the_lookup_is_not_scoped_to_a_terminal(self, tmux, journal):
        """A terminal-scoped lookup could answer 'nothing was written'
        about a control that was — the worst answer this surface has."""
        _deliver(journal)
        assert service.lookup_control_input(CONTROL, journal=journal).terminal_id == TERMINAL


class TestCrashWindow:
    def test_a_stranded_claim_resolves_to_ambiguous_when_asked(self, journal, db_path):
        """A dead owner had the right to write and may have used it."""
        stale = ControlInputJournal(db_path, owner_pid=_dead_pid())
        stale.open_intent(
            ControlInputBinding(
                request_id=CONTROL,
                terminal_id=TERMINAL,
                pane_id=PANE,
                window_id=WINDOW,
                pane_pid=PANE_PID,
                generation=GENERATION,
                request_sha256="a" * 64,
            )
        )
        stale.claim_write(CONTROL)

        answer = service.lookup_control_input(CONTROL, journal=journal)
        assert answer.outcome == AMBIGUOUS
        assert answer.reason_code == REASON_OWNER_LOST_MID_WRITE

    def test_a_stranded_intent_resolves_to_refused_when_asked(self, journal, db_path):
        """It never reached the claim, so the pane was never touched."""
        stale = ControlInputJournal(db_path, owner_pid=_dead_pid())
        stale.open_intent(
            ControlInputBinding(
                request_id=CONTROL,
                terminal_id=TERMINAL,
                pane_id=PANE,
                window_id=WINDOW,
                pane_pid=PANE_PID,
                generation=GENERATION,
                request_sha256="b" * 64,
            )
        )

        answer = service.lookup_control_input(CONTROL, journal=journal)
        assert answer.outcome == REFUSED
        assert answer.reason_code == REASON_OWNER_LOST_BEFORE_WRITE


class TestProtocolCompatibility:
    def test_an_unknown_protocol_is_unsupported_and_never_falls_back(self, tmux, journal):
        """No degradation to a paste or to raw keys: a control the
        operator believes was sent once must not arrive as other bytes."""
        result = _deliver(journal, protocol="cao-control-input-v99")
        assert result.outcome == UNSUPPORTED
        assert result.reason_code == REASON_PROTOCOL_MISMATCH
        assert result.http_status == 422
        assert tmux.writes == []
        assert journal.find(CONTROL) is None

    def test_the_current_protocol_is_accepted(self, tmux, journal):
        result = _deliver(journal, protocol=CONTROL_INPUT_PROTOCOL)
        assert result.outcome == ACCEPTED

    def test_the_protocol_is_checked_before_the_request_shape(self, tmux, journal):
        """A caller speaking another protocol may have other rules; a
        field error would invite a retry that can never succeed."""
        result = service.deliver_control_input(
            TERMINAL,
            control_id="not a valid id",
            text="",
            enter="maybe",
            protocol="cao-control-input-v99",
            journal=journal,
        )
        assert result.reason_code == REASON_PROTOCOL_MISMATCH


class TestCapabilityAdvertisement:
    def test_support_is_discoverable_without_typing_anything(self):
        """A probe that succeeded would already have typed into a composer."""
        caps = service.control_input_capabilities()
        assert caps["protocol"] == CONTROL_INPUT_PROTOCOL
        assert caps["literal_write"] is True
        assert caps["bracketed_paste"] is False
        assert caps["max_text_bytes"] == service.MAX_TEXT_BYTES

    def test_the_advertised_vocabulary_is_the_one_enforced(self):
        caps = service.control_input_capabilities()
        assert REASON_PANE_BUSY in caps["reason_codes"]
        assert set(caps["outcomes"]) == {ACCEPTED, REFUSED, AMBIGUOUS, UNSUPPORTED}
        assert caps["execution_modes"] == [service.EXECUTION_MODE_NATIVE_TUI]


class TestResultInvariants:
    def test_a_reason_can_never_be_reported_with_the_wrong_outcome(self):
        """The one place a reason and an outcome meet on the wire."""
        with pytest.raises(ValueError):
            service.ControlInputResult(
                control_id=CONTROL, outcome=REFUSED, reason_code=REASON_WRITE_INCOMPLETE
            )
        with pytest.raises(ValueError):
            service.ControlInputResult(
                control_id=CONTROL, outcome=AMBIGUOUS, reason_code=REASON_PANE_BUSY
            )

    def test_an_unknown_outcome_or_reason_is_rejected(self):
        with pytest.raises(ValueError):
            service.ControlInputResult(control_id=CONTROL, outcome="probably-fine")
        with pytest.raises(ValueError):
            service.ControlInputResult(control_id=CONTROL, outcome=REFUSED, reason_code="vibes")

    def test_only_refused_licenses_a_re_attempt(self):
        for outcome, reattemptable in [
            (REFUSED, True),
            (ACCEPTED, False),
            (AMBIGUOUS, False),
            (UNSUPPORTED, False),
        ]:
            payload = service.ControlInputResult(control_id=CONTROL, outcome=outcome).as_response()
            assert payload["reattemptable"] is reattemptable
