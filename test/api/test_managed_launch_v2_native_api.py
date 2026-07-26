"""The v2 native seam over HTTP — the exact wire an orchestrator drives.

Every other native suite calls the service functions directly.  This one
goes through the ASGI app, because the surface an external orchestrator
actually has is the seven ``/managed-launch/v2/...`` routes and the
capability document, not the Python API.  A contract that is only proven
in-process is a contract whose transport nobody has checked: route
wiring, request-model validation, status codes, and the response fields a
caller must read back are all invisible to a service-level test.

The lifecycle here is **reserve → launch → bind → admit**.  The bind step
has no v1 analogue, and launch does not return readiness the way v1 does
— it returns the reservation in state ``launching`` and leaves readiness
for bind to consume.  Those two facts are the whole difference a v1
client has to absorb, so they are asserted rather than described.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from typing import Any, Mapping, Optional
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.clients import database as _db
from cli_agent_orchestrator.models.managed_launch_v2 import PROTOCOL_VERSION_V2
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import kimi_native_bootstrap as boot
from cli_agent_orchestrator.services import managed_launch
from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services import managed_provider_bridge as bridge
from cli_agent_orchestrator.services import native_pane_input as npi
from cli_agent_orchestrator.services import session_env

PINNED_VERSION_BANNER = "kimi 0.29.0"
SESSION_ID = "session_9f2c41ab"
MODEL = "gpt-5.6-sol"
EFFORT = "xhigh"
NONCE = "n" * 40

V2_ROOT = "/managed-launch/v2/reservations"


@pytest.fixture(autouse=True)
def _companion(tmp_path, monkeypatch):
    monkeypatch.setattr(v2, "COMPANION_DIR", tmp_path / "companion")
    monkeypatch.setattr("cli_agent_orchestrator.constants.COMPANION_DIR", tmp_path / "companion")
    monkeypatch.setattr(bridge, "BRIDGE_ROOT", tmp_path / "bridge")


@pytest.fixture
def worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _reserve_payload(worktree, tmp_path, **changes) -> dict[str, Any]:
    executable = tmp_path / "fake-kimi"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    payload = {
        "protocol_version": PROTOCOL_VERSION_V2,
        "reservation_id": str(uuid.uuid4()),
        "session_name": "cao-test",
        "provider": "kimi_cli",
        "agent_profile": "reviewer",
        "caller_id": "deadbeef",
        "working_directory": str(worktree),
        "trusted_project_root": None,
        "expected_model": MODEL,
        "expected_effort": EFFORT,
        "provider_executable": str(executable),
        "provider_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "obligation_generation": "obgen-7c2e4a1b",
        "task_id": "self-heal-demo-task",
        "run_id": "run-0001",
        "delivery_id": str(uuid.uuid4()),
        "launch_nonce": NONCE,
        "execution_mode": "native_tui",
    }
    payload.update(changes)
    return payload


class _FakeAcp:
    """A bootstrap transport that mints an id and honours the route."""

    def __init__(self) -> None:
        self._options = [
            {"id": "model", "category": "model", "currentValue": "kimi-default"},
            {"id": "thinking", "category": "thought_level", "currentValue": "low"},
        ]

    def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        if method == "initialize":
            return {"protocolVersion": 1}
        if method == "session/new":
            return {"sessionId": SESSION_ID, "configOptions": self._options}
        if method == "session/set_config_option":
            for option in self._options:
                if option["id"] == params["configId"]:
                    option["currentValue"] = params["value"]
            return {"configOptions": self._options}
        raise AssertionError(f"unexpected bootstrap method {method!r}")

    def terminate(self) -> Mapping[str, Any]:
        return {
            "pid": 4242,
            "exit_status": 0,
            "escalation": [boot.STEP_STDIN_CLOSED],
            "reaped": True,
        }


class _Harness:
    def __init__(self) -> None:
        self.terminals: list[dict[str, Any]] = []
        self.bridge_requests: list[dict[str, Any]] = []
        self.turn_state = TerminalStatus.IDLE

    @property
    def launched_argv(self) -> list[str]:
        assert self.terminals, "no pane was ever created"
        return list(self.terminals[-1]["managed_native_command"])


@pytest.fixture
def harness(monkeypatch):
    """Fake only the provider-facing boundaries; the seam itself is real."""
    state = _Harness()

    async def _create_terminal(**kwargs):
        state.terminals.append(kwargs)
        terminal_id = kwargs["reserved_terminal_id"]
        # A real row in the v2 store. The launch path writes one and later
        # steps read it back, so a fake that returned an id without a row
        # would test those steps against a terminal that does not exist.
        _db.create_terminal_v2(
            terminal_id,
            kwargs.get("session_name") or "cao-test",
            kwargs.get("window_name") or f"w-{terminal_id}",
            kwargs.get("provider") or "kimi_cli",
            generation=kwargs.get("terminal_generation"),
            pane_id="%7",
            window_id="@7",
            server_socket_path="/private/tmp/cao-native.sock",
            session_id="$1",
            pane_pid=4242,
        )
        return {"terminal_id": terminal_id}

    def _observe(self):
        return {
            "pane_id": "%7",
            "pid": 4321,
            "start_marker": "Thu Jul 24 10:00:00 2026",
            "argv": state.launched_argv,
            "cwd": self._record["working_directory"],
        }

    monkeypatch.setattr(boot, "StdioAcpBootstrap", lambda **kwargs: _FakeAcp())
    monkeypatch.setattr(bridge, "provider_version_banner", lambda *a, **k: PINNED_VERSION_BANNER)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.create_terminal", _create_terminal
    )
    monkeypatch.setattr(v2._V2NativePane, "observe", _observe)
    # The pane's own reading of its own screen. Stubbed because the launch
    # now waits for a composer that accepts input before it certifies
    # readiness, and there is no real TUI behind this fake pane to paint
    # one -- an unstubbed read would report every launch as still booting.
    monkeypatch.setattr(npi, "observe_kimi_turn_state", lambda *a, **k: state.turn_state)
    return state


def _bind_payload(record: dict[str, Any], attempt_id: Optional[str] = None) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION_V2,
        "terminal_id": record["terminal_id"],
        "generation": record["generation"],
        "attempt_id": attempt_id or str(uuid.uuid4()),
    }


def _caller_side_binding_digest(bound: dict[str, Any]) -> str:
    """The digest an admit call carries, derived the way a caller must.

    Recomputed here from the bind response rather than read from a fork
    helper: this is exactly the derivation an external orchestrator has
    to implement, and a test that borrowed the fork's own function would
    prove nothing about whether the wire carries enough to do it.
    """
    canonical = json.dumps(bound["binding"], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _reserve_launch_bind(client, worktree, tmp_path):
    payload = _reserve_payload(worktree, tmp_path)
    reserved = client.post(V2_ROOT, json=payload)
    assert reserved.status_code == 201, reserved.text
    reservation_id = payload["reservation_id"]
    launched = client.post(f"{V2_ROOT}/{reservation_id}/launch")
    assert launched.status_code == 200, launched.text
    bind_body = _bind_payload(launched.json())
    bound = client.post(f"{V2_ROOT}/{reservation_id}/bind", json=bind_body)
    assert bound.status_code == 200, bound.text
    return payload, launched.json(), bind_body, bound.json()


# --------------------------------------------------------------------
# The wire an orchestrator drives
# --------------------------------------------------------------------


def test_the_native_chain_completes_over_http(
    client, isolated_memory_db, worktree, tmp_path, harness
):
    """reserve → launch → bind, with the pane running the provider itself."""
    payload, launched, _bind_body, bound = _reserve_launch_bind(client, worktree, tmp_path)

    assert launched["execution_mode"] == em.NATIVE_TUI
    assert bound["state"] == "bound"
    assert bound["binding"]["execution_mode"] == em.NATIVE_TUI
    assert bound["binding"]["native_session_id"] == SESSION_ID
    # The pane's primary process is the provider resuming the minted
    # session — the one fact that distinguishes this mode from ACP.
    assert harness.launched_argv[0] == payload["provider_executable"]
    assert SESSION_ID in harness.launched_argv


def test_launch_returns_the_reservation_not_a_readiness_payload(
    client, isolated_memory_db, worktree, tmp_path, harness
):
    """The single largest shape difference from v1, stated as a test.

    v1's launch answers with ``state: "ready"`` and the readiness receipt
    inline; a v1 client reads its identity out of that response.  v2's
    launch answers with the reservation record in state ``launching`` and
    publishes readiness where ``bind`` will read it.  A client that ported
    its v1 expectations across would find no receipt and conclude the
    launch failed.
    """
    payload = _reserve_payload(worktree, tmp_path)
    client.post(V2_ROOT, json=payload)
    launched = client.post(f"{V2_ROOT}/{payload['reservation_id']}/launch").json()

    assert launched["state"] == "launching"
    assert "readiness" not in launched
    assert launched["binding"] is None
    assert launched["protocol_version"] == PROTOCOL_VERSION_V2
    assert launched["protocol_vintage"] == "v2"


def test_the_reservation_echo_survives_the_round_trip(
    client, isolated_memory_db, worktree, tmp_path
):
    """A caller can verify its own request against the stored one.

    The mode appears both top-level and in the echoed request, and the
    launch nonce appears in neither — only its digest — so an echo check
    can be exact without the surface having to hand a secret back.
    """
    payload = _reserve_payload(worktree, tmp_path)
    record = client.post(V2_ROOT, json=payload).json()

    assert record["execution_mode"] == em.NATIVE_TUI
    assert record["execution_mode_source"] == em.SOURCE_LAUNCH
    assert record["is_legacy_execution_mode"] is False
    assert record["request"]["execution_mode"] == "native_tui"
    assert "launch_nonce" not in record["request"]
    assert record["launch_nonce_digest"] == hashlib.sha256(NONCE.encode()).hexdigest()


def test_the_binding_digest_is_derivable_from_the_bind_response(
    client, isolated_memory_db, worktree, tmp_path, harness
):
    """Admission's precondition is computable from what bind returned.

    If it were not, a caller would have to be told the digest out of
    band, and the bind-before-admit gate would be a fork-internal fact
    rather than a checkable one.
    """
    _payload, _launched, _bind_body, bound = _reserve_launch_bind(client, worktree, tmp_path)

    assert _caller_side_binding_digest(bound) == v2.native_binding_digest(bound)


# --------------------------------------------------------------------
# Response loss: every v2 boundary is re-drivable by exact id
# --------------------------------------------------------------------


def test_a_lost_launch_response_is_recovered_without_a_second_pane(
    client, isolated_memory_db, worktree, tmp_path, harness
):
    """Re-POSTing launch after a lost response starts nothing twice.

    This is what stands in for v1's reconcile endpoint, which v2 does not
    have: the claim is atomic, so the retry observes the claim instead of
    making a new one.  A second pane here would mean two writers on one
    single-writer provider session.
    """
    payload = _reserve_payload(worktree, tmp_path)
    client.post(V2_ROOT, json=payload)
    first = client.post(f"{V2_ROOT}/{payload['reservation_id']}/launch").json()
    second = client.post(f"{V2_ROOT}/{payload['reservation_id']}/launch")

    assert second.status_code == 200
    assert second.json()["terminal_id"] == first["terminal_id"]
    assert second.json()["generation"] == first["generation"]
    assert len(harness.terminals) == 1


def test_a_lost_bind_response_is_recovered_by_the_same_attempt(
    client, isolated_memory_db, worktree, tmp_path, harness
):
    """The attempt id is the exact-id handle a lost bind response needs."""
    payload, _launched, bind_body, bound = _reserve_launch_bind(client, worktree, tmp_path)
    replay = client.post(f"{V2_ROOT}/{payload['reservation_id']}/bind", json=bind_body)

    assert replay.status_code == 200
    assert replay.json()["binding"] == bound["binding"]


def test_a_bind_retry_under_a_new_attempt_id_conflicts(
    client, isolated_memory_db, worktree, tmp_path, harness
):
    """A fresh attempt is a different identity, never a safe replay.

    A caller that lost a response must re-send the same attempt, not mint
    a new one — otherwise a second binding could be published for a
    generation that is already bound.
    """
    payload, _launched, _bind_body, _bound = _reserve_launch_bind(client, worktree, tmp_path)
    other = client.post(
        f"{V2_ROOT}/{payload['reservation_id']}/bind",
        json=_bind_payload(_launched, attempt_id=str(uuid.uuid4())),
    )

    assert other.status_code == 409


def test_a_get_answers_the_exact_state_after_any_lost_response(
    client, isolated_memory_db, worktree, tmp_path, harness
):
    """GET is the query half of exact-id reconcile at every boundary."""
    payload, _launched, _bind_body, bound = _reserve_launch_bind(client, worktree, tmp_path)
    fetched = client.get(f"{V2_ROOT}/{payload['reservation_id']}")

    assert fetched.status_code == 200
    assert fetched.json()["state"] == "bound"
    assert fetched.json()["binding"] == bound["binding"]
    assert client.get(f"{V2_ROOT}/{uuid.uuid4()}").status_code == 404


# --------------------------------------------------------------------
# Truth in advertisement
# --------------------------------------------------------------------


def test_the_capability_document_separates_the_two_surfaces(client):
    """Native is offered by the surface that has the branch, and only it.

    Advertising native on v1 while v1 launches ACP would be a defect, not
    a fix: a caller trusting that claim would get a silent substitution
    and the request echo would confirm the substitution rather than catch
    it.  The two keys exist so one surface can offer a mode the other
    cannot run.
    """
    payload = client.get("/managed-launch/capabilities").json()

    assert payload["execution_modes"] == list(managed_launch.SUPPORTED_EXECUTION_MODES)
    assert "native_tui" not in payload["execution_modes"]
    assert "native_tui" in payload["v2_launchable_execution_modes"]
    assert payload["protocol_version"] == "cao-managed-launch-v1"


def test_the_capability_document_advertises_the_closed_glm_route(client):
    assert client.get("/managed-launch/capabilities").json()["glm_route_envelope"] is True


def test_session_env_rebind_is_idempotent_and_only_updates_future_panes(
    client, isolated_memory_db, monkeypatch
):
    backend = MagicMock()
    backend.session_exists.return_value = True
    monkeypatch.setattr("cli_agent_orchestrator.api.main.get_backend", lambda: backend)
    env_vars = {
        "CAO_CONDUCTOR_ROUTES": "/private/tmp/routes.json",
        "CAO_CONDUCTOR_SHIM_DIR": "/private/tmp/shims",
    }
    fingerprint = hashlib.sha256(
        json.dumps(env_vars, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    response = client.put(
        "/sessions/cao-rebind/env",
        json={"env_vars": env_vars, "fingerprint": fingerprint},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"fingerprint": fingerprint}
    assert session_env.get_session_env("cao-rebind") == env_vars
    backend.create_window.assert_not_called()

    replay = client.put(
        "/sessions/cao-rebind/env",
        json={"env_vars": env_vars, "fingerprint": fingerprint},
    )
    assert replay.status_code == 200
    assert session_env.get_session_env("cao-rebind") == env_vars


def test_session_env_rebind_returns_404_for_an_absent_session(client, monkeypatch):
    backend = MagicMock()
    backend.session_exists.return_value = False
    monkeypatch.setattr("cli_agent_orchestrator.api.main.get_backend", lambda: backend)

    response = client.put(
        "/sessions/cao-missing/env",
        json={"env_vars": {}, "fingerprint": "0" * 64},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "session not found"


def test_session_env_rebind_returns_409_for_a_fingerprint_mismatch(client, monkeypatch):
    backend = MagicMock()
    backend.session_exists.return_value = True
    monkeypatch.setattr("cli_agent_orchestrator.api.main.get_backend", lambda: backend)
    env_vars = {"CAO_CONDUCTOR_MODEL": "glm-5.2"}

    response = client.put(
        "/sessions/cao-fingerprint/env",
        json={"env_vars": env_vars, "fingerprint": "0" * 64},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "env fingerprint mismatch"


def test_session_env_rebind_returns_400_for_an_invalid_session_name(client):
    response = client.put(
        "/sessions/bad%20name/env",
        json={"env_vars": {}, "fingerprint": "0" * 64},
    )

    assert response.status_code == 400


def test_session_env_rebind_returns_503_when_the_store_is_unavailable(client, monkeypatch):
    backend = MagicMock()
    backend.session_exists.return_value = True
    monkeypatch.setattr("cli_agent_orchestrator.api.main.get_backend", lambda: backend)
    monkeypatch.setattr(
        session_env,
        "set_session_env",
        MagicMock(side_effect=session_env.SessionEnvStoreError("store unavailable")),
    )

    response = client.put(
        "/sessions/cao-store-error/env",
        json={"env_vars": {}, "fingerprint": hashlib.sha256(b"{}").hexdigest()},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "session environment could not be persisted"


def test_session_env_rebind_returns_503_when_session_existence_is_unreadable(client, monkeypatch):
    backend = MagicMock()
    backend.session_exists.side_effect = RuntimeError("backend unreadable")
    monkeypatch.setattr("cli_agent_orchestrator.api.main.get_backend", lambda: backend)

    response = client.put(
        "/sessions/cao-unreadable/env",
        json={"env_vars": {}, "fingerprint": hashlib.sha256(b"{}").hexdigest()},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "could not determine whether the session exists"


def test_the_v1_surface_refuses_native_with_nothing_persisted(
    client, isolated_memory_db, worktree, tmp_path
):
    """The ACP surface stays ACP-only, and says so before any effect.

    Refused at reserve, which is the last point still free of provider
    effects — so the caller gets a typed refusal and the store gets
    nothing, rather than a native-labelled row that would launch ACP.
    """
    executable = tmp_path / "fake-kimi"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    reservation_id = str(uuid.uuid4())
    payload = {
        "protocol_version": "cao-managed-launch-v1",
        "reservation_id": reservation_id,
        "session_name": "cao-test",
        "provider": "kimi_cli",
        "agent_profile": "reviewer",
        "caller_id": "deadbeef",
        "project": "test-project",
        "task_id": "test-task",
        "delivery_id": str(uuid.uuid4()),
        "working_directory": str(worktree),
        "trusted_project_root": None,
        "expected_model": MODEL,
        "expected_effort": EFFORT,
        "provider_executable": str(executable),
        "provider_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "execution_mode": "native_tui",
    }
    refused = client.post("/managed-launch/reservations", json=payload)

    assert refused.status_code == 409, refused.text
    assert "native_tui" in refused.text
    assert client.get(f"/managed-launch/reservations/{reservation_id}").status_code == 404


def test_every_advertised_v2_mode_has_a_launch_branch(client):
    """The advertisement is read from the surface, never restated."""
    advertised = client.get("/managed-launch/capabilities").json()["v2_launchable_execution_modes"]

    assert advertised == list(v2.LAUNCHABLE_EXECUTION_MODES)


def test_v2_reservation_rejects_unknown_fields(client, worktree, tmp_path):
    payload = _reserve_payload(worktree, tmp_path)
    payload["unexpected_field"] = "refused"

    response = client.post(V2_ROOT, json=payload)

    assert response.status_code == 422


def test_a_not_yet_ready_bind_is_425_with_the_exact_serialized_envelope(
    client, isolated_memory_db, worktree, tmp_path, monkeypatch
):
    """The transient bind refusal, as it actually appears on the wire.

    Both sides had been modelling this envelope independently — the
    consumer's client and its own fake shared one assumption about where
    ``reason`` sits, and would have passed together while failing against
    this server. So the shape is asserted here, through the real ASGI
    app, where FastAPI does the serializing: a test that built the body
    itself would be asserting its own model of the framework.

    ``reason`` is nested under ``detail`` because that is what an
    ``HTTPException(detail={...})`` produces. It is the one refusal a
    consumer may retry, which is why it needs its own status *and* a
    closed-vocabulary token — everything else on this surface stays 409
    and permanent.
    """
    payload = _reserve_payload(worktree, tmp_path)
    assert client.post(V2_ROOT, json=payload).status_code == 201
    reservation_id = payload["reservation_id"]
    v2.claim_launch(reservation_id)
    # The bridge has started but published no durable readiness yet: the
    # ordinary early state, not a fault.
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.read_state",
        lambda _rid: {"state": "starting"},
        raising=False,
    )

    response = client.post(
        f"{V2_ROOT}/{reservation_id}/bind",
        json=_bind_payload(
            {
                "terminal_id": _terminal_of(reservation_id),
                "generation": _generation_of(reservation_id),
            }
        ),
    )

    assert response.status_code == 425, response.text
    body = response.json()
    assert set(body) == {"detail"}
    assert body["detail"]["reason"] == "bind-bridge-not-durably-ready"
    assert isinstance(body["detail"]["message"], str) and body["detail"]["message"]
    # Not root-level: a consumer that reads body["reason"] finds nothing,
    # which is exactly the divergence this pins.
    assert "reason" not in body


def _terminal_of(reservation_id: str) -> str:
    return v2.get(reservation_id)["terminal_id"]


def _generation_of(reservation_id: str) -> str:
    return v2.get(reservation_id)["generation"]


def test_the_capabilities_advertise_the_typed_not_ready_contract(client):
    """A new consumer can negotiate 425 instead of discovering it.

    An old peer uses 425 for nothing and answers every not-yet-ready bind
    with a permanent 409, so a consumer that assumed the typed contract
    would never retry and the launch would fail on terms neither side
    agreed to. These two keys let it fail closed before a reservation
    exists; their absence is exactly the signal that the peer is old.
    """
    capabilities = client.get("/managed-launch/capabilities").json()

    published = capabilities["native_bind_not_ready_status"]
    # A JSON *number*, not a quoted one. The consumer compares it to an
    # integer status, so a string would satisfy a loose check on one side
    # and fail the gate on the other — the same both-sides-green,
    # fails-together shape as the nested-envelope divergence.
    assert isinstance(published, int) and not isinstance(published, bool)
    assert published == 425
    assert capabilities["native_bind_not_ready_reason"] == "bind-bridge-not-durably-ready"


def test_the_advertisement_is_bound_to_the_behaviour_it_describes(client):
    """Pinned against what the endpoint actually does, not a shared literal.

    Two constants agreeing is not the property that matters — an
    advertisement can drift from the behaviour it describes and still
    match a literal a test copied from it. So the published values are
    compared to the status and reason the error mapper really produces
    for the typed refusal, which is the thing a consumer will meet.
    """
    from cli_agent_orchestrator.api.main import _managed_launch_http_error

    raised = _managed_launch_http_error(
        managed_launch.ManagedLaunchNotReady(
            "early", reason=managed_launch.REASON_BIND_BRIDGE_NOT_DURABLY_READY
        )
    )
    capabilities = client.get("/managed-launch/capabilities").json()

    assert capabilities["native_bind_not_ready_status"] == raised.status_code
    assert capabilities["native_bind_not_ready_reason"] == raised.detail["reason"]


def test_the_advertisement_matches_the_live_wire_answer(
    client, isolated_memory_db, worktree, tmp_path, monkeypatch
):
    """End to end: negotiate from the capabilities, then meet the refusal.

    This is the sequence a consumer actually performs, so it is the one
    worth asserting — a capability document that agreed with a helper but
    not with the route would pass every test above.
    """
    capabilities = client.get("/managed-launch/capabilities").json()
    payload = _reserve_payload(worktree, tmp_path)
    assert client.post(V2_ROOT, json=payload).status_code == 201
    reservation_id = payload["reservation_id"]
    v2.claim_launch(reservation_id)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.read_state",
        lambda _rid: {"state": "starting"},
        raising=False,
    )

    response = client.post(
        f"{V2_ROOT}/{reservation_id}/bind",
        json=_bind_payload(
            {
                "terminal_id": _terminal_of(reservation_id),
                "generation": _generation_of(reservation_id),
            }
        ),
    )

    assert response.status_code == capabilities["native_bind_not_ready_status"]
    assert response.json()["detail"]["reason"] == capabilities["native_bind_not_ready_reason"]
