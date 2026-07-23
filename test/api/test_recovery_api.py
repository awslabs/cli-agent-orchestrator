"""API tests for the recovery control-plane surfaces (v2, fence, destructive, capabilities)."""

from __future__ import annotations

import hashlib
import subprocess
import uuid

import pytest

from cli_agent_orchestrator.constants import COMPANION_DIR as REAL_COMPANION_DIR
from cli_agent_orchestrator.services import generation_fence as gf
from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services.destructive_endpoint import write_binding_record

NONCE = "n" * 40


@pytest.fixture(autouse=True)
def _companion(tmp_path, monkeypatch):
    monkeypatch.setattr(v2, "COMPANION_DIR", tmp_path / "companion")
    monkeypatch.setattr("cli_agent_orchestrator.constants.COMPANION_DIR", tmp_path / "companion")
    return tmp_path / "companion"


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


def _reserve_payload(worktree, tmp_path, **changes):
    executable = tmp_path / "fake-provider"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    payload = {
        "protocol_version": "cao-managed-launch-v2",
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
        "launch_nonce": NONCE,
    }
    payload.update(changes)
    return payload


def test_recovery_capabilities_truthful(client):
    response = client.get("/managed/recovery-capabilities")
    assert response.status_code == 200
    payload = response.json()
    assert payload["protocol"] == "cao-recovery-capabilities-v1"
    assert payload["containment"] == "unproven"
    assert payload["observed_route"] == {
        "codex": "unsupported",
        "claude": "unsupported",
        "kimi": "unproven",
    }
    assert payload["resume"]["kimi"]["identity_available"] is False
    assert payload["resource_registry_version"] == 1


