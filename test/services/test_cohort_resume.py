"""Operator-only Resume of a stopped cohort (cond-0379 M3-C C4).

The load-bearing case is the *partial* restore. One member that could not come
back must not strand the siblings that did, so these tests pin the terminal
commit, the single wake, and the durable per-member evidence together.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from cli_agent_orchestrator.services import cohort_journal as cohort
from cli_agent_orchestrator.services import cohort_resume as resume
from cli_agent_orchestrator.services import operation_journal as oj
from cli_agent_orchestrator.services import session_lifecycle as sl
from cli_agent_orchestrator.services import stable_agent_roster as roster

SESSION = "cao-cohort-resume"
DIGEST = "c" * 64


@pytest.fixture(autouse=True)
def _db(isolated_memory_db, monkeypatch, tmp_path):
    from cli_agent_orchestrator import constants

    monkeypatch.setattr(constants, "COMPANION_DIR", tmp_path / "companion")
    return isolated_memory_db


def _run(coro):
    return asyncio.run(coro)


def _bind(*, suffix: str, role: str = roster.ROLE_WORKER):
    return roster.bind_generation(
        roster.BindingContract(
            agent_id=str(uuid.uuid4()),
            session_name=SESSION,
            role=role,
            profile_family="supervisor" if role == roster.ROLE_SUPERVISOR else "developer",
            harness="claude_code",
            native_session_id=f"native-{suffix}",
            acquisition_method="chosen_session_id",
            terminal_id=f"term-{suffix}",
            generation=str(uuid.uuid4()),
            pane_id=f"%{suffix}",
            pane_pid=6000 + int(suffix),
            process_identity={"pid": 6000 + int(suffix), "start_marker": f"m-{suffix}"},
            execution_mode="native_tui",
            admitted=True,
        )
    )


def _stop_cohort():
    """Drive a real Stop cohort to its terminal state, then retire the fleet.

    Built through the journal rather than hand-written rows so the Resume
    under test consumes exactly the provenance a real Stop leaves behind.
    """
    boundary = cohort.observe_boundary(SESSION)
    operation = cohort.claim_operation(
        cohort.OperationRequest(
            operation_id=str(uuid.uuid4()),
            session_name=SESSION,
            operation_kind=cohort.KIND_STOP,
            requested_mode=cohort.MODE_FORCE,
            initiator_kind=cohort.INITIATOR_OPERATOR,
            initiated_by="colin",
            lifecycle_epoch=boundary["lifecycle_epoch"],
            lifecycle_observation=boundary["lifecycle_observation"],
            roster_revision=boundary["roster_revision"],
            member_snapshot_digest=boundary["member_snapshot_digest"],
        )
    )
    teardown = cohort.begin_stop_teardown(
        cohort.StopTeardownRequest(
            transition_id=str(uuid.uuid4()),
            operation_id=operation["operation_id"],
            expected_state_epoch=int(operation["state_epoch"]),
            actor="colin",
        )
    )["operation"]
    for member in cohort.get_operation(operation["operation_id"])["members"]:
        if not member["included"]:
            continue
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
    # A real Stop retires every incarnation it collected, which is what makes
    # the resumed agents dormant and the exact M3-B source retired.
    for member in cohort.get_operation(operation["operation_id"])["members"]:
        if not member["included"]:
            continue
        roster.retire_incarnation(
            terminal_id=member["terminal_id"],
            generation=member["generation"],
            reason="stopped",
        )
    return cohort.get_operation(operation["operation_id"])


def _claim_resume(source, *, target: str = sl.WORKING):
    boundary = cohort.observe_boundary(SESSION, resume_source_operation_id=source["operation_id"])
    return cohort.claim_operation(
        cohort.OperationRequest(
            operation_id=str(uuid.uuid4()),
            session_name=SESSION,
            operation_kind=cohort.KIND_RESUME,
            requested_mode=cohort.MODE_SAFE,
            initiator_kind=cohort.INITIATOR_OPERATOR,
            initiated_by="colin",
            lifecycle_epoch=boundary["lifecycle_epoch"],
            lifecycle_observation=boundary["lifecycle_observation"],
            roster_revision=boundary["roster_revision"],
            member_snapshot_digest=boundary["member_snapshot_digest"],
            source_operation_id=source["operation_id"],
            resume_target=target,
        )
    )


def _request(operation, **overrides):
    base = dict(
        operation_id=operation["operation_id"],
        expected_state_epoch=int(operation["state_epoch"]),
        restore_transition_id=str(uuid.uuid4()),
        commit_transition_id=str(uuid.uuid4()),
        reconciliation_transition_id=str(uuid.uuid4()),
        actor="colin",
    )
    base.update(overrides)
    return resume.ResumeRequest(**base)


def _restorer(outcomes):
    """A restorer keyed by the member's terminal id, recording call order."""
    calls = []

    async def _restore(member, _operation):
        calls.append(member["terminal_id"])
        return outcomes[member["terminal_id"]]

    return _restore, calls


