"""HTTP-boundary tests for the identity-bound control-input surface.

The service suite proves what happens to a control.  These prove the wire
says so: that a caller reading nothing but a status code and a JSON body
reaches the same conclusion the service reached, and — the part that only
shows up at this layer — that the two statuses demanding opposite actions
can never be confused.  A ``200`` carrying ``refused`` means "this server
implements controls and declined this one, send it again".  A ``404``
means "this server has no control surface at all, and no re-attempt of
any kind is licensed".  A surface that answered ``404`` for an unknown
terminal would collapse those two into one signal, and a client acting on
the wrong reading either gives up on a working server or downgrades to
ordinary paste — which is the leak this lane exists to remove.

The fake tmux client here implements only the two calls the delivery path
is allowed to make.  If the route ever grew a fallback to ``send_keys``
or a paste buffer, these tests would fail with ``AttributeError`` rather
than quietly exercising it.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.security import auth
from cli_agent_orchestrator.services import control_input_service as service
from cli_agent_orchestrator.services import native_pane_input
from cli_agent_orchestrator.services.control_input_contract import (
    ACCEPTED,
    AMBIGUOUS,
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START,
    CONTROL_INPUT_DIGEST_DOMAIN,
    CONTROL_INPUT_OUTCOMES,
    CONTROL_INPUT_PROTOCOL,
    CONTROL_INPUT_REQUEST_SCHEMA_VERSION,
    IDENTITY_FIELDS,
    REASON_IDENTITY_MISMATCH,
    REASON_ILLEGAL_CONTROL_BYTES,
    REASON_OWNER_LOST_BEFORE_WRITE,
    REASON_OWNER_LOST_MID_WRITE,
    REASON_PANE_BUSY,
    REASON_PANE_DEAD,
    REASON_PROTOCOL_MISMATCH,
    REASON_REQUEST_REBOUND,
    REASON_STALE_GENERATION,
    REASON_UNKNOWN_TERMINAL,
    REFUSED,
    UNSUPPORTED,
    classify_transport_status,
    contains_bracketed_paste_sentinel,
    control_input_request_digest,
    is_reattemptable,
)
from cli_agent_orchestrator.services.control_input_journal import (
    ControlInputBinding,
    ControlInputJournal,
)
from cli_agent_orchestrator.services.native_pane_input import SubmissionBarrier
from cli_agent_orchestrator.services.pane_input_arbiter import (
    pane_input_lease,
    reset_pane_input_arbiter,
)

TERMINAL = "a1b2c3d4"
UNKNOWN_TERMINAL = "ffffffff"
CONTROL = "ctl-6f1b9c2d"
PANE = "%17"
WINDOW = "@3"
PANE_PID = 4242
GENERATION = "gen-7"
# Canonical already, so a mismatch in a failing test is a real one.
SOCKET = "/private/tmp/tmux-501/cao-test"
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
    """A tmux client offering exactly one way to write, and no fallback."""

    def __init__(self, identities=None):
        self._identities = list(identities or [FakePaneIdentity()])
        self.on_write = None
        self.writes = []
        self._guard = threading.Lock()

    def pane_control_identity(
        self,
        *,
        pane_id=None,
        session_name=None,
        window_name=None,
        deadline_monotonic=None,
    ):
        if len(self._identities) > 1:
            return self._identities.pop(0)
        return self._identities[0]

    # Keyword-only and undefaulted, mirroring the real primitive.
    def send_literal_line(
        self,
        pane_id,
        text,
        submit=True,
        *,
        expected_server_identity,
        deadline_monotonic=None,
    ):
        if self.on_write is not None:
            self.on_write()
        with self._guard:
            self.writes.append(
                {
                    "pane_id": pane_id,
                    "text": text,
                    "submit": submit,
                    "expected_server_identity": expected_server_identity,
                }
            )
        return 1

    # The cond-0178 copy-mode guard primitives; default "not in copy mode"
    # so no exit control is recorded for a test that does not ask for one.
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
        with self._guard:
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
def _isolated_state(monkeypatch, tmp_path):
    """Pane locks and the journal follow the test's state root, not the host's."""
    monkeypatch.setattr("cli_agent_orchestrator.constants.CAO_HOME_DIR", str(tmp_path / "state"))
    reset_pane_input_arbiter()
    service.reset_control_input_journal()
    yield
    reset_pane_input_arbiter()
    service.reset_control_input_journal()


@pytest.fixture(autouse=True)
def _clear_scope_overrides():
    yield
    app.dependency_overrides.pop(auth.get_current_scopes, None)


@pytest.fixture
def auth_on(monkeypatch):
    """Turn the auth layer on; with it off the dependency enforces nothing."""
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")


@pytest.fixture
def tmux(monkeypatch):
    """A tmux backend where exactly one terminal exists.

    Keyed by terminal id rather than answering for every id, so the
    unknown-terminal path is exercised through the same wiring as the
    known one instead of through a differently-patched world.
    """
    client = FakeTmux()
    monkeypatch.setattr(service, "_tmux_client", lambda: client)
    monkeypatch.setattr(
        service,
        "_terminal_metadata",
        lambda terminal_id: _metadata() if terminal_id == TERMINAL else None,
    )
    monkeypatch.setattr(service, "_managed_identity", lambda terminal_id: None)
    return client


def _post(client, *, terminal=TERMINAL, **body):
    payload = {"control_id": CONTROL, "text": TEXT}
    payload.update(body)
    return client.post(f"/terminals/{terminal}/control-input", json=payload)


def _grant(*scopes):
    async def _dep():
        return list(scopes)

    app.dependency_overrides[auth.get_current_scopes] = _dep


def _dead_pid():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    return child.pid


@contextmanager
def _pane_held_elsewhere(pane_id=PANE):
    """Hold the pane lease from another thread.

    Another thread is required: the lease is non-reentrant by design, so
    holding it on this one would raise a reentry error rather than
    produce the busy refusal under test.
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


