from __future__ import annotations

import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
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


def _reserve_request(tmp_path, **changes):
    payload = {
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
    }
    payload.update(changes)
    return ManagedLaunchReserveRequest(**payload)


def _admit_request(message="review the exact head", **changes):
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "delivery_id": str(uuid.uuid4()),
        "message": message,
        "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
        "sender_id": "deadbeef",
        "orchestration_type": "assign",
    }
    payload.update(changes)
    return ManagedLaunchAdmitRequest(**payload)


def _ready_record(request):
    record, _ = managed_launch.reserve(request)
    record, should_launch = managed_launch.claim_launch(request.reservation_id)
    assert should_launch
    receipt = {
        "receipt_id": str(uuid.uuid4()),
        "terminal_id": record["terminal_id"],
        "generation": record["generation"],
        "provider": record["provider"],
        "agent_profile": record["agent_profile"],
        "model": request.expected_model,
        "effort": request.expected_effort,
    }
    return managed_launch.mark_ready(
        request.reservation_id,
        terminal_id=record["terminal_id"],
        generation=record["generation"],
        receipt=receipt,
    )


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
    claimed, should_send = managed_launch.claim_admission(request.reservation_id, admission)
    duplicate, should_send_again = managed_launch.claim_admission(request.reservation_id, admission)
    assert should_send is True
    assert should_send_again is False
    assert claimed["state"] == duplicate["state"] == "admitting"

    completed = managed_launch.complete_admission(request.reservation_id, admission.delivery_id)
    completed_again = managed_launch.complete_admission(
        request.reservation_id, admission.delivery_id
    )
    assert completed["state"] == completed_again["state"] == "admitted"
    receipt = completed["admission"]["provider_submission_receipt"]
    assert receipt == completed_again["admission"]["provider_submission_receipt"]
    assert receipt["reservation_id"] == request.reservation_id
    assert receipt["delivery_id"] == admission.delivery_id
    assert receipt["terminal_id"] == completed["terminal_id"]
    assert receipt["generation"] == completed["generation"]
    assert receipt["message_sha256"] == admission.message_sha256


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


@pytest.mark.asyncio
async def test_launch_reserved_attests_before_no_task_terminal_start(
    isolated_memory_db, tmp_path, monkeypatch
):
    request = _reserve_request(tmp_path)
    record, _ = managed_launch.reserve(request)
    calls = []

    def fake_attest(root, *, expected_model, expected_effort):
        calls.append(("attest", root, expected_model, expected_effort))
        return {
            "model": expected_model,
            "reasoning_effort": expected_effort,
            "project_root": root,
        }

    async def fake_create_terminal(**kwargs):
        calls.append(("create", kwargs))
        assert kwargs["initial_message"] is None
        assert kwargs["reserved_terminal_id"] == record["terminal_id"]
        assert kwargs["preserve_on_init_failure"] is True
        assert kwargs["expected_model"] == "gpt-5.6-sol"
        assert kwargs["expected_effort"] == "xhigh"
        return SimpleNamespace(status="idle")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.codex_trust.attest_trusted_project",
        fake_attest,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.create_terminal",
        fake_create_terminal,
    )

    ready = await managed_launch.launch_reserved(request.reservation_id)
    duplicate = await managed_launch.launch_reserved(request.reservation_id)
    assert ready["state"] == duplicate["state"] == "ready"
    assert [call[0] for call in calls] == ["attest", "create"]


@pytest.mark.asyncio
async def test_kimi_launch_attests_provider_route_before_no_task_terminal_start(
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
    record, _ = managed_launch.reserve(request)
    calls = []

    def fake_attest(root, *, expected_model, expected_effort):
        calls.append(("attest", root, expected_model, expected_effort))
        return {
            "model": expected_model,
            "reasoning_effort": expected_effort,
            "project_root": root,
            "no_prompt_sent": True,
        }

    async def fake_create_terminal(**kwargs):
        calls.append(("create", kwargs))
        assert kwargs["initial_message"] is None
        assert kwargs["reserved_terminal_id"] == record["terminal_id"]
        assert kwargs["expected_model"] == "kimi-code/k3"
        assert kwargs["expected_effort"] == "max"
        return SimpleNamespace(status="idle")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.kimi_route.attest_kimi_route",
        fake_attest,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.create_terminal",
        fake_create_terminal,
    )

    ready = await managed_launch.launch_reserved(request.reservation_id)
    assert ready["state"] == "ready"
    assert ready["readiness"]["model"] == "kimi-code/k3"
    assert ready["readiness"]["effort"] == "max"
    assert [call[0] for call in calls] == ["attest", "create"]


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
        "cli_agent_orchestrator.services.terminal_service.send_input",
        fail_after_possible_send,
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
        lambda terminal_id, registry=None: calls.append(terminal_id) or False,
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
    assert calls == [record["terminal_id"]]

    admitted_request = _reserve_request(tmp_path)
    _ready_record(admitted_request)
    admission = _admit_request()
    managed_launch.claim_admission(admitted_request.reservation_id, admission)
    managed_launch.complete_admission(admitted_request.reservation_id, admission.delivery_id)
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
