"""HTTP-boundary tests for the read-only composer-observation route.

A conductor that has sent a control needs to know, without sending another
byte, whether the exact text it expected is still resting in the provider's
composer.  This route answers that question by reading the pinned composer
region under the same pane-input lease that guards writes, and by binding the
answer to the exact pane and provider process identity.

The route never returns raw composer text: only a digest and byte length when
a positive observation is proven.  Every refusal is typed so the conductor can
map it to a zero-effect decision.
"""

from __future__ import annotations

import hashlib
import threading
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.security import auth
from cli_agent_orchestrator.services import control_input_service as service
from cli_agent_orchestrator.services import native_pane_input
from cli_agent_orchestrator.services.control_input_service import (
    EXECUTION_MODE_NATIVE_TUI,
)
from cli_agent_orchestrator.services.pane_input_arbiter import (
    pane_input_lease,
    reset_pane_input_arbiter,
)

TERMINAL = "a1b2c3d4"
UNKNOWN_TERMINAL = "ffffffff"
PANE = "%17"
WINDOW = "@3"
PANE_PID = 4242
GENERATION = "gen-7"
SOCKET = "/private/tmp/tmux-501/cao-test"
TEXT = "/compact"

COMPOSER_OBSERVATION_PROTOCOL = "cao-composer-observation-v1"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _metadata(**overrides):
    fields = {
        "pane_id": PANE,
        "generation": GENERATION,
        "provider": "codex",
        "tmux_session": "cao",
        "server_socket_path": SOCKET,
    }
    fields.update(overrides)
    return fields


class FakePaneIdentity:
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
        self.dead = dead
        self.server_socket_path = server_socket_path


class FakeTmux:
    def __init__(self, identities=None):
        self._identities = list(identities or [FakePaneIdentity()])

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


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
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
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")


@pytest.fixture
def tmux(monkeypatch):
    client = FakeTmux()
    monkeypatch.setattr(service, "_tmux_client", lambda: client)
    monkeypatch.setattr(
        service,
        "_terminal_metadata",
        lambda terminal_id: _metadata() if terminal_id == TERMINAL else None,
    )
    monkeypatch.setattr(
        service,
        "_managed_identity",
        lambda terminal_id: (
            {
                "reservation_id": "res-1",
                "generation": GENERATION,
                "execution_mode": EXECUTION_MODE_NATIVE_TUI,
                "native_session_id": "native-sess-1",
                "provider_process_id": f"{PANE_PID}@marker-1",
                "provider_version": "0.146.0",
            }
            if terminal_id == TERMINAL
            else None
        ),
    )
    return client


def _codex_screen(*, composed=""):
    """A Codex screen whose composer holds ``composed``.

    The Codex composer is the last ``›`` prompt row and any wrapped rows
    before the following blank separator; the footer/status sits below the
    separator.
    """
    return [
        "transcript row",
        f"› {composed}",
        "",
        "  gpt-5.6-terra · 99% context left · ? for shortcuts",
    ]


def _grant(*scopes):
    async def _dep():
        return list(scopes)

    app.dependency_overrides[auth.get_current_scopes] = _dep


def _get(client, terminal=TERMINAL, *, sha256=None, bytes_=None):
    params = {}
    if sha256 is not None:
        params["expected_text_sha256"] = sha256
    if bytes_ is not None:
        params["expected_text_bytes"] = bytes_
    return client.get(f"/terminals/{terminal}/composer-observation", params=params)


class TestCapabilityAdvertisement:
    """The route is advertised only when the exact terminal can use it safely."""

    def test_the_identity_route_advertises_composer_observation_for_a_pinned_build(
        self, client, tmux
    ):
        body = client.get(f"/terminals/{TERMINAL}/control-identity").json()
        assert body["control_input"]["composer_observation"]["supported"] is True
        assert (
            body["control_input"]["composer_observation"]["protocol"]
            == COMPOSER_OBSERVATION_PROTOCOL
        )

    def test_an_unpinned_build_does_not_advertise_the_route(self, client, tmux, monkeypatch):
        monkeypatch.setattr(
            service,
            "_managed_identity",
            lambda terminal_id: (
                {
                    "reservation_id": "res-1",
                    "generation": GENERATION,
                    "execution_mode": EXECUTION_MODE_NATIVE_TUI,
                    "native_session_id": "native-sess-1",
                    "provider_process_id": f"{PANE_PID}@marker-1",
                    "provider_version": "0.145.0",
                }
                if terminal_id == TERMINAL
                else None
            ),
        )
        body = client.get(f"/terminals/{TERMINAL}/control-identity").json()
        assert body["control_input"]["composer_observation"]["supported"] is False

    def test_a_non_native_terminal_does_not_advertise_the_route(self, client, tmux, monkeypatch):
        monkeypatch.setattr(
            service,
            "_managed_identity",
            lambda terminal_id: (
                {
                    "reservation_id": "res-1",
                    "generation": GENERATION,
                    "execution_mode": "acp",
                    "native_session_id": "native-sess-1",
                    "provider_process_id": f"{PANE_PID}@marker-1",
                    "provider_version": "0.146.0",
                }
                if terminal_id == TERMINAL
                else None
            ),
        )
        body = client.get(f"/terminals/{TERMINAL}/control-identity").json()
        assert body["control_input"]["composer_observation"]["supported"] is False


