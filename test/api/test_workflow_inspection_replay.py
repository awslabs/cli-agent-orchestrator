"""U3 endpoint tests — inspection + event-replay read API (issue #504, FR-5).

Covers the two U3 read routes over a REAL durable journal (temp SQLite DB), not
mocks — the point is to prove journal-authoritative behaviour end-to-end:

- ``GET /workflows/runs/{run_id}`` returns the enriched ``RunInspection`` with
  per-step projections (FR-5.1); the response is a UNION SUPERSET of the pre-U3
  snapshot (BR-2) — the #505 fields (run_id, state, current_step_id, per-step
  id/state/attempts) are all still present.
- FALLBACK-FIRES (BR-1 / NFR-DUR-1): after ``run_registry.clear()`` (a simulated
  restart) the SAME inspect STILL serves from the journal. This is the
  silent-data-loss guard — if the journal fallback were removed, inspect would
  404 and this test goes RED (see the mutation note on the test).
- 404 on a never-acked run id (BR-7) and on a corrupt spec snapshot (BR-7).
- ``GET /workflows/runs/{run_id}/events`` replay cursor: ``?after_seq=n`` returns
  only seq > n in order (BR-3); a declared gap (append 1,2,4) travels alongside
  the events as a ``GapMarker`` (BR-4); ``after_seq`` beyond max -> empty page,
  not an error (BR-6).

The journal is pointed at a temp DB via the patched ``DATABASE_FILE`` and the
event migration memo is reset, mirroring ``test_workflow_journal_events.py``.
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
    # The registry is a process-global cache; start each test with it empty so a
    # prior test's live record never masks the journal-authoritative read path.
    monkeypatch.setattr(workflow_service, "run_registry", {})
    yield db_path
    workflow_journal._event_migrated_paths.clear()


def _seed_run(
    run_id: str = "r1",
    *,
    state: str = "running",
    step_state: str = "failed",
    spec_snapshot: str | None = None,
) -> None:
    """Seed a run + one step directly into the durable journal (no live record)."""
    workflow_journal.insert_run(
        run_id=run_id,
        workflow_name="wf",
        spec_snapshot=spec_snapshot if spec_snapshot is not None else _SPEC.model_dump_json(),
        inputs_json="{}",
        state=state,
        started_at="2026-07-27T00:00:00Z",
    )
    workflow_journal.insert_steps(run_id, [("s1", step_state)], "2026-07-27T00:00:00Z")
    workflow_journal.update_step(
        run_id,
        "s1",
        step_state,
        2,
        "2026-07-27T00:00:01Z",
        output_json='{"answer":42}',
        error="boom",
        error_kind="timeout",
    )


# ---------------------------------------------------------------------------
# Inspect (FR-5.1)
# ---------------------------------------------------------------------------
def test_inspect_returns_run_inspection_with_per_step_projections(client):
    _seed_run("r1")
    resp = client.get("/workflows/runs/r1")
    assert resp.status_code == 200
    body = resp.json()

    # Enriched run metadata (FR-5.1 additive fields).
    assert body["run_id"] == "r1"
    assert body["workflow_name"] == "wf"
    assert body["state"] == "running"
    assert body["started_at"] == "2026-07-27T00:00:00Z"
    assert body["finished_at"] is None
    assert body["tier"] == "yaml"

    # Per-step durable projection, keyed on ``id`` (the #505 field name).
    assert len(body["steps"]) == 1
    step = body["steps"][0]
    assert step["id"] == "s1"
    assert step["state"] == "failed"
    assert step["attempts"] == 2
    assert step["output_json"] == '{"answer":42}'
    assert step["error"] == "boom"
    assert step["error_kind"] == "timeout"


def test_inspect_response_is_a_superset_of_the_505_snapshot_fields(client):
    """BR-2 contract preservation: every field #505's status/result clients read
    (run_id, state, current_step_id; per-step id/state/attempts) is still present
    in the enriched response — the enrichment ADDS fields, never removes them."""
    _seed_run("r1")
    body = client.get("/workflows/runs/r1").json()
    for key in ("run_id", "state", "current_step_id", "steps"):
        assert key in body
    for key in ("id", "state", "attempts"):
        assert key in body["steps"][0]


def test_inspect_still_serves_from_journal_after_registry_cleared(client):
    """FALLBACK-FIRES (BR-1 / NFR-DUR-1) — the silent-data-loss guard.

    The run is seeded ONLY into the durable journal; ``run_registry`` is empty.
    First inspect must rebuild-from-journal and 200. To PROVE the read is
    journal-authoritative and not registry-served, we clear the registry AGAIN
    (dropping the cache the first read populated) and re-inspect: it must STILL
    200 with the same run.

    MUTATION that turns this RED: delete the journal-fallback in
    ``workflow_service.get_run_status`` (the ``_rebuild_record_from_journal``
    branch on a ``run_registry`` cache miss). With the registry empty, inspect
    would then raise ``KeyError`` -> 404 and both assertions below fail. This is
    the union/fallback lesson: the journal source must answer where the live
    cache is empty, never be silently replaced or swallowed.
    """
    _seed_run("r1")

    # run_registry is empty (the fixture reset it) -> first read MUST hit the
    # journal fallback.
    assert workflow_service.run_registry == {}
    first = client.get("/workflows/runs/r1")
    assert first.status_code == 200
    assert first.json()["run_id"] == "r1"

    # Simulate another restart: drop whatever the first read cached, re-inspect.
    workflow_service.run_registry.clear()
    second = client.get("/workflows/runs/r1")
    assert second.status_code == 200
    assert second.json()["workflow_name"] == "wf"
    assert second.json()["steps"][0]["state"] == "failed"


def test_inspect_unknown_run_404(client):
    resp = client.get("/workflows/runs/ghost")
    assert resp.status_code == 404


def test_inspect_corrupt_snapshot_404(client):
    """BR-7: a run whose spec snapshot is unparseable degrades to 404 (the
    rebuild returns None), matching the existing get_run_status behaviour."""
    _seed_run("bad", spec_snapshot="{not valid json")
    resp = client.get("/workflows/runs/bad")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Event-timeline replay (FR-5.2)
# ---------------------------------------------------------------------------
def _append(run_id: str, *seqs: int) -> None:
    for seq in seqs:
        workflow_journal.append_event(
            run_id, seq, "step.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
        )


def test_events_returns_full_ordered_timeline(client):
    _append("r1", 1, 2, 3)
    body = client.get("/workflows/runs/r1/events").json()
    assert [e["seq"] for e in body["events"]] == [1, 2, 3]
    assert body["gaps"] == []
    assert body["next_after_seq"] == 3


def test_events_after_seq_cursor_returns_only_later_seqs_in_order(client):
    """BR-3: ?after_seq=n returns events with seq > n, seq-ordered, dedupe-free."""
    _append("r1", 1, 2, 3, 4)
    body = client.get("/workflows/runs/r1/events?after_seq=2").json()
    assert [e["seq"] for e in body["events"]] == [3, 4]
    assert body["next_after_seq"] == 4


def test_events_declared_gap_travels_with_events(client):
    """BR-4: append 1,2,4 (seq 3 swallowed) -> the GapMarker is returned ALONGSIDE
    the events; the sequence is never renumbered to hide the hole."""
    _append("r1", 1, 2, 4)
    body = client.get("/workflows/runs/r1/events").json()
    assert [e["seq"] for e in body["events"]] == [1, 2, 4]  # not renumbered to 1,2,3
    assert len(body["gaps"]) == 1
    gap = body["gaps"][0]
    assert gap["after_seq"] == 2
    assert gap["before_seq"] == 4
    assert gap["missing_count"] == 1
    assert gap["reason"] == "append_failed"
    assert body["next_after_seq"] == 4


def test_events_after_seq_beyond_max_returns_empty_page_not_error(client):
    """BR-6: a caught-up follower (after_seq past the max) gets an empty page,
    not a 4xx."""
    _append("r1", 1, 2, 3)
    resp = client.get("/workflows/runs/r1/events?after_seq=99")
    assert resp.status_code == 200
    body = resp.json()
    assert body["events"] == []
    assert body["gaps"] == []
    assert body["next_after_seq"] is None


def test_events_unknown_run_returns_empty_page_not_error(client):
    """An unknown run id has no events -> an empty page (BR-6 shape), not a 404:
    the replay read is journal-authoritative and a missing run reads as caught-up
    with nothing to deliver."""
    resp = client.get("/workflows/runs/never-existed/events")
    assert resp.status_code == 200
    assert resp.json() == {"events": [], "gaps": [], "next_after_seq": None}


def test_events_are_journal_authoritative_after_registry_cleared(client):
    """NFR-DUR-1: the event timeline is fully replayable from the durable journal
    with no live record — clearing run_registry does not affect the read."""
    _append("r1", 1, 2, 3)
    workflow_service.run_registry.clear()
    body = client.get("/workflows/runs/r1/events").json()
    assert [e["seq"] for e in body["events"]] == [1, 2, 3]