class TestCapabilityAdvertisement:
    """Support is discoverable without typing anything into a composer."""

    def test_the_capability_document_states_what_this_surface_promises(self, client):
        response = client.get("/control-input/capabilities")
        assert response.status_code == 200
        body = response.json()
        assert body["protocol"] == CONTROL_INPUT_PROTOCOL
        assert body["request_schema_version"] == CONTROL_INPUT_REQUEST_SCHEMA_VERSION
        assert body["digest_domain"] == CONTROL_INPUT_DIGEST_DOMAIN
        assert body["identity_fields"] == list(IDENTITY_FIELDS)
        assert set(body["outcomes"]) == set(CONTROL_INPUT_OUTCOMES)
        # The three promises a caller would otherwise have to infer from
        # behaviour — which it can only observe by sending a control.
        assert body["literal_write"] is True
        assert body["bracketed_paste"] is False
        assert body["enter_required"] is True

    def test_the_probe_is_answerable_by_a_caller_that_may_not_write(self, client, auth_on):
        """Otherwise support could only be discovered by attempting a
        delivery, and a successful probe has already typed something."""
        _grant()
        response = client.get("/control-input/capabilities")
        assert response.status_code == 200
        assert response.json()["protocol"] == CONTROL_INPUT_PROTOCOL

    def test_the_capability_route_outranks_the_lookup_route(self, client):
        """``capabilities`` is a legal control id, so declaration order is
        what keeps the probe from becoming a lookup.

        A reordering would answer the probe with a refusal document that
        also carries a ``protocol`` key — close enough to fool a client
        checking only that field, while silently reporting on a control
        nobody sent.
        """
        body = client.get("/control-input/capabilities").json()
        assert "max_text_bytes" in body
        assert "outcome" not in body


class TestV2ChordDiscovery:
    """A conductor that needs v2 reads support before sending a chord, because
    a v2 request against a v1 server would otherwise be silently delivered as
    text without the chord (pydantic ignores unknown fields)."""

    def test_the_capability_document_advertises_v2_and_the_chord_allowlist(self, client):
        body = client.get("/control-input/capabilities").json()
        # v1 stays the named default; v2, v3 and v4 are advertised alongside it.
        assert body["request_schema_version"] == CONTROL_INPUT_REQUEST_SCHEMA_VERSION
        assert body["request_schema_versions"] == [1, 2, 3, 4]
        assert body["digest_domain"] == CONTROL_INPUT_DIGEST_DOMAIN
        # The steer-chord allowlist is truthful: only the pinned Kimi chord.
        assert body["steer_chords"] == {"kimi_cli": ["C-s"]}
        # The v3 sequence surface states its exact representable forms.
        assert body["sequence"]["event_types"] == ["chord", "key", "text"]
        # The §3.2 key set (advertised order is sorted and non-normative).
        assert set(body["sequence"]["keys"]) == {
            "Escape",
            "C-c",
            "C-s",
            "Enter",
            "Backspace",
            "Up",
            "Down",
            "Left",
            "Right",
            "Home",
            "End",
            "PageUp",
            "PageDown",
            "Delete",
            "Insert",
            "Tab",
        }
        assert body["sequence"]["max_events"] == 32
        assert body["sequence"]["max_text_bytes"] == 512

    def test_the_identity_route_advertises_the_control_input_block(self, client, tmux):
        body = client.get(f"/terminals/{TERMINAL}/control-identity").json()
        block = body["control_input"]
        assert block["schema_versions"] == [1, 2, 3, 4]
        assert block["chords"] == {"kimi_cli": ["C-s"]}
        assert set(block["sequence"]["keys"]) == {
            "Escape",
            "C-c",
            "C-s",
            "Enter",
            "Backspace",
            "Up",
            "Down",
            "Left",
            "Right",
            "Home",
            "End",
            "PageUp",
            "PageDown",
            "Delete",
            "Insert",
            "Tab",
        }

    def test_the_identity_route_block_is_absent_on_an_unknown_terminal(self, client, tmux):
        # No body to inspect on a 404; the block is only on a resolved terminal.
        assert client.get(f"/terminals/{UNKNOWN_TERMINAL}/control-identity").status_code == 404


class TestSendingAControl:
    """The happy path, and what the wire says about it."""

    def test_the_text_is_typed_literally_and_submitted(self, client, tmux):
        response = _post(client)
        assert response.status_code == 200
        body = response.json()
        assert body["outcome"] == ACCEPTED
        assert body["reason_code"] is None
        assert body["in_flight"] is False
        assert body["text_sent"] is True
        assert body["enter_sent"] is True
        assert body["chunks_sent"] == 1
        assert body["enter_attempted"] is True
        assert tmux.writes == [
            {
                "pane_id": PANE,
                "text": TEXT,
                "submit": True,
                # The bound server reaches the write primitive across the
                # HTTP boundary too, so a request that crossed the wire is
                # no less pinned to one server than a direct call.
                "expected_server_identity": SOCKET,
            }
        ]

    def test_no_paste_framing_reaches_the_pane(self, client, tmux):
        _post(client)
        written = tmux.writes[0]["text"]
        assert written == TEXT
        assert not contains_bracketed_paste_sentinel(written)
        assert BRACKETED_PASTE_START not in written
        assert BRACKETED_PASTE_END not in written

    def test_enter_false_is_carried_through_and_reported(self, client, tmux):
        """The submit is the irreversible half; the wire must not round it
        up to a default."""
        body = _post(client, enter=False).json()
        assert body["outcome"] == ACCEPTED
        assert body["enter_sent"] is False
        assert body["enter_attempted"] is False
        assert tmux.writes[0]["submit"] is False

    def test_the_response_names_the_target_it_actually_wrote_to(self, client, tmux):
        body = _post(client).json()
        assert body["terminal_id"] == TERMINAL
        assert body["resolved_identity"]["pane"]["pane_id"] == PANE
        assert body["resolved_identity"]["pane"]["window_id"] == WINDOW
        assert body["resolved_identity"]["pane_birth_id"] == PANE

    def test_the_wire_digest_is_the_contract_digest(self, client, tmux):
        """Computed independently here from the request the caller sent.

        This is the cross-implementation binding at the boundary: if the
        server ever digested something other than the canonical preimage,
        a client's own comparison would start failing on every control.
        """
        body = _post(client).json()
        assert body["request_digest"] == control_input_request_digest(
            control_id=CONTROL, text=TEXT, enter=True, expected_identity=None
        )

    def test_a_screened_payload_is_refused_at_200_with_nothing_written(self, client, tmux):
        body = _post(client, text=f"{BRACKETED_PASTE_START}/compact").json()
        assert body["outcome"] == REFUSED
        assert body["reason_code"] == REASON_ILLEGAL_CONTROL_BYTES
        assert tmux.writes == []


