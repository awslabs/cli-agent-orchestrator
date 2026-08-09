"""M3-A / cond-0377: stable-agent binding at the canonical launch seams.

The v2 managed worker path binds the roster in the same transaction as
``bound``, gates real task admission on the durable roster state, and
records the admitted state at admission completion.  The unmanaged
supervisor path binds at terminal creation with a truthful
``identity_missing`` lineage and repairs it when the provider's identity
is observed.

No provider, tmux, or network I/O: the v2 seam runs with a faked durable
bridge readiness state, and the supervisor seam is exercised against the
roster contract directly.
"""

from __future__ import annotations

import hashlib
import subprocess
import uuid

import pytest

from cli_agent_orchestrator.models.managed_launch_v2 import (
    PROTOCOL_VERSION_V2,
    ManagedLaunchV2AdmitRequest,
    ManagedLaunchV2BindRequest,
    ManagedLaunchV2ReserveRequest,
)
from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services import stable_agent_roster as roster
from cli_agent_orchestrator.services.managed_launch import ManagedLaunchConflict
from cli_agent_orchestrator.services.managed_provider_bridge import BRIDGE_VERSION

DELIVERY_ID = "33333333-3333-4333-8333-333333333333"


@pytest.fixture(autouse=True)
def _companion(tmp_path, monkeypatch):
    monkeypatch.setattr(v2, "COMPANION_DIR", tmp_path / "companion")


@pytest.fixture
def persisted_terminal(monkeypatch):
    """The unmanaged bind seam only binds terminals whose row durably
    persisted; tests that call the seam directly stand in for the row."""
    from cli_agent_orchestrator.services import terminal_service as ts

    monkeypatch.setattr(
        ts, "get_terminal_metadata", lambda terminal_id: {"provider": "claude_code"}
    )


@pytest.fixture
def strict_close_db(isolated_memory_db, monkeypatch):
    """SessionLocal whose close() strictly rolls back any pending outer
    transaction — the coordinator's stated replay premise.  The replay-side
    roster repair must be committed EXPLICITLY, never left to close()."""
    from sqlalchemy import create_engine as _ce
    from sqlalchemy.orm import Session as _SA_Session
    from sqlalchemy.orm import sessionmaker as _sm

    class _StrictCloseSession(_SA_Session):
        def close(self):
            if self.in_transaction():
                self.rollback()
            super().close()

    sessionmaker = _sm(
        autocommit=False, autoflush=False, bind=isolated_memory_db, class_=_StrictCloseSession
    )
    monkeypatch.setattr(roster.database, "SessionLocal", sessionmaker)
    monkeypatch.setattr(v2.database, "SessionLocal", sessionmaker)
    return sessionmaker


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
        "agent_profile": record["agent_profile"],
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


