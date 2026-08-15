"""Atomic M3-C C2 Stop-barrier and terminal lifecycle tests (cond-0379)."""

from __future__ import annotations

import threading
import uuid

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import cohort_journal as cohort
from cli_agent_orchestrator.services import operation_journal as oj
from cli_agent_orchestrator.services import session_lifecycle as sl
from cli_agent_orchestrator.services import stable_agent_roster as roster

SESSION = "cao-cohort-terminal"
_DIGEST = "b" * 64


@pytest.fixture(autouse=True)
def _db(isolated_memory_db):
    return isolated_memory_db


def _bind_agent(*, suffix: str, role: str = roster.ROLE_WORKER):
    return roster.bind_generation(
        roster.BindingContract(
            agent_id=str(uuid.uuid4()),
            session_name=SESSION,
            role=role,
            profile_family="supervisor" if role == roster.ROLE_SUPERVISOR else "developer",
            harness="claude_code",
            native_session_id=f"native-{suffix}",
            acquisition_method="chosen_session_id",
            terminal_id=f"terminal-{suffix}",
            generation=str(uuid.uuid4()),
            pane_id=f"%{suffix}",
            pane_pid=7000 + int(suffix),
            process_identity={"pid": 7000 + int(suffix), "start_marker": f"m-{suffix}"},
            execution_mode="native_tui",
            admitted=True,
        )
    )


def _claim(*, kind: str, mode: str):
    boundary = cohort.observe_boundary(SESSION)
    request = cohort.OperationRequest(
        operation_id=str(uuid.uuid4()),
        session_name=SESSION,
        operation_kind=kind,
        requested_mode=mode,
        initiator_kind=cohort.INITIATOR_OPERATOR,
        initiated_by="colin",
        lifecycle_epoch=boundary["lifecycle_epoch"],
        lifecycle_observation=boundary["lifecycle_observation"],
        roster_revision=boundary["roster_revision"],
        member_snapshot_digest=boundary["member_snapshot_digest"],
    )
    return cohort.claim_operation(request)


def _transition(operation, to_state: str, **changes):
    payload = {
        "transition_id": str(uuid.uuid4()),
        "operation_id": operation["operation_id"],
        "expected_state_epoch": operation["state_epoch"],
        "to_state": to_state,
        "actor": "colin",
    }
    payload.update(changes)
    return cohort.transition_operation(cohort.TransitionRequest(**payload))["operation"]


def _begin_teardown(operation, **changes):
    payload = {
        "transition_id": str(uuid.uuid4()),
        "operation_id": operation["operation_id"],
        "expected_state_epoch": operation["state_epoch"],
        "actor": "colin",
    }
    payload.update(changes)
    return cohort.StopTeardownRequest(**payload)


def _record_all(operation, *, final_state: str, risk: str, boundary: bool = False):
    members = cohort.get_operation(operation["operation_id"])["members"]
    for member in members:
        if not member["included"]:
            continue
        cohort.record_member_result(
            cohort.MemberResult(
                operation_id=operation["operation_id"],
                agent_id=member["agent_id"],
                expected_result_revision=member["result_revision"],
                final_state=final_state,
                background_command_loss_risk=risk,
                boundary_digest=_DIGEST if boundary else None,
            )
        )


def _commit(operation):
    return cohort.TerminalCommitRequest(
        transition_id=str(uuid.uuid4()),
        operation_id=operation["operation_id"],
        expected_state_epoch=operation["state_epoch"],
        actor="colin",
        reason="verified cohort terminal state",
        receipt_digest=_DIGEST,
    )


def test_generic_stop_transition_cannot_bypass_the_paired_barrier_claim():
    _bind_agent(suffix="1")
    operation = _claim(kind=cohort.KIND_STOP, mode=cohort.MODE_FORCE)

    with pytest.raises(cohort.CohortJournalConflict, match=r"allowed=\[\]"):
        _transition(operation, cohort.STATE_TEARING_DOWN)

    assert oj.get_session_barrier(SESSION) is None
    assert cohort.get_operation(operation["operation_id"])["state"] == cohort.STATE_PREPARING