class TestEnterStatedAsNull:
    """An explicit ``"enter": null`` is not the omitted field.

    Pydantic parses an omission and an explicit null to the same ``None``,
    but they are different requests on the v1/v2 wire: the omission carries
    the v1 default (submit), while the explicit null failed validation at
    F1 — ``enter`` was a non-Optional bool — and keeps failing rather than
    silently becoming ``enter=true``.  Raw field presence, consulted at
    the edge, is what still tells the two apart.
    """

    def test_an_explicit_null_enter_is_a_validation_error(self, client, tmux):
        response = _post(client, enter=None)
        assert response.status_code == 422
        assert "enter" in response.json()["detail"]
        assert tmux.writes == []

    def test_an_omitted_enter_keeps_the_v1_default(self, client, tmux):
        body = _post(client).json()
        assert body["outcome"] == ACCEPTED
        assert body["enter_sent"] is True
        assert tmux.writes[0]["submit"] is True

    def test_a_stated_null_enter_beside_events_is_refused(self, client, tmux):
        # The v3 either/or rule stays strict on stated fields: a nulled
        # ``enter`` beside ``events`` is a stated v1/v2 field, refused as
        # ambiguous intent rather than resolved by precedence.
        response = client.post(
            f"/terminals/{TERMINAL}/control-input",
            json={
                "control_id": CONTROL,
                "events": [{"type": "key", "key": "Escape"}],
                "enter": None,
            },
        )
        assert response.status_code == 422
        assert tmux.writes == []


class TestStatusDiscipline:
    """Which failures are typed 200s and which are transport-level errors."""

    def test_an_unknown_terminal_is_a_typed_refusal_not_a_404(self, client, tmux):
        """The single most important status decision on this surface.

        A ``404`` here would be indistinguishable from a server with no
        control route, and the two demand opposite actions: re-attempt
        against a working server, versus stop because none is possible.
        """
        response = _post(client, terminal=UNKNOWN_TERMINAL)
        assert response.status_code == 200
        body = response.json()
        assert body["outcome"] == REFUSED
        assert body["reason_code"] == REASON_UNKNOWN_TERMINAL
        # Re-attemptable because it is provable, not because retrying this
        # terminal would help: a refusal is the one outcome that proves
        # zero bytes reached any pane.
        assert body["reattemptable"] is True
        assert tmux.writes == []

    def test_no_terminal_level_failure_answers_404(self, client, tmux):
        """Every one of these is a fact about a terminal, not about the
        route's existence, so none may borrow the route-absent signal."""
        cases = [
            {"terminal": UNKNOWN_TERMINAL},
            {"text": f"/compact{BRACKETED_PASTE_END}"},
            {"expected_identity": {"terminal_generation": "gen-stale"}},
        ]
        for case in cases:
            response = _post(client, **case)
            assert response.status_code == 200, case
            assert response.json()["outcome"] in CONTROL_INPUT_OUTCOMES, case

    def test_a_malformed_control_id_is_rejected_before_any_outcome_exists(self, client, tmux):
        """No typed outcome could honestly describe a request this server
        cannot even key, so it is a request error rather than a refusal."""
        response = _post(client, control_id="not a valid id")
        assert response.status_code == 422
        assert tmux.writes == []

    def test_text_over_the_limit_is_a_request_error(self, client, tmux):
        response = _post(client, text="x" * (service.MAX_TEXT_BYTES + 1))
        assert response.status_code == 422
        assert tmux.writes == []

    def test_an_unbounded_wait_cannot_be_requested(self, client, tmux):
        """The bound is what keeps a truthful "busy, nothing written, try
        again" from becoming a request that never answers."""
        response = _post(client, lease_timeout=30.0)
        assert response.status_code == 422
        assert tmux.writes == []

    def test_a_malformed_terminal_id_never_reaches_the_service(self, client, tmux):
        response = _post(client, terminal="not-a-terminal")
        assert response.status_code == 422
        assert tmux.writes == []


class TestStaleOrWrongIdentity:
    """A control aimed at a terminal that has been replaced is refused."""

    def test_a_stale_generation_is_refused_before_the_first_byte(self, client, tmux):
        body = _post(client, expected_identity={"terminal_generation": "gen-1"}).json()
        assert body["outcome"] == REFUSED
        assert body["reason_code"] == REASON_STALE_GENERATION
        assert tmux.writes == []

    def test_a_wrong_pane_birth_id_is_refused(self, client, tmux):
        body = _post(client, expected_identity={"pane_birth_id": "%99"}).json()
        assert body["outcome"] == REFUSED
        assert body["reason_code"] == REASON_IDENTITY_MISMATCH
        assert tmux.writes == []

    def test_a_dead_pane_is_refused(self, client, monkeypatch):
        dead = FakeTmux([FakePaneIdentity(dead=True)])
        monkeypatch.setattr(service, "_tmux_client", lambda: dead)
        monkeypatch.setattr(service, "_terminal_metadata", lambda t: _metadata())
        monkeypatch.setattr(service, "_managed_identity", lambda t: None)
        body = _post(client).json()
        assert body["outcome"] == REFUSED
        assert body["reason_code"] == REASON_PANE_DEAD
        assert dead.writes == []

    def test_a_pane_replaced_after_resolution_is_caught_under_the_lease(self, client, monkeypatch):
        """The re-verification is the whole point of taking the lease
        first; the route must not bypass it by trusting the earlier read.

        The first identity read resolves the target, the second happens
        with the lease held — and reports a different pane process, which
        is what a terminal replaced in the interval looks like.
        """
        swapped = FakeTmux(
            [FakePaneIdentity(), FakePaneIdentity(pane_pid=PANE_PID + 1)],
        )
        monkeypatch.setattr(service, "_tmux_client", lambda: swapped)
        monkeypatch.setattr(service, "_terminal_metadata", lambda t: _metadata())
        monkeypatch.setattr(service, "_managed_identity", lambda t: None)
        body = _post(client).json()
        assert body["outcome"] == REFUSED
        assert body["reason_code"] == REASON_IDENTITY_MISMATCH
        assert swapped.writes == []


class TestControlIdentity:
    """Where a caller learns the identity it is allowed to declare."""

    def test_it_reports_the_declarable_identity_and_the_live_pane(self, client, tmux):
        response = client.get(f"/terminals/{TERMINAL}/control-identity")
        assert response.status_code == 200
        body = response.json()
        assert body["terminal_id"] == TERMINAL
        assert body["terminal_generation"] == GENERATION
        assert body["pane_birth_id"] == PANE
        assert body["pane"] == {
            "pane_id": PANE,
            "window_id": WINDOW,
            "pane_pid": PANE_PID,
            "dead": False,
            "bound_server_socket_path": SOCKET,
            "observed_server_socket_path": SOCKET,
        }

    def test_its_view_can_be_declared_back_and_accepted(self, client, tmux):
        """A round trip: whatever this route reports must be an
        expectation the delivery path will honour, or the two surfaces
        disagree about the same terminal."""
        identity = client.get(f"/terminals/{TERMINAL}/control-identity").json()
        declared = {field: identity[field] for field in IDENTITY_FIELDS}
        body = _post(client, expected_identity=declared).json()
        assert body["outcome"] == ACCEPTED

    def test_an_unknown_terminal_is_a_404_here_because_nothing_is_delivered(self, client, tmux):
        """A pure lookup may use ``404``: both readings — no such terminal,
        or no such route — lead to the same action, which is not to send.

        Support is not probed here for exactly that reason; the capability
        document is the unambiguous signal.
        """
        response = client.get(f"/terminals/{UNKNOWN_TERMINAL}/control-identity")
        assert response.status_code == 404


