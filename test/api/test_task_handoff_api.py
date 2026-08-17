"""M3-E HTTP surface: the four boundaries of a reversible handback."""

from __future__ import annotations

import uuid

import pytest

from cli_agent_orchestrator.services import task_handoff as th
from cli_agent_orchestrator.services import task_occurrence as occ

SESSION = "cao-api-m3e"
DIGEST_A = "a" * 64
PACKET = "d" * 64
OBSERVED_AT = "2026-08-16T12:00:00Z"


@pytest.fixture(autouse=True)
def db(isolated_memory_db):
    return isolated_memory_db


def _open_occurrence(agent_id, *, suffix="1", round_index=0):
    return occ.open_occurrence(
        occ.OpenRequest(
            task_occurrence_id=str(uuid.uuid4()),
            session_name=SESSION,
            agent_id=agent_id,
            round_index=round_index,
            dispatch_digest=DIGEST_A,
            incarnation=occ.EffectIncarnation(
                incarnation_id=f"inc-{suffix}", terminal_id=f"term-{suffix}"
            ),
        )
    )


def _evidence(suffix="1", **overrides):
    body = {
        "incarnation_id": f"inc-{suffix}",
        "terminal_id": f"term-{suffix}",
        "turn_state": "terminal",
        "observed_at": OBSERVED_AT,
    }
    body.update(overrides)
    return body


def _begin_body(occurrence_id, to_agent_id, **overrides):
    body = {
        "handoff_id": str(uuid.uuid4()),
        "task_occurrence_id": occurrence_id,
        "to_agent_id": to_agent_id,
        "packet_digest": PACKET,
        "evidence": _evidence(),
        "initiated_by": "supervisor",
        "expected_donor_revision": 0,
    }
    body.update(overrides)
    return body


def _pair(client):
    donor_agent = str(uuid.uuid4())
    recipient_agent = str(uuid.uuid4())
    donor = _open_occurrence(donor_agent)
    begun = client.post(
        f"/sessions/{SESSION}/task-handoffs",
        json=_begin_body(donor["task_occurrence_id"], recipient_agent),
    )
    assert begun.status_code == 200, begun.text
    return donor_agent, recipient_agent, donor, begun.json()


def _deliver(client, handoff, *, incarnation_id="inc-2", terminal_id="term-2"):
    return client.post(
        f"/task-handoffs/{handoff['handoff_id']}/packet-delivery",
        json={
            "delivered": True,
            "outcome": "accepted",
            "incarnation_id": incarnation_id,
            "terminal_id": terminal_id,
        },
    )


# ---------------------------------------------------------------------------
# the happy path, boundary by boundary
# ---------------------------------------------------------------------------


def test_a_handback_runs_begin_deliver_transfer(client):
    donor_agent, recipient_agent, donor, handoff = _pair(client)
    assert handoff["state"] == th.STATE_PENDING
    assert handoff["packet_control_id"]

    delivered = _deliver(client, handoff)
    assert delivered.status_code == 200, delivered.text
    assert delivered.json()["delivery_state"] == th.DELIVERY_DELIVERED

    transferred = client.post(
        f"/task-handoffs/{handoff['handoff_id']}/transfer",
        json={
            "expected_revision": 0,
            "completed_by": "supervisor",
            "incarnation_id": "inc-2",
            "terminal_id": "term-2",
        },
    )
    assert transferred.status_code == 200, transferred.text
    body = transferred.json()
    assert body["state"] == th.STATE_TRANSFERRED
    assert body["successor_occurrence"]["agent_id"] == recipient_agent
    assert occ.get_occurrence(donor["task_occurrence_id"])["state"] == occ.STATE_FINALIZED


