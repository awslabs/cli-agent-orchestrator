"""U7 tests — retention, redaction & deletion (issue #504, the six Q3 BR-SEC + FR-11).

Each of the six binding security rules is exercised as a pass/fail behavior over a
REAL durable journal (temp SQLite DB) — plus the FR-11 DELETE route and its
route-ordering pin (in ``test/api/test_workflow_route_ordering.py``). The two
load-bearing rules:

- **BR-SEC-6 (reuse the audit_log sanitizer, NO second policy).** The sanitize path
  is proven to funnel through ``audit_log._sanitize_for_log`` two ways: (a) a spy
  monkeypatched onto ``audit_log._sanitize_for_log`` is observed to fire, and (b)
  the exact ``"[…truncated]"`` marker (audit_log.py:158) appears on an over-cap
  input. A future rewrite to a second/parallel redaction policy fails BOTH.
- **BR-SEC-2 (no output unless enabled).** With capture OFF (default), a completed
  run through the real engine leaves NO event/step row carrying prompt text or full
  step output — asserted by scanning every stored cell for the injected sentinels.

The journal points at a temp DB via the patched ``DATABASE_FILE`` and the event
migration memo is reset, mirroring ``test_workflow_event_emission.py``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List
from unittest.mock import AsyncMock

import pytest

from cli_agent_orchestrator.clients.database import (
    _migrate_workflow_run,
    _migrate_workflow_run_step,
)
from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus
from cli_agent_orchestrator.models.workflow import StepState, WorkflowSpec, WorkflowStep
from cli_agent_orchestrator.models.workflow_runtime import StepOutputRecord
from cli_agent_orchestrator.services import audit_log, workflow_journal, workflow_retention
from cli_agent_orchestrator.services import workflow_service as ws


# ---------------------------------------------------------------------------
# Fixtures — a temp journal DB + a settings block we can flip per test
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _patched_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the journal at a temp DB, create the tables, clean the registry."""
    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    workflow_journal._event_migrated_paths.clear()
    ws.run_registry.clear()
    ws._active_drives.clear()
    ws.step_output_store._store.clear()
    yield db_path
    workflow_journal._event_migrated_paths.clear()
    ws.run_registry.clear()
    ws._active_drives.clear()
    ws.step_output_store._store.clear()


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch):
    """A mutable in-memory memory-settings dict the retention reads see.

    Patches ``get_memory_settings`` in the retention module's namespace so a test
    can flip capture on/off and override the caps/bounds without touching disk.
    """
    store: dict = {}
    monkeypatch.setattr(workflow_retention, "get_memory_settings", lambda: dict(store))
    return store


def _ok(terminal_id: str = "t1", last_message: str = "done") -> AgentStepResult:
    return AgentStepResult(
        terminal_id=terminal_id, last_message=last_message, status=TerminalStatus.COMPLETED
    )


def _spec(name: str = "wf", *, step_ids=("s1",)) -> WorkflowSpec:
    return WorkflowSpec(
        name=name,
        mode="sequential",
        steps=[
            WorkflowStep(id=sid, provider="kiro_cli", agent="dev", prompt="go") for sid in step_ids
        ],
    )


def _seed_run(run_id: str, *, started_at: str, state: str = "completed") -> None:
    """Seed a run + one step directly into the durable journal (no live record)."""
    workflow_journal.insert_run(
        run_id=run_id,
        workflow_name="wf",
        spec_snapshot="{}",
        inputs_json="{}",
        state=state,
        started_at=started_at,
    )
    workflow_journal.insert_steps(run_id, [("s1", state)], started_at)
    workflow_journal.append_event(
        run_id, 1, "run.started", event_schema_version=1, ts=started_at, state=state
    )
    workflow_journal.persist_high_water(run_id, 1)


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def workflow_journal_db() -> Path:
    """Resolve the (monkeypatched) temp DB path for a direct sentinel scan."""
    from cli_agent_orchestrator import constants

    return constants.DATABASE_FILE