class TestScopeEnforcement:
    def test_a_read_token_may_not_type_into_a_pane(self, client, tmux, auth_on):
        _grant(auth.SCOPE_READ)
        assert _post(client).status_code == 403
        assert tmux.writes == []

    def test_a_write_token_may(self, client, tmux, auth_on):
        _grant(auth.SCOPE_WRITE)
        assert _post(client).status_code == 200

    def test_a_read_token_may_reconcile_a_lost_response(self, client, tmux, auth_on):
        """Reconciliation must not require write scope: a caller holding
        only read scope can still be the one that needs to find out what
        happened."""
        _grant(auth.SCOPE_READ)
        response = client.get(f"/control-input/{CONTROL}")
        assert response.status_code == 200

    def test_a_read_token_may_resolve_an_identity(self, client, tmux, auth_on):
        _grant(auth.SCOPE_READ)
        assert client.get(f"/terminals/{TERMINAL}/control-identity").status_code == 200


class TestAtMostOnceOverTheWire:
    """One control id types once, however many times it is asked."""

    def test_a_repeated_post_replays_the_first_answer_and_writes_once(self, client, tmux):
        first = _post(client).json()
        second = _post(client).json()
        assert first["outcome"] == ACCEPTED
        assert second["outcome"] == ACCEPTED
        assert second["request_digest"] == first["request_digest"]
        assert second["chunks_sent"] == first["chunks_sent"]
        assert len(tmux.writes) == 1

    def test_a_lost_response_is_resolved_by_asking_not_by_resending(self, client, tmux):
        """The reconciliation path exists so that a dropped reply never
        forces the caller to choose between a duplicate and a lost
        control."""
        sent = _post(client).json()
        # The caller never saw the reply above; it asks instead.
        looked_up = client.get(f"/control-input/{CONTROL}")
        assert looked_up.status_code == 200
        body = looked_up.json()
        assert body["outcome"] == ACCEPTED
        assert body["control_id"] == CONTROL
        assert body["terminal_id"] == TERMINAL
        assert body["request_digest"] == sent["request_digest"]
        assert body["enter_sent"] is True
        assert len(tmux.writes) == 1

    def test_the_same_id_bound_to_different_text_is_refused_not_retyped(self, client, tmux):
        """A replayed id carrying other bytes is a different control, and
        the first answer does not describe it."""
        _post(client)
        body = _post(client, text="/clear").json()
        assert body["outcome"] == REFUSED
        assert len(tmux.writes) == 1

    def test_an_unknown_control_id_proves_nothing_was_written(self, client, tmux):
        """Not a guess: the intent is committed before the first byte, so
        the absence of a record is positive evidence of no write."""
        response = client.get("/control-input/ctl-never-sent")
        assert response.status_code == 200
        body = response.json()
        assert body["outcome"] == REFUSED
        assert body["reason_code"] == REASON_OWNER_LOST_BEFORE_WRITE
        assert body["reattemptable"] is True

    def test_a_malformed_id_cannot_be_looked_up(self, client, tmux):
        assert client.get("/control-input/not%20an%20id").status_code == 422


class TestConcurrencyAtTheBoundary:
    """One pane, one writer, enforced under real threads."""

    def test_a_busy_pane_refuses_without_writing(self, client, tmux):
        with _pane_held_elsewhere():
            response = _post(client)
        assert response.status_code == 200
        body = response.json()
        assert body["outcome"] == REFUSED
        assert body["reason_code"] == REASON_PANE_BUSY
        assert tmux.writes == []

    def test_a_busy_refusal_may_be_re_attempted_once_the_pane_frees(self, client, tmux):
        """``reattemptable: true`` has to be true in practice.

        A stored refusal that replayed forever would make one momentarily
        busy pane permanently poison a control id, while the caller's own
        model says a refusal may be retried.
        """
        with _pane_held_elsewhere():
            busy = _post(client).json()
        assert busy["outcome"] == REFUSED
        retried = _post(client).json()
        assert retried["outcome"] == ACCEPTED
        assert len(tmux.writes) == 1

    @contextmanager
    def _mid_write(self, client, tmux, control_id):
        """Suspend one request inside the write and let another arrive.

        The overlap is arranged rather than raced: the first request is
        held between taking the lease and returning, so the second is
        guaranteed to arrive while the pane is genuinely being written.
        A sleep-based race would pass just as happily when the two never
        overlapped at all.
        """
        writing, resume = threading.Event(), threading.Event()
        answer = {}

        def hold():
            writing.set()
            assert resume.wait(10), "the second request never finished"

        tmux.on_write = hold

        def send():
            answer["body"] = _post(client, control_id=control_id).json()

        worker = threading.Thread(target=send, daemon=True)
        worker.start()
        assert writing.wait(10), "the first request never reached the write"
        try:
            yield answer
        finally:
            resume.set()
            worker.join(30)

    def test_a_second_control_arriving_mid_write_is_refused_without_bytes(self, client, tmux):
        """Two controls, one pane, one writer at a time.

        The loser is told the pane is busy — a refusal, which proves zero
        bytes and licenses a re-attempt — rather than being queued behind
        a write whose duration it cannot know.
        """
        with self._mid_write(client, tmux, "ctl-race-first") as first:
            loser = _post(client, control_id="ctl-race-second").json()

        assert loser["outcome"] == REFUSED
        assert loser["reason_code"] == REASON_PANE_BUSY
        assert loser["text_sent"] is False
        assert first["body"]["outcome"] == ACCEPTED
        assert len(tmux.writes) == 1
        assert tmux.writes[0]["text"] == TEXT

    def test_the_same_id_arriving_mid_write_is_never_typed_twice(self, client, tmux):
        """A retry that overtakes its own first attempt.

        It cannot be told "refused" — that would license a re-send of a
        control currently being written — and it cannot be told
        "accepted" by a claim it does not hold.  ``in_flight`` is the
        only answer that is true.
        """
        with self._mid_write(client, tmux, CONTROL) as first:
            overlapping = _post(client).json()

        assert overlapping["outcome"] is None
        assert overlapping["in_flight"] is True
        assert overlapping["text_sent"] is False
        assert first["body"]["outcome"] == ACCEPTED
        assert len(tmux.writes) == 1