# ---------------------------------------------------------------------------
# the mandatory case: a partial restore settles
# ---------------------------------------------------------------------------


def test_partial_restore_settles_and_wakes_once_with_every_outcome():
    _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    _bind(suffix="2")
    _bind(suffix="3")
    source = _stop_cohort()
    operation = _claim_resume(source)
    restore, _calls = _restorer(
        {
            "term-1": resume.MemberRestore(resume.OUTCOME_EXACT, "supervisor back"),
            "term-2": resume.MemberRestore(resume.OUTCOME_EXACT, "worker back"),
            "term-3": resume.MemberRestore(resume.OUTCOME_FAILED, "provider refused"),
        }
    )
    wakes = []

    async def _waker(_operation, results, identifier):
        wakes.append((identifier, sorted(r["final_state"] for r in results)))
        return resume.SupervisorWake(True, receipt_digest=DIGEST)

    settled = _run(
        resume.execute_resume_and_start(_request(operation), restorer=restore, waker=_waker)
    )

    # One failed member does not hold its restored siblings.
    assert settled["state"] == cohort.STATE_SETTLED
    assert sl.describe(SESSION)["lifecycle"] == sl.WORKING
    # Exactly one wake, and it describes every outcome including the failure.
    assert len(wakes) == 1
    assert wakes[0][1] == ["failed", "restored-exact", "restored-exact"]
    finals = {
        member["terminal_id"]: member["final_state"]
        for member in cohort.get_operation(operation["operation_id"])["members"]
    }
    assert finals == {
        "term-1": cohort.FINAL_RESTORED_EXACT,
        "term-2": cohort.FINAL_RESTORED_EXACT,
        "term-3": cohort.FINAL_FAILED,
    }


def test_failed_and_unresumable_are_terminal_but_undecided_is_not():
    _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    _bind(suffix="2")
    source = _stop_cohort()
    operation = _claim_resume(source)
    restore, _calls = _restorer(
        {
            "term-1": resume.MemberRestore(resume.OUTCOME_UNRESUMABLE, "no resume path"),
            "term-2": resume.MemberRestore(resume.OUTCOME_FAILED, "refused"),
        }
    )

    settled = _run(
        resume.execute_resume_and_start(
            _request(operation),
            restorer=restore,
            waker=lambda *_a: _delivered(),
        )
    )
    assert settled["state"] == cohort.STATE_SETTLED


async def _delivered():
    return resume.SupervisorWake(True, receipt_digest=DIGEST)


def test_undecided_member_blocks_the_commit_and_emits_no_wake():
    _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    _bind(suffix="2")
    source = _stop_cohort()
    operation = _claim_resume(source)
    restore, _calls = _restorer(
        {
            "term-1": resume.MemberRestore(resume.OUTCOME_EXACT, "back"),
            "term-2": resume.MemberRestore(resume.OUTCOME_UNDECIDED, "ambiguous"),
        }
    )
    wakes = []

    async def _waker(*_args):
        wakes.append(1)
        return resume.SupervisorWake(True)

    result = _run(
        resume.execute_resume_and_start(_request(operation), restorer=restore, waker=_waker)
    )

    assert result["state"] == cohort.STATE_RECONCILIATION_REQUIRED
    assert wakes == []


