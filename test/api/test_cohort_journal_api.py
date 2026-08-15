"""Read-only C1 cohort projections on the lifecycle API router."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import OperationalError

from cli_agent_orchestrator.services import cohort_journal as cohort
from cli_agent_orchestrator.services import stable_agent_roster as roster

SESSION = "cao-api-cohort"


@pytest.fixture(autouse=True)
def db(isolated_memory_db):
    return isolated_memory_db


def _claim():
    roster.bind_generation(
        roster.BindingContract(
            agent_id=str(uuid.uuid4()),
            session_name=SESSION,
            role=roster.ROLE_SUPERVISOR,
            profile_family="supervisor",
            harness="claude_code",
            native_session_id="api-native",
            acquisition_method="chosen_session_id",
            terminal_id="api-term",
            generation=str(uuid.uuid4()),
            pane_id="%91",
            pane_pid=9191,
            process_identity={"pid": 9191, "start_marker": "api-marker"},
            execution_mode="native_tui",
            admitted=True,
        )
    )
    boundary = cohort.observe_boundary(SESSION)
    return cohort.claim_operation(
        cohort.OperationRequest(
            operation_id=str(uuid.uuid4()),
            session_name=SESSION,
            operation_kind=cohort.KIND_PAUSE,
            requested_mode=cohort.MODE_SAFE,
            initiator_kind=cohort.INITIATOR_OPERATOR,
            initiated_by="colin",
            lifecycle_epoch=boundary["lifecycle_epoch"],
            lifecycle_observation=boundary["lifecycle_observation"],
            roster_revision=boundary["roster_revision"],
            member_snapshot_digest=boundary["member_snapshot_digest"],
        )
    )


def test_exact_cohort_read_projects_members_and_transitions(client):
    operation = _claim()
    response = client.get(f"/cohort-operations/{operation['operation_id']}")

    assert response.status_code == 200
    assert response.json()["operation_id"] == operation["operation_id"]
    assert len(response.json()["members"]) == 1
    assert response.json()["transitions"] == []


def test_session_list_is_tmux_independent_and_normalises_the_name(client, monkeypatch):
    operation = _claim()

    def _tmux_is_forbidden(*_args, **_kwargs):
        raise AssertionError("cohort reads must not consult tmux")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.session_service.get_backend", _tmux_is_forbidden
    )
    response = client.get("/sessions/api-cohort/cohort-operations")

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["operations"][0]["operation_id"] == operation["operation_id"]


def test_unknown_or_malformed_operation_has_typed_http_status(client):
    missing = client.get(f"/cohort-operations/{uuid.uuid4()}")
    malformed = client.get("/cohort-operations/not-a-uuid")

    assert missing.status_code == 404
    assert malformed.status_code == 400


def test_unavailable_cohort_store_is_a_typed_503(client, monkeypatch):
    operation = _claim()

    def _locked_store():
        raise OperationalError("SELECT", {}, RuntimeError("database is locked"))

    monkeypatch.setattr(cohort.database, "SessionLocal", _locked_store)
    response = client.get(f"/cohort-operations/{operation['operation_id']}")

    assert response.status_code == 503
    assert "read failed" in response.json()["detail"]
