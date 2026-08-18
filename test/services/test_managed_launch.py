from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.models.managed_launch import (
    PROTOCOL_VERSION,
    ManagedLaunchAdmitRequest,
    ManagedLaunchCleanupRequest,
    ManagedLaunchObservationRequest,
    ManagedLaunchReserveRequest,
    ManagedLaunchRouteAttestRequest,
)
from cli_agent_orchestrator.services import managed_launch
from cli_agent_orchestrator.services import managed_provider_bridge as bridge
from cli_agent_orchestrator.services import operation_journal
from cli_agent_orchestrator.services.managed_provider_bridge import BRIDGE_VERSION

DELIVERY_ID = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def isolated_effect_admission(isolated_memory_db, monkeypatch, tmp_path):
    from cli_agent_orchestrator import constants

    monkeypatch.setattr(constants, "COMPANION_DIR", tmp_path / "companion")
    return isolated_memory_db


def _reserve_request(tmp_path, **changes):
    # P1-9 (final conformance §20.2f): managed reservations pin the provider
    # executable's absolute canonical path + digest; the fixture creates a
    # real stub executable so the pin verifies hermetically.
    executable = tmp_path / "fake-provider"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "reservation_id": str(uuid.uuid4()),
        "session_name": "cao-test",
        "provider": "codex",
        "agent_profile": "reviewer-sol-max",
        "caller_id": "deadbeef",
        "project": "test-project",
        "task_id": "test-task",
        "delivery_id": DELIVERY_ID,
        "working_directory": str(tmp_path),
        "trusted_project_root": str(tmp_path),
        "expected_model": "gpt-5.6-sol",
        "expected_effort": "xhigh",
        "provider_executable": str(executable),
        "provider_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
    }
    payload.update(changes)
    return ManagedLaunchReserveRequest(**payload)


