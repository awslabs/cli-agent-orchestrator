"""Tests for managed-launch v2 (T-ADM-1) and the DB vintage surface (T-MIG-6 fork side)."""

from __future__ import annotations

import hashlib
import subprocess
import uuid

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.managed_launch_v2 import (
    PROTOCOL_VERSION_V2,
    ManagedLaunchV2AdmitRequest,
    ManagedLaunchV2BindRequest,
    ManagedLaunchV2ReserveRequest,
)
from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services.managed_launch import (
    ManagedLaunchConflict,
    ManagedLaunchNotFound,
)
from cli_agent_orchestrator.services.managed_provider_bridge import BRIDGE_VERSION


@pytest.fixture(autouse=True)
def _companion(tmp_path, monkeypatch):
    monkeypatch.setattr(v2, "COMPANION_DIR", tmp_path / "companion")


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


def _reserve_request(worktree, tmp_path, **changes):
    executable = tmp_path / "fake-provider"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    payload = {
        "protocol_version": PROTOCOL_VERSION_V2,
        "reservation_id": str(uuid.uuid4()),
        "session_name": "cao-test",
        "provider": "codex",
        "agent_profile": "reviewer-sol-max",
        "caller_id": "deadbeef",
        "working_directory": str(worktree),
        "trusted_project_root": str(worktree),
        "expected_model": "gpt-5.6-sol",
        "expected_effort": "xhigh",
        "provider_executable": str(executable),
        "provider_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "obligation_generation": "obgen-7c2e4a1b",
        "task_id": "self-heal-demo-task",
        "run_id": "run-0001",
        "launch_nonce": "n" * 40,
    }
    payload.update(changes)
    return ManagedLaunchV2ReserveRequest(**payload)


def _ready_bridge_state(record, monkeypatch):
    receipt = {
        "bridge_version": BRIDGE_VERSION,
        "receipt_id": "thr_0192a7b4",
        "provider_session_id": "thr_0192a7b4",
        "provider_receipt_kind": "codex-thread-start",
        "provider_transcript_sha256": "a" * 64,
        "provider_version": "0.145.0",
        "model_input_ready": True,
        "reservation_id": record["reservation_id"],
        "terminal_id": record["terminal_id"],
        "generation": record["generation"],
        "provider": "codex",
        "agent_profile": "reviewer-sol-max",
        "model": "gpt-5.6-sol",
        "effort": "xhigh",
        "working_directory": record["working_directory"],
    }
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.read_state",
        lambda rid: {"state": "ready", "readiness": receipt},
        raising=False,
    )
    return receipt


def _bind_request(record, **changes):
    payload = {
        "protocol_version": PROTOCOL_VERSION_V2,
        "terminal_id": record["terminal_id"],
        "generation": record["generation"],
        "attempt_id": str(uuid.uuid4()),
    }
    payload.update(changes)
    return ManagedLaunchV2BindRequest(**payload)


def test_reserve_idempotent_and_nonce_digest_only(isolated_memory_db, worktree, tmp_path):
    request = _reserve_request(worktree, tmp_path)
    record, created = v2.reserve(request)
    assert created
    assert record["protocol_vintage"] == "v2"
    assert record["state"] == "reserved"
    assert record["launch_nonce_digest"] == hashlib.sha256(b"n" * 40).hexdigest()
    assert "launch_nonce" not in record["request"]  # raw nonce never persists
    again, created_again = v2.reserve(request)
    assert not created_again
    assert again["generation"] == record["generation"]
    changed = _reserve_request(
        worktree, tmp_path, reservation_id=request.reservation_id, expected_model="other"
    )
    with pytest.raises(ManagedLaunchConflict):
        v2.reserve(changed)


def test_v2_rows_invisible_to_v1_queries(isolated_memory_db, worktree, tmp_path):
    request = _reserve_request(worktree, tmp_path)
    v2.reserve(request)
    with database.SessionLocal() as db:
        v1_hit = (
            db.query(database.ManagedLaunchReservationModel)
            .filter(database.ManagedLaunchReservationModel.reservation_id == request.reservation_id)
            .first()
        )
        v2_hit = (
            db.query(database.ManagedLaunchV2ReservationModel)
            .filter(
                database.ManagedLaunchV2ReservationModel.reservation_id == request.reservation_id
            )
            .first()
        )
    assert v1_hit is None  # zero v1 visibility into the v2 surface
    assert v2_hit is not None
    assert v2_hit.protocol_vintage == "v2"


