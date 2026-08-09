"""HTTP read/audit surface for the M3-A stable-agent roster (cond-0377).

All routes are read-only and must never crash on legacy, missing, corrupt,
or unknown-version rows.  The mutating seams (launch binding, admission,
teardown retirement) are covered by the service-level tests.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import stable_agent_roster as roster


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/roster-api.db")
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(roster.database, "SessionLocal", sessionmaker(bind=engine))
    return engine


def _bind_agent(**changes) -> dict:
    payload = {
        "agent_id": "11111111-1111-4111-8111-111111111111",
        "session_name": "cao-campaign",
        "role": roster.ROLE_WORKER,
        "profile_family": "developer",
        "harness": "claude_code",
        "native_session_id": "11111111-2222-4333-8444-555555555555",
        "acquisition_method": roster.ACQUISITION_CHOSEN_SESSION_ID,
        "route_provenance": {"provider_route": "anthropic"},
        "terminal_id": "a1b2c3d4",
        "generation": "00000000-0000-4000-8000-000000000001",
        "execution_mode": "native_tui",
    }
    payload.update(changes)
    return roster.bind_generation(roster.BindingContract(**payload))


def test_list_agents_empty_and_with_rows(client, db):
    response = client.get("/roster/agents")
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "cao-m3-roster-list-v1"
    assert body["agents"] == []

    bound = _bind_agent()
    scoped = client.get("/roster/agents", params={"session_name": "cao-campaign"})
    assert scoped.status_code == 200
    agents = scoped.json()["agents"]
    assert len(agents) == 1
    assert agents[0]["agent_id"] == bound["agent"]["agent_id"]
    assert agents[0]["role"] == roster.ROLE_WORKER

    other = client.get("/roster/agents", params={"session_name": "cao-nothing"})
    assert other.status_code == 200
    assert other.json()["agents"] == []


def test_get_agent_with_history(client, db):
    bound = _bind_agent()
    roster.retire_incarnation(
        terminal_id="a1b2c3d4",
        generation="00000000-0000-4000-8000-000000000001",
        reason="done",
    )
    fallback = _bind_agent(
        native_session_id="77777777-6666-4555-8444-333333333333",
        terminal_id="d4e5f607",
        generation="00000000-0000-4000-8000-000000000005",
    )
    response = client.get(f"/roster/agents/{bound['agent']['agent_id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "cao-m3-roster-agent-v1"
    assert len(body["lineages"]) == 2
    assert len(body["incarnations"]) == 2
    assert body["agent"]["current_lineage"]["lineage_id"] == fallback["lineage"]["lineage_id"]

    missing = client.get("/roster/agents/00000000-0000-4000-8000-0000000000ff")
    assert missing.status_code == 404
    assert "unknown stable agent" in missing.json()["detail"]


def test_get_incarnation_by_terminal(client, db):
    empty = client.get("/roster/terminals/a1b2c3d4")
    assert empty.status_code == 200
    assert empty.json()["incarnation"] is None

    _bind_agent()
    response = client.get("/roster/terminals/a1b2c3d4")
    assert response.status_code == 200
    incarnation = response.json()["incarnation"]
    assert incarnation["terminal_id"] == "a1b2c3d4"
    assert incarnation["disposition"] == roster.INCARNATION_BOUND


def test_audit_dry_run_reports_truthfully(client, db):
    from datetime import datetime, timezone

    from cli_agent_orchestrator.clients import database

    _bind_agent()
    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with database.SessionLocal() as session:
        session.add(
            database.StableAgentModel(
                agent_id="00000000-0000-4000-8000-0000000000dd",
                session_name="cao-legacy",
                role=roster.ROLE_WORKER,
                profile_family="developer",
                disposition=roster.DISPOSITION_IDENTITY_MISSING,
                resume_contract_version="unknown-version-0",
                revision=1,
                created_at=stamp,
                updated_at=stamp,
            )
        )
        session.add(
            database.StableAgentLineageModel(
                lineage_id="00000000-0000-4000-8000-0000000000ee",
                agent_id="00000000-0000-4000-8000-0000000000dd",
                harness="claude_code",
                native_session_id=None,
                route_provenance_json="{not json",
                lineage_origin=roster.LINEAGE_ORIGIN_INITIAL,
                created_at=stamp,
                updated_at=stamp,
            )
        )
        session.commit()

    response = client.get("/roster/audit")
    assert response.status_code == 200
    audit = response.json()
    assert audit["schema"] == "cao-m3-roster-audit-v1"
    assert audit["agents_total"] == 2
    assert audit["identity_missing_count"] == 1
    assert audit["problems_count"] >= 2
    kinds = {p["kind"] for p in audit["problems"]}
    assert "corrupt-route-provenance" in kinds
    assert "unknown-disposition" in kinds or "unknown-resume-contract" in kinds
    # The dry-run audit never mutates.
    assert roster.list_agents(session_name="cao-legacy")[0]["disposition"] == (
        roster.DISPOSITION_IDENTITY_MISSING
    )


def test_get_incarnation_by_exact_generation(client, db):
    gen1 = "00000000-0000-4000-8000-0000000000a1"
    gen2 = "00000000-0000-4000-8000-0000000000a2"
    first = _bind_agent(generation=gen1)
    roster.retire_incarnation(terminal_id="a1b2c3d4", generation=gen1, reason="done")
    # The same stable agent reincarnates on a new generation of the same
    # terminal id (M3-B passes the prior agent id explicitly).
    second = _bind_agent(generation=gen2, native_session_id="77777777-6666-4555-8444-333333333333")
    assert second["agent"]["agent_id"] == first["agent"]["agent_id"]

    exact = client.get("/roster/terminals/a1b2c3d4", params={"generation": gen1})
    assert exact.status_code == 200
    assert exact.json()["incarnation"]["generation"] == gen1
    assert exact.json()["incarnation"]["disposition"] == roster.INCARNATION_RETIRED

    live = client.get("/roster/terminals/a1b2c3d4")
    assert live.status_code == 200
    assert live.json()["incarnation"]["generation"] == gen2
