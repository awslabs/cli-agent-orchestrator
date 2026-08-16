"""M3-D safe drain: exactly-once steering, boundary proof, and the receipt."""

from __future__ import annotations

import uuid

import pytest

from cli_agent_orchestrator.services import cohort_journal as cohort
from cli_agent_orchestrator.services import control_input_service
from cli_agent_orchestrator.services import stable_agent_roster as roster
from cli_agent_orchestrator.services import supervisor_drain as drain
from cli_agent_orchestrator.services import task_occurrence as occ
from cli_agent_orchestrator.services.control_input_contract import ACCEPTED, REFUSED

SESSION = "cao-m3d-drain"
_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64


@pytest.fixture(autouse=True)
def _db(isolated_memory_db, monkeypatch, tmp_path):
    from cli_agent_orchestrator import constants

    monkeypatch.setattr(constants, "COMPANION_DIR", tmp_path / "companion")
    return isolated_memory_db


def _bind(*, role=roster.ROLE_WORKER, suffix="1"):
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
            pane_pid=4000 + int(suffix),
            process_identity={"pid": 4000 + int(suffix), "start_marker": f"marker-{suffix}"},
            execution_mode="native_tui",
            admitted=True,
        )
    )


def _request(intent=drain.INTENT_PAUSE, drain_id=None):
    return drain.DrainRequest(
        drain_id=drain_id or str(uuid.uuid4()),
        session_name=SESSION,
        intent=intent,
        initiated_by="colin",
    )


def _accepted(control_id="c"):
    return control_input_service.ControlInputResult(control_id=control_id, outcome=ACCEPTED)


def _refused(control_id="c"):
    return control_input_service.ControlInputResult(
        control_id=control_id, outcome=REFUSED, reason_code="pane-busy", detail="pane busy"
    )


class _Observer:
    """A scripted observer: one answer before the steer, one after."""

    def __init__(self, before, after=None):
        self.before = before
        self.after = after if after is not None else before
        self.calls: list[tuple[str, str]] = []

    def __call__(self, member, phase):
        self.calls.append((member["agent_id"], phase))
        return self.before if phase == "before" else self.after


class _Steerer:
    def __init__(self, result=None):
        self.result = result or _accepted()
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, member, control_id, text):
        self.calls.append((member["agent_id"], control_id, text))
        return self.result


def _active():
    return drain.DrainObservation(drain.OBS_ACTIVE, detail="still working")


def _parked():
    return drain.DrainObservation(
        drain.OBS_PARKED,
        task_occurrence_id=str(uuid.uuid4()),
        report_digest=_DIGEST_A,
        checkpoint_digest=_DIGEST_B,
        boundary_digest=_DIGEST_A,
    )


def _idle_with_evidence():
    return drain.DrainObservation(
        drain.OBS_IDLE, report_digest=_DIGEST_A, boundary_digest=_DIGEST_A
    )


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def test_the_snapshot_binds_the_exact_lifecycle_and_roster_boundary():
    _bind(role=roster.ROLE_SUPERVISOR, suffix="1")
    _bind(suffix="2")
    boundary = cohort.observe_boundary(SESSION)

    record = drain.snapshot_drain(_request())

    assert record["lifecycle_epoch"] == boundary["lifecycle_epoch"]
    assert record["roster_revision"] == boundary["roster_revision"]
    assert record["snapshot_digest"] == boundary["member_snapshot_digest"]
    assert record["state"] == drain.STATE_PENDING
    assert record["receipt_digest"] is None
    assert len(record["members"]) == 2


def test_a_second_drain_at_the_same_boundary_is_refused_not_duplicated():
    _bind(suffix="1")
    drain.snapshot_drain(_request())

    with pytest.raises(drain.SupervisorDrainConflict, match="already has pause drain"):
        drain.snapshot_drain(_request())


def test_a_replayed_snapshot_adopts_the_durable_drain():
    _bind(suffix="1")
    drain_id = str(uuid.uuid4())
    first = drain.snapshot_drain(_request(drain_id=drain_id))
    again = drain.snapshot_drain(_request(drain_id=drain_id))

    assert again["adopted"] is True
    assert again["drain_id"] == first["drain_id"]


def test_a_drain_snapshot_excludes_agents_already_dormant_at_the_boundary():
    _bind(suffix="1")
    dormant = _bind(suffix="2")
    roster.retire_incarnation(
        terminal_id=dormant["incarnation"]["terminal_id"],
        generation=dormant["incarnation"]["generation"],
        reason="retired-before-drain",
    )

    record = drain.snapshot_drain(_request())

    assert [member["agent_id"] for member in record["members"]] != [dormant["agent"]["agent_id"]]
    assert dormant["agent"]["agent_id"] not in {m["agent_id"] for m in record["members"]}


