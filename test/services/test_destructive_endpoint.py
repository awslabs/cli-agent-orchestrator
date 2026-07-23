"""Tests for the conditional destructive endpoint (T-HB-6 fork side)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cli_agent_orchestrator.services import heartbeat_store as hb
from cli_agent_orchestrator.services.destructive_endpoint import (
    DestructiveEndpoint,
    DestructiveIntent,
    DestructiveRefused,
    write_binding_record,
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
        "requires_containment": False,
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
        provider_version="0.145.0",
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


def test_execute_runs_effect_and_returns_receipt(bound):
    store, token = bound
    endpoint = DestructiveEndpoint(companion_dir=store)
    calls = []
    receipt = endpoint.execute(
        _intent(fencing_token_id=token.id), effect=lambda: calls.append(1) or "torn-down"
    )
    assert receipt["outcome"] == "completed"
    assert receipt["result"] == "torn-down"
    assert calls == [1]


def test_active_heartbeat_refuses_zero_mutation(bound):
    store, token = bound
    now = datetime.now(UTC)
    producer = hb.HeartbeatProducer(
        companion_dir=store, identity=_identity(), token=token, clock=lambda: now
    )
    producer.beat(
        turn_state="active",
        provider_turn_id="t",
        evidence_kind="app_server_event",
        evidence_id="e1",
    )
    endpoint = DestructiveEndpoint(companion_dir=store, clock=lambda: now)
    calls = []
    with pytest.raises(DestructiveRefused, match="ACTIVE"):
        endpoint.execute(_intent(fencing_token_id=token.id), effect=lambda: calls.append(1))
    assert calls == []  # zero mutation


def test_stale_heartbeat_permits_with_caller_proof(bound):
    store, token = bound
    old = datetime.now(UTC) - timedelta(seconds=600)
    producer = hb.HeartbeatProducer(
        companion_dir=store, identity=_identity(), token=token, clock=lambda: old
    )
    producer.beat(
        turn_state="terminal",
        provider_turn_id="t",
        evidence_kind="app_server_event",
        evidence_id="e1",
    )
    endpoint = DestructiveEndpoint(companion_dir=store)
    receipt = endpoint.execute(_intent(fencing_token_id=token.id), effect=lambda: "done")
    assert receipt["outcome"] == "completed"


def test_binding_mismatch_refuses(bound):
    store, token = bound
    endpoint = DestructiveEndpoint(companion_dir=store)
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
    endpoint = DestructiveEndpoint(companion_dir=store)
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
    endpoint = DestructiveEndpoint(companion_dir=store)
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
    endpoint = DestructiveEndpoint(companion_dir=store)
    endpoint.execute(_intent(fencing_token_id=token.id), effect=lambda: None)
    receipt = endpoint.execute(
        _intent(intent_id="3d813cbb-47fb-42ba-91df-831e1593ac29", fencing_token_id=token.id),
        effect=lambda: None,
    )
    assert receipt["outcome"] == "completed"


def test_containment_required_effect_refused_while_unproven(bound):
    store, token = bound
    endpoint = DestructiveEndpoint(companion_dir=store, containment_proven=lambda: False)
    with pytest.raises(DestructiveRefused, match="containment"):
        endpoint.execute(
            _intent(requires_containment=True, fencing_token_id=token.id), effect=lambda: None
        )
    proving = DestructiveEndpoint(companion_dir=store, containment_proven=lambda: True)
    receipt = proving.execute(
        _intent(requires_containment=True, fencing_token_id=token.id), effect=lambda: "ok"
    )
    assert receipt["outcome"] == "completed"


def test_malformed_heartbeat_fails_closed(bound):
    store, token = bound
    path = hb.heartbeat_path(store, "a1b2c3d4", "gen-000042")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{{{")
    endpoint = DestructiveEndpoint(companion_dir=store)
    with pytest.raises(DestructiveRefused, match="malformed"):
        endpoint.execute(_intent(fencing_token_id=token.id), effect=lambda: None)
