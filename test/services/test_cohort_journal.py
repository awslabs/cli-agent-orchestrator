"""Dark M3-C C1 cohort journal and closed transition tests (cond-0379)."""

from __future__ import annotations

import uuid

import pytest

from cli_agent_orchestrator.services import cohort_journal as cohort
from cli_agent_orchestrator.services import operation_journal as oj
from cli_agent_orchestrator.services import session_lifecycle as sl
from cli_agent_orchestrator.services import stable_agent_roster as roster

SESSION = "cao-cohort-a"
_DIGEST = "a" * 64


@pytest.fixture(autouse=True)
def _db(isolated_memory_db):
    return isolated_memory_db


def _bind_agent(*, role=roster.ROLE_WORKER, suffix="1", native=True):
    return roster.bind_generation(
        roster.BindingContract(
            agent_id=str(uuid.uuid4()),
            session_name=SESSION,
            role=role,
            profile_family="supervisor" if role == roster.ROLE_SUPERVISOR else "developer",
            harness="claude_code",
            native_session_id=f"native-{suffix}" if native else None,
            acquisition_method="chosen_session_id" if native else None,
            terminal_id=f"term-{suffix}",
            generation=str(uuid.uuid4()),
            pane_id=f"%{suffix}",
            pane_pid=4000 + int(suffix),
            process_identity={"pid": 4000 + int(suffix), "start_marker": f"marker-{suffix}"},
            execution_mode="native_tui",
            admitted=True,
        )
    )