# ---------------------------------------------------------------------------
# steering exactly once
# ---------------------------------------------------------------------------


def test_a_non_idle_worker_is_steered_exactly_once_with_a_derived_control_id():
    worker = _bind(suffix="1")
    request = _request()
    steerer = _Steerer()

    record = drain.execute_drain(request, observer=_Observer(_active(), _parked()), steerer=steerer)

    assert len(steerer.calls) == 1
    agent_id, control_id, text = steerer.calls[0]
    assert agent_id == worker["agent"]["agent_id"]
    assert control_id == drain.steer_control_id(request.drain_id, agent_id)
    assert request.drain_id in text
    assert record["state"] == drain.STATE_COMPLETE

    # A replay of the same drain re-reads the durable answer; it does not
    # steer the worker a second time.
    again = drain.execute_drain(request, observer=_Observer(_active()), steerer=steerer)
    assert len(steerer.calls) == 1
    assert again["state"] == drain.STATE_COMPLETE


def test_an_already_idle_worker_is_never_steered():
    _bind(suffix="1")
    steerer = _Steerer()

    record = drain.execute_drain(
        _request(), observer=_Observer(_idle_with_evidence()), steerer=steerer
    )

    assert steerer.calls == []
    states = {member["member_state"] for member in record["members"]}
    assert states == {drain.MEMBER_ALREADY_IDLE}
    assert record["state"] == drain.STATE_COMPLETE


def test_a_parked_worker_is_never_steered_and_settles_parked():
    _bind(suffix="1")
    steerer = _Steerer()

    record = drain.execute_drain(_request(), observer=_Observer(_parked()), steerer=steerer)

    assert steerer.calls == []
    assert record["members"][0]["member_state"] == drain.MEMBER_PARKED


def test_a_refused_steer_leaves_the_member_undecided_and_the_drain_unfinished():
    _bind(suffix="1")
    steerer = _Steerer(_refused())

    record = drain.execute_drain(_request(), observer=_Observer(_active()), steerer=steerer)

    assert record["state"] == drain.STATE_RECONCILIATION_REQUIRED
    assert record["receipt_digest"] is None
    assert record["members"][0]["steer_state"] == drain.STEER_REFUSED
    assert record["members"][0]["member_state"] == drain.MEMBER_RECONCILIATION_REQUIRED


# ---------------------------------------------------------------------------
# boundary proof: quiescence AND evidence
# ---------------------------------------------------------------------------


def test_quiescence_without_a_report_or_checkpoint_is_not_a_boundary():
    _bind(suffix="1")
    quiet_but_silent = drain.DrainObservation(drain.OBS_IDLE)

    record = drain.execute_drain(
        _request(), observer=_Observer(quiet_but_silent), steerer=_Steerer()
    )

    assert record["state"] == drain.STATE_RECONCILIATION_REQUIRED
    assert "no boundary report or checkpoint" in record["members"][0]["detail"]


def test_a_report_from_a_still_working_worker_is_not_a_boundary():
    _bind(suffix="1")
    working_with_report = drain.DrainObservation(
        drain.OBS_ACTIVE, report_digest=_DIGEST_A, boundary_digest=_DIGEST_A
    )

    record = drain.execute_drain(
        _request(),
        observer=_Observer(working_with_report, working_with_report),
        steerer=_Steerer(),
    )

    assert record["state"] == drain.STATE_RECONCILIATION_REQUIRED
    assert record["members"][0]["member_state"] == drain.MEMBER_RECONCILIATION_REQUIRED


def _patch_live_pane(monkeypatch, generation):
    """A live, generation-matching pane: the case a heuristic would misread."""
    monkeypatch.setattr(
        control_input_service,
        "resolve_control_identity",
        lambda terminal_id: type(
            "R",
            (),
            {
                "terminal_generation": generation,
                "pane_dead": False,
                "provider": "claude_code",
                "provider_version": None,
            },
        )(),
    )


def test_the_default_observer_never_infers_idleness_from_a_live_pane(monkeypatch):
    """The Codex canary: an aborted-looking TUI can still have a live child."""
    worker = _bind(suffix="1")
    _patch_live_pane(monkeypatch, worker["incarnation"]["generation"])

    record = drain.execute_drain(_request(), steerer=_Steerer())

    assert record["state"] == drain.STATE_RECONCILIATION_REQUIRED
    assert record["members"][0]["observed_state"] == drain.OBS_UNKNOWN