def test_v2_reserve_query_roundtrip(client, isolated_memory_db, worktree, tmp_path):
    payload = _reserve_payload(worktree, tmp_path)
    response = client.post("/managed-launch/v2/reservations", json=payload)
    assert response.status_code == 201
    record = response.json()
    assert record["created"] is True
    assert record["protocol_vintage"] == "v2"
    assert record["launch_nonce_digest"] == hashlib.sha256(NONCE.encode()).hexdigest()
    assert "launch_nonce" not in record["request"]
    again = client.post("/managed-launch/v2/reservations", json=payload)
    assert again.status_code == 201
    assert again.json()["created"] is False
    fetched = client.get(f"/managed-launch/v2/reservations/{payload['reservation_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["state"] == "reserved"
    missing = client.get(f"/managed-launch/v2/reservations/{uuid.uuid4()}")
    assert missing.status_code == 404


def test_v2_wrong_protocol_version_422(client, isolated_memory_db, worktree, tmp_path):
    payload = _reserve_payload(worktree, tmp_path, protocol_version="cao-managed-launch-v1")
    response = client.post("/managed-launch/v2/reservations", json=payload)
    assert response.status_code == 422


def test_fence_install_and_outcomes(client, isolated_memory_db, worktree, tmp_path):
    payload = _reserve_payload(worktree, tmp_path)
    record = client.post("/managed-launch/v2/reservations", json=payload).json()
    fence_request = {
        "schema": "cao-w13-fence-req-v1",
        "terminal_id": record["terminal_id"],
        "terminal_generation": record["generation"],
        "obligation_generation": record["obligation_generation"],
        "attempt_id": str(uuid.uuid4()),
        "intent_id": str(uuid.uuid4()),
        "report_sha256": "a" * 64,
    }
    installed = client.post("/managed-launch/v2/fence", json=fence_request)
    assert installed.status_code == 200
    assert installed.json()["outcome"] == "fenced"
    assert installed.json()["fence_receipt_sha256"]
    again = client.post("/managed-launch/v2/fence", json=fence_request)
    assert again.json()["outcome"] == "already-fenced"
    assert again.json()["fence_receipt_sha256"] == installed.json()["fence_receipt_sha256"]
    # A generation unknown to the fork gets the truthful outcome.
    unknown = client.post(
        "/managed-launch/v2/fence",
        json={
            **fence_request,
            "terminal_generation": str(uuid.uuid4()),
            "intent_id": str(uuid.uuid4()),
        },
    )
    assert unknown.json()["outcome"] == "unknown-generation"


def test_fence_install_binds_body_identity_to_reservation_row(
    client, isolated_memory_db, worktree, tmp_path
):
    # FENCE durable regression: a fence body naming a different terminal
    # than the v2 generation's owner is never acknowledged — the truthful
    # outcome is unknown-generation and nothing is written under the
    # attacker-selected path; the row's own terminal drives the state path.
    from cli_agent_orchestrator.services.generation_fence import fence_state_path

    payload = _reserve_payload(worktree, tmp_path)
    record = client.post("/managed-launch/v2/reservations", json=payload).json()
    companion = tmp_path / "companion"
    fence_request = {
        "schema": "cao-w13-fence-req-v1",
        "terminal_id": "feedface",  # attacker-selected, not the row's terminal
        "terminal_generation": record["generation"],
        "obligation_generation": record["obligation_generation"],
        "attempt_id": str(uuid.uuid4()),
        "intent_id": str(uuid.uuid4()),
        "report_sha256": "a" * 64,
    }
    refused = client.post("/managed-launch/v2/fence", json=fence_request)
    assert refused.status_code == 200
    assert refused.json()["outcome"] == "unknown-generation"
    assert not fence_state_path(companion, "feedface", record["generation"]).exists()
    assert not fence_state_path(companion, record["terminal_id"], record["generation"]).exists()
    # A mismatched obligation generation conflicts (never acknowledged).
    mismatched = client.post(
        "/managed-launch/v2/fence",
        json={
            **fence_request,
            "terminal_id": record["terminal_id"],
            "obligation_generation": "other-obligation",
        },
    )
    assert mismatched.status_code == 409
    # The correct identity still fences, under the row's terminal path.
    correct = client.post(
        "/managed-launch/v2/fence",
        json={**fence_request, "terminal_id": record["terminal_id"]},
    )
    assert correct.json()["outcome"] == "fenced"
    assert fence_state_path(companion, record["terminal_id"], record["generation"]).exists()


def test_destructive_endpoint_refusal_and_execution(
    client, isolated_memory_db, worktree, tmp_path, monkeypatch
):
    companion = tmp_path / "companion"
    reservation_id = str(uuid.uuid4())
    generation = str(uuid.uuid4())
    attempt_id = str(uuid.uuid4())
    write_binding_record(
        companion,
        terminal_id="a1b2c3d4",
        generation=generation,
        reservation_id=reservation_id,
        attempt_id=attempt_id,
        launch_nonce_digest="a" * 64,
        fencing_token_id="token-1",
        provider="codex",
        native_session_id="thr_1",
    )
    # The v2 reservation lookup drives the effect's session identity.
    monkeypatch.setattr(
        "cli_agent_orchestrator.api.main.managed_launch_v2.get",
        lambda rid: {"session_name": "cao-test"},
    )
    intent = {
        "intent_id": str(uuid.uuid4()),
        "kind": "terminal-teardown",
        "terminal_id": "a1b2c3d4",
        "generation": generation,
        "reservation_id": reservation_id,
        "attempt_id": attempt_id,
        "fencing_token_id": "token-1",
    }
    monkeypatch.setattr(
        "cli_agent_orchestrator.api.main.terminal_service.delete_terminal",
        lambda *a, **k: True,
    )
    # Containment is derived server-side by effect class; with the
    # composition unproven the teardown refuses — there is no request bit.
    refused = client.post("/managed/destructive", json=intent)
    assert refused.status_code == 409
    # With a proven composition AND the durable dual-exit proof, it runs.
    from cli_agent_orchestrator.services import containment
    from cli_agent_orchestrator.services.destructive_endpoint import write_dual_exit_proof

    class _ProvenComposition:
        def status(self):
            return "proven"

    monkeypatch.setattr(containment, "ContainmentComposition", _ProvenComposition)
    write_dual_exit_proof(
        companion,
        terminal_id="a1b2c3d4",
        generation=generation,
        reservation_id=reservation_id,
        attempt_id=attempt_id,
        fencing_token_id="token-1",
        provider_exit={"pid": 1, "exit_code": 0},
        bridge_exit={"pid": 2, "exit_code": 0},
    )
    executed = client.post("/managed/destructive", json=intent)
    assert executed.status_code == 200
    assert executed.json()["outcome"] == "completed"
    # Idempotent re-issue of the same intent id.
    assert client.post("/managed/destructive", json=intent).json() == executed.json()
    # Binding mismatch refuses with zero effect.
    mismatch = client.post(
        "/managed/destructive",
        json={**intent, "intent_id": str(uuid.uuid4()), "fencing_token_id": "wrong"},
    )
    assert mismatch.status_code == 409


def test_v1_surface_unaffected_by_v2(client):
    response = client.get("/managed-launch/capabilities")
    assert response.status_code == 200
    assert response.json()["protocol_version"] == "cao-managed-launch-v1"