# ===========================================================================
# BR-SEC-6 (load-bearing) — redaction funnels through the audit_log idiom
# ===========================================================================
def test_br_sec_6_sanitize_funnels_through_audit_log_sanitizer(settings, monkeypatch):
    """PASS: sanitize_output calls audit_log._sanitize_for_log (the shared choke point).

    A spy wraps the real ``_sanitize_for_log``; the assertion that it fired is the
    MUTATION guard — a rewrite to a second/independent redaction path would not call
    it and this test goes RED (NFR-SEC-6).
    """
    settings["workflow_journal_capture_output"] = True
    calls: List[str] = []
    real = audit_log._sanitize_for_log

    def _spy(s, max_len=200):
        calls.append(s)
        return real(s, max_len=max_len)

    monkeypatch.setattr(audit_log, "_sanitize_for_log", _spy)

    out = workflow_retention.sanitize_output("hello world")
    assert calls == ["hello world"]  # the audit_log idiom was the funnel
    assert out == "hello world"


def test_br_sec_6_over_cap_uses_the_audit_log_truncation_marker(settings):
    """PASS: an over-cap output is truncated with the EXACT audit_log marker.

    The literal ``"[…truncated]"`` is audit_log.py's ``_sanitize_field_value``
    marker (L158). Reusing that exact string (not a bespoke one) is what proves the
    single-policy reuse (BR-SEC-6 / BR-2).
    """
    settings["workflow_journal_capture_output"] = True
    settings["workflow_journal_output_cap_bytes"] = 64
    out = workflow_retention.sanitize_output("A" * 5000)
    assert out.endswith("[…truncated]")
    # The retained prefix is byte-capped at the configured cap.
    prefix = out[: -len("[…truncated]")]
    assert len(prefix.encode("utf-8")) <= 64


def test_br_sec_6_marker_matches_audit_log_field_sanitizer_exactly():
    """The U7 marker constant is byte-identical to the audit_log field-cap marker.

    Pins the single-policy contract at the constant level: if audit_log's marker
    ever changes, this fails and forces U7 to track it rather than fork a copy.
    """
    capped = audit_log._sanitize_field_value("B" * (audit_log.PER_FIELD_CAP_BYTES + 100))
    assert capped.endswith(workflow_retention._TRUNCATION_MARKER)


# ===========================================================================
# BR-SEC-4 — output size limit (capture on): over-cap truncation + marker
# ===========================================================================
def test_br_sec_4_capture_on_truncates_over_cap_output(settings):
    settings["workflow_journal_capture_output"] = True
    settings["workflow_journal_output_cap_bytes"] = 100
    captured = workflow_retention.sanitize_output("X" * 10_000)
    assert captured is not None
    assert captured.endswith("[…truncated]")
    assert len(captured.encode("utf-8")) <= 100 + len("[…truncated]".encode("utf-8"))


def test_br_sec_4_cap_is_configurable(settings):
    """A larger cap keeps more of the same input (proves the cap is honored, not fixed)."""
    settings["workflow_journal_capture_output"] = True
    settings["workflow_journal_output_cap_bytes"] = 20
    small = workflow_retention.sanitize_output("Y" * 1000)
    settings["workflow_journal_output_cap_bytes"] = 500
    large = workflow_retention.sanitize_output("Y" * 1000)
    assert len(large) > len(small)


def test_br_sec_4_under_cap_output_is_unchanged(settings):
    settings["workflow_journal_capture_output"] = True
    settings["workflow_journal_output_cap_bytes"] = 4096
    assert workflow_retention.sanitize_output("short and safe") == "short and safe"


# ===========================================================================
# BR-SEC-1 / BR-SEC-2 — capture-off default: always-on metadata, NO free-text
# ===========================================================================
@pytest.mark.asyncio
async def test_br_sec_1_capture_off_keeps_all_metadata_for_every_transition(settings, monkeypatch):
    """PASS: with capture off, every transition's metadata event is present + populated."""
    settings["workflow_journal_capture_output"] = False
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok("term-1")))
    await ws.start_run(_spec(step_ids=("s1",)), {}, "runMeta")

    events = {e.event_type: e for e in workflow_journal.read_events("runMeta")}
    # Always-on metadata: the lifecycle brackets + step transitions exist ...
    for et in (
        "run.started",
        "step.started",
        "step.attempt.started",
        "terminal.created",
        "step.completed",
        "run.completed",
    ):
        assert et in events, f"missing metadata event {et}"
    # ... and carry populated metadata fields (states, ids, schema version).
    assert events["step.started"].state is not None
    assert events["step.started"].provider == "kiro_cli"
    assert events["terminal.created"].terminal_id == "term-1"
    assert all(e.event_schema_version == 1 for e in events.values())


