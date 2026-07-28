"""U7 DELETE-route tests — explicit per-run deletion (issue #504, FR-11 / NFR-SEC-5).

Exercises ``DELETE /workflows/runs/{run_id}`` over a REAL durable journal (temp
SQLite DB): a delete removes the run and all its retained data so the U3 inspect /
events reads return not-found/empty afterward (BR-SEC-5), and an unknown-id delete
is a well-defined no-op that never faults other reads (BR-3). The route-ordering pin
(this path resolves to the delete handler, not the ``/workflows/{name}`` catch-all)
lives in ``test_workflow_route_ordering.py``.

The journal points at a temp DB via the patched ``DATABASE_FILE`` and a clean
registry, mirroring ``test_workflow_inspection_replay.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent_orchestrator.models.workflow import WorkflowSpec, WorkflowStep
from cli_agent_orchestrator.services import workflow_journal, workflow_service

_SPEC = WorkflowSpec(
    name="wf",
    steps=[WorkflowStep(id="s1", provider="claude_code", agent="dev", prompt="go")],
)


@pytest.fixture(autouse=True)
def _isolated_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fresh temp journal DB + clean registry/migration memo for each test."""
    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    workflow_journal._event_migrated_paths.clear()
    monkeypatch.setattr(workflow_service, "run_registry", {})
    yield db_path
    workflow_journal._event_migrated_paths.clear()


def _seed_run(run_id: str = "r1") -> None:
    """Seed a run + step + one event directly into the durable journal."""
    workflow_journal.insert_run(
        run_id=run_id,
        workflow_name="wf",
        spec_snapshot=_SPEC.model_dump_json(),
        inputs_json="{}",
        state="completed",
        started_at="2026-07-27T00:00:00Z",
    )
    workflow_journal.insert_steps(run_id, [("s1", "completed")], "2026-07-27T00:00:00Z")
    workflow_journal.append_event(
        run_id, 1, "run.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
    )
    workflow_journal.persist_high_water(run_id, 1)


def test_delete_run_returns_204_and_removes_everything(client):
    """FR-11 / BR-SEC-5: delete -> 204, and inspect/events/steps all read empty after."""
    _seed_run("r1")
    # Precondition: the run is inspectable and has an event.
    assert client.get("/workflows/runs/r1").status_code == 200
    assert client.get("/workflows/runs/r1/events").json()["events"]

    resp = client.delete("/workflows/runs/r1")
    assert resp.status_code == 204
    assert resp.content == b""

    # Inspect now 404s; events read back empty; the DAL rows are all gone.
    assert client.get("/workflows/runs/r1").status_code == 404
    assert client.get("/workflows/runs/r1/events").json() == {
        "events": [],
        "gaps": [],
        "next_after_seq": None,
    }
    assert workflow_journal.get_run("r1") is None
    assert workflow_journal.get_steps("r1") == []
    assert workflow_journal.read_events("r1") == []


def test_delete_unknown_run_is_a_noop_204(client):
    """BR-3: deleting an unknown id is a well-defined no-op (204), never an error."""
    resp = client.delete("/workflows/runs/never-existed")
    assert resp.status_code == 204


def test_delete_unknown_run_does_not_fault_other_reads(client):
    """BR-3: an unknown-id delete leaves a co-existing run fully intact."""
    _seed_run("keep")
    assert client.delete("/workflows/runs/ghost").status_code == 204
    assert client.get("/workflows/runs/keep").status_code == 200


def test_delete_is_idempotent(client):
    """Deleting the same run twice is safe: the second call is the unknown-id no-op."""
    _seed_run("r1")
    assert client.delete("/workflows/runs/r1").status_code == 204
    assert client.delete("/workflows/runs/r1").status_code == 204


def test_delete_route_invokes_journal_delete_run(client, monkeypatch):
    """The route delegates to U1's delete_run (invoke, not reimplement) — spy it fires."""
    seen: list = []
    real = workflow_journal.delete_run

    def _spy(run_id):
        seen.append(run_id)
        return real(run_id)

    monkeypatch.setattr(workflow_journal, "delete_run", _spy)
    _seed_run("r1")
    client.delete("/workflows/runs/r1")
    assert seen == ["r1"]


