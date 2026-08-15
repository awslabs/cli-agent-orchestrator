"""Public operator surfaces for fleet Pause/Stop/Resume (cond-0379 M3-C C4).

The contract these pin is mostly about *separation*: safe and force are
different routes, force needs its own acknowledgement, and Resume is
operator-only and never reachable as a side effect of anything else.
"""

from __future__ import annotations

import uuid

import pytest

from cli_agent_orchestrator.services import cohort_journal as cohort
from cli_agent_orchestrator.services import cohort_resume
from cli_agent_orchestrator.services import operation_journal as oj
from cli_agent_orchestrator.services import session_lifecycle as sl
from cli_agent_orchestrator.services import stable_agent_roster as roster

SESSION = "cao-api-cohort-ops"
DIGEST = "d" * 64


@pytest.fixture(autouse=True)
def db(isolated_memory_db, monkeypatch, tmp_path):
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
            pane_pid=7000 + int(suffix),
            process_identity={"pid": 7000 + int(suffix), "start_marker": f"m-{suffix}"},
            execution_mode="native_tui",
            admitted=True,
        )
    )


def _body(**overrides):
    body = {"operation_id": str(uuid.uuid4()), "initiated_by": "colin"}
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# safe and force are separate routes with separate gates
# ---------------------------------------------------------------------------


def test_force_pause_requires_its_own_acknowledgement(client):
    _bind(suffix="1")
    response = client.post(
        f"/sessions/{SESSION}/cohort/pause/force",
        json=_body(acknowledged_interrupt=False),
    )

    assert response.status_code == 400
    assert "acknowledged_interrupt" in response.json()["detail"]
    # Nothing was claimed: a refused acknowledgement is a zero-effect refusal.
    assert cohort.list_operations(SESSION) == []


def test_force_stop_needs_both_acknowledgements(client):
    _bind(suffix="1")

    one_way_only = client.post(
        f"/sessions/{SESSION}/cohort/stop/force",
        json=_body(acknowledged_one_way=True, acknowledged_force=False),
    )
    force_only = client.post(
        f"/sessions/{SESSION}/cohort/stop/force",
        json=_body(acknowledged_one_way=False, acknowledged_force=True),
    )

    assert one_way_only.status_code == 400
    assert "acknowledged_force" in one_way_only.json()["detail"]
    assert force_only.status_code == 400
    assert "acknowledged_one_way" in force_only.json()["detail"]
    assert cohort.list_operations(SESSION) == []


def test_safe_stop_requires_the_m3d_drain_receipt(client):
    _bind(suffix="1")
    response = client.post(
        f"/sessions/{SESSION}/cohort/stop/safe",
        json=_body(acknowledged_one_way=True),
    )

    # A missing receipt is a schema refusal, not a silent downgrade to force.
    assert response.status_code == 422


def test_the_safe_routes_cannot_be_asked_for_force(client):
    """There is no mode field to set, on any of the four."""
    _bind(suffix="1")
    response = client.post(
        f"/sessions/{SESSION}/cohort/pause/safe",
        json=_body(drain_receipt_digest=DIGEST, members=[], mode="force"),
    )

    assert response.status_code == 422


