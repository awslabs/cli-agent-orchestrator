"""M3-D HTTP surface: occurrences, safe drain, and supervisor wakes."""

from __future__ import annotations

import uuid

import pytest

from cli_agent_orchestrator.services import control_input_service
from cli_agent_orchestrator.services import stable_agent_roster as roster
from cli_agent_orchestrator.services import supervisor_drain as drain
from cli_agent_orchestrator.services import task_occurrence as occ
from cli_agent_orchestrator.services.control_input_contract import ACCEPTED

SESSION = "cao-api-m3d"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64


@pytest.fixture(autouse=True)
def db(isolated_memory_db):
    return isolated_memory_db


def _bind(suffix="1", role=roster.ROLE_WORKER):
    return roster.bind_generation(
        roster.BindingContract(
            agent_id=str(uuid.uuid4()),
            session_name=SESSION,
            role=role,
            profile_family="supervisor" if role == roster.ROLE_SUPERVISOR else "developer",
            harness="claude_code",
            native_session_id=f"api-native-{suffix}",
            acquisition_method="chosen_session_id",
            terminal_id=f"api-term-{suffix}",
            generation=str(uuid.uuid4()),
            pane_id=f"%9{suffix}",
            pane_pid=9100 + int(suffix),
            process_identity={"pid": 9100 + int(suffix), "start_marker": f"api-m-{suffix}"},
            execution_mode="native_tui",
            admitted=True,
        )
    )