def _commit_fixture_worktree(tmp_path) -> None:
    """Give launch tests a real repository/head for the rendezvous tuple."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "fake-provider"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=managed-launch-test",
            "-c",
            "user.email=managed-launch@example.test",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=tmp_path,
        check=True,
    )


def _admit_request(message="review the exact head", **changes):
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "delivery_id": DELIVERY_ID,
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
    }
    payload.update(changes)
    return ManagedLaunchAdmitRequest(**payload)


def _ready_receipt_for(record, request):
    return {
        "bridge_version": BRIDGE_VERSION,
        "receipt_id": "provider-session-ready-opaque",
        "provider_session_id": "provider-session-ready-opaque",
        "provider_receipt_kind": "codex-thread-start",
        "provider_transcript_sha256": "a" * 64,
        # P1-8 (final conformance §20.2f): the complete readiness schema —
        # provider version + explicit model-input-ready are mandatory.
        "provider_version": "0.146.0",
        "model_input_ready": True,
        "reservation_id": request.reservation_id,
        "terminal_id": record["terminal_id"],
        "generation": record["generation"],
        "provider": record["provider"],
        "agent_profile": record["agent_profile"],
        "model": request.expected_model,
        "effort": request.expected_effort,
        "working_directory": request.working_directory,
    }


def _ready_record(request):
    record, _ = managed_launch.reserve(request)
    record, should_launch = managed_launch.claim_launch(request.reservation_id)
    assert should_launch
    receipt = _ready_receipt_for(record, request)
    return managed_launch.mark_ready(
        request.reservation_id,
        terminal_id=record["terminal_id"],
        generation=record["generation"],
        receipt=receipt,
    )


def _submission_receipt(record, admission):
    return {
        "bridge_version": BRIDGE_VERSION,
        "receipt_id": "provider-turn-opaque",
        "provider_session_id": "provider-session-ready-opaque",
        "provider_turn_id": "provider-turn-opaque",
        "provider_receipt_kind": "codex-turn-start",
        "provider_transcript_sha256": "b" * 64,
        "reservation_id": record["reservation_id"],
        "terminal_id": record["terminal_id"],
        "generation": record["generation"],
        "provider": record["provider"],
        "agent_profile": record["agent_profile"],
        "model": record["request"]["expected_model"],
        "effort": record["request"]["expected_effort"],
        "working_directory": record["working_directory"],
        "delivery_id": admission.delivery_id,
        "receiver_id": record["terminal_id"],
        "message_sha256": admission.message_sha256,
        "sender_id": admission.sender_id,
        "context": admission.context.model_dump(mode="json"),
        "provider_accepted": True,
        "submitted_at": "2026-07-22T00:00:00Z",
    }


def _launch_failure_state(record, request, detail="ambient bridge control rejected"):
    inventory = bridge._environment_inventory("codex", ["HOME", "PATH"])
    failure = bridge._launch_failure(
        {
            "reservation_id": record["reservation_id"],
            "terminal_id": record["terminal_id"],
            "generation": record["generation"],
            "delivery_id": request.delivery_id,
        },
        bridge.BridgeError(detail),
        inventory,
        provider_io_started=False,
    )
    return {
        "bridge_version": BRIDGE_VERSION,
        "state": "launch-failed-bridge",
        "readiness": None,
        "submission": None,
        "environment_inventory": inventory,
        "launch_failure": failure,
    }


def test_reserve_is_idempotent_and_queryable(isolated_memory_db, tmp_path):
    request = _reserve_request(tmp_path)
    first, created = managed_launch.reserve(request)
    second, created_again = managed_launch.reserve(request)

    assert created is True
    assert created_again is False
    assert first == second == managed_launch.get(request.reservation_id)
    assert first["state"] == "reserved"
    assert len(first["terminal_id"]) == 8
    assert uuid.UUID(first["generation"])


def test_reservation_id_cannot_be_rebound(isolated_memory_db, tmp_path):
    request = _reserve_request(tmp_path)
    managed_launch.reserve(request)
    changed = request.model_copy(update={"expected_effort": "high"})
    with pytest.raises(managed_launch.ManagedLaunchConflict):
        managed_launch.reserve(changed)


def test_delivery_identity_is_required_and_cannot_be_rebound(isolated_memory_db, tmp_path):
    payload = _reserve_request(tmp_path).model_dump(mode="json")
    payload.pop("delivery_id")
    with pytest.raises(ValueError):
        ManagedLaunchReserveRequest(**payload)

    request = _reserve_request(tmp_path)
    managed_launch.reserve(request)
    changed = request.model_copy(update={"delivery_id": "44444444-4444-4444-8444-444444444444"})
    with pytest.raises(managed_launch.ManagedLaunchConflict):
        managed_launch.reserve(changed)


def test_launch_failure_finalizes_exact_delivery_once_and_rejects_tampering(
    isolated_memory_db, tmp_path
):
    request = _reserve_request(tmp_path)
    managed_launch.reserve(request)
    record, should_launch = managed_launch.claim_launch(request.reservation_id)
    assert should_launch
    bridge_state = _launch_failure_state(record, request)

    failed = managed_launch.mark_launch_failed_bridge(request.reservation_id, bridge_state)
    repeated = managed_launch.mark_launch_failed_bridge(request.reservation_id, bridge_state)
    assert failed == repeated
    assert failed["state"] == "launch-failed-bridge"
    assert failed["admission"] == {
        "schema": "cao-managed-launch-delivery-terminal-v1",
        "delivery_id": request.delivery_id,
        "status": "never-submitted",
        "reservation_id": request.reservation_id,
        "terminal_id": record["terminal_id"],
        "generation": record["generation"],
        "failure_evidence_sha256": failed["launch_failure"]["evidence_sha256"],
        "finalized_at": failed["launch_failure"]["failed_at"],
    }
    assert failed["negative"] == failed["launch_failure"]
    assert failed["observations"][-1]["failure"] == failed["launch_failure"]
    assert (
        bridge.validate_launch_failure(
            bridge_state,
            reservation_id=request.reservation_id,
            terminal_id=record["terminal_id"],
            generation=record["generation"],
            delivery_id=request.delivery_id,
            provider="codex",
        )
        == failed["launch_failure"]
    )

    second_request = _reserve_request(tmp_path, delivery_id="55555555-5555-4555-8555-555555555555")
    managed_launch.reserve(second_request)
    second_record, _ = managed_launch.claim_launch(second_request.reservation_id)
    tampered = _launch_failure_state(second_record, second_request)
    tampered = deepcopy(tampered)
    tampered["launch_failure"]["generation"] = str(uuid.uuid4())
    with pytest.raises(managed_launch.ManagedLaunchConflict, match="identity/evidence mismatch"):
        managed_launch.mark_launch_failed_bridge(second_request.reservation_id, tampered)
    unchanged = managed_launch.get(second_request.reservation_id)
    assert unchanged["state"] == "launching"
    assert unchanged["admission"] is None
    assert unchanged["observations"] == []


def test_trusted_root_must_equal_canonical_worktree(isolated_memory_db, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    request = _reserve_request(tmp_path, trusted_project_root=str(other))
    with pytest.raises(managed_launch.ManagedLaunchConflict):
        managed_launch.reserve(request)


def test_launch_claim_allocates_no_second_generation(isolated_memory_db, tmp_path):
    request = _reserve_request(tmp_path)
    original, _ = managed_launch.reserve(request)
    first, should_launch = managed_launch.claim_launch(request.reservation_id)
    second, should_launch_again = managed_launch.claim_launch(request.reservation_id)

    assert should_launch is True
    assert should_launch_again is False
    assert first["terminal_id"] == second["terminal_id"] == original["terminal_id"]
    assert first["generation"] == second["generation"] == original["generation"]


def test_concurrent_launch_claim_has_exactly_one_winner(isolated_memory_db, tmp_path):
    request = _reserve_request(tmp_path)
    managed_launch.reserve(request)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: managed_launch.claim_launch(request.reservation_id),
                range(8),
            )
        )

    assert sum(should_launch for _, should_launch in results) == 1
    identities = {(row["terminal_id"], row["generation"]) for row, _ in results}
    assert len(identities) == 1


def test_admission_requires_readiness_and_is_idempotent(isolated_memory_db, tmp_path):
    request = _reserve_request(tmp_path)
    managed_launch.reserve(request)
    admission = _admit_request()
    with pytest.raises(managed_launch.ManagedLaunchConflict):
        managed_launch.claim_admission(request.reservation_id, admission)

    _ready_record(request)
    wrong_delivery = _admit_request(delivery_id="44444444-4444-4444-8444-444444444444")
    with pytest.raises(managed_launch.ManagedLaunchConflict, match="immutable"):
        managed_launch.claim_admission(request.reservation_id, wrong_delivery)
    assert managed_launch.get(request.reservation_id)["admission"] is None
    claimed, should_send = managed_launch.claim_admission(request.reservation_id, admission)
    duplicate, should_send_again = managed_launch.claim_admission(request.reservation_id, admission)
    assert should_send is True
    assert should_send_again is False
    assert claimed["state"] == duplicate["state"] == "admitting"

    provider_receipt = _submission_receipt(claimed, admission)
    completed = managed_launch.complete_admission(
        request.reservation_id, admission.delivery_id, provider_receipt
    )
    completed_again = managed_launch.complete_admission(
        request.reservation_id, admission.delivery_id, provider_receipt
    )
    assert completed["state"] == completed_again["state"] == "admitted"
    receipt = completed["admission"]["provider_submission_receipt"]
    assert receipt == completed_again["admission"]["provider_submission_receipt"]
    assert receipt["reservation_id"] == request.reservation_id
    assert receipt["delivery_id"] == admission.delivery_id
    assert receipt["terminal_id"] == completed["terminal_id"]
    assert receipt["receiver_id"] == completed["terminal_id"]
    assert receipt["generation"] == completed["generation"]
    assert receipt["message_sha256"] == admission.message_sha256
    assert receipt["context"] == admission.context.model_dump(mode="json")


def test_concurrent_admission_claim_has_exactly_one_sender(isolated_memory_db, tmp_path):
    request = _reserve_request(tmp_path)
    _ready_record(request)
    admission = _admit_request()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: managed_launch.claim_admission(request.reservation_id, admission),
                range(8),
            )
        )

    assert sum(should_send for _, should_send in results) == 1
    assert {row["admission"]["delivery_id"] for row, _ in results} == {admission.delivery_id}


def test_admission_digest_and_identity_are_immutable(isolated_memory_db, tmp_path):
    request = _reserve_request(tmp_path)
    _ready_record(request)
    admission = _admit_request()
    managed_launch.claim_admission(request.reservation_id, admission)

    changed = _admit_request(
        delivery_id=admission.delivery_id,
        message="different",
    )
    with pytest.raises(managed_launch.ManagedLaunchConflict):
        managed_launch.claim_admission(request.reservation_id, changed)

    bad_digest = _admit_request(message_sha256="0" * 64)
    with pytest.raises(managed_launch.ManagedLaunchConflict):
        managed_launch.claim_admission(request.reservation_id, bad_digest)


def test_admission_project_task_must_match_reserved_launch_identity(isolated_memory_db, tmp_path):
    request = _reserve_request(tmp_path)
    _ready_record(request)
    admission = _admit_request()
    mismatched_context = admission.context.model_dump(mode="json")
    mismatched_context["task_id"] = "foreign-task"

    with pytest.raises(
        managed_launch.ManagedLaunchConflict,
        match="project/task identity does not match reservation",
    ):
        managed_launch.claim_admission(
            request.reservation_id,
            _admit_request(context=mismatched_context),
        )
    record = managed_launch.get(request.reservation_id)
    assert record["state"] == "ready"
    assert record["admission"] is None


def test_observation_append_is_idempotent(isolated_memory_db, tmp_path):
    request = _reserve_request(tmp_path)
    record, _ = managed_launch.reserve(request)
    observation = ManagedLaunchObservationRequest(
        protocol_version=PROTOCOL_VERSION,
        observation_id=str(uuid.uuid4()),
        kind="preflight",
        terminal_id=record["terminal_id"],
        generation=record["generation"],
        provider=record["provider"],
        agent_profile=record["agent_profile"],
        model=request.expected_model,
        effort=request.expected_effort,
        preflight_class="update-prompt",
        evidence_digest="a" * 64,
        detail="structured provider observation",
    )
    first = managed_launch.append_observation(request.reservation_id, observation)
    second = managed_launch.append_observation(request.reservation_id, observation)
    assert first == second
    assert len(first["observations"]) == 1


@pytest.mark.parametrize("kind", ["negative", "cancelled"])
def test_observation_cannot_replace_launch_failure_terminal_proof(
    isolated_memory_db, tmp_path, kind
):
    request = _reserve_request(tmp_path)
    managed_launch.reserve(request)
    record, should_launch = managed_launch.claim_launch(request.reservation_id)
    assert should_launch
    bridge_state = _launch_failure_state(record, request)
    failed = managed_launch.mark_launch_failed_bridge(request.reservation_id, bridge_state)
    terminal_proof = {
        "state": failed["state"],
        "admission": deepcopy(failed["admission"]),
        "negative": deepcopy(failed["negative"]),
        "launch_failure": deepcopy(failed["launch_failure"]),
    }
    observation = ManagedLaunchObservationRequest(
        protocol_version=PROTOCOL_VERSION,
        observation_id=str(uuid.uuid4()),
        kind=kind,
        terminal_id=record["terminal_id"],
        generation=record["generation"],
        provider=record["provider"],
        agent_profile=record["agent_profile"],
        model=request.expected_model,
        effort=request.expected_effort,
        evidence_digest="d" * 64,
        detail="late observation after exact launch failure finalization",
    )

    observed = managed_launch.append_observation(request.reservation_id, observation)

    assert {key: observed[key] for key in terminal_proof} == terminal_proof
    assert observed["state"] == "launch-failed-bridge"
    assert observed["observations"][-1]["observation_id"] == observation.observation_id
    assert observed["observations"][-1]["kind"] == kind


def test_concurrent_observations_are_append_only(isolated_memory_db, tmp_path):
    request = _reserve_request(tmp_path)
    record, _ = managed_launch.reserve(request)

    def append(index):
        observation = ManagedLaunchObservationRequest(
            protocol_version=PROTOCOL_VERSION,
            observation_id=str(uuid.uuid4()),
            kind="preflight",
            terminal_id=record["terminal_id"],
            generation=record["generation"],
            provider=record["provider"],
            agent_profile=record["agent_profile"],
            model=request.expected_model,
            effort=request.expected_effort,
            preflight_class=f"structured-{index}",
            evidence_digest=hashlib.sha256(str(index).encode()).hexdigest(),
        )
        return managed_launch.append_observation(request.reservation_id, observation)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, range(8)))

    final = managed_launch.get(request.reservation_id)
    assert len(final["observations"]) == 8
    assert {item["preflight_class"] for item in final["observations"]} == {
        f"structured-{index}" for index in range(8)
    }


def test_stale_generation_evidence_is_rejected(isolated_memory_db, tmp_path):
    request = _reserve_request(tmp_path)
    record, _ = managed_launch.reserve(request)
    observation = ManagedLaunchObservationRequest(
        protocol_version=PROTOCOL_VERSION,
        observation_id=str(uuid.uuid4()),
        kind="negative",
        terminal_id=record["terminal_id"],
        generation=str(uuid.uuid4()),
        provider=record["provider"],
        agent_profile=record["agent_profile"],
        model=request.expected_model,
        effort=request.expected_effort,
        evidence_digest="b" * 64,
    )
    with pytest.raises(managed_launch.ManagedLaunchConflict):
        managed_launch.append_observation(request.reservation_id, observation)


def test_cancelled_or_negative_reservation_refuses_admission(isolated_memory_db, tmp_path):
    for kind in ("cancelled", "negative"):
        request = _reserve_request(tmp_path)
        record = _ready_record(request)
        observation = ManagedLaunchObservationRequest(
            protocol_version=PROTOCOL_VERSION,
            observation_id=str(uuid.uuid4()),
            kind=kind,
            terminal_id=record["terminal_id"],
            generation=record["generation"],
            provider=record["provider"],
            agent_profile=record["agent_profile"],
            model=request.expected_model,
            effort=request.expected_effort,
            evidence_digest="c" * 64,
        )
        terminal = managed_launch.append_observation(request.reservation_id, observation)
        assert terminal["state"] == kind
        with pytest.raises(managed_launch.ManagedLaunchConflict):
            managed_launch.claim_admission(request.reservation_id, _admit_request())


def test_reconcile_never_mutates_or_relaunches(isolated_memory_db, tmp_path):
    request = _reserve_request(tmp_path)
    reserved, _ = managed_launch.reserve(request)
    reconciled = managed_launch.reconcile(request.reservation_id)
    assert reconciled["state"] == "reserved"
    assert reconciled["recovery_only"] is False
    assert reconciled["terminal_record_present"] is False
    assert reconciled["generation"] == reserved["generation"]


def test_reconcile_adopts_durable_readiness_without_provider_io(
    isolated_memory_db, tmp_path, monkeypatch
):
    request = _reserve_request(tmp_path)
    record, _ = managed_launch.reserve(request)
    record, should_launch = managed_launch.claim_launch(request.reservation_id)
    assert should_launch is True
    receipt = _ready_receipt_for(record, request)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.read_state",
        lambda _: {"state": "ready", "readiness": receipt},
    )

    reconciled = managed_launch.reconcile(request.reservation_id)

    assert reconciled["state"] == "ready"
    assert reconciled["readiness"] == receipt


def test_reconcile_adopts_durable_submission_without_replay(
    isolated_memory_db, tmp_path, monkeypatch
):
    request = _reserve_request(tmp_path)
    _ready_record(request)
    admission = _admit_request()
    admitting, should_send = managed_launch.claim_admission(request.reservation_id, admission)
    assert should_send is True
    receipt = _submission_receipt(admitting, admission)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.read_state",
        lambda _: {"state": "admitted", "submission": receipt},
    )

    reconciled = managed_launch.reconcile(request.reservation_id)

    assert reconciled["state"] == "admitted"
    assert reconciled["admission"]["provider_submission_receipt"] == receipt


def test_reconcile_adopts_durable_preflight_block_without_relaunch(
    isolated_memory_db, tmp_path, monkeypatch
):
    request = _reserve_request(tmp_path)
    managed_launch.reserve(request)
    managed_launch.claim_launch(request.reservation_id)
    bridge_state = {
        "state": "preflight_blocked",
        "error": "provider initialization failed",
    }
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.read_state",
        lambda _: bridge_state,
    )

    reconciled = managed_launch.reconcile(request.reservation_id)

    assert reconciled["state"] == "preflight_blocked"
    assert reconciled["observations"][0]["evidence"] == bridge_state


@pytest.mark.asyncio
async def test_launch_reserved_uses_exact_provider_bridge_before_readiness(
    isolated_memory_db, tmp_path, monkeypatch
):
    request = _reserve_request(tmp_path)
    _commit_fixture_worktree(tmp_path)
    record, _ = managed_launch.reserve(request)
    calls = []

    monkeypatch.setattr(managed_launch, "_executable_identity", lambda _: ("/provider", "d" * 64))
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.profile_digest",
        lambda _: "e" * 64,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.write_request",
        lambda reservation_id, body: calls.append(("write", reservation_id, body)),
    )

    async def fake_create_terminal(**kwargs):
        calls.append(("create", kwargs))
        assert kwargs["initial_message"] is None
        assert kwargs["reserved_terminal_id"] == record["terminal_id"]
        assert kwargs["preserve_on_init_failure"] is True
        assert kwargs["expected_model"] == "gpt-5.6-sol"
        assert kwargs["expected_effort"] == "xhigh"
        assert kwargs["managed_native_command"][0]
        return SimpleNamespace(status="idle")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.request_bridge",
        lambda reservation_id, body, timeout: {
            "state": "ready",
            "readiness": {
                **_ready_receipt_for(record, request),
                "provider_receipt_kind": "codex-thread-start",
            },
        },
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.create_terminal",
        fake_create_terminal,
    )

    ready = await managed_launch.launch_reserved(request.reservation_id)
    duplicate = await managed_launch.launch_reserved(request.reservation_id)
    assert ready["state"] == duplicate["state"] == "ready"
    assert [call[0] for call in calls] == ["write", "create"]
    written = calls[0][2]
    assert written["rendezvous_identity"]["project"] == request.project
    assert written["rendezvous_identity"]["task_id"] == request.task_id
    assert written["delivery_id"] == request.delivery_id


@pytest.mark.asyncio
async def test_launch_reserved_finalizes_durable_bridge_failure_without_readiness_wait(
    isolated_memory_db, tmp_path, monkeypatch
):
    request = _reserve_request(tmp_path)
    _commit_fixture_worktree(tmp_path)
    record, _ = managed_launch.reserve(request)
    failure_state = _launch_failure_state(record, request)
    calls = []

    monkeypatch.setattr(managed_launch, "_executable_identity", lambda _: ("/provider", "d" * 64))
    monkeypatch.setattr(bridge, "profile_digest", lambda _: "e" * 64)
    monkeypatch.setattr(
        bridge,
        "write_request",
        lambda reservation_id, body: calls.append(("write", reservation_id, body)),
    )
    monkeypatch.setattr(bridge, "read_state", lambda reservation_id: failure_state)

    async def fail_create_terminal(**kwargs):
        calls.append(("create", kwargs))
        raise RuntimeError("bridge process exited before readiness")

    def must_not_wait_for_readiness(*args, **kwargs):
        calls.append(("readiness-wait", args, kwargs))
        raise AssertionError("durable launch failure must bypass the readiness timeout")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.create_terminal",
        fail_create_terminal,
    )
    monkeypatch.setattr(bridge, "request_bridge", must_not_wait_for_readiness)

    failed = await managed_launch.launch_reserved(request.reservation_id)
    assert failed["state"] == "launch-failed-bridge"
    assert failed["admission"]["status"] == "never-submitted"
    assert failed["admission"]["delivery_id"] == request.delivery_id
    assert [call[0] for call in calls] == ["write", "create"]


@pytest.mark.asyncio
async def test_v1_long_worktree_uses_exact_admission_identity_in_bounded_launcher_mapping(
    isolated_memory_db, tmp_path, monkeypatch
):
    worktree = tmp_path
    for index in range(7):
        worktree = worktree / f"cond0081-unchanged-long-worktree-segment-{index}"
    worktree.mkdir(parents=True)
    request = _reserve_request(
        worktree,
        project="cao-conductor-self-heal",
        task_id="self-heal-control-plane-recovery-fix-cond0081-activation-observation",
    )
    _commit_fixture_worktree(worktree)
    record, _ = managed_launch.reserve(request)
    written = {}

    monkeypatch.setattr(managed_launch, "_executable_identity", lambda _: ("/provider", "d" * 64))
    monkeypatch.setattr(bridge, "profile_digest", lambda _: "e" * 64)
    monkeypatch.setattr(
        bridge,
        "write_request",
        lambda reservation_id, body: written.update(
            {"reservation_id": reservation_id, "body": body}
        ),
    )

    async def fake_create_terminal(**kwargs):
        assert kwargs["working_directory"] == str(worktree.resolve())
        return SimpleNamespace(status="idle")

    monkeypatch.setattr(
        bridge,
        "request_bridge",
        lambda reservation_id, body, timeout: {
            "state": "ready",
            "readiness": _ready_receipt_for(record, request),
        },
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.create_terminal",
        fake_create_terminal,
    )

    result = await managed_launch.launch_reserved(request.reservation_id)

    identity = written["body"]["rendezvous_identity"]
    assert result["state"] == "ready"
    assert written["body"]["delivery_id"] == request.delivery_id
    assert identity["project"] == request.project
    assert identity["task_id"] == request.task_id
    assert identity["task_id"] != request.reservation_id
    assert identity["worktree_realpath"] == str(worktree.resolve())
    assert len(os.fsencode(identity["worktree_realpath"])) > bridge._AF_UNIX_SAFE_PATH_BYTES
    assert len(os.fsencode(bridge.rendezvous_paths(identity)["socket"])) <= (
        bridge._AF_UNIX_SAFE_PATH_BYTES
    )


@pytest.mark.asyncio
async def test_kimi_launch_uses_exact_provider_bridge_route(
    isolated_memory_db, tmp_path, monkeypatch
):
    request = _reserve_request(
        tmp_path,
        provider="kimi_cli",
        agent_profile="kimi-k3-max-fix",
        trusted_project_root=None,
        expected_model="kimi-code/k3",
        expected_effort="max",
    )
    _commit_fixture_worktree(tmp_path)
    record, _ = managed_launch.reserve(request)
    calls = []

    monkeypatch.setattr(managed_launch, "_executable_identity", lambda _: ("/provider", "d" * 64))
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.profile_digest",
        lambda _: "e" * 64,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.write_request",
        lambda reservation_id, body: calls.append(("write", reservation_id, body)),
    )

    async def fake_create_terminal(**kwargs):
        calls.append(("create", kwargs))
        assert kwargs["initial_message"] is None
        assert kwargs["reserved_terminal_id"] == record["terminal_id"]
        assert kwargs["expected_model"] == "kimi-code/k3"
        assert kwargs["expected_effort"] == "max"
        assert kwargs["managed_native_command"][0]
        return SimpleNamespace(status="idle")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.request_bridge",
        lambda reservation_id, body, timeout: {
            "state": "ready",
            "readiness": {
                **_ready_receipt_for(record, request),
                "provider_receipt_kind": "kimi-acp-session-new",
            },
        },
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.create_terminal",
        fake_create_terminal,
    )

    ready = await managed_launch.launch_reserved(request.reservation_id)
    assert ready["state"] == "ready"
    assert ready["readiness"]["model"] == "kimi-code/k3"
    assert ready["readiness"]["effort"] == "max"
    assert [call[0] for call in calls] == ["write", "create"]


@pytest.mark.asyncio
async def test_send_failure_is_preserved_and_never_retried(
    isolated_memory_db, tmp_path, monkeypatch
):
    request = _reserve_request(tmp_path)
    _ready_record(request)
    admission = _admit_request()
    calls = []

    def fail_after_possible_send(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("response lost")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.request_bridge",
        fail_after_possible_send,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.read_state",
        lambda _: None,
    )
    ambiguous = await managed_launch.admit_reserved(request.reservation_id, admission)
    duplicate = await managed_launch.admit_reserved(request.reservation_id, admission)

    assert ambiguous["admission"]["status"] == "ambiguous_preserved"
    assert duplicate["admission"]["status"] == "ambiguous_preserved"
    assert len(calls) == 1


def test_zero_task_route_attestation_is_provider_bound(tmp_path, monkeypatch):
    calls = []

    def fake_attest(root, *, expected_model, expected_effort):
        calls.append((root, expected_model, expected_effort))
        return {
            "model": expected_model,
            "reasoning_effort": expected_effort,
            "no_prompt_sent": True,
        }

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.kimi_route.attest_kimi_route",
        fake_attest,
    )
    request = ManagedLaunchRouteAttestRequest(
        protocol_version=PROTOCOL_VERSION,
        provider="kimi_cli",
        agent_profile="kimi-k3-max-fix",
        working_directory=str(tmp_path),
        expected_model="kimi-code/k3",
        expected_effort="max",
    )
    receipt = managed_launch.attest_route(request)

    assert receipt["no_task_admitted"] is True
    assert receipt["model"] == "kimi-code/k3"
    assert receipt["effort"] == "max"
    assert calls == [(str(tmp_path), "kimi-code/k3", "max")]


def _attest_request(tmp_path, provider, **changes):
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "provider": provider,
        "agent_profile": "reviewer",
        "working_directory": str(tmp_path),
        "expected_model": "claude-opus-5",
        "expected_effort": "xhigh",
    }
    payload.update(changes)
    return ManagedLaunchRouteAttestRequest(**payload)


class TestRouteAttestationDispatchesByProvider:
    """One attestor per provider, chosen by name, never by falling through.

    The accepted provider set was widened to include ``claude_code`` when
    the reserve request was, while the dispatch was still "codex, or
    else" — so a Claude attestation ran the *Kimi* binary and returned a
    Kimi receipt under ``provider: "claude_code"``. A breaker reading that
    would open a Claude route on another provider's evidence, which is
    exactly the substitution a route attestation exists to prevent.
    """

    def test_claude_reaches_the_claude_attestor_and_not_the_kimi_one(self, tmp_path, monkeypatch):
        called = []

        def _claude(root, *, expected_model, expected_effort):
            called.append("claude")
            return {"probe_version": "claude-cli-route-v1", "no_prompt_sent": True}

        def _kimi(root, *, expected_model, expected_effort):
            called.append("kimi")
            return {"probe_version": "kimi-acp-route-v1", "no_prompt_sent": True}

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.claude_route.attest_claude_route", _claude
        )
        monkeypatch.setattr("cli_agent_orchestrator.services.kimi_route.attest_kimi_route", _kimi)

        receipt = managed_launch.attest_route(_attest_request(tmp_path, "claude_code"))

        assert called == ["claude"]
        assert receipt["provider"] == "claude_code"
        assert receipt["provider_route_receipt"]["probe_version"] == "claude-cli-route-v1"

    def test_kimi_still_reaches_the_kimi_attestor(self, tmp_path, monkeypatch):
        """The fix is a dispatch, not a redirect."""
        called = []

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.kimi_route.attest_kimi_route",
            lambda root, **kw: called.append("kimi") or {"probe_version": "kimi-acp-route-v1"},
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.claude_route.attest_claude_route",
            lambda root, **kw: called.append("claude") or {},
        )

        managed_launch.attest_route(_attest_request(tmp_path, "kimi_cli"))

        assert called == ["kimi"]

    def test_claude_attestation_is_available_rather_than_a_dead_end(self, tmp_path, monkeypatch):
        """Refusing every Claude attestation would wedge recovery instead.

        The breaker calls this to decide whether one new launch attempt is
        permitted; an endpoint that always refuses for Claude leaves a
        failed managed Claude route unrecoverable. So the receipt is real
        — it just names what it did not observe.
        """
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.claude_route.shutil.which",
            lambda binary: "/usr/local/bin/claude",
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.claude_route.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="2.1.220 (Claude Code)\n"),
        )

        receipt = managed_launch.attest_route(_attest_request(tmp_path, "claude_code"))
        provider_receipt = receipt["provider_route_receipt"]

        assert receipt["no_task_admitted"] is True
        assert provider_receipt["claude_version"] == "2.1.220"
        # Requested is not resolved, and the receipt says so in both
        # directions rather than leaving the reader to infer it.
        assert provider_receipt["requested_model"] == "claude-opus-5"
        assert provider_receipt["observed_model"] is None
        assert provider_receipt["observed_effort"] is None
        assert provider_receipt["pre_turn_route_surface"] is False
        assert provider_receipt["unobserved_reason"]

    def test_an_unlisted_claude_build_is_attested_under_open_enforcement(
        self, tmp_path, monkeypatch
    ):
        """Unpinned: an unlisted parseable build is probed, not refused.

        The receipt names exactly what was observed — the requested route
        as requested, no pre-turn resolution — so admitting an unlisted
        build asserts nothing beyond the observation itself.
        """
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.claude_route.shutil.which",
            lambda binary: "/usr/local/bin/claude",
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.claude_route.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="2.1.218\n"),
        )

        receipt = managed_launch.attest_route(_attest_request(tmp_path, "claude_code"))

        assert receipt["no_task_admitted"] is True
        assert receipt["provider_route_receipt"]["claude_version"] == "2.1.218"

    def test_strict_mode_quarantines_the_unlisted_claude_build(self, tmp_path, monkeypatch):
        """The opt-in quarantine still refuses an unlisted build."""
        monkeypatch.setenv("CAO_PROVIDER_VERSION_ENFORCEMENT_CLAUDE", "strict")
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.claude_route.shutil.which",
            lambda binary: "/usr/local/bin/claude",
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.claude_route.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="2.1.218\n"),
        )

        with pytest.raises(managed_launch.ManagedLaunchConflict):
            managed_launch.attest_route(_attest_request(tmp_path, "claude_code"))

    @pytest.mark.parametrize("mode", ["open", "strict"])
    def test_an_unparseable_claude_banner_is_refused_in_every_mode(
        self, tmp_path, monkeypatch, mode
    ):
        """Unparseable is a failed observation, distinct from unlisted."""
        monkeypatch.setenv("CAO_PROVIDER_VERSION_ENFORCEMENT_CLAUDE", mode)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.claude_route.shutil.which",
            lambda binary: "/usr/local/bin/claude",
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.claude_route.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="garbage banner\n"),
        )

        with pytest.raises(managed_launch.ManagedLaunchConflict):
            managed_launch.attest_route(_attest_request(tmp_path, "claude_code"))

    def test_trusted_project_root_stays_codex_only_for_claude(self, tmp_path):
        with pytest.raises(managed_launch.ManagedLaunchConflict):
            managed_launch.attest_route(
                _attest_request(tmp_path, "claude_code", trusted_project_root=str(tmp_path))
            )


def _codex_app_server_stdout(root: str) -> str:
    """A codex app-server exchange that echoes the attested route.

    ``expected_model``/``expected_effort`` in the request are the failure
    domain being attested; the mocked thread/start resolves to the same
    values so the zero-turn route proof validates.  The exchange is the
    exact one the route attestor sends: initialize, config/read, an
    ephemeral thread/start, and never a turn/start.
    """
    responses = [
        {"id": 1, "result": {"serverInfo": {"name": "codex"}}},
        {
            "id": 2,
            "result": {
                "config": {"projects": {root: {"trust_level": "trusted"}}},
                "origins": {"projects": {root: {"trust_level": "sessionFlags"}}},
                "layers": [],
            },
        },
        {
            "id": 3,
            "result": {
                "cwd": root,
                "model": "claude-opus-5",
                "modelProvider": "openai",
                "reasoningEffort": "xhigh",
                "thread": {"id": "thread-zero-turn"},
            },
        },
    ]
    return "".join(json.dumps(item) + "\n" for item in responses)


class TestCodexRouteAttestationAdmitsTheStageProvenBuild:
    """The public ``attest_route`` seam (the ``--attest-launch-domain`` path).

    The launch breaker calls this endpoint to re-arm one launch attempt
    after a failure.  A Codex CLI 0.147.0 install was refused here with
    HTTP 409 ("expected codex-cli 0.146.0, observed codex-cli 0.147.0"),
    leaving the breaker impossible to re-arm. The capability-scoped
    admission makes 0.147.0 attest with the same zero-task, no-task-admitted
    receipt while 0.148.0 still maps to the 409 conflict.
    """

    def _patch_codex_attestor(self, monkeypatch, banner: str, tmp_path):
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.codex_trust.subprocess.run",
            lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=banner, stderr=""),
        )

        def fake_app_server(argv, requests, timeout):
            return _codex_app_server_stdout(str(tmp_path.resolve())), "", -15

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.codex_trust._run_app_server_probe",
            fake_app_server,
        )
        # The public seam digests ~/.codex/config.toml before and after the
        # probe to prove the probe never mutates it. The operator's real file
        # is an undeclared input: absent in CI (digests as "absent"), but a
        # dotfile-managed symlink on some dev machines, which the digester
        # rightly refuses. Point both reads at a fixture file so the
        # before/after integrity comparison still runs against real content.
        from cli_agent_orchestrator.services import codex_trust

        fixture_config = tmp_path / "codex-config.toml"
        fixture_config.write_text('model = "gpt-5"\n')
        real_digest = codex_trust._digest_or_absent
        monkeypatch.setattr(
            codex_trust,
            "_digest_or_absent",
            lambda path: real_digest(fixture_config),
        )

    def test_codex_0147_attests_zero_tasks_at_the_public_seam(self, tmp_path, monkeypatch):
        self._patch_codex_attestor(monkeypatch, "codex-cli 0.147.0\n", tmp_path)

        receipt = managed_launch.attest_route(
            _attest_request(tmp_path, "codex", trusted_project_root=str(tmp_path))
        )

        assert receipt["no_task_admitted"] is True
        assert receipt["provider"] == "codex"
        provider_receipt = receipt["provider_route_receipt"]
        assert provider_receipt["codex_version"] == "codex-cli 0.147.0"
        assert provider_receipt["no_turn_started"] is True
        assert provider_receipt["trust_level"] == "trusted"
        assert provider_receipt["config_origin"] == "sessionFlags"

    def test_codex_0146_keeps_attesting_at_the_public_seam(self, tmp_path, monkeypatch):
        self._patch_codex_attestor(monkeypatch, "codex-cli 0.146.0\n", tmp_path)

        receipt = managed_launch.attest_route(
            _attest_request(tmp_path, "codex", trusted_project_root=str(tmp_path))
        )
        assert receipt["no_task_admitted"] is True
        assert receipt["provider_route_receipt"]["codex_version"] == "codex-cli 0.146.0"

    def test_codex_0148_still_refuses_at_the_public_seam(self, tmp_path, monkeypatch):
        self._patch_codex_attestor(monkeypatch, "codex-cli 0.148.0\n", tmp_path)

        with pytest.raises(managed_launch.ManagedLaunchConflict, match="unsupported Codex version"):
            managed_launch.attest_route(
                _attest_request(tmp_path, "codex", trusted_project_root=str(tmp_path))
            )


def test_cleanup_is_exact_idempotent_and_refuses_admitted_generation(
    isolated_memory_db, tmp_path, monkeypatch
):
    request = _reserve_request(tmp_path)
    record, _ = managed_launch.reserve(request)
    record = managed_launch.mark_preflight_blocked(
        request.reservation_id,
        preflight_class="trust-preauthorization",
        detail="blocked before task admission",
    )
    calls = []
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.delete_terminal",
        lambda terminal_id, registry=None, expected_generation=None, expected_session=None: (
            calls.append((terminal_id, expected_generation, expected_session)) or True
        ),
    )
    backend = SimpleNamespace(window_exists=lambda session, window: False)
    monkeypatch.setattr(
        "cli_agent_orchestrator.backends.registry.get_backend",
        lambda: backend,
    )
    cleanup = ManagedLaunchCleanupRequest(
        protocol_version=PROTOCOL_VERSION,
        cleanup_id=str(uuid.uuid4()),
        terminal_id=record["terminal_id"],
        generation=record["generation"],
    )

    first = managed_launch.cleanup_reserved(request.reservation_id, cleanup)
    second = managed_launch.cleanup_reserved(request.reservation_id, cleanup)
    assert first["state"] == second["state"] == "cleaned"
    assert first["cleanup"]["generation"] == record["generation"]
    assert calls == [(record["terminal_id"], record["generation"], record["session_name"])]

    admitted_request = _reserve_request(tmp_path)
    _ready_record(admitted_request)
    admission = _admit_request()
    admitting, _ = managed_launch.claim_admission(admitted_request.reservation_id, admission)
    managed_launch.complete_admission(
        admitted_request.reservation_id,
        admission.delivery_id,
        _submission_receipt(admitting, admission),
    )
    admitted = managed_launch.get(admitted_request.reservation_id)
    wrong = cleanup.model_copy(
        update={
            "cleanup_id": str(uuid.uuid4()),
            "terminal_id": admitted["terminal_id"],
            "generation": admitted["generation"],
        }
    )
    with pytest.raises(managed_launch.ManagedLaunchConflict):
        managed_launch.cleanup_reserved(admitted_request.reservation_id, wrong)


# -- P1-5: provider-native allowlisted receipt kinds (spec §20.2e, §20.3 31(5))


def test_pane_id_readiness_kind_rejected_before_state_advance(isolated_memory_db, tmp_path):
    request = _reserve_request(tmp_path)
    record, _ = managed_launch.reserve(request)
    managed_launch.claim_launch(request.reservation_id)
    receipt = _ready_receipt_for(record, request)
    receipt["provider_receipt_kind"] = "pane-id"
    with pytest.raises(managed_launch.ManagedLaunchConflict):
        managed_launch.mark_ready(
            request.reservation_id,
            terminal_id=record["terminal_id"],
            generation=record["generation"],
            receipt=receipt,
        )
    assert managed_launch.get(request.reservation_id)["state"] == "launching"


def test_wrong_kind_readiness_rejected(isolated_memory_db, tmp_path):
    request = _reserve_request(tmp_path)
    record, _ = managed_launch.reserve(request)
    managed_launch.claim_launch(request.reservation_id)
    receipt = _ready_receipt_for(record, request)
    receipt["provider_receipt_kind"] = "codex-turn-start"  # turn kind, not session
    with pytest.raises(managed_launch.ManagedLaunchConflict):
        managed_launch.mark_ready(
            request.reservation_id,
            terminal_id=record["terminal_id"],
            generation=record["generation"],
            receipt=receipt,
        )
    assert managed_launch.get(request.reservation_id)["state"] == "launching"


def test_readiness_receipt_id_must_be_provider_session(isolated_memory_db, tmp_path):
    request = _reserve_request(tmp_path)
    record, _ = managed_launch.reserve(request)
    managed_launch.claim_launch(request.reservation_id)
    receipt = _ready_receipt_for(record, request)
    receipt["receipt_id"] = "locally-minted"
    with pytest.raises(managed_launch.ManagedLaunchConflict):
        managed_launch.mark_ready(
            request.reservation_id,
            terminal_id=record["terminal_id"],
            generation=record["generation"],
            receipt=receipt,
        )
    assert managed_launch.get(request.reservation_id)["state"] == "launching"


def test_wrong_kind_submission_rejected_before_admitted(isolated_memory_db, tmp_path):
    request = _reserve_request(tmp_path)
    _ready_record(request)
    admission = _admit_request()
    admitting, _ = managed_launch.claim_admission(request.reservation_id, admission)
    receipt = _submission_receipt(admitting, admission)
    receipt["provider_receipt_kind"] = "kimi-session-update"  # wrong provider
    with pytest.raises(managed_launch.ManagedLaunchConflict):
        managed_launch.complete_admission(request.reservation_id, admission.delivery_id, receipt)
    assert managed_launch.get(request.reservation_id)["state"] == "admitting"


def test_submission_receipt_id_must_be_provider_turn(isolated_memory_db, tmp_path):
    request = _reserve_request(tmp_path)
    _ready_record(request)
    admission = _admit_request()
    admitting, _ = managed_launch.claim_admission(request.reservation_id, admission)
    receipt = _submission_receipt(admitting, admission)
    receipt["receipt_id"] = "conductor-fabricated-uuid"
    with pytest.raises(managed_launch.ManagedLaunchConflict):
        managed_launch.complete_admission(request.reservation_id, admission.delivery_id, receipt)
    assert managed_launch.get(request.reservation_id)["state"] == "admitting"


# -- P1-8/P1-9: complete readiness schema + pinned executable (§20.2f) -------


def test_readiness_without_provider_version_rejected(isolated_memory_db, tmp_path):
    # §20.2f P1-8: the provider version is mandatory before ready persists.
    request = _reserve_request(tmp_path)
    record, _ = managed_launch.reserve(request)
    managed_launch.claim_launch(request.reservation_id)
    receipt = _ready_receipt_for(record, request)
    del receipt["provider_version"]
    with pytest.raises(managed_launch.ManagedLaunchConflict):
        managed_launch.mark_ready(
            request.reservation_id,
            terminal_id=record["terminal_id"],
            generation=record["generation"],
            receipt=receipt,
        )
    assert managed_launch.get(request.reservation_id)["state"] == "launching"


def test_readiness_without_explicit_model_input_ready_rejected(isolated_memory_db, tmp_path):
    # §20.2f P1-8: model_input_ready must be explicitly true — omission and
    # false both fail closed.
    request = _reserve_request(tmp_path)
    record, _ = managed_launch.reserve(request)
    managed_launch.claim_launch(request.reservation_id)
    for value in (None, False):
        receipt = _ready_receipt_for(record, request)
        receipt["model_input_ready"] = value
        with pytest.raises(managed_launch.ManagedLaunchConflict):
            managed_launch.mark_ready(
                request.reservation_id,
                terminal_id=record["terminal_id"],
                generation=record["generation"],
                receipt=receipt,
            )
    assert managed_launch.get(request.reservation_id)["state"] == "launching"


def test_reserve_without_pinned_executable_refused(isolated_memory_db, tmp_path):
    # §20.2f P1-9: no pinned executable identity → fail closed at reservation;
    # ambient PATH resolution is never a fallback.
    for changes in (
        {"provider_executable": None},
        {"provider_executable_sha256": None},
        {"provider_executable": None, "provider_executable_sha256": None},
        {"provider_executable": "kimi"},  # non-absolute
    ):
        request = _reserve_request(tmp_path, **changes)
        with pytest.raises(Exception):
            managed_launch.reserve(request)


def test_malformed_executable_digest_rejected_by_schema(tmp_path):
    with pytest.raises(Exception):
        _reserve_request(tmp_path, provider_executable_sha256="Z" * 64)


@pytest.mark.asyncio
async def test_launch_refused_when_pinned_executable_digest_drifts(isolated_memory_db, tmp_path):
    # §20.2f P1-9: the pinned executable changed after reservation → the
    # launch fails closed (preflight-blocked) with zero provider I/O.
    request = _reserve_request(tmp_path)
    managed_launch.reserve(request)
    executable = tmp_path / "fake-provider"
    executable.write_text("#!/bin/sh\nexit 1\n")  # drift after the pin
    result = await managed_launch.launch_reserved(request.reservation_id)
    assert result["state"] == "preflight_blocked"
    details = [obs.get("detail", "") for obs in result.get("observations") or []]
    assert any("digest" in detail for detail in details), details


# -- P1-7/P1-10: companion producers (final conformance §20.2f) ---------------


def test_ready_and_admission_publish_exact_companion_receipts(
    isolated_memory_db, tmp_path, monkeypatch
):
    # The REAL producer path (no injected sentinel boundary): the validated
    # provider-native readiness/submission receipts publish the generation-
    # bound route identity and the message-turn acknowledgement.
    from cli_agent_orchestrator.services import companion_receipts

    monkeypatch.setattr(companion_receipts, "COMPANION_DIR", tmp_path / "companion")
    request = _reserve_request(tmp_path)
    record, _ = managed_launch.reserve(request)
    managed_launch.claim_launch(request.reservation_id)
    receipt = _ready_receipt_for(record, request)
    managed_launch.mark_ready(
        request.reservation_id,
        terminal_id=record["terminal_id"],
        generation=record["generation"],
        receipt=receipt,
    )
    route = companion_receipts.get_route(record["terminal_id"], record["generation"])
    assert route["receipt_id"] == "provider-session-ready-opaque"
    assert route["turn_id"] == "provider-session-ready-opaque"
    assert route["provider_version"] == "0.146.0"
    assert route["generation"] == record["generation"]
    # a replacement generation is never served this receipt
    assert companion_receipts.get_route(record["terminal_id"], str(uuid.uuid4())) is None

    admission = _admit_request()
    admitting, _ = managed_launch.claim_admission(request.reservation_id, admission)
    managed_launch.complete_admission(
        request.reservation_id,
        admission.delivery_id,
        _submission_receipt(admitting, admission),
    )
    # per-turn route identity moved to the exact provider turn
    route = companion_receipts.get_route(record["terminal_id"], record["generation"])
    assert route["turn_id"] == "provider-turn-opaque"
    # the message-turn acknowledgement binds message id + digest, receiver
    # generation, and provider session/turn — and carries no message body
    ack = companion_receipts.get_message_ack(
        record["terminal_id"], record["generation"], admission.delivery_id
    )
    assert ack["kind"] == "submitted"
    assert ack["message_id"] == admission.delivery_id
    assert ack["message_sha256"] == admission.message_sha256
    assert ack["receiver_generation"] == record["generation"]
    assert ack["provider_turn_id"] == "provider-turn-opaque"
    assert ack["provider_session_id"] == "provider-session-ready-opaque"
    assert "message" not in ack


def test_deliver_inbox_via_bridge_exact_binding(isolated_effect_admission, tmp_path, monkeypatch):
    # P1-7: one exact queued inbox message is submitted through the receiver's
    # live managed bridge; anything else falls back WITHOUT an ack.
    request = _reserve_request(tmp_path)
    record = _ready_record(request)
    admission = _admit_request()
    admitting, _ = managed_launch.claim_admission(request.reservation_id, admission)
    record = managed_launch.complete_admission(
        request.reservation_id,
        admission.delivery_id,
        _submission_receipt(admitting, admission),
    )
    calls = []
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.request_bridge",
        lambda reservation_id, body, timeout: calls.append(body) or {"ok": True},
    )
    assert managed_launch.deliver_inbox_via_bridge(
        record["terminal_id"], message_id=7, message="ping", sender_id="sup-1"
    )
    assert len(calls) == 1
    body = calls[0]
    assert body["op"] == "deliver"
    assert body["message_id"] == "7"
    assert body["message_sha256"] == hashlib.sha256(b"ping").hexdigest()
    assert body["sender_id"] == "sup-1"
    assert body["reservation_id"] == request.reservation_id
    # unmanaged terminal: no bridge, no ack — ordinary path, never inferred
    assert not managed_launch.deliver_inbox_via_bridge(
        "ffffffff", message_id=8, message="ping", sender_id="sup-1"
    )
    assert len(calls) == 1


def test_deliver_inbox_via_bridge_unavailable_is_no_ack_fallback(
    isolated_effect_admission, tmp_path, monkeypatch
):
    request = _reserve_request(tmp_path)
    record = _ready_record(request)

    def unavailable(*_a, **_k):
        raise RuntimeError("bridge socket gone")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.request_bridge",
        unavailable,
    )
    assert not managed_launch.deliver_inbox_via_bridge(
        record["terminal_id"], message_id=7, message="ping", sender_id="sup-1"
    )


def test_stop_barrier_refuses_managed_inbox_before_bridge(isolated_effect_admission, monkeypatch):
    identity = {
        "reservation_id": "reservation-1",
        "terminal_id": "deadbeef",
        "generation": "generation-1",
        "session_name": "cao-test",
        "provider": "codex",
        "state": "admitted",
    }
    monkeypatch.setattr(managed_launch, "managed_control_identity", lambda _tid: identity)
    operation_journal.claim_session_barrier("cao-test", claimed_by="stop-operation")
    refusals = []
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.callback_recovery.mark_delivery_refused",
        lambda operation_key, **kwargs: refusals.append((operation_key, kwargs)),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.request_bridge",
        lambda *_args, **_kwargs: pytest.fail("bridge must not receive a post-Stop inbox payload"),
    )

    assert not managed_launch.deliver_inbox_via_bridge(
        "deadbeef",
        message_id=7,
        message="ping",
        sender_id="supervisor",
        recovery_operation_key="recovery-1",
    )
    assert refusals == [
        (
            "recovery-1",
            {
                "reason_code": "session-effect-barrier",
                "proven_before_provider_io": True,
            },
        )
    ]


def test_managed_control_identity_does_not_hide_missing_columns(isolated_memory_db):
    """Schema drift must fail closed: a missing column in the v2
    reservations table is never silently converted into "unmanaged"."""
    import sqlite3

    from sqlalchemy import text

    # The v2 table exists but is missing the roster additive column the
    # roster owns — the narrow projection must still resolve the managed
    # identity rather than erasing it.
    with isolated_memory_db.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS managed_launch_v2_reservations ("
                "reservation_id TEXT PRIMARY KEY, "
                "terminal_id TEXT NOT NULL UNIQUE, "
                "generation TEXT NOT NULL UNIQUE, "
                "protocol_vintage TEXT NOT NULL, "
                "session_name TEXT NOT NULL, "
                "provider TEXT NOT NULL, "
                "agent_profile TEXT NOT NULL, "
                "caller_id TEXT NOT NULL, "
                "working_directory TEXT NOT NULL, "
                "obligation_generation TEXT NOT NULL, "
                "run_id TEXT NOT NULL, "
                "launch_nonce_digest TEXT NOT NULL, "
                "state TEXT NOT NULL, "
                "request_json TEXT NOT NULL, "
                "created_at TEXT NOT NULL, "
                "updated_at TEXT NOT NULL"
                ")"
            )
        )
        # A managed-v2 reservation row without the additive column.
        conn.execute(
            text(
                "INSERT INTO managed_launch_v2_reservations("
                "reservation_id, terminal_id, generation, protocol_vintage, "
                "session_name, provider, agent_profile, caller_id, "
                "working_directory, obligation_generation, run_id, "
                "launch_nonce_digest, state, request_json, created_at, updated_at"
                ") VALUES ("
                "'11111111-1111-4111-8111-111111111111', 'a1b2c3d4', "
                "'00000000-0000-4000-8000-000000000001', 'v2', "
                "'cao-test', 'codex', 'developer', 'deadbeef', '/tmp/repo', "
                "'obgen-1', 'run-1', 'n' * 40, 'admitted', '{}', 't', 't')"
            )
        )
        conn.commit()

    identity = managed_launch.managed_control_identity("a1b2c3d4")
    assert identity is not None
    assert identity["vintage"] == "v2"
    assert identity["terminal_id"] == "a1b2c3d4"
    assert identity["controllable"] is True

    # A column the projection DOES read, removed = genuine drift: fail
    # closed (raise), never "unmanaged".
    with isolated_memory_db.begin() as conn:
        conn.execute(text("ALTER TABLE managed_launch_v2_reservations DROP COLUMN state"))
        conn.commit()
    with pytest.raises(Exception):
        managed_launch.managed_control_identity("a1b2c3d4")


def test_input_carrying_managed_operation_arms_processing_transition(
    isolated_effect_admission, monkeypatch
):
    identity = {
        "reservation_id": "reservation-1",
        "terminal_id": "deadbeef",
        "generation": "generation-1",
        "session_name": "cao-test",
        "provider": "codex",
        "controllable": True,
    }
    events = []
    monkeypatch.setattr(managed_launch, "managed_control_identity", lambda _tid: identity)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.notify_input_sent",
        lambda terminal_id: events.append(("armed", terminal_id)),
    )

    def request_bridge(_reservation_id, _command, *, timeout):
        events.append(("submitted", timeout))
        return {"receipt": {"state": "accepted"}}

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.request_bridge",
        request_bridge,
    )

    managed_launch.begin_managed_session_operation(
        "deadbeef",
        operation_id="operation-1",
        action="follow-up",
        generation="generation-1",
        message="continue",
    )

    assert events == [("armed", "deadbeef"), ("submitted", 45.0)]


def test_read_only_managed_operation_does_not_arm_processing_transition(
    isolated_effect_admission, monkeypatch
):
    identity = {
        "reservation_id": "reservation-1",
        "terminal_id": "deadbeef",
        "generation": "generation-1",
        "session_name": "cao-test",
        "provider": "codex",
        "controllable": True,
    }
    armed = []
    monkeypatch.setattr(managed_launch, "managed_control_identity", lambda _tid: identity)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.notify_input_sent",
        armed.append,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.request_bridge",
        lambda *_args, **_kwargs: {"receipt": {"state": "completed"}},
    )

    managed_launch.begin_managed_session_operation(
        "deadbeef",
        operation_id="operation-1",
        action="route-query",
        generation="generation-1",
    )

    assert armed == []


def test_stop_barrier_refuses_managed_operation_before_status_or_bridge(
    isolated_effect_admission, monkeypatch
):
    identity = {
        "reservation_id": "reservation-1",
        "terminal_id": "deadbeef",
        "generation": "generation-1",
        "session_name": "cao-test",
        "provider": "codex",
        "controllable": True,
    }
    monkeypatch.setattr(managed_launch, "managed_control_identity", lambda _tid: identity)
    operation_journal.claim_session_barrier("cao-test", claimed_by="stop-operation")
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.notify_input_sent",
        lambda *_args: pytest.fail("status must not arm after Stop"),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.request_bridge",
        lambda *_args, **_kwargs: pytest.fail("bridge must not receive a post-Stop effect"),
    )

    with pytest.raises(managed_launch.ManagedLaunchConflict, match="Stop barrier is claimed"):
        managed_launch.begin_managed_session_operation(
            "deadbeef",
            operation_id="operation-1",
            action="follow-up",
            generation="generation-1",
            message="continue",
        )
