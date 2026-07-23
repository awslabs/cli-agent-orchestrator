from __future__ import annotations

import hashlib
import uuid

from cli_agent_orchestrator.models.managed_launch import PROTOCOL_VERSION


def _reservation(tmp_path):
    # P1-9 (final conformance §20.2f): managed reservations pin the provider
    # executable's absolute canonical path + digest.
    executable = tmp_path / "fake-provider"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "reservation_id": str(uuid.uuid4()),
        "session_name": "cao-test",
        "provider": "codex",
        "agent_profile": "reviewer-sol-max",
        "caller_id": "deadbeef",
        "working_directory": str(tmp_path),
        "trusted_project_root": str(tmp_path),
        "expected_model": "gpt-5.6-sol",
        "expected_effort": "xhigh",
        "provider_executable": str(executable),
        "provider_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
    }


def _evidence(record, kind):
    return {
        "protocol_version": PROTOCOL_VERSION,
        "observation_id": str(uuid.uuid4()),
        "kind": kind,
        "terminal_id": record["terminal_id"],
        "generation": record["generation"],
        "provider": record["provider"],
        "agent_profile": record["agent_profile"],
        "model": record["request"]["expected_model"],
        "effort": record["request"]["expected_effort"],
        "evidence_digest": hashlib.sha256(kind.encode()).hexdigest(),
    }


def test_capability_handshake_is_exact_and_versioned(client):
    response = client.get("/managed-launch/capabilities")
    assert response.status_code == 200
    assert response.json() == {
        "protocol_version": PROTOCOL_VERSION,
        "reservation_query": True,
        "reservation_reconcile": True,
        "no_task_launch": True,
        "generation_bound_readiness": True,
        "idempotent_task_admission": True,
        "generation_bound_negative": True,
        "generation_bound_cancel": True,
        "generation_bound_cleanup": True,
        "provider_submission_receipt": True,
        "provider_native_exact_session_receipts": True,
        "zero_task_route_attestation": True,
        "pinned_provider_executable": True,
        "trusted_project_root_providers": ["codex"],
        "readiness_providers": ["codex", "kimi_cli"],
    }


def test_reserve_query_reconcile_and_cancel_round_trip(client, isolated_memory_db, tmp_path):
    payload = _reservation(tmp_path)
    created = client.post("/managed-launch/reservations", json=payload)
    assert created.status_code == 201
    record = created.json()
    assert record["created"] is True

    duplicate = client.post("/managed-launch/reservations", json=payload)
    assert duplicate.status_code == 201
    assert duplicate.json()["created"] is False
    assert duplicate.json()["generation"] == record["generation"]

    queried = client.get(f"/managed-launch/reservations/{payload['reservation_id']}")
    assert queried.status_code == 200
    assert queried.json()["generation"] == record["generation"]

    reconciled = client.post(f"/managed-launch/reservations/{payload['reservation_id']}/reconcile")
    assert reconciled.status_code == 200
    assert reconciled.json()["recovery_only"] is False

    cancelled = client.post(
        f"/managed-launch/reservations/{payload['reservation_id']}/cancel",
        json=_evidence(record, "cancelled"),
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"


def test_negative_and_cancel_endpoints_reject_wrong_kind(client, isolated_memory_db, tmp_path):
    payload = _reservation(tmp_path)
    record = client.post("/managed-launch/reservations", json=payload).json()
    response = client.post(
        f"/managed-launch/reservations/{payload['reservation_id']}/negative",
        json=_evidence(record, "cancelled"),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "endpoint requires kind=negative"


def test_protocol_version_mismatch_fails_closed(client, isolated_memory_db, tmp_path):
    payload = _reservation(tmp_path)
    payload["protocol_version"] = "cao-managed-launch-v0"
    response = client.post("/managed-launch/reservations", json=payload)
    assert response.status_code == 422