def _open_body(agent_id, **overrides):
    body = {
        "task_occurrence_id": str(uuid.uuid4()),
        "agent_id": agent_id,
        "round_index": 0,
        "dispatch_digest": DIGEST_A,
        "incarnation_id": "inc-1",
        "terminal_id": "api-term-1",
        "generation": "gen-1",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# occurrence routes
# ---------------------------------------------------------------------------


def test_open_read_and_finalize_one_occurrence(client):
    worker = _bind()
    body = _open_body(worker["agent"]["agent_id"])

    opened = client.post(f"/sessions/{SESSION}/task-occurrences", json=body)
    assert opened.status_code == 200, opened.text
    occurrence_id = opened.json()["task_occurrence_id"]
    assert occurrence_id != body["incarnation_id"]
    assert occurrence_id != body["generation"]

    boundary = client.post(
        f"/task-occurrences/{occurrence_id}/boundary",
        json={
            "expected_revision": 0,
            "recorded_by": "worker",
            "report_digest": DIGEST_B,
            "seed": {
                "quality": "complete",
                "summary_digest": DIGEST_B,
                "artifacts": [
                    {
                        "artifact_id": "notes",
                        "kind": "markdown",
                        "reference": "/tmp/notes.md",
                        "content_digest": DIGEST_C,
                    }
                ],
            },
        },
    )
    assert boundary.status_code == 200, boundary.text

    finalized = client.post(
        f"/task-occurrences/{occurrence_id}/finalize",
        json={
            "expected_revision": boundary.json()["revision"],
            "disposition": "reported",
            "finalized_by": "supervisor",
        },
    )
    assert finalized.status_code == 200
    read = client.get(f"/task-occurrences/{occurrence_id}").json()
    assert read["state"] == "finalized"
    assert read["finalized"]["report_digest"] == DIGEST_B
    assert read["seed_verdict"]["sufficient_for_fresh_start"] is True


def test_a_second_open_occurrence_for_one_agent_is_a_conflict(client):
    worker = _bind()
    agent_id = worker["agent"]["agent_id"]
    first = client.post(f"/sessions/{SESSION}/task-occurrences", json=_open_body(agent_id))
    assert first.status_code == 200

    second = client.post(
        f"/sessions/{SESSION}/task-occurrences",
        json=_open_body(agent_id, round_index=1),
    )

    assert second.status_code == 409
    assert "one agent executes one task at a time" in second.json()["detail"]


def test_reopening_a_finalized_occurrence_is_refused(client):
    worker = _bind()
    body = _open_body(worker["agent"]["agent_id"])
    occurrence_id = client.post(f"/sessions/{SESSION}/task-occurrences", json=body).json()[
        "task_occurrence_id"
    ]
    client.post(
        f"/task-occurrences/{occurrence_id}/finalize",
        json={"expected_revision": 0, "disposition": "reported", "finalized_by": "supervisor"},
    )

    again = client.post(f"/sessions/{SESSION}/task-occurrences", json=body)

    assert again.status_code == 409
    assert "never reopens a finished one" in again.json()["detail"]


def test_agent_history_separates_the_live_round_from_finished_ones(client):
    worker = _bind()
    agent_id = worker["agent"]["agent_id"]
    first = client.post(f"/sessions/{SESSION}/task-occurrences", json=_open_body(agent_id)).json()[
        "task_occurrence_id"
    ]
    client.post(
        f"/task-occurrences/{first}/finalize",
        json={"expected_revision": 0, "disposition": "reported", "finalized_by": "supervisor"},
    )
    second = client.post(
        f"/sessions/{SESSION}/task-occurrences", json=_open_body(agent_id, round_index=1)
    ).json()["task_occurrence_id"]

    history = client.get(f"/sessions/{SESSION}/agents/{agent_id}/task-occurrences").json()

    assert history["open"]["task_occurrence_id"] == second
    assert [item["task_occurrence_id"] for item in history["finalized"]] == [first]


def test_the_occurrence_read_route_does_not_require_a_live_tmux_session(client):
    """A finished round outlives its pane; that is the whole point."""
    worker = _bind()
    occurrence_id = client.post(
        f"/sessions/{SESSION}/task-occurrences", json=_open_body(worker["agent"]["agent_id"])
    ).json()["task_occurrence_id"]

    # No tmux session named SESSION exists in this test process at all.
    assert client.get(f"/task-occurrences/{occurrence_id}").status_code == 200
    assert client.get(f"/sessions/{SESSION}/task-occurrences").status_code == 200


def test_a_seed_body_must_state_its_quality(client):
    worker = _bind()
    body = _open_body(worker["agent"]["agent_id"])
    body["seed"] = {"summary_digest": DIGEST_B}

    assert client.post(f"/sessions/{SESSION}/task-occurrences", json=body).status_code == 422


def test_unknown_fields_are_rejected_rather_than_ignored(client):
    worker = _bind()
    body = _open_body(worker["agent"]["agent_id"])
    body["seed_quality"] = "complete"  # not a field: a typo that would drop the seed

    assert client.post(f"/sessions/{SESSION}/task-occurrences", json=body).status_code == 422


# ---------------------------------------------------------------------------
# extensions
# ---------------------------------------------------------------------------


def test_an_unknown_extension_is_preserved_and_routed_never_redispatched(client):
    worker = _bind()
    occurrence_id = client.post(
        f"/sessions/{SESSION}/task-occurrences", json=_open_body(worker["agent"]["agent_id"])
    ).json()["task_occurrence_id"]

    attached = client.post(
        f"/task-occurrences/{occurrence_id}/extensions",
        json={
            "extension_id": "claim-1",
            "extension_kind": "future.completion-claim/v9",
            "extension_version": "9",
            "decider": "cao-conductor",
            "payload": {"verdict": "complete", "extra": {"nested": True}},
            "claims_final": True,
        },
    )
    assert attached.status_code == 200
    assert attached.json()["recognized"] is False
    assert attached.json()["payload"] == {"verdict": "complete", "extra": {"nested": True}}

    # A future completion claim does not close the round.
    blocked = client.post(
        f"/task-occurrences/{occurrence_id}/finalize",
        json={"expected_revision": 0, "disposition": "reported", "finalized_by": "supervisor"},
    )
    assert blocked.status_code == 409
    assert "awaiting their decider" in blocked.json()["detail"]

    pending = client.get(
        "/task-occurrence-extensions/pending", params={"decider": "cao-conductor"}
    ).json()
    assert [item["extension_id"] for item in pending["extensions"]] == ["claim-1"]

    routed = client.post(
        f"/task-occurrences/{occurrence_id}/extensions/claim-1/route",
        json={"routed_by": "supervisor"},
    )
    assert routed.status_code == 200
    assert routed.json()["routing_state"] == "routed"
    assert client.get("/task-occurrence-extensions/pending").json()["count"] == 0
    # Routing did not finalize the occurrence or alter the payload.
    after = client.get(f"/task-occurrences/{occurrence_id}").json()
    assert after["state"] == "open"
    assert after["extensions"][0]["payload"]["verdict"] == "complete"


# ---------------------------------------------------------------------------
# safe drain and the receipt boundary
# ---------------------------------------------------------------------------


def _patch_drain_seams(monkeypatch, *, observation):
    monkeypatch.setattr(drain, "_default_observer", lambda member, phase: observation)
    monkeypatch.setattr(
        drain,
        "_default_steerer",
        lambda member, control_id, text: control_input_service.ControlInputResult(
            control_id=control_id, outcome=ACCEPTED
        ),
    )


def _parked():
    return drain.DrainObservation(
        drain.OBS_PARKED, report_digest=DIGEST_B, boundary_digest=DIGEST_A
    )


def test_a_drain_that_proves_a_boundary_yields_a_spendable_receipt(client, monkeypatch):
    _bind()
    _patch_drain_seams(monkeypatch, observation=_parked())
    drain_id = str(uuid.uuid4())

    response = client.post(
        f"/sessions/{SESSION}/drain/safe",
        json={"drain_id": drain_id, "intent": "pause", "initiated_by": "colin"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "complete"
    read = client.get(f"/drains/{drain_id}").json()
    assert read["provenance"]["receipt_digest"] == response.json()["receipt_digest"]
    assert read["provenance"]["retryable"] is False


def test_an_unproven_drain_has_no_receipt_and_stays_retryable(client, monkeypatch):
    _bind()
    _patch_drain_seams(
        monkeypatch, observation=drain.DrainObservation(drain.OBS_ACTIVE, detail="still working")
    )
    drain_id = str(uuid.uuid4())

    response = client.post(
        f"/sessions/{SESSION}/drain/safe",
        json={"drain_id": drain_id, "intent": "pause", "initiated_by": "colin"},
    )

    assert response.json()["state"] == "reconciliation-required"
    assert response.json()["receipt_digest"] is None
    provenance = client.get(f"/drains/{drain_id}").json()["provenance"]
    assert provenance["retryable"] is True
    # The way out is explicit: retry the drain, or promote deliberately.
    assert provenance["force_promotion_receipt"]


def test_a_safe_pause_spends_a_complete_drain_and_refuses_an_unfinished_one(client, monkeypatch):
    _bind()
    _patch_drain_seams(
        monkeypatch, observation=drain.DrainObservation(drain.OBS_ACTIVE, detail="working")
    )
    unfinished = str(uuid.uuid4())
    client.post(
        f"/sessions/{SESSION}/drain/safe",
        json={"drain_id": unfinished, "intent": "pause", "initiated_by": "colin"},
    )

    refused = client.post(
        f"/sessions/{SESSION}/cohort/pause/safe-drained",
        json={
            "operation_id": str(uuid.uuid4()),
            "drain_id": unfinished,
            "initiated_by": "colin",
        },
    )
    assert refused.status_code == 409
    assert "has no receipt" in refused.json()["detail"]

    _patch_drain_seams(monkeypatch, observation=_parked())
    client.post(
        f"/sessions/{SESSION}/drain/safe",
        json={
            "drain_id": unfinished,
            "intent": "pause",
            "initiated_by": "colin",
            "retry": True,
        },
    )
    paused = client.post(
        f"/sessions/{SESSION}/cohort/pause/safe-drained",
        json={
            "operation_id": str(uuid.uuid4()),
            "drain_id": unfinished,
            "initiated_by": "colin",
        },
    )

    assert paused.status_code == 200, paused.text
    assert paused.json()["state"] == "paused"
    assert paused.json()["provenance"]["current_mode"] == "safe"
    assert paused.json()["provenance"]["promoted_to_force"] is False
    assert client.get(f"/sessions/{SESSION}/lifecycle").json()["lifecycle"] == "paused"


def test_a_stop_drain_receipt_cannot_be_spent_on_a_pause(client, monkeypatch):
    _bind()
    _patch_drain_seams(monkeypatch, observation=_parked())
    drain_id = str(uuid.uuid4())
    client.post(
        f"/sessions/{SESSION}/drain/safe",
        json={"drain_id": drain_id, "intent": "stop", "initiated_by": "colin"},
    )

    response = client.post(
        f"/sessions/{SESSION}/cohort/pause/safe-drained",
        json={"operation_id": str(uuid.uuid4()), "drain_id": drain_id, "initiated_by": "colin"},
    )

    assert response.status_code == 409
    assert "a pause spends a pause drain" in response.json()["detail"]


def test_a_drain_from_another_session_is_refused(client, monkeypatch):
    _bind()
    _patch_drain_seams(monkeypatch, observation=_parked())
    drain_id = str(uuid.uuid4())
    client.post(
        f"/sessions/{SESSION}/drain/safe",
        json={"drain_id": drain_id, "intent": "pause", "initiated_by": "colin"},
    )

    response = client.post(
        "/sessions/cao-somewhere-else/cohort/pause/safe-drained",
        json={"operation_id": str(uuid.uuid4()), "drain_id": drain_id, "initiated_by": "colin"},
    )

    assert response.status_code == 409
    assert "belongs to session" in response.json()["detail"]


# ---------------------------------------------------------------------------
# supervisor wakes
# ---------------------------------------------------------------------------


def test_a_session_with_no_wake_reads_as_an_empty_list_not_an_error(client):
    response = client.get(f"/sessions/{SESSION}/reconciliation-wakes")

    assert response.status_code == 200
    assert response.json() == {"wakes": [], "count": 0}


def test_an_operation_with_no_wake_is_a_typed_404(client):
    response = client.get(f"/cohort-operations/{uuid.uuid4()}/reconciliation-wake")

    assert response.status_code == 404
    assert "no supervisor reconciliation wake" in response.json()["detail"]


def test_unknown_reads_are_typed_not_generic_500s(client):
    assert client.get(f"/task-occurrences/{uuid.uuid4()}").status_code == 404
    assert client.get("/task-occurrences/not-a-uuid").status_code == 400
    assert client.get(f"/drains/{uuid.uuid4()}").status_code == 404


# ---------------------------------------------------------------------------
# a Pause receipt is not Stop evidence (cond-0380 P1-3)
# ---------------------------------------------------------------------------


def _complete_drain(client, monkeypatch, intent):
    _patch_drain_seams(monkeypatch, observation=_parked())
    drain_id = str(uuid.uuid4())
    response = client.post(
        f"/sessions/{SESSION}/drain/safe",
        json={"drain_id": drain_id, "intent": intent, "initiated_by": "colin"},
    )
    assert response.json()["state"] == "complete", response.text
    return response.json()


def test_a_pause_receipt_is_refused_as_safe_stop_evidence(client, monkeypatch):
    """The whole point of the split: the two drains prove different things.

    A Pause drain steers workers to a boundary and announces no teardown. A
    Stop drain additionally records CAO's intent to collect each pane *before*
    it disappears. Accepting the first as the second would let a safe Stop
    collect panes nobody announced — a force Stop wearing the safe label.
    """
    _bind()
    pause = _complete_drain(client, monkeypatch, "pause")

    response = client.post(
        f"/sessions/{SESSION}/cohort/stop/safe",
        json={
            "operation_id": str(uuid.uuid4()),
            "initiated_by": "colin",
            "drain_receipt_digest": pause["receipt_digest"],
            "acknowledged_one_way": True,
        },
    )

    assert response.status_code == 409
    assert "pause drain" in response.json()["detail"]


def test_an_invented_digest_is_refused_as_safe_stop_evidence(client, monkeypatch):
    _bind()

    response = client.post(
        f"/sessions/{SESSION}/cohort/stop/safe",
        json={
            "operation_id": str(uuid.uuid4()),
            "initiated_by": "colin",
            "drain_receipt_digest": "f" * 64,
            "acknowledged_one_way": True,
        },
    )

    assert response.status_code == 409
    assert "no complete stop drain" in response.json()["detail"]


def test_a_stop_receipt_carries_its_pre_teardown_intent(client, monkeypatch):
    _bind()
    stop = _complete_drain(client, monkeypatch, "stop")
    members = client.get(f"/drains/{stop['drain_id']}").json()["members"]

    assert [member["teardown_state"] for member in members] == ["requested"]
    assert all(member["teardown_request_id"] for member in members)


def test_a_stop_drain_whose_teardown_was_not_announced_cannot_be_spent(client, monkeypatch):
    """Belt and braces: the receipt is checked against the members it covers."""
    from cli_agent_orchestrator.services import supervisor_drain as drain_service

    _bind()
    stop = _complete_drain(client, monkeypatch, "stop")
    record = drain_service.get_drain(stop["drain_id"])
    member = record["members"][0]
    drain_service.record_member(
        stop["drain_id"],
        member["agent_id"],
        expected_revision=int(member["revision"]),
        observation=drain_service.DrainObservation(
            drain_service.OBS_PARKED, report_digest=DIGEST_B, boundary_digest=DIGEST_A
        ),
        member_state=drain_service.MEMBER_PARKED,
        steer_state=member["steer_state"],
        teardown_state=drain_service.TEARDOWN_UNPROVEN,
    )

    response = client.post(
        f"/sessions/{SESSION}/cohort/stop/safe",
        json={
            "operation_id": str(uuid.uuid4()),
            "initiated_by": "colin",
            "drain_receipt_digest": stop["receipt_digest"],
            "acknowledged_one_way": True,
        },
    )

    assert response.status_code == 409
    assert "teardown" in response.json()["detail"]


# ---------------------------------------------------------------------------
# a receipt binds the boundary it was earned at (cond-0380 P1-4)
# ---------------------------------------------------------------------------


def test_a_pause_receipt_is_refused_after_the_lifecycle_epoch_moves(client, monkeypatch):
    """A drain proves a boundary the fleet reached *then*.

    Spending it against a session that has since been declared something else
    would pause a fleet on classifications that describe a different moment.
    """
    from cli_agent_orchestrator.services import session_lifecycle as sl

    _bind()
    drain = _complete_drain(client, monkeypatch, "pause")
    sl.declare(SESSION, sl.WORKING, declared_by="colin")

    response = client.post(
        f"/sessions/{SESSION}/cohort/pause/safe-drained",
        json={
            "operation_id": str(uuid.uuid4()),
            "drain_id": drain["drain_id"],
            "initiated_by": "colin",
        },
    )

    assert response.status_code == 409
    assert "moved since" in response.json()["detail"] or "lifecycle" in response.json()["detail"]
    assert client.get(f"/sessions/{SESSION}/lifecycle").json()["lifecycle"] != "paused"


def test_a_pause_receipt_is_refused_after_the_roster_changes(client, monkeypatch):
    _bind(suffix="1")
    drain = _complete_drain(client, monkeypatch, "pause")
    _bind(suffix="2")  # a worker joined after the boundary was proven

    response = client.post(
        f"/sessions/{SESSION}/cohort/pause/safe-drained",
        json={
            "operation_id": str(uuid.uuid4()),
            "drain_id": drain["drain_id"],
            "initiated_by": "colin",
        },
    )

    assert response.status_code == 409
    assert client.get(f"/sessions/{SESSION}/lifecycle").json()["lifecycle"] != "paused"


def test_a_pause_receipt_is_refused_after_intervening_later_work(client, monkeypatch):
    """The roster does not move when a supervisor dispatches another round.

    Opening a task occurrence touches no stable-agent revision, so boundary
    binding alone cannot see it. Quiescence has to be re-proved against M3-D's
    own record at the moment the receipt is spent.
    """
    worker = _bind()
    agent_id = worker["agent"]["agent_id"]
    drain = _complete_drain(client, monkeypatch, "pause")

    opened = client.post(
        f"/sessions/{SESSION}/task-occurrences", json=_open_body(agent_id, round_index=7)
    )
    assert opened.status_code == 200, opened.text

    response = client.post(
        f"/sessions/{SESSION}/cohort/pause/safe-drained",
        json={
            "operation_id": str(uuid.uuid4()),
            "drain_id": drain["drain_id"],
            "initiated_by": "colin",
        },
    )

    assert response.status_code == 409
    assert "later work" in response.json()["detail"]
    assert client.get(f"/sessions/{SESSION}/lifecycle").json()["lifecycle"] != "paused"
    # And it never self-promotes: no force operation was created.
    operations = client.get(f"/sessions/{SESSION}/cohort-operations").json()["operations"]
    assert all(item["current_mode"] == "safe" for item in operations)


def test_a_pause_receipt_is_refused_when_the_drained_round_advanced(client, monkeypatch):
    """The member's own occurrence moved on: its boundary is no longer current."""
    worker = _bind()
    agent_id = worker["agent"]["agent_id"]
    opened = client.post(f"/sessions/{SESSION}/task-occurrences", json=_open_body(agent_id)).json()
    client.post(
        f"/task-occurrences/{opened['task_occurrence_id']}/boundary",
        json={"expected_revision": 0, "recorded_by": "worker", "report_digest": DIGEST_B},
    )

    # The real observer here: the pane is absent, so the member is positively
    # parked and its evidence comes from its own open occurrence.
    monkeypatch.setattr(
        drain,
        "_default_steerer",
        lambda member, control_id, text: control_input_service.ControlInputResult(
            control_id=control_id, outcome=ACCEPTED
        ),
    )
    drain_id = str(uuid.uuid4())
    ran = client.post(
        f"/sessions/{SESSION}/drain/safe",
        json={"drain_id": drain_id, "intent": "pause", "initiated_by": "colin"},
    )
    assert ran.json()["state"] == "complete", ran.text

    # The worker did more work after the boundary the drain recorded.
    client.post(
        f"/task-occurrences/{opened['task_occurrence_id']}/boundary",
        json={"expected_revision": 1, "recorded_by": "worker", "checkpoint_digest": DIGEST_C},
    )

    response = client.post(
        f"/sessions/{SESSION}/cohort/pause/safe-drained",
        json={
            "operation_id": str(uuid.uuid4()),
            "drain_id": drain_id,
            "initiated_by": "colin",
        },
    )

    assert response.status_code == 409
    assert "later work" in response.json()["detail"]


def test_a_current_receipt_still_pauses(client, monkeypatch):
    """The repair must not make the honest path unreachable."""
    _bind()
    drain = _complete_drain(client, monkeypatch, "pause")

    response = client.post(
        f"/sessions/{SESSION}/cohort/pause/safe-drained",
        json={
            "operation_id": str(uuid.uuid4()),
            "drain_id": drain["drain_id"],
            "initiated_by": "colin",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "paused"
    assert response.json()["provenance"]["current_mode"] == "safe"


def test_a_current_stop_receipt_still_stops(client, monkeypatch):
    """The repair must not make the honest safe Stop unreachable either."""
    from cli_agent_orchestrator.services import terminal_service

    _bind()
    stop = _complete_drain(client, monkeypatch, "stop")
    # Pane collection is the fork's; this test is about the receipt gate.
    monkeypatch.setattr(
        terminal_service, "delete_terminal", lambda terminal_id, registry=None, **kwargs: True
    )

    response = client.post(
        f"/sessions/{SESSION}/cohort/stop/safe",
        json={
            "operation_id": str(uuid.uuid4()),
            "initiated_by": "colin",
            "drain_receipt_digest": stop["receipt_digest"],
            "acknowledged_one_way": True,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "stopped"
    assert response.json()["provenance"]["current_mode"] == "safe"
    assert response.json()["provenance"]["promoted_to_force"] is False


def test_a_stop_receipt_is_refused_after_the_boundary_moves(client, monkeypatch):
    from cli_agent_orchestrator.services import session_lifecycle as sl

    _bind()
    stop = _complete_drain(client, monkeypatch, "stop")
    sl.declare(SESSION, sl.WORKING, declared_by="colin")

    response = client.post(
        f"/sessions/{SESSION}/cohort/stop/safe",
        json={
            "operation_id": str(uuid.uuid4()),
            "initiated_by": "colin",
            "drain_receipt_digest": stop["receipt_digest"],
            "acknowledged_one_way": True,
        },
    )

    assert response.status_code == 409
    assert "moved since" in response.json()["detail"]
    assert client.get(f"/sessions/{SESSION}/lifecycle").json()["lifecycle"] != "stopped"