class TestPositiveObservation:
    """The exact expected text is observed in the pinned composer region."""

    def test_a_matching_digest_and_byte_length_returns_observed_true(
        self, client, tmux, monkeypatch
    ):
        monkeypatch.setattr(
            native_pane_input,
            "capture_pane_screen",
            lambda pane_id, timeout=10.0: _codex_screen(composed=TEXT),
        )
        response = _get(client, sha256=_sha256(TEXT), bytes_=len(TEXT.encode("utf-8")))
        assert response.status_code == 200
        body = response.json()
        assert body["protocol"] == COMPOSER_OBSERVATION_PROTOCOL
        assert body["observed"] is True
        assert body["terminal_id"] == TERMINAL
        # The observation is later consumed as an identity-bound recovery
        # proof.  Echo the declarable control identity under its exact wire
        # names, not only the lower-level pane fields used to take the sample.
        assert body["terminal_incarnation"] is None
        assert body["terminal_generation"] == GENERATION
        assert body["pane_birth_id"] == PANE
        assert body["provider_process_id"] == f"{PANE_PID}@marker-1"
        assert body["pane_id"] == PANE
        assert body["pane_pid"] == PANE_PID
        assert body["provider"] == "codex"
        assert body["provider_version"] == "0.146.0"
        assert body["execution_mode"] == EXECUTION_MODE_NATIVE_TUI
        assert body["native_session_id"] == "native-sess-1"
        assert body["session_name"] == "cao"
        assert body["content_sha256"] == _sha256(TEXT)
        assert body["content_bytes"] == len(TEXT.encode("utf-8"))
        assert body["submission_observed"] == "unsubmitted"
        assert body["evidence_ref"].startswith(f"capture-pane:{PANE}:")

    def test_kimi_box_padding_is_removed_using_the_expected_byte_count(
        self, client, tmux, monkeypatch
    ):
        text = (
            "[conduct] Continue the retained round: re-read the durable task at "
            "/tmp/task-round-5.md and proceed."
        )
        resolved = service.ResolvedControlIdentity(
            terminal_id=TERMINAL,
            terminal_incarnation=None,
            terminal_generation=GENERATION,
            provider="kimi_cli",
            native_session_id="native-sess-1",
            execution_mode=EXECUTION_MODE_NATIVE_TUI,
            session_name="cao",
            provider_version="0.33.0",
            managed_reservation_id="res-1",
            pane_id=PANE,
            window_id=WINDOW,
            pane_pid=PANE_PID,
            managed=True,
            bound_server_socket_path=SOCKET,
        )
        monkeypatch.setattr(service, "resolve_control_identity", lambda terminal_id: resolved)
        monkeypatch.setattr(
            native_pane_input,
            "capture_pane_screen",
            lambda pane_id, timeout=10.0: [
                "transcript row",
                " ╭────────────────────────────────────────────────────────────────╮",
                f" │ > {text}{' ' * 68}│",
                " ╰────────────────────────────────────────────────────────────────╯",
                " footer/status",
            ],
        )

        response = _get(client, sha256=_sha256(text), bytes_=len(text.encode("utf-8")))

        assert response.status_code == 200
        body = response.json()
        assert body["observed"] is True
        assert body["submission_observed"] == "unsubmitted"
        assert body["content_sha256"] == _sha256(text)
        assert body["content_bytes"] == len(text.encode("utf-8"))