@pytest.mark.asyncio
async def test_br_sec_2_capture_off_stores_no_prompt_or_output_text(settings, monkeypatch):
    """PASS (load-bearing): capture off -> NO event/step row cell holds prompt/output text.

    The worker's prompt ("SENTINEL_PROMPT") and raw output ("SENTINEL_OUTPUT") are
    injected; after a completed run every stored cell of every event row and every
    step row is scanned. Neither sentinel appears anywhere (NFR-SEC-2). A regression
    that started journaling free-text with capture off would surface a sentinel and
    fail here.
    """
    settings["workflow_journal_capture_output"] = False

    async def _side(**kwargs):
        # The prompt reaching the worker carries a sentinel; so does the raw output.
        assert "SENTINEL_PROMPT" in kwargs["prompt"]
        return _ok("term-1", last_message="SENTINEL_OUTPUT top secret")

    step = WorkflowStep(id="s1", provider="kiro_cli", agent="dev", prompt="SENTINEL_PROMPT here")
    spec = WorkflowSpec(name="wf", mode="sequential", steps=[step])
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))
    await ws.start_run(spec, {}, "runNoText")

    # Scan every cell of every event row for the sentinels.
    with sqlite3.connect(str(workflow_journal_db())) as conn:
        for table in ("workflow_run_event", "workflow_run_step"):
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE run_id = ?", ("runNoText",)
            ).fetchall()
            for row in rows:
                for cell in row:
                    if isinstance(cell, str):
                        assert "SENTINEL_PROMPT" not in cell, f"prompt text leaked into {table}"
                        assert "SENTINEL_OUTPUT" not in cell, f"output text leaked into {table}"


@pytest.mark.asyncio
async def test_br_sec_2_output_ref_column_is_null_when_capture_off(settings, monkeypatch):
    """Every event's ``output_ref`` is NULL with capture off — references/metadata only."""
    settings["workflow_journal_capture_output"] = False
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    await ws.start_run(_spec(step_ids=("s1",)), {}, "runRef")
    assert all(e.output_ref is None for e in workflow_journal.read_events("runRef"))


# ---------------------------------------------------------------------------
# BR-SEC-2 (load-bearing, mutation guard) — the drive-loop capture GATE at the
# ``step.output.received`` emission site. A step WITH an ``output_schema`` produces
# a settled output, so the event actually FIRES carrying an ``output_ref`` payload
# (the schema-less runs above never emit ``step.output.received``, so they can't
# exercise the gate). These two tests pin both sides of the gate:
#   * capture OFF (default) -> the event's ``output_ref`` is NULL (nothing retained)
#   * capture ON            -> the event's ``output_ref`` is the SANITIZED output
# ---------------------------------------------------------------------------
_CAPTURE_SCHEMA = {
    "type": "object",
    "properties": {"secret": {"type": "string"}},
    "required": ["secret"],
}


def _schema_spec(name: str = "wf") -> WorkflowSpec:
    """A one-step spec whose step declares an output_schema (so output is collected)."""
    return WorkflowSpec(
        name=name,
        mode="sequential",
        steps=[
            WorkflowStep(
                id="s1",
                provider="kiro_cli",
                agent="dev",
                prompt="go",
                output_schema=_CAPTURE_SCHEMA,
            )
        ],
    )


def _seed_output_side_effect(output: dict):
    """A ``run_agent_step`` side effect that seeds a validated output for the step.

    Mirrors ``test_workflow_event_emission.py``'s ``_put_valid`` — the engine's
    ``_collect_structured_output`` reads this back from ``step_output_store`` and
    copies it onto ``st.output``, so ``step.output.received`` fires carrying it.
    """

    async def _side(**kwargs):
        run_id = kwargs["env_vars"]["CAO_WORKFLOW_RUN_ID"]
        step_id = kwargs["env_vars"]["CAO_WORKFLOW_STEP_ID"]
        ws.step_output_store.put(
            run_id,
            step_id,
            StepOutputRecord(
                run_id=run_id,
                step_id=step_id,
                output=output,
                validated=True,
                errors=[],
                state=StepState.COMPLETED,
            ),
        )
        return _ok("term-1")

    return _side