class TestCrashWindowOverTheWire:
    """A request whose owner died is answerable by asking."""

    def _stranded(self, *, claimed, request_sha256=None):
        """Leave a record behind that no live process owns.

        The digest defaults to the one the endpoint's own request would
        produce, so the re-arrival is byte-identical and the record is
        replayed rather than rejected as a rebinding.
        """
        stale = ControlInputJournal(service.control_input_journal_path(), owner_pid=_dead_pid())
        stale.open_intent(
            ControlInputBinding(
                request_id=CONTROL,
                terminal_id=TERMINAL,
                pane_id=PANE,
                window_id=WINDOW,
                pane_pid=PANE_PID,
                generation=GENERATION,
                # Must match what the endpoint's own request would bind,
                # or the re-arrival is a rebinding instead of the replay
                # these crash-window tests are about.
                server_socket_path=SOCKET,
                request_sha256=request_sha256
                or control_input_request_digest(
                    control_id=CONTROL, text=TEXT, enter=True, expected_identity=None
                ),
            )
        )
        if claimed:
            stale.claim_write(CONTROL)

    def test_a_death_after_the_claim_reads_ambiguous(self, client, tmux):
        """The owner held the right to write and may have used it; no
        durable fact says whether it did."""
        self._stranded(claimed=True)
        body = client.get(f"/control-input/{CONTROL}").json()
        assert body["outcome"] == AMBIGUOUS
        assert body["reason_code"] == REASON_OWNER_LOST_MID_WRITE
        assert body["in_flight"] is False
        assert body["reattemptable"] is False

    def test_a_death_before_the_claim_reads_refused(self, client, tmux):
        """It never reached the claim, so the pane was never touched."""
        self._stranded(claimed=False)
        body = client.get(f"/control-input/{CONTROL}").json()
        assert body["outcome"] == REFUSED
        assert body["reason_code"] == REASON_OWNER_LOST_BEFORE_WRITE
        assert body["reattemptable"] is True

    def test_an_ambiguous_control_is_not_retyped_by_a_second_post(self, client, tmux):
        """The terminal outcome stands: re-sending is exactly the
        duplicate the ambiguous answer refuses to license."""
        self._stranded(claimed=True)
        body = _post(client).json()
        assert body["outcome"] == AMBIGUOUS
        assert body["reason_code"] == REASON_OWNER_LOST_MID_WRITE
        assert tmux.writes == []

    def test_a_stranded_id_reused_for_other_bytes_is_rebound_not_replayed(self, client, tmux):
        """A different control wearing a used id must not inherit that
        id's answer.

        Refused rather than ambiguous, and truthfully so: this request's
        own digest never reached the journal, so nothing of *these* bytes
        was written, whatever happened to the earlier ones.
        """
        self._stranded(claimed=True, request_sha256="a" * 64)
        body = _post(client).json()
        assert body["outcome"] == REFUSED
        assert body["reason_code"] == REASON_REQUEST_REBOUND
        assert tmux.writes == []


class TestProtocolCompatibility:
    """Old and new on either side, and never a fallback between them."""

    def test_an_unknown_protocol_answers_unsupported_and_writes_nothing(self, client, tmux):
        response = _post(client, protocol="cao-control-input-v99")
        assert response.status_code == 422
        body = response.json()
        assert body["outcome"] == UNSUPPORTED
        assert body["reason_code"] == REASON_PROTOCOL_MISMATCH
        assert tmux.writes == []

    def test_that_422_is_distinguishable_from_a_field_error(self, client, tmux):
        """Both are ``422``; only one carries a typed body.

        A client that told ``classify_transport_status`` it was a protocol
        rejection gets ``unsupported`` — stop — while a body-shape ``422``
        stays a request error the caller can fix.
        """
        mismatch = _post(client, protocol="cao-control-input-v99")
        assert (
            classify_transport_status(mismatch.status_code, protocol_mismatch=True) == UNSUPPORTED
        )
        field_error = _post(client, control_id="not a valid id")
        assert field_error.status_code == 422
        assert "outcome" not in field_error.json()

    def test_the_current_protocol_is_accepted(self, client, tmux):
        body = _post(client, protocol=CONTROL_INPUT_PROTOCOL).json()
        assert body["outcome"] == ACCEPTED

    def test_a_200_body_is_authoritative_and_not_second_guessed(self, client, tmux):
        """The transport classifier must defer to the typed outcome on
        ``200``; otherwise a refusal and an acceptance would be read the
        same way."""
        response = _post(client)
        assert classify_transport_status(response.status_code) is None
        assert response.json()["outcome"] == ACCEPTED

    def test_a_new_client_against_an_old_server_reads_unsupported(self):
        """A server predating this protocol has no such routes.

        Its ``404`` resolves to ``unsupported``, which is not
        re-attemptable — so nothing about it can license a downgrade to
        ordinary paste or raw keys, even though the legacy input route
        sitting right next to it would happily accept the text.
        """
        old_server = FastAPI()

        @old_server.post("/terminals/{terminal_id}/input")
        async def _legacy_input(terminal_id: str, message: str) -> dict:
            return {"success": True}

        legacy = TestClient(old_server)
        probe = legacy.get("/control-input/capabilities")
        assert probe.status_code == 404
        assert classify_transport_status(probe.status_code) == UNSUPPORTED

        send = legacy.post(
            f"/terminals/{TERMINAL}/control-input",
            json={"control_id": CONTROL, "text": TEXT, "enter": True},
        )
        assert send.status_code == 404
        assert classify_transport_status(send.status_code) == UNSUPPORTED
        assert is_reattemptable(UNSUPPORTED) is False

    def test_an_old_client_against_this_server_still_works(self, client, tmux):
        """A caller that omits every optional field — no protocol, no
        digest, no expectation — is a valid caller, not a degraded one."""
        response = client.post(
            f"/terminals/{TERMINAL}/control-input",
            json={"control_id": CONTROL, "text": TEXT},
        )
        assert response.status_code == 200
        assert response.json()["outcome"] == ACCEPTED
        assert tmux.writes[0]["submit"] is True


# --- cond-0026: the submission observation on the wire -------------------------


class _FakeCodexComposer:
    """The composer the capture fake serves, defect included on demand."""

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


class _CodexFakeTmux(FakeTmux):
    def __init__(self, composer):
        super().__init__()
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