def _admit_request(bound, digest, **changes):
    payload = {
        "protocol_version": PROTOCOL_VERSION_V2,
        "delivery_id": DELIVERY_ID,
        "message": "implement the thing",
        "message_sha256": hashlib.sha256(b"implement the thing").hexdigest(),
        "sender_id": "deadbeef",
        "orchestration_type": "assign",
        "context": {
            "boot_id": str(uuid.uuid4()),
            "project": "cao-test",
            "task_id": "self-heal-demo-task",
            "run_id": "run-0001",
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


def _reserve_and_bind(worktree, tmp_path, monkeypatch, **reserve_changes):
    request = _reserve_request(worktree, tmp_path, **reserve_changes)
    record, _ = v2.reserve(request)
    v2.claim_launch(record["reservation_id"])
    receipt = _ready_bridge_state(record, monkeypatch)
    bound = v2.bind_native(record["reservation_id"], _bind_request(record))
    return record, bound, receipt


# ---------------------------------------------------------------------------
# the v2 worker seam: bind -> gate -> admitted
# ---------------------------------------------------------------------------


def test_v2_bind_creates_durable_roster_binding(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    record, bound, receipt = _reserve_and_bind(worktree, tmp_path, monkeypatch)

    agents = roster.list_agents(session_name="cao-test")
    assert len(agents) == 1
    agent = agents[0]
    # The agent id is the one minted at reserve and persisted before any
    # provider effect: response loss returns the same id.
    assert agent["agent_id"] == record["stable_agent_id"]
    assert agent["role"] == roster.ROLE_WORKER
    assert agent["profile_family"] == "reviewer-sol-max"
    assert agent["disposition"] == roster.DISPOSITION_LIVE

    lineages = roster.list_lineages(agent_id=agent["agent_id"])
    assert len(lineages) == 1
    lineage = lineages[0]
    assert lineage["harness"] == "codex"
    assert lineage["native_session_id"] == receipt["provider_session_id"]
    # Route provenance is preserved from the machine contract.
    assert lineage["route_provenance"]["provider_route"] == "anthropic"
    assert (
        lineage["route_provenance"]["route_payload_sha256"]
        == bound["bind_intent"]["binding_record"]["route_payload_sha256"]
    )
    # Codex app-server mints the id before a turn: recorded honestly.
    assert lineage["acquisition_method"] == roster.ACQUISITION_ZERO_TURN_BOOTSTRAP

    incarnations = roster.list_incarnations(agent_id=agent["agent_id"])
    assert len(incarnations) == 1
    incarnation = incarnations[0]
    assert incarnation["terminal_id"] == record["terminal_id"]
    assert incarnation["generation"] == record["generation"]
    assert incarnation["disposition"] == roster.INCARNATION_BOUND


def test_v2_admission_gate_and_admitted_state(isolated_memory_db, worktree, tmp_path, monkeypatch):
    record, bound, _receipt = _reserve_and_bind(worktree, tmp_path, monkeypatch)
    digest = v2.native_binding_digest(bound)
    admit = _admit_request(bound, digest)

    claimed, should_send = v2.claim_admission(record["reservation_id"], admit)
    assert should_send
    # The gate passes only because the roster binding is durable.
    roster.assert_admission_ready(
        terminal_id=record["terminal_id"], generation=record["generation"]
    )

    receipt = {
        "receipt_id": "turn-1",
        "provider_session_id": bound["binding"]["native_session_id"],
        "provider_turn_id": "turn-1",
        "provider_receipt_kind": "codex-turn-start",
    }
    completed = v2.complete_admission(record["reservation_id"], admit.delivery_id, receipt)
    assert completed["state"] == "admitted"

    incarnation = roster.get_incarnation_by_terminal(record["terminal_id"])
    assert incarnation["disposition"] == roster.INCARNATION_ADMITTED


def test_completion_after_teardown_race_preserves_admitted(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    """i-0023: after the task bytes are posted, a teardown that retires the
    roster incarnation must not roll the reservation back to ``admitting``:
    the delivery truth (``admitted``) is preserved and the bytes are never
    resent.  The roster's lifecycle (retired) and the reservation's
    delivery truth (admitted) are different durable facts."""
    record, bound, _receipt = _reserve_and_bind(worktree, tmp_path, monkeypatch)
    digest = v2.native_binding_digest(bound)
    admit = _admit_request(bound, digest)
    claimed, should_send = v2.claim_admission(record["reservation_id"], admit)
    assert should_send
    assert claimed["state"] == "admitting"

    # Teardown wins the race after the provider posted the bytes.
    roster.retire_incarnation(
        terminal_id=record["terminal_id"],
        generation=record["generation"],
        reason="teardown",
    )

    receipt = {
        "receipt_id": "turn-1",
        "provider_session_id": bound["binding"]["native_session_id"],
        "provider_turn_id": "turn-1",
        "provider_receipt_kind": "codex-turn-start",
    }
    completed = v2.complete_admission(record["reservation_id"], admit.delivery_id, receipt)
    assert completed["state"] == "admitted"

    # Never resent: the reservation stays durably admitted on replay.
    replayed = v2.get(record["reservation_id"])
    assert replayed["state"] == "admitted"
    assert replayed["admission"]["status"] == "admitted"


def _commit_spy(monkeypatch):
    """Wrap SessionLocal so the tests can assert an EXPLICIT commit of the
    replay-side roster repair — the requirement the coordinator stated —
    independent of any toolchain's close()/rollback semantics."""
    real_session = roster.database.SessionLocal
    commits: list[int] = []

    def _factory():
        session = real_session()
        original = session.commit

        def _commit(*args, **kwargs):
            commits.append(1)
            return original(*args, **kwargs)

        session.commit = _commit
        return session

    monkeypatch.setattr(roster.database, "SessionLocal", _factory)
    monkeypatch.setattr(v2.database, "SessionLocal", _factory)
    return commits


def test_replay_converges_roster_mark_admitted_after_transient_failure(
    isolated_memory_db, strict_close_db, worktree, tmp_path, monkeypatch
):
    """P1-2 (ACP): the first completion persists reservation ``admitted``
    even when the roster mark is transiently unavailable; an idempotent
    replay re-marks and the roster durably reads ``admitted`` from a
    fresh session — the replay-side repair must be committed, never
    rolled back with the outer session close."""
    record, bound, _receipt = _reserve_and_bind(worktree, tmp_path, monkeypatch)
    digest = v2.native_binding_digest(bound)
    admit = _admit_request(bound, digest)
    claimed, should_send = v2.claim_admission(record["reservation_id"], admit)
    assert should_send

    receipt = {
        "receipt_id": "turn-1",
        "provider_session_id": bound["binding"]["native_session_id"],
        "provider_turn_id": "turn-1",
        "provider_receipt_kind": "codex-turn-start",
    }
    real_mark = roster.mark_admitted
    calls = {"n": 0}

    def _failing_first(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise roster.StableAgentUnavailable("transient roster store failure")
        return real_mark(*args, **kwargs)

    monkeypatch.setattr(roster, "mark_admitted", _failing_first)

    completed = v2.complete_admission(record["reservation_id"], admit.delivery_id, receipt)
    assert completed["state"] == "admitted"
    incarnation = roster.get_incarnation_by_terminal(
        record["terminal_id"], generation=record["generation"]
    )
    assert incarnation["disposition"] == roster.INCARNATION_BOUND

    # Restore successful roster marking and replay the exact completion;
    # the replay must EXPLICITLY commit the roster repair (never leave it
    # to session-close semantics).
    monkeypatch.setattr(roster, "mark_admitted", real_mark)
    commits = _commit_spy(monkeypatch)
    again = v2.complete_admission(record["reservation_id"], admit.delivery_id, receipt)
    assert again["state"] == "admitted"
    assert commits, "the replay-side roster repair must be explicitly committed"

    # The roster must durably read admitted from a fresh session.
    incarnation = roster.get_incarnation_by_terminal(
        record["terminal_id"], generation=record["generation"]
    )
    assert incarnation["disposition"] == roster.INCARNATION_ADMITTED

    # Delivery identity/bytes unchanged: exact-replay conflict checks hold.
    replay = v2.get(record["reservation_id"])
    assert replay["state"] == "admitted"
    assert replay["admission"]["delivery_id"] == admit.delivery_id
    assert replay["admission"]["provider_submission_receipt"] == receipt


def test_native_replay_converges_roster_mark_admitted_after_transient_failure(
    isolated_memory_db, strict_close_db, worktree, tmp_path, monkeypatch
):
    """P1-2 (native): the same replay-side convergence on the native
    completion path."""
    from cli_agent_orchestrator.services.managed_provider_bridge import BRIDGE_VERSION as _BV

    executable = tmp_path / "fake-kimi"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    payload = {
        "protocol_version": PROTOCOL_VERSION_V2,
        "reservation_id": str(uuid.uuid4()),
        "session_name": "cao-test-native",
        "provider": "kimi_cli",
        "agent_profile": "reviewer",
        "caller_id": "deadbeef",
        "working_directory": str(worktree),
        "expected_model": "gpt-5.6-sol",
        "expected_effort": "xhigh",
        "provider_executable": str(executable),
        "provider_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "obligation_generation": "obgen-7c2e4a1b",
        "task_id": "self-heal-demo-task",
        "run_id": "run-0001",
        "delivery_id": DELIVERY_ID,
        "launch_nonce": "n" * 40,
        "execution_mode": "native_tui",
    }
    record, _ = v2.reserve(ManagedLaunchV2ReserveRequest(**payload))
    reservation_id = record["reservation_id"]
    v2.claim_launch(reservation_id)
    session_id = f"kimi-{uuid.uuid4().hex[:12]}"
    receipt = {
        "bridge_version": _BV,
        "receipt_id": session_id,
        "provider_session_id": session_id,
        "provider_receipt_kind": "kimi-native-tui-attached",
        "provider_version": "kimi 0.29.0",
        "model_input_ready": True,
        "reservation_id": reservation_id,
        "terminal_id": record["terminal_id"],
        "generation": record["generation"],
        "provider": "kimi_cli",
        "agent_profile": "reviewer",
        "model": "gpt-5.6-sol",
        "effort": "xhigh",
        "working_directory": str(worktree),
    }
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.read_state",
        lambda rid: {"state": "ready", "readiness": receipt},
        raising=False,
    )
    bound = v2.bind_native(reservation_id, _bind_request(record))
    assert bound["state"] == "bound"

    digest = v2.native_binding_digest(bound)
    admit = _admit_request(bound, digest)
    claimed, should_send = v2.claim_admission(reservation_id, admit)
    assert should_send

    operation = {
        "schema": "cao-kimi-native-control-v1",
        "operation_id": admit.delivery_id,
        "kind": "queue",
        "state": "accepted",
        "provider": "kimi_cli",
        "native_session_id": session_id,
        "terminal_id": record["terminal_id"],
        "generation": record["generation"],
        "execution_mode": "native_tui",
        "payload_sha256": hashlib.sha256(b"x").hexdigest(),
        "posted": True,
        "provider_accepted": True,
    }
    real_mark = roster.mark_admitted
    calls = {"n": 0}

    def _failing_first(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise roster.StableAgentUnavailable("transient roster store failure")
        return real_mark(*args, **kwargs)

    monkeypatch.setattr(roster, "mark_admitted", _failing_first)
    expected_digest = hashlib.sha256(b"x").hexdigest()
    completed = v2.complete_native_admission(
        reservation_id, admit.delivery_id, operation, expected_digest
    )
    assert completed["state"] == "admitted"
    incarnation = roster.get_incarnation_by_terminal(
        record["terminal_id"], generation=record["generation"]
    )
    assert incarnation["disposition"] == roster.INCARNATION_BOUND

    monkeypatch.setattr(roster, "mark_admitted", real_mark)
    commits = _commit_spy(monkeypatch)
    again = v2.complete_native_admission(
        reservation_id, admit.delivery_id, operation, expected_digest
    )
    assert again["state"] == "admitted"
    assert commits, "the replay-side roster repair must be explicitly committed"
    incarnation = roster.get_incarnation_by_terminal(
        record["terminal_id"], generation=record["generation"]
    )
    assert incarnation["disposition"] == roster.INCARNATION_ADMITTED

    # A later teardown wins physical lifecycle ownership. Replaying the
    # exact already-admitted native completion must preserve delivery truth,
    # must not resend bytes, and must not revive the retired incarnation.
    roster.retire_incarnation(
        terminal_id=record["terminal_id"],
        generation=record["generation"],
        reason="teardown after delivery",
    )
    retired_replay = v2.complete_native_admission(
        reservation_id, admit.delivery_id, operation, expected_digest
    )
    assert retired_replay["state"] == "admitted"
    incarnation = roster.get_incarnation_by_terminal(
        record["terminal_id"], generation=record["generation"]
    )
    assert incarnation["disposition"] == roster.INCARNATION_RETIRED


def test_bind_unavailable_stays_retryable_not_conflict(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    """A transient roster unavailability must map to a retryable managed
    unavailable result, never a permanent conflict; immutable conflicts
    stay conflicts."""
    from cli_agent_orchestrator.services import stable_agent_roster as _roster
    from cli_agent_orchestrator.services.managed_launch import (
        ManagedLaunchUnavailable,
    )

    def _fresh_record():
        request = _reserve_request(worktree, tmp_path)
        record, _ = v2.reserve(request)
        v2.claim_launch(record["reservation_id"])
        _ready_bridge_state(record, monkeypatch)
        return record

    record = _fresh_record()

    def _unavailable(*args, **kwargs):
        raise _roster.StableAgentUnavailable("store contention; retry the bind")

    monkeypatch.setattr(_roster, "bind_generation", _unavailable)
    with pytest.raises(ManagedLaunchUnavailable, match="retry the bind"):
        v2.bind_native(record["reservation_id"], _bind_request(record))

    record = _fresh_record()

    def _conflict(*args, **kwargs):
        raise _roster.StableAgentConflict("immutable identity mismatch")

    monkeypatch.setattr(_roster, "bind_generation", _conflict)
    with pytest.raises(ManagedLaunchConflict, match="immutable identity mismatch"):
        v2.bind_native(record["reservation_id"], _bind_request(record))


def test_v2_admission_refused_without_durable_roster_binding(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    """Structural proof: a bound reservation whose roster binding is absent
    cannot admit a single task byte (the gate refuses before any I/O)."""
    record, _bound, _receipt = _reserve_and_bind(worktree, tmp_path, monkeypatch)
    # Simulate a store where the roster bind never ran (crash window, old
    # writer): drop the roster rows for this terminal.
    incarnation = roster.get_incarnation_by_terminal(record["terminal_id"])
    from cli_agent_orchestrator.clients import database

    with database.SessionLocal() as db:
        db.query(database.StableAgentIncarnationModel).filter(
            database.StableAgentIncarnationModel.incarnation_id == incarnation["incarnation_id"]
        ).delete()
        db.commit()

    bound = v2.get(record["reservation_id"])
    digest = v2.native_binding_digest(bound)
    admit = _admit_request(bound, digest)
    with pytest.raises(ManagedLaunchConflict, match="stable-agent binding is durable"):
        v2.claim_admission(record["reservation_id"], admit)


def test_v2_bind_replay_adopts_roster_rows(isolated_memory_db, worktree, tmp_path, monkeypatch):
    request = _reserve_request(worktree, tmp_path)
    record, _ = v2.reserve(request)
    v2.claim_launch(record["reservation_id"])
    _ready_bridge_state(record, monkeypatch)
    bind_request = _bind_request(record)
    v2.bind_native(record["reservation_id"], bind_request)
    agents_before = roster.list_agents()
    lineages_before = roster.list_lineages()
    incarnations_before = roster.list_incarnations()

    # The caller lost the response and re-issues the exact bind (same
    # attempt id); the reservation converges to bound and the roster
    # rows are adopted, never duplicated.
    again = v2.bind_native(record["reservation_id"], bind_request)
    assert again["state"] == "bound"
    assert len(roster.list_agents()) == len(agents_before)
    assert len(roster.list_lineages()) == len(lineages_before)
    assert len(roster.list_incarnations()) == len(incarnations_before)


# ---------------------------------------------------------------------------
# the unmanaged supervisor seam: identity_missing then repair
# ---------------------------------------------------------------------------


def test_unmanaged_supervisor_binds_identity_missing_then_repairs(
    isolated_memory_db,
):
    contract = roster.BindingContract(
        agent_id=roster.derive_initial_agent_id("b2c3d4e5"),
        session_name="cao-campaign",
        role=roster.ROLE_SUPERVISOR,
        profile_family="code_supervisor",
        harness="claude_code",
        native_session_id=None,
        terminal_id="b2c3d4e5",
        generation="00000000-0000-4000-8000-0000000000aa",
        pane_id="%201",
        pane_pid=5555,
        process_identity={"pid": 5555, "start_marker": "2026-08-09T00:00:00Z"},
        execution_mode="native_tui",
    )
    bound = roster.bind_generation(contract)
    assert bound["agent"]["role"] == roster.ROLE_SUPERVISOR
    assert bound["lineage"]["native_session_id"] is None
    assert bound["agent"]["disposition"] == roster.DISPOSITION_IDENTITY_MISSING

    # The provider answers (SessionStart hook): the repair binds the id
    # onto the missing lineage exactly once.
    repaired = roster.record_native_identity(
        terminal_id="b2c3d4e5",
        native_session_id="11111111-2222-4333-8444-5555555555cc",
        harness="claude_code",
    )
    assert repaired["lineage"]["native_session_id"] == "11111111-2222-4333-8444-5555555555cc"
    assert repaired["agent"]["disposition"] == roster.DISPOSITION_LIVE
    assert len(roster.list_lineages(agent_id=bound["agent"]["agent_id"])) == 1

    # The supervisor survives the disposable incarnation's retirement.
    roster.retire_incarnation(
        terminal_id="b2c3d4e5",
        generation="00000000-0000-4000-8000-0000000000aa",
        reason="pane cleaned up at a safe boundary",
    )
    agent = roster.get_agent(bound["agent"]["agent_id"])
    assert agent["disposition"] == roster.DISPOSITION_DORMANT
    assert agent["current_lineage"]["native_session_id"] == "11111111-2222-4333-8444-5555555555cc"


def test_supervisor_and_worker_share_one_contract_via_launch_seams(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    """A v2 worker and an unmanaged supervisor both land in the same
    session's roster through the same identity contract."""
    _reserve_and_bind(worktree, tmp_path, monkeypatch, session_name="cao-campaign")
    roster.bind_generation(
        roster.BindingContract(
            agent_id=roster.derive_initial_agent_id("b2c3d4e5"),
            session_name="cao-campaign",
            role=roster.ROLE_SUPERVISOR,
            profile_family="code_supervisor",
            harness="claude_code",
            native_session_id=None,
            terminal_id="b2c3d4e5",
            generation="00000000-0000-4000-8000-0000000000bb",
            execution_mode="native_tui",
        )
    )
    agents = roster.list_agents(session_name="cao-campaign")
    roles = {agent["role"] for agent in agents}
    assert roles == {roster.ROLE_SUPERVISOR, roster.ROLE_WORKER}


# ---------------------------------------------------------------------------
# P1-3: role is launch truth, never a profile-name heuristic
# ---------------------------------------------------------------------------


def test_v2_worker_role_is_explicit_not_profile_derived(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    """A worker profile NAMED like a supervisor stays a worker: managed-v2
    reservations in this slice are worker launches, bound explicitly."""
    record, _bound, _receipt = _reserve_and_bind(
        worktree, tmp_path, monkeypatch, agent_profile="supervisor"
    )
    agent = roster.get_agent(record["stable_agent_id"])
    assert agent["role"] == roster.ROLE_WORKER
    assert agent["profile_family"] == "supervisor"


def test_session_creation_passes_supervisor_role_explicitly(isolated_memory_db, persisted_terminal):
    """Session creation owns the initial supervisor role: the unmanaged
    bind seam derives the agent id from the terminal identity and binds
    the role the owning operation passed."""
    from cli_agent_orchestrator.services.terminal_service import _roster_bind_unmanaged

    _roster_bind_unmanaged(
        terminal_id="b2c3d4e5",
        session_name="cao-campaign",
        stable_agent_role=roster.ROLE_SUPERVISOR,
        agent_profile="my-custom-supervisor",
        provider="claude_code",
        terminal_generation="00000000-0000-4000-8000-0000000000cc",
        pane_id="%301",
        pane_pid=7777,
        native_status_source=True,
    )
    agent = roster.get_agent(roster.derive_initial_agent_id("b2c3d4e5"))
    assert agent["role"] == roster.ROLE_SUPERVISOR
    assert agent["profile_family"] == "my-custom-supervisor"
    assert agent["disposition"] == roster.DISPOSITION_IDENTITY_MISSING


def test_additional_terminals_default_to_worker(isolated_memory_db, persisted_terminal):
    """Sidecars without an explicit role are workers, whatever their
    profile is named."""
    from cli_agent_orchestrator.services.terminal_service import _roster_bind_unmanaged

    _roster_bind_unmanaged(
        terminal_id="c3d4e5f6",
        session_name="cao-campaign",
        stable_agent_role=None,
        agent_profile="supervisor",
        provider="claude_code",
        terminal_generation="00000000-0000-4000-8000-0000000000dd",
        pane_id="%302",
        pane_pid=7778,
        native_status_source=False,
    )
    agent = roster.get_agent(roster.derive_initial_agent_id("c3d4e5f6"))
    assert agent["role"] == roster.ROLE_WORKER
    assert agent["profile_family"] == "supervisor"


# ---------------------------------------------------------------------------
# P1-4: a failed new-launch roster bind is a failed launch
# ---------------------------------------------------------------------------


def test_unmanaged_bind_failure_is_typed_and_leaves_no_roster_record(
    isolated_memory_db, persisted_terminal, monkeypatch
):
    """A newly created terminal whose stable-agent row cannot be durably
    bound must not be reported as a successful rostered launch: the
    unmanaged bind seam raises a typed failure and nothing is recorded."""
    from cli_agent_orchestrator.services import stable_agent_roster as _roster
    from cli_agent_orchestrator.services.terminal_service import _roster_bind_unmanaged

    def _refuse(*args, **kwargs):
        raise _roster.StableAgentUnavailable("roster store is down")

    monkeypatch.setattr(_roster, "bind_generation", _refuse)
    with pytest.raises(_roster.StableAgentUnavailable, match="roster store is down"):
        _roster_bind_unmanaged(
            terminal_id="b2c3d4e5",
            session_name="cao-campaign",
            stable_agent_role=roster.ROLE_SUPERVISOR,
            agent_profile="code_supervisor",
            provider="claude_code",
            terminal_generation="00000000-0000-4000-8000-0000000000ee",
            pane_id="%303",
            pane_pid=7779,
            native_status_source=True,
        )
    assert roster.list_agents() == []
    assert roster.list_incarnations() == []


def test_retirement_failure_never_blocks_teardown(
    isolated_memory_db, persisted_terminal, monkeypatch
):
    """Teardown retirement stays best-effort: a roster failure during
    retirement must never propagate into the teardown path."""
    from cli_agent_orchestrator.services import stable_agent_roster as _roster
    from cli_agent_orchestrator.services.terminal_service import (
        _roster_bind_unmanaged,
        _roster_retire_incarnation_best_effort,
    )

    _roster_bind_unmanaged(
        terminal_id="b2c3d4e5",
        session_name="cao-campaign",
        stable_agent_role=roster.ROLE_SUPERVISOR,
        agent_profile="code_supervisor",
        provider="claude_code",
        terminal_generation="00000000-0000-4000-8000-0000000000ff",
        pane_id="%304",
        pane_pid=7780,
        native_status_source=True,
    )

    def _boom(*args, **kwargs):
        raise _roster.StableAgentUnavailable("store down")

    monkeypatch.setattr(_roster, "retire_incarnation", _boom)
    # The best-effort wrapper swallows the failure (logged, never raised).
    _roster_retire_incarnation_best_effort("b2c3d4e5", "00000000-0000-4000-8000-0000000000ff")
    # The roster record survives the failed retirement attempt.
    agent = roster.get_agent(roster.derive_initial_agent_id("b2c3d4e5"))
    assert agent["agent_id"]


# ---------------------------------------------------------------------------
# legacy / corrupt rows never block launches
# ---------------------------------------------------------------------------


def test_corrupt_roster_rows_do_not_block_a_v2_launch(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    from datetime import datetime, timezone

    from cli_agent_orchestrator.clients import database

    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with database.SessionLocal() as db:
        db.add(
            database.StableAgentModel(
                agent_id="00000000-0000-4000-8000-0000000000dd",
                session_name="cao-corrupt",
                role=roster.ROLE_WORKER,
                profile_family="developer",
                disposition="sentient",
                resume_contract_version="cao-m3-roster-v0-unknown",
                revision=1,
                created_at=stamp,
                updated_at=stamp,
            )
        )
        db.add(
            database.StableAgentLineageModel(
                lineage_id="00000000-0000-4000-8000-0000000000ee",
                agent_id="00000000-0000-4000-8000-0000000000dd",
                harness="claude_code",
                native_session_id=None,
                route_provenance_json="{not json",
                lineage_origin=roster.LINEAGE_ORIGIN_INITIAL,
                created_at=stamp,
                updated_at=stamp,
            )
        )
        db.commit()

    audit = roster.audit_dry_run()
    assert audit["problems_count"] >= 1
    assert any(p["kind"] == "corrupt-route-provenance" for p in audit["problems"])
    assert any(p["kind"] == "unknown-disposition" for p in audit["problems"])

    # An unrelated launch still binds cleanly.
    _reserve_and_bind(worktree, tmp_path, monkeypatch, session_name="cao-clean")
    agents = roster.list_agents(session_name="cao-clean")
    assert len(agents) == 1
    assert agents[0]["disposition"] == roster.DISPOSITION_LIVE


# ---------------------------------------------------------------------------
# Muse enrollment honesty at the capability surface
# ---------------------------------------------------------------------------


def test_muse_v2_enrollment_truthful_not_faked(isolated_memory_db):
    """The roster identity contract supports the Muse harness domain, and
    the managed-v2 capability surface truthfully reports that Muse is not
    v2-launchable — no readiness is fabricated for a path that does not
    exist yet.  Managed-v2 Muse ENROLLMENT is a required cond-0377
    follow-up sub-slice; only the installed activation matrix for the
    enrolled cells is M3-F."""
    from cli_agent_orchestrator.services.managed_launch_v2 import NATIVE_TUI_PROVIDERS

    assert "muse_cli" not in NATIVE_TUI_PROVIDERS
    capabilities = v2.native_tui_capabilities()
    assert "muse_cli" not in capabilities["providers"]

    # The identity contract for the Muse harness is real: caller-chosen id.
    bound = roster.bind_generation(
        roster.BindingContract(
            agent_id=roster.derive_initial_agent_id("e5f60718"),
            session_name="cao-muse",
            role=roster.ROLE_WORKER,
            profile_family="developer",
            harness="muse_cli",
            native_session_id="11111111-2222-4333-8444-5555555555dd",
            acquisition_method=roster.ACQUISITION_CHOSEN_SESSION_ID,
            terminal_id="e5f60718",
            generation="00000000-0000-4000-8000-0000000000cc",
        )
    )
    assert bound["lineage"]["harness"] == "muse_cli"
    assert bound["lineage"]["acquisition_method"] == roster.ACQUISITION_CHOSEN_SESSION_ID