# ---------------------------------------------------------------------------
# PR 526 review — MUST-FIX 3: DELETE must refuse a still-LIVE run (409).
#
# Before the fix the endpoint accepted ANY run id with no state check and evicted
# the live registry entry. The drive loop kept executing with nothing able to
# reach it: cancel returned 200 before the delete and failed after, and the
# loop's later appends landed as orphan event rows that neither a read path nor
# the retention sweep can reclaim (both join through the deleted run row).
# ---------------------------------------------------------------------------
def _seed_live_run(run_id: str = "r1", state: str = "running") -> None:
    """Seed a NON-terminal run (the zombie-creating case)."""
    workflow_journal.insert_run(
        run_id=run_id,
        workflow_name="wf",
        spec_snapshot=_SPEC.model_dump_json(),
        inputs_json="{}",
        state=state,
        started_at="2026-07-27T00:00:00Z",
    )
    workflow_journal.insert_steps(run_id, [("s1", "running")], "2026-07-27T00:00:00Z")
    workflow_journal.append_event(
        run_id, 1, "run.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
    )
    workflow_journal.persist_high_water(run_id, 1)


@pytest.mark.parametrize("state", ["running", "pending"])
def test_delete_rejects_a_non_terminal_run_with_409(client, state):
    """A live run is refused, and NOTHING is destroyed — the durable run row and
    its events both survive the rejected delete.

    (Inspect is asserted only for ``running``: a ``pending`` run legitimately
    404s on the inspect route for reasons unrelated to this guard, so asserting
    it for every state would test the wrong thing.)
    """
    _seed_live_run("r1", state)

    resp = client.delete("/workflows/runs/r1")
    assert resp.status_code == 409
    assert state in resp.json()["detail"]

    # The run survived intact: durable row still there, events still present.
    assert workflow_journal.get_run("r1") is not None
    assert client.get("/workflows/runs/r1/events").json()["events"]
    if state == "running":
        assert client.get("/workflows/runs/r1").status_code == 200


def test_delete_rejection_does_not_evict_the_registry_entry(client):
    """The zombie mechanism specifically: a rejected delete must NOT evict the
    live registry record, or the run becomes uncancellable while still running."""
    _seed_live_run("r1")
    sentinel = object()
    workflow_service.run_registry["r1"] = sentinel

    assert client.delete("/workflows/runs/r1").status_code == 409
    assert workflow_service.run_registry.get("r1") is sentinel


def test_delete_rejection_does_not_call_journal_delete_run(client, monkeypatch):
    """The cascade must not even be attempted for a live run."""
    called: list = []
    monkeypatch.setattr(workflow_journal, "delete_run", lambda rid: called.append(rid))
    _seed_live_run("r1")
    assert client.delete("/workflows/runs/r1").status_code == 409
    assert called == []


@pytest.mark.parametrize("state", ["completed", "failed", "cancelled"])
def test_delete_still_succeeds_for_every_terminal_state(client, state):
    """The guard is scoped to LIVE runs only — every terminal state still deletes."""
    _seed_live_run("r1", state)
    assert client.delete("/workflows/runs/r1").status_code == 204
    assert workflow_journal.get_run("r1") is None


def test_delete_of_unknown_id_stays_a_204_no_op(client):
    """Idempotency (BR-3) is preserved: the 409 applies to KNOWN live runs only,
    so an absent id is still a well-defined no-op, not a 409 or a 404."""
    assert client.delete("/workflows/runs/never-existed").status_code == 204


def test_cancel_then_delete_is_the_supported_sequence(client):
    """The documented path off the 409: cancel to a terminal state, then delete."""
    _seed_live_run("r1")
    assert client.delete("/workflows/runs/r1").status_code == 409
    workflow_journal.update_run_state("r1", "cancelled", "2026-07-27T00:00:05Z")
    assert client.delete("/workflows/runs/r1").status_code == 204