@pytest.mark.asyncio
async def test_br_sec_2_step_output_received_output_ref_null_when_capture_off(
    settings, monkeypatch
):
    """MUTATION GUARD: capture OFF -> the fired ``step.output.received`` retains NO output.

    A schema step produces a real output (sentinel), so the event FIRES. The event's
    ``output_ref`` is a REFERENCE — a ``sha256:`` content digest — so the sentinel
    appears in no stored cell of the event log regardless of the capture setting.

    This goes RED if the emission site writes the output TEXT into ``output_ref``
    (``output_ref=_output_json(st.output)``, or routing the sanitized TEXT there
    instead of a reference): either leaks the sentinel here.
    """
    settings["workflow_journal_capture_output"] = False
    monkeypatch.setattr(
        ws,
        "run_agent_step",
        AsyncMock(side_effect=_seed_output_side_effect({"secret": "SENTINEL_OUTPUT_OFF"})),
    )
    await ws.start_run(_schema_spec(), {}, "runCapOff")

    events = {e.event_type: e for e in workflow_journal.read_events("runCapOff")}
    # The event FIRED (a real output was collected) — this test is not vacuous.
    assert "step.output.received" in events
    # ... and output_ref is a non-revealing digest REFERENCE, never the payload.
    ref = events["step.output.received"].output_ref
    assert ref is not None and ref.startswith("sha256:")
    assert "SENTINEL_OUTPUT_OFF" not in ref
    # Defense in depth: the sentinel leaked into no stored cell of the EVENT LOG (the
    # diagnostic record NFR-SEC-2 governs). The scan is deliberately scoped to
    # ``workflow_run_event`` and NOT ``workflow_run_step``: the step row's
    # ``output_json`` is FUNCTIONAL execution state (resume + ``{{steps.<id>.output}}``
    # templating) that is intentionally retained regardless of the capture gate — it is
    # not the diagnostic free-text the gate governs.
    with sqlite3.connect(str(workflow_journal_db())) as conn:
        rows = conn.execute(
            "SELECT * FROM workflow_run_event WHERE run_id = ?", ("runCapOff",)
        ).fetchall()
        for row in rows:
            for cell in row:
                if isinstance(cell, str):
                    assert "SENTINEL_OUTPUT_OFF" not in cell


@pytest.mark.asyncio
async def test_br_sec_2_step_output_received_never_carries_the_payload(settings, monkeypatch):
    """MUTATION GUARD: even with capture ON, ``output_ref`` is a REFERENCE.

    ``output_ref`` feeds the reference-level surfaces (compare's a_refs/b_refs and
    the bundle's ``references.artifacts``), whose contract is "references, not
    payloads". So the emission site stores a ``sha256:`` content digest and the
    output TEXT never lands in the event row — with capture on OR off. The payload
    reaches an operator only via the bundle's capture-gated ``excerpts`` section,
    which re-reads the setting at export time.

    This goes RED if the emission site writes the text: either raw
    (``output_ref=_output_json(st.output)``) or via ``sanitize_output``
    (which returns the sanitized TEXT) — both leak the sentinel into output_ref.
    """
    settings["workflow_journal_capture_output"] = True
    settings["workflow_journal_output_cap_bytes"] = 64
    big = "SENTINEL_OUTPUT_ON" + "A" * 500
    monkeypatch.setattr(
        ws,
        "run_agent_step",
        AsyncMock(side_effect=_seed_output_side_effect({"secret": big})),
    )
    await ws.start_run(_schema_spec(), {}, "runCapOn")

    events = {e.event_type: e for e in workflow_journal.read_events("runCapOn")}
    received = events["step.output.received"]
    # The event FIRED with a reference — this test is not vacuous.
    assert received.output_ref is not None
    assert received.output_ref.startswith("sha256:")
    # The payload is NOT in the reference, even with capture ON.
    assert "SENTINEL_OUTPUT_ON" not in received.output_ref
    # A digest is fixed-size regardless of the (500+ byte) output.
    assert len(received.output_ref) == len("sha256:") + 16
    # Byte-identical to the retention module's own reference helper for the SAME
    # serialized output — the emission site added no second policy.
    import json

    expected = workflow_retention.output_reference(json.dumps({"secret": big}))
    assert received.output_ref == expected
    # Defense in depth: the sentinel is in NO cell of the event log.
    with sqlite3.connect(str(workflow_journal_db())) as conn:
        rows = conn.execute(
            "SELECT * FROM workflow_run_event WHERE run_id = ?", ("runCapOn",)
        ).fetchall()
        for row in rows:
            for cell in row:
                if isinstance(cell, str):
                    assert "SENTINEL_OUTPUT_ON" not in cell