def test_an_unknown_field_is_refused_rather_than_ignored(client):
    _bind(suffix="1")
    response = client.post(
        f"/sessions/{SESSION}/cohort/pause/force",
        json=_body(acknowledged_interrupt=True, expected_epoch=3),
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# safe Pause carries M3-D's evidence and decides nothing itself
# ---------------------------------------------------------------------------


def test_safe_pause_records_m3d_member_evidence_verbatim(client):
    agent = _bind(suffix="1")["agent"]["agent_id"]
    body = _body(
        drain_receipt_digest=DIGEST,
        members=[
            {
                "agent_id": agent,
                "expected_result_revision": 0,
                "final_state": "drained",
                "background_command_loss_risk": "none",
                "task_occurrence_id": "m3d-opaque-occurrence",
                "boundary_digest": "b" * 64,
            }
        ],
    )

    response = client.post(f"/sessions/{SESSION}/cohort/pause/safe", json=body)

    assert response.status_code == 200
    assert response.json()["state"] == cohort.STATE_PAUSED
    assert sl.describe(SESSION)["lifecycle"] == sl.PAUSED
    member = cohort.get_operation(body["operation_id"])["members"][0]
    # Carried, not interpreted.
    assert member["task_occurrence_id"] == "m3d-opaque-occurrence"
    assert member["interrupt_action"] is None


def test_safe_pause_refuses_a_force_only_member_state(client):
    agent = _bind(suffix="1")["agent"]["agent_id"]
    body = _body(
        drain_receipt_digest=DIGEST,
        members=[
            {
                "agent_id": agent,
                "expected_result_revision": 0,
                "final_state": "interrupted",
                "background_command_loss_risk": "possible",
            }
        ],
    )

    response = client.post(f"/sessions/{SESSION}/cohort/pause/safe", json=body)

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# the durable projection
# ---------------------------------------------------------------------------


def test_the_projection_carries_the_full_operation_provenance(client):
    agent = _bind(suffix="1")["agent"]["agent_id"]
    body = _body(
        drain_receipt_digest=DIGEST,
        members=[
            {
                "agent_id": agent,
                "expected_result_revision": 0,
                "final_state": "drained",
                "background_command_loss_risk": "none",
            }
        ],
    )
    client.post(f"/sessions/{SESSION}/cohort/pause/safe", json=body)

    response = client.get(f"/cohort-operations/{body['operation_id']}")

    assert response.status_code == 200
    provenance = response.json()["provenance"]
    assert provenance["operation_id"] == body["operation_id"]
    assert provenance["operation_kind"] == cohort.KIND_PAUSE
    assert provenance["requested_mode"] == cohort.MODE_SAFE
    assert provenance["current_mode"] == cohort.MODE_SAFE
    assert provenance["promoted_to_force"] is False
    assert provenance["initiator_kind"] == cohort.INITIATOR_OPERATOR
    assert provenance["initiated_by"] == "colin"
    assert provenance["lifecycle_epoch"] == 0
    assert len(provenance["roster_revision"]) == 64
    assert provenance["member_outcomes"] == {"drained": 1}
    # Continuity keeps the three identities separate.
    continuity = provenance["continuity"][0]
    assert continuity["agent_id"] == agent
    assert continuity["native_session_id"] == "native-1"
    assert continuity["incarnation_id"] != continuity["agent_id"]
    assert continuity["lineage_id"] != continuity["incarnation_id"]


def test_a_promotion_is_visible_in_the_projection_with_its_receipt(client):
    _bind(suffix="1")
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
    drained = cohort.transition_operation(
        cohort.TransitionRequest(
            transition_id=str(uuid.uuid4()),
            operation_id=operation["operation_id"],
            expected_state_epoch=0,
            to_state=cohort.STATE_DRAINING,
            actor="colin",
        )
    )["operation"]
    cohort.transition_operation(
        cohort.TransitionRequest(
            transition_id=str(uuid.uuid4()),
            operation_id=operation["operation_id"],
            expected_state_epoch=int(drained["state_epoch"]),
            to_state=cohort.STATE_INTERRUPTING,
            actor="colin",
            promote_to_force=True,
            receipt_digest=DIGEST,
        )
    )

    provenance = client.get(f"/cohort-operations/{operation['operation_id']}").json()["provenance"]

    assert provenance["requested_mode"] == cohort.MODE_SAFE
    assert provenance["current_mode"] == cohort.MODE_FORCE
    assert provenance["promoted_to_force"] is True
    assert provenance["promotion_receipt_digest"] == DIGEST
    assert provenance["promoted_by"] == "colin"


def _boundary():
    boundary = cohort.observe_boundary(SESSION)
    return {
        "lifecycle_epoch": boundary["lifecycle_epoch"],
        "lifecycle_observation": boundary["lifecycle_observation"],
        "roster_revision": boundary["roster_revision"],
        "member_snapshot_digest": boundary["member_snapshot_digest"],
    }


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


def _stopped_fleet():
    """A session with a real terminally stopped cohort behind it."""
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
        if member["included"]:
            roster.retire_incarnation(
                terminal_id=member["terminal_id"],
                generation=member["generation"],
                reason="stopped",
            )
    return operation


def _restore_everything(monkeypatch, outcome=cohort_resume.OUTCOME_EXACT):
    async def _restore(_member, _operation):
        return cohort_resume.MemberRestore(outcome, "test restore")

    monkeypatch.setattr(cohort_resume, "_default_restorer", _restore)
    return _restore


def test_resume_paused_restores_the_fleet_and_types_nothing(client, monkeypatch):
    from cli_agent_orchestrator.services import control_input_service

    _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    _bind(suffix="2")
    _stopped_fleet()
    _restore_everything(monkeypatch)

    def _no_bytes(*_args, **_kwargs):
        raise AssertionError("Resume paused must not deliver a byte")

    monkeypatch.setattr(control_input_service, "deliver_control_input", _no_bytes)

    response = client.post(f"/sessions/{SESSION}/cohort/resume/paused", json=_body())

    assert response.status_code == 200
    assert response.json()["state"] == cohort.STATE_SETTLED
    assert sl.describe(SESSION)["lifecycle"] == sl.PAUSED
    assert oj.get_session_barrier(SESSION)["state"] == oj.BARRIER_OPEN


def test_resume_start_emits_exactly_one_wake_even_with_a_failed_member(client, monkeypatch):
    _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    _bind(suffix="2")
    _stopped_fleet()
    wakes = []

    async def _restore(member, _operation):
        if member["terminal_id"] == "term-2":
            return cohort_resume.MemberRestore(cohort_resume.OUTCOME_FAILED, "refused")
        return cohort_resume.MemberRestore(cohort_resume.OUTCOME_EXACT, "back")

    async def _waker(_operation, results, identifier):
        wakes.append((identifier, sorted(r["final_state"] for r in results)))
        return cohort_resume.SupervisorWake(True, receipt_digest=DIGEST)

    monkeypatch.setattr(cohort_resume, "_default_restorer", _restore)
    monkeypatch.setattr(cohort_resume, "_default_waker", _waker)

    body = _body()
    response = client.post(f"/sessions/{SESSION}/cohort/resume/start", json=body)

    assert response.status_code == 200
    assert response.json()["state"] == cohort.STATE_SETTLED
    assert sl.describe(SESSION)["lifecycle"] == sl.WORKING
    assert len(wakes) == 1
    assert wakes[0][1] == ["failed", "restored-exact"]

    # A duplicate response is retried by the operator's client; it must not
    # become a second wake.
    again = client.post(f"/sessions/{SESSION}/cohort/resume/start", json=body)
    assert again.status_code == 200
    assert len(wakes) == 1


def test_resume_refuses_a_session_that_was_never_stopped(client):
    _bind(suffix="1")

    response = client.post(f"/sessions/{SESSION}/cohort/resume/paused", json=_body())

    assert response.status_code == 409
    assert "no terminally stopped cohort" in response.json()["detail"]


def test_resume_start_refuses_a_session_that_was_paused_when_stopped(client, monkeypatch):
    _bind(suffix="1", role=roster.ROLE_SUPERVISOR)
    sl.declare(SESSION, sl.WORKING, declared_by="colin")
    sl.request_pause(SESSION, requested_by="colin")
    sl.settle_pause(SESSION, declared_by="supervisor")
    _stopped_fleet()
    assert sl.describe(SESSION)["restore_to"] == sl.PAUSED

    response = client.post(f"/sessions/{SESSION}/cohort/resume/start", json=_body())

    assert response.status_code == 409
    assert "resume it paused" in response.json()["detail"]