class TestNegativeObservation:
    """The composer does not hold the exact expected text."""

    def test_an_empty_composer_returns_observed_false(self, client, tmux, monkeypatch):
        monkeypatch.setattr(
            native_pane_input,
            "capture_pane_screen",
            lambda pane_id, timeout=10.0: _codex_screen(composed=""),
        )
        response = _get(client, sha256=_sha256(TEXT), bytes_=len(TEXT.encode("utf-8")))
        assert response.status_code == 200
        body = response.json()
        assert body["observed"] is False
        assert "content_sha256" not in body
        assert "content_bytes" not in body
        assert body["submission_observed"] == "unknown"

    def test_a_digest_mismatch_returns_observed_false(self, client, tmux, monkeypatch):
        monkeypatch.setattr(
            native_pane_input,
            "capture_pane_screen",
            lambda pane_id, timeout=10.0: _codex_screen(composed="/other"),
        )
        response = _get(client, sha256=_sha256(TEXT), bytes_=len("/other".encode("utf-8")))
        assert response.status_code == 200
        body = response.json()
        assert body["observed"] is False
        assert body["submission_observed"] == "unknown"
        assert {
            key: body[key]
            for key in (
                "terminal_id",
                "terminal_incarnation",
                "terminal_generation",
                "pane_birth_id",
                "provider_process_id",
                "provider",
                "native_session_id",
                "execution_mode",
                "session_name",
            )
        } == service.resolve_control_identity(TERMINAL).expected_identity_view()

    def test_a_byte_count_mismatch_returns_observed_false(self, client, tmux, monkeypatch):
        monkeypatch.setattr(
            native_pane_input,
            "capture_pane_screen",
            lambda pane_id, timeout=10.0: _codex_screen(composed=TEXT),
        )
        response = _get(client, sha256=_sha256(TEXT), bytes_=len(TEXT.encode("utf-8")) + 1)
        assert response.status_code == 200
        body = response.json()
        assert body["observed"] is False

    def test_a_capture_failure_returns_observed_false(self, client, tmux, monkeypatch):
        monkeypatch.setattr(
            native_pane_input,
            "capture_pane_screen",
            lambda pane_id, timeout=10.0: (_ for _ in ()).throw(
                native_pane_input.NativePaneInputUnavailable("tmux hung")
            ),
        )
        response = _get(client, sha256=_sha256(TEXT), bytes_=len(TEXT.encode("utf-8")))
        assert response.status_code == 200
        body = response.json()
        assert body["observed"] is False
        assert body["submission_observed"] == "unknown"


class TestIdentityRefusal:
    """Identity drift under the lease is refused, not observed."""

    def test_an_unknown_terminal_is_404(self, client, tmux):
        response = _get(
            client,
            terminal=UNKNOWN_TERMINAL,
            sha256=_sha256(TEXT),
            bytes_=len(TEXT.encode("utf-8")),
        )
        assert response.status_code == 404

    def test_a_pane_replaced_under_the_lease_is_refused(self, client, monkeypatch):
        swapped = FakeTmux(
            [FakePaneIdentity(), FakePaneIdentity(pane_pid=PANE_PID + 1)],
        )
        monkeypatch.setattr(service, "_tmux_client", lambda: swapped)
        monkeypatch.setattr(
            service,
            "_terminal_metadata",
            lambda terminal_id: _metadata() if terminal_id == TERMINAL else None,
        )
        monkeypatch.setattr(
            service,
            "_managed_identity",
            lambda terminal_id: (
                {
                    "reservation_id": "res-1",
                    "generation": GENERATION,
                    "execution_mode": EXECUTION_MODE_NATIVE_TUI,
                    "native_session_id": "native-sess-1",
                    "provider_process_id": f"{PANE_PID}@marker-1",
                    "provider_version": "0.146.0",
                }
                if terminal_id == TERMINAL
                else None
            ),
        )
        response = _get(client, sha256=_sha256(TEXT), bytes_=len(TEXT.encode("utf-8")))
        assert response.status_code == 409
        body = response.json()
        assert body["refusal"]["reason"] == "identity-mismatch"


class TestParameterValidation:
    """Malformed expectations are rejected before touching the pane."""

    def test_a_missing_sha256_is_422(self, client, tmux):
        response = _get(client, bytes_=len(TEXT.encode("utf-8")))
        assert response.status_code == 422

    def test_a_non_hex_sha256_is_422(self, client, tmux):
        response = _get(client, sha256="not-hex", bytes_=len(TEXT.encode("utf-8")))
        assert response.status_code == 422

    def test_a_negative_byte_count_is_422(self, client, tmux):
        response = _get(client, sha256=_sha256(TEXT), bytes_=-1)
        assert response.status_code == 422