def test_force_stop_enters_teardown_and_claims_exact_barrier_atomically():
    _bind_agent(suffix="1")
    operation = _claim(kind=cohort.KIND_STOP, mode=cohort.MODE_FORCE)
    request = _begin_teardown(operation, reason="operator force stop")

    first = cohort.begin_stop_teardown(request)
    replay = cohort.begin_stop_teardown(request)

    assert first["adopted"] is False
    assert first["operation"]["state"] == cohort.STATE_TEARING_DOWN
    assert first["barrier"]["claimed_by"] == operation["operation_id"]
    assert replay["adopted"] is True
    assert replay["transition"] == first["transition"]
    assert oj.get_session_barrier(SESSION)["claimed_by"] == operation["operation_id"]


def test_stop_teardown_participates_in_the_callers_outer_rollback():
    _bind_agent(suffix="1")
    operation = _claim(kind=cohort.KIND_STOP, mode=cohort.MODE_FORCE)
    session = database.SessionLocal()
    try:
        session.begin()
        result = cohort.begin_stop_teardown(_begin_teardown(operation), db=session)
        assert result["operation"]["state"] == cohort.STATE_TEARING_DOWN
        assert (
            oj.get_session_barrier(SESSION, db=session)["claimed_by"] == operation["operation_id"]
        )
        session.rollback()
    finally:
        session.close()

    assert oj.get_session_barrier(SESSION) is None
    assert cohort.get_operation(operation["operation_id"])["state"] == cohort.STATE_PREPARING


def test_stop_teardown_refuses_stale_roster_without_claiming_barrier():
    _bind_agent(suffix="1")
    operation = _claim(kind=cohort.KIND_STOP, mode=cohort.MODE_FORCE)
    _bind_agent(suffix="2")

    with pytest.raises(cohort.CohortJournalConflict, match="moved before Stop teardown"):
        cohort.begin_stop_teardown(_begin_teardown(operation))

    assert oj.get_session_barrier(SESSION) is None
    assert cohort.get_operation(operation["operation_id"])["state"] == cohort.STATE_PREPARING


def test_safe_stop_requires_drain_receipt_and_preserves_safe_mode():
    _bind_agent(suffix="1")
    operation = _claim(kind=cohort.KIND_STOP, mode=cohort.MODE_SAFE)
    operation = _transition(operation, cohort.STATE_DRAINING)

    with pytest.raises(cohort.CohortJournalConflict, match="M3-D drain receipt"):
        cohort.begin_stop_teardown(_begin_teardown(operation))

    result = cohort.begin_stop_teardown(_begin_teardown(operation, receipt_digest=_DIGEST))
    assert result["operation"]["state"] == cohort.STATE_TEARING_DOWN
    assert result["operation"]["current_mode"] == cohort.MODE_SAFE
    assert result["transition"]["receipt_digest"] == _DIGEST


def test_safe_to_force_stop_promotion_is_receipted_and_barrier_paired():
    _bind_agent(suffix="1")
    operation = _claim(kind=cohort.KIND_STOP, mode=cohort.MODE_SAFE)
    operation = _transition(operation, cohort.STATE_DRAINING)

    with pytest.raises(cohort.CohortJournalConflict, match="begin_stop_teardown"):
        _transition(
            operation,
            cohort.STATE_TEARING_DOWN,
            promote_to_force=True,
            receipt_digest=_DIGEST,
        )

    result = cohort.begin_stop_teardown(
        _begin_teardown(operation, promote_to_force=True, receipt_digest=_DIGEST)
    )
    assert result["operation"]["requested_mode"] == cohort.MODE_SAFE
    assert result["operation"]["current_mode"] == cohort.MODE_FORCE
    assert result["transition"]["from_mode"] == cohort.MODE_SAFE
    assert result["transition"]["to_mode"] == cohort.MODE_FORCE
    assert result["barrier"]["claimed_by"] == operation["operation_id"]


def test_another_barrier_owner_forces_visible_reconciliation():
    _bind_agent(suffix="1")
    operation = _claim(kind=cohort.KIND_STOP, mode=cohort.MODE_FORCE)
    oj.claim_session_barrier(SESSION, claimed_by="legacy-stop", reason="won first")

    with pytest.raises(cohort.CohortJournalConflict, match="legacy-stop"):
        cohort.begin_stop_teardown(_begin_teardown(operation))

    stored = cohort.get_operation(operation["operation_id"])
    assert stored["state"] == cohort.STATE_PREPARING
    assert stored["transitions"] == []