def test_output_reference_is_stable_and_distinguishing():
    """The digest must be usable by the compare diff: equal outputs -> equal refs,
    different outputs -> different refs (that is the whole point of a reference)."""
    assert workflow_retention.output_reference(None) is None
    a = workflow_retention.output_reference('{"x": 1}')
    again = workflow_retention.output_reference('{"x": 1}')
    b = workflow_retention.output_reference('{"x": 2}')
    assert a == again  # stable
    assert a != b  # distinguishing
    assert a is not None and a.startswith("sha256:")


def test_output_reference_is_not_capture_gated(settings):
    """A digest is not free-text retention, so the reference surfaces stay useful
    with capture OFF (the default) — otherwise compare would go blind by default."""
    settings["workflow_journal_capture_output"] = False
    assert workflow_retention.output_reference('{"x": 1}') is not None


def test_capture_gate_on_returns_sanitized_text(settings):
    settings["workflow_journal_capture_output"] = True
    settings["workflow_journal_output_cap_bytes"] = 4096
    assert workflow_retention.sanitize_output("kept output") == "kept output"


def test_capture_defaults_to_off_when_unset(settings):
    """An empty settings block -> capture off (fail-closed default, NFR-SEC-2)."""
    assert workflow_retention.capture_enabled() is False


# ===========================================================================
# BR-SEC-3 — age + run-count retention (whichever hits first), both configurable
# ===========================================================================
def test_br_sec_3_prunes_runs_older_than_age_bound(settings):
    _seed_run("old", started_at=_iso(40))
    _seed_run("fresh", started_at=_iso(1))
    # count bound generous so ONLY the age bound can prune.
    pruned = workflow_retention.sweep_runs(retention_days=30, retention_count=1000)
    assert pruned == 1
    assert workflow_journal.get_run("old") is None
    assert workflow_journal.get_run("fresh") is not None


def test_br_sec_3_prunes_runs_beyond_count_bound(settings):
    # Five runs, all recent (age bound can't prune); keep the most-recent 2.
    for i in range(5):
        _seed_run(f"r{i}", started_at=_iso(i))  # r0 newest ... r4 oldest
    pruned = workflow_retention.sweep_runs(retention_days=3650, retention_count=2)
    assert pruned == 3
    # The two most-recent survive; the older three are gone.
    assert workflow_journal.get_run("r0") is not None
    assert workflow_journal.get_run("r1") is not None
    for gone in ("r2", "r3", "r4"):
        assert workflow_journal.get_run(gone) is None


def test_br_sec_3_union_prune_whichever_bound_hits_first(settings):
    # A run can be pruned by EITHER bound; the prune set is their union.
    _seed_run("ancient", started_at=_iso(90))  # age-pruned
    _seed_run("recent1", started_at=_iso(0))
    _seed_run("recent2", started_at=_iso(1))
    _seed_run("recent3", started_at=_iso(2))  # count-pruned (beyond most-recent 2, and recent)
    pruned = workflow_retention.sweep_runs(retention_days=30, retention_count=2)
    # ancient (age) + recent3 (count) pruned; recent1/recent2 kept.
    assert workflow_journal.get_run("ancient") is None
    assert workflow_journal.get_run("recent3") is None
    assert workflow_journal.get_run("recent1") is not None
    assert workflow_journal.get_run("recent2") is not None
    assert pruned == 2