def test_rollback_releases_and_reports_the_donor(client):
    donor_agent, recipient_agent, donor, handoff = _pair(client)
    rolled = client.post(
        f"/task-handoffs/{handoff['handoff_id']}/rollback",
        json={"rolled_back_by": "supervisor", "reason": "quota returned"},
    )
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["state"] == th.STATE_ROLLED_BACK
    assert rolled.json()["donor_restored"] is True
    assert occ.get_occurrence(donor["task_occurrence_id"])["state"] == occ.STATE_OPEN


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def test_reads_answer_who_is_held_and_why(client):
    donor_agent, recipient_agent, _donor, handoff = _pair(client)

    fetched = client.get(f"/task-handoffs/{handoff['handoff_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["handoff_id"] == handoff["handoff_id"]

    listed = client.get(f"/sessions/{SESSION}/task-handoffs")
    assert [item["handoff_id"] for item in listed.json()] == [handoff["handoff_id"]]
    assert client.get(f"/sessions/{SESSION}/task-handoffs?state=transferred").json() == []

    held = client.get(f"/agents/{donor_agent}/task-handoff-hold")
    assert held.status_code == 200
    assert held.json()["held"] is True
    assert held.json()["handoff"]["role"] == th.ROLE_DONOR

    free = client.get(f"/agents/{uuid.uuid4()}/task-handoff-hold")
    assert free.status_code == 200
    assert free.json()["held"] is False


def test_the_capability_route_is_not_shadowed_by_the_handoff_id_route(client):
    # ``/task-handoffs/capability`` must resolve as itself, not as a handoff id.
    response = client.get("/task-handoffs/capability")
    assert response.status_code == 200, response.text
    assert response.json()["automatic_producer"] is False


# ---------------------------------------------------------------------------
# typed refusals
# ---------------------------------------------------------------------------


def test_an_unknown_handoff_is_404_and_a_malformed_id_is_400(client):
    assert client.get(f"/task-handoffs/{uuid.uuid4()}").status_code == 404
    assert client.get("/task-handoffs/not-a-uuid").status_code == 400


def test_a_conflicting_begin_is_409(client):
    donor_agent, recipient_agent, donor, _handoff = _pair(client)
    again = client.post(
        f"/sessions/{SESSION}/task-handoffs",
        json=_begin_body(donor["task_occurrence_id"], str(uuid.uuid4())),
    )
    assert again.status_code == 409, again.text


def test_a_transfer_without_a_delivered_packet_is_409(client):
    _donor_agent, _recipient, _donor, handoff = _pair(client)
    response = client.post(
        f"/task-handoffs/{handoff['handoff_id']}/transfer",
        json={
            "expected_revision": 0,
            "completed_by": "supervisor",
            "incarnation_id": "inc-2",
            "terminal_id": "term-2",
        },
    )
    assert response.status_code == 409
    assert "catch-up packet" in response.json()["detail"]


def test_a_begin_names_the_donor_revision_its_packet_digest_describes(client):
    # cond-0439. The packet is digested before begin; if the donor moved in the
    # window, begin must refuse rather than anchor `donor_revision` at a
    # revision the packet never described. The caller's recovery is ordinary:
    # re-observe and retry with a fresh packet.
    donor_agent = str(uuid.uuid4())
    donor = _open_occurrence(donor_agent)
    occ.record_boundary(
        occ.BoundaryRecord(
            task_occurrence_id=donor["task_occurrence_id"],
            expected_revision=0,
            recorded_by="worker",
            report_digest=DIGEST_A,
        )
    )
    stale = client.post(
        f"/sessions/{SESSION}/task-handoffs",
        json=_begin_body(donor["task_occurrence_id"], str(uuid.uuid4()), expected_donor_revision=0),
    )
    assert stale.status_code == 409, stale.text
    assert "packet digest describes" in stale.json()["detail"]

    fresh = client.post(
        f"/sessions/{SESSION}/task-handoffs",
        json=_begin_body(donor["task_occurrence_id"], str(uuid.uuid4()), expected_donor_revision=1),
    )
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["donor_revision"] == 1


def test_an_active_turn_is_409_rather_than_a_silent_handback(client):
    donor_agent = str(uuid.uuid4())
    donor = _open_occurrence(donor_agent)
    response = client.post(
        f"/sessions/{SESSION}/task-handoffs",
        json=_begin_body(
            donor["task_occurrence_id"],
            str(uuid.uuid4()),
            evidence=_evidence(turn_state="active"),
        ),
    )
    assert response.status_code == 409
    assert "active managed turn" in response.json()["detail"]


def test_unknown_body_fields_are_rejected_rather_than_ignored(client):
    donor_agent = str(uuid.uuid4())
    donor = _open_occurrence(donor_agent)
    # cond-0439 note: this test used to pin that `expected_revision` on a begin
    # body was 422 -- which also pinned that a caller had NO way to pin the
    # revision its packet digest describes. `expected_donor_revision` is now a
    # first-class, required field (see the CAS test below), so the strict-body
    # pin moves to a genuinely unknown field: a dropped `expected_epoch` would
    # still turn a compare-and-swap into last-write-wins.
    body = _begin_body(donor["task_occurrence_id"], str(uuid.uuid4()))
    body["expected_epoch"] = 0
    assert client.post(f"/sessions/{SESSION}/task-handoffs", json=body).status_code == 422

    # The new CAS field is required: dropping it must not default to "unchecked".
    body = _begin_body(donor["task_occurrence_id"], str(uuid.uuid4()))
    del body["expected_donor_revision"]
    assert client.post(f"/sessions/{SESSION}/task-handoffs", json=body).status_code == 422

    # A dropped observed_at would let the server invent one, which would make a
    # caller's own retry hash differently and conflict with itself.
    body = _begin_body(donor["task_occurrence_id"], str(uuid.uuid4()))
    del body["evidence"]["observed_at"]
    assert client.post(f"/sessions/{SESSION}/task-handoffs", json=body).status_code == 422

    # A turn state outside the closed vocabulary is not a new kind of quiet.
    body = _begin_body(donor["task_occurrence_id"], str(uuid.uuid4()))
    body["evidence"]["turn_state"] = "probably-fine"
    assert client.post(f"/sessions/{SESSION}/task-handoffs", json=body).status_code == 422


def test_a_delivered_packet_must_name_the_incarnation_that_received_it(client):
    _donor_agent, _recipient, _donor, handoff = _pair(client)
    response = client.post(
        f"/task-handoffs/{handoff['handoff_id']}/packet-delivery",
        json={"delivered": True},
    )
    assert response.status_code == 400
    assert "recipient incarnation" in response.json()["detail"]


def test_the_transfer_route_accepts_no_native_session_id(client):
    """M3-E carries none, and the route must not be the exception.

    An ordinary field-name slip in the caller -- it holds both incarnations
    when it builds this body -- would otherwise stamp the donor's native
    session onto the recipient's occurrence, a durably false harness binding
    that every later reader of that provenance would trust.
    """
    _donor_agent, recipient_agent, _donor, handoff = _pair(client)
    _deliver(client, handoff)
    body = {
        "expected_revision": 0,
        "completed_by": "supervisor",
        "incarnation_id": "inc-2",
        "terminal_id": "term-2",
        "native_session_id": "native-of-the-donor",
    }
    assert (
        client.post(f"/task-handoffs/{handoff['handoff_id']}/transfer", json=body).status_code
        == 422
    )

    del body["native_session_id"]
    transferred = client.post(f"/task-handoffs/{handoff['handoff_id']}/transfer", json=body)
    assert transferred.status_code == 200, transferred.text
    assert transferred.json()["successor_occurrence"]["native_session_id"] is None


def test_every_route_declares_the_scope_its_effect_needs(client):
    # A declaration pin rather than an end-to-end 403: the four mutating routes
    # must never silently become readable, and losing a `_WRITE` default is a
    # one-character edit no other test in this file would notice.
    import inspect

    from cli_agent_orchestrator.api import task_handoff as api

    write_routes = {
        api.begin_task_handoff,
        api.record_task_handoff_delivery,
        api.complete_task_handoff,
        api.rollback_task_handoff,
    }
    read_routes = {
        api.get_task_handoff,
        api.list_task_handoffs,
        api.get_agent_handoff_hold,
        api.task_handoff_capability,
    }
    for endpoint in write_routes:
        default = inspect.signature(endpoint).parameters["_scopes"].default
        assert default is api._WRITE, endpoint.__name__
    for endpoint in read_routes:
        default = inspect.signature(endpoint).parameters["_scopes"].default
        assert default is api._READ, endpoint.__name__
