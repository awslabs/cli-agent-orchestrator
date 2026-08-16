"""M3-D supervisor reconciliation: one wake, exact content, zero on paused."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from cli_agent_orchestrator.services import cohort_journal as cohort
from cli_agent_orchestrator.services import cohort_operations, cohort_resume, control_input_service
from cli_agent_orchestrator.services import session_lifecycle as sl
from cli_agent_orchestrator.services import stable_agent_roster as roster
from cli_agent_orchestrator.services import supervisor_reconciliation as recon
from cli_agent_orchestrator.services import task_occurrence as occ
from cli_agent_orchestrator.services.control_input_contract import ACCEPTED, REFUSED

SESSION = "cao-m3d-recon"
DIGEST = "c" * 64


@pytest.fixture(autouse=True)
def _db(isolated_memory_db, monkeypatch, tmp_path):
    from cli_agent_orchestrator import constants

    monkeypatch.setattr(constants, "COMPANION_DIR", tmp_path / "companion")
    return isolated_memory_db


def _run(coro):
    return asyncio.run(coro)


def _bind(*, suffix, role=roster.ROLE_WORKER):
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
            pane_pid=7000 + int(suffix),
            process_identity={"pid": 7000 + int(suffix), "start_marker": f"m-{suffix}"},
            execution_mode="native_tui",
            admitted=True,
        )
    )


def _stop_cohort():
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
    for member in cohort.get_operation(operation["operation_id"])["members"]:
        if not member["included"]:
            continue
        roster.retire_incarnation(
            terminal_id=member["terminal_id"],
            generation=member["generation"],
            reason="stopped",
        )
    return cohort.get_operation(operation["operation_id"])


def _claim_resume(source, *, target=sl.WORKING, operation_id=None):
    boundary = cohort.observe_boundary(SESSION, resume_source_operation_id=source["operation_id"])
    return cohort.claim_operation(
        cohort.OperationRequest(
            operation_id=operation_id or str(uuid.uuid4()),
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


def _restorer(outcomes):
    async def _restore(member, _operation):
        return outcomes[member["terminal_id"]]

    return _restore


class _Deliverer:
    def __init__(self, outcome=ACCEPTED):
        self.outcome = outcome
        self.calls: list[tuple[str, str]] = []

    def __call__(self, target, control_id, text):
        self.calls.append((control_id, text))
        return control_input_service.ControlInputResult(
            control_id=control_id,
            outcome=self.outcome,
            reason_code=None if self.outcome == ACCEPTED else "pane-busy",
            detail="",
        )


def _rebind_supervisor(agent_id, suffix="9"):
    """Bring one agent back on a new pane, as an exact restore would."""
    agent = roster.get_agent(agent_id)
    return roster.bind_generation(
        roster.BindingContract(
            agent_id=agent_id,
            session_name=SESSION,
            role=agent["role"],
            profile_family=agent["profile_family"],
            harness=agent["current_lineage"]["harness"],
            native_session_id=agent["current_lineage"]["native_session_id"],
            acquisition_method="pinned_resume",
            terminal_id=f"term-{suffix}",
            generation=str(uuid.uuid4()),
            pane_id=f"%{suffix}",
            pane_pid=7900 + int(suffix),
            process_identity={"pid": 7900 + int(suffix), "start_marker": f"m-{suffix}"},
            execution_mode="native_tui",
            admitted=True,
        )
    )


# ---------------------------------------------------------------------------
# message content
# ---------------------------------------------------------------------------


def test_the_message_lists_every_category_the_supervisor_cannot_see_itself():
    outcomes = [
        recon.WorkerOutcome("a" * 36, roster.ROLE_SUPERVISOR, recon.CATEGORY_EXACT, "term-1"),
        recon.WorkerOutcome(
            "b" * 36,
            roster.ROLE_WORKER,
            recon.CATEGORY_FRESH,
            "term-2",
            seed_quality=occ.SEED_COMPLETE,
            seed_sufficient=True,
        ),
        recon.WorkerOutcome("c" * 36, roster.ROLE_WORKER, recon.CATEGORY_FAILED, "term-3"),
        recon.WorkerOutcome("d" * 36, roster.ROLE_WORKER, recon.CATEGORY_INTERRUPTED, "term-4"),
        recon.WorkerOutcome("e" * 36, roster.ROLE_WORKER, recon.CATEGORY_PARKED, "term-5"),
        recon.WorkerOutcome("f" * 36, roster.ROLE_WORKER, recon.CATEGORY_UNRESUMABLE, "term-6"),
    ]

    message = recon.render_message(
        session_name=SESSION,
        source_kind=recon.SOURCE_RESUME_AND_START,
        operation_id="0" * 36,
        resume_target=sl.WORKING,
        outcomes=outcomes,
    )

    text = message["text"]
    for token in ("exact=1", "fresh=1", "interrupted=1", "parked=1", "failed=1", "unresumable=1"):
        assert token in text
    # The workers needing action are named; the ones that came back fine are
    # counted, because the line has a hard byte budget.
    assert "term-2:fresh/seed=complete" in text
    assert "term-3:failed" in text
    assert "term-6:unresumable" in text
    assert "do not replay their input" in text
    assert message["truncated"] is False


def test_the_message_is_deterministic_and_fits_the_control_byte_budget():
    outcomes = [
        recon.WorkerOutcome(
            str(uuid.uuid5(uuid.NAMESPACE_DNS, str(index))),
            roster.ROLE_WORKER,
            recon.CATEGORY_FAILED,
            f"terminal-{index:03d}",
        )
        for index in range(60)
    ]

    first = recon.render_message(
        session_name=SESSION,
        source_kind=recon.SOURCE_RESUME_AND_START,
        operation_id="0" * 36,
        resume_target=sl.WORKING,
        outcomes=outcomes,
    )
    second = recon.render_message(
        session_name=SESSION,
        source_kind=recon.SOURCE_RESUME_AND_START,
        operation_id="0" * 36,
        resume_target=sl.WORKING,
        outcomes=outcomes,
    )

    assert first["text"] == second["text"]
    assert len(first["text"].encode("utf-8")) <= recon.MAX_MESSAGE_BYTES
    assert first["truncated"] is True
    # A truncated line still carries the full counts, so nothing is hidden.
    assert "failed=60" in first["text"]
    assert "\n" not in first["text"]


def test_a_fresh_worker_is_reported_with_the_completeness_of_its_seed():
    supervisor = _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    worker = _bind(suffix="2")
    occ.open_occurrence(
        occ.OpenRequest(
            task_occurrence_id=str(uuid.uuid4()),
            session_name=SESSION,
            agent_id=worker["agent"]["agent_id"],
            round_index=0,
            dispatch_digest="a" * 64,
            incarnation=occ.EffectIncarnation(incarnation_id="inc-2", terminal_id="term-2"),
            seed=occ.TaskSeed(occ.SEED_TRUNCATED, summary_digest="b" * 64),
        )
    )
    record = {
        "operation_id": str(uuid.uuid4()),
        "session_name": SESSION,
        "resume_target": sl.WORKING,
        "members": [
            {
                "agent_id": supervisor["agent"]["agent_id"],
                "role": roster.ROLE_SUPERVISOR,
                "included": True,
                "terminal_id": "term-1",
                "harness": "claude_code",
                "final_state": cohort.FINAL_RESTORED_EXACT,
            },
            {
                "agent_id": worker["agent"]["agent_id"],
                "role": roster.ROLE_WORKER,
                "included": True,
                "terminal_id": "term-2",
                "harness": "claude_code",
                "final_state": cohort.FINAL_RESTORED_FRESH,
            },
        ],
    }

    outcomes = recon.classify_members(record)
    fresh = next(item for item in outcomes if item.category == recon.CATEGORY_FRESH)

    assert fresh.seed_quality == occ.SEED_TRUNCATED
    assert fresh.seed_sufficient is False
    message = recon.render_message(
        session_name=SESSION,
        source_kind=recon.SOURCE_RESUME_AND_START,
        operation_id=record["operation_id"],
        resume_target=sl.WORKING,
        outcomes=outcomes,
    )
    assert "term-2:fresh/seed=truncated" in message["text"]


def test_members_excluded_at_the_boundary_are_not_reported_as_lost():
    record = {
        "operation_id": str(uuid.uuid4()),
        "session_name": SESSION,
        "resume_target": sl.WORKING,
        "members": [
            {
                "agent_id": "a" * 36,
                "role": roster.ROLE_WORKER,
                "included": False,
                "final_state": cohort.FINAL_EXCLUDED_HISTORICAL,
            }
        ],
    }
    assert recon.classify_members(record) == []


# ---------------------------------------------------------------------------
# exactly one wake
# ---------------------------------------------------------------------------


def test_resume_and_start_delivers_one_wake_and_a_retry_adopts_it():
    supervisor = _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    _bind(suffix="2")
    source = _stop_cohort()
    operation = _claim_resume(source)
    supervisor_id = supervisor["agent"]["agent_id"]

    async def _restore(member, _operation):
        if member["agent_id"] == supervisor_id:
            _rebind_supervisor(supervisor_id)
        return cohort_resume.MemberRestore(cohort_resume.OUTCOME_EXACT, "back")

    deliverer = _Deliverer()
    settled = _run(
        cohort_resume.execute_resume_and_start(
            cohort_resume.ResumeRequest(operation_id=operation["operation_id"], actor="colin"),
            restorer=_restore,
            waker=recon.make_waker(deliverer=deliverer),
        )
    )

    assert settled["state"] == cohort.STATE_SETTLED
    assert len(deliverer.calls) == 1
    control_id, text = deliverer.calls[0]
    assert control_id == cohort_resume.wake_id(operation["operation_id"])
    assert SESSION in text

    wake = recon.get_wake(operation["operation_id"])
    assert wake["delivery_state"] == recon.DELIVERY_DELIVERED
    assert wake["receipt_digest"]
    assert wake["message"]["text"] == text

    # A replay of the settled Resume neither re-composes nor re-sends.
    _run(
        cohort_resume.execute_resume_and_start(
            cohort_resume.ResumeRequest(operation_id=operation["operation_id"], actor="colin"),
            restorer=_restore,
            waker=recon.make_waker(deliverer=deliverer),
        )
    )
    assert len(deliverer.calls) == 1


def test_a_wake_that_did_not_land_leaves_the_resume_in_reconciliation():
    supervisor = _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    source = _stop_cohort()
    operation = _claim_resume(source)
    supervisor_id = supervisor["agent"]["agent_id"]

    async def _restore(member, _operation):
        _rebind_supervisor(supervisor_id)
        return cohort_resume.MemberRestore(cohort_resume.OUTCOME_EXACT, "back")

    deliverer = _Deliverer(outcome=REFUSED)
    result = _run(
        cohort_resume.execute_resume_and_start(
            cohort_resume.ResumeRequest(operation_id=operation["operation_id"], actor="colin"),
            restorer=_restore,
            waker=recon.make_waker(deliverer=deliverer),
        )
    )

    assert result["state"] == cohort.STATE_RECONCILIATION_REQUIRED
    wake = recon.get_wake(operation["operation_id"])
    assert wake["delivery_state"] == recon.DELIVERY_UNDELIVERED
    assert wake["receipt_digest"] is None

    # The retry reuses the same wake id and, this time, lands it.
    landing = _Deliverer()
    retried = _run(
        cohort_resume.execute_resume_retry(
            cohort_resume.ResumeRequest(operation_id=operation["operation_id"], actor="colin"),
            restorer=_restore,
            waker=recon.make_waker(deliverer=landing),
        )
    )
    assert retried["state"] == cohort.STATE_SETTLED
    assert landing.calls[0][0] == cohort_resume.wake_id(operation["operation_id"])
    assert recon.get_wake(operation["operation_id"])["delivery_state"] == recon.DELIVERY_DELIVERED


def test_a_delivered_wake_is_never_downgraded_by_a_later_attempt():
    supervisor = _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    source = _stop_cohort()
    operation = _claim_resume(source)
    _rebind_supervisor(supervisor["agent"]["agent_id"])
    record = cohort.get_operation(operation["operation_id"])
    identifier = cohort_resume.wake_id(operation["operation_id"])

    first = recon.deliver_reconciliation_wake(
        wake_id=identifier, record=record, deliverer=_Deliverer()
    )
    assert first["delivery_state"] == recon.DELIVERY_DELIVERED

    second = recon.deliver_reconciliation_wake(
        wake_id=identifier, record=record, deliverer=_Deliverer(outcome=REFUSED)
    )
    assert second["delivery_state"] == recon.DELIVERY_DELIVERED
    assert second["adopted"] is True


def test_the_wake_is_addressed_to_the_supervisors_current_incarnation():
    """An exact restore returns the same conversation on a *new* pane."""
    supervisor = _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    source = _stop_cohort()
    operation = _claim_resume(source)
    rebound = _rebind_supervisor(supervisor["agent"]["agent_id"], suffix="9")

    targets: list[dict] = []

    def _deliver(target, control_id, text):
        targets.append(target)
        return control_input_service.ControlInputResult(control_id=control_id, outcome=ACCEPTED)

    recon.deliver_reconciliation_wake(
        wake_id=cohort_resume.wake_id(operation["operation_id"]),
        record=cohort.get_operation(operation["operation_id"]),
        deliverer=_deliver,
    )

    assert targets[0]["terminal_id"] == "term-9"
    assert targets[0]["generation"] == rebound["incarnation"]["generation"]
    # ...and not the pre-stop pane the cohort snapshot recorded.
    assert targets[0]["terminal_id"] != "term-1"


def test_a_supervisor_with_no_live_pane_is_reported_undelivered_not_guessed():
    _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    source = _stop_cohort()
    operation = _claim_resume(source)

    def _never(target, control_id, text):  # pragma: no cover - must not run
        raise AssertionError("nothing may be typed when there is no live supervisor pane")

    result = recon.deliver_reconciliation_wake(
        wake_id=cohort_resume.wake_id(operation["operation_id"]),
        record=cohort.get_operation(operation["operation_id"]),
        deliverer=_never,
    )

    assert result["delivery_state"] == recon.DELIVERY_UNDELIVERED
    assert result["reason_code"] == "supervisor-pane-absent"


# ---------------------------------------------------------------------------
# Resume-paused sends nothing
# ---------------------------------------------------------------------------


def test_resume_paused_sends_zero_input():
    supervisor = _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    source = _stop_cohort()
    operation = _claim_resume(source, target=sl.PAUSED)
    supervisor_id = supervisor["agent"]["agent_id"]

    async def _restore(member, _operation):
        _rebind_supervisor(supervisor_id)
        return cohort_resume.MemberRestore(cohort_resume.OUTCOME_EXACT, "back")

    settled = _run(
        cohort_resume.execute_resume_paused(
            cohort_resume.ResumeRequest(operation_id=operation["operation_id"], actor="colin"),
            restorer=_restore,
        )
    )

    assert settled["state"] == cohort.STATE_SETTLED
    assert sl.describe(SESSION)["lifecycle"] == sl.PAUSED
    assert recon.get_wake(operation["operation_id"]) is None
    assert recon.list_wakes(SESSION) == []


def test_delivering_a_wake_for_a_paused_target_is_refused_outright():
    _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    source = _stop_cohort()
    operation = _claim_resume(source, target=sl.PAUSED)

    with pytest.raises(recon.SupervisorReconciliationInvalid, match="zero input"):
        recon.deliver_reconciliation_wake(
            wake_id=cohort_resume.wake_id(operation["operation_id"]),
            record=cohort.get_operation(operation["operation_id"]),
            deliverer=_Deliverer(),
        )


# ---------------------------------------------------------------------------
# paused -> working
# ---------------------------------------------------------------------------


def test_paused_to_working_wakes_once_and_a_replay_adopts():
    supervisor = _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    source = _stop_cohort()
    operation = _claim_resume(source, target=sl.PAUSED)
    supervisor_id = supervisor["agent"]["agent_id"]

    async def _restore(member, _operation):
        _rebind_supervisor(supervisor_id)
        return cohort_resume.MemberRestore(cohort_resume.OUTCOME_EXACT, "back")

    _run(
        cohort_resume.execute_resume_paused(
            cohort_resume.ResumeRequest(operation_id=operation["operation_id"], actor="colin"),
            restorer=_restore,
        )
    )
    sl.declare(SESSION, sl.WORKING, declared_by="colin")

    deliverer = _Deliverer()
    first = recon.wake_paused_to_working(
        SESSION, source_operation_id=operation["operation_id"], deliverer=deliverer
    )
    second = recon.wake_paused_to_working(
        SESSION, source_operation_id=operation["operation_id"], deliverer=deliverer
    )

    assert first["delivery_state"] == recon.DELIVERY_DELIVERED
    assert first["source_kind"] == recon.SOURCE_PAUSED_TO_WORKING
    assert first["wake_id"] == recon.paused_to_working_wake_id(operation["operation_id"])
    assert len(deliverer.calls) == 1
    assert second["adopted"] is True
    assert f"resumed to {sl.WORKING}" in first["message"]["text"]


def test_paused_to_working_refuses_a_session_that_is_not_declared_working():
    supervisor = _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    source = _stop_cohort()
    operation = _claim_resume(source, target=sl.PAUSED)

    async def _restore(member, _operation):
        _rebind_supervisor(supervisor["agent"]["agent_id"])
        return cohort_resume.MemberRestore(cohort_resume.OUTCOME_EXACT, "back")

    _run(
        cohort_resume.execute_resume_paused(
            cohort_resume.ResumeRequest(operation_id=operation["operation_id"], actor="colin"),
            restorer=_restore,
        )
    )

    with pytest.raises(recon.SupervisorReconciliationConflict, match="follows the declaration"):
        recon.wake_paused_to_working(
            SESSION, source_operation_id=operation["operation_id"], deliverer=_Deliverer()
        )


# ---------------------------------------------------------------------------
# M3-C integration: the operator route uses M3-D's authority by default
# ---------------------------------------------------------------------------


def test_the_operator_resume_route_reaches_m3d_by_default(monkeypatch):
    """M3-C's default waker resolves to M3-D without being handed one."""
    supervisor = _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    source = _stop_cohort()
    supervisor_id = supervisor["agent"]["agent_id"]

    async def _restore(member, _operation):
        _rebind_supervisor(supervisor_id)
        return cohort_resume.MemberRestore(cohort_resume.OUTCOME_EXACT, "back")

    deliverer = _Deliverer()
    monkeypatch.setattr(recon, "_deliver", deliverer)
    del source

    request = cohort_operations.OperatorRequest(session_name=SESSION, initiated_by="colin")
    result = _run(cohort_operations.resume_and_start(request, restorer=_restore))

    assert result["state"] == cohort.STATE_SETTLED
    assert len(deliverer.calls) == 1
    wake = recon.get_wake(request.operation_id)
    assert wake is not None and wake["delivery_state"] == recon.DELIVERY_DELIVERED


