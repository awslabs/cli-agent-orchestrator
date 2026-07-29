"""Tests for the conditional destructive endpoint (T-HB-6 fork side).

Contract under test (P1 destructive-endpoint correction): whether an effect
class requires proven containment is derived SERVER-SIDE from the effect
kind — the request carries no containment bit.  Every heartbeat reading
that is not a current, correctly-bound ACTIVE lease (missing, stale,
skewed, malformed, wrong-identity, fencing-mismatch) fails closed unless a
durable, complete, server-verified dual-exit proof exists; an ACTIVE
reading always refuses with zero mutation.  The final generation/fence
critical section is held through the effect itself.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from cli_agent_orchestrator.services import generation_fence as gf
from cli_agent_orchestrator.services import heartbeat_store as hb
from cli_agent_orchestrator.services.destructive_endpoint import (
    DestructiveEndpoint,
    DestructiveError,
    DestructiveIntent,
    DestructiveRefused,
    write_binding_record,
    write_dual_exit_proof,
)

UTC = timezone.utc


def _intent(**changes):
    fields = {
        "intent_id": "0f8fad5a-1c87-4d3e-9b96-1b6b2c8e5f10",
        "kind": "terminal-teardown",
        "terminal_id": "a1b2c3d4",
        "generation": "gen-000042",
        "reservation_id": "11111111-1111-4111-8111-111111111111",
        "attempt_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
        "fencing_token_id": "token-1",
    }
    fields.update(changes)
    return DestructiveIntent(**fields)


ATTEMPT = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
RESERVATION = "11111111-1111-4111-8111-111111111111"


def _identity():
    return hb.HeartbeatIdentity(
        project="p",
        task_id="t",
        run_id="r",
        obligation_generation="obgen-1",
        reservation_id=RESERVATION,
        launch_nonce_digest="a" * 64,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        attempt_id=ATTEMPT,
        provider="codex",
        provider_version="0.146.0",
        native_session_id="thr_0192a7b4",
        assigned_policy_sha256="7" * 64,
        segment_hash="9" * 64,
    )


@pytest.fixture
def bound(tmp_path):
    store = tmp_path / "companion"
    token = hb.issue_fencing_token(store, "a1b2c3d4", "gen-000042", ATTEMPT)
    write_binding_record(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        reservation_id=RESERVATION,
        attempt_id=ATTEMPT,
        launch_nonce_digest="a" * 64,
        fencing_token_id=token.id,
        provider="codex",
        native_session_id="thr_0192a7b4",
    )
    return store, token


def _dual_exit(store, token, **changes):
    """Publish the durable dual-exit proof bound to the fixture identity."""
    fields = {
        "terminal_id": "a1b2c3d4",
        "generation": "gen-000042",
        "reservation_id": RESERVATION,
        "attempt_id": ATTEMPT,
        "fencing_token_id": token.id,
        "provider_exit": {"pid": 4242, "exit_code": 0},
        "bridge_exit": {"pid": 4343, "exit_code": 0},
    }
    fields.update(changes)
    return write_dual_exit_proof(store, **fields)


def _proven(store, **kwargs):
    """An endpoint with a proven containment composition."""
    return DestructiveEndpoint(companion_dir=store, containment_proven=lambda: True, **kwargs)


def _beat(store, token, at, **kwargs):
    producer = hb.HeartbeatProducer(
        companion_dir=store, identity=_identity(), token=token, clock=lambda: at
    )
    producer.beat(
        turn_state=kwargs.get("turn_state", "active"),
        provider_turn_id="t",
        evidence_kind="app_server_event",
        evidence_id="e1",
    )


def test_execute_runs_effect_and_returns_receipt(bound):
    store, token = bound
    _dual_exit(store, token)
    endpoint = _proven(store)
    calls = []
    receipt = endpoint.execute(
        _intent(fencing_token_id=token.id), effect=lambda: calls.append(1) or "torn-down"
    )
    assert receipt["outcome"] == "completed"
    assert receipt["result"] == "torn-down"
    assert calls == [1]


def test_active_heartbeat_refuses_zero_mutation(bound):
    store, token = bound
    _dual_exit(store, token)
    now = datetime.now(UTC)
    _beat(store, token, now)
    endpoint = _proven(store, clock=lambda: now)
    calls = []
    with pytest.raises(DestructiveRefused, match="ACTIVE"):
        endpoint.execute(_intent(fencing_token_id=token.id), effect=lambda: calls.append(1))
    assert calls == []  # zero mutation — even with containment proven and a proof


def test_missing_heartbeat_fails_closed_without_dual_exit_proof(bound):
    # DES-1 durable regression: no heartbeat at all is untrustworthy, never
    # permission — absent a durable dual-exit proof the effect is refused
    # even when containment is proven.
    store, token = bound
    endpoint = _proven(store)
    calls = []
    with pytest.raises(DestructiveRefused, match="dual-exit"):
        endpoint.execute(_intent(fencing_token_id=token.id), effect=lambda: calls.append(1))
    assert calls == []


def test_missing_heartbeat_permits_only_with_dual_exit_proof(bound):
    store, token = bound
    _dual_exit(store, token)
    endpoint = _proven(store)
    receipt = endpoint.execute(_intent(fencing_token_id=token.id), effect=lambda: "done")
    assert receipt["outcome"] == "completed"


def test_stale_heartbeat_requires_dual_exit_proof(bound):
    # A stale/expired lease is not proof of death on its own (the old
    # caller-proof path is gone): refused without the proof, permitted with.
    store, token = bound
    old = datetime.now(UTC) - timedelta(seconds=600)
    _beat(store, token, old, turn_state="terminal")
    endpoint = _proven(store)
    with pytest.raises(DestructiveRefused, match="dual-exit"):
        endpoint.execute(_intent(fencing_token_id=token.id), effect=lambda: "done")
    _dual_exit(store, token)
    receipt = endpoint.execute(_intent(fencing_token_id=token.id), effect=lambda: "done")
    assert receipt["outcome"] == "completed"


def test_future_skewed_heartbeat_fails_closed(bound):
    # DES-3 durable regression: a valid-looking heartbeat 60 s in the future
    # is untrustworthy — never permission for the effect.
    store, token = bound
    now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    _beat(store, token, now + timedelta(seconds=60))
    endpoint = _proven(store, clock=lambda: now)
    calls = []
    with pytest.raises(DestructiveRefused, match="dual-exit"):
        endpoint.execute(_intent(fencing_token_id=token.id), effect=lambda: calls.append(1))
    assert calls == []
    _dual_exit(store, token)
    receipt = endpoint.execute(_intent(fencing_token_id=token.id), effect=lambda: "done")
    assert receipt["outcome"] == "completed"


def test_dual_exit_proof_identity_mismatch_refuses(bound):
    store, token = bound
    _dual_exit(store, token, reservation_id="9b2e6679-7425-40de-944b-e07fc1f90ae7")
    endpoint = _proven(store)
    with pytest.raises(DestructiveRefused, match="identity mismatch"):
        endpoint.execute(_intent(fencing_token_id=token.id), effect=lambda: None)


def test_dual_exit_proof_must_bind_the_binding_fencing_token(bound):
    store, token = bound
    _dual_exit(store, token, fencing_token_id="forged-token")
    endpoint = _proven(store)
    with pytest.raises(DestructiveRefused, match="identity mismatch"):
        endpoint.execute(_intent(fencing_token_id=token.id), effect=lambda: None)


def test_dual_exit_proof_is_single_shot_and_complete(bound):
    store, token = bound
    with pytest.raises(DestructiveError, match="both exit"):
        write_dual_exit_proof(
            store,
            terminal_id="a1b2c3d4",
            generation="gen-000042",
            reservation_id=RESERVATION,
            attempt_id=ATTEMPT,
            fencing_token_id=token.id,
            provider_exit={"pid": 1, "exit_code": 0},
            bridge_exit={},
        )
    _dual_exit(store, token)
    with pytest.raises(DestructiveError, match="already exists"):
        _dual_exit(store, token)


def test_binding_mismatch_refuses(bound):
    store, token = bound
    endpoint = _proven(store)
    with pytest.raises(DestructiveRefused, match="no fork-owned binding"):
        endpoint.execute(
            _intent(generation="gen-000043", fencing_token_id=token.id), effect=lambda: None
        )
    with pytest.raises(DestructiveRefused, match="mismatch"):
        endpoint.execute(
            _intent(attempt_id="9b2e6679-7425-40de-944b-e07fc1f90ae7", fencing_token_id=token.id),
            effect=lambda: None,
        )
    with pytest.raises(DestructiveRefused, match="mismatch"):
        endpoint.execute(_intent(fencing_token_id="wrong-token"), effect=lambda: None)


def test_single_use_intent_idempotent_reissue(bound):
    store, token = bound
    _dual_exit(store, token)
    endpoint = _proven(store)
    calls = []
    first = endpoint.execute(
        _intent(fencing_token_id=token.id), effect=lambda: calls.append(1) or "x"
    )
    # Same intent id re-issued (crash recovery): returns the stored
    # receipt without re-driving the completed effect.
    second = endpoint.execute(
        _intent(fencing_token_id=token.id), effect=lambda: calls.append(2) or "y"
    )
    assert second == first
    assert calls == [1]


def test_pending_effect_redriven_after_crash(bound):
    store, token = bound
    _dual_exit(store, token)
    endpoint = _proven(store)
    calls = []

    def crashing_effect():
        calls.append(1)
        raise RuntimeError("kill during effect")

    with pytest.raises(RuntimeError):
        endpoint.execute(_intent(fencing_token_id=token.id), effect=crashing_effect)
    # The intent was consumed (pending); re-issuing the same intent
    # re-drives the idempotent effect.
    receipt = endpoint.execute(_intent(fencing_token_id=token.id), effect=lambda: "recovered")
    assert receipt["outcome"] == "completed"


def test_distinct_intent_is_a_new_single_use_token(bound):
    store, token = bound
    _dual_exit(store, token)
    endpoint = _proven(store)
    endpoint.execute(_intent(fencing_token_id=token.id), effect=lambda: None)
    receipt = endpoint.execute(
        _intent(intent_id="3d813cbb-47fb-42ba-91df-831e1593ac29", fencing_token_id=token.id),
        effect=lambda: None,
    )
    assert receipt["outcome"] == "completed"


def test_containment_requirement_is_server_side(bound):
    # terminal-teardown is a containment-required effect class by the
    # fork's own table: with containment unproven the effect refuses even
    # with a complete dual-exit proof — there is no request bit to waive it.
    store, token = bound
    _dual_exit(store, token)
    endpoint = DestructiveEndpoint(companion_dir=store, containment_proven=lambda: False)
    calls = []
    with pytest.raises(DestructiveRefused, match="containment"):
        endpoint.execute(_intent(fencing_token_id=token.id), effect=lambda: calls.append(1))
    assert calls == []
    proving = _proven(store)
    receipt = proving.execute(_intent(fencing_token_id=token.id), effect=lambda: "ok")
    assert receipt["outcome"] == "completed"


def test_malformed_heartbeat_fails_closed(bound):
    store, token = bound
    path = hb.heartbeat_path(store, "a1b2c3d4", "gen-000042")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{{{")
    endpoint = _proven(store)
    with pytest.raises(DestructiveRefused, match="dual-exit"):
        endpoint.execute(_intent(fencing_token_id=token.id), effect=lambda: None)
    # Malformed is untrustworthy, not death: the dual-exit proof governs.
    _dual_exit(store, token)
    receipt = endpoint.execute(_intent(fencing_token_id=token.id), effect=lambda: "ok")
    assert receipt["outcome"] == "completed"


def test_critical_section_held_through_effect(bound):
    # The final generation/fence critical section wraps the effect itself:
    # a fence install issued while the effect is in flight must block until
    # the effect completes — it can never interleave with the destructive
    # action it is meant to order against.
    store, token = bound
    _dual_exit(store, token)
    endpoint = _proven(store)
    in_effect = threading.Event()
    finish_effect = threading.Event()
    installed: list = []

    def slow_effect():
        in_effect.set()
        assert finish_effect.wait(timeout=10)
        return "torn-down"

    def install():
        installed.append(
            gf.install_fence(
                store,
                terminal_id="a1b2c3d4",
                generation="gen-000042",
                vintage="v2",
                request={
                    "schema": gf.FENCE_REQUEST_SCHEMA,
                    "terminal_generation": "gen-000042",
                    "obligation_generation": "obgen-1",
                    "attempt_id": ATTEMPT,
                    "intent_id": "3d813cbb-47fb-42ba-91df-831e1593ac29",
                    "report_sha256": "a" * 64,
                },
                fencing_token_id=token.id,
            )
        )

    result: list = []

    def run():
        result.append(endpoint.execute(_intent(fencing_token_id=token.id), effect=slow_effect))

    worker = threading.Thread(target=run)
    worker.start()
    assert in_effect.wait(timeout=10)
    installer = threading.Thread(target=install)
    installer.start()
    installer.join(timeout=2)
    assert installer.is_alive()  # blocked: the critical section is held
    finish_effect.set()
    worker.join(timeout=10)
    installer.join(timeout=10)
    assert not worker.is_alive() and not installer.is_alive()
    assert result[0]["outcome"] == "completed"
    assert installed[0]["outcome"] == gf.OUTCOME_FENCED
