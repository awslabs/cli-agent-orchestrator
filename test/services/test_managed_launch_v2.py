"""Tests for managed-launch v2 (T-ADM-1) and the DB vintage surface (T-MIG-6 fork side)."""

from __future__ import annotations

import hashlib
import subprocess
import uuid
from copy import deepcopy

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.managed_launch_v2 import (
    PROTOCOL_VERSION_V2,
    ManagedLaunchV2AdmitRequest,
    ManagedLaunchV2BindRequest,
    ManagedLaunchV2ReserveRequest,
)
from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services import managed_provider_bridge as bridge
from cli_agent_orchestrator.services.managed_launch import (
    ManagedLaunchConflict,
    ManagedLaunchNotFound,
    ManagedLaunchNotReady,
)
from cli_agent_orchestrator.services.managed_provider_bridge import BRIDGE_VERSION

DELIVERY_ID = "33333333-3333-4333-8333-333333333333"


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
        "delivery_id": DELIVERY_ID,
        "launch_nonce": "n" * 40,
    }
    payload.update(changes)
    return ManagedLaunchV2ReserveRequest(**payload)


def _ready_bridge_state(record, monkeypatch):
    # Each reservation mints its own provider-native session id, exactly as
    # a real launch does; the roster (M3-A) refuses one native id bound to
    # two live incarnations, so the shared fake id would make every second
    # bind in a test collide with the first.
    session_id = f"thr_{uuid.uuid4().hex[:16]}"
    receipt = {
        "bridge_version": BRIDGE_VERSION,
        "receipt_id": session_id,
        "provider_session_id": session_id,
        "provider_receipt_kind": "codex-thread-start",
        "provider_transcript_sha256": "a" * 64,
        "provider_version": "0.146.0",
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


def test_v2_delivery_identity_is_required_and_launch_failure_is_idempotent(
    isolated_memory_db, worktree, tmp_path
):
    payload = _reserve_request(worktree, tmp_path).model_dump(mode="json")
    payload.pop("delivery_id")
    with pytest.raises(ValueError):
        ManagedLaunchV2ReserveRequest(**payload)

    request = _reserve_request(worktree, tmp_path)
    v2.reserve(request)
    record, should_launch = v2.claim_launch(request.reservation_id)
    assert should_launch
    inventory = bridge._environment_inventory("codex", ["HOME", "PATH"])
    failure = bridge._launch_failure(
        {
            "reservation_id": record["reservation_id"],
            "terminal_id": record["terminal_id"],
            "generation": record["generation"],
            "delivery_id": request.delivery_id,
        },
        bridge.BridgeError("provider initialization failed"),
        inventory,
        provider_io_started=False,
    )
    state = {
        "state": "launch-failed-bridge",
        "readiness": None,
        "submission": None,
        "environment_inventory": inventory,
        "launch_failure": failure,
    }
    failed = v2._mark_launch_failed_bridge(request.reservation_id, state)
    repeated = v2._mark_launch_failed_bridge(request.reservation_id, state)
    assert failed == repeated
    assert failed["state"] == "launch-failed-bridge"
    assert failed["admission"]["delivery_id"] == request.delivery_id
    assert failed["admission"]["status"] == "never-submitted"
    assert (
        failed["admission"]["failure_evidence_sha256"]
        == failed["launch_failure"]["evidence_sha256"]
    )

    second = _reserve_request(
        worktree,
        tmp_path,
        delivery_id="55555555-5555-4555-8555-555555555555",
    )
    v2.reserve(second)
    second_record, _ = v2.claim_launch(second.reservation_id)
    bad_state = deepcopy(state)
    bad_state["launch_failure"]["reservation_id"] = second.reservation_id
    bad_state["launch_failure"]["terminal_id"] = second_record["terminal_id"]
    bad_state["launch_failure"]["generation"] = second_record["generation"]
    bad_state["launch_failure"]["delivery_id"] = DELIVERY_ID
    with pytest.raises(ManagedLaunchConflict, match="identity/evidence mismatch"):
        v2._mark_launch_failed_bridge(second.reservation_id, bad_state)
    unchanged = v2.get(second.reservation_id)
    assert unchanged["state"] == "launching"
    assert unchanged["admission"] is None


def test_bind_journals_native_bound(isolated_memory_db, worktree, tmp_path, monkeypatch):
    request = _reserve_request(worktree, tmp_path)
    record, _ = v2.reserve(request)
    v2.claim_launch(record["reservation_id"])
    receipt = _ready_bridge_state(record, monkeypatch)
    bound = v2.bind_native(record["reservation_id"], _bind_request(record))
    assert bound["state"] == "bound"
    binding = bound["binding"]
    assert binding["native_session_id"] == receipt["provider_session_id"]
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
    # Deliberately no longer a conflict. This is the one bind refusal that
    # retrying can resolve, and it must be separable on the wire from a
    # permanent one: a consumer that could not tell them apart inferred
    # transience from the row state instead, which read every permanent
    # conflict leaving the row 'launching' — identity mismatch, mode
    # violation, foreign single-writer holder — as a slow start, polled
    # it, and reported it with the breaker untripped.
    with pytest.raises(ManagedLaunchNotReady, match="ready") as raised:
        v2.bind_native(record["reservation_id"], _bind_request(record))
    assert raised.value.reason == "bind-bridge-not-durably-ready"
    # Still an error, and still not a conflict: a consumer keying on
    # ManagedLaunchConflict must not treat this as permanent.
    assert not isinstance(raised.value, ManagedLaunchConflict)


def test_bind_receipt_unproven_version_refused(isolated_memory_db, worktree, tmp_path, monkeypatch):
    request = _reserve_request(worktree, tmp_path)
    record, _ = v2.reserve(request)
    v2.claim_launch(record["reservation_id"])
    receipt = _ready_bridge_state(record, monkeypatch)
    receipt["provider_version"] = "0.144.6"
    with pytest.raises(ManagedLaunchConflict, match="native readiness proof is unavailable"):
        v2.bind_native(record["reservation_id"], _bind_request(record))


def _admit_request(record, digest, **changes):
    message = "review the exact head"
    payload = {
        "protocol_version": PROTOCOL_VERSION_V2,
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
    wrong_delivery = _admit_request(
        bound,
        digest,
        delivery_id="44444444-4444-4444-8444-444444444444",
    )
    with pytest.raises(ManagedLaunchConflict, match="immutable"):
        v2.claim_admission(record["reservation_id"], wrong_delivery)
    assert v2.get(record["reservation_id"])["admission"] is None
    claimed, should_send = v2.claim_admission(record["reservation_id"], admit)
    assert should_send and claimed["state"] == "admitting"
    again, send_again = v2.claim_admission(record["reservation_id"], admit)
    assert not send_again
    receipt = {
        "receipt_id": "turn-1",
        "provider_session_id": bound["binding"]["native_session_id"],
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


def test_fenced_generation_refuses_acp_admission_before_claim_or_provider_io(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
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

    bridge_calls = []
    monkeypatch.setattr(
        bridge,
        "request_bridge",
        lambda *args, **kwargs: bridge_calls.append((args, kwargs)),
    )

    import asyncio

    refused = asyncio.run(
        v2.admit_reserved(
            record["reservation_id"],
            _admit_request(bound, v2.native_binding_digest(bound)),
        )
    )

    assert refused["admission"]["status"] == "refused"
    assert refused["admission"]["refusal_reason"] == "generation_fenced"
    assert refused["admission"]["retryable"] is False
    assert bridge_calls == []


def test_acp_fence_after_durable_claim_converges_to_permanent_zero_io_refusal(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    """A bridge's known pre-I/O fence result is not response-loss ambiguity.

    This uses the exact request-bridge seam: the claim has committed before
    the bridge call begins, then the bridge's own pre-provider admission
    detects a just-installed W13 fence.  It is deliberately distinct from
    the fence-before-claim test above.
    """
    request = _reserve_request(worktree, tmp_path)
    record, _ = v2.reserve(request)
    v2.claim_launch(record["reservation_id"])
    _ready_bridge_state(record, monkeypatch)
    bound = v2.bind_native(record["reservation_id"], _bind_request(record))
    admit = _admit_request(bound, v2.native_binding_digest(bound))
    from cli_agent_orchestrator.services import generation_fence as gf

    durable_claims = []
    provider_byte_calls = []
    receipt_calls = []

    def request_bridge_after_park(reservation_id, command, *, timeout):
        # ``claim_admission`` committed before `request_bridge` is reached.
        claimed = v2.get(reservation_id)
        durable_claims.append(claimed["admission"])
        assert claimed["state"] == "admitting"
        assert claimed["admission"]["status"] == "io-attempted"
        assert command["delivery_id"] == admit.delivery_id

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
        # This is the bridge's pre-provider boundary.  Both observable
        # provider effects are deliberately below it, so the real fence
        # check must stop them before it returns the closed BridgeError.
        try:
            gf.assert_admission_open(
                tmp_path / "companion", record["terminal_id"], record["generation"]
            )
        except gf.FencedError as exc:
            raise bridge.BridgeError(f"w13-fenced-before-provider-io: {exc}") from exc
        provider_byte_calls.append(command)
        receipt_calls.append(command)
        raise AssertionError("the parked generation reached the provider-effect branch")

    monkeypatch.setattr(bridge, "request_bridge", request_bridge_after_park)

    import asyncio

    refused = asyncio.run(v2.admit_reserved(record["reservation_id"], admit))

    assert len(durable_claims) == 1
    assert durable_claims[0]["status"] == "io-attempted"
    assert refused["admission"]["status"] == "refused"
    assert refused["admission"]["refusal_reason"] == "generation_fenced"
    assert v2._is_retryable_refusal(refused["admission"], admit.delivery_id) is False
    assert refused["state"] == "admitting"
    assert provider_byte_calls == []
    assert receipt_calls == []
    durable = v2.get(record["reservation_id"])
    assert durable["admission"]["status"] == "refused"
    assert durable["admission"]["refusal_reason"] == "generation_fenced"


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


def test_project_persists_to_bridge_handoff(isolated_memory_db, worktree, tmp_path, monkeypatch):
    # ADM-1 durable regression: the immutable project identity survives the
    # reserve wire model, persists on the reservation, reaches the bridge
    # request verbatim (never silently substituted), and the v2 launch
    # persists only to the isolated v2 vintage surface.
    import asyncio

    from cli_agent_orchestrator.services import managed_provider_bridge as bridge
    from cli_agent_orchestrator.services import terminal_service

    request = _reserve_request(worktree, tmp_path, project="actual-project-which-must-bind")
    record, _ = v2.reserve(request)
    assert record["request"]["project"] == "actual-project-which-must-bind"
    captured: dict = {}
    monkeypatch.setattr(
        bridge, "write_request", lambda _rid, req: captured.update(req), raising=False
    )
    # Keep the test hermetic: the profile digest otherwise reads agent
    # profiles from the developer's real HOME.
    monkeypatch.setattr(bridge, "profile_digest", lambda _profile: "a" * 64, raising=False)
    create_calls: dict = {}

    async def fake_create(**kwargs):
        create_calls.update(kwargs)
        return object()

    monkeypatch.setattr(terminal_service, "create_terminal", fake_create)
    monkeypatch.setattr(
        bridge,
        "request_bridge",
        lambda _rid, _cmd, timeout=120.0: {"state": "ready", "readiness": {"ok": True}},
        raising=False,
    )
    asyncio.run(v2.launch_reserved(record["reservation_id"]))
    assert captured["project"] == "actual-project-which-must-bind"
    assert create_calls["protocol_vintage"] == "v2"


def test_admission_replay_binds_full_identity(isolated_memory_db, worktree, tmp_path, monkeypatch):
    # ADM-2 durable regression: the same delivery id carrying a changed
    # message, sender, context, or binding is a DIFFERENT immutable
    # identity and is refused — only the byte-identical replay dedupes.
    request = _reserve_request(worktree, tmp_path)
    record, _ = v2.reserve(request)
    v2.claim_launch(record["reservation_id"])
    _ready_bridge_state(record, monkeypatch)
    bound = v2.bind_native(record["reservation_id"], _bind_request(record))
    digest = v2.native_binding_digest(bound)
    admit = _admit_request(bound, digest)
    _, should_send = v2.claim_admission(record["reservation_id"], admit)
    assert should_send
    replayed, send_again = v2.claim_admission(record["reservation_id"], admit)
    assert not send_again
    changed_message = "a different task under the same delivery id"
    changed_context = admit.context.model_dump(mode="json")
    changed_context["project"] = "someone-elses-project"
    for changes in (
        {
            "message": changed_message,
            "message_sha256": hashlib.sha256(changed_message.encode()).hexdigest(),
        },
        {"sender_id": "0badf00d"},
        {"context": changed_context},
        {"native_binding_digest": "0" * 64},
    ):
        with pytest.raises(ManagedLaunchConflict):
            v2.claim_admission(
                record["reservation_id"],
                _admit_request(bound, digest, delivery_id=admit.delivery_id, **changes),
            )


def test_bind_crash_reconciles_to_bound(isolated_memory_db, worktree, tmp_path, monkeypatch):
    # ADM-3 durable regression: a crash after the immutable binding
    # publication but before the SQL commit no longer strands the row
    # `launching` — the journaled bind intent lets the retry adopt the
    # already-published record (byte-identical) and converge to `bound`.
    request = _reserve_request(worktree, tmp_path)
    record, _ = v2.reserve(request)
    v2.claim_launch(record["reservation_id"])
    _ready_bridge_state(record, monkeypatch)
    bind_request = _bind_request(record)
    real_write = v2.write_binding_record

    def write_then_crash(*args, **kwargs):
        path = real_write(*args, **kwargs)
        raise RuntimeError("simulated process death after binding publication")

    monkeypatch.setattr(v2, "write_binding_record", write_then_crash)
    with pytest.raises(v2.ManagedLaunchUnavailable):
        v2.bind_native(record["reservation_id"], bind_request)
    stranded = v2.get(record["reservation_id"])
    assert stranded["state"] == "launching"
    assert stranded["bind_intent"] is not None  # the recoverable boundary held
    monkeypatch.setattr(v2, "write_binding_record", real_write)
    converged = v2.bind_native(record["reservation_id"], bind_request)
    assert converged["state"] == "bound"
    assert converged["binding"]["attempt_id"] == bind_request.attempt_id
    # Byte-identical idempotence: a third bind returns the same binding.
    again = v2.bind_native(record["reservation_id"], bind_request)
    assert again["binding"] == converged["binding"]


def test_bind_reconcile_refuses_mismatched_publication(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    # The other side of ADM-3: an existing binding record that does NOT
    # match the journaled intent is a conflict, never a silent overwrite
    # of the immutable publication.
    import json as _json

    from cli_agent_orchestrator.services.destructive_endpoint import binding_record_path

    request = _reserve_request(worktree, tmp_path)
    record, _ = v2.reserve(request)
    v2.claim_launch(record["reservation_id"])
    _ready_bridge_state(record, monkeypatch)
    bind_request = _bind_request(record)
    real_write = v2.write_binding_record

    def write_then_crash(*args, **kwargs):
        path = real_write(*args, **kwargs)
        raise RuntimeError("simulated process death after binding publication")

    monkeypatch.setattr(v2, "write_binding_record", write_then_crash)
    with pytest.raises(v2.ManagedLaunchUnavailable):
        v2.bind_native(record["reservation_id"], bind_request)
    monkeypatch.setattr(v2, "write_binding_record", real_write)
    path = binding_record_path(v2.COMPANION_DIR, record["terminal_id"], record["generation"])
    corrupted = _json.loads(path.read_bytes())
    corrupted["native_session_id"] = "forged-session"
    path.write_text(_json.dumps(corrupted, sort_keys=True) + "\n")
    with pytest.raises(ManagedLaunchConflict, match="does not match the journaled"):
        v2.bind_native(record["reservation_id"], bind_request)
    assert v2.get(record["reservation_id"])["state"] == "launching"