# ---------------------------------------------------------------------------
# a resend is the *same* message, not a fresh render (cond-0380 P1-2)
# ---------------------------------------------------------------------------


def test_a_resend_after_response_loss_delivers_the_persisted_bytes():
    """The claim is the message. Live facts move; the wake must not.

    A wake is claimed under one control id, and the delivering seam's
    at-most-once contract is keyed on that id. If a resend under the same id
    carried *different* bytes, the seam would either suppress the corrected
    text or deliver two different messages the supervisor cannot tell apart —
    and the durable ledger would describe neither.
    """
    supervisor = _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    worker = _bind(suffix="2")
    source = _stop_cohort()
    operation = _claim_resume(source)
    _rebind_supervisor(supervisor["agent"]["agent_id"])
    identifier = cohort_resume.wake_id(operation["operation_id"])

    for member in cohort.get_operation(operation["operation_id"])["members"]:
        if member["included"]:
            cohort.record_member_result(
                cohort.MemberResult(
                    operation_id=operation["operation_id"],
                    agent_id=member["agent_id"],
                    expected_result_revision=0,
                    final_state=cohort.FINAL_RESTORED_EXACT,
                    background_command_loss_risk=cohort.LOSS_NONE,
                )
            )

    # First attempt: the claim persists the message, delivery does not land.
    refused = _Deliverer(outcome=REFUSED)
    first = recon.deliver_reconciliation_wake(
        wake_id=identifier,
        record=cohort.get_operation(operation["operation_id"]),
        deliverer=refused,
    )
    assert first["delivery_state"] == recon.DELIVERY_UNDELIVERED
    claimed_text = first["message"]["text"]
    assert "exact=2" in claimed_text

    # Now a live fact moves underneath: one member is re-recorded, and the
    # fresh worker's seed appears. A re-render would say something different.
    cohort.record_member_result(
        cohort.MemberResult(
            operation_id=operation["operation_id"],
            agent_id=worker["agent"]["agent_id"],
            expected_result_revision=1,
            final_state=cohort.FINAL_RESTORED_FRESH,
            background_command_loss_risk=cohort.LOSS_NONE,
        )
    )
    moved = cohort.get_operation(operation["operation_id"])
    rerendered = recon.render_message(
        session_name=SESSION,
        source_kind=recon.SOURCE_RESUME_AND_START,
        operation_id=operation["operation_id"],
        resume_target=sl.WORKING,
        outcomes=recon.classify_members(moved),
    )["text"]
    assert rerendered != claimed_text  # the divergence this test exists for

    landing = _Deliverer()
    second = recon.deliver_reconciliation_wake(wake_id=identifier, record=moved, deliverer=landing)

    assert second["delivery_state"] == recon.DELIVERY_DELIVERED
    delivered_control_id, delivered_text = landing.calls[0]
    assert delivered_control_id == identifier
    # The resend is byte-identical to the claim, and the ledger describes
    # exactly what was sent.
    assert delivered_text == claimed_text
    assert delivered_text != rerendered
    stored = recon.get_wake(operation["operation_id"])
    assert stored["message"]["text"] == delivered_text
    assert stored["message_digest"] == occ.digest(stored["message"])


def test_the_persisted_digest_always_describes_the_delivered_bytes():
    supervisor = _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    source = _stop_cohort()
    operation = _claim_resume(source)
    _rebind_supervisor(supervisor["agent"]["agent_id"])
    deliverer = _Deliverer()

    recon.deliver_reconciliation_wake(
        wake_id=cohort_resume.wake_id(operation["operation_id"]),
        record=cohort.get_operation(operation["operation_id"]),
        deliverer=deliverer,
    )

    stored = recon.get_wake(operation["operation_id"])
    assert deliverer.calls[0][1] == stored["message"]["text"]
    assert stored["message_digest"] == occ.digest(stored["message"])