class TestSafetyAndSecrets:
    """The response never leaks composer text or starts subprocesses."""

    def test_raw_composer_text_is_never_returned(self, client, tmux, monkeypatch):
        monkeypatch.setattr(
            native_pane_input,
            "capture_pane_screen",
            lambda pane_id, timeout=10.0: _codex_screen(composed=TEXT),
        )
        response = _get(client, sha256=_sha256(TEXT), bytes_=len(TEXT.encode("utf-8")))
        body = response.json()
        raw = str(body)
        assert TEXT not in raw
        assert "raw" not in body

    def test_the_route_acquires_the_same_pane_input_lease(self, client, tmux, monkeypatch):
        held = threading.Event()
        observed_during_lease = []

        def capturing_screen(pane_id, timeout=10.0):
            observed_during_lease.append(held.is_set())
            return _codex_screen(composed=TEXT)

        monkeypatch.setattr(native_pane_input, "capture_pane_screen", capturing_screen)

        acquired, release = threading.Event(), threading.Event()

        def hold():
            with pane_input_lease(PANE, holder="other-writer", timeout=0.0):
                acquired.set()
                release.wait(10)

        worker = threading.Thread(target=hold, daemon=True)
        worker.start()
        assert acquired.wait(10)
        try:
            response = _get(client, sha256=_sha256(TEXT), bytes_=len(TEXT.encode("utf-8")))
            assert response.status_code == 409
            body = response.json()
            assert body["refusal"]["reason"] == "pane-busy"
        finally:
            release.set()
            worker.join(10)

    def test_unexpected_errors_become_typed_responses(self, client, tmux, monkeypatch):
        monkeypatch.setattr(
            service,
            "resolve_control_identity",
            lambda terminal_id: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        response = _get(client, sha256=_sha256(TEXT), bytes_=len(TEXT.encode("utf-8")))
        assert response.status_code == 500
        body = response.json()
        assert body["protocol"] == COMPOSER_OBSERVATION_PROTOCOL
        assert body["observed"] is False


class TestUnsupportedProvider:
    """A provider/build without a pinned observation layout is unsupported."""

    def test_an_unsupported_provider_does_not_serve_the_route(self, client, tmux, monkeypatch):
        monkeypatch.setattr(
            service,
            "_terminal_metadata",
            lambda terminal_id: _metadata(provider="muse_cli") if terminal_id == TERMINAL else None,
        )
        monkeypatch.setattr(
            service,
            "_managed_identity",
            lambda terminal_id: (
                {
                    "reservation_id": "res-1",
                    "generation": GENERATION,
                    "execution_mode": EXECUTION_MODE_NATIVE_TUI,
                    "native_session_id": "native-sess-1",
                    "provider_process_id": f"{PANE_PID}@marker-1",
                    "provider_version": "0.1.0",
                }
                if terminal_id == TERMINAL
                else None
            ),
        )
        response = _get(client, sha256=_sha256(TEXT), bytes_=len(TEXT.encode("utf-8")))
        assert response.status_code == 409
        body = response.json()
        assert body["refusal"]["reason"] == "provider-unsupported"

    def test_an_unpinned_kimi_neighbour_does_not_serve_the_route(self, client, tmux, monkeypatch):
        # 0.33.0 is pinned; its neighbours must fail closed rather than inherit.
        monkeypatch.setattr(
            service,
            "_terminal_metadata",
            lambda terminal_id: _metadata(provider="kimi_cli") if terminal_id == TERMINAL else None,
        )
        monkeypatch.setattr(
            service,
            "_managed_identity",
            lambda terminal_id: (
                {
                    "reservation_id": "res-1",
                    "generation": GENERATION,
                    "execution_mode": EXECUTION_MODE_NATIVE_TUI,
                    "native_session_id": "native-sess-1",
                    "provider_process_id": f"{PANE_PID}@marker-1",
                    "provider_version": "0.33.1",
                }
                if terminal_id == TERMINAL
                else None
            ),
        )
        response = _get(client, sha256=_sha256(TEXT), bytes_=len(TEXT.encode("utf-8")))
        assert response.status_code == 409
        body = response.json()
        assert body["refusal"]["reason"] == "provider-unsupported"
