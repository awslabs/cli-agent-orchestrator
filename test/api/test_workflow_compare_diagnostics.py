"""U6 endpoint tests — run comparison + diagnostic bundle (issue #504, FR-8, FR-9).

Covers the two U6 read-only export routes over a REAL durable journal (temp
SQLite DB), not mocks — the point is to prove journal-authoritative,
redaction-funnelled behaviour end-to-end:

- ``GET /workflows/runs/{run_id}/compare?against={other}``:
  * aligned steps surface per-side deltas (attempts / duration / provider /
    agent / validation / failure-retry) for both runs (FR-8.1);
  * a step present in only one run is ADDED / REMOVED, never silently dropped
    (BR-1);
  * output/artifact differences are reported at the ``output_ref`` REFERENCE
    level, never by diffing payloads (BR-2);
  * an unknown/deleted ``against`` (or baseline) id -> 404 for that side, not a
    partial silent compare (BR-8).
- ``GET /workflows/runs/{run_id}/diagnostics``:
  * the bundle contains EVERY FR-9.1 section (BR-3);
  * inputs are redacted through U7's ``sanitize_output`` — the audit_log
    cap-and-mark choke point — proven BOTH by a spy on
    ``audit_log._sanitize_for_log`` AND by the ``"[…truncated]"`` marker (BR-4);
  * with capture OFF (the default) the bundle carries NO output text — metadata +
    references only (BR-9);
  * references are terminal-id + offsets and artifact ``output_ref`` strings, not
    copies (BR-2);
  * **after ``run_registry.clear()`` the bundle STILL assembles from the durable
    journal (BR-6 / FR-9.2)** — the mutation-relevant guard: if the assembly
    depended on ``run_registry`` (rather than ``get_run``) the cleared registry
    would 404 and the test goes RED.

The journal is pointed at a temp DB via the patched ``DATABASE_FILE`` and the
event migration memo is reset, mirroring ``test_workflow_inspection_replay.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent_orchestrator.models.workflow import WorkflowSpec, WorkflowStep
from cli_agent_orchestrator.services import (
    audit_log,
    workflow_journal,
    workflow_retention,
    workflow_service,
)

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
    run_id: str,
    *,
    state: str = "completed",
    spec_snapshot: str | None = None,
    inputs_json: str = "{}",
    workflow_name: str = "wf",
) -> None:
    """Seed a run row directly into the durable journal (no live record)."""
    workflow_journal.insert_run(
        run_id=run_id,
        workflow_name=workflow_name,
        spec_snapshot=spec_snapshot if spec_snapshot is not None else _SPEC.model_dump_json(),
        inputs_json=inputs_json,
        state=state,
        started_at="2026-07-27T00:00:00Z",
    )


def _seed_step(
    run_id: str,
    step_id: str,
    *,
    state: str = "completed",
    attempts: int = 1,
    output_json: str | None = None,
    error: str | None = None,
    error_kind: str | None = None,
    reprompted: int | None = None,
) -> None:
    """Seed a step row; update_step projects the U1 additive columns."""
    workflow_journal.insert_steps(run_id, [(step_id, state)], "2026-07-27T00:00:00Z")
    # update_step writes state/attempts/output/error/error_kind. reprompted /
    # terminal_id are additive columns set by the engine path, not update_step, so
    # seed them via append_step-agnostic direct SQL only where a test needs them.
    workflow_journal.update_step(
        run_id,
        step_id,
        state,
        attempts,
        "2026-07-27T00:00:01Z",
        output_json=output_json,
        error=error,
        error_kind=error_kind,
    )
    if reprompted is not None:
        with workflow_journal._connect() as conn:
            conn.execute(
                "UPDATE workflow_run_step SET reprompted = ? WHERE run_id = ? AND step_id = ?",
                (reprompted, run_id, step_id),
            )


def _append_event(
    run_id: str,
    seq: int,
    event_type: str = "step.completed",
    **kwargs,
) -> None:
    """Append one durable event (seq is the caller-allocated ordering authority)."""
    workflow_journal.append_event(
        run_id,
        seq,
        event_type,
        event_schema_version=1,
        ts="2026-07-27T00:00:00Z",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Compare (FR-8.1)
# ---------------------------------------------------------------------------
def test_compare_aligned_step_surfaces_per_side_deltas(client):
    """FR-8.1: an aligned step returns each run's attempts / duration / provider /
    agent / validation / failure-retry deltas."""
    _seed_run("a")
    _seed_run("b")
    _seed_step("a", "s1", state="completed", attempts=1, error_kind=None)
    _seed_step("b", "s1", state="failed", attempts=3, error_kind="timeout", reprompted=2)
    # Baseline s1: claude_code / dev, 100ms, validation ok, output ref refA.
    _append_event(
        "a",
        1,
        "step.completed",
        step_id="s1",
        provider="claude_code",
        agent_profile="dev",
        engine="cli",
        elapsed_ms=100,
        validation_result="ok",
        output_ref="refA",
    )
    # Compare s1: kiro_cli / reviewer, 250ms total across two events, validation
    # invalid, output ref refB.
    _append_event(
        "b",
        1,
        "step.attempt.failed",
        step_id="s1",
        provider="kiro_cli",
        agent_profile="reviewer",
        engine="cli",
        elapsed_ms=150,
        validation_result="invalid",
        output_ref="refB",
    )
    _append_event(
        "b",
        2,
        "step.failed",
        step_id="s1",
        provider="kiro_cli",
        agent_profile="reviewer",
        engine="cli",
        elapsed_ms=100,
        validation_result="invalid",
        output_ref="refB",
    )

    resp = client.get("/workflows/runs/a/compare?against=b")
    assert resp.status_code == 200
    body = resp.json()
    assert body["baseline_run_id"] == "a"
    assert body["compare_run_id"] == "b"

    assert len(body["steps"]) == 1
    step = body["steps"][0]
    assert step["step_id"] == "s1"
    assert step["status"] == "aligned"

    a, b = step["a"], step["b"]
    assert a["attempts"] == 1 and b["attempts"] == 3
    assert a["duration_ms"] == 100 and b["duration_ms"] == 250
    assert a["provider"] == "claude_code" and b["provider"] == "kiro_cli"
    assert a["agent_profile"] == "dev" and b["agent_profile"] == "reviewer"
    assert a["validation"] == "ok" and b["validation"] == "invalid"
    assert a["state"] == "completed" and b["state"] == "failed"
    assert a["error_kind"] is None and b["error_kind"] == "timeout"
    assert b["reprompted"] == 2

    # Reference-level output diff (BR-2): differing output_ref sets, not payloads.
    assert len(body["output_diffs"]) == 1
    diff = body["output_diffs"][0]
    assert diff["step_id"] == "s1"
    assert diff["a_refs"] == ["refA"]
    assert diff["b_refs"] == ["refB"]


def test_compare_added_and_removed_steps_never_silently_dropped(client):
    """BR-1: a step present in only one run is ADDED (compare-only) or REMOVED
    (baseline-only) — never silently omitted."""
    _seed_run("a")
    _seed_run("b")
    _seed_step("a", "s1")  # aligned
    _seed_step("a", "s2")  # baseline-only -> removed
    _seed_step("b", "s1")  # aligned
    _seed_step("b", "s3")  # compare-only -> added

    body = client.get("/workflows/runs/a/compare?against=b").json()
    by_id = {s["step_id"]: s for s in body["steps"]}
    assert set(by_id) == {"s1", "s2", "s3"}
    assert by_id["s1"]["status"] == "aligned"
    assert by_id["s1"]["a"] is not None and by_id["s1"]["b"] is not None

    assert by_id["s2"]["status"] == "removed"
    assert by_id["s2"]["a"] is not None
    assert by_id["s2"]["b"] is None

    assert by_id["s3"]["status"] == "added"
    assert by_id["s3"]["a"] is None
    assert by_id["s3"]["b"] is not None


def test_compare_unknown_against_returns_404(client):
    """BR-8: comparing against an unknown/deleted run id -> 404, not a partial
    silent comparison."""
    _seed_run("a")
    _seed_step("a", "s1")
    resp = client.get("/workflows/runs/a/compare?against=ghost")
    assert resp.status_code == 404
    assert "ghost" in resp.json()["detail"]


def test_compare_unknown_baseline_returns_404(client):
    """BR-8 (baseline side): an unknown path run id is a 404 too."""
    _seed_run("b")
    resp = client.get("/workflows/runs/ghost/compare?against=b")
    assert resp.status_code == 404


def test_compare_is_journal_authoritative_after_registry_cleared(client):
    """BR-6 / NFR-DUR-1: both sides load from the durable journal — clearing
    run_registry (a simulated restart) does not affect the comparison."""
    _seed_run("a")
    _seed_run("b")
    _seed_step("a", "s1")
    _seed_step("b", "s1")
    workflow_service.run_registry.clear()
    resp = client.get("/workflows/runs/a/compare?against=b")
    assert resp.status_code == 200
    assert resp.json()["steps"][0]["status"] == "aligned"


# ---------------------------------------------------------------------------
# Diagnostic bundle (FR-9.1)
# ---------------------------------------------------------------------------
def test_diagnostics_bundle_contains_every_fr91_section(client):
    """BR-3: the bundle contains each FR-9.1 section — spec id + content hash,
    redacted inputs, ordered timeline + gaps, step outcomes + structured errors,
    provider/agent/engine environment, terminal + artifact references, excerpts."""
    import hashlib

    snapshot = _SPEC.model_dump_json()
    _seed_run("r1", spec_snapshot=snapshot, inputs_json='{"topic":"widgets"}')
    _seed_step("r1", "s1", state="failed", attempts=2, error_kind="provider_error")
    # Two events + a swallowed seq (3 skipped) so a GapMarker is declared.
    _append_event(
        "r1",
        1,
        "step.started",
        step_id="s1",
        provider="claude_code",
        agent_profile="dev",
        engine="cli",
        terminal_id="term-1",
        terminal_offset_start=0,
        terminal_offset_len=42,
        output_ref="artifact://s1",
    )
    _append_event(
        "r1",
        2,
        "step.output.received",
        step_id="s1",
        provider="claude_code",
        agent_profile="dev",
        engine="cli",
        terminal_id="term-1",
        terminal_offset_start=42,
        terminal_offset_len=10,
        output_ref="artifact://s1",
    )
    _append_event("r1", 4, "step.failed", step_id="s1", error_kind="provider_error")

    resp = client.get("/workflows/runs/r1/diagnostics")
    assert resp.status_code == 200
    body = resp.json()

    # spec id + content hash (of the DURABLE snapshot).
    assert body["spec_id"] == "wf"
    assert body["spec_content_hash"] == hashlib.sha256(snapshot.encode("utf-8")).hexdigest()

    # inputs present (redaction proven in the dedicated BR-4 tests below).
    assert "widgets" in body["inputs"]

    # ordered event timeline with the declared gap (seq 3 swallowed).
    assert [e["seq"] for e in body["events"]] == [1, 2, 4]
    assert len(body["gaps"]) == 1
    assert body["gaps"][0]["after_seq"] == 2
    assert body["gaps"][0]["before_seq"] == 4

    # step outcomes + structured error (always-on metadata, no free text).
    assert body["step_outcomes"] == [
        {"step_id": "s1", "state": "failed", "error_kind": "provider_error"}
    ]

    # environment: distinct provider/agent/engine.
    assert body["environment"]["providers"] == ["claude_code"]
    assert body["environment"]["agent_profiles"] == ["dev"]
    assert body["environment"]["engines"] == ["cli"]

    # references, not payloads (BR-2): terminal id + offsets, artifact refs.
    assert body["references"]["terminals"] == [
        {"terminal_id": "term-1", "offset_start": 0, "offset_len": 42},
        {"terminal_id": "term-1", "offset_start": 42, "offset_len": 10},
    ]
    assert body["references"]["artifacts"] == ["artifact://s1"]

    # capture posture is declared; default OFF -> no excerpts (BR-9, asserted
    # thoroughly in its own test).
    assert body["capture_enabled"] is False
    assert body["excerpts"] == []


def test_diagnostics_unknown_run_returns_404(client):
    resp = client.get("/workflows/runs/ghost/diagnostics")
    assert resp.status_code == 404


def test_diagnostics_inputs_funnel_through_the_audit_log_sanitizer(client, monkeypatch):
    """BR-4 (choke-point proof): the inputs section is redacted through U7's
    ``sanitize_output``, which itself funnels through ``audit_log._sanitize_for_log``.

    A spy wraps the ONE audit_log choke point and records that the run's inputs text
    passed through it. This is the mutation-relevant assertion: a second/parallel
    redaction path bypassing audit_log would leave the spy un-called for the inputs
    and fail here (NFR-SEC-6)."""
    _seed_run("r1", inputs_json='{"secret":"hunter2"}')

    calls: list[str] = []
    real = audit_log._sanitize_for_log

    def _spy(s, *args, **kwargs):
        calls.append(s)
        return real(s, *args, **kwargs)

    monkeypatch.setattr(audit_log, "_sanitize_for_log", _spy)

    resp = client.get("/workflows/runs/r1/diagnostics")
    assert resp.status_code == 200
    # The inputs text passed through the audit_log choke point (the single funnel).
    assert any('"secret":"hunter2"' in c for c in calls)


def test_diagnostics_inputs_are_size_limited_with_truncation_marker(client):
    """BR-4 / BR-5 (marker proof): an oversized inputs blob is capped and marked
    with the SAME ``"[…truncated]"`` marker as the audit_log idiom — no second
    truncation policy."""
    big = '{"blob":"' + ("x" * 20000) + '"}'
    _seed_run("r1", inputs_json=big)
    body = client.get("/workflows/runs/r1/diagnostics").json()
    assert body["inputs"].endswith("[…truncated]")
    # Capped near U7's OUTPUT_CAP_BYTES (8 KiB), far below the 20 KB source.
    assert len(body["inputs"].encode("utf-8")) <= workflow_retention.OUTPUT_CAP_BYTES + 64


def test_diagnostics_capture_off_emits_no_output_text_refs_only(client):
    """BR-9: with capture disabled (the default posture) the bundle carries NO
    step-output text — only metadata + references."""
    _seed_run("r1")
    _seed_step("r1", "s1", state="completed", output_json='{"answer":"SECRET-OUTPUT"}')
    _append_event(
        "r1",
        1,
        "step.completed",
        step_id="s1",
        provider="claude_code",
        output_ref="artifact://s1",
    )
    # capture defaults OFF; assert the gate is off for this environment.
    assert workflow_retention.capture_enabled() is False

    body = client.get("/workflows/runs/r1/diagnostics").json()
    assert body["capture_enabled"] is False
    assert body["excerpts"] == []
    # The raw step output text must appear NOWHERE in the serialized bundle.
    import json as _json

    assert "SECRET-OUTPUT" not in _json.dumps(body)
    # References are still present (metadata is always-on, NFR-SEC-1).
    assert body["references"]["artifacts"] == ["artifact://s1"]


def test_diagnostics_capture_on_emits_sanitized_excerpts(client, monkeypatch):
    """BR-5 / NFR-SEC-4/6: with capture enabled, step outputs are exported as
    excerpts, each funnelled through ``sanitize_output`` (size-limited + redacted)."""
    monkeypatch.setattr(workflow_retention, "capture_enabled", lambda: True)
    _seed_run("r1")
    _seed_step("r1", "s1", state="completed", output_json='{"answer":"kept"}')

    body = client.get("/workflows/runs/r1/diagnostics").json()
    assert body["capture_enabled"] is True
    assert len(body["excerpts"]) == 1
    assert body["excerpts"][0]["step_id"] == "s1"
    assert "kept" in body["excerpts"][0]["excerpt"]


def test_diagnostics_capture_on_sanitizes_oversized_excerpts(client, monkeypatch):
    """BR-5: an oversized captured output is truncated with the audit_log marker —
    the excerpt path reuses the SAME single redactor as the inputs path."""
    monkeypatch.setattr(workflow_retention, "capture_enabled", lambda: True)
    _seed_run("r1")
    _seed_step("r1", "s1", state="completed", output_json="y" * 20000)

    body = client.get("/workflows/runs/r1/diagnostics").json()
    assert len(body["excerpts"]) == 1
    assert body["excerpts"][0]["excerpt"].endswith("[…truncated]")


def test_diagnostics_bundle_assembles_from_journal_after_registry_cleared(client):
    """BR-6 / FR-9.2 (MUTATION-RELEVANT journal-only guard).

    The run is seeded ONLY into the durable journal; ``run_registry`` is empty.
    After ``run_registry.clear()`` (a simulated restart) the bundle STILL assembles
    — proving it is reconstructable from the durable journal ALONE, usable by a
    support user who was not at the machine.

    MUTATION that turns this RED: replace the route's
    ``workflow_journal.get_run(run_id)`` load with a ``run_registry`` lookup. With
    the registry cleared, that lookup returns nothing, the route 404s, and both
    assertions below fail. This is the journal-authoritative lesson: the durable
    read is the source of truth; no bundle field may depend on the live cache."""
    import hashlib

    snapshot = _SPEC.model_dump_json()
    _seed_run("r1", spec_snapshot=snapshot, inputs_json='{"k":"v"}')
    _seed_step("r1", "s1", state="failed", error_kind="timeout")
    _append_event("r1", 1, "step.failed", step_id="s1", provider="claude_code")

    # No live record exists; clear the registry to simulate a fresh restart.
    workflow_service.run_registry.clear()
    assert workflow_service.run_registry == {}

    resp = client.get("/workflows/runs/r1/diagnostics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["spec_id"] == "wf"
    assert body["spec_content_hash"] == hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
    assert body["step_outcomes"][0]["error_kind"] == "timeout"
    assert body["environment"]["providers"] == ["claude_code"]