@pytest.fixture
def codex(monkeypatch):
    """One Codex terminal behind the app, with a test-speed barrier."""
    composer = _FakeCodexComposer()
    tmux_client = _CodexFakeTmux(composer)
    monkeypatch.setattr(service, "_tmux_client", lambda: tmux_client)
    monkeypatch.setattr(
        service,
        "_terminal_metadata",
        lambda terminal_id: _metadata(provider="codex") if terminal_id == TERMINAL else None,
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
    return SimpleNamespace(client=tmux_client, composer=composer)


class TestSubmissionObservationOnTheWire:
    """The typed body carries the observation; the wire never has to guess."""

    def test_a_codex_control_reports_submission_observed(self, client, codex):
        response = _post(client)

        assert response.status_code == 200
        body = response.json()
        assert body["outcome"] == ACCEPTED
        assert body["submission_observed"] == "submitted"
        assert body["submission_evidence_ref"].startswith(f"capture-pane:{PANE}:")
        # One text write and exactly one Enter, in that order.
        assert [write["submit"] for write in codex.client.writes] == [False, True]

    def test_the_lookup_route_reports_the_same_stored_observation(self, client, codex):
        posted = _post(client).json()
        assert posted["outcome"] == ACCEPTED

        looked_up = client.get(f"/control-input/{CONTROL}")
        assert looked_up.status_code == 200
        body = looked_up.json()
        assert body["outcome"] == ACCEPTED
        assert body["submission_observed"] == "submitted"
        assert body["submission_evidence_ref"] == posted["submission_evidence_ref"]

    def test_an_unsubmitted_codex_control_is_ambiguous_on_the_wire(self, client, codex):
        codex.composer.swallow_enter = True

        body = _post(client).json()
        assert body["outcome"] == AMBIGUOUS
        assert body["reason_code"] == "submission-unproven"
        assert body["submission_observed"] == "unsubmitted"
        assert [write["submit"] for write in codex.client.writes] == [False, True]

    def test_a_non_codex_control_projects_the_typed_null(self, client, tmux):
        """Keys always present, values null: absence is never ambiguous."""
        body = _post(client).json()
        assert body["outcome"] == ACCEPTED
        assert "submission_observed" in body
        assert body["submission_observed"] is None
        assert "submission_evidence_ref" in body
        assert body["submission_evidence_ref"] is None


# --- §3.5/§4.1: additive capability blocks and the v4 declaration carrier -----

from cli_agent_orchestrator.services.control_input_contract import (  # noqa: E402
    CONTROL_INPUT_REQUEST_SCHEMA_VERSION_V4,
    control_input_request_digest_v4,
)


def _kimi_resolved(**overrides):
    fields = {
        "terminal_id": TERMINAL,
        "terminal_incarnation": None,
        # A managed generation is a UUID: the readiness gate derives the
        # managed window name from it and refuses anything else.
        "terminal_generation": "11111111-2222-3333-4444-555555555555",
        "provider": "kimi_cli",
        "native_session_id": "sess-1",
        "execution_mode": service.EXECUTION_MODE_NATIVE_TUI,
        "session_name": "cao",
        "provider_process_id": "4242@boot-1",
        "provider_version": "0.29.2",
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


class TestAdditiveCapabilities:
    """The §3.5 blocks are additive: every deployed key is unchanged, and
    the new keys ride alongside — old clients ignore what they do not know."""

    def test_the_streaming_block_is_advertised_with_server_owned_policy(self, client):
        body = client.get("/control-input/capabilities").json()
        assert body["streaming"] == {
            "supported": True,
            "max_in_flight": 1,
            "coalesce_window_ms": 200,
        }

    def test_the_provider_controls_block_is_the_discovery_union(self, client):
        body = client.get("/control-input/capabilities").json()
        controls = body["provider_controls"]
        assert set(controls) == {"codex", "kimi_cli", "claude_code"}
        assert controls["kimi_cli"]["compact"] == {
            "events": [{"type": "text", "text": "/compact"}, {"type": "key", "key": "Enter"}]
        }
        assert controls["kimi_cli"]["stop"] == {"events": [{"type": "key", "key": "Escape"}]}
        assert controls["kimi_cli"]["steer_chords"] == ["C-s"]
        assert controls["kimi_cli"]["dispatch_grace_ms"] == 5000
        assert controls["claude_code"]["compact"] == controls["kimi_cli"]["compact"]
        assert controls["claude_code"]["stop"] == controls["kimi_cli"]["stop"]
        assert controls["claude_code"]["steer_chords"] == []
        # No grace for Claude: providers without one omit the key (§3.5).
        assert "dispatch_grace_ms" not in controls["claude_code"]
        assert controls["codex"]["compact"] == controls["kimi_cli"]["compact"]
        assert controls["codex"]["stop"] == controls["kimi_cli"]["stop"]
        assert controls["codex"]["steer_chords"] == []
        assert "dispatch_grace_ms" not in controls["codex"]

    def test_the_command_controls_block_is_advertised(self, client):
        body = client.get("/control-input/capabilities").json()
        assert body["command_controls"] == {"composer_nonempty_guard": True}

    def test_the_deployed_capability_keys_are_unchanged(self, client):
        """The additive-only proof: every key the deployed document carried
        is still present with its deployed value (the §10.1 golden diff)."""
        body = client.get("/control-input/capabilities").json()
        assert body["protocol"] == CONTROL_INPUT_PROTOCOL
        assert body["request_schema_version"] == 1
        assert body["digest_domain"] == CONTROL_INPUT_DIGEST_DOMAIN
        assert body["steer_chords"] == {"kimi_cli": ["C-s"]}
        assert body["identity_fields"] == list(IDENTITY_FIELDS)
        assert set(body["outcomes"]) == set(CONTROL_INPUT_OUTCOMES)
        assert body["max_text_bytes"] == 512
        assert body["literal_write"] is True
        assert body["bracketed_paste"] is False
        assert body["enter_required"] is True
        assert body["server_identity_bound"] is True
        assert body["execution_modes"] == ["native_tui"]
        assert "malformed-command-declaration" in body["reason_codes"]
        assert "composer-nonempty" in body["reason_codes"]

    def test_the_per_terminal_block_is_the_build_exact_send_authority(self, client, monkeypatch):
        """§3.5: the identity route's block resolves this terminal's provider
        at this terminal's build — its steer_chords is the exact set the
        server would admit for this pane, and the guard availability is the
        honest per-build fact."""
        monkeypatch.setattr(service, "resolve_control_identity", lambda tid: _kimi_resolved())
        body = client.get(f"/terminals/{TERMINAL}/control-identity").json()
        block = body["control_input"]
        assert block["provider_controls"] == {
            "kimi_cli": {
                "compact": {
                    "events": [
                        {"type": "text", "text": "/compact"},
                        {"type": "key", "key": "Enter"},
                    ]
                },
                "stop": {"events": [{"type": "key", "key": "Escape"}]},
                "steer_chords": ["C-s"],
                "dispatch_grace_ms": 5000,
                # §8.6 additive Lane C blocks (build-exact like steer chords).
                "operator_message": {
                    "supported": True,
                    "max_text_bytes": 8192,
                    "multiline": True,
                    "max_attachments": 4,
                },
                "image": {
                    "supported": True,
                    "formats": ["png"],
                    "max_bytes": 5242880,
                    "max_width": 8000,
                    "max_height": 8000,
                    "mechanism": "staged-path-text",
                    "reference_template": (
                        "Use the ReadMediaFile tool to read the image file at "
                        "{path} and analyze it in the context of this message."
                    ),
                    "evidence": "live acceptance on pinned 0.29.2 (§10.6)",
                },
                # §6.7 (r15): the build-exact interactive-streaming send
                # authority for this terminal's pinned build.
                "interactive_streaming": {"supported": True},
            }
        }
        assert block["command_controls"] == {"composer_nonempty_guard": True}

    def test_the_per_terminal_block_on_an_unpinned_build_admits_no_chords(
        self, client, monkeypatch
    ):
        """Build-exact, never the union: a kimi build outside the pinned
        table advertises an empty chord set and no emptiness guard, so the
        client refuses locally with zero POSTs (D9)."""
        monkeypatch.setattr(
            service,
            "resolve_control_identity",
            lambda tid: _kimi_resolved(provider_version="9.9.9"),
        )
        block = client.get(f"/terminals/{TERMINAL}/control-identity").json()["control_input"]
        assert block["provider_controls"]["kimi_cli"]["steer_chords"] == []
        assert block["command_controls"] == {"composer_nonempty_guard": False}
        # §8.6: the Lane C blocks fail closed with the chords — an unproven
        # build advertises no message/image capability (omitted, not nulled).
        assert "operator_message" not in block["provider_controls"]["kimi_cli"]
        assert "image" not in block["provider_controls"]["kimi_cli"]

    def test_a_provider_without_a_registry_entry_has_no_controls_block(self, client, monkeypatch):
        monkeypatch.setattr(
            service,
            "resolve_control_identity",
            lambda tid: _kimi_resolved(provider="opencode", provider_version="1.2.3"),
        )
        block = client.get(f"/terminals/{TERMINAL}/control-identity").json()["control_input"]
        assert "provider_controls" not in block
        assert block["command_controls"] == {"composer_nonempty_guard": False}


class _FakeSequenceAdapter:
    """An adapter that executes plans through the transport, recording calls."""

    class ComposerWriteInterrupted(Exception):
        def __init__(self, detail):
            super().__init__(detail)
            self.detail = detail

    def __init__(self):
        self.calls = []

    def execute_composer_plan(self, *, plan, transport, submit, deadline_monotonic=None):
        self.calls.append((plan, submit))
        transport.send_literal(plan["lines"][0])
        if submit:
            transport.send_enter()


class TestCommandClassOverTheWire:
    """The v4 declaration carrier at the HTTP boundary (§4.1): declared
    commands run the guard; undeclared payloads are prose, byte-identical
    to before the carrier existed."""

    def _wire_managed_kimi(self, monkeypatch, *, empty, execution_close=None):
        from cli_agent_orchestrator.services import managed_launch_v2
        from cli_agent_orchestrator.services.control_input_contract import SUBMISSION_SUBMITTED

        # A fresh server, dispatch-wise: an earlier test's accepted Enter
        # would otherwise sit inside its grace and answer pane-busy where
        # this test means to exercise the composer guard.
        with service._native_kimi_dispatch_guard_lock:
            service._native_kimi_dispatch_times.clear()
        adapter = _FakeSequenceAdapter()
        monkeypatch.setattr(service, "resolve_control_identity", lambda tid: _kimi_resolved())
        client = FakeTmux()
        monkeypatch.setattr(service, "_tmux_client", lambda: client)
        monkeypatch.setattr(
            service,
            "_native_sequence_preflight",
            lambda *a, **k: (adapter, {0: {"lines": ["/compact"]}}, None),
        )
        monkeypatch.setattr(
            managed_launch_v2, "_observe_turn_state", lambda *a, **k: TerminalStatus.IDLE
        )
        monkeypatch.setattr(
            native_pane_input, "observe_composer_empty", lambda pane_id, pin, **k: empty
        )
        monkeypatch.setattr(
            native_pane_input, "capture_execution_rows", lambda pane_id, pin, **k: []
        )
        if execution_close is None:
            execution_close = (SUBMISSION_SUBMITTED, "capture-pane:%17:wire:sha256:beef")
        monkeypatch.setattr(
            native_pane_input,
            "observe_command_execution",
            lambda pane_id, pin, **k: execution_close,
        )
        return client, adapter

    def test_a_declared_compact_against_a_proven_empty_composer_is_accepted(
        self, client, monkeypatch
    ):
        tmux_client, adapter = self._wire_managed_kimi(monkeypatch, empty=True)
        events = [{"type": "text", "text": "/compact"}, {"type": "key", "key": "Enter"}]
        digest = control_input_request_digest_v4(
            control_id=CONTROL, events=events, payload_class="command", expected_identity=None
        )
        response = _post(
            client, text=None, events=events, payload_class="command", request_digest=digest
        )
        assert response.status_code == 200
        body = response.json()
        assert body["outcome"] == ACCEPTED
        assert body["request_schema_version"] == CONTROL_INPUT_REQUEST_SCHEMA_VERSION_V4
        assert adapter.calls == [({"lines": ["/compact"]}, True)]
        assert [event["outcome"] for event in body["events"]] == ["sent", "sent"]
        # The r11 close: accepted only with the execution evidence attached
        # — over the wire as in the journal (no null-evidence acceptance).
        assert body["submission_observed"] == "submitted"
        assert body["submission_evidence_ref"] == "capture-pane:%17:wire:sha256:beef"
        # The exact-id reconcile replays the evidence-bearing record.
        looked_up = client.get(f"/control-input/{CONTROL}").json()
        assert looked_up["outcome"] == ACCEPTED
        assert looked_up["submission_observed"] == "submitted"
        assert looked_up["submission_evidence_ref"] == "capture-pane:%17:wire:sha256:beef"

    def test_a_declared_command_with_execution_unproven_closes_ambiguous(self, client, monkeypatch):
        from cli_agent_orchestrator.services.control_input_contract import SUBMISSION_UNKNOWN

        tmux_client, adapter = self._wire_managed_kimi(
            monkeypatch, empty=True, execution_close=(SUBMISSION_UNKNOWN, None)
        )
        events = [{"type": "text", "text": "/compact"}, {"type": "key", "key": "Enter"}]
        response = _post(client, text=None, events=events, payload_class="command")
        assert response.status_code == 200
        body = response.json()
        assert body["outcome"] == AMBIGUOUS
        assert body["reason_code"] == "submission-unproven"
        assert body["reattemptable"] is False
        assert adapter.calls == [({"lines": ["/compact"]}, True)]  # one write, never resent
        looked_up = client.get(f"/control-input/{CONTROL}").json()
        assert looked_up["outcome"] == AMBIGUOUS
        assert looked_up["reason_code"] == "submission-unproven"

    def test_a_late_signal_closes_ambiguous_over_the_wire_with_no_second_write(
        self, client, monkeypatch
    ):
        """The steer-041 boundary over HTTP: a submitted observation that
        completes after the write deadline is unproven evidence — the
        close is the terminal ambiguity, and the exact-id replay never
        writes again."""
        import time as _time

        from cli_agent_orchestrator.services.control_input_contract import SUBMISSION_SUBMITTED

        monkeypatch.setattr(service, "WRITE_DEADLINE_SECONDS", 0.2)

        def _late_submitted(*args, **kwargs):
            _time.sleep(0.4)
            return (SUBMISSION_SUBMITTED, "capture-pane:%17:late:sha256:1afe")

        tmux_client, adapter = self._wire_managed_kimi(monkeypatch, empty=True)
        monkeypatch.setattr(native_pane_input, "observe_command_execution", _late_submitted)
        events = [{"type": "text", "text": "/compact"}, {"type": "key", "key": "Enter"}]
        response = _post(client, text=None, events=events, payload_class="command")
        assert response.status_code == 200
        body = response.json()
        assert body["outcome"] == AMBIGUOUS
        assert body["reason_code"] == "submission-unproven"
        # No accepted record carries the after-deadline evidence.
        assert body["outcome"] != ACCEPTED
        looked_up = client.get(f"/control-input/{CONTROL}").json()
        assert looked_up["outcome"] == AMBIGUOUS
        assert looked_up["reason_code"] == "submission-unproven"
        assert adapter.calls == [({"lines": ["/compact"]}, True)]

    def test_a_declared_compact_against_a_nonempty_composer_is_the_typed_refusal(
        self, client, monkeypatch
    ):
        tmux_client, adapter = self._wire_managed_kimi(monkeypatch, empty=False)
        events = [{"type": "text", "text": "/compact"}, {"type": "key", "key": "Enter"}]
        response = _post(client, text=None, events=events, payload_class="command")
        assert response.status_code == 200
        body = response.json()
        assert body["outcome"] == REFUSED
        assert body["reason_code"] == "composer-nonempty"
        assert body["reattemptable"] is True
        assert adapter.calls == []
        assert tmux_client.writes == []
        # The refusal is durable: the exact-id reconcile answers from the journal.
        looked_up = client.get(f"/control-input/{CONTROL}").json()
        assert looked_up["outcome"] == REFUSED
        assert looked_up["reason_code"] == "composer-nonempty"

    def test_a_malformed_declaration_is_a_typed_200_refusal_not_a_422(self, client, tmux):
        response = _post(
            client, text=None, events=[{"type": "text", "text": "prose"}], payload_class="command"
        )
        assert response.status_code == 200
        body = response.json()
        assert body["outcome"] == REFUSED
        assert body["reason_code"] == "malformed-command-declaration"
        assert body["request_schema_version"] == CONTROL_INPUT_REQUEST_SCHEMA_VERSION_V4
        assert tmux.writes == []

    def test_an_unknown_payload_class_value_is_the_typed_refusal(self, client, tmux):
        response = _post(
            client,
            text=None,
            events=[{"type": "text", "text": "/compact"}],
            payload_class="probe",
        )
        body = response.json()
        assert body["outcome"] == REFUSED
        assert body["reason_code"] == "malformed-command-declaration"
        assert tmux.writes == []

    def test_payload_class_beside_the_v1_fields_is_a_422(self, client, tmux):
        response = _post(client, payload_class="command")
        assert response.status_code == 422
        assert tmux.writes == []

    def test_an_undeclared_batch_beginning_with_a_slash_is_prose(self, client, tmux):
        """The r7 regression: a streamed utterance split so a batch begins
        '/tmp/x' is undeclared prose — no guard, no refusal, delivered
        exactly as before the carrier existed."""
        response = _post(client, text=None, events=[{"type": "text", "text": "/tmp/x"}])
        assert response.status_code == 200
        body = response.json()
        assert body["outcome"] == ACCEPTED
        assert body["request_schema_version"] == 3
        assert tmux.writes == [
            {
                "pane_id": PANE,
                "text": "/tmp/x",
                "submit": False,
                "expected_server_identity": SOCKET,
            }
        ]


class TestParseNotationEndpoint:
    """The §5.3 server-authoritative parse: the events and preview, or a
    422 carrying offset-addressed errors.  It only parses — nothing is
    persisted and nothing reaches a pane."""

    def test_a_valid_notation_resolves_to_events_and_preview(self, client, tmux):
        response = client.post(
            "/macros/parse-notation", json={"notation": '"/model" enter up*3 enter'}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["events"] == [
            {"type": "text", "text": "/model"},
            {"type": "key", "key": "Enter"},
            {"type": "key", "key": "Up"},
            {"type": "key", "key": "Up"},
            {"type": "key", "key": "Up"},
            {"type": "key", "key": "Enter"},
        ]
        assert body["preview"] == '"/model" [Enter] [Up]×3 [Enter]'
        assert tmux.writes == []

    def test_an_invalid_notation_is_a_422_with_offset_and_message(self, client):
        response = client.post("/macros/parse-notation", json={"notation": "ctrl+shift+x"})
        assert response.status_code == 422
        body = response.json()
        assert body["errors"][0]["offset"] == 0
        assert "cannot be represented" in body["errors"][0]["message"]

    def test_the_parse_is_the_same_authority_the_module_speaks(self, client):
        """The endpoint answers exactly what parse_notation answers — the
        two surfaces of the one authority cannot drift."""
        from cli_agent_orchestrator.services import macro_notation

        notation = '"save, + / close" ctrl+s'
        expected = macro_notation.parse_notation(notation)
        body = client.post("/macros/parse-notation", json={"notation": notation}).json()
        assert body["events"] == expected.events
        assert body["preview"] == expected.preview

    def test_the_route_requires_a_scope(self, client, auth_on):
        response = client.post("/macros/parse-notation", json={"notation": "enter"})
        assert response.status_code in (401, 403)

    def test_a_read_scope_may_parse(self, client, auth_on):
        _grant(auth.SCOPE_READ)
        response = client.post("/macros/parse-notation", json={"notation": "enter"})
        assert response.status_code == 200