def test_the_default_observer_treats_no_open_occurrence_as_positive_parked(monkeypatch):
    """First-party proof, not a screen reading: M3-D's own task authority."""
    worker = _bind(suffix="1")
    agent_id = worker["agent"]["agent_id"]
    record = occ.open_occurrence(
        occ.OpenRequest(
            task_occurrence_id=str(uuid.uuid4()),
            session_name=SESSION,
            agent_id=agent_id,
            round_index=0,
            dispatch_digest=_DIGEST_A,
            incarnation=occ.EffectIncarnation(
                incarnation_id=worker["incarnation"]["incarnation_id"], terminal_id="term-1"
            ),
        )
    )
    boundary = occ.record_boundary(
        occ.BoundaryRecord(
            task_occurrence_id=record["task_occurrence_id"],
            expected_revision=0,
            recorded_by="worker",
            report_digest=_DIGEST_A,
            checkpoint_digest=_DIGEST_B,
        )
    )
    occ.finalize_occurrence(
        occ.FinalizeRequest(
            task_occurrence_id=record["task_occurrence_id"],
            expected_revision=boundary["revision"],
            disposition=occ.DISPOSITION_REPORTED,
            finalized_by="supervisor",
        )
    )
    _patch_live_pane(monkeypatch, worker["incarnation"]["generation"])
    steerer = _Steerer()

    result = drain.execute_drain(_request(), steerer=steerer)

    assert steerer.calls == []
    assert result["state"] == drain.STATE_COMPLETE
    assert result["members"][0]["member_state"] == drain.MEMBER_PARKED
    assert result["members"][0]["report_digest"] == _DIGEST_A


# ---------------------------------------------------------------------------
# ordering: supervisor last
# ---------------------------------------------------------------------------


def test_the_supervisor_is_observed_only_after_every_worker_is_decided():
    _bind(role=roster.ROLE_SUPERVISOR, suffix="1")
    _bind(suffix="2")
    supervisor_id = {
        agent["agent_id"]
        for agent in roster.list_agents(SESSION)
        if agent["role"] == roster.ROLE_SUPERVISOR
    }.pop()
    observer = _Observer(_active(), _parked())

    record = drain.execute_drain(_request(), observer=observer, steerer=_Steerer())

    # Every observation of a worker precedes the first observation of the
    # supervisor.
    supervisor_calls = [i for i, call in enumerate(observer.calls) if call[0] == supervisor_id]
    worker_calls = [i for i, call in enumerate(observer.calls) if call[0] != supervisor_id]
    assert supervisor_calls and worker_calls
    assert min(supervisor_calls) > max(worker_calls)
    assert record["state"] == drain.STATE_COMPLETE


def test_an_undecided_worker_leaves_the_supervisor_pending_rather_than_parked():
    _bind(role=roster.ROLE_SUPERVISOR, suffix="1")
    _bind(suffix="2")
    supervisor_id = {
        agent["agent_id"]
        for agent in roster.list_agents(SESSION)
        if agent["role"] == roster.ROLE_SUPERVISOR
    }.pop()
    observer = _Observer(_active(), _active())

    record = drain.execute_drain(_request(), observer=observer, steerer=_Steerer())

    assert record["state"] == drain.STATE_RECONCILIATION_REQUIRED
    supervisor = next(m for m in record["members"] if m["agent_id"] == supervisor_id)
    assert supervisor["member_state"] == drain.MEMBER_PENDING
    assert "drains last" in supervisor["detail"]
    assert supervisor_id not in [call[0] for call in observer.calls]


# ---------------------------------------------------------------------------
# stop: teardown requested before the pane disappears
# ---------------------------------------------------------------------------


def test_a_stop_drain_records_cao_teardown_before_it_has_a_receipt():
    _bind(suffix="1")
    announced: list[tuple[str, str]] = []

    def _requester(member, request_id):
        # The receipt does not exist yet at this point: the drain is still
        # pending, so no Stop can have consumed anything.
        current = drain.get_drain(member["drain_id"])
        assert current["receipt_digest"] is None
        announced.append((member["agent_id"], request_id))
        return True

    record = drain.execute_drain(
        _request(drain.INTENT_STOP),
        observer=_Observer(_parked()),
        steerer=_Steerer(),
        teardown_requester=_requester,
    )

    assert len(announced) == 1
    assert record["state"] == drain.STATE_COMPLETE
    assert record["members"][0]["teardown_state"] == drain.TEARDOWN_REQUESTED
    assert record["members"][0]["teardown_request_id"] == announced[0][1]
    assert record["receipt_digest"]


def test_a_stop_drain_whose_teardown_cannot_be_recorded_does_not_complete():
    _bind(suffix="1")

    record = drain.execute_drain(
        _request(drain.INTENT_STOP),
        observer=_Observer(_parked()),
        steerer=_Steerer(),
        teardown_requester=lambda member, request_id: False,
    )

    assert record["state"] == drain.STATE_RECONCILIATION_REQUIRED
    assert record["receipt_digest"] is None
    assert record["members"][0]["teardown_state"] == drain.TEARDOWN_UNPROVEN