def _request(boundary, *, operation_id=None, kind=cohort.KIND_PAUSE, mode=cohort.MODE_SAFE):
    return cohort.OperationRequest(
        operation_id=operation_id or str(uuid.uuid4()),
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


def _transition(operation, to_state, **changes):
    payload = {
        "transition_id": str(uuid.uuid4()),
        "operation_id": operation["operation_id"],
        "expected_state_epoch": operation["state_epoch"],
        "to_state": to_state,
        "actor": "colin",
    }
    payload.update(changes)
    return cohort.TransitionRequest(**payload)


def test_boundary_is_digest_bound_and_keeps_historical_agents_visible():
    supervisor = _bind_agent(role=roster.ROLE_SUPERVISOR, suffix="1")
    live = _bind_agent(suffix="2")
    dormant = _bind_agent(suffix="3")
    roster.retire_incarnation(
        terminal_id=dormant["incarnation"]["terminal_id"],
        generation=dormant["incarnation"]["generation"],
        reason="completed-before-stop",
    )

    boundary = cohort.observe_boundary(SESSION)

    assert boundary["lifecycle_observation"] == sl.WORKING
    assert boundary["lifecycle_epoch"] == 0
    assert len(boundary["members"]) == 3
    by_id = {member["agent_id"]: member for member in boundary["members"]}
    assert by_id[supervisor["agent"]["agent_id"]]["included"] is True
    assert by_id[live["agent"]["agent_id"]]["included"] is True
    assert by_id[dormant["agent"]["agent_id"]]["included"] is False
    assert by_id[dormant["agent"]["agent_id"]]["final_state"] == cohort.FINAL_EXCLUDED_HISTORICAL
    assert len(boundary["roster_revision"]) == 64
    assert len(boundary["member_snapshot_digest"]) == 64


def test_claim_is_atomic_durable_and_exact_replay_adopts():
    _bind_agent(role=roster.ROLE_SUPERVISOR, suffix="1")
    _bind_agent(suffix="2")
    boundary = cohort.observe_boundary(SESSION)
    request = _request(boundary)

    first = cohort.claim_operation(request)
    replay = cohort.claim_operation(request)
    stored = cohort.get_operation(request.operation_id)

    assert first["adopted"] is False
    assert replay["adopted"] is True
    assert stored["state"] == cohort.STATE_PREPARING
    assert stored["state_epoch"] == 0
    assert stored["requested_mode"] == cohort.MODE_SAFE
    assert stored["current_mode"] == cohort.MODE_SAFE
    assert len(stored["members"]) == 2
    assert stored["transitions"] == []


def test_changed_replay_and_competing_operation_surface_the_winner():
    _bind_agent(suffix="1")
    boundary = cohort.observe_boundary(SESSION)
    request = _request(boundary)
    cohort.claim_operation(request)

    with pytest.raises(cohort.CohortJournalConflict, match="different immutable"):
        cohort.claim_operation(
            cohort.OperationRequest(
                **{
                    **request.__dict__,
                    "initiated_by": "somebody-else",
                }
            )
        )
    with pytest.raises(cohort.CohortJournalConflict, match="winning operation"):
        cohort.claim_operation(_request(boundary, operation_id=str(uuid.uuid4())))


def test_claim_refuses_a_stale_roster_or_lifecycle_observation():
    _bind_agent(suffix="1")
    stale_roster = cohort.observe_boundary(SESSION)
    _bind_agent(suffix="2")
    with pytest.raises(cohort.CohortJournalConflict, match="moved since"):
        cohort.claim_operation(_request(stale_roster))

    fresh = cohort.observe_boundary(SESSION)
    sl.declare(SESSION, sl.WORKING, declared_by="supervisor")
    with pytest.raises(cohort.CohortJournalConflict, match="moved since"):
        cohort.claim_operation(_request(fresh))


def test_safe_pause_never_silently_becomes_force():
    _bind_agent(suffix="1")
    operation = cohort.claim_operation(_request(cohort.observe_boundary(SESSION)))
    draining = cohort.transition_operation(_transition(operation, cohort.STATE_DRAINING))[
        "operation"
    ]

    with pytest.raises(cohort.CohortJournalConflict, match="cannot move"):
        cohort.transition_operation(_transition(draining, cohort.STATE_INTERRUPTING))
    with pytest.raises(cohort.CohortJournalInvalid, match="requires a receipt"):
        _transition(
            draining,
            cohort.STATE_INTERRUPTING,
            promote_to_force=True,
        )

    promoted = cohort.transition_operation(
        _transition(
            draining,
            cohort.STATE_INTERRUPTING,
            promote_to_force=True,
            receipt_digest=_DIGEST,
            reason="operator chose immediate interruption",
        )
    )
    assert promoted["operation"]["requested_mode"] == cohort.MODE_SAFE
    assert promoted["operation"]["current_mode"] == cohort.MODE_FORCE
    assert promoted["transition"]["from_mode"] == cohort.MODE_SAFE
    assert promoted["transition"]["to_mode"] == cohort.MODE_FORCE
    assert promoted["transition"]["receipt_digest"] == _DIGEST


def test_reconciliation_requires_a_receipted_retry_or_force_promotion():
    _bind_agent(suffix="1")
    operation = cohort.claim_operation(_request(cohort.observe_boundary(SESSION)))
    draining = cohort.transition_operation(_transition(operation, cohort.STATE_DRAINING))[
        "operation"
    ]
    reconciliation = cohort.transition_operation(
        _transition(
            draining,
            cohort.STATE_RECONCILIATION_REQUIRED,
            reason="supervisor drain receipt timed out",
        )
    )["operation"]

    with pytest.raises(cohort.CohortJournalConflict, match="needs a receipt"):
        cohort.transition_operation(_transition(reconciliation, cohort.STATE_DRAINING))
    retried = cohort.transition_operation(
        _transition(
            reconciliation,
            cohort.STATE_DRAINING,
            receipt_digest="b" * 64,
            reason="operator requested a safe retry",
        )
    )["operation"]
    reconciliation_again = cohort.transition_operation(
        _transition(retried, cohort.STATE_RECONCILIATION_REQUIRED)
    )["operation"]
    promoted = cohort.transition_operation(
        _transition(
            reconciliation_again,
            cohort.STATE_INTERRUPTING,
            promote_to_force=True,
            receipt_digest="c" * 64,
            reason="operator explicitly promoted after failed retry",
        )
    )["operation"]

    assert promoted["current_mode"] == cohort.MODE_FORCE
    assert promoted["state"] == cohort.STATE_INTERRUPTING


@pytest.mark.parametrize(
    ("kind", "mode", "in_progress_state", "terminal_state"),
    [
        (cohort.KIND_PAUSE, cohort.MODE_SAFE, cohort.STATE_DRAINING, cohort.STATE_PAUSED),
        (
            cohort.KIND_STOP,
            cohort.MODE_FORCE,
            cohort.STATE_TEARING_DOWN,
            cohort.STATE_STOPPED,
        ),
    ],
)
def test_c1_cannot_record_terminal_state_without_the_later_lifecycle_cas(
    kind, mode, in_progress_state, terminal_state
):
    _bind_agent(suffix="1")
    operation = cohort.claim_operation(
        _request(cohort.observe_boundary(SESSION), kind=kind, mode=mode)
    )
    in_progress = cohort.transition_operation(_transition(operation, in_progress_state))[
        "operation"
    ]

    with pytest.raises(cohort.CohortJournalConflict, match="cannot move"):
        cohort.transition_operation(_transition(in_progress, terminal_state))


def test_force_stop_has_its_own_closed_path_and_transition_replay_adopts():
    _bind_agent(suffix="1")
    operation = cohort.claim_operation(
        _request(
            cohort.observe_boundary(SESSION),
            kind=cohort.KIND_STOP,
            mode=cohort.MODE_FORCE,
        )
    )
    request = _transition(operation, cohort.STATE_TEARING_DOWN)
    first = cohort.transition_operation(request)
    replay = cohort.transition_operation(request)

    assert first["adopted"] is False
    assert replay["adopted"] is True
    assert replay["operation"]["state"] == cohort.STATE_TEARING_DOWN
    assert replay["operation"]["state_epoch"] == 1


def test_member_evidence_is_bounded_cas_state_and_safe_mode_cannot_interrupt():
    worker = _bind_agent(suffix="1")
    operation = cohort.claim_operation(_request(cohort.observe_boundary(SESSION)))
    agent_id = worker["agent"]["agent_id"]
    request = cohort.MemberResult(
        operation_id=operation["operation_id"],
        agent_id=agent_id,
        expected_result_revision=0,
        task_occurrence_id="cond-0380:round-7:task-2",
        boundary_digest="b" * 64,
        report_digest="c" * 64,
        checkpoint_digest="d" * 64,
        final_state=cohort.FINAL_DRAINED,
        background_command_loss_risk=cohort.LOSS_NONE,
    )
    first = cohort.record_member_result(request)
    replay = cohort.record_member_result(request)

    assert first["adopted"] is False
    assert replay["adopted"] is True
    assert replay["result_revision"] == 1
    assert replay["task_occurrence_id"] == "cond-0380:round-7:task-2"

    with pytest.raises(cohort.CohortJournalConflict, match="force-mode"):
        cohort.record_member_result(
            cohort.MemberResult(
                operation_id=operation["operation_id"],
                agent_id=agent_id,
                expected_result_revision=1,
                final_state=cohort.FINAL_INTERRUPTED,
                background_command_loss_risk=cohort.LOSS_POSSIBLE,
                interrupt_action="escape",
                interrupt_outcome="turn-cancelled",
            )
        )


def test_an_included_member_cannot_be_relabeled_excluded_historical():
    worker = _bind_agent(suffix="1")
    operation = cohort.claim_operation(_request(cohort.observe_boundary(SESSION)))

    with pytest.raises(cohort.CohortJournalConflict, match="cannot be relabeled"):
        cohort.record_member_result(
            cohort.MemberResult(
                operation_id=operation["operation_id"],
                agent_id=worker["agent"]["agent_id"],
                expected_result_revision=0,
                final_state=cohort.FINAL_EXCLUDED_HISTORICAL,
                background_command_loss_risk=cohort.LOSS_NONE,
            )
        )


def test_c1_journaling_has_no_lifecycle_or_stop_barrier_effect():
    _bind_agent(suffix="1")
    before = sl.describe(SESSION)
    operation = cohort.claim_operation(
        _request(
            cohort.observe_boundary(SESSION),
            kind=cohort.KIND_STOP,
            mode=cohort.MODE_FORCE,
        )
    )
    cohort.transition_operation(_transition(operation, cohort.STATE_TEARING_DOWN))

    after = sl.describe(SESSION)
    assert after == before
    assert oj.get_session_barrier(SESSION) is None


def test_list_operations_is_session_scoped_and_tmux_independent():
    _bind_agent(suffix="1")
    request = _request(cohort.observe_boundary(SESSION))
    cohort.claim_operation(request)

    assert [row["operation_id"] for row in cohort.list_operations(SESSION)] == [
        request.operation_id
    ]
    assert cohort.list_operations("cao-another-session") == []
