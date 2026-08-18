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
import time
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.clients.tmux import TmuxLiteralSendError, TmuxServerIdentityError
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import control_input_service as service
from cli_agent_orchestrator.services import generation_fence as gf
from cli_agent_orchestrator.services import native_pane_input
from cli_agent_orchestrator.services.control_input_contract import (
    ACCEPTED,
    AMBIGUOUS,
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START,
    BRACKETED_PASTE_START_C1,
    CONTROL_INPUT_PROTOCOL,
    REASON_CONTROL_ROUTE_ABSENT,
    REASON_GENERATION_FENCED,
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
    REASON_SERVER_IDENTITY_UNREADABLE,
    REASON_STALE_GENERATION,
    REASON_UNKNOWN_TERMINAL,
    REASON_WRITE_INCOMPLETE,
    REFUSED,
    SUBMISSION_SUBMITTED,
    SUBMISSION_UNKNOWN,
    SUBMISSION_UNSUBMITTED,
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
    ControlInputNotFound,
)
from cli_agent_orchestrator.services.native_pane_input import (
    NativePaneInputUnavailable,
    SubmissionBarrier,
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
# Absolute and already canonical, so normalize_server_identity is an
# identity function on it and a test that fails is reporting a real
# disagreement rather than a realpath difference.
SOCKET = "/private/tmp/tmux-501/cao-test"
OTHER_SOCKET = "/private/tmp/tmux-501/somebody-elses-server"
TEXT = "/compact"


class FakePaneIdentity:
    """Stands in for tmux's observed pane facts."""

    def __init__(
        self,
        *,
        pane_id=PANE,
        window_id=WINDOW,
        pane_pid=PANE_PID,
        dead=False,
        server_socket_path=SOCKET,
    ):
        self.pane_id = pane_id
        self.window_id = window_id
        self.pane_pid = pane_pid
        self.session_name = "cao"
        self.window_name = "worker-1"
        self.bracketed_paste_proven = False
        self.dead = dead
        self.server_socket_path = server_socket_path


class FakeTmux:
    """A tmux client that records every write, and offers no other way to write.

    Deliberately missing ``send_keys``, ``paste_buffer`` and friends: if
    the delivery path ever grew a fallback, these tests would fail with
    an AttributeError rather than silently exercising the fallback.
    """

    def __init__(
        self,
        identities=None,
        *,
        write_error=None,
        on_write=None,
        read_error=None,
        read_error_after=0,
    ):
        if identities is None:
            identities = [FakePaneIdentity()]
        self._identities = list(identities)
        self._write_error = write_error
        self._on_write = on_write
        self._read_error = read_error
        self._read_error_after = read_error_after
        self._successful_reads = 0
        self.writes = []
        self.identity_reads = 0

    def pane_control_identity(
        self,
        *,
        pane_id=None,
        session_name=None,
        window_name=None,
        deadline_monotonic=None,
    ):
        self.identity_reads += 1
        # Time out only after N successful reads, so a test can let the
        # pre-lease resolution succeed and time out the in-lease preflight.
        if self._read_error is not None and self._successful_reads >= self._read_error_after:
            raise self._read_error
        self._successful_reads += 1
        if len(self._identities) > 1:
            return self._identities.pop(0)
        return self._identities[0]

    # Keyword-only and undefaulted, exactly like the real primitive: a
    # fake that tolerated the argument being omitted would let the one
    # mistake §24.7 is about pass every test in this file.
    def send_literal_line(
        self,
        pane_id,
        text,
        submit=True,
        *,
        expected_server_identity,
        deadline_monotonic=None,
    ):
        if self._on_write is not None:
            self._on_write()
        if self._write_error is not None:
            raise self._write_error
        self.writes.append(
            {
                "pane_id": pane_id,
                "text": text,
                "submit": submit,
                "expected_server_identity": expected_server_identity,
            }
        )
        return 1

    def send_sequence_key(
        self,
        pane_id,
        key,
        *,
        expected_server_identity,
        deadline_monotonic=None,
    ):
        if self._on_write is not None:
            self._on_write()
        if self._write_error is not None:
            raise self._write_error
        self.writes.append(
            {
                "pane_id": pane_id,
                "key": key,
                "expected_server_identity": expected_server_identity,
            }
        )

    def send_steer_chord(
        self,
        pane_id,
        chord,
        *,
        expected_server_identity,
        deadline_monotonic=None,
    ):
        if self._on_write is not None:
            self._on_write()
        if self._write_error is not None:
            raise self._write_error
        self.writes.append(
            {
                "pane_id": pane_id,
                "chord": chord,
                "expected_server_identity": expected_server_identity,
            }
        )

    # The cond-0178 copy-mode guard primitives.  Default: the pane is
    # provably not in copy mode, so no exit control is ever recorded for a
    # test that does not ask for one — the guard is never speculative.
    def pane_in_copy_mode(
        self,
        pane_id,
        *,
        expected_server_identity,
        deadline_monotonic=None,
    ):
        return False

    def send_copy_mode_cancel(
        self,
        pane_id,
        *,
        expected_server_identity,
        deadline_monotonic=None,
    ):
        self.writes.append(
            {
                "pane_id": pane_id,
                "copy_mode_cancel": True,
                "expected_server_identity": expected_server_identity,
            }
        )
        return True


def _metadata(**overrides):
    fields = {
        "pane_id": PANE,
        "generation": GENERATION,
        "provider": "claude-code",
        "tmux_session": "cao",
        "server_socket_path": SOCKET,
    }
    fields.update(overrides)
    return fields


@pytest.fixture(autouse=True)
def _isolated_state(isolated_memory_db, monkeypatch, tmp_path):
    """Pane locks and the journal both follow the state root, never the host's."""
    from cli_agent_orchestrator import constants

    monkeypatch.setattr("cli_agent_orchestrator.constants.CAO_HOME_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(constants, "COMPANION_DIR", tmp_path / "companion")
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


def test_unmanaged_activated_control_refuses_before_roster_binding(
    isolated_memory_db, tmux, journal, monkeypatch
):
    """A visible ordinary Claude pane is not writable while its pre-task
    stable-agent/native binding is still absent."""
    monkeypatch.setattr(
        service,
        "_terminal_metadata",
        lambda terminal_id: _metadata(provider="claude_code"),
    )
    from cli_agent_orchestrator.services import stable_agent_roster as roster
    from cli_agent_orchestrator.services import unmanaged_native_identity as seam

    roster.bind_generation(
        roster.BindingContract(
            agent_id=roster.derive_initial_agent_id(TERMINAL, GENERATION),
            session_name="cao",
            role=roster.ROLE_WORKER,
            profile_family="developer",
            harness="claude_code",
            terminal_id=TERMINAL,
            generation=GENERATION,
            pane_id=PANE,
            pane_pid=PANE_PID,
            execution_mode="native_tui",
            continuity_note=seam.PRE_TASK_IDENTITY_PENDING,
        )
    )

    result = _deliver(journal)

    assert result.outcome == REFUSED
    assert result.reason_code == REASON_LINEAGE_UNPROVEN
    assert tmux.writes == []
    with pytest.raises(ControlInputNotFound):
        journal.get(CONTROL)


def test_unmanaged_control_refused_while_row_pending_marker_no_roster(
    isolated_memory_db, tmux, journal, monkeypatch
):
    """The row is addressable before the pre-task roster marker commits.
    A row stamped with the new-launch pending state must refuse a concurrent
    control with the typed lineage refusal and zero pane writes — never a
    legacy exemption."""
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.services import unmanaged_native_identity as seam

    database.create_terminal(
        TERMINAL,
        "cao",
        "worker-abcd",
        "claude_code",
        generation=GENERATION,
        pane_id=PANE,
        pane_pid=PANE_PID,
        pre_task_identity_state=seam.PRE_TASK_IDENTITY_PENDING,
    )
    monkeypatch.setattr(
        service,
        "_terminal_metadata",
        lambda terminal_id: _metadata(
            provider="claude_code", pre_task_identity_state=seam.PRE_TASK_IDENTITY_PENDING
        ),
    )

    result = _deliver(journal)

    assert result.outcome == REFUSED
    assert result.reason_code == REASON_LINEAGE_UNPROVEN
    assert tmux.writes == []
    with pytest.raises(ControlInputNotFound):
        journal.get(CONTROL)


def test_control_gate_uses_resolved_state_without_db_fallback(
    isolated_memory_db, tmux, journal, monkeypatch
):
    """The admission gate consumes the state its identity resolution already
    read and never issues a second metadata query.  An injected database
    failure cannot be converted into a legacy exemption: the carried pending
    state still refuses the control with zero pane writes."""
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.services import unmanaged_native_identity as seam

    monkeypatch.setattr(
        service,
        "_terminal_metadata",
        lambda terminal_id: _metadata(
            provider="claude_code", pre_task_identity_state=seam.PRE_TASK_IDENTITY_PENDING
        ),
    )

    def _db_must_not_be_consulted(*args, **kwargs):
        raise AssertionError("admission must not re-query the database")

    monkeypatch.setattr(database, "get_terminal_metadata", _db_must_not_be_consulted)

    result = _deliver(journal)

    assert result.outcome == REFUSED
    assert result.reason_code == REASON_LINEAGE_UNPROVEN
    assert tmux.writes == []
    with pytest.raises(ControlInputNotFound):
        journal.get(CONTROL)


def test_unmanaged_control_refused_after_capture_before_ready(
    isolated_memory_db, tmux, journal, monkeypatch
):
    """Identity capture alone is not admission.  Between the durable
    native-id capture and provider/TUI readiness, a concurrent control must
    be refused with the typed lineage refusal and zero pane writes."""
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.services import stable_agent_roster as roster
    from cli_agent_orchestrator.services import unmanaged_native_identity as seam

    native_id = "019fb17d-0c6d-7161-a408-6b1fa61c8f2d"
    database.create_terminal(
        TERMINAL,
        "cao",
        "worker-abcd",
        "claude_code",
        generation=GENERATION,
        pane_id=PANE,
        pane_pid=PANE_PID,
        native_session_id=native_id,
        pre_task_identity_state=seam.PRE_TASK_IDENTITY_CAPTURED,
    )
    roster.bind_generation(
        roster.BindingContract(
            agent_id=roster.derive_initial_agent_id(TERMINAL, GENERATION),
            session_name="cao",
            role=roster.ROLE_WORKER,
            profile_family="developer",
            harness="claude_code",
            native_session_id=native_id,
            acquisition_method=roster.ACQUISITION_CHOSEN_SESSION_ID,
            terminal_id=TERMINAL,
            generation=GENERATION,
            pane_id=PANE,
            pane_pid=PANE_PID,
            execution_mode="native_tui",
            continuity_note=seam.PRE_TASK_IDENTITY_CAPTURED,
        )
    )
    monkeypatch.setattr(
        service,
        "_terminal_metadata",
        lambda terminal_id: _metadata(
            provider="claude_code", pre_task_identity_state=seam.PRE_TASK_IDENTITY_CAPTURED
        ),
    )

    result = _deliver(journal)

    assert result.outcome == REFUSED
    assert result.reason_code == REASON_LINEAGE_UNPROVEN
    assert tmux.writes == []
    with pytest.raises(ControlInputNotFound):
        journal.get(CONTROL)


def test_unmanaged_control_gate_opens_after_readiness_transition(
    isolated_memory_db, tmux, journal, monkeypatch
):
    """The same row becomes control-admissible only after the readiness
    transition: the shared admission seam passes (the rest of the
    identity-bound checks then decide the write)."""
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.services import stable_agent_roster as roster
    from cli_agent_orchestrator.services import unmanaged_native_identity as seam

    native_id = "019fb17d-0c6d-7161-a408-6b1fa61c8f2d"
    database.create_terminal(
        TERMINAL,
        "cao",
        "worker-abcd",
        "claude_code",
        generation=GENERATION,
        pane_id=PANE,
        pane_pid=PANE_PID,
        native_session_id=native_id,
        pre_task_identity_state=seam.PRE_TASK_IDENTITY_CAPTURED,
    )
    roster.bind_generation(
        roster.BindingContract(
            agent_id=roster.derive_initial_agent_id(TERMINAL, GENERATION),
            session_name="cao",
            role=roster.ROLE_WORKER,
            profile_family="developer",
            harness="claude_code",
            native_session_id=native_id,
            acquisition_method=roster.ACQUISITION_CHOSEN_SESSION_ID,
            terminal_id=TERMINAL,
            generation=GENERATION,
            pane_id=PANE,
            pane_pid=PANE_PID,
            execution_mode="native_tui",
            continuity_note=seam.PRE_TASK_IDENTITY_CAPTURED,
        )
    )

    def _live_metadata(terminal_id):
        # The real metadata read reflects the durable row: captured before
        # the readiness transition, ready after it.
        row = database.get_terminal_metadata(terminal_id) or {}
        return _metadata(
            provider="claude_code",
            pre_task_identity_state=row.get("pre_task_identity_state"),
        )

    monkeypatch.setattr(service, "_terminal_metadata", _live_metadata)

    # Before the transition the control is refused at the lineage gate...
    result = _deliver(journal)
    assert result.outcome == REFUSED
    assert result.reason_code == REASON_LINEAGE_UNPROVEN
    assert tmux.writes == []
    with pytest.raises(ControlInputNotFound):
        journal.get(CONTROL)

    # ...and after the transition the same gate opens: the roster lineage
    # AND the dedicated row state both reached ready.
    seam.mark_pre_task_identity_ready(terminal_id=TERMINAL, generation=GENERATION)
    row = database.get_terminal_metadata(TERMINAL)
    assert row["pre_task_identity_state"] == seam.PRE_TASK_IDENTITY_READY
    assert row["native_session_id"] == native_id
    seam.assert_unmanaged_admission_ready(
        TERMINAL,
        {
            "provider": "claude_code",
            "generation": GENERATION,
            "pre_task_identity_state": seam.PRE_TASK_IDENTITY_READY,
        },
    )
    result = _deliver(journal)
    assert result.reason_code != REASON_LINEAGE_UNPROVEN or result.outcome == ACCEPTED


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
        assert tmux.writes == [
            {
                "pane_id": PANE,
                "text": TEXT,
                "submit": True,
                # The write primitive is handed the *bound* server, never
                # the one just observed: handing it the observation would
                # ask it to compare a reading with itself.
                "expected_server_identity": SOCKET,
            }
        ]

    def test_stop_barrier_refuses_before_any_provider_byte(self, isolated_memory_db, tmux, journal):
        from cli_agent_orchestrator.services import operation_journal
        from cli_agent_orchestrator.services.control_input_contract import (
            REASON_SESSION_EFFECT_BARRIER,
        )

        operation_journal.claim_session_barrier("cao", claimed_by="stop-operation")

        result = _deliver(journal)

        assert result.outcome == REFUSED
        assert result.reason_code == REASON_SESSION_EFFECT_BARRIER
        assert tmux.writes == []

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
            "bound_server_socket_path": SOCKET,
            "observed_server_socket_path": SOCKET,
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
                # Must match what the service will bind, or this is a
                # rebinding rather than the claim contention under test.
                server_socket_path=SOCKET,
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
                server_socket_path=SOCKET,
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
                server_socket_path=SOCKET,
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


# ---------------------------------------------------------------------------
# v2 chord (schema v2 steer chord)
# ---------------------------------------------------------------------------

from cli_agent_orchestrator.services.control_input_contract import (  # noqa: E402
    REASON_UNSUPPORTED_CHORD,
    control_input_request_digest_v2,
)


def _chord_digest(chord="C-s"):
    return control_input_request_digest_v2(
        control_id=CONTROL,
        text=TEXT,
        enter=False,
        chord=chord,
        expected_identity={"terminal_id": TERMINAL, "terminal_generation": GENERATION},
    )


def _chord_binding(digest):
    return ControlInputBinding(
        request_id=CONTROL,
        terminal_id=TERMINAL,
        pane_id=PANE,
        window_id=WINDOW,
        pane_pid=PANE_PID,
        request_sha256=digest,
        generation=GENERATION,
        server_socket_path=SOCKET,
    )


def _chord_resolved(provider="kimi_cli", version="0.29.0"):
    return service.ResolvedControlIdentity(
        terminal_id=TERMINAL,
        terminal_incarnation=None,
        terminal_generation=GENERATION,
        provider=provider,
        native_session_id="sess-1",
        execution_mode=service.EXECUTION_MODE_NATIVE_TUI,
        session_name="cao",
        provider_process_id="4242@boot-1",
        provider_version=version,
        pane_id=PANE,
        window_id=WINDOW,
        pane_pid=PANE_PID,
        bound_server_socket_path=SOCKET,
        observed_server_socket_path=SOCKET,
    )


class _FakeChordAdapter:
    """A native adapter that types the text and (unlike the real one) no more."""

    class ComposerWriteInterrupted(Exception):
        def __init__(self, detail, *, enter_attempted=False):
            super().__init__(detail)
            self.detail = detail
            self.enter_attempted = enter_attempted

    def __init__(self, *, raise_after_text=None):
        self._raise_after_text = raise_after_text

    def execute_composer_plan(self, *, plan, transport, submit, deadline_monotonic=None):
        transport.send_literal("typed-text")
        if self._raise_after_text is not None:
            raise self._raise_after_text


class _FakeChordClient:
    """Records literal writes and steer-chord presses, nothing else."""

    def __init__(self, *, literal_error=None, chord_error=None):
        self.sent = []
        self._literal_error = literal_error
        self._chord_error = chord_error

    def send_literal_line(
        self,
        pane_id,
        text,
        submit=True,
        *,
        expected_server_identity,
        deadline_monotonic=None,
    ):
        if self._literal_error is not None:
            raise self._literal_error
        self.sent.append(("literal", text, submit))
        return 1

    def send_steer_chord(
        self, pane_id, chord, *, expected_server_identity, deadline_monotonic=None
    ):
        if self._chord_error is not None:
            raise self._chord_error
        self.sent.append(("chord", chord))

    def send_control_key(self, pane_id, key, *, expected_server_identity, deadline_monotonic=None):
        self.sent.append(("key", key))

    def send_sequence_key(self, pane_id, key, *, expected_server_identity, deadline_monotonic=None):
        self.sent.append(("key", key))


class TestChordShapeValidation:
    def test_chord_requires_enter_false(self):
        with pytest.raises(service.ControlInputRequestInvalid):
            service._require_shape(CONTROL, TEXT, True, None, chord="C-s")

    def test_empty_chord_rejected(self):
        with pytest.raises(service.ControlInputRequestInvalid):
            service._require_shape(CONTROL, TEXT, False, None, chord="")

    def test_non_string_chord_rejected(self):
        with pytest.raises(service.ControlInputRequestInvalid):
            service._require_shape(CONTROL, TEXT, False, None, chord=123)

    def test_no_chord_allows_enter_true(self):
        service._require_shape(CONTROL, TEXT, True, None, chord=None)

    def test_chord_with_enter_false_is_well_formed(self):
        service._require_shape(CONTROL, TEXT, False, None, chord="C-s")


class TestSteerChordAllowlist:
    def test_allowed_chord_proceeds(self):
        assert service._steer_chord_refusal(_chord_resolved(version="0.29.0"), "C-s") is None
        assert service._steer_chord_refusal(_chord_resolved(version="0.29.1"), "C-s") is None

    def test_wrong_chord_refused_with_zero_bytes(self):
        reason = service._steer_chord_refusal(_chord_resolved(version="0.29.0"), "C-x")
        assert reason is not None
        assert reason[0] == REASON_UNSUPPORTED_CHORD

    def test_unpinned_version_refused(self):
        reason = service._steer_chord_refusal(_chord_resolved(version="0.40.0"), "C-s")
        assert reason is not None and reason[0] == REASON_UNSUPPORTED_CHORD

    def test_absent_version_refused(self):
        reason = service._steer_chord_refusal(_chord_resolved(version=None), "C-s")
        assert reason is not None and reason[0] == REASON_UNSUPPORTED_CHORD

    def test_wrong_provider_refused(self):
        reason = service._steer_chord_refusal(
            _chord_resolved(provider="claude_code", version="0.29.0"), "C-s"
        )
        assert reason is not None and reason[0] == REASON_UNSUPPORTED_CHORD

    def test_absent_chord_is_not_a_refusal(self):
        assert service._steer_chord_refusal(_chord_resolved(version="0.29.0"), None) is None


class TestChordExecution:
    """text-then-chord under one lease; chord failure after text is ambiguous."""

    def _send(self, journal, client, *, chord, chord_error=None, enter=False):
        client._chord_error = chord_error
        adapter = _FakeChordAdapter()
        digest = (
            _chord_digest(chord)
            if chord
            else control_input_request_digest(
                control_id=CONTROL,
                text=TEXT,
                enter=enter,
                expected_identity={"terminal_id": TERMINAL, "terminal_generation": GENERATION},
            )
        )
        binding = _chord_binding(digest)
        # The real caller opens intent and claims the write before this runs;
        # _send_through_native_adapter assumes a WRITING record.
        journal.open_intent(binding)
        journal.claim_write(CONTROL)
        return service._send_through_native_adapter(
            journal,
            client,
            binding,
            adapter=adapter,
            plan={"lines": [TEXT]},
            enter=enter,
            chord=chord,
            terminal_id=TERMINAL,
            resolved=_chord_resolved(),
            digest=digest,
            deadline_monotonic=time.monotonic() + service.WRITE_DEADLINE_SECONDS,
        )

    def test_text_is_written_then_chord_pressed_last(self, journal):
        client = _FakeChordClient()
        result = self._send(journal, client, chord="C-s")
        assert result.outcome == ACCEPTED
        assert result.chord == "C-s"
        assert result.chord_attempted is True
        assert result.chord_sent is True
        # Ordering: the literal write precedes the chord press.
        kinds = [entry[0] for entry in client.sent]
        assert kinds.index("literal") < kinds.index("chord")
        assert client.sent[-1] == ("chord", "C-s")

    def test_chord_failure_after_text_is_ambiguous(self, journal):
        client = _FakeChordClient()
        result = self._send(journal, client, chord="C-s", chord_error=RuntimeError("boom"))
        assert result.outcome == AMBIGUOUS
        assert result.reason_code == REASON_WRITE_INCOMPLETE
        assert result.chord == "C-s"
        assert result.chord_attempted is True
        # The text reached the pane (literal recorded) but the chord did not land.
        assert result.chord_sent is False
        assert any(entry[0] == "literal" for entry in client.sent)
        assert not any(entry[0] == "chord" for entry in client.sent)
        # The durable record agrees: ambiguous, chord attempted, not sent.
        record = journal.find(CONTROL)
        assert record.state == STATE_AMBIGUOUS
        assert record.chord == "C-s"
        assert record.chord_attempted is True
        assert record.chord_sent is False

    def test_text_write_failure_preserves_requested_chord_facts(self, journal):
        client = _FakeChordClient(literal_error=RuntimeError("literal write failed"))

        result = self._send(journal, client, chord="C-s")

        assert result.outcome == AMBIGUOUS
        assert result.chord == "C-s"
        assert result.chord_attempted is False
        assert result.chord_sent is False
        record = journal.find(CONTROL)
        assert record.chord == "C-s"
        assert record.chord_attempted is False
        assert record.chord_sent is False

    def test_no_chord_skips_the_chord_press(self, journal):
        client = _FakeChordClient()
        result = self._send(journal, client, chord=None, enter=False)
        assert result.outcome == ACCEPTED
        assert result.chord is None
        assert not any(entry[0] == "chord" for entry in client.sent)

    def test_replay_returns_the_journaled_record_with_no_new_io(self, journal):
        client = _FakeChordClient()
        first = self._send(journal, client, chord="C-s")
        assert first.outcome == ACCEPTED
        assert len(client.sent) >= 2  # text + chord were written
        # A lost-response replay queries by control id (the conductor's
        # GET /control-input/{control_id}); the journaled record answers with
        # zero new I/O and the same chord outcome.
        again = service.lookup_control_input(CONTROL, journal=journal)
        assert again.outcome == ACCEPTED
        assert again.chord == "C-s"
        assert again.chord_sent is True
        # No additional writes occurred: the fake recorded only the first send.
        assert len(client.sent) == 2


class TestBoundedWriteDeadline:
    """A hung tmux call is bounded, classified truthfully, and releases the
    lease so a fresh control succeeds exactly once with no late bytes."""

    def _deliver_with(self, monkeypatch, journal, client, **overrides):
        monkeypatch.setattr(service, "_tmux_client", lambda: client)
        monkeypatch.setattr(service, "_terminal_metadata", lambda terminal_id: _metadata())
        monkeypatch.setattr(service, "_managed_identity", lambda terminal_id: None)
        kwargs = {"control_id": CONTROL, "text": TEXT, "enter": True}
        kwargs.update(overrides)
        return service.deliver_control_input(TERMINAL, journal=journal, **kwargs)

    def test_a_post_claim_block_is_cut_off_by_the_absolute_deadline(self, monkeypatch, journal):
        class DeadlineAwareBlockingTmux(FakeTmux):
            def __init__(self):
                super().__init__()
                self.write_calls = 0

            def send_literal_line(
                self,
                pane_id,
                text,
                submit=True,
                *,
                expected_server_identity,
                deadline_monotonic=None,
            ):
                self.write_calls += 1
                remaining = deadline_monotonic - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
                raise subprocess.TimeoutExpired(cmd=["tmux", "send-keys"], timeout=remaining)

        client = DeadlineAwareBlockingTmux()
        production_deadline = service.WRITE_DEADLINE_SECONDS
        monkeypatch.setattr(service, "WRITE_DEADLINE_SECONDS", 0.05)
        started = time.monotonic()
        result = self._deliver_with(monkeypatch, journal, client)
        elapsed = time.monotonic() - started

        assert result.outcome == AMBIGUOUS
        assert result.reason_code == REASON_WRITE_INCOMPLETE
        # The write itself is bounded by WRITE_DEADLINE_SECONDS.  Keep a
        # generous wall-clock allowance for interpreter/coverage and CI
        # scheduling jitter; the assertion is about returning promptly, not
        # enforcing a second timing contract on top of the primitive.
        assert elapsed < 0.30
        assert journal.get(CONTROL).state == service.STATE_AMBIGUOUS
        assert client.write_calls == 1

        # The artificial 50 ms deadline only establishes the bounded-write
        # classification above. Restore the ordinary deadline before this
        # separate probe, whose contract is lease release rather than CI
        # scheduling latency for an otherwise healthy write.
        monkeypatch.setattr(service, "WRITE_DEADLINE_SECONDS", production_deadline)
        # The request returned only after the inline write stopped, so the
        # lease is free and no detached worker can produce a late byte.
        healthy = FakeTmux()
        monkeypatch.setattr(service, "_tmux_client", lambda: healthy)
        fresh = service.deliver_control_input(
            TERMINAL,
            journal=journal,
            control_id="ctl-after-deadline-7d",
            text=TEXT,
            enter=True,
        )
        assert fresh.outcome == ACCEPTED
        assert len(healthy.writes) == 1
        assert client.writes == []

    def test_a_preflight_read_timeout_is_a_reattemptable_write_deadline(self, monkeypatch, journal):
        # The pre-lease resolution reads first and must succeed; the in-lease
        # re-verification preflight is the read that times out.
        client = FakeTmux(
            read_error=subprocess.TimeoutExpired(cmd=["tmux"], timeout=10),
            read_error_after=1,
        )
        result = self._deliver_with(monkeypatch, journal, client)
        assert result.outcome == REFUSED
        assert result.reason_code == service.REASON_WRITE_DEADLINE
        assert result.as_response()["reattemptable"] is True
        # Nothing was written: the timeout was a pre-write read.
        assert client.writes == []

    def test_a_write_call_timeout_is_ambiguous(self, monkeypatch, journal):
        client = FakeTmux(write_error=subprocess.TimeoutExpired(cmd=["tmux"], timeout=10))
        result = self._deliver_with(monkeypatch, journal, client)
        assert result.outcome == AMBIGUOUS
        assert result.reason_code == REASON_WRITE_INCOMPLETE
        # A write timeout is never a reattempt licence.
        assert result.as_response()["reattemptable"] is False

    def test_a_post_claim_server_identity_error_is_durably_ambiguous(self, monkeypatch, journal):
        client = FakeTmux(
            write_error=TmuxServerIdentityError(
                "tmux server identity became unreadable",
                reason_code=REASON_SERVER_IDENTITY_UNREADABLE,
                bound=SOCKET,
                observed=None,
            )
        )

        result = self._deliver_with(monkeypatch, journal, client)

        assert result.outcome == AMBIGUOUS
        assert result.reason_code == REASON_WRITE_INCOMPLETE
        assert REASON_SERVER_IDENTITY_UNREADABLE in result.detail
        record = journal.get(CONTROL)
        assert record.state == STATE_AMBIGUOUS
        assert record.reason_code == REASON_WRITE_INCOMPLETE
        assert service.lookup_control_input(CONTROL, journal=journal).outcome == AMBIGUOUS

    def test_a_hung_write_releases_the_lease_for_a_fresh_control(self, monkeypatch, journal):
        hung = FakeTmux(write_error=subprocess.TimeoutExpired(cmd=["tmux"], timeout=10))
        first = self._deliver_with(monkeypatch, journal, hung)
        assert first.outcome == AMBIGUOUS
        # The lease was released by the bounded timeout: a fresh control id
        # on the same pane delivers exactly once, with no late bytes.
        healthy = FakeTmux()
        monkeypatch.setattr(service, "_tmux_client", lambda: healthy)
        again = service.deliver_control_input(
            TERMINAL, journal=journal, control_id="ctl-fresh-9a", text=TEXT, enter=True
        )
        assert again.outcome == ACCEPTED
        assert len(healthy.writes) == 1
        assert hung.writes == []

    def test_a_write_deadline_refusal_lets_a_clean_retry_succeed(self, monkeypatch, journal):
        stalling = FakeTmux(
            read_error=subprocess.TimeoutExpired(cmd=["tmux"], timeout=10),
            read_error_after=1,
        )
        first = self._deliver_with(monkeypatch, journal, stalling)
        assert first.outcome == REFUSED
        assert first.reason_code == service.REASON_WRITE_DEADLINE
        healthy = FakeTmux()
        monkeypatch.setattr(service, "_tmux_client", lambda: healthy)
        again = service.deliver_control_input(
            TERMINAL, journal=journal, control_id="ctl-retry-7b", text=TEXT, enter=True
        )
        assert again.outcome == ACCEPTED

    def test_the_overall_deadline_is_under_the_conductor_client_default(self):
        from cli_agent_orchestrator.clients.tmux import TMUX_CALL_TIMEOUT_SECONDS

        # The conductor's default client timeout is 30s (mcp_request_timeout);
        # the overall write deadline sits below it, and each call below that.
        assert service.WRITE_DEADLINE_SECONDS < 30
        assert TMUX_CALL_TIMEOUT_SECONDS <= service.WRITE_DEADLINE_SECONDS


class TestChordJournalReplay:
    def test_delivered_chord_round_trips_through_the_record(self, journal):
        digest = _chord_digest()
        journal.open_intent(_chord_binding(digest))
        journal.claim_write(CONTROL)
        journal.mark_delivered(
            CONTROL,
            chunks_sent=1,
            enter_attempted=False,
            chord="C-s",
            chord_attempted=True,
            chord_sent=True,
            evidence_digest=digest,
        )
        record = journal.find(CONTROL)
        assert record.chord == "C-s"
        assert record.chord_attempted is True
        assert record.chord_sent is True
        result = service._from_record(record)
        assert result.chord == "C-s"
        assert result.chord_sent is True
        wire = result.as_response()
        assert wire["chord"] == "C-s"
        assert wire["chord_sent"] is True
        assert wire["chord_attempted"] is True


class TestNativeInboxPayload:
    """The internal generation-bound native inbox payload path.

    Ordinary inbox prose is long and multiline, which the public control
    shape deliberately refuses; the payload path keeps every proof the
    control path makes (identity resolution, live re-read under the shared
    lease, generation re-proof, proven composer plan, identity-bound
    transport) while dropping the control-plane discipline (byte cap,
    single-line rule, control id, chord, journal).  REFUSED — and only
    REFUSED — proves zero bytes reached the pane.
    """

    def _resolved(self, terminal_id="a1b2c3d4"):
        return service.ResolvedControlIdentity(
            terminal_id=terminal_id,
            terminal_incarnation=None,
            terminal_generation="11111111-2222-3333-4444-555555555555",
            provider="kimi_cli",
            native_session_id="ns-1",
            execution_mode="native_tui",
            session_name="cao-payload",
            provider_process_id="4321@marker",
            provider_version="0.29.2",
            pane_id="%1",
            window_id="@1",
            pane_pid=4321,
            pane_dead=False,
            recorded_pane_id="%1",
            bound_server_socket_path="/tmp/payload-sock",
            observed_server_socket_path="/tmp/payload-sock",
        )

    def _wire(self, monkeypatch, resolved, adapter, plan, turn_status=TerminalStatus.IDLE):
        from cli_agent_orchestrator.services import managed_launch_v2

        # Each test models a fresh server lifetime unless it deliberately
        # performs multiple deliveries itself.
        with service._native_kimi_dispatch_guard_lock:
            service._native_kimi_dispatch_times.clear()
        monkeypatch.setattr(service, "resolve_control_identity", lambda tid: resolved)
        live = SimpleNamespace(
            dead=False,
            window_id=resolved.window_id,
            pane_pid=resolved.pane_pid,
            server_socket_path=resolved.bound_server_socket_path,
        )
        # The guard primitives default to "provably not in copy mode": a test
        # that does not model the wheel path sees no exit control, ever.
        client = SimpleNamespace(
            pane_control_identity=lambda *, pane_id, deadline_monotonic: live,
            pane_in_copy_mode=lambda pane_id, **kwargs: False,
            send_copy_mode_cancel=lambda pane_id, **kwargs: True,
        )
        monkeypatch.setattr(service, "_tmux_client", lambda: client)
        monkeypatch.setattr(
            service,
            "_native_composer_preflight",
            lambda *a, **k: (adapter, plan, None) if adapter is not None else (None, None, plan),
        )
        observations = []

        def _observe(provider, **kwargs):
            observations.append((provider, kwargs))
            if isinstance(turn_status, BaseException):
                raise turn_status
            return turn_status

        monkeypatch.setattr(managed_launch_v2, "_observe_turn_state", _observe)
        return observations

    def test_happy_path_types_the_plan_once(self, monkeypatch):
        resolved = self._resolved()
        executed = []

        class _Adapter:
            def execute_composer_plan(self, *, plan, transport, submit, deadline_monotonic):
                executed.append((plan, submit))
                transport.chunks_sent = 5
                transport.enter_attempted = True
                return {"lines_typed": 3, "enter_sent": True}

        adapter = _Adapter()
        plan = {"deliverable": True, "lines": ["a", "b"]}
        self._wire(monkeypatch, resolved, adapter, plan)

        result = service.deliver_native_inbox_payload("a1b2c3d4", text="a\nb")

        assert result.outcome == ACCEPTED
        assert result.enter_sent is True
        assert result.chunks_sent == 5
        assert executed == [(plan, True)]

    def test_stop_barrier_refuses_native_payload_before_adapter_write(self, monkeypatch):
        from cli_agent_orchestrator.services import operation_journal

        resolved = self._resolved()
        executed = []

        class _Adapter:
            def execute_composer_plan(self, **_kwargs):
                executed.append(True)
                pytest.fail("adapter must not receive a post-Stop payload")

        self._wire(monkeypatch, resolved, _Adapter(), {"deliverable": True})
        operation_journal.claim_session_barrier(resolved.session_name, claimed_by="stop-operation")

        result = service.deliver_native_inbox_payload("a1b2c3d4", text="hello")

        assert result.outcome == REFUSED
        assert result.reason_code == "session-effect-barrier"
        assert executed == []

    def test_preflight_refusal_is_zero_bytes(self, monkeypatch):
        resolved = self._resolved()
        self._wire(
            monkeypatch,
            resolved,
            None,
            ("provider-unsupported", "no composer behaviour is proven for this build"),
        )

        result = service.deliver_native_inbox_payload("a1b2c3d4", text="hello")

        assert result.outcome == REFUSED
        assert result.reason_code == "provider-unsupported"
        assert result.chunks_sent == 0

    def test_adapter_interruption_is_ambiguous_never_replayed(self, monkeypatch):
        from cli_agent_orchestrator.services import kimi_native_control as knc

        resolved = self._resolved()

        class _Adapter:
            ComposerWriteInterrupted = knc.ComposerWriteInterrupted

            def execute_composer_plan(self, *, plan, transport, submit, deadline_monotonic):
                transport.chunks_sent = 2
                raise knc.ComposerWriteInterrupted(
                    "transport raised while typing line 2 of 3",
                    enter_attempted=False,
                )

        self._wire(monkeypatch, resolved, _Adapter(), {"deliverable": True})

        result = service.deliver_native_inbox_payload("a1b2c3d4", text="a\nb\nc")

        assert result.outcome == AMBIGUOUS
        assert result.chunks_sent == 2
        assert result.enter_sent is False

    def test_lease_busy_refuses_with_zero_bytes(self, monkeypatch):
        resolved = self._resolved()
        self._wire(monkeypatch, resolved, object(), {"deliverable": True})

        @contextmanager
        def _busy_lease(*a, **k):
            raise service.PaneBusyError("held by another writer")

        monkeypatch.setattr(service, "pane_input_lease", _busy_lease)

        result = service.deliver_native_inbox_payload("a1b2c3d4", text="hello")

        assert result.outcome == REFUSED
        assert result.reason_code == REASON_PANE_BUSY
        assert result.chunks_sent == 0

    def test_idle_observation_admits_the_write_under_the_lease(self, monkeypatch):
        resolved = self._resolved()
        executed = []

        class _Adapter:
            def execute_composer_plan(self, *, plan, transport, submit, deadline_monotonic):
                executed.append(plan)
                transport.chunks_sent = 1
                transport.enter_attempted = True
                return {"lines_typed": 1, "enter_sent": True}

        observations = self._wire(
            monkeypatch, resolved, _Adapter(), {"deliverable": True}, TerminalStatus.IDLE
        )

        result = service.deliver_native_inbox_payload("a1b2c3d4", text="hello")

        assert result.outcome == ACCEPTED
        assert len(executed) == 1
        assert observations == [
            (
                "kimi_cli",
                {
                    "pane_id": resolved.pane_id,
                    "terminal_id": "a1b2c3d4",
                    "session_name": resolved.session_name,
                    "window_name": "managed-a1b2c3d4-111111112222",
                },
            )
        ]

    def test_completed_observation_admits_the_write_under_the_lease(self, monkeypatch):
        """A completed turn is parked at the same input-ready composer as IDLE."""
        resolved = self._resolved()
        executed = []

        class _Adapter:
            def execute_composer_plan(self, *, plan, transport, submit, deadline_monotonic):
                executed.append(plan)
                transport.chunks_sent = 1
                transport.enter_attempted = True

        self._wire(
            monkeypatch,
            resolved,
            _Adapter(),
            {"deliverable": True},
            TerminalStatus.COMPLETED,
        )

        result = service.deliver_native_inbox_payload("a1b2c3d4", text="follow up")

        assert result.outcome == ACCEPTED
        assert result.enter_sent is True
        assert executed == [{"deliverable": True}]

    def test_back_to_back_completed_frame_refuses_second_sender_run(self, monkeypatch):
        """The prior ready frame must not admit another payload after Enter."""
        resolved = self._resolved()
        executed = []

        class _Adapter:
            def execute_composer_plan(self, *, plan, transport, submit, deadline_monotonic):
                executed.append(plan)
                transport.chunks_sent = 1
                transport.enter_attempted = True

        self._wire(
            monkeypatch,
            resolved,
            _Adapter(),
            {"deliverable": True},
            TerminalStatus.COMPLETED,
        )

        first = service.deliver_native_inbox_payload("a1b2c3d4", text="first sender")
        second = service.deliver_native_inbox_payload("a1b2c3d4", text="second sender")

        assert first.outcome == ACCEPTED
        assert second.outcome == REFUSED
        assert second.reason_code == REASON_PANE_BUSY
        assert second.chunks_sent == 0
        assert executed == [{"deliverable": True}]

    def test_dispatch_guard_is_generation_bound_and_expires(self):
        original = self._resolved()
        replacement = self._resolved()
        replacement = replace(
            replacement,
            terminal_generation="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        )
        original_binding = ControlInputBinding(
            request_id="inbox-payload",
            terminal_id=original.terminal_id,
            pane_id=original.pane_id,
            window_id=original.window_id,
            pane_pid=original.pane_pid,
            request_sha256="0" * 64,
            generation=original.terminal_generation,
            server_socket_path=original.bound_server_socket_path,
        )
        replacement_binding = replace(
            original_binding,
            generation=replacement.terminal_generation,
        )
        original_key = service._native_kimi_dispatch_key(original, original_binding)
        replacement_key = service._native_kimi_dispatch_key(replacement, replacement_binding)
        service._mark_native_kimi_dispatch(original_key, now=100.0)

        assert service._native_kimi_dispatch_is_guarded(original_key, now=104.9)
        assert not service._native_kimi_dispatch_is_guarded(replacement_key, now=104.9)
        assert not service._native_kimi_dispatch_is_guarded(original_key, now=105.0)

    def test_non_idle_observation_refuses_with_zero_bytes(self, monkeypatch):
        resolved = self._resolved()
        adapter = MagicMock()
        self._wire(monkeypatch, resolved, adapter, {"deliverable": True}, TerminalStatus.PROCESSING)

        result = service.deliver_native_inbox_payload("a1b2c3d4", text="hello")

        assert result.outcome == REFUSED
        assert result.reason_code == REASON_PANE_BUSY
        assert result.chunks_sent == 0
        adapter.execute_composer_plan.assert_not_called()

    def test_observer_exception_refuses_with_zero_bytes(self, monkeypatch):
        resolved = self._resolved()
        adapter = MagicMock()
        self._wire(
            monkeypatch,
            resolved,
            adapter,
            {"deliverable": True},
            RuntimeError("the pane could not be read"),
        )

        result = service.deliver_native_inbox_payload("a1b2c3d4", text="hello")

        assert result.outcome == REFUSED
        assert result.reason_code == REASON_PANE_BUSY
        assert result.chunks_sent == 0
        adapter.execute_composer_plan.assert_not_called()

    def test_two_runs_reobserve_busy_then_idle_delivers_once(self, monkeypatch):
        from cli_agent_orchestrator.services import managed_launch_v2

        resolved = self._resolved()
        executed = []

        class _Adapter:
            def execute_composer_plan(self, *, plan, transport, submit, deadline_monotonic):
                executed.append(plan)
                transport.chunks_sent = 1
                transport.enter_attempted = True
                return {"lines_typed": 1, "enter_sent": True}

        self._wire(monkeypatch, resolved, _Adapter(), {"deliverable": True})
        statuses = iter([TerminalStatus.PROCESSING, TerminalStatus.IDLE])
        monkeypatch.setattr(
            managed_launch_v2, "_observe_turn_state", lambda provider, **kw: next(statuses)
        )

        busy = service.deliver_native_inbox_payload("a1b2c3d4", text="hello")
        assert busy.outcome == REFUSED
        assert executed == []

        idle = service.deliver_native_inbox_payload("a1b2c3d4", text="hello")
        assert idle.outcome == ACCEPTED
        assert len(executed) == 1


class TestInboxPayloadScreen:
    """The payload byte-safety screen: LF prose passes; every other control
    byte class refuses before any I/O."""

    @pytest.mark.parametrize(
        "bad",
        ["line one\rline two", "esc\x1b[200~paste", "c1\x85byte", "del\x7fbyte", "bell\x07"],
    )
    def test_unsafe_bytes_refuse_before_any_io(self, monkeypatch, bad):
        def _boom(tid):  # resolution must never be reached for a screened payload
            raise AssertionError("resolve called for a screened payload")

        monkeypatch.setattr(service, "resolve_control_identity", _boom)

        result = service.deliver_native_inbox_payload("a1b2c3d4", text=bad)

        assert result.outcome == REFUSED
        assert result.reason_code == REASON_ILLEGAL_CONTROL_BYTES
        assert result.chunks_sent == 0

    def test_empty_payload_refuses_before_any_io(self, monkeypatch):
        def _boom(tid):
            raise AssertionError("resolve called for an empty payload")

        monkeypatch.setattr(service, "resolve_control_identity", _boom)

        result = service.deliver_native_inbox_payload("a1b2c3d4", text="")

        assert result.outcome == REFUSED

    def test_multiline_and_long_prose_passes_the_screen(self):
        paragraph = "ordinary agent prose with detail\n"
        payload = paragraph * 300  # ~10 KB, 300 embedded LFs
        assert service.screen_inbox_payload_text(payload) is None


class TestPublicControlShapeStaysStrict:
    """The public control-input shape is unchanged by the payload path:
    over-512-byte and multiline requests are still rejected."""

    def test_over_512_bytes_still_rejected(self):
        with pytest.raises(service.ControlInputRequestInvalid):
            service.deliver_control_input(
                "deadbeef", control_id="c-shape-1", text="x" * 513, enter=True
            )

    def test_multiline_still_rejected(self):
        result = service.deliver_control_input(
            "deadbeef", control_id="c-shape-2", text="line one\nline two", enter=True
        )
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_MULTILINE_REJECTED


# --- Codex provider-visible submission (cond-0026) ----------------------------


class FakeComposer:
    """The Codex composer as a tiny state machine behind ``capture-pane``.

    Models the one fact the barrier cares about: whether the composer is
    visibly holding the control text.  ``swallow_enter`` reproduces the
    cond-0026 defect — the Enter arrives inside the composer's input-burst
    window and the text simply stays put, with tmux reporting success for
    every write.
    """

    def __init__(self, *, swallow_enter=False):
        self.composed = ""
        self.transcript = [f"transcript row {index}" for index in range(10)]
        self.swallow_enter = swallow_enter

    def on_write(self, text, submit):
        if submit:
            if not self.swallow_enter:
                self.transcript.append(f"› {self.composed}")
                self.composed = ""
        else:
            self.composed += text

    def rows(self):
        return self.transcript + [
            "╭──────────────────────────────────────────────────────╮",
            f"│ > {self.composed}",
            "╰──────────────────────────────────────────────────────╯",
            "  gpt-5.6-terra · 99% context left · ? for shortcuts",
        ]


class CodexFakeTmux(FakeTmux):
    """A FakeTmux that drives the fake composer as its writes land."""

    def __init__(self, composer, **kwargs):
        super().__init__(**kwargs)
        self._composer = composer

    def send_literal_line(
        self,
        pane_id,
        text,
        submit=True,
        *,
        expected_server_identity,
        deadline_monotonic=None,
    ):
        accepted = super().send_literal_line(
            pane_id,
            text,
            submit=submit,
            expected_server_identity=expected_server_identity,
            deadline_monotonic=deadline_monotonic,
        )
        self._composer.on_write(text, submit)
        return accepted


class KimiFakeComposer(FakeComposer):
    """Kimi's pinned prompt box and two status rows."""

    def rows(self):
        return self.transcript + [
            "╭──────────────────────────────────────────────────────╮",
            f"│ > {self.composed}",
            "╰──────────────────────────────────────────────────────╯",
            " auto  K3 thinking: max  …/workspace",
            " context: 17% (172k/1M)",
        ]


@pytest.fixture
def codex(monkeypatch):
    """A Codex terminal whose composer answers capture-pane, and a fast barrier.

    The pinned production bounds (3 s settle, 5 s observation) are right
    for a live composer and wrong for a test clock; the table entry is
    replaced with a tight one, which exercises the same code paths at
    test speed.
    """
    composer = FakeComposer()
    client = CodexFakeTmux(composer)
    monkeypatch.setattr(service, "_tmux_client", lambda: client)
    monkeypatch.setattr(
        service, "_terminal_metadata", lambda terminal_id: _metadata(provider="codex")
    )
    monkeypatch.setattr(service, "_managed_identity", lambda terminal_id: None)
    monkeypatch.setattr(
        native_pane_input,
        "capture_pane_screen",
        lambda pane_id, timeout=10.0: composer.rows(),
    )
    monkeypatch.setitem(
        native_pane_input._SUBMISSION_BARRIERS,
        "codex",
        SubmissionBarrier(
            compose_settle_seconds=0.3,
            post_enter_seconds=0.3,
            poll_interval_seconds=0.01,
            composer_tail_rows=4,
        ),
    )
    return SimpleNamespace(client=client, composer=composer)


@pytest.fixture
def kimi(monkeypatch):
    """A Kimi terminal using the production five-row observation region."""
    composer = KimiFakeComposer()
    client = CodexFakeTmux(composer)
    monkeypatch.setattr(service, "_tmux_client", lambda: client)
    monkeypatch.setattr(
        service, "_terminal_metadata", lambda terminal_id: _metadata(provider="kimi_cli")
    )
    monkeypatch.setattr(service, "_managed_identity", lambda terminal_id: None)
    monkeypatch.setattr(
        native_pane_input,
        "capture_pane_screen",
        lambda pane_id, timeout=10.0: composer.rows(),
    )
    monkeypatch.setitem(
        native_pane_input._SUBMISSION_BARRIERS,
        "kimi_cli",
        SubmissionBarrier(
            compose_settle_seconds=0.3,
            post_enter_seconds=0.3,
            poll_interval_seconds=0.01,
            composer_tail_rows=5,
        ),
    )
    return SimpleNamespace(client=client, composer=composer)


class TestCodexSubmissionBarrier:
    """cond-0026: transport acceptance is not read as submission for Codex."""

    def test_text_and_enter_are_serialized_through_the_composer(self, codex, journal):
        """A1's mechanism: text, compose-visible settle, one Enter, observation."""
        result = _deliver(journal)

        assert result.outcome == ACCEPTED
        assert result.submission_observed == SUBMISSION_SUBMITTED
        assert result.submission_evidence_ref is not None
        assert result.submission_evidence_ref.startswith(f"capture-pane:{PANE}:")
        assert result.text_sent is True and result.enter_sent is True
        # Exactly one text write and exactly one Enter, in that order, and
        # never a fused text+Enter burst.
        assert codex.client.writes == [
            {
                "pane_id": PANE,
                "text": TEXT,
                "submit": False,
                "expected_server_identity": SOCKET,
            },
            {
                "pane_id": PANE,
                "text": "",
                "submit": True,
                "expected_server_identity": SOCKET,
            },
        ]
        record = journal.get(CONTROL)
        assert record.state == DELIVERED
        assert record.submission_observed == SUBMISSION_SUBMITTED
        assert record.submission_evidence_ref == result.submission_evidence_ref
        # The response states plainly what was and was not proven.
        assert "not provider completion" in result.detail

    def test_a_swallowed_enter_is_ambiguous_and_never_re_sent(self, codex, journal):
        """A3: the defect itself — submission unproven, no second Enter."""
        codex.composer.swallow_enter = True
        result = _deliver(journal)

        assert result.outcome == AMBIGUOUS
        assert result.reason_code == "submission-unproven"
        assert result.submission_observed == SUBMISSION_UNSUBMITTED
        assert result.submission_evidence_ref is not None
        # One text write, one Enter, full stop.  The blind second Enter
        # that would double-submit is exactly what must never happen.
        assert [write["submit"] for write in codex.client.writes] == [False, True]
        record = journal.get(CONTROL)
        assert record.state == STATE_AMBIGUOUS
        assert record.enter_attempted is True
        assert record.submission_observed == SUBMISSION_UNSUBMITTED

    def test_text_that_never_composes_withholds_the_enter(self, codex, journal):
        """A3: the barrier's Enter fires only at a proven-composed control."""
        codex.composer.rows = lambda: ["nothing on screen"] * 14
        result = _deliver(journal)

        assert result.outcome == AMBIGUOUS
        assert result.reason_code == "submission-unproven"
        assert result.submission_observed == SUBMISSION_UNKNOWN
        assert result.enter_attempted is False
        # The text write landed; zero Enters were sent.
        assert [write["submit"] for write in codex.client.writes] == [False]
        record = journal.get(CONTROL)
        assert record.state == STATE_AMBIGUOUS
        assert record.enter_attempted is False
        assert record.submission_observed == SUBMISSION_UNKNOWN

    def test_blindness_after_the_enter_is_unknown_never_submitted(
        self, codex, journal, monkeypatch
    ):
        """A3: a post-Enter observation that cannot be made proves nothing."""
        calls = {"count": 0}

        def flaky_capture(pane_id, timeout=10.0):
            calls["count"] += 1
            if calls["count"] == 1:
                return codex.composer.rows()
            raise NativePaneInputUnavailable("pane unreadable")

        # Settle succeeds against the real composer; every post-Enter
        # capture then fails, so there is no observation to classify.
        monkeypatch.setattr(native_pane_input, "capture_pane_screen", flaky_capture)

        result = _deliver(journal)

        assert result.outcome == AMBIGUOUS
        assert result.reason_code == "submission-unproven"
        assert result.submission_observed == SUBMISSION_UNKNOWN
        assert result.submission_evidence_ref is None
        assert [write["submit"] for write in codex.client.writes] == [False, True]

    def test_an_ambiguous_retry_replays_the_stored_record_with_zero_new_io(self, codex, journal):
        """A6a: an ambiguous record never gains new I/O on retry."""
        codex.composer.swallow_enter = True
        first = _deliver(journal)
        assert first.outcome == AMBIGUOUS
        writes_after_first = len(codex.client.writes)
        events_after_first = len(journal.get(CONTROL).events)

        replayed = _deliver(journal)

        assert replayed.outcome == AMBIGUOUS
        assert replayed.reason_code == "submission-unproven"
        assert replayed.submission_observed == SUBMISSION_UNSUBMITTED
        assert replayed.submission_evidence_ref == first.submission_evidence_ref
        assert len(codex.client.writes) == writes_after_first
        assert len(journal.get(CONTROL).events) == events_after_first

    def test_a_delivered_retry_replays_the_stored_observation_verbatim(self, codex, journal):
        """A6a: a delivered record replays outcome plus observation, no new I/O."""
        first = _deliver(journal)
        assert first.outcome == ACCEPTED
        writes_after_first = len(codex.client.writes)

        replayed = _deliver(journal)

        assert replayed.outcome == ACCEPTED
        assert replayed.submission_observed == SUBMISSION_SUBMITTED
        assert replayed.submission_evidence_ref == first.submission_evidence_ref
        assert len(codex.client.writes) == writes_after_first

    def test_a_lost_response_is_answered_with_the_stored_observation(self, codex, journal):
        """A6a: the exact-id lookup reports the observation, not a guess."""
        first = _deliver(journal)
        assert first.outcome == ACCEPTED
        writes_after_first = len(codex.client.writes)

        answer = service.lookup_control_input(CONTROL, journal=journal)

        assert answer.outcome == ACCEPTED
        assert answer.submission_observed == SUBMISSION_SUBMITTED
        assert answer.submission_evidence_ref == first.submission_evidence_ref
        assert len(codex.client.writes) == writes_after_first

    def test_a_crash_mid_write_replays_ambiguous_with_a_typed_null_observation(
        self, codex, journal, db_path
    ):
        """A6a: crash window — the sweep's ambiguous record invents nothing."""
        dead = ControlInputJournal(db_path, owner_pid=_dead_pid(), owner_token="prev")
        dead.open_intent(
            ControlInputBinding(
                request_id=CONTROL,
                terminal_id=TERMINAL,
                pane_id=PANE,
                window_id=WINDOW,
                pane_pid=PANE_PID,
                request_sha256=control_input_request_digest(
                    control_id=CONTROL, text=TEXT, enter=True, expected_identity=None
                ),
                generation=GENERATION,
                server_socket_path=SOCKET,
            )
        )
        dead.claim_write(CONTROL)

        result = _deliver(journal)

        assert result.outcome == AMBIGUOUS
        assert result.reason_code == "owner-lost-mid-write"
        assert result.submission_observed is None
        assert result.submission_evidence_ref is None
        # The replay drove no new I/O of any kind.
        assert codex.client.writes == []
        # And a further identical retry replays that terminal record.
        again = _deliver(journal)
        assert again.outcome == AMBIGUOUS
        assert again.submission_observed is None
        assert codex.client.writes == []

    def test_a_refused_record_reattempts_and_then_replays_terminally(self, codex, journal):
        """A6b: refused -> intent reattempt, then terminal stored-row replay."""
        with _pane_held_elsewhere():
            refused = _deliver(journal)
        assert refused.outcome == REFUSED
        assert refused.reason_code == "pane-busy"
        assert refused.submission_observed is None
        assert codex.client.writes == []

        delivered = _deliver(journal)
        assert delivered.outcome == ACCEPTED
        assert delivered.submission_observed == SUBMISSION_SUBMITTED
        writes_after_delivery = len(codex.client.writes)
        assert writes_after_delivery == 2

        replayed = _deliver(journal)
        assert replayed.outcome == ACCEPTED
        assert replayed.submission_observed == SUBMISSION_SUBMITTED
        assert replayed.submission_evidence_ref == delivered.submission_evidence_ref
        assert len(codex.client.writes) == writes_after_delivery
        assert [event["to_state"] for event in journal.get(CONTROL).events] == [
            "intent",
            "refused",
            "intent",
            "writing",
            "delivered",
        ]

    def test_a_stale_identity_is_refused_before_any_write(self, codex, journal):
        """Stale identity: the barrier is never reached, zero bytes as ever."""
        result = _deliver(journal, expected_identity={"terminal_generation": "gen-old"})

        assert result.outcome == REFUSED
        assert result.reason_code == "stale-generation"
        assert result.submission_observed is None
        assert codex.client.writes == []

    def test_enter_false_needs_no_barrier_and_records_no_observation(self, codex, journal):
        """No Enter is requested, so there is nothing to observe."""
        result = _deliver(journal, enter=False)

        assert result.outcome == ACCEPTED
        assert result.submission_observed is None
        assert result.submission_evidence_ref is None
        assert [write["submit"] for write in codex.client.writes] == [False]

    def test_a_non_codex_provider_keeps_the_fused_write(self, tmux, journal):
        """A4: an unpinned provider keeps the fused-write behavior."""
        result = _deliver(journal)

        assert result.outcome == ACCEPTED
        assert result.submission_observed is None
        assert result.as_response()["submission_observed"] is None
        assert result.as_response()["submission_evidence_ref"] is None
        # One fused text+Enter write, exactly as before cond-0026.
        assert [write["submit"] for write in tmux.writes] == [True]


class TestKimiSubmissionBarrier:
    """A retained Kimi round gets provider-visible submission truth."""

    def test_kimi_reports_one_observed_submission(self, kimi, journal):
        result = _deliver(journal)

        assert result.outcome == ACCEPTED
        assert result.submission_observed == SUBMISSION_SUBMITTED
        assert result.submission_evidence_ref is not None
        assert [write["submit"] for write in kimi.client.writes] == [False, True]
        assert journal.get(CONTROL).submission_observed == SUBMISSION_SUBMITTED

    def test_kimi_swallowed_enter_is_ambiguous_and_not_replayed(self, kimi, journal):
        kimi.composer.swallow_enter = True

        first = _deliver(journal)
        writes_after_first = len(kimi.client.writes)
        replayed = _deliver(journal)

        assert first.outcome == AMBIGUOUS
        assert first.submission_observed == SUBMISSION_UNSUBMITTED
        assert replayed.submission_observed == SUBMISSION_UNSUBMITTED
        assert len(kimi.client.writes) == writes_after_first == 2


# --- Schema v3: ordered structured event sequences ---------------------------

from cli_agent_orchestrator.services.control_input_contract import (  # noqa: E402
    REASON_UNREPRESENTABLE_EVENT,
    REASON_UNSUPPORTED_CHORD,
    REASON_UNSUPPORTED_KEY,
    control_input_request_digest_v3,
)

SEQ_EVENTS = [
    {"type": "text", "text": "make it so, + \\"},
    {"type": "key", "key": "Enter"},
]


def _deliver_sequence(journal, events=SEQ_EVENTS, **overrides):
    kwargs = {"control_id": CONTROL, "events": events}
    kwargs.update(overrides)
    return service.deliver_control_input(TERMINAL, journal=journal, **kwargs)


def _seq_resolved(**overrides):
    fields = {
        "terminal_id": TERMINAL,
        "terminal_incarnation": None,
        "terminal_generation": "11111111-2222-3333-4444-555555555555",
        "provider": "kimi_cli",
        "native_session_id": "sess-1",
        "execution_mode": service.EXECUTION_MODE_NATIVE_TUI,
        "session_name": "cao",
        "provider_process_id": "4242@boot-1",
        "provider_version": "0.29.0",
        "pane_id": PANE,
        "window_id": WINDOW,
        "pane_pid": PANE_PID,
        "pane_dead": False,
        "managed": True,
        "recorded_pane_id": PANE,
        "bound_server_socket_path": SOCKET,
        "observed_server_socket_path": SOCKET,
    }
    fields.update(overrides)
    return service.ResolvedControlIdentity(**fields)


class TestSequenceShape:
    def test_events_and_text_is_never_both(self, journal):
        with pytest.raises(service.ControlInputRequestInvalid):
            _deliver_sequence(journal, text="hello")

    def test_events_and_enter_is_never_both(self, journal):
        with pytest.raises(service.ControlInputRequestInvalid):
            _deliver_sequence(journal, enter=True)

    def test_events_and_chord_is_never_both(self, journal):
        with pytest.raises(service.ControlInputRequestInvalid):
            _deliver_sequence(journal, chord="C-s")

    def test_empty_sequence_is_no_control(self, journal):
        with pytest.raises(service.ControlInputRequestInvalid):
            _deliver_sequence(journal, events=[])

    def test_thirty_three_events_is_over_the_cap(self, journal):
        with pytest.raises(service.ControlInputRequestInvalid):
            _deliver_sequence(journal, events=[{"type": "key", "key": "Escape"}] * 33)

    def test_aggregate_text_bytes_are_capped(self, journal):
        events = [{"type": "text", "text": "a" * 300}, {"type": "text", "text": "b" * 300}]
        with pytest.raises(service.ControlInputRequestInvalid):
            _deliver_sequence(journal, events=events)

    def test_unknown_type_with_fields_is_a_shape_error(self, journal):
        with pytest.raises(service.ControlInputRequestInvalid):
            _deliver_sequence(journal, events=[{"type": "macro", "name": "x"}])

    def test_v1_omitted_enter_keeps_the_wire_default(self, tmux, journal):
        result = service.deliver_control_input(
            TERMINAL, control_id=CONTROL, text=TEXT, journal=journal
        )
        assert result.outcome == ACCEPTED
        assert tmux.writes[-1]["submit"] is True

    def test_an_explicit_null_enter_is_not_the_omission(self, tmux, journal):
        """The API edge marks a stated JSON ``"enter": null`` with
        :data:`ENTER_EXPLICIT_NULL`: it failed validation at F1 (a
        non-Optional bool) and must not silently become the v1 default."""
        with pytest.raises(service.ControlInputRequestInvalid):
            _deliver(journal, enter=service.ENTER_EXPLICIT_NULL)
        assert tmux.writes == []

    def test_a_stated_null_enter_beside_events_is_never_both(self, journal):
        """The v3 either/or rule stays strict on stated fields: a nulled
        ``enter`` beside ``events`` is a stated v1/v2 field, refused as
        ambiguous intent rather than resolved by precedence."""
        with pytest.raises(service.ControlInputRequestInvalid):
            _deliver_sequence(journal, enter=service.ENTER_EXPLICIT_NULL)


class TestSequenceDeliveryUnmanaged:
    """The generic literal sink: ordering, coalescing, and honest outcomes."""

    def test_text_then_enter_is_one_exact_write(self, tmux, journal):
        result = _deliver_sequence(journal)
        assert result.outcome == ACCEPTED
        assert result.request_schema_version == 3
        # The text+Enter pair is the exact v1 write: one call, one Enter.
        assert tmux.writes == [
            {
                "pane_id": PANE,
                "text": "make it so, + \\",
                "submit": True,
                "expected_server_identity": SOCKET,
            }
        ]
        assert result.text_sent is True
        assert result.enter_sent is True
        assert [event["outcome"] for event in result.events] == ["sent", "sent"]
        assert result.events[0]["ordinal"] == 0
        assert result.events[1] == {"ordinal": 1, "type": "key", "key": "Enter", "outcome": "sent"}

    def test_ordered_mixed_events_write_in_order(self, tmux, journal):
        events = [
            {"type": "key", "key": "Escape"},
            {"type": "text", "text": "a, b + c\\d"},
            {"type": "key", "key": "Backspace"},
            {"type": "key", "key": "C-c"},
            {"type": "key", "key": "C-s"},
            {"type": "key", "key": "Enter"},
        ]
        result = _deliver_sequence(journal, events=events)
        assert result.outcome == ACCEPTED
        kinds = []
        for write in tmux.writes:
            if "text" in write:
                kinds.append(("literal", write["text"], write["submit"]))
            elif "key" in write:
                kinds.append(("key", write["key"]))
            else:
                kinds.append(("chord", write["chord"]))
        assert kinds == [
            ("key", "Escape"),
            ("literal", "a, b + c\\d", False),  # comma/plus/backslash are ordinary text
            ("key", "Backspace"),
            ("key", "C-c"),
            ("key", "C-s"),
            ("literal", "", True),  # bare Enter is the submitting key on its own
        ]
        assert [event["outcome"] for event in result.events] == ["sent"] * 6

    def test_bare_enter_without_text_is_sendable(self, tmux, journal):
        result = _deliver_sequence(journal, events=[{"type": "key", "key": "Enter"}])
        assert result.outcome == ACCEPTED
        assert tmux.writes[0]["text"] == "" and tmux.writes[0]["submit"] is True
        assert result.enter_sent is True
        assert result.text_sent is False

    def test_unsupported_key_is_a_zero_byte_refusal(self, tmux, journal):
        result = _deliver_sequence(journal, events=[{"type": "key", "key": "M-x"}])
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_UNSUPPORTED_KEY
        assert tmux.writes == []
        assert journal.find(CONTROL) is None  # refused before the intent
        assert [event["outcome"] for event in result.events] == ["refused"]

    def test_unrepresentable_event_is_a_zero_byte_refusal(self, tmux, journal):
        result = _deliver_sequence(journal, events=[{"type": "macro"}])
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_UNREPRESENTABLE_EVENT
        assert tmux.writes == []
        assert [event["outcome"] for event in result.events] == ["refused"]

    def test_modifier_combinations_are_refused_never_approximated(self, tmux, journal):
        for index, key in enumerate(("C-M-x", "M-Tab", "S-Enter", "C-c C-c")):
            result = _deliver_sequence(
                journal, events=[{"type": "key", "key": key}], control_id=f"ctl-mod-{index}"
            )
            assert result.outcome == REFUSED, key
            assert result.reason_code == REASON_UNSUPPORTED_KEY, key
        assert tmux.writes == []

    def test_chord_event_reuses_the_provider_pin(self, tmux, journal):
        # The fixture provider is "claude-code", which has no native control
        # adapter, so the chord is the zero-byte refusal the v2 table gives.
        result = _deliver_sequence(journal, events=[{"type": "chord", "chord": "C-s"}])
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_UNSUPPORTED_CHORD
        assert tmux.writes == []

    def test_mid_sequence_failure_is_ambiguous_with_the_event_boundary(self, journal):
        calls = {"count": 0}

        def fail_on_second():
            calls["count"] += 1
            if calls["count"] == 2:
                raise TmuxLiteralSendError("tmux went away", chunks_sent=0, enter_attempted=False)

        client = FakeTmux(on_write=fail_on_second)
        events = [
            {"type": "text", "text": "typed first"},
            {"type": "key", "key": "Escape"},
            {"type": "key", "key": "C-c"},
        ]
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(service, "_tmux_client", lambda: client)
            mp.setattr(service, "_terminal_metadata", lambda terminal_id: _metadata())
            mp.setattr(service, "_managed_identity", lambda terminal_id: None)
            result = _deliver_sequence(journal, events=events)
        assert result.outcome == AMBIGUOUS
        assert result.reason_code == REASON_WRITE_INCOMPLETE
        outcomes = [(event["type"], event.get("key"), event["outcome"]) for event in result.events]
        assert outcomes == [
            ("text", None, "sent"),
            ("key", "Escape", "attempted"),
            ("key", "C-c", "skipped"),
        ]
        record = journal.find(CONTROL)
        assert record.state == STATE_AMBIGUOUS

        # A possibly-partial write is never auto-replayed: an identical retry
        # is answered from the stored row with zero new I/O.
        writes_before = len(client.writes)
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(service, "_tmux_client", lambda: client)
            mp.setattr(service, "_terminal_metadata", lambda terminal_id: _metadata())
            mp.setattr(service, "_managed_identity", lambda terminal_id: None)
            replay = _deliver_sequence(journal, events=events)
        assert replay.outcome == AMBIGUOUS
        assert replay.request_schema_version == 3
        assert len(client.writes) == writes_before

    def test_pane_busy_refusal_rearms_and_then_replays_terminally(self, journal):
        client = FakeTmux()
        events = [{"type": "text", "text": "busy test"}]
        with _pane_held_elsewhere():
            with pytest.MonkeyPatch().context() as mp:
                mp.setattr(service, "_tmux_client", lambda: client)
                mp.setattr(service, "_terminal_metadata", lambda terminal_id: _metadata())
                mp.setattr(service, "_managed_identity", lambda terminal_id: None)
                refused = _deliver_sequence(journal, events=events)
        assert refused.outcome == REFUSED
        assert refused.reason_code == REASON_PANE_BUSY
        assert [event["outcome"] for event in refused.events] == ["refused"]

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(service, "_tmux_client", lambda: client)
            mp.setattr(service, "_terminal_metadata", lambda terminal_id: _metadata())
            mp.setattr(service, "_managed_identity", lambda terminal_id: None)
            delivered = _deliver_sequence(journal, events=events)
            assert delivered.outcome == ACCEPTED
            writes_after_delivery = len(client.writes)
            replay = _deliver_sequence(journal, events=events)
        assert replay.outcome == ACCEPTED
        assert replay.request_schema_version == 3
        assert len(client.writes) == writes_after_delivery  # terminal replay: zero new I/O


class TestSequenceIntentPolicy:
    @pytest.mark.parametrize(
        "event,intent",
        [
            ({"type": "text", "text": "x"}, service.INTENT_COMPOSER),
            ({"type": "key", "key": "Enter"}, service.INTENT_COMPOSER),
            ({"type": "key", "key": "Backspace"}, service.INTENT_COMPOSER),
            ({"type": "key", "key": "Escape"}, service.INTENT_INTERRUPT),
            ({"type": "key", "key": "C-c"}, service.INTENT_INTERRUPT),
            ({"type": "key", "key": "C-s"}, service.INTENT_INTERRUPT),
            ({"type": "chord", "chord": "C-s"}, service.INTENT_INTERRUPT),
            # §3.2, pinned: every one of the eleven new navigation/editing
            # keys is composer-class — idle-gated like any composer write,
            # because no per-provider evidence supports exempting them.
            ({"type": "key", "key": "Up"}, service.INTENT_COMPOSER),
            ({"type": "key", "key": "Down"}, service.INTENT_COMPOSER),
            ({"type": "key", "key": "Left"}, service.INTENT_COMPOSER),
            ({"type": "key", "key": "Right"}, service.INTENT_COMPOSER),
            ({"type": "key", "key": "Home"}, service.INTENT_COMPOSER),
            ({"type": "key", "key": "End"}, service.INTENT_COMPOSER),
            ({"type": "key", "key": "PageUp"}, service.INTENT_COMPOSER),
            ({"type": "key", "key": "PageDown"}, service.INTENT_COMPOSER),
            ({"type": "key", "key": "Delete"}, service.INTENT_COMPOSER),
            ({"type": "key", "key": "Insert"}, service.INTENT_COMPOSER),
            ({"type": "key", "key": "Tab"}, service.INTENT_COMPOSER),
        ],
    )
    def test_every_event_form_has_one_class(self, event, intent):
        assert service._sequence_event_intent(event) == intent

    @pytest.mark.parametrize(
        "events,gated",
        [
            ([{"type": "key", "key": "Escape"}], False),
            ([{"type": "key", "key": "Escape"}, {"type": "key", "key": "C-c"}], False),
            ([{"type": "chord", "chord": "C-s"}], False),
            ([{"type": "text", "text": "x"}], True),
            ([{"type": "key", "key": "Enter"}], True),
            ([{"type": "key", "key": "Backspace"}], True),
            # A mixed sequence is gated as a whole.
            ([{"type": "key", "key": "Escape"}, {"type": "text", "text": "x"}], True),
        ],
    )
    def test_sequence_gate_covers_any_composer_event(self, events, gated):
        assert service._sequence_is_readiness_gated(events) is gated


class _FakeSequenceAdapter:
    """An adapter that executes plans through the transport, recording calls."""

    class ComposerWriteInterrupted(Exception):
        def __init__(self, detail, *, enter_attempted=False):
            super().__init__(detail)
            self.detail = detail
            self.enter_attempted = enter_attempted

    def __init__(self, *, raise_with=None):
        self.calls = []
        self.submit_calls = []
        self._raise_with = raise_with

    def execute_composer_plan(self, *, plan, transport, submit, deadline_monotonic=None):
        self.calls.append((plan, submit))
        transport.send_literal(plan["lines"][0])
        if submit:
            transport.send_enter()
        if self._raise_with is not None:
            raise self._raise_with

    def submit_composer_plan(self, *, plan, transport, deadline_monotonic=None):
        self.submit_calls.append(plan)
        transport.send_enter()


class TestSequenceManagedAdapter:
    """The managed native path: plans, the proven submit sequence, ordering."""

    def _send(
        self,
        journal,
        client,
        events,
        *,
        adapter=None,
        dispatch_key=None,
        submission_barrier=None,
    ):
        adapter = adapter or _FakeSequenceAdapter()
        plans = {
            index: {"lines": [event["text"]]}
            for index, event in enumerate(events)
            if event["type"] == "text"
        }
        digest = control_input_request_digest_v3(
            control_id=CONTROL, events=events, expected_identity=None
        )
        binding = _chord_binding(digest)
        # The caller opens intent and claims the write before this runs.
        journal.open_intent(binding)
        journal.claim_write(CONTROL)
        return adapter, service._send_sequence_through_native_adapter(
            journal,
            client,
            binding,
            adapter=adapter,
            plans=plans,
            events=events,
            terminal_id=TERMINAL,
            resolved=_chord_resolved(),
            digest=digest,
            deadline_monotonic=time.monotonic() + service.WRITE_DEADLINE_SECONDS,
            dispatch_key=dispatch_key,
            submission_barrier=submission_barrier,
        )

    def test_text_then_enter_submits_through_the_adapter_once(self, journal):
        client = _FakeChordClient()
        events = [{"type": "text", "text": "hello"}, {"type": "key", "key": "Enter"}]
        adapter, result = self._send(journal, client, events)
        assert result.outcome == ACCEPTED
        # One plan executed with submit=True: the adapter's proven submit
        # sequence is the only submit mechanism — no second blind Enter.
        assert adapter.calls == [({"lines": ["hello"]}, True)]
        assert [event["outcome"] for event in result.events] == ["sent", "sent"]
        assert result.enter_sent is True
        record = journal.find(CONTROL)
        assert record.state == DELIVERED
        assert record.enter_attempted is True

    def test_managed_kimi_reports_provider_visible_submission(self, kimi, journal):
        events = [
            {"type": "text", "text": "continue retained task"},
            {"type": "key", "key": "Enter"},
        ]
        adapter, result = self._send(
            journal,
            kimi.client,
            events,
            submission_barrier=native_pane_input.submission_barrier_for("kimi_cli"),
        )

        assert result.outcome == ACCEPTED
        assert result.submission_observed == SUBMISSION_SUBMITTED
        assert result.submission_evidence_ref is not None
        assert adapter.calls == [({"lines": ["continue retained task"]}, False)]
        assert adapter.submit_calls == [{"lines": ["continue retained task"]}]
        assert [write["submit"] for write in kimi.client.writes] == [False, True]
        assert journal.find(CONTROL).submission_observed == SUBMISSION_SUBMITTED

    def test_managed_kimi_swallowed_enter_is_ambiguous_without_replay(self, kimi, journal):
        kimi.composer.swallow_enter = True
        events = [
            {"type": "text", "text": "continue retained task"},
            {"type": "key", "key": "Enter"},
        ]
        _, result = self._send(
            journal,
            kimi.client,
            events,
            submission_barrier=native_pane_input.submission_barrier_for("kimi_cli"),
        )
        writes_after_first = len(kimi.client.writes)
        replayed = service.lookup_control_input(CONTROL, journal=journal)

        assert result.outcome == AMBIGUOUS
        assert result.submission_observed == SUBMISSION_UNSUBMITTED
        assert replayed.submission_observed == SUBMISSION_UNSUBMITTED
        assert len(kimi.client.writes) == writes_after_first == 2

    def test_bare_enter_is_the_named_key_not_a_plan(self, journal):
        client = _FakeChordClient()
        adapter, result = self._send(journal, client, [{"type": "key", "key": "Enter"}])
        assert result.outcome == ACCEPTED
        assert adapter.calls == []  # no composer plan for a bare key
        assert ("literal", "", True) in client.sent

    def test_ordering_across_text_keys_and_chords(self, journal):
        client = _FakeChordClient()
        events = [
            {"type": "key", "key": "Escape"},
            {"type": "text", "text": "one"},
            {"type": "key", "key": "C-c"},
            {"type": "text", "text": "two"},
            {"type": "key", "key": "Enter"},
            {"type": "chord", "chord": "C-s"},
        ]
        adapter, result = self._send(journal, client, events)
        assert result.outcome == ACCEPTED
        kinds = [entry[0] for entry in client.sent]
        assert kinds == ["key", "literal", "key", "literal", "literal", "chord"]
        assert client.sent[0] == ("key", "Escape")
        assert client.sent[2] == ("key", "C-c")
        # The second text carried the Enter with it (submit=True): the text
        # literal precedes the submitting Enter literal, both inside the
        # adapter's one proven submit sequence.
        assert client.sent[3] == ("literal", "two", False)
        assert client.sent[4] == ("literal", "", True)
        assert adapter.calls == [({"lines": ["one"]}, False), ({"lines": ["two"]}, True)]
        assert [event["outcome"] for event in result.events] == ["sent"] * 6

    def test_ordering_across_text_navigation_and_chords(self, journal):
        """The §3.2 extension rides the same ordering: text, navigation
        keys, and a chord land in sequence order with per-event outcomes."""
        client = _FakeChordClient()
        events = [
            {"type": "text", "text": "/model"},
            {"type": "key", "key": "Up"},
            {"type": "key", "key": "Up"},
            {"type": "key", "key": "PageDown"},
            {"type": "key", "key": "Enter"},
            {"type": "chord", "chord": "C-s"},
        ]
        adapter, result = self._send(journal, client, events)
        assert result.outcome == ACCEPTED
        kinds = [entry[0] for entry in client.sent]
        assert kinds == ["literal", "key", "key", "key", "literal", "chord"]
        assert client.sent[1] == ("key", "Up")
        assert client.sent[2] == ("key", "Up")
        assert client.sent[3] == ("key", "PageDown")
        assert [event["outcome"] for event in result.events] == ["sent"] * 6

    def test_interrupted_text_leaves_the_tail_skipped(self, journal):
        client = _FakeChordClient()
        adapter = _FakeSequenceAdapter(
            raise_with=_FakeSequenceAdapter.ComposerWriteInterrupted(
                "transport died mid-line", enter_attempted=False
            )
        )
        events = [
            {"type": "text", "text": "partial"},
            {"type": "key", "key": "Escape"},
            {"type": "chord", "chord": "C-s"},
        ]
        _, result = self._send(journal, client, events, adapter=adapter)
        assert result.outcome == AMBIGUOUS
        outcomes = [event["outcome"] for event in result.events]
        assert outcomes == ["attempted", "skipped", "skipped"]
        record = journal.find(CONTROL)
        assert record.state == STATE_AMBIGUOUS
        # The ambiguous record never gains new I/O on a lookup-replay.
        again = service.lookup_control_input(CONTROL, journal=journal)
        assert again.outcome == AMBIGUOUS

    def test_enter_attempted_marks_text_sent_and_enter_attempted(self, journal):
        client = _FakeChordClient()
        interrupted = _FakeSequenceAdapter.ComposerWriteInterrupted(
            "the Enter raised", enter_attempted=True
        )

        class _Adapter(_FakeSequenceAdapter):
            def execute_composer_plan(self, *, plan, transport, submit, deadline_monotonic=None):
                transport.send_literal(plan["lines"][0])
                transport.enter_attempted = True
                raise interrupted

        events = [{"type": "text", "text": "typed"}, {"type": "key", "key": "Enter"}]
        _, result = self._send(journal, client, events, adapter=_Adapter())
        assert result.outcome == AMBIGUOUS
        assert [event["outcome"] for event in result.events] == ["sent", "attempted"]


class TestSequenceReadinessGate:
    """The per-event intent policy at the managed boundary (§5.3)."""

    def _wire(self, monkeypatch, resolved, adapter, plans, turn_status=TerminalStatus.IDLE):
        from cli_agent_orchestrator.services import managed_launch_v2

        with service._native_kimi_dispatch_guard_lock:
            service._native_kimi_dispatch_times.clear()
        monkeypatch.setattr(service, "resolve_control_identity", lambda tid: resolved)
        client = FakeTmux(
            identities=[
                FakePaneIdentity(
                    pane_id=resolved.pane_id,
                    window_id=resolved.window_id,
                    pane_pid=resolved.pane_pid,
                )
            ]
        )
        monkeypatch.setattr(service, "_tmux_client", lambda: client)
        monkeypatch.setattr(
            service,
            "_native_sequence_preflight",
            lambda *a, **k: (adapter, plans, None),
        )

        def _observe(provider, **kwargs):
            if isinstance(turn_status, BaseException):
                raise turn_status
            return turn_status

        monkeypatch.setattr(managed_launch_v2, "_observe_turn_state", _observe)
        return client

    def test_composer_sequence_is_idle_gated(self, monkeypatch, journal):
        resolved = _seq_resolved()
        client = self._wire(
            monkeypatch,
            resolved,
            _FakeSequenceAdapter(),
            {0: {"lines": ["hello"]}},
            turn_status=TerminalStatus.PROCESSING,
        )
        result = _deliver_sequence(journal, events=[{"type": "text", "text": "hello"}])
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_PANE_BUSY
        assert client.writes == []
        assert [event["outcome"] for event in result.events] == ["refused"]

    def test_interrupt_sequence_is_deliverable_during_an_active_turn(self, monkeypatch, journal):
        resolved = _seq_resolved()
        client = self._wire(
            monkeypatch,
            resolved,
            _FakeSequenceAdapter(),
            {},
            turn_status=TerminalStatus.PROCESSING,
        )
        events = [{"type": "key", "key": "Escape"}, {"type": "key", "key": "C-c"}]
        result = _deliver_sequence(journal, events=events)
        assert result.outcome == ACCEPTED
        assert [write["key"] for write in client.writes] == ["Escape", "C-c"]

    def test_chord_sequence_is_deliverable_during_an_active_turn(self, monkeypatch, journal):
        resolved = _seq_resolved()
        client = self._wire(
            monkeypatch,
            resolved,
            _FakeSequenceAdapter(),
            {},
            turn_status=TerminalStatus.PROCESSING,
        )
        result = _deliver_sequence(journal, events=[{"type": "chord", "chord": "C-s"}])
        assert result.outcome == ACCEPTED
        assert client.writes == [
            {"pane_id": PANE, "chord": "C-s", "expected_server_identity": SOCKET}
        ]

    def test_a_mixed_sequence_is_gated_as_a_whole(self, monkeypatch, journal):
        resolved = _seq_resolved()
        client = self._wire(
            monkeypatch,
            resolved,
            _FakeSequenceAdapter(),
            {1: {"lines": ["hello"]}},
            turn_status=TerminalStatus.PROCESSING,
        )
        events = [{"type": "key", "key": "Escape"}, {"type": "text", "text": "hello"}]
        result = _deliver_sequence(journal, events=events)
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_PANE_BUSY
        assert client.writes == []

    def test_idle_composer_sequence_delivers(self, monkeypatch, journal):
        resolved = _seq_resolved()
        client = self._wire(
            monkeypatch,
            resolved,
            _FakeSequenceAdapter(),
            {0: {"lines": ["hello"]}},
            turn_status=TerminalStatus.IDLE,
        )
        result = _deliver_sequence(journal, events=[{"type": "text", "text": "hello"}])
        assert result.outcome == ACCEPTED
        assert len(client.writes) == 1

    def test_unobservable_turn_state_is_a_zero_byte_refusal(self, monkeypatch, journal):
        resolved = _seq_resolved()
        client = self._wire(
            monkeypatch,
            resolved,
            _FakeSequenceAdapter(),
            {0: {"lines": ["hello"]}},
            turn_status=RuntimeError("pane unreadable"),
        )
        result = _deliver_sequence(journal, events=[{"type": "text", "text": "hello"}])
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_PANE_BUSY
        assert client.writes == []

    def test_kimi_dispatch_grace_gates_composer_but_not_interrupt(self, monkeypatch, journal):
        resolved = _seq_resolved()
        binding = ControlInputBinding(
            request_id=CONTROL,
            terminal_id=TERMINAL,
            pane_id=PANE,
            window_id=WINDOW,
            pane_pid=PANE_PID,
            request_sha256="0" * 64,
            generation=GENERATION,
            server_socket_path=SOCKET,
        )
        # Marked after wiring: the harness clears the grace table to model a
        # fresh server, so a dispatch marked before it never happened.
        client = self._wire(
            monkeypatch,
            resolved,
            _FakeSequenceAdapter(),
            {0: {"lines": ["hello"]}},
            turn_status=TerminalStatus.IDLE,
        )
        service._mark_native_kimi_dispatch(service._native_kimi_dispatch_key(resolved, binding))
        refused = _deliver_sequence(journal, events=[{"type": "text", "text": "hello"}])
        assert refused.outcome == REFUSED
        assert refused.reason_code == REASON_PANE_BUSY

        delivered = _deliver_sequence(
            journal, events=[{"type": "key", "key": "Escape"}], control_id="ctl-interrupt-1"
        )
        assert delivered.outcome == ACCEPTED
        assert [write["key"] for write in client.writes] == ["Escape"]


# --- §4.1: the declared command-class guard ----------------------------------

from cli_agent_orchestrator.services.control_input_contract import (  # noqa: E402
    REASON_COMPOSER_NONEMPTY,
    REASON_MALFORMED_COMMAND_DECLARATION,
    REASON_PROVIDER_UNSUPPORTED,
    control_input_request_digest_v4,
)

COMMAND_EVENTS = [{"type": "text", "text": "/compact"}, {"type": "key", "key": "Enter"}]


def _deliver_declared(journal, events=COMMAND_EVENTS, **overrides):
    kwargs = {"control_id": CONTROL, "events": events, "payload_class": "command"}
    kwargs.update(overrides)
    return service.deliver_control_input(TERMINAL, journal=journal, **kwargs)


class TestCommandDeclarationShape:
    """The v4 carrier's request-level rules: the either/or, the typed
    malformed-declaration refusal, and the no-shape-detection rule."""

    def test_payload_class_beside_the_v1_fields_is_a_shape_error(self, journal):
        """v4 = v3 + the declaration carrier: the field is undefined beside
        text/enter, refused as a shape error rather than silently dropped
        (a declared command delivered as prose is a control nobody authorised)."""
        with pytest.raises(service.ControlInputRequestInvalid):
            service.deliver_control_input(
                TERMINAL,
                control_id=CONTROL,
                text="/compact",
                enter=True,
                payload_class="command",
                journal=journal,
            )

    def test_payload_class_beside_v2_chord_is_a_shape_error(self, journal):
        with pytest.raises(service.ControlInputRequestInvalid):
            service.deliver_control_input(
                TERMINAL,
                control_id=CONTROL,
                text="/compact",
                enter=False,
                chord="C-s",
                payload_class="command",
                journal=journal,
            )

    def test_an_unknown_payload_class_value_is_the_typed_refusal(self, tmux, journal):
        result = _deliver_declared(journal, payload_class="probe")
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_MALFORMED_COMMAND_DECLARATION
        assert result.request_schema_version == 4
        assert result.as_response()["reattemptable"] is True
        assert tmux.writes == []

    def test_a_non_string_payload_class_is_the_typed_refusal(self, tmux, journal):
        result = _deliver_declared(journal, payload_class=42)
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_MALFORMED_COMMAND_DECLARATION
        assert tmux.writes == []

    @pytest.mark.parametrize(
        "events",
        [
            [{"type": "text", "text": "prose"}],
            [{"type": "text", "text": "see /tmp/x"}],
            [{"type": "key", "key": "Enter"}],
            [{"type": "text", "text": "/a"}, {"type": "text", "text": "/b"}],
            [{"type": "text", "text": "/a"}, {"type": "key", "key": "Escape"}],
            [
                {"type": "text", "text": "/a"},
                {"type": "key", "key": "Enter"},
                {"type": "key", "key": "Enter"},
            ],
        ],
    )
    def test_a_declared_command_outside_the_grammar_is_the_typed_refusal(
        self, tmux, journal, events
    ):
        result = _deliver_declared(journal, events=events)
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_MALFORMED_COMMAND_DECLARATION
        assert [event["outcome"] for event in result.events] == ["refused"] * len(events)
        assert tmux.writes == []

    def test_an_explicit_null_payload_class_is_the_absent_declaration(self, tmux, journal):
        """A JSON null is the prose spelling: the request is v3, and the
        guard never fires."""
        result = service.deliver_control_input(
            TERMINAL,
            control_id=CONTROL,
            events=[{"type": "text", "text": "hello"}],
            payload_class=None,
            journal=journal,
        )
        assert result.outcome == ACCEPTED
        assert result.request_schema_version == 3

    def test_a_declared_request_rebinds_against_a_v3_caller_digest(self, tmux, journal):
        """The declaration participates in the digest: a caller that
        authorised the v3 (undeclared) request is not bound to the
        declared one — rebound blindness is what the carrier prevents."""
        v3_digest = control_input_request_digest_v3(
            control_id=CONTROL, events=COMMAND_EVENTS, expected_identity=None
        )
        result = _deliver_declared(journal, request_digest=v3_digest)
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_REQUEST_REBOUND
        assert tmux.writes == []


def _deliver_interactive(journal, events, **overrides):
    kwargs = {"control_id": CONTROL, "events": events, "payload_class": "interactive"}
    kwargs.update(overrides)
    return service.deliver_control_input(TERMINAL, journal=journal, **kwargs)


class TestInteractiveDeclaration:
    """§6.7 (r15): a declared interactive batch bypasses only the provider
    turn-state readiness refusal and the kimi dispatch grace — capability-
    gated per terminal build — with every other guard (lease, identity,
    copy mode, journal, deadline) exactly as for an undeclared batch."""

    INTERACTIVE_EVENTS = [{"type": "text", "text": "queued mid-turn"}]

    def _wire(self, monkeypatch, resolved, adapter, plans, turn_status=TerminalStatus.IDLE):
        from cli_agent_orchestrator.services import managed_launch_v2

        with service._native_kimi_dispatch_guard_lock:
            service._native_kimi_dispatch_times.clear()
        monkeypatch.setattr(service, "resolve_control_identity", lambda tid: resolved)
        client = FakeTmux(
            identities=[
                FakePaneIdentity(
                    pane_id=resolved.pane_id,
                    window_id=resolved.window_id,
                    pane_pid=resolved.pane_pid,
                )
            ]
        )
        monkeypatch.setattr(service, "_tmux_client", lambda: client)
        monkeypatch.setattr(
            service,
            "_native_sequence_preflight",
            lambda *a, **k: (adapter, plans, None),
        )

        def _observe(provider, **kwargs):
            if isinstance(turn_status, BaseException):
                raise turn_status
            return turn_status

        monkeypatch.setattr(managed_launch_v2, "_observe_turn_state", _observe)
        return client

    def test_interactive_bypasses_the_turn_gate_that_gates_the_same_events_undeclared(
        self, monkeypatch, journal
    ):
        resolved = _seq_resolved()
        client = self._wire(
            monkeypatch,
            resolved,
            _FakeSequenceAdapter(),
            {0: {"lines": ["queued mid-turn"]}},
            turn_status=TerminalStatus.PROCESSING,
        )
        undeclared = _deliver_sequence(journal, events=self.INTERACTIVE_EVENTS)
        assert undeclared.outcome == REFUSED
        assert undeclared.reason_code == REASON_PANE_BUSY
        assert client.writes == []

        declared = _deliver_interactive(
            journal, self.INTERACTIVE_EVENTS, control_id="ctl-interactive-1"
        )
        assert declared.outcome == ACCEPTED
        assert declared.request_schema_version == 4
        assert len(client.writes) == 1

    def test_interactive_bypasses_the_kimi_dispatch_grace_but_marks_after_enter(
        self, monkeypatch, journal
    ):
        resolved = _seq_resolved()
        binding = ControlInputBinding(
            request_id=CONTROL,
            terminal_id=TERMINAL,
            pane_id=PANE,
            window_id=WINDOW,
            pane_pid=PANE_PID,
            request_sha256="0" * 64,
            generation=GENERATION,
            server_socket_path=SOCKET,
        )
        client = self._wire(
            monkeypatch,
            resolved,
            _FakeSequenceAdapter(),
            {0: {"lines": ["queued mid-turn"]}},
            turn_status=TerminalStatus.IDLE,
        )
        # Inside the grace of a preceding Enter: undeclared is gated (the
        # §6.4 pause case), the declared interactive batch is not.
        service._mark_native_kimi_dispatch(service._native_kimi_dispatch_key(resolved, binding))
        declared = _deliver_interactive(
            journal,
            [{"type": "text", "text": "queued mid-turn"}, {"type": "key", "key": "Enter"}],
            control_id="ctl-interactive-1",
        )
        assert declared.outcome == ACCEPTED
        # The interactive Enter still marked the dispatch: a following
        # undeclared composer batch keeps its grace protection.
        undeclared = _deliver_sequence(
            journal, events=self.INTERACTIVE_EVENTS, control_id="ctl-undeclared-1"
        )
        assert undeclared.outcome == REFUSED
        assert undeclared.reason_code == REASON_PANE_BUSY
        assert "dispatch grace" in undeclared.detail

    def test_interactive_still_refuses_real_lease_contention(self, monkeypatch, journal):
        resolved = _seq_resolved()
        client = self._wire(
            monkeypatch,
            resolved,
            _FakeSequenceAdapter(),
            {0: {"lines": ["queued mid-turn"]}},
        )

        @contextmanager
        def _held_lease(*a, **k):
            raise service.PaneBusyError(
                "input lease is held by another process/thread: operator-message:op-1"
            )

        monkeypatch.setattr(service, "pane_input_lease", _held_lease)
        result = _deliver_interactive(journal, self.INTERACTIVE_EVENTS)
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_PANE_BUSY
        assert "input lease is held by" in result.detail
        assert result.request_schema_version == 4
        assert client.writes == []

    def test_interactive_on_an_unlisted_build_delivers_under_the_default(
        self, monkeypatch, journal
    ):
        """Unpinned: interactive streaming follows the version observation.

        The declaration rides the deployed v3 sequence transport; an
        unlisted-but-observed build advertises it as the conservative
        default, so the batch delivers.  Only a failed observation (no
        version at all) withholds the block.
        """
        resolved = _seq_resolved(provider_version="9.9.9")
        client = self._wire(
            monkeypatch,
            resolved,
            _FakeSequenceAdapter(),
            {0: {"lines": ["queued mid-turn"]}},
        )
        result = _deliver_interactive(journal, self.INTERACTIVE_EVENTS)
        assert result.outcome == ACCEPTED
        assert any(write.get("text") == "queued mid-turn" for write in client.writes)

    def test_interactive_on_an_unknown_version_fails_closed(self, monkeypatch, journal):
        resolved = _seq_resolved(provider_version=None)
        client = self._wire(
            monkeypatch,
            resolved,
            _FakeSequenceAdapter(),
            {0: {"lines": ["queued mid-turn"]}},
        )
        result = _deliver_interactive(journal, self.INTERACTIVE_EVENTS)
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_PROVIDER_UNSUPPORTED
        assert client.writes == []

    def test_interactive_still_runs_the_copy_mode_guard(self, monkeypatch, journal):
        resolved = _seq_resolved()
        client = self._wire(
            monkeypatch,
            resolved,
            _FakeSequenceAdapter(),
            {0: {"lines": ["queued mid-turn"]}},
        )
        monkeypatch.setattr(
            service,
            "_copy_mode_guard_refusal",
            lambda *a, **k: ("copy-mode-active", "the pane is in copy mode"),
        )
        result = _deliver_interactive(journal, self.INTERACTIVE_EVENTS)
        assert result.outcome == REFUSED
        assert result.reason_code == "copy-mode-active"
        assert client.writes == []

    def test_interactive_refuses_fail_closed_in_copy_mode_without_exiting_it(
        self, monkeypatch, journal
    ):
        """§6.7 (r15): the interactive bypass never skips the copy-mode
        guard, and for a declared batch the guard is fail-closed — zero
        bytes, no exit control, the operator's copy mode untouched."""
        resolved = _seq_resolved()
        client = self._wire(
            monkeypatch,
            resolved,
            _FakeSequenceAdapter(),
            {0: {"lines": ["queued mid-turn"]}},
        )
        monkeypatch.setattr(client, "pane_in_copy_mode", lambda pane_id, **kwargs: True)
        result = _deliver_interactive(journal, self.INTERACTIVE_EVENTS)
        assert result.outcome == REFUSED
        assert result.reason_code == "copy-mode-active"
        assert "left untouched" in result.detail
        # Fail closed: no exit control and no payload bytes.
        assert client.writes == []

    def test_undeclared_keeps_the_legacy_copy_mode_exit_and_deliver(self, monkeypatch, journal):
        """The legacy undeclared behavior is preserved exactly: a proven
        copy mode is exited once and the payload delivered exactly once."""
        resolved = _seq_resolved()
        client = self._wire(
            monkeypatch,
            resolved,
            _FakeSequenceAdapter(),
            {0: {"lines": ["queued mid-turn"]}},
        )
        in_mode = {"value": True}
        monkeypatch.setattr(client, "pane_in_copy_mode", lambda pane_id, **kwargs: in_mode["value"])
        original_cancel = client.send_copy_mode_cancel

        def _cancel(pane_id, **kwargs):
            in_mode["value"] = False
            return original_cancel(pane_id, **kwargs)

        monkeypatch.setattr(client, "send_copy_mode_cancel", _cancel)
        result = _deliver_sequence(journal, events=self.INTERACTIVE_EVENTS)
        assert result.outcome == ACCEPTED
        assert client.writes[0]["copy_mode_cancel"] is True
        assert any(write.get("text") == "queued mid-turn" for write in client.writes)

    def test_a_divergent_declaration_on_a_reused_id_is_request_rebound(self, monkeypatch, journal):
        resolved = _seq_resolved()
        self._wire(
            monkeypatch,
            resolved,
            _FakeSequenceAdapter(),
            {0: {"lines": ["queued mid-turn"]}},
        )
        declared = _deliver_interactive(journal, self.INTERACTIVE_EVENTS)
        assert declared.outcome == ACCEPTED
        # The same id with the same events but undeclared is a different
        # request (the declaration participates in the digest), not a replay.
        undeclared = _deliver_sequence(journal, events=self.INTERACTIVE_EVENTS)
        assert undeclared.outcome == REFUSED
        assert undeclared.reason_code == REASON_REQUEST_REBOUND

    def test_interactive_legal_payload_is_the_v3_sequence_grammar(self, monkeypatch, journal):
        """Prose text, ordinary keys, and chords ride a declaration that
        never enters the command grammar — even slash-led prose."""
        resolved = _seq_resolved()
        client = self._wire(
            monkeypatch,
            resolved,
            _FakeSequenceAdapter(),
            {0: {"lines": ["see /tmp/x"]}},
        )
        result = _deliver_interactive(
            journal,
            [{"type": "text", "text": "see /tmp/x"}, {"type": "key", "key": "Down"}],
        )
        assert result.outcome == ACCEPTED
        assert len(client.writes) == 2


class TestCommandClassGuard:
    """The never-concatenate guard (§4.1): a declared command is written
    only against a composer *proven empty*, under the lease, before the
    claim — and undeclared payloads never see the guard at all."""

    def _wire(
        self,
        monkeypatch,
        resolved,
        adapter,
        plans,
        *,
        empty,
        turn_status=TerminalStatus.IDLE,
        execution_close=None,
    ):
        from cli_agent_orchestrator.services import managed_launch_v2
        from cli_agent_orchestrator.services.control_input_contract import SUBMISSION_SUBMITTED

        with service._native_kimi_dispatch_guard_lock:
            service._native_kimi_dispatch_times.clear()
        monkeypatch.setattr(service, "resolve_control_identity", lambda tid: resolved)
        client = FakeTmux(
            identities=[
                FakePaneIdentity(
                    pane_id=resolved.pane_id,
                    window_id=resolved.window_id,
                    pane_pid=resolved.pane_pid,
                )
            ]
        )
        monkeypatch.setattr(service, "_tmux_client", lambda: client)
        monkeypatch.setattr(
            service,
            "_native_sequence_preflight",
            lambda *a, **k: (adapter, plans, None),
        )
        monkeypatch.setattr(managed_launch_v2, "_observe_turn_state", lambda *a, **k: turn_status)
        # The pre-write baseline capture and the r11 post-write execution
        # observation: no real pane exists here, so both are staged.  The
        # default close is the proven one — submitted with an evidence ref —
        # and two-close tests pass their own (observed, ref) pair.
        monkeypatch.setattr(
            native_pane_input, "capture_execution_rows", lambda pane_id, pin, **k: []
        )
        if execution_close is None:
            execution_close = (SUBMISSION_SUBMITTED, "capture-pane:%17:test:sha256:beef")
        monkeypatch.setattr(
            native_pane_input,
            "observe_command_execution",
            lambda pane_id, pin, **k: execution_close,
        )
        if isinstance(empty, BaseException):
            monkeypatch.setattr(
                native_pane_input, "observe_composer_empty", self._raising_observer(empty)
            )
        else:
            observations = []
            monkeypatch.setattr(
                native_pane_input,
                "observe_composer_empty",
                lambda pane_id, pin, **k: observations.append((pane_id, pin)) or empty,
            )
            return client, observations
        return client, None

    @staticmethod
    def _raising_observer(exc):
        def _observe(pane_id, pin, **kwargs):
            raise exc

        return _observe

    def _declared(
        self, journal, monkeypatch, *, empty, resolved=None, execution_close=None, **overrides
    ):
        # 0.29.2 is the live-verified emptiness pin; an unpinned build is
        # the provider-unsupported case, pinned separately below.
        resolved = resolved or _seq_resolved(provider_version="0.29.2")
        adapter = _FakeSequenceAdapter()
        plans = {0: {"lines": ["/compact"]}}
        wired = self._wire(
            monkeypatch, resolved, adapter, plans, empty=empty, execution_close=execution_close
        )
        client = wired[0]
        result = _deliver_declared(journal, **overrides)
        return result, client, adapter, wired[1]

    def test_a_nonempty_composer_is_the_zero_byte_refusal(self, monkeypatch, journal):
        result, client, adapter, _ = self._declared(journal, monkeypatch, empty=False)
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_COMPOSER_NONEMPTY
        assert result.request_schema_version == 4
        assert result.as_response()["reattemptable"] is True
        # Zero command bytes — and zero bytes of any kind: the guard
        # observes and refuses, it never clears (no Escape, no edit keys).
        assert client.writes == []
        assert adapter.calls == []
        assert [event["outcome"] for event in result.events] == ["refused"] * 2

    def test_an_unobservable_composer_fails_closed_identically(self, monkeypatch, journal):
        """ "Could not look" is not "empty": an unproven composer is the
        same zero-byte refusal as a proven-non-empty one."""
        result, client, adapter, _ = self._declared(journal, monkeypatch, empty=None)
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_COMPOSER_NONEMPTY
        assert client.writes == []
        assert adapter.calls == []

    def test_a_failed_observation_also_fails_closed(self, monkeypatch, journal):
        result, client, adapter, _ = self._declared(
            journal, monkeypatch, empty=NativePaneInputUnavailable("tmux died")
        )
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_COMPOSER_NONEMPTY
        assert client.writes == []
        assert adapter.calls == []

    def test_a_proven_empty_composer_delivers_exactly_the_command(self, monkeypatch, journal):
        result, client, adapter, observations = self._declared(journal, monkeypatch, empty=True)
        assert result.outcome == ACCEPTED
        assert result.request_schema_version == 4
        assert adapter.calls == [({"lines": ["/compact"]}, True)]
        assert [event["outcome"] for event in result.events] == ["sent"] * 2
        # The observation ran under the lease against the bound pane.
        assert observations == [
            (PANE, native_pane_input.composer_emptiness_pin_for("kimi_cli", "0.29.2"))
        ]
        # The r11 close: accepted only with the execution evidence attached
        # — an accepted declared-command record with a null submission
        # observation or evidence ref is the PR #48 defect class.
        assert result.submission_observed == "submitted"
        assert result.submission_evidence_ref == "capture-pane:%17:test:sha256:beef"
        record = journal.find(CONTROL)
        assert record.state == DELIVERED
        assert record.submission_observed == "submitted"
        assert record.submission_evidence_ref == "capture-pane:%17:test:sha256:beef"

    def test_a_provider_build_without_a_pin_is_provider_unsupported(self, monkeypatch, journal):
        """No proven emptiness determination for this exact build: refused
        rather than guessed at (§4.1)."""
        result, client, adapter, _ = self._declared(
            journal, monkeypatch, empty=True, resolved=_seq_resolved(provider_version="0.28.0")
        )
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_PROVIDER_UNSUPPORTED
        assert client.writes == []
        assert adapter.calls == []

    def test_the_guard_is_journaled_and_reconciles_by_exact_id(self, monkeypatch, journal):
        result, _, _, _ = self._declared(journal, monkeypatch, empty=False)
        assert result.reason_code == REASON_COMPOSER_NONEMPTY
        looked_up = service.lookup_control_input(CONTROL, journal=journal)
        assert looked_up.outcome == REFUSED
        assert looked_up.reason_code == REASON_COMPOSER_NONEMPTY
        assert [event["outcome"] for event in looked_up.events] == ["refused"] * 2

    def test_a_retry_after_the_refusal_rearms_and_delivers(self, monkeypatch, journal):
        """refusal proves zero bytes, so the same control id may re-arm
        (the deployed refused → intent edge): once the operator clears the
        composer, the identical retry delivers."""
        refused, _, _, _ = self._declared(journal, monkeypatch, empty=False)
        assert refused.reason_code == REASON_COMPOSER_NONEMPTY
        accepted, _, adapter, _ = self._declared(journal, monkeypatch, empty=True)
        assert accepted.outcome == ACCEPTED
        assert adapter.calls == [({"lines": ["/compact"]}, True)]

    def test_the_readiness_gate_still_precedes_the_guard(self, monkeypatch, journal):
        """During an active turn the answer is the deployed pane-busy, and
        the composer is never observed: the guard adds nothing to the
        existing gates."""
        resolved = _seq_resolved()
        adapter = _FakeSequenceAdapter()
        client, observations = self._wire(
            monkeypatch,
            resolved,
            adapter,
            {0: {"lines": ["/compact"]}},
            empty=AssertionError("the guard must not observe during a turn"),
            turn_status=TerminalStatus.PROCESSING,
        )
        result = _deliver_declared(journal)
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_PANE_BUSY
        assert client.writes == []

    def test_a_stale_identity_never_reaches_the_guard(self, monkeypatch, journal):
        resolved = _seq_resolved()
        adapter = _FakeSequenceAdapter()
        self._wire(
            monkeypatch,
            resolved,
            adapter,
            {0: {"lines": ["/compact"]}},
            empty=AssertionError("the guard must not observe for a stale binding"),
        )
        result = _deliver_declared(
            journal, expected_identity={"terminal_generation": "gen-from-before"}
        )
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_STALE_GENERATION
        assert result.request_schema_version == 4

    def test_undeclared_command_shaped_text_is_prose_and_never_guarded(self, monkeypatch, journal):
        """The r7 carrier case: a batch whose text begins with '/' is
        undeclared prose.  The streamed `see /tmp/x` split — a batch
        starting '/tmp/x' after the quiet timer — sails through with no
        guard and no observation, exactly as before v4 existed."""
        resolved = _seq_resolved()
        adapter = _FakeSequenceAdapter()
        self._wire(
            monkeypatch,
            resolved,
            adapter,
            {0: {"lines": ["/tmp/x"]}},
            empty=AssertionError("undeclared prose must never be observed"),
        )
        result = _deliver_sequence(journal, events=[{"type": "text", "text": "/tmp/x"}])
        assert result.outcome == ACCEPTED
        assert result.request_schema_version == 3
        assert adapter.calls == [({"lines": ["/tmp/x"]}, False)]

    def test_undeclared_compact_text_uses_the_kimi_submission_barrier(
        self, monkeypatch, kimi, journal
    ):
        """The deployed Compact button sends '/compact' as ordinary text:
        undeclared text remains prose rather than acquiring command
        semantics, while its text-plus-Enter still gets the provider's
        ordinary submission proof."""
        from cli_agent_orchestrator.services import managed_launch_v2

        resolved = _seq_resolved()
        adapter = _FakeSequenceAdapter()
        monkeypatch.setattr(service, "resolve_control_identity", lambda tid: resolved)
        monkeypatch.setattr(service, "_tmux_client", lambda: kimi.client)
        monkeypatch.setattr(
            service,
            "_native_sequence_preflight",
            lambda *a, **k: (adapter, {0: {"lines": ["/compact"]}}, None),
        )
        monkeypatch.setattr(
            managed_launch_v2,
            "_observe_turn_state",
            lambda provider, **kwargs: TerminalStatus.IDLE,
        )
        result = _deliver_sequence(journal, events=COMMAND_EVENTS)

        assert result.outcome == ACCEPTED
        assert result.request_schema_version == 3
        assert result.submission_observed == SUBMISSION_SUBMITTED
        assert adapter.calls == [({"lines": ["/compact"]}, False)]
        assert adapter.submit_calls == [{"lines": ["/compact"]}]
        assert [write["submit"] for write in kimi.client.writes] == [False, True]


class TestCommandTwoClose:
    """The r11 two-close rule for declared commands (§4.1): transport
    acceptance is not command execution — ``accepted`` only with the
    pinned execution signal observed and its evidence journaled;
    otherwise ``ambiguous``/``submission-unproven``, terminal, never a
    retry licence."""

    def _declared(self, journal, monkeypatch, *, execution_close, resolved=None, **overrides):
        harness = TestCommandClassGuard()
        resolved = resolved or _seq_resolved(provider_version="0.29.2")
        adapter = _FakeSequenceAdapter()
        harness._wire(
            monkeypatch,
            resolved,
            adapter,
            {0: {"lines": ["/compact"]}},
            empty=True,
            execution_close=execution_close,
        )
        result = _deliver_declared(journal, **overrides)
        return result, adapter

    def test_an_unproven_execution_closes_ambiguous_never_accepted(self, monkeypatch, journal):
        result, adapter = self._declared(
            journal, monkeypatch, execution_close=(SUBMISSION_UNKNOWN, None)
        )
        assert result.outcome == AMBIGUOUS
        assert result.reason_code == "submission-unproven"
        assert result.request_schema_version == 4
        assert result.submission_observed == "unknown"
        assert "execution signal" in result.detail
        # The write happened; the close is terminal for automation — the
        # exact-id reconcile replays the record and a re-POST never resends.
        record = journal.find(CONTROL)
        assert record.state == STATE_AMBIGUOUS
        assert record.submission_observed == "unknown"
        looked_up = service.lookup_control_input(CONTROL, journal=journal)
        assert looked_up.outcome == AMBIGUOUS
        assert looked_up.reason_code == "submission-unproven"
        assert adapter.calls == [({"lines": ["/compact"]}, True)]  # exactly one write, ever

    def test_a_resting_command_closes_unsubmitted_with_evidence(self, monkeypatch, journal):
        result, _ = self._declared(
            journal,
            monkeypatch,
            execution_close=(SUBMISSION_UNSUBMITTED, "capture-pane:%17:t2:sha256:cafe"),
        )
        assert result.outcome == AMBIGUOUS
        assert result.reason_code == "submission-unproven"
        assert result.submission_observed == "unsubmitted"
        assert result.submission_evidence_ref == "capture-pane:%17:t2:sha256:cafe"

    def test_a_same_id_repost_replays_the_ambiguous_record_with_zero_new_writes(
        self, monkeypatch, journal
    ):
        result, adapter = self._declared(
            journal, monkeypatch, execution_close=(SUBMISSION_UNKNOWN, None)
        )
        assert result.outcome == AMBIGUOUS
        replay, _ = self._declared(journal, monkeypatch, execution_close=(SUBMISSION_UNKNOWN, None))
        assert replay.outcome == AMBIGUOUS
        assert replay.reason_code == "submission-unproven"
        # One composer plan execution total — the replay answered from the
        # journal, never from the pane.
        assert adapter.calls == [({"lines": ["/compact"]}, True)]

    def test_a_build_without_the_execution_pin_is_refused_pre_write(self, monkeypatch, journal):
        """Both pins are required: an emptiness pin alone would close on
        transport facts — the PR #48 defect class."""
        monkeypatch.setattr(native_pane_input, "command_execution_pin_for", lambda *a: None)
        result, adapter = self._declared(journal, monkeypatch, execution_close=None)
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_PROVIDER_UNSUPPORTED
        assert "execution observation" in result.detail
        assert adapter.calls == []

    def test_a_failed_baseline_capture_fails_the_guard_closed(self, monkeypatch, journal):
        harness = TestCommandClassGuard()
        resolved = _seq_resolved(provider_version="0.29.2")
        adapter = _FakeSequenceAdapter()
        harness._wire(monkeypatch, resolved, adapter, {0: {"lines": ["/compact"]}}, empty=True)
        monkeypatch.setattr(
            native_pane_input,
            "capture_execution_rows",
            lambda *a, **k: (_ for _ in ()).throw(NativePaneInputUnavailable("tmux died")),
        )
        # The baseline feeds the guard's observation, so an empty baseline
        # is an unproven composer — zero bytes, never a guessed write.
        monkeypatch.setattr(
            native_pane_input,
            "observe_composer_empty",
            lambda pane_id, pin, **k: None if k.get("screen")() == [] else True,
        )
        result = _deliver_declared(journal)
        assert result.outcome == REFUSED
        assert result.reason_code == REASON_COMPOSER_NONEMPTY
        assert adapter.calls == []

    def test_every_accepted_declared_command_carries_execution_evidence(self, monkeypatch, journal):
        """The PR #48 regression shape, forbidden outright: no accepted
        declared-command record may have a null submission observation or
        a null evidence reference."""
        result, _ = self._declared(
            journal,
            monkeypatch,
            execution_close=(SUBMISSION_SUBMITTED, "capture-pane:%17:t3:sha256:d00d"),
        )
        assert result.outcome == ACCEPTED
        assert result.submission_observed is not None
        assert result.submission_evidence_ref is not None
        record = journal.find(CONTROL)
        assert record.state == DELIVERED
        assert record.submission_observed is not None
        assert record.submission_evidence_ref is not None
        # And the replayed answer carries the same evidence, not a null.
        looked_up = service.lookup_control_input(CONTROL, journal=journal)
        assert looked_up.outcome == ACCEPTED
        assert looked_up.submission_observed == "submitted"
        assert looked_up.submission_evidence_ref == "capture-pane:%17:t3:sha256:d00d"

    def test_a_late_signal_never_closes_accepted_on_the_send_path(self, monkeypatch, journal):
        """The caller-side deadline defense (steer-041): the helper hands
        back a submitted observation *after* the write deadline — a
        capture that completed late — and the close is still the terminal
        ambiguity, never an accepted record carrying after-deadline
        evidence.  Replaying by exact id writes nothing further."""
        import time as _time

        monkeypatch.setattr(service, "WRITE_DEADLINE_SECONDS", 0.2)

        def _late_submitted(*args, **kwargs):
            _time.sleep(0.4)
            return (SUBMISSION_SUBMITTED, "capture-pane:%17:late:sha256:1afe")

        harness = TestCommandClassGuard()
        resolved = _seq_resolved(provider_version="0.29.2")
        adapter = _FakeSequenceAdapter()
        harness._wire(monkeypatch, resolved, adapter, {0: {"lines": ["/compact"]}}, empty=True)
        monkeypatch.setattr(native_pane_input, "observe_command_execution", _late_submitted)
        result = _deliver_declared(journal)
        assert result.outcome == AMBIGUOUS
        assert result.reason_code == "submission-unproven"
        assert result.submission_observed == "unknown"
        # No accepted record exists: the journal never reached delivered,
        # so nothing carries the after-deadline evidence ref.
        record = journal.find(CONTROL)
        assert record.state == STATE_AMBIGUOUS
        assert record.submission_evidence_ref != "capture-pane:%17:late:sha256:1afe"
        looked_up = service.lookup_control_input(CONTROL, journal=journal)
        assert looked_up.outcome == AMBIGUOUS
        assert looked_up.reason_code == "submission-unproven"
        # One composer plan execution total — the exact-id reconcile
        # answered from the record and never wrote again.
        assert adapter.calls == [({"lines": ["/compact"]}, True)]

    def test_a_late_signal_never_closes_accepted_on_the_literal_sink(self, monkeypatch, journal):
        """The literal-sink twin (steer-043): the unmanaged send path has
        its own caller defense, and it holds the same boundary — a helper
        returning submitted only after the write deadline closes the
        declared command terminally ambiguous with no accepted record and
        no after-deadline evidence anywhere."""
        import time as _time

        monkeypatch.setattr(service, "WRITE_DEADLINE_SECONDS", 0.2)

        def _late_submitted(*args, **kwargs):
            _time.sleep(0.4)
            return (SUBMISSION_SUBMITTED, "capture-pane:%17:late:sha256:1afe")

        # The literal sink serves an unmanaged pane: same provider+build
        # pins, no managed adapter preflight.
        resolved = _seq_resolved(provider_version="0.29.2", managed=False)
        monkeypatch.setattr(service, "resolve_control_identity", lambda tid: resolved)
        client = FakeTmux(
            identities=[
                FakePaneIdentity(
                    pane_id=resolved.pane_id,
                    window_id=resolved.window_id,
                    pane_pid=resolved.pane_pid,
                )
            ]
        )
        monkeypatch.setattr(service, "_tmux_client", lambda: client)
        monkeypatch.setattr(native_pane_input, "observe_composer_empty", lambda *a, **k: True)
        monkeypatch.setattr(native_pane_input, "capture_execution_rows", lambda *a, **k: [])
        monkeypatch.setattr(native_pane_input, "observe_command_execution", _late_submitted)

        result = _deliver_declared(journal)
        assert result.outcome == AMBIGUOUS
        assert result.reason_code == "submission-unproven"
        assert result.submission_observed == "unknown"
        # Null evidence in the response AND in the journal row — nothing
        # may carry the after-deadline ref.
        assert result.submission_evidence_ref is None
        record = journal.find(CONTROL)
        assert record.state == STATE_AMBIGUOUS
        assert record.submission_evidence_ref is None
        # The one fused text+Enter write happened exactly once; the
        # exact-id replay adds zero writes.
        writes_before = len(client.writes)
        assert writes_before == 1
        looked_up = service.lookup_control_input(CONTROL, journal=journal)
        assert looked_up.outcome == AMBIGUOUS
        assert looked_up.reason_code == "submission-unproven"
        assert len(client.writes) == writes_before


class TestPaneBusyDetailDiscriminators:
    """The three pane-busy detail strings, verbatim and pairwise-disjoint
    (§10.1): streaming's pause/disarm routing reads them, so a wording
    tweak must fail this suite loudly rather than silently re-route."""

    GRACE = "inside its dispatch grace"
    TURN = "not idle"
    LEASE = "input lease is held by"

    def test_the_three_discriminators_are_pairwise_disjoint(self):
        for mine, theirs in (
            (self.GRACE, (self.TURN, self.LEASE)),
            (self.TURN, (self.GRACE, self.LEASE)),
            (self.LEASE, (self.GRACE, self.TURN)),
        ):
            assert all(mine not in other for other in theirs)

    def test_dispatch_grace_detail(self, monkeypatch, journal):
        resolved = _seq_resolved()
        binding = _chord_binding(
            control_input_request_digest_v3(
                control_id=CONTROL,
                events=[{"type": "text", "text": "hello"}],
                expected_identity=None,
            )
        )
        gate = TestSequenceReadinessGate()
        gate._wire(
            monkeypatch,
            resolved,
            _FakeSequenceAdapter(),
            {0: {"lines": ["hello"]}},
            turn_status=TerminalStatus.IDLE,
        )
        service._mark_native_kimi_dispatch(service._native_kimi_dispatch_key(resolved, binding))
        result = _deliver_sequence(journal, events=[{"type": "text", "text": "hello"}])
        assert result.reason_code == REASON_PANE_BUSY
        assert self.GRACE in result.detail

    def test_turn_state_detail(self, monkeypatch, journal):
        resolved = _seq_resolved()
        gate = TestSequenceReadinessGate()
        gate._wire(
            monkeypatch,
            resolved,
            _FakeSequenceAdapter(),
            {0: {"lines": ["hello"]}},
            turn_status=TerminalStatus.PROCESSING,
        )
        result = _deliver_sequence(journal, events=[{"type": "text", "text": "hello"}])
        assert result.reason_code == REASON_PANE_BUSY
        assert self.TURN in result.detail

    def test_arbiter_contention_detail(self, tmux, journal):
        with _pane_held_elsewhere():
            result = _deliver_sequence(journal, events=[{"type": "key", "key": "Up"}])
        assert result.reason_code == REASON_PANE_BUSY
        assert self.LEASE in result.detail
        assert tmux.writes == []


class TestSequenceJournalReplay:
    """The v5 stored-row replay: stored per-event results, zero new I/O."""

    def test_delivered_sequence_replays_stored_event_outcomes(self, tmux, journal):
        events = [
            {"type": "text", "text": "first"},
            {"type": "key", "key": "Enter"},
            {"type": "key", "key": "Escape"},
        ]
        first = _deliver_sequence(journal, events=events)
        assert first.outcome == ACCEPTED
        writes_after = len(tmux.writes)

        replay = _deliver_sequence(journal, events=events)
        assert replay.outcome == ACCEPTED
        assert replay.request_schema_version == 3
        assert len(tmux.writes) == writes_after  # zero new writes
        assert [event["outcome"] for event in replay.events] == ["sent"] * 3
        assert replay.events[2] == {
            "ordinal": 2,
            "type": "key",
            "key": "Escape",
            "outcome": "sent",
        }
        # The exact-id lookup answers the same stored row.
        looked = service.lookup_control_input(CONTROL, journal=journal)
        assert looked.request_schema_version == 3
        assert [event["outcome"] for event in looked.events] == ["sent"] * 3

    def test_ambiguous_sequence_replays_the_stored_boundary(self, journal):
        calls = {"count": 0}

        def fail_on_second():
            calls["count"] += 1
            if calls["count"] == 2:
                raise TmuxLiteralSendError("tmux went away", chunks_sent=0, enter_attempted=False)

        client = FakeTmux(on_write=fail_on_second)
        events = [
            {"type": "text", "text": "typed first"},
            {"type": "key", "key": "Escape"},
            {"type": "key", "key": "C-c"},
        ]
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(service, "_tmux_client", lambda: client)
            mp.setattr(service, "_terminal_metadata", lambda terminal_id: _metadata())
            mp.setattr(service, "_managed_identity", lambda terminal_id: None)
            first = _deliver_sequence(journal, events=events)
        assert first.outcome == AMBIGUOUS
        writes_after = len(client.writes)

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(service, "_tmux_client", lambda: client)
            mp.setattr(service, "_terminal_metadata", lambda terminal_id: _metadata())
            mp.setattr(service, "_managed_identity", lambda terminal_id: None)
            replay = _deliver_sequence(journal, events=events)
        assert replay.outcome == AMBIGUOUS
        assert len(client.writes) == writes_after  # never auto-replayed
        assert [event["outcome"] for event in replay.events] == [
            "sent",
            "attempted",
            "skipped",
        ]

    def test_refused_sequence_stores_and_rearms_event_outcomes(self, journal):
        client = FakeTmux()
        events = [{"type": "text", "text": "busy test"}, {"type": "key", "key": "Escape"}]
        with _pane_held_elsewhere():
            with pytest.MonkeyPatch().context() as mp:
                mp.setattr(service, "_tmux_client", lambda: client)
                mp.setattr(service, "_terminal_metadata", lambda terminal_id: _metadata())
                mp.setattr(service, "_managed_identity", lambda terminal_id: None)
                refused = _deliver_sequence(journal, events=events)
        assert refused.outcome == REFUSED
        record = journal.get(CONTROL)
        assert [e["outcome"] for e in record.sequence_events] == ["refused", "refused"]

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(service, "_tmux_client", lambda: client)
            mp.setattr(service, "_terminal_metadata", lambda terminal_id: _metadata())
            mp.setattr(service, "_managed_identity", lambda terminal_id: None)
            delivered = _deliver_sequence(journal, events=events)
        assert delivered.outcome == ACCEPTED
        record = journal.get(CONTROL)
        assert [e["outcome"] for e in record.sequence_events] == ["sent", "sent"]


class TestSequenceSubmissionBarrier:
    """A sequence's text+Enter pair reuses the provider-pinned barrier."""

    def _codex_metadata(self):
        return _metadata(provider="codex")

    def test_text_then_enter_crosses_the_barrier_once(self, monkeypatch, journal):
        from cli_agent_orchestrator.services import native_pane_input

        monkeypatch.setattr(
            service, "_terminal_metadata", lambda terminal_id: self._codex_metadata()
        )
        monkeypatch.setattr(service, "_managed_identity", lambda terminal_id: None)
        client = FakeTmux()
        monkeypatch.setattr(service, "_tmux_client", lambda: client)
        observations = []
        monkeypatch.setattr(
            native_pane_input,
            "await_compose_visible",
            lambda pane_id, text, *, barrier, deadline_monotonic: observations.append(
                ("compose", text)
            )
            or True,
        )
        monkeypatch.setattr(
            native_pane_input,
            "observe_submission",
            lambda pane_id, text, *, barrier, deadline_monotonic: ("submitted", "evidence://ref-1"),
        )
        events = [{"type": "text", "text": "codex task"}, {"type": "key", "key": "Enter"}]
        result = _deliver_sequence(journal, events=events)
        assert result.outcome == ACCEPTED
        assert result.submission_observed == "submitted"
        assert result.submission_evidence_ref == "evidence://ref-1"
        # The barrier's two writes: text unsubmitted, then exactly one Enter.
        assert [(write.get("text"), write.get("submit")) for write in client.writes] == [
            ("codex task", False),
            ("", True),
        ]
        assert observations == [("compose", "codex task")]
        assert [event["outcome"] for event in result.events] == ["sent", "sent"]
        # The observation is journaled with the delivered record.
        record = journal.get(CONTROL)
        assert record.submission_observed == "submitted"
        assert record.submission_evidence_ref == "evidence://ref-1"

    def test_kimi_text_then_enter_reports_submission(self, kimi, journal):
        events = [
            {"type": "text", "text": "continue retained task"},
            {"type": "key", "key": "Enter"},
        ]

        result = _deliver_sequence(journal, events=events)

        assert result.outcome == ACCEPTED
        assert result.submission_observed == SUBMISSION_SUBMITTED
        assert result.submission_evidence_ref is not None
        assert [write.get("submit") for write in kimi.client.writes] == [
            False,
            True,
        ]
        assert [event["outcome"] for event in result.events] == ["sent", "sent"]

    def test_the_barrier_withholds_the_enter_when_text_never_settles(self, monkeypatch, journal):
        from cli_agent_orchestrator.services import native_pane_input

        monkeypatch.setattr(
            service, "_terminal_metadata", lambda terminal_id: self._codex_metadata()
        )
        monkeypatch.setattr(service, "_managed_identity", lambda terminal_id: None)
        client = FakeTmux()
        monkeypatch.setattr(service, "_tmux_client", lambda: client)
        monkeypatch.setattr(
            native_pane_input,
            "await_compose_visible",
            lambda pane_id, text, *, barrier, deadline_monotonic: False,
        )
        events = [{"type": "text", "text": "resting"}, {"type": "key", "key": "Enter"}]
        result = _deliver_sequence(journal, events=events)
        assert result.outcome == AMBIGUOUS
        assert result.reason_code == "submission-unproven"
        # Zero Enters: the Enter event is provably skipped, the text is
        # provably sent, and the sequence is terminal — never auto-replayed.
        assert [event["outcome"] for event in result.events] == ["sent", "skipped"]
        assert all(not write.get("submit") for write in client.writes)
        record = journal.get(CONTROL)
        assert record.state == STATE_AMBIGUOUS
        assert record.enter_attempted is False
        assert [e["outcome"] for e in record.sequence_events] == ["sent", "skipped"]

    def test_an_unsubmitted_observation_never_sends_a_second_enter(self, monkeypatch, journal):
        from cli_agent_orchestrator.services import native_pane_input

        monkeypatch.setattr(
            service, "_terminal_metadata", lambda terminal_id: self._codex_metadata()
        )
        monkeypatch.setattr(service, "_managed_identity", lambda terminal_id: None)
        client = FakeTmux()
        monkeypatch.setattr(service, "_tmux_client", lambda: client)
        monkeypatch.setattr(
            native_pane_input,
            "await_compose_visible",
            lambda pane_id, text, *, barrier, deadline_monotonic: True,
        )
        monkeypatch.setattr(
            native_pane_input,
            "observe_submission",
            lambda pane_id, text, *, barrier, deadline_monotonic: (
                "unsubmitted",
                "evidence://ref-2",
            ),
        )
        events = [
            {"type": "text", "text": "swallowed"},
            {"type": "key", "key": "Enter"},
            {"type": "key", "key": "Escape"},
        ]
        result = _deliver_sequence(journal, events=events)
        assert result.outcome == AMBIGUOUS
        assert result.reason_code == "submission-unproven"
        assert result.submission_observed == "unsubmitted"
        # Exactly one Enter ever; the tail never ran.
        enters = [write for write in client.writes if write.get("submit")]
        assert len(enters) == 1
        assert [event["outcome"] for event in result.events] == ["sent", "sent", "skipped"]


def test_parked_managed_literal_and_sequence_controls_write_zero_tmux_bytes(
    journal, tmux, monkeypatch, tmp_path
):
    """Both public control grammars meet the same managed park boundary."""
    from cli_agent_orchestrator import constants

    companion = tmp_path / "companion"
    monkeypatch.setattr(constants, "COMPANION_DIR", companion)
    resolved = _seq_resolved()
    monkeypatch.setattr(service, "resolve_control_identity", lambda _terminal: resolved)
    gf.install_fence(
        companion,
        terminal_id=TERMINAL,
        generation=resolved.terminal_generation,
        vintage="v2",
        fencing_token_id="token-1",
        request={
            "schema": gf.FENCE_REQUEST_SCHEMA,
            "terminal_generation": resolved.terminal_generation,
            "obligation_generation": "obligation-1",
            "attempt_id": "attempt-1",
            "intent_id": "11111111-1111-4111-8111-111111111111",
            "report_sha256": "a" * 64,
        },
    )

    literal = _deliver(journal, control_id="ctl-fenced-literal", text="literal")
    sequence = _deliver_sequence(
        journal,
        control_id="ctl-fenced-sequence",
        events=[{"type": "text", "text": "sequence"}, {"type": "key", "key": "Enter"}],
    )

    assert literal.outcome == sequence.outcome == REFUSED
    assert literal.reason_code == sequence.reason_code == REASON_GENERATION_FENCED
    assert tmux.writes == []


def test_unmanaged_control_ignores_an_unrelated_park_receipt_and_writes(
    journal, tmux, monkeypatch, tmp_path
):
    """M3 fences managed generations only; raw control keeps its prior lane."""
    from cli_agent_orchestrator import constants
    from cli_agent_orchestrator.services import native_pane_input

    companion = tmp_path / "companion"
    monkeypatch.setattr(constants, "COMPANION_DIR", companion)
    resolved = _seq_resolved(managed=False)
    monkeypatch.setattr(service, "resolve_control_identity", lambda _terminal: resolved)
    monkeypatch.setattr(
        native_pane_input,
        "await_compose_visible",
        lambda _pane, _text, *, barrier, deadline_monotonic: True,
    )
    monkeypatch.setattr(
        native_pane_input,
        "observe_submission",
        lambda _pane, _text, *, barrier, deadline_monotonic: (
            SUBMISSION_SUBMITTED,
            "evidence://unmanaged-after-park",
        ),
    )
    gf.install_park(
        companion,
        fencing_token_id="other-token",
        request={
            "schema": gf.PARK_REQUEST_SCHEMA,
            "operation_id": "22222222-2222-4222-8222-222222222222",
            "reservation_id": "33333333-3333-4333-8333-333333333333",
            "terminal_id": "feedface",
            "terminal_generation": "other-generation",
            "logical_task_id": "other-task",
            "retained_round": 0,
            "obligation_generation": "other-obligation",
            "attempt_id": "other-attempt",
            "report_sha256": "b" * 64,
        },
    )

    result = _deliver(journal, control_id="ctl-unmanaged-after-park", text="still-send")

    assert result.outcome == ACCEPTED
    assert tmux.writes