# ---------------------------------------------------------------------------
# Resume paused: strict zero input
# ---------------------------------------------------------------------------


def test_resume_paused_sends_zero_input_and_never_wakes(monkeypatch):
    from cli_agent_orchestrator.services import control_input_service

    _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    _bind(suffix="2")
    source = _stop_cohort()
    operation = _claim_resume(source, target=sl.PAUSED)

    def _no_bytes(*_args, **_kwargs):
        raise AssertionError("Resume paused must not deliver a single byte")

    monkeypatch.setattr(control_input_service, "deliver_control_input", _no_bytes)
    monkeypatch.setattr(control_input_service, "deliver_native_inbox_payload", _no_bytes)
    restore, _calls = _restorer(
        {
            "term-1": resume.MemberRestore(resume.OUTCOME_EXACT, "back"),
            "term-2": resume.MemberRestore(resume.OUTCOME_EXACT, "back"),
        }
    )

    settled = _run(resume.execute_resume_paused(_request(operation), restorer=restore))

    assert settled["state"] == cohort.STATE_SETTLED
    assert sl.describe(SESSION)["lifecycle"] == sl.PAUSED


def test_resume_paused_has_no_waker_parameter():
    """Zero input is structural, not a runtime branch somebody can flip."""
    import inspect

    assert "waker" not in inspect.signature(resume.execute_resume_paused).parameters
    assert "waker" in inspect.signature(resume.execute_resume_and_start).parameters


def test_resume_and_start_refuses_a_paused_target():
    _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    source = _stop_cohort()
    operation = _claim_resume(source, target=sl.PAUSED)
    restore, _calls = _restorer({"term-1": resume.MemberRestore(resume.OUTCOME_EXACT)})

    with pytest.raises(resume.CohortResumeConflict, match="restores to"):
        _run(
            resume.execute_resume_and_start(
                _request(operation), restorer=restore, waker=lambda *_a: _delivered()
            )
        )


# ---------------------------------------------------------------------------
# duplicate response / retry
# ---------------------------------------------------------------------------


def test_replayed_resume_adopts_and_never_wakes_twice():
    _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    source = _stop_cohort()
    operation = _claim_resume(source)
    restore, calls = _restorer({"term-1": resume.MemberRestore(resume.OUTCOME_EXACT)})
    wakes = []

    async def _waker(_operation, _results, identifier):
        wakes.append(identifier)
        return resume.SupervisorWake(True, receipt_digest=DIGEST)

    request = _request(operation)
    first = _run(resume.execute_resume_and_start(request, restorer=restore, waker=_waker))
    second = _run(resume.execute_resume_and_start(request, restorer=restore, waker=_waker))

    assert first["state"] == second["state"] == cohort.STATE_SETTLED
    assert len(wakes) == 1
    assert calls == ["term-1"]


def test_wake_id_is_stable_per_operation_so_a_retry_adopts():
    identifier = str(uuid.uuid4())
    assert resume.wake_id(identifier) == resume.wake_id(identifier)
    assert resume.wake_id(identifier) != resume.wake_id(str(uuid.uuid4()))


def test_member_operation_ids_are_derived_so_a_retry_adopts_the_same_reincarnation():
    operation_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    assert resume.member_operation_id(operation_id, agent_id) == resume.member_operation_id(
        operation_id, agent_id
    )
    assert resume.member_operation_id(operation_id, agent_id) != resume.member_operation_id(
        operation_id, str(uuid.uuid4())
    )


