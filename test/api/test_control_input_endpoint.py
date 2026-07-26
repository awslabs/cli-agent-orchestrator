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

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.security import auth
from cli_agent_orchestrator.services import control_input_service as service
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

    def pane_control_identity(self, *, pane_id=None, session_name=None, window_name=None):
        if len(self._identities) > 1:
            return self._identities.pop(0)
        return self._identities[0]

    # Keyword-only and undefaulted, mirroring the real primitive.
    def send_literal_line(self, pane_id, text, submit=True, *, expected_server_identity):
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
        # v1 stays the named default; v2 is advertised alongside it.
        assert body["request_schema_version"] == CONTROL_INPUT_REQUEST_SCHEMA_VERSION
        assert body["request_schema_versions"] == [1, 2]
        assert body["digest_domain"] == CONTROL_INPUT_DIGEST_DOMAIN
        # The steer-chord allowlist is truthful: only the pinned Kimi chord.
        assert body["steer_chords"] == {"kimi_cli": ["C-s"]}

    def test_the_identity_route_advertises_the_control_input_block(self, client, tmux):
        body = client.get(f"/terminals/{TERMINAL}/control-identity").json()
        block = body["control_input"]
        assert block["schema_versions"] == [1, 2]
        assert block["chords"] == {"kimi_cli": ["C-s"]}

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