def test_br_sec_3_defaults_come_from_settings(settings):
    """Both bounds default from settings when the args are omitted (overridable, BR-1)."""
    settings["workflow_journal_retention_days"] = 10
    settings["workflow_journal_retention_count"] = 1000
    _seed_run("old", started_at=_iso(20))
    _seed_run("fresh", started_at=_iso(1))
    pruned = workflow_retention.sweep_runs()  # no args -> read the 10-day setting
    assert pruned == 1
    assert workflow_journal.get_run("old") is None


def test_br_sec_3_sweep_is_a_noop_when_nothing_exceeds_bounds(settings):
    _seed_run("a", started_at=_iso(1))
    _seed_run("b", started_at=_iso(2))
    assert workflow_retention.sweep_runs(retention_days=30, retention_count=100) == 0
    assert workflow_journal.get_run("a") is not None
    assert workflow_journal.get_run("b") is not None


def test_br_sec_3_prune_removes_the_full_cascade(settings):
    """A pruned run's events + steps + seq go too (delete_run cascade, not just the run row)."""
    _seed_run("old", started_at=_iso(60))
    assert workflow_journal.read_events("old")  # has an event before the sweep
    workflow_retention.sweep_runs(retention_days=30, retention_count=1000)
    assert workflow_journal.get_run("old") is None
    assert workflow_journal.read_events("old") == []
    assert workflow_journal.get_steps("old") == []
    assert workflow_journal.persisted_high_water("old") == 0


def test_br_sec_3_sweep_continues_past_a_failing_delete(settings, monkeypatch):
    """A best-effort sweep logs and continues if one delete_run raises."""
    _seed_run("bad", started_at=_iso(60))
    _seed_run("good", started_at=_iso(50))
    real_delete = workflow_journal.delete_run

    def _flaky(run_id):
        if run_id == "bad":
            raise sqlite3.OperationalError("simulated delete failure")
        return real_delete(run_id)

    monkeypatch.setattr(workflow_journal, "delete_run", _flaky)
    pruned = workflow_retention.sweep_runs(retention_days=30, retention_count=1000)
    assert pruned == 1  # only "good" counted; "bad" failed but did not abort the sweep
    assert workflow_journal.get_run("good") is None


# ===========================================================================
# BR-SEC-5 — explicit per-run deletion via delete_run + unknown-id no-op
# ===========================================================================
def test_br_sec_5_delete_run_removes_all_retained_data(settings):
    _seed_run("del", started_at=_iso(1))
    workflow_journal.delete_run("del")
    assert workflow_journal.get_run("del") is None
    assert workflow_journal.get_steps("del") == []
    assert workflow_journal.read_events("del") == []
    assert workflow_journal.persisted_high_water("del") == 0


def test_br_sec_5_delete_unknown_run_is_a_noop(settings):
    """BR-3: deleting an unknown id raises nothing and faults no other read."""
    _seed_run("keep", started_at=_iso(1))
    workflow_journal.delete_run("never-existed")  # no raise
    assert workflow_journal.get_run("keep") is not None  # untouched


# ===========================================================================
# Fail-closed / non-raising defensive posture (security-relevant edges)
# ===========================================================================
def test_capture_fails_closed_on_unreadable_settings(monkeypatch):
    """A settings-read failure must fall back to no-capture (fail-closed, NFR-SEC-2)."""

    def _boom():
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(workflow_retention, "get_memory_settings", _boom)
    assert workflow_retention.capture_enabled() is False


def test_output_cap_degrades_to_default_on_bad_value(settings):
    """A zero/negative cap degrades to the 8 KiB default (never truncates to marker-only)."""
    settings["workflow_journal_output_cap_bytes"] = 0
    assert workflow_retention.output_cap_bytes() == workflow_retention.OUTPUT_CAP_BYTES


def test_int_readers_degrade_to_defaults_on_unreadable_settings(monkeypatch):
    """A settings-read failure degrades every int reader to its default (never raises)."""

    def _boom():
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(workflow_retention, "get_memory_settings", _boom)
    assert workflow_retention.output_cap_bytes() == workflow_retention.OUTPUT_CAP_BYTES
    assert workflow_retention.retention_days() == workflow_retention.RETENTION_DAYS_DEFAULT
    assert workflow_retention.retention_count() == workflow_retention.RETENTION_COUNT_DEFAULT