def test_an_undelivered_wake_reconciles_rather_than_settling():
    _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    source = _stop_cohort()
    operation = _claim_resume(source)
    restore, _calls = _restorer({"term-1": resume.MemberRestore(resume.OUTCOME_EXACT)})

    async def _undelivered(*_args):
        return resume.SupervisorWake(False, detail="no authority")

    result = _run(
        resume.execute_resume_and_start(_request(operation), restorer=restore, waker=_undelivered)
    )

    assert result["state"] == cohort.STATE_RECONCILIATION_REQUIRED


def test_the_dark_default_waker_has_no_authority_and_says_so():
    wake = _run(resume._default_waker({}, [], "id"))
    assert wake.delivered is False
    assert "no supervisor reconciliation authority" in wake.detail


# ---------------------------------------------------------------------------
# ordering and continuity
# ---------------------------------------------------------------------------


def test_the_supervisor_is_restored_before_its_workers():
    _bind(suffix="2")
    _bind(suffix="3")
    _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    source = _stop_cohort()
    operation = _claim_resume(source, target=sl.PAUSED)
    restore, calls = _restorer(
        {
            "term-1": resume.MemberRestore(resume.OUTCOME_EXACT),
            "term-2": resume.MemberRestore(resume.OUTCOME_EXACT),
            "term-3": resume.MemberRestore(resume.OUTCOME_EXACT),
        }
    )

    _run(resume.execute_resume_paused(_request(operation), restorer=restore))

    assert calls[0] == "term-1"


def test_fresh_restore_is_explicit_and_never_a_silent_exact_downgrade():
    """A fresh outcome only ever comes from a supplied authority."""
    _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    source = _stop_cohort()
    operation = _claim_resume(source, target=sl.PAUSED)
    restore, _calls = _restorer(
        {"term-1": resume.MemberRestore(resume.OUTCOME_FRESH, "operator-proven fresh restore")}
    )

    settled = _run(resume.execute_resume_paused(_request(operation), restorer=restore))

    assert settled["state"] == cohort.STATE_SETTLED
    member = cohort.get_operation(operation["operation_id"])["members"][0]
    assert member["final_state"] == cohort.FINAL_RESTORED_FRESH


def test_the_default_restorer_never_downgrades_a_refused_exact_restore_to_fresh():
    _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    source = _stop_cohort()
    operation = _claim_resume(source, target=sl.PAUSED)
    member = cohort.get_operation(operation["operation_id"])["members"][0]

    # No restore contract was ever published for this incarnation, so M3-B
    # cannot authorize an exact resume. The honest answer is `failed`, never a
    # new native session standing in for the old transcript.
    outcome = _run(resume._default_restorer(member, _operation_view(operation)))

    assert outcome.outcome in {resume.OUTCOME_FAILED, resume.OUTCOME_UNRESUMABLE}
    assert outcome.outcome != resume.OUTCOME_FRESH


def _operation_view(operation):
    return cohort.get_operation(operation["operation_id"])


def test_a_member_with_no_resume_identity_is_unresumable_not_failed():
    _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    source = _stop_cohort()
    operation = _claim_resume(source, target=sl.PAUSED)
    member = dict(cohort.get_operation(operation["operation_id"])["members"][0])
    member["native_session_id"] = None

    outcome = _run(resume._default_restorer(member, _operation_view(operation)))

    assert outcome.outcome == resume.OUTCOME_UNRESUMABLE


# ---------------------------------------------------------------------------
# the Stop barrier
# ---------------------------------------------------------------------------


def test_resume_releases_only_its_own_stop_barrier():
    _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    source = _stop_cohort()
    assert oj.get_session_barrier(SESSION)["state"] == oj.BARRIER_CLAIMED

    operation = _claim_resume(source, target=sl.PAUSED)
    cohort.begin_resume_restore(
        cohort.ResumeRestoreRequest(
            transition_id=str(uuid.uuid4()),
            operation_id=operation["operation_id"],
            expected_state_epoch=int(operation["state_epoch"]),
            actor="colin",
        )
    )

    assert oj.get_session_barrier(SESSION)["state"] == oj.BARRIER_OPEN
    assert sl.describe(SESSION)["lifecycle"] == sl.PAUSED