def test_a_pause_drain_requests_no_teardown():
    _bind(suffix="1")

    record = drain.execute_drain(_request(), observer=_Observer(_parked()), steerer=_Steerer())

    assert record["members"][0]["teardown_state"] == drain.TEARDOWN_NOT_REQUIRED


# ---------------------------------------------------------------------------
# timeout, retry, force boundary
# ---------------------------------------------------------------------------


def test_an_unfinished_drain_is_not_retried_implicitly():
    _bind(suffix="1")
    request = _request()
    steerer = _Steerer()
    drain.execute_drain(request, observer=_Observer(_active(), _active()), steerer=steerer)
    assert len(steerer.calls) == 1

    again = drain.execute_drain(request, observer=_Observer(_active(), _parked()), steerer=steerer)

    # No second steer, and the durable answer is returned unchanged.
    assert len(steerer.calls) == 1
    assert again["state"] == drain.STATE_RECONCILIATION_REQUIRED


def test_an_explicit_retry_continues_the_same_drain_without_re_steering():
    _bind(suffix="1")
    request = _request()
    steerer = _Steerer()
    drain.execute_drain(request, observer=_Observer(_active(), _active()), steerer=steerer)

    retried = drain.execute_drain(
        request, observer=_Observer(_parked(), _parked()), steerer=steerer, retry=True
    )

    assert len(steerer.calls) == 1  # the steer already landed; it is not repeated
    assert retried["state"] == drain.STATE_COMPLETE
    assert retried["attempt"] == 1
    assert retried["receipt_digest"]


def test_a_retry_never_re_decides_a_member_that_already_reached_a_boundary():
    _bind(suffix="1")
    _bind(suffix="2")
    request = _request()
    calls: list[str] = []

    def _one_worker_only(member, phase):
        calls.append(member["agent_id"])
        return _parked() if member["terminal_id"] == "term-1" else _active()

    first = drain.execute_drain(request, observer=_one_worker_only, steerer=_Steerer())
    assert first["state"] == drain.STATE_RECONCILIATION_REQUIRED
    decided = [m for m in first["members"] if m["member_state"] == drain.MEMBER_PARKED]
    assert len(decided) == 1
    calls.clear()

    drain.execute_drain(request, observer=_Observer(_parked()), steerer=_Steerer(), retry=True)

    assert decided[0]["agent_id"] not in calls


def test_a_force_promotion_receipt_is_derived_from_the_stalled_drain_and_stable():
    _bind(suffix="1")
    request = _request()
    record = drain.execute_drain(
        request, observer=_Observer(_active(), _active()), steerer=_Steerer()
    )

    receipt = drain.force_promotion_receipt(record)
    assert receipt == drain.force_promotion_receipt(drain.get_drain(request.drain_id))
    assert drain.get_drain(request.drain_id)["provenance"]["force_promotion_receipt"] == receipt
    # A complete drain has nothing to promote past.
    assert record["state"] == drain.STATE_RECONCILIATION_REQUIRED


# ---------------------------------------------------------------------------
# the receipt
# ---------------------------------------------------------------------------


def test_only_a_complete_drain_carries_a_receipt_and_it_binds_the_boundary():
    _bind(suffix="1")
    request = _request()
    record = drain.execute_drain(request, observer=_Observer(_parked()), steerer=_Steerer())

    assert record["state"] == drain.STATE_COMPLETE
    assert record["receipt_digest"] == drain.receipt_digest(record)
    provenance = drain.get_drain(request.drain_id)["provenance"]
    assert provenance["receipt_digest"] == record["receipt_digest"]
    assert provenance["retryable"] is False
    assert provenance["force_promotion_receipt"] is None


def test_cohort_member_results_refuse_an_incomplete_drain():
    _bind(suffix="1")
    request = _request()
    record = drain.execute_drain(
        request, observer=_Observer(_active(), _active()), steerer=_Steerer()
    )

    with pytest.raises(drain.SupervisorDrainConflict, match="only a complete drain"):
        drain.cohort_member_results(record, str(uuid.uuid4()), cohort_operation={"members": []})


def test_latest_complete_drain_only_returns_a_spendable_receipt():
    _bind(suffix="1")
    request = _request()
    drain.execute_drain(request, observer=_Observer(_active(), _active()), steerer=_Steerer())

    assert drain.latest_complete_drain(SESSION, drain.INTENT_PAUSE) is None

    drain.execute_drain(request, observer=_Observer(_parked()), steerer=_Steerer(), retry=True)
    found = drain.latest_complete_drain(SESSION, drain.INTENT_PAUSE)
    assert found is not None and found["drain_id"] == request.drain_id