def test_safe_pause_terminal_commit_pairs_member_proof_and_lifecycle():
    _bind_agent(suffix="1", role=roster.ROLE_SUPERVISOR)
    _bind_agent(suffix="2")
    operation = _claim(kind=cohort.KIND_PAUSE, mode=cohort.MODE_SAFE)
    operation = _transition(operation, cohort.STATE_DRAINING)
    _record_all(
        operation,
        final_state=cohort.FINAL_DRAINED,
        risk=cohort.LOSS_NONE,
        boundary=True,
    )
    request = _commit(operation)

    first = cohort.commit_terminal(request)
    replay = cohort.commit_terminal(request)

    assert first["adopted"] is False
    assert first["operation"]["state"] == cohort.STATE_PAUSED
    assert first["lifecycle"]["lifecycle"] == sl.PAUSED
    assert first["lifecycle"]["epoch"] == 1
    assert first["barrier"] is None
    assert replay["adopted"] is True
    assert sl.describe(SESSION)["lifecycle"] == sl.PAUSED


@pytest.mark.parametrize(
    "final_state",
    [cohort.FINAL_ALREADY_IDLE, cohort.FINAL_PARKED],
)
def test_safe_pause_accepts_members_that_needed_no_drain_work(final_state):
    _bind_agent(suffix="1")
    operation = _claim(kind=cohort.KIND_PAUSE, mode=cohort.MODE_SAFE)
    operation = _transition(operation, cohort.STATE_DRAINING)
    _record_all(operation, final_state=final_state, risk=cohort.LOSS_NONE)

    result = cohort.commit_terminal(_commit(operation))

    assert result["operation"]["state"] == cohort.STATE_PAUSED
    assert result["lifecycle"]["lifecycle"] == sl.PAUSED


def test_terminal_commit_participates_in_the_callers_outer_rollback():
    _bind_agent(suffix="1")
    operation = _claim(kind=cohort.KIND_PAUSE, mode=cohort.MODE_FORCE)
    operation = _transition(operation, cohort.STATE_INTERRUPTING)
    _record_all(
        operation,
        final_state=cohort.FINAL_INTERRUPTED,
        risk=cohort.LOSS_POSSIBLE,
    )
    session = database.SessionLocal()
    try:
        session.begin()
        result = cohort.commit_terminal(_commit(operation), db=session)
        assert result["operation"]["state"] == cohort.STATE_PAUSED
        assert result["lifecycle"]["lifecycle"] == sl.PAUSED
        session.rollback()
    finally:
        session.close()

    assert sl.describe(SESSION)["lifecycle"] == sl.WORKING
    assert cohort.get_operation(operation["operation_id"])["state"] == cohort.STATE_INTERRUPTING


def test_terminal_member_evidence_is_immutable_but_exact_replay_adopts():
    worker = _bind_agent(suffix="1")
    operation = _claim(kind=cohort.KIND_PAUSE, mode=cohort.MODE_FORCE)
    operation = _transition(operation, cohort.STATE_INTERRUPTING)
    evidence = cohort.MemberResult(
        operation_id=operation["operation_id"],
        agent_id=worker["agent"]["agent_id"],
        expected_result_revision=0,
        final_state=cohort.FINAL_INTERRUPTED,
        background_command_loss_risk=cohort.LOSS_POSSIBLE,
    )
    cohort.record_member_result(evidence)
    cohort.commit_terminal(_commit(operation))

    assert cohort.record_member_result(evidence)["adopted"] is True
    with pytest.raises(cohort.CohortJournalConflict, match="evidence is immutable"):
        cohort.record_member_result(
            cohort.MemberResult(
                operation_id=operation["operation_id"],
                agent_id=worker["agent"]["agent_id"],
                expected_result_revision=1,
                final_state=cohort.FINAL_FAILED,
                background_command_loss_risk=cohort.LOSS_KNOWN,
            )
        )


def test_force_pause_refuses_a_surviving_member_then_commits_after_interrupt():
    _bind_agent(suffix="1")
    operation = _claim(kind=cohort.KIND_PAUSE, mode=cohort.MODE_FORCE)
    operation = _transition(operation, cohort.STATE_INTERRUPTING)

    with pytest.raises(cohort.CohortJournalConflict, match="terminal result set"):
        cohort.commit_terminal(_commit(operation))
    assert sl.describe(SESSION)["lifecycle"] == sl.WORKING

    _record_all(
        operation,
        final_state=cohort.FINAL_INTERRUPTED,
        risk=cohort.LOSS_POSSIBLE,
    )
    result = cohort.commit_terminal(_commit(operation))
    assert result["operation"]["state"] == cohort.STATE_PAUSED
    assert result["lifecycle"]["lifecycle"] == sl.PAUSED