def test_claim_launch_single_winner(isolated_memory_db, worktree, tmp_path):
    request = _reserve_request(worktree, tmp_path)
    record, _ = v2.reserve(request)
    first, won = v2.claim_launch(record["reservation_id"])
    assert won and first["state"] == "launching"
    _, won_again = v2.claim_launch(record["reservation_id"])
    assert not won_again
    with pytest.raises(ManagedLaunchNotFound):
        v2.claim_launch(str(uuid.uuid4()))


def test_bind_journals_native_bound(isolated_memory_db, worktree, tmp_path, monkeypatch):
    request = _reserve_request(worktree, tmp_path)
    record, _ = v2.reserve(request)
    v2.claim_launch(record["reservation_id"])
    _ready_bridge_state(record, monkeypatch)
    bound = v2.bind_native(record["reservation_id"], _bind_request(record))
    assert bound["state"] == "bound"
    binding = bound["binding"]
    assert binding["native_session_id"] == "thr_0192a7b4"
    assert binding["issuance_source"] == "app_server_thread_start"
    assert len(binding["creation_payload_sha256"]) == 64
    assert len(binding["binding_payload_sha256"]) == 64
    assert binding["fencing_token_id"]
    assert v2.native_binding_digest(bound)
    # Idempotent for the same attempt; conflict for another.
    again = v2.bind_native(
        record["reservation_id"],
        _bind_request(record, attempt_id=binding["attempt_id"]),
    )
    assert again["binding"] == binding
    with pytest.raises(ManagedLaunchConflict):
        v2.bind_native(record["reservation_id"], _bind_request(record))


def test_bind_refused_before_ready(isolated_memory_db, worktree, tmp_path, monkeypatch):
    request = _reserve_request(worktree, tmp_path)
    record, _ = v2.reserve(request)
    v2.claim_launch(record["reservation_id"])
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.read_state",
        lambda rid: {"state": "starting"},
        raising=False,
    )
    with pytest.raises(ManagedLaunchConflict, match="ready"):
        v2.bind_native(record["reservation_id"], _bind_request(record))


def test_bind_receipt_version_drift_refused(isolated_memory_db, worktree, tmp_path, monkeypatch):
    request = _reserve_request(worktree, tmp_path)
    record, _ = v2.reserve(request)
    v2.claim_launch(record["reservation_id"])
    receipt = _ready_bridge_state(record, monkeypatch)
    receipt["provider_version"] = "0.144.6"
    with pytest.raises(ManagedLaunchConflict, match="drift"):
        v2.bind_native(record["reservation_id"], _bind_request(record))


def _admit_request(record, digest, **changes):
    message = "review the exact head"
    payload = {
        "protocol_version": PROTOCOL_VERSION_V2,
        "delivery_id": str(uuid.uuid4()),
        "message": message,
        "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
        "sender_id": "deadbeef",
        "orchestration_type": "assign",
        "context": {
            "boot_id": "11111111-1111-4111-8111-111111111111",
            "project": "test-project",
            "task_id": "test-task",
            "run_id": "test-task",
            "task_sha256": "1" * 64,
            "plan_sha256": "2" * 64,
            "dossier_sha256": "3" * 64,
            "lease_sha256": "4" * 64,
            "command_packet_sha256": "5" * 64,
            "source_chain_sha256": "6" * 64,
        },
        "native_binding_digest": digest,
    }
    payload.update(changes)
    return ManagedLaunchV2AdmitRequest(**payload)


def test_admit_without_native_bound_sends_zero_task_bytes(isolated_memory_db, worktree, tmp_path):
    request = _reserve_request(worktree, tmp_path)
    record, _ = v2.reserve(request)
    v2.claim_launch(record["reservation_id"])
    # Crash-before-bind (or bind never attempted): no admission possible.
    with pytest.raises(ManagedLaunchConflict, match="native_bound"):
        v2.claim_admission(record["reservation_id"], _admit_request(record, "0" * 64))
    assert v2.get(record["reservation_id"])["state"] == "launching"