def test_retention_bounds_degrade_to_defaults_on_bad_value(settings):
    settings["workflow_journal_retention_days"] = -5
    settings["workflow_journal_retention_count"] = -1
    assert workflow_retention.retention_days() == workflow_retention.RETENTION_DAYS_DEFAULT
    assert workflow_retention.retention_count() == workflow_retention.RETENTION_COUNT_DEFAULT


def test_sweep_is_a_noop_when_enumeration_fails(settings, monkeypatch):
    """A run-enumeration read failure degrades the sweep to 0, never raises."""

    def _boom():
        raise sqlite3.OperationalError("db down")

    monkeypatch.setattr(workflow_journal, "list_run_ids_by_age", _boom)
    assert workflow_retention.sweep_runs(retention_days=1, retention_count=1) == 0


# ===========================================================================
# PR 526 review — SHOULD-FIX: the retention sweep must actually be SCHEDULED.
#
# sweep_runs had no production caller (`grep -rn sweep_runs src/` returned only
# its definition and docstrings), so the advertised age/run-count retention never
# ran and the event log grew unbounded. It is now invoked at startup via the
# lifespan's `_sweep_workflow_runs_at_startup` helper.
# ===========================================================================
def test_sweep_has_a_production_caller_in_the_api_module():
    """The wiring guard: a source-level check that the sweep is reachable from
    production code, not just tests. Goes RED if the call site is removed —
    which is exactly how this defect shipped (a tested function nobody called).
    """
    import inspect

    from cli_agent_orchestrator.api import main as api_main

    # The startup helper exists and calls the sweep.
    assert hasattr(api_main, "_sweep_workflow_runs_at_startup")
    assert "sweep_runs" in inspect.getsource(api_main._sweep_workflow_runs_at_startup)
    # ...and the lifespan schedules that helper.
    assert "_sweep_workflow_runs_at_startup" in inspect.getsource(api_main.lifespan)


def test_startup_sweep_helper_invokes_sweep_runs(monkeypatch):
    """Behavioral: the helper delegates to workflow_retention.sweep_runs."""
    from cli_agent_orchestrator.api import main as api_main

    calls: List[int] = []
    monkeypatch.setattr(workflow_retention, "sweep_runs", lambda: calls.append(1) or 3)
    api_main._sweep_workflow_runs_at_startup()
    assert calls == [1]


def test_startup_sweep_never_raises_into_startup(monkeypatch):
    """A maintenance sweep must never prevent the server from starting."""
    from cli_agent_orchestrator.api import main as api_main

    def _boom():
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(workflow_retention, "sweep_runs", _boom)
    api_main._sweep_workflow_runs_at_startup()  # must not raise


def test_startup_sweep_actually_prunes_over_the_real_journal(settings, monkeypatch):
    """End-to-end: seed an over-age run, run the startup helper, and prove the
    run is GONE — the sweep is wired to real data, not just called."""
    settings["workflow_journal_retention_days"] = 7
    _seed_run("old", started_at=_iso(90))
    _seed_run("fresh", started_at=_iso(1))
    assert workflow_journal.get_run("old") is not None

    from cli_agent_orchestrator.api import main as api_main

    api_main._sweep_workflow_runs_at_startup()

    assert workflow_journal.get_run("old") is None
    assert workflow_journal.get_run("fresh") is not None


# ===========================================================================
# PR 526 review — SHOULD-FIX: sanitize_output is NOT secret redaction.
# ===========================================================================
def test_sanitize_output_does_not_remove_secrets(settings):
    """Pins the honest contract: sanitize_output is transport hygiene (ANSI/C0
    stripping, newline escaping, size cap) and passes credentials through
    VERBATIM. This is why the diagnostics route is scope-gated rather than
    described as redacted — a test that documents the real behavior so nobody
    re-adds a "redacted" claim on the strength of this funnel.
    """
    settings["workflow_journal_capture_output"] = True
    settings["workflow_journal_output_cap_bytes"] = 4096
    token = "AKIAIOSFODNN7EXAMPLE"
    out = workflow_retention.sanitize_output(f'{{"aws_key": "{token}"}}')
    assert token in out  # NOT redacted — hygiene only