def test_a_stranger_cannot_release_the_stop_barrier():
    _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    _stop_cohort()

    with pytest.raises(oj.OperationJournalConflict, match="only that operation's Resume"):
        oj.release_session_barrier(SESSION, claimed_by=str(uuid.uuid4()))


def test_a_resume_cannot_settle_if_a_new_stop_reclaimed_the_barrier():
    _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    source = _stop_cohort()
    operation = _claim_resume(source, target=sl.PAUSED)
    restored = cohort.begin_resume_restore(
        cohort.ResumeRestoreRequest(
            transition_id=str(uuid.uuid4()),
            operation_id=operation["operation_id"],
            expected_state_epoch=int(operation["state_epoch"]),
            actor="colin",
        )
    )["operation"]
    member = cohort.get_operation(operation["operation_id"])["members"][0]
    cohort.record_member_result(
        cohort.MemberResult(
            operation_id=operation["operation_id"],
            agent_id=member["agent_id"],
            expected_result_revision=0,
            final_state=cohort.FINAL_RESTORED_EXACT,
            background_command_loss_risk=cohort.LOSS_NONE,
        )
    )
    # A newer Stop wins the session while this Resume was restoring.
    oj.claim_session_barrier(SESSION, claimed_by=str(uuid.uuid4()), reason="a newer Stop")

    with pytest.raises(cohort.CohortJournalConflict, match="reclaimed"):
        cohort.commit_terminal(
            cohort.TerminalCommitRequest(
                transition_id=str(uuid.uuid4()),
                operation_id=operation["operation_id"],
                expected_state_epoch=int(restored["state_epoch"]),
                actor="colin",
                receipt_digest=DIGEST,
            )
        )


def test_the_barrier_release_and_lifecycle_write_are_one_transaction(monkeypatch):
    """Neither half can be observed without the other.

    The failure is injected in the exact window the atomicity claim is about:
    after the barrier release has been issued, before the lifecycle row is
    rewritten. If those were two transactions, the session would be left
    readable as ``stopped`` with an *open* barrier — the state in which every
    effect is admitted against a fleet that has no panes.
    """
    _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    source = _stop_cohort()
    operation = _claim_resume(source, target=sl.PAUSED)

    def _die_after_release(*_args, **_kwargs):
        raise cohort.CohortJournalConflict("injected mid-transaction failure")

    monkeypatch.setattr(cohort, "_resume_lifecycle_row", _die_after_release)

    with pytest.raises(cohort.CohortJournalConflict, match="injected"):
        cohort.begin_resume_restore(
            cohort.ResumeRestoreRequest(
                transition_id=str(uuid.uuid4()),
                operation_id=operation["operation_id"],
                expected_state_epoch=int(operation["state_epoch"]),
                actor="colin",
            )
        )

    # The release rolled back with the write it was paired to.
    assert oj.get_session_barrier(SESSION)["state"] == oj.BARRIER_CLAIMED
    assert sl.describe(SESSION)["lifecycle"] == sl.STOPPED
    assert cohort.get_operation(operation["operation_id"])["state"] == cohort.STATE_PREPARING


def test_a_stale_boundary_observation_refuses_before_the_barrier_is_touched():
    """The classic stale-read race: something moved between claim and restore."""
    _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    source = _stop_cohort()
    operation = _claim_resume(source, target=sl.PAUSED)

    # Archiving bumps the lifecycle epoch without leaving `stopped`, so the
    # claim's recorded epoch no longer describes the session.
    sl.set_archived(SESSION, True, declared_by="someone-else")

    with pytest.raises(cohort.CohortJournalConflict, match="moved before Resume restore"):
        cohort.begin_resume_restore(
            cohort.ResumeRestoreRequest(
                transition_id=str(uuid.uuid4()),
                operation_id=operation["operation_id"],
                expected_state_epoch=int(operation["state_epoch"]),
                actor="colin",
            )
        )

    assert oj.get_session_barrier(SESSION)["state"] == oj.BARRIER_CLAIMED