def test_stop_terminal_commit_requires_reaped_members_and_exact_barrier():
    _bind_agent(suffix="1")
    operation = _claim(kind=cohort.KIND_STOP, mode=cohort.MODE_FORCE)
    operation = cohort.begin_stop_teardown(_begin_teardown(operation))["operation"]

    with pytest.raises(cohort.CohortJournalConflict, match="terminal result set"):
        cohort.commit_terminal(_commit(operation))
    assert sl.describe(SESSION)["lifecycle"] == sl.WORKING

    _record_all(
        operation,
        final_state=cohort.FINAL_STOPPED,
        risk=cohort.LOSS_POSSIBLE,
    )
    result = cohort.commit_terminal(_commit(operation))
    assert result["operation"]["state"] == cohort.STATE_STOPPED
    assert result["lifecycle"]["lifecycle"] == sl.STOPPED
    assert result["lifecycle"]["restore_to"] == sl.WORKING
    assert result["barrier"]["claimed_by"] == operation["operation_id"]


def test_lifecycle_drift_refuses_terminal_commit_without_changing_cohort():
    _bind_agent(suffix="1")
    operation = _claim(kind=cohort.KIND_PAUSE, mode=cohort.MODE_FORCE)
    operation = _transition(operation, cohort.STATE_INTERRUPTING)
    _record_all(
        operation,
        final_state=cohort.FINAL_INTERRUPTED,
        risk=cohort.LOSS_POSSIBLE,
    )
    sl.declare(SESSION, sl.COMPLETE, declared_by="supervisor")

    with pytest.raises(cohort.CohortJournalConflict, match="lifecycle moved"):
        cohort.commit_terminal(_commit(operation))

    assert sl.describe(SESSION)["lifecycle"] == sl.COMPLETE
    assert cohort.get_operation(operation["operation_id"])["state"] == cohort.STATE_INTERRUPTING


def test_concurrent_stop_teardown_transitions_have_one_durable_winner():
    _bind_agent(suffix="1")
    operation = _claim(kind=cohort.KIND_STOP, mode=cohort.MODE_FORCE)
    requests = [_begin_teardown(operation), _begin_teardown(operation)]
    start = threading.Barrier(2)
    results: list[dict] = []
    conflicts: list[BaseException] = []
    unexpected: list[BaseException] = []

    def run(request):
        try:
            start.wait(timeout=5)
            results.append(cohort.begin_stop_teardown(request))
        except cohort.CohortJournalConflict as exc:
            conflicts.append(exc)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            unexpected.append(exc)

    threads = [threading.Thread(target=run, args=(request,)) for request in requests]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert unexpected == []
    assert len(results) == 1
    assert len(conflicts) == 1
    stored = cohort.get_operation(operation["operation_id"])
    assert stored["state"] == cohort.STATE_TEARING_DOWN
    assert len(stored["transitions"]) == 1
    assert oj.get_session_barrier(SESSION)["claimed_by"] == operation["operation_id"]


def test_concurrent_terminal_commits_increment_lifecycle_once():
    _bind_agent(suffix="1")
    operation = _claim(kind=cohort.KIND_PAUSE, mode=cohort.MODE_FORCE)
    operation = _transition(operation, cohort.STATE_INTERRUPTING)
    _record_all(
        operation,
        final_state=cohort.FINAL_INTERRUPTED,
        risk=cohort.LOSS_POSSIBLE,
    )
    requests = [_commit(operation), _commit(operation)]
    start = threading.Barrier(2)
    results: list[dict] = []
    conflicts: list[BaseException] = []
    unexpected: list[BaseException] = []

    def run(request):
        try:
            start.wait(timeout=5)
            results.append(cohort.commit_terminal(request))
        except cohort.CohortJournalConflict as exc:
            conflicts.append(exc)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            unexpected.append(exc)

    threads = [threading.Thread(target=run, args=(request,)) for request in requests]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert unexpected == []
    assert len(results) == 1
    assert len(conflicts) == 1
    assert sl.describe(SESSION)["lifecycle"] == sl.PAUSED
    assert sl.describe(SESSION)["epoch"] == 1
    stored = cohort.get_operation(operation["operation_id"])
    assert stored["state"] == cohort.STATE_PAUSED
    assert len(stored["transitions"]) == 2