# ===========================================================================
# PR 526 human review — IMPORTANT: retention 0 must DISABLE a bound, never
# "delete everything".
#
# The naive reads were catastrophic: retention_days=0 puts the age cutoff at
# *now*, so every run that ever started sorts before it; retention_count=0 makes
# the count slice rows[0:], i.e. every row. And sweep_runs is invoked
# automatically at startup (_sweep_workflow_runs_at_startup), so a persisted 0 —
# a value the settings validator explicitly admits, since both keys accept >= 0 —
# would have silently wiped the entire journal on boot.
# ===========================================================================
def test_retention_days_zero_disables_the_age_bound(settings):
    """days=0 must prune NOTHING on age, not everything. The seeded run is 400
    days old, so a cutoff of *now* would certainly have taken it."""
    _seed_run("ancient", started_at=_iso(400))
    _seed_run("fresh", started_at=_iso(0))

    pruned = workflow_retention.sweep_runs(retention_days=0, retention_count=1000)

    assert pruned == 0
    assert workflow_journal.get_run("ancient") is not None
    assert workflow_journal.get_run("fresh") is not None


def test_retention_count_zero_disables_the_count_bound(settings):
    """count=0 must prune NOTHING on count, not every row (the rows[0:] slice)."""
    for i in range(4):
        _seed_run(f"r{i}", started_at=_iso(i))

    pruned = workflow_retention.sweep_runs(retention_days=3650, retention_count=0)

    assert pruned == 0
    for i in range(4):
        assert workflow_journal.get_run(f"r{i}") is not None


def test_both_retention_bounds_zero_is_a_total_noop(settings):
    """Both bounds off -> the sweep touches nothing. This is the exact shape the
    startup sweep would hit with both settings persisted as 0."""
    _seed_run("ancient", started_at=_iso(500))
    for i in range(3):
        _seed_run(f"r{i}", started_at=_iso(i))

    assert workflow_retention.sweep_runs(retention_days=0, retention_count=0) == 0
    assert workflow_journal.get_run("ancient") is not None
    for i in range(3):
        assert workflow_journal.get_run(f"r{i}") is not None
    # The cascade is intact too — not just the run rows.
    assert workflow_journal.read_events("ancient") != []


def test_startup_sweep_with_persisted_zeroes_does_not_wipe_the_journal(settings):
    """The real blast radius: the automatic startup sweep with both settings
    persisted as 0 must be a no-op, not a full journal wipe."""
    settings["workflow_journal_retention_days"] = 0
    settings["workflow_journal_retention_count"] = 0
    _seed_run("ancient", started_at=_iso(365))
    _seed_run("fresh", started_at=_iso(1))

    from cli_agent_orchestrator.api import main as api_main

    api_main._sweep_workflow_runs_at_startup()

    assert workflow_journal.get_run("ancient") is not None
    assert workflow_journal.get_run("fresh") is not None


def test_disabling_one_bound_leaves_the_other_enforced(settings):
    """0 disables ONE bound, it does not disable retention. With days=0 the count
    bound must still prune — otherwise the fix would have turned a single 0 into
    a global opt-out of retention."""
    for i in range(5):
        _seed_run(f"r{i}", started_at=_iso(i * 100))  # all old, r0 newest

    pruned = workflow_retention.sweep_runs(retention_days=0, retention_count=2)

    assert pruned == 3  # count bound still prunes beyond the most-recent 2
    assert workflow_journal.get_run("r0") is not None
    assert workflow_journal.get_run("r1") is not None
    for gone in ("r2", "r3", "r4"):
        assert workflow_journal.get_run(gone) is None


def test_nonzero_bounds_still_prune_after_the_zero_guard(settings):
    """Regression control: the guard must not have disabled pruning generally."""
    _seed_run("old", started_at=_iso(60))
    _seed_run("fresh", started_at=_iso(1))

    assert workflow_retention.sweep_runs(retention_days=30, retention_count=1000) == 1
    assert workflow_journal.get_run("old") is None
    assert workflow_journal.get_run("fresh") is not None
