"""Physical M3-C Pause/Stop executor tests (cond-0379 C3)."""

from __future__ import annotations

import threading
import uuid

import pytest

from cli_agent_orchestrator.services import cohort_effects as effects
from cli_agent_orchestrator.services import cohort_journal as cohort
from cli_agent_orchestrator.services import operation_journal as oj
from cli_agent_orchestrator.services import session_lifecycle as sl
from cli_agent_orchestrator.services import stable_agent_roster as roster
from cli_agent_orchestrator.services.control_input_contract import ACCEPTED
from cli_agent_orchestrator.services.control_input_service import ControlInputResult

SESSION = "cao-cohort-effects"
DIGEST = "a" * 64


@pytest.fixture(autouse=True)
def _db(isolated_memory_db, monkeypatch, tmp_path):
    from cli_agent_orchestrator import constants

    monkeypatch.setattr(constants, "COMPANION_DIR", tmp_path / "companion")
    return isolated_memory_db


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
            pane_pid=5000 + int(suffix),
            process_identity={"pid": 5000 + int(suffix), "start_marker": f"m-{suffix}"},
            execution_mode="native_tui",
            admitted=True,
        )
    )


def _claim(kind: str, mode: str):
    boundary = cohort.observe_boundary(SESSION)
    return cohort.claim_operation(
        cohort.OperationRequest(
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
    )


def _transition(operation, state):
    return cohort.transition_operation(
        cohort.TransitionRequest(
            transition_id=str(uuid.uuid4()),
            operation_id=operation["operation_id"],
            expected_state_epoch=operation["state_epoch"],
            to_state=state,
            actor="colin",
        )
    )["operation"]


def test_safe_pause_consumes_opaque_member_evidence_without_force_effects():
    _bind(suffix="1")
    operation = _transition(_claim(cohort.KIND_PAUSE, cohort.MODE_SAFE), cohort.STATE_DRAINING)
    member = cohort.get_operation(operation["operation_id"])["members"][0]
    result = cohort.MemberResult(
        operation_id=operation["operation_id"],
        agent_id=member["agent_id"],
        expected_result_revision=0,
        final_state=cohort.FINAL_DRAINED,
        background_command_loss_risk=cohort.LOSS_NONE,
        task_occurrence_id="opaque-occurrence",
        boundary_digest="b" * 64,
    )

    stored = effects.execute_safe_pause(
        effects.SafePauseRequest(
            operation_id=operation["operation_id"],
            commit_transition_id=str(uuid.uuid4()),
            actor="colin",
            drain_receipt_digest=DIGEST,
            member_results=(result,),
        )
    )

    assert stored["state"] == cohort.STATE_PAUSED
    assert sl.describe(SESSION)["lifecycle"] == sl.PAUSED
    member = cohort.get_operation(operation["operation_id"])["members"][0]
    assert member["task_occurrence_id"] == "opaque-occurrence"
    assert member["interrupt_action"] is None


def test_force_pause_workers_first_and_commits_only_after_child_clear(monkeypatch):
    worker = _bind(suffix="1")
    supervisor = _bind(suffix="2", role=roster.ROLE_SUPERVISOR)
    operation = _claim(cohort.KIND_PAUSE, cohort.MODE_FORCE)
    calls = []
    monkeypatch.setattr(
        effects,
        "_interrupt_events",
        lambda _member: [{"type": "key", "key": "Escape"}],
    )

    def observe(member, phase):
        if phase == "before":
            return effects.PauseObservation(effects.OBS_ACTIVE, False)
        return effects.PauseObservation(effects.OBS_INTERRUPTED, True, "child exited")

    def interrupt(member, _events, control_id):
        calls.append((member["agent_id"], control_id))
        return ControlInputResult(control_id=control_id, outcome=ACCEPTED, detail="sent")

    stored = effects.execute_force_pause(
        effects.ForcePauseRequest(
            operation_id=operation["operation_id"],
            expected_state_epoch=0,
            interrupt_transition_id=str(uuid.uuid4()),
            commit_transition_id=str(uuid.uuid4()),
            reconciliation_transition_id=str(uuid.uuid4()),
            actor="colin",
        ),
        observer=observe,
        interrupt=interrupt,
    )

    assert [call[0] for call in calls] == [
        worker["agent"]["agent_id"],
        supervisor["agent"]["agent_id"],
    ]
    assert stored["state"] == cohort.STATE_PAUSED
    assert sl.describe(SESSION)["lifecycle"] == sl.PAUSED


def test_force_pause_turn_abort_with_surviving_child_never_false_pauses(monkeypatch):
    _bind(suffix="1")
    operation = _claim(cohort.KIND_PAUSE, cohort.MODE_FORCE)
    monkeypatch.setattr(
        effects,
        "_interrupt_events",
        lambda _member: [{"type": "key", "key": "Escape"}],
    )

    def observe(_member, phase):
        return effects.PauseObservation(
            effects.OBS_ACTIVE if phase == "before" else effects.OBS_INTERRUPTED,
            False,
            "tracked sleep child survived",
        )

    def interrupt(_member, _events, control_id):
        return ControlInputResult(control_id=control_id, outcome=ACCEPTED, detail="turn aborted")

    stored = effects.execute_force_pause(
        effects.ForcePauseRequest(
            operation_id=operation["operation_id"],
            expected_state_epoch=0,
            interrupt_transition_id=str(uuid.uuid4()),
            commit_transition_id=str(uuid.uuid4()),
            reconciliation_transition_id=str(uuid.uuid4()),
            actor="colin",
        ),
        observer=observe,
        interrupt=interrupt,
    )

    assert stored["state"] == cohort.STATE_RECONCILIATION_REQUIRED
    assert sl.describe(SESSION)["lifecycle"] == sl.WORKING
    member = cohort.get_operation(operation["operation_id"])["members"][0]
    assert member["final_state"] == cohort.FINAL_RECONCILIATION_REQUIRED
    assert member["interrupt_outcome"] == ACCEPTED


def test_force_pause_provider_exit_with_surviving_child_never_false_pauses(monkeypatch):
    _bind(suffix="1")
    operation = _claim(cohort.KIND_PAUSE, cohort.MODE_FORCE)
    monkeypatch.setattr(
        effects,
        "_interrupt_events",
        lambda _member: [{"type": "key", "key": "Escape"}],
    )

    def observe(_member, phase):
        if phase == "before":
            return effects.PauseObservation(effects.OBS_ACTIVE, False)
        return effects.PauseObservation(
            effects.OBS_EXITED, False, "provider exited; tracked task child survived"
        )

    stored = effects.execute_force_pause(
        effects.ForcePauseRequest(
            operation_id=operation["operation_id"],
            expected_state_epoch=0,
            interrupt_transition_id=str(uuid.uuid4()),
            commit_transition_id=str(uuid.uuid4()),
            reconciliation_transition_id=str(uuid.uuid4()),
            actor="colin",
        ),
        observer=observe,
        interrupt=lambda _member, _events, control_id: ControlInputResult(
            control_id=control_id, outcome=ACCEPTED, detail="turn aborted"
        ),
    )

    assert stored["state"] == cohort.STATE_RECONCILIATION_REQUIRED
    assert sl.describe(SESSION)["lifecycle"] == sl.WORKING


def test_force_pause_without_exact_build_control_never_guesses_an_interrupt(monkeypatch):
    _bind(suffix="1")
    operation = _claim(cohort.KIND_PAUSE, cohort.MODE_FORCE)
    monkeypatch.setattr(effects, "_interrupt_events", lambda _member: None)

    stored = effects.execute_force_pause(
        effects.ForcePauseRequest(
            operation_id=operation["operation_id"],
            expected_state_epoch=0,
            interrupt_transition_id=str(uuid.uuid4()),
            commit_transition_id=str(uuid.uuid4()),
            reconciliation_transition_id=str(uuid.uuid4()),
            actor="colin",
        ),
        observer=lambda _member, _phase: effects.PauseObservation(
            effects.OBS_ACTIVE, False, "tracked task remains active"
        ),
        interrupt=lambda *_args: pytest.fail("unproven provider control must not be sent"),
    )

    assert stored["state"] == cohort.STATE_RECONCILIATION_REQUIRED
    member = cohort.get_operation(operation["operation_id"])["members"][0]
    assert member["interrupt_action"] is None
    assert member["background_command_loss_risk"] == cohort.LOSS_UNKNOWN


def _stop_request(operation, **changes):
    values = {
        "operation_id": operation["operation_id"],
        "expected_state_epoch": operation["state_epoch"],
        "teardown_transition_id": str(uuid.uuid4()),
        "commit_transition_id": str(uuid.uuid4()),
        "reconciliation_transition_id": str(uuid.uuid4()),
        "actor": "colin",
    }
    values.update(changes)
    return effects.StopEffectsRequest(**values)


def test_force_stop_claims_barrier_before_worker_and_supervisor_reap():
    worker = _bind(suffix="1")
    supervisor = _bind(suffix="2", role=roster.ROLE_SUPERVISOR)
    operation = _claim(cohort.KIND_STOP, cohort.MODE_FORCE)
    calls = []

    def interrupt_waits(session_name, operation_id):
        barrier = oj.get_session_barrier(SESSION)
        assert barrier["claimed_by"] == operation["operation_id"]
        calls.append("waits")
        assert session_name == SESSION
        assert operation_id == operation["operation_id"]
        return [{"registration_id": "wait-1", "state": "interrupted-by-stop"}]

    def reap(member):
        barrier = oj.get_session_barrier(SESSION)
        assert barrier["claimed_by"] == operation["operation_id"]
        calls.append(member["terminal_id"])
        return True

    stored = effects.execute_stop(
        _stop_request(operation), reaper=reap, wait_interruptor=interrupt_waits
    )

    assert calls == [
        "waits",
        worker["incarnation"]["terminal_id"],
        supervisor["incarnation"]["terminal_id"],
    ]
    assert stored["state"] == cohort.STATE_STOPPED
    assert sl.describe(SESSION)["lifecycle"] == sl.STOPPED


def test_safe_stop_with_undrained_m3b_operation_has_zero_reap_effects(monkeypatch):
    _bind(suffix="1")
    operation = _transition(_claim(cohort.KIND_STOP, cohort.MODE_SAFE), cohort.STATE_DRAINING)
    monkeypatch.setattr(
        effects,
        "_pending_reincarnations",
        lambda _session: [{"operation_id": "m3b-in-flight"}],
    )
    reaped = []

    stored = effects.execute_stop(
        _stop_request(operation, drain_receipt_digest=DIGEST),
        reaper=lambda member: reaped.append(member) or True,
        safe_operation_drainer=lambda _operation: False,
    )

    assert stored["state"] == cohort.STATE_RECONCILIATION_REQUIRED
    assert reaped == []
    assert sl.describe(SESSION)["lifecycle"] == sl.WORKING


def test_safe_stop_requires_durable_drain_truth_before_any_reap(monkeypatch):
    _bind(suffix="1")
    operation = _transition(_claim(cohort.KIND_STOP, cohort.MODE_SAFE), cohort.STATE_DRAINING)
    pending = {"operation_id": "m3b-still-pending"}
    monkeypatch.setattr(effects, "_pending_reincarnations", lambda _session: [pending])
    reaped = []

    stored = effects.execute_stop(
        _stop_request(operation, drain_receipt_digest=DIGEST),
        reaper=lambda member: reaped.append(member) or True,
        safe_operation_drainer=lambda _operation: True,
    )

    assert stored["state"] == cohort.STATE_RECONCILIATION_REQUIRED
    assert reaped == []


def test_reconciliation_response_loss_replay_does_not_begin_a_new_stop_attempt(monkeypatch):
    _bind(suffix="1")
    operation = _transition(_claim(cohort.KIND_STOP, cohort.MODE_SAFE), cohort.STATE_DRAINING)
    request = _stop_request(operation, drain_receipt_digest=DIGEST)
    monkeypatch.setattr(
        effects,
        "_pending_reincarnations",
        lambda _session: [{"operation_id": "m3b-in-flight"}],
    )
    first = effects.execute_stop(
        request,
        reaper=lambda _member: pytest.fail("undrained safe Stop must not reap"),
        safe_operation_drainer=lambda _operation: False,
    )
    second = effects.execute_stop(
        request,
        reaper=lambda _member: pytest.fail("response-loss replay must not reap"),
        safe_operation_drainer=lambda _operation: pytest.fail(
            "response-loss replay must not start a new drain"
        ),
    )

    assert first["state"] == cohort.STATE_RECONCILIATION_REQUIRED
    assert second["operation_id"] == first["operation_id"]
    assert second["state_epoch"] == first["state_epoch"]


def test_force_stop_reaps_known_m3b_successor_without_waiting(monkeypatch):
    _bind(suffix="1")
    operation = _claim(cohort.KIND_STOP, cohort.MODE_FORCE)
    reincarnation = {
        "operation_id": str(uuid.uuid4()),
        "session_name": SESSION,
        "successor_terminal_id": "deadbeef",
        "successor_generation": str(uuid.uuid4()),
        "result_state": oj.RESULT_PENDING,
    }
    monkeypatch.setattr(effects, "_pending_reincarnations", lambda _session: [reincarnation])
    calls = []
    reconciled = []

    def record_result(operation_id, state, *, detail, evidence):
        reconciled.append((operation_id, state, detail, evidence))
        return {"operation": {**reincarnation, "result_state": state}}

    monkeypatch.setattr(oj, "record_result", record_result)

    stored = effects.execute_stop(
        _stop_request(operation),
        reaper=lambda member: calls.append(member["terminal_id"]) or True,
        safe_operation_drainer=lambda _operation: pytest.fail("force Stop must not drain"),
    )

    assert calls == ["term-1", "deadbeef"]
    assert reconciled[0][0] == reincarnation["operation_id"]
    assert reconciled[0][1] == oj.RESULT_RECONCILIATION_REQUIRED
    assert reconciled[0][3]["stop_operation_id"] == operation["operation_id"]
    assert stored["state"] == cohort.STATE_STOPPED


def test_stopped_response_loss_replay_reaps_late_m3b_successor(monkeypatch):
    _bind(suffix="1")
    operation = _claim(cohort.KIND_STOP, cohort.MODE_FORCE)
    request = _stop_request(operation)
    monkeypatch.setattr(effects, "_pending_reincarnations", lambda _session: [])
    first = effects.execute_stop(request, reaper=lambda _member: True)
    assert first["state"] == cohort.STATE_STOPPED

    late = {
        "operation_id": str(uuid.uuid4()),
        "session_name": SESSION,
        "successor_terminal_id": "deadbeef",
        "successor_generation": str(uuid.uuid4()),
        "result_state": oj.RESULT_PENDING,
    }
    monkeypatch.setattr(effects, "_pending_reincarnations", lambda _session: [late])
    monkeypatch.setattr(
        oj,
        "record_result",
        lambda _operation_id, state, **_kwargs: {"operation": {**late, "result_state": state}},
    )
    reaped = []

    replay = effects.execute_stop(
        request, reaper=lambda member: reaped.append(dict(member)) or True
    )

    assert replay["state"] == cohort.STATE_STOPPED
    assert [item["terminal_id"] for item in reaped] == ["deadbeef"]
    assert reaped[0]["generation"] == late["successor_generation"]


def test_partial_force_stop_is_truthful_and_siblings_continue():
    _bind(suffix="1")
    _bind(suffix="2")
    operation = _claim(cohort.KIND_STOP, cohort.MODE_FORCE)

    def reap(member):
        if member["terminal_id"] == "term-1":
            raise RuntimeError("tmux unavailable")
        return True

    stored = effects.execute_stop(_stop_request(operation), reaper=reap)
    members = {
        m["terminal_id"]: m for m in cohort.get_operation(operation["operation_id"])["members"]
    }

    assert stored["state"] == cohort.STATE_RECONCILIATION_REQUIRED
    assert members["term-1"]["final_state"] == cohort.FINAL_RECONCILIATION_REQUIRED
    assert members["term-2"]["final_state"] == cohort.FINAL_STOPPED
    assert sl.describe(SESSION)["lifecycle"] == sl.WORKING


def test_stop_barrier_waits_for_prior_effect_and_refuses_every_later_effect():
    _bind(suffix="1")
    operation = _claim(cohort.KIND_STOP, cohort.MODE_FORCE)
    entered = threading.Event()
    release = threading.Event()
    stop_done = threading.Event()

    def prior_effect():
        with cohort.session_effect_admission(SESSION):
            entered.set()
            assert release.wait(timeout=5)

    def stop():
        assert entered.wait(timeout=5)
        cohort.begin_stop_teardown(
            cohort.StopTeardownRequest(
                transition_id=str(uuid.uuid4()),
                operation_id=operation["operation_id"],
                expected_state_epoch=0,
                actor="colin",
            )
        )
        stop_done.set()

    effect_thread = threading.Thread(target=prior_effect)
    stop_thread = threading.Thread(target=stop)
    effect_thread.start()
    stop_thread.start()
    assert entered.wait(timeout=5)
    assert not stop_done.wait(timeout=0.1)
    release.set()
    effect_thread.join(timeout=5)
    stop_thread.join(timeout=5)

    assert stop_done.is_set()
    with pytest.raises(cohort.SessionEffectRefused, match="Stop barrier is claimed"):
        with cohort.session_effect_admission(SESSION):
            pytest.fail("a post-Stop effect must never enter")


def test_session_effect_admission_refuses_stopped_lifecycle_without_barrier():
    sl.stop(SESSION, declared_by="colin")

    with pytest.raises(cohort.SessionEffectRefused, match="only an operator Resume"):
        with cohort.session_effect_admission(SESSION):
            pytest.fail("a stopped lifecycle must never admit an effect")


def test_session_effect_admission_refuses_unreadable_lifecycle(monkeypatch):
    monkeypatch.setattr(
        sl,
        "describe",
        lambda _session: {
            "session_name": SESSION,
            "lifecycle": sl.WORKING,
            "unreadable": "database unavailable",
        },
    )

    with pytest.raises(cohort.SessionEffectRefused, match="lifecycle is unreadable"):
        with cohort.session_effect_admission(SESSION):
            pytest.fail("an unreadable lifecycle must never admit an effect")
