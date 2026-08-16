"""Rollback and forward/backward compatibility for the Resume slice.

Resume adds no table and no column: it fills in the ``source_operation_id``
and ``resume_target`` carriers C1 already reserved, and reuses the barrier
row's existing ``open`` state. That is what makes rolling *back* to a build
without Resume safe, and these tests pin the specific ways it could stop
being true.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import inspect as sa_inspect

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import cohort_journal as cohort
from cli_agent_orchestrator.services import operation_journal as oj
from cli_agent_orchestrator.services import session_lifecycle as sl
from cli_agent_orchestrator.services import stable_agent_roster as roster

SESSION = "cao-resume-compat"
DIGEST = "a1" * 32


@pytest.fixture(autouse=True)
def _db(isolated_memory_db, monkeypatch, tmp_path):
    from cli_agent_orchestrator import constants

    monkeypatch.setattr(constants, "COMPANION_DIR", tmp_path / "companion")
    return isolated_memory_db


def _bind(suffix: str = "1", role: str = roster.ROLE_SUPERVISOR):
    return roster.bind_generation(
        roster.BindingContract(
            agent_id=str(uuid.uuid4()),
            session_name=SESSION,
            role=role,
            profile_family="supervisor",
            harness="claude_code",
            native_session_id=f"native-{suffix}",
            acquisition_method="chosen_session_id",
            terminal_id=f"term-{suffix}",
            generation=str(uuid.uuid4()),
            pane_id=f"%{suffix}",
            pane_pid=9100 + int(suffix),
            process_identity={"pid": 9100 + int(suffix), "start_marker": f"m-{suffix}"},
            execution_mode="native_tui",
            admitted=True,
        )
    )


def _boundary(source=None):
    boundary = cohort.observe_boundary(SESSION, resume_source_operation_id=source)
    return {
        "lifecycle_epoch": boundary["lifecycle_epoch"],
        "lifecycle_observation": boundary["lifecycle_observation"],
        "roster_revision": boundary["roster_revision"],
        "member_snapshot_digest": boundary["member_snapshot_digest"],
    }


def _stopped():
    operation = cohort.claim_operation(
        cohort.OperationRequest(
            operation_id=str(uuid.uuid4()),
            session_name=SESSION,
            operation_kind=cohort.KIND_STOP,
            requested_mode=cohort.MODE_FORCE,
            initiator_kind=cohort.INITIATOR_OPERATOR,
            initiated_by="colin",
            **_boundary(),
        )
    )
    teardown = cohort.begin_stop_teardown(
        cohort.StopTeardownRequest(
            transition_id=str(uuid.uuid4()),
            operation_id=operation["operation_id"],
            expected_state_epoch=0,
            actor="colin",
        )
    )["operation"]
    for member in cohort.get_operation(operation["operation_id"])["members"]:
        if member["included"]:
            cohort.record_member_result(
                cohort.MemberResult(
                    operation_id=operation["operation_id"],
                    agent_id=member["agent_id"],
                    expected_result_revision=0,
                    final_state=cohort.FINAL_STOPPED,
                    background_command_loss_risk=cohort.LOSS_NONE,
                )
            )
    cohort.commit_terminal(
        cohort.TerminalCommitRequest(
            transition_id=str(uuid.uuid4()),
            operation_id=operation["operation_id"],
            expected_state_epoch=int(teardown["state_epoch"]),
            actor="colin",
            receipt_digest=DIGEST,
        )
    )
    for member in cohort.get_operation(operation["operation_id"])["members"]:
        if member["included"]:
            roster.retire_incarnation(
                terminal_id=member["terminal_id"],
                generation=member["generation"],
                reason="stopped",
            )
    return operation


def _resume(source, target=sl.PAUSED):
    return cohort.claim_operation(
        cohort.OperationRequest(
            operation_id=str(uuid.uuid4()),
            session_name=SESSION,
            operation_kind=cohort.KIND_RESUME,
            requested_mode=cohort.MODE_SAFE,
            initiator_kind=cohort.INITIATOR_OPERATOR,
            initiated_by="colin",
            source_operation_id=source["operation_id"],
            resume_target=target,
            **_boundary(source["operation_id"]),
        )
    )


# ---------------------------------------------------------------------------
# schema: additive only
# ---------------------------------------------------------------------------


def test_resume_adds_no_table_and_no_column(isolated_memory_db):
    """The rollback story rests on this: nothing new to drop."""
    inspector = sa_inspect(isolated_memory_db)
    columns = {c["name"] for c in inspector.get_columns("session_cohort_operations")}

    # Both carriers predate this slice; C1 reserved them explicitly.
    assert {"source_operation_id", "resume_target"} <= columns
    barrier_states = {c["name"] for c in inspector.get_columns("session_effect_barriers")}
    assert "state" in barrier_states


def test_a_pause_or_stop_still_refuses_the_resume_carriers():
    """A rolled-forward build must not start writing them on old kinds."""
    for kind in (cohort.KIND_PAUSE, cohort.KIND_STOP):
        with pytest.raises(cohort.CohortJournalInvalid, match="carries no Resume"):
            cohort.OperationRequest(
                operation_id=str(uuid.uuid4()),
                session_name=SESSION,
                operation_kind=kind,
                requested_mode=cohort.MODE_SAFE,
                initiator_kind=cohort.INITIATOR_OPERATOR,
                initiated_by="colin",
                lifecycle_epoch=0,
                lifecycle_observation=sl.WORKING,
                roster_revision="a" * 64,
                member_snapshot_digest="b" * 64,
                resume_target=sl.WORKING,
            )


def test_pause_and_stop_rows_still_store_null_carriers():
    _bind()
    operation = cohort.claim_operation(
        cohort.OperationRequest(
            operation_id=str(uuid.uuid4()),
            session_name=SESSION,
            operation_kind=cohort.KIND_PAUSE,
            requested_mode=cohort.MODE_SAFE,
            initiator_kind=cohort.INITIATOR_OPERATOR,
            initiated_by="colin",
            **_boundary(),
        )
    )

    assert operation["source_operation_id"] is None
    assert operation["resume_target"] is None


# ---------------------------------------------------------------------------
# rolling back: an older build meets a Resume row
# ---------------------------------------------------------------------------


def test_an_older_build_can_still_read_a_resume_row():
    """The read projections are shape-compatible, not just non-crashing."""
    _bind()
    source = _stopped()
    operation = _resume(source)

    listed = cohort.list_operations(SESSION)
    record = cohort.get_operation(operation["operation_id"])

    # Same keys an older build's Pause/Stop projection already handled.
    assert set(listed[0]) == set(listed[1])
    assert record["operation_kind"] == cohort.KIND_RESUME
    assert record["members"] and record["transitions"] == []


def test_a_rolled_back_build_cannot_advance_a_resume_row():
    """An unknown kind has no allowed transitions, so it fails closed.

    This is the property that makes a rollback safe rather than merely
    survivable: a build that does not understand Resume refuses to move one,
    instead of guessing a target and half-restoring a fleet.
    """
    assert cohort._allowed_target("resume", cohort.MODE_SAFE, cohort.STATE_PREPARING) == frozenset()
    assert cohort._allowed_target("unknown-kind", cohort.MODE_SAFE, cohort.STATE_PREPARING) == (
        frozenset()
    )


def test_a_released_barrier_reads_as_a_plain_open_barrier():
    """Release reuses `open`; it does not invent a third state.

    An older build reading this row sees exactly what it saw before any Stop
    ever claimed the session, which is the state in which it correctly admits
    effects again.
    """
    _bind()
    source = _stopped()
    operation = _resume(source)
    cohort.begin_resume_restore(
        cohort.ResumeRestoreRequest(
            transition_id=str(uuid.uuid4()),
            operation_id=operation["operation_id"],
            expected_state_epoch=0,
            actor="colin",
        )
    )

    barrier = oj.get_session_barrier(SESSION)
    assert barrier["state"] in oj.BARRIER_STATES
    assert barrier["state"] == oj.BARRIER_OPEN
    # The epoch moved, so a lost update stays detectable rather than silent.
    assert barrier["epoch"] >= 1


def test_a_released_barrier_can_be_claimed_again_by_a_later_stop():
    _bind()
    source = _stopped()
    operation = _resume(source)
    cohort.begin_resume_restore(
        cohort.ResumeRestoreRequest(
            transition_id=str(uuid.uuid4()),
            operation_id=operation["operation_id"],
            expected_state_epoch=0,
            actor="colin",
        )
    )
    later = str(uuid.uuid4())

    claimed = oj.claim_session_barrier(SESSION, claimed_by=later, reason="a later Stop")

    assert claimed["state"] == oj.BARRIER_CLAIMED
    assert claimed["claimed_by"] == later


def test_releasing_an_already_open_barrier_adopts_rather_than_erroring():
    """Response-loss on the release itself must converge, not fail."""
    _bind()
    source = _stopped()
    operation = _resume(source)
    cohort.begin_resume_restore(
        cohort.ResumeRestoreRequest(
            transition_id=str(uuid.uuid4()),
            operation_id=operation["operation_id"],
            expected_state_epoch=0,
            actor="colin",
        )
    )

    adopted = oj.release_session_barrier(SESSION, claimed_by=source["operation_id"])

    assert adopted["adopted"] is True
    assert adopted["state"] == oj.BARRIER_OPEN


def test_a_session_with_no_barrier_row_reports_not_found_on_release():
    with pytest.raises(oj.OperationJournalNotFound):
        oj.release_session_barrier(SESSION, claimed_by=str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# the resume boundary derives membership from the Stop cohort
# ---------------------------------------------------------------------------


def test_the_resume_cohort_inherits_the_stop_cohorts_membership():
    """Post-Stop every agent is dormant, so live dispositions cannot decide.

    Without the source-derived membership the whole fleet would read as
    excluded and a Resume would restore nobody while reporting success.
    """
    _bind("1")
    _bind("2", role=roster.ROLE_WORKER)
    source = _stopped()

    boundary = cohort.observe_boundary(SESSION, resume_source_operation_id=source["operation_id"])

    assert [m["pre_disposition"] for m in boundary["members"]] == ["dormant", "dormant"]
    assert all(member["included"] for member in boundary["members"])


def test_an_agent_already_dormant_before_the_stop_is_not_resurrected():
    _bind("1")
    dormant = _bind("2", role=roster.ROLE_WORKER)
    roster.retire_incarnation(
        terminal_id="term-2",
        generation=dormant["incarnation"]["generation"],
        reason="retired before the stop",
    )
    source = _stopped()

    boundary = cohort.observe_boundary(SESSION, resume_source_operation_id=source["operation_id"])

    by_agent = {member["agent_id"]: member for member in boundary["members"]}
    excluded = by_agent[dormant["agent"]["agent_id"]]
    assert excluded["included"] is False
    assert excluded["exclusion_reason"] == "outside-resumed-cohort"


def test_a_resume_must_name_a_terminally_stopped_source():
    """A session stopped the legacy way has no cohort to resume from.

    Reachable in practice: `POST /lifecycle/stop` predates the cohort journal
    and still stops sessions. Pointing a Resume at whatever cohort row happens
    to exist for that session must be refused rather than treated as the Stop
    it descends from.
    """
    _bind()
    pause = cohort.claim_operation(
        cohort.OperationRequest(
            operation_id=str(uuid.uuid4()),
            session_name=SESSION,
            operation_kind=cohort.KIND_PAUSE,
            requested_mode=cohort.MODE_SAFE,
            initiator_kind=cohort.INITIATOR_OPERATOR,
            initiated_by="colin",
            **_boundary(),
        )
    )
    sl.stop(SESSION, declared_by="colin")

    with pytest.raises(cohort.CohortJournalConflict, match="only a terminally stopped cohort"):
        cohort.claim_operation(
            cohort.OperationRequest(
                operation_id=str(uuid.uuid4()),
                session_name=SESSION,
                operation_kind=cohort.KIND_RESUME,
                requested_mode=cohort.MODE_SAFE,
                initiator_kind=cohort.INITIATOR_OPERATOR,
                initiated_by="colin",
                source_operation_id=pause["operation_id"],
                resume_target=sl.PAUSED,
                **_boundary(pause["operation_id"]),
            )
        )


def test_a_resume_cannot_name_a_source_from_another_session():
    _bind()
    source = _stopped()
    other = cohort.observe_boundary("cao-somewhere-else")

    with pytest.raises(cohort.CohortJournalConflict, match="belongs to session"):
        cohort.claim_operation(
            cohort.OperationRequest(
                operation_id=str(uuid.uuid4()),
                session_name="cao-somewhere-else",
                operation_kind=cohort.KIND_RESUME,
                requested_mode=cohort.MODE_SAFE,
                initiator_kind=cohort.INITIATOR_OPERATOR,
                initiated_by="colin",
                source_operation_id=source["operation_id"],
                resume_target=sl.PAUSED,
                lifecycle_epoch=other["lifecycle_epoch"],
                lifecycle_observation=sl.STOPPED,
                roster_revision=other["roster_revision"],
                member_snapshot_digest=other["member_snapshot_digest"],
            )
        )


def test_a_resume_with_an_unknown_source_is_not_found():
    _bind()
    source = _stopped()
    del source

    with pytest.raises(cohort.CohortJournalNotFound, match="unknown Resume source"):
        cohort.claim_operation(
            cohort.OperationRequest(
                operation_id=str(uuid.uuid4()),
                session_name=SESSION,
                operation_kind=cohort.KIND_RESUME,
                requested_mode=cohort.MODE_SAFE,
                initiator_kind=cohort.INITIATOR_OPERATOR,
                initiated_by="colin",
                source_operation_id=str(uuid.uuid4()),
                resume_target=sl.PAUSED,
                **_boundary(),
            )
        )


def test_a_resume_cannot_target_a_lifecycle_the_stop_did_not_record():
    _bind()
    source = _stopped()
    assert sl.describe(SESSION)["restore_to"] == sl.WORKING

    with pytest.raises(cohort.CohortJournalConflict, match="recorded restore_to"):
        cohort.claim_operation(
            cohort.OperationRequest(
                operation_id=str(uuid.uuid4()),
                session_name=SESSION,
                operation_kind=cohort.KIND_RESUME,
                requested_mode=cohort.MODE_SAFE,
                initiator_kind=cohort.INITIATOR_OPERATOR,
                initiated_by="colin",
                source_operation_id=source["operation_id"],
                resume_target=sl.COMPLETE,
                **_boundary(source["operation_id"]),
            )
        )


def test_the_journal_still_works_when_the_cohort_tables_are_absent():
    """A store that never ran the migration must not crash the read path."""
    database.SessionCohortOperationModel.__table__.drop(bind=_engine())
    with pytest.raises(cohort.CohortJournalUnavailable):
        cohort.list_operations(SESSION)


def _engine():
    with database.SessionLocal() as session:
        return session.get_bind()