def test_admit_with_wrong_binding_digest_refused(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    request = _reserve_request(worktree, tmp_path)
    record, _ = v2.reserve(request)
    v2.claim_launch(record["reservation_id"])
    _ready_bridge_state(record, monkeypatch)
    bound = v2.bind_native(record["reservation_id"], _bind_request(record))
    with pytest.raises(ManagedLaunchConflict, match="native_bound"):
        v2.claim_admission(record["reservation_id"], _admit_request(bound, "0" * 64))
    assert v2.get(record["reservation_id"])["state"] == "bound"


def test_admission_lifecycle_and_ambiguity(isolated_memory_db, worktree, tmp_path, monkeypatch):
    request = _reserve_request(worktree, tmp_path)
    record, _ = v2.reserve(request)
    v2.claim_launch(record["reservation_id"])
    _ready_bridge_state(record, monkeypatch)
    bound = v2.bind_native(record["reservation_id"], _bind_request(record))
    digest = v2.native_binding_digest(bound)
    admit = _admit_request(bound, digest)
    claimed, should_send = v2.claim_admission(record["reservation_id"], admit)
    assert should_send and claimed["state"] == "admitting"
    again, send_again = v2.claim_admission(record["reservation_id"], admit)
    assert not send_again
    receipt = {
        "receipt_id": "turn-1",
        "provider_session_id": "thr_0192a7b4",
        "provider_turn_id": "turn-1",
        "provider_receipt_kind": "codex-turn-start",
    }
    completed = v2.complete_admission(record["reservation_id"], admit.delivery_id, receipt)
    assert completed["state"] == "admitted"
    # Ambiguity path on a fresh reservation.
    request2 = _reserve_request(worktree, tmp_path)
    record2, _ = v2.reserve(request2)
    v2.claim_launch(record2["reservation_id"])
    _ready_bridge_state(record2, monkeypatch)
    bound2 = v2.bind_native(record2["reservation_id"], _bind_request(record2))
    admit2 = _admit_request(bound2, v2.native_binding_digest(bound2))
    v2.claim_admission(record2["reservation_id"], admit2)
    ambiguous = v2.mark_admission_ambiguous(
        record2["reservation_id"], admit2.delivery_id, "bridge died after accept"
    )
    assert ambiguous["admission"]["status"] == "ambiguous_preserved"


def test_fenced_generation_rejects_admission(isolated_memory_db, worktree, tmp_path, monkeypatch):
    request = _reserve_request(worktree, tmp_path)
    record, _ = v2.reserve(request)
    v2.claim_launch(record["reservation_id"])
    _ready_bridge_state(record, monkeypatch)
    bound = v2.bind_native(record["reservation_id"], _bind_request(record))
    from cli_agent_orchestrator.services import generation_fence as gf

    gf.install_fence(
        tmp_path / "companion",
        terminal_id=record["terminal_id"],
        generation=record["generation"],
        vintage="v2",
        request={
            "schema": gf.FENCE_REQUEST_SCHEMA,
            "terminal_generation": record["generation"],
            "obligation_generation": record["obligation_generation"],
            "attempt_id": bound["binding"]["attempt_id"],
            "intent_id": str(uuid.uuid4()),
            "report_sha256": "a" * 64,
        },
        fencing_token_id=bound["binding"]["fencing_token_id"],
    )

    async def _admit():
        return await v2.admit_reserved(
            record["reservation_id"],
            _admit_request(bound, v2.native_binding_digest(bound)),
        )

    import asyncio

    with pytest.raises(gf.FencedError):
        asyncio.run(_admit())


def test_attempt_resume_refused_45_while_containment_red(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    request = _reserve_request(worktree, tmp_path)
    record, _ = v2.reserve(request)
    v2.claim_launch(record["reservation_id"])
    _ready_bridge_state(record, monkeypatch)
    v2.bind_native(record["reservation_id"], _bind_request(record))
    with pytest.raises(ManagedLaunchConflict, match="45"):
        v2.attempt_resume(record["reservation_id"], containment_proven=False)
