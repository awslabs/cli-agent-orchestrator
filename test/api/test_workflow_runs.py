"""Tests for the Bolt-3 run-engine endpoints (issue #312, N5).

Covers the three run endpoints (POST /workflows/runs, GET .../{run_id}, POST
.../{run_id}/cancel) and their error mapping (C5 / B3-BR-14): 200 happy run, 404
unknown run/spec, 400 invalid inputs, 409 cancel-of-finished, 501 reserved mode,
500 on WorkflowEngineError. The engine service is mocked — no real terminals.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.models.workflow import (
    NotBuiltYetError,
    RunState,
    StepState,
    WorkflowSpec,
    WorkflowStep,
)
from cli_agent_orchestrator.models.workflow_runtime import (
    RunStatus,
    StepResult,
    StepStatus,
    WorkflowRunResult,
)
from cli_agent_orchestrator.services import workflow_service

_SPEC = WorkflowSpec(
    name="wf", steps=[WorkflowStep(id="s1", provider="claude_code", agent="dev", prompt="go")]
)


def _result(state=RunState.COMPLETED) -> WorkflowRunResult:
    return WorkflowRunResult(
        run_id="run1",
        workflow_name="wf",
        state=state,
        steps=[StepResult(id="s1", state=StepState.COMPLETED, attempts=1, output={"a": 1})],
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:00:01Z",
    )


@pytest.fixture
def patch_engine(monkeypatch):
    """Patch the spec resolver + engine so the endpoint runs without terminals."""
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.workflow_spec_service.get_workflow",
        lambda name_or_path, scan_dir=None: _SPEC,
    )
    return monkeypatch


def test_run_happy_200(client, patch_engine):
    async def _fake_start(spec, inputs, run_id):
        return _result()

    patch_engine.setattr(workflow_service, "start_run", _fake_start)
    resp = client.post("/workflows/runs", json={"name_or_path": "wf", "inputs": {}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "completed"
    assert body["steps"][0]["id"] == "s1"


def test_run_unknown_spec_404(client, monkeypatch):
    def _raise(name_or_path, scan_dir=None):
        raise KeyError("nope")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.workflow_spec_service.get_workflow", _raise
    )
    resp = client.post("/workflows/runs", json={"name_or_path": "ghost", "inputs": {}})
    assert resp.status_code == 404


def test_run_invalid_inputs_400(client, patch_engine):
    async def _fake_start(spec, inputs, run_id):
        raise ValueError("missing required input 'topic'")

    patch_engine.setattr(workflow_service, "start_run", _fake_start)
    resp = client.post("/workflows/runs", json={"name_or_path": "wf", "inputs": {}})
    assert resp.status_code == 400
    assert "topic" in resp.json()["detail"]


def test_run_reserved_mode_501(client, patch_engine):
    async def _fake_start(spec, inputs, run_id):
        raise NotBuiltYetError("workflow mode 'parallel' is reserved (not built yet)")

    patch_engine.setattr(workflow_service, "start_run", _fake_start)
    resp = client.post("/workflows/runs", json={"name_or_path": "wf", "inputs": {}})
    assert resp.status_code == 501
    assert "reserved" in resp.json()["detail"]


def test_run_duplicate_run_id_409(client, patch_engine):
    async def _fake_start(spec, inputs, run_id):
        raise KeyError("run_id 'dup' already exists")

    patch_engine.setattr(workflow_service, "start_run", _fake_start)
    resp = client.post(
        "/workflows/runs", json={"name_or_path": "wf", "inputs": {}, "run_id": "dup"}
    )
    assert resp.status_code == 409


def test_run_engine_error_500(client, patch_engine):
    async def _fake_start(spec, inputs, run_id):
        raise workflow_service.WorkflowEngineError("unsupported template reference")

    patch_engine.setattr(workflow_service, "start_run", _fake_start)
    resp = client.post("/workflows/runs", json={"name_or_path": "wf", "inputs": {}})
    assert resp.status_code == 500


def test_get_run_status_200(client, monkeypatch):
    snapshot = RunStatus(
        run_id="run1",
        state=RunState.RUNNING,
        current_step_id="s1",
        steps=[StepStatus(id="s1", state=StepState.RUNNING, attempts=1)],
    )
    monkeypatch.setattr(workflow_service, "get_run_status", lambda rid: snapshot)
    resp = client.get("/workflows/runs/run1")
    assert resp.status_code == 200
    assert resp.json()["state"] == "running"
    assert resp.json()["current_step_id"] == "s1"


def test_get_run_status_unknown_404(client, monkeypatch):
    def _raise(rid):
        raise KeyError(rid)

    monkeypatch.setattr(workflow_service, "get_run_status", _raise)
    resp = client.get("/workflows/runs/ghost")
    assert resp.status_code == 404


def _seed_yaml_record(monkeypatch, run_id="run1"):
    """Seed a live YAML-tier record so U5's cancel dispatch (BR-15, registry-first)
    routes into the (mocked) base ``cancel_run`` — the same call the pre-U5 route
    made unconditionally. Without a live record, U5 dispatches through the
    journal-fallback arm (BR-16) instead of the live-registry arm."""
    import types

    monkeypatch.setattr(
        workflow_service, "run_registry", {run_id: types.SimpleNamespace(tier="yaml")}
    )


def test_cancel_run_200(client, monkeypatch):
    _seed_yaml_record(monkeypatch, "run1")
    monkeypatch.setattr(workflow_service, "cancel_run", lambda rid: None)
    resp = client.post("/workflows/runs/run1/cancel")
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_cancel_run_unknown_404(client, monkeypatch):
    def _raise(rid):
        raise KeyError(rid)

    # "ghost" has no live record AND no journal row -> the journal-fallback
    # arm (BR-16) raises 404 before ever calling cancel_run.
    monkeypatch.setattr(workflow_service, "cancel_run", _raise)
    resp = client.post("/workflows/runs/ghost/cancel")
    assert resp.status_code == 404


def test_cancel_finished_run_409(client, monkeypatch):
    _seed_yaml_record(monkeypatch, "run1")

    def _raise(rid):
        raise ValueError("run 'run1' is already completed; cannot cancel")

    monkeypatch.setattr(workflow_service, "cancel_run", _raise)
    resp = client.post("/workflows/runs/run1/cancel")
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Unit A — script-run input validation + cap, BEFORE any journal row (BR-A3)
# ---------------------------------------------------------------------------
@pytest.fixture
def script_run_env(client, monkeypatch, tmp_path):
    """A ScriptSpec-returning resolver + a fresh journal DB.

    ``run_script_workflow`` is patched to a spy that would ONLY be reached if
    validation/cap passed — the tests assert it is NOT called on rejection AND
    that no ``workflow_run`` row was written (the run route validates + caps the
    inputs BEFORE any journal write or registry entry, BR-A3 / ADR-6)."""
    from cli_agent_orchestrator.clients.database import _migrate_workflow_run
    from cli_agent_orchestrator.models.workflow import InputDecl, ScriptSpec
    from cli_agent_orchestrator.services import script_runner, workflow_journal

    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_run()

    spec = ScriptSpec(
        name="scr",
        path="/tmp/scr.py",
        source="print('x')\n",
        content_hash="deadbeef",
        inputs={
            "topic": InputDecl(type="string", required=True),
            "note": InputDecl(type="string", required=False),
        },
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.workflow_spec_service.get_workflow",
        lambda name_or_path, scan_dir=None: spec,
    )

    spy = {"called": False, "inputs": None}

    async def _fake_run(spec_arg, inputs, run_id):
        spy["called"] = True
        spy["inputs"] = inputs
        return _result(state=RunState.COMPLETED)

    monkeypatch.setattr(script_runner, "run_script_workflow", _fake_run)
    return {"spy": spy, "journal": workflow_journal}


def test_script_run_undeclared_input_400_no_journal_row(client, script_run_env):
    resp = client.post(
        "/workflows/runs",
        json={"name_or_path": "scr", "inputs": {"topic": "t", "bogus": 1}, "run_id": "runA"},
    )
    assert resp.status_code == 400
    assert "bogus" in resp.json()["detail"]
    assert script_run_env["spy"]["called"] is False
    assert script_run_env["journal"].get_run("runA") is None  # BR-A3: no orphan row


def test_script_run_missing_required_400_no_journal_row(client, script_run_env):
    resp = client.post(
        "/workflows/runs",
        json={"name_or_path": "scr", "inputs": {}, "run_id": "runB"},
    )
    assert resp.status_code == 400
    assert "topic" in resp.json()["detail"]
    assert script_run_env["spy"]["called"] is False
    assert script_run_env["journal"].get_run("runB") is None


def test_script_run_wrong_type_400_no_journal_row(client, script_run_env):
    resp = client.post(
        "/workflows/runs",
        json={"name_or_path": "scr", "inputs": {"topic": 123}, "run_id": "runC"},
    )
    assert resp.status_code == 400
    assert script_run_env["spy"]["called"] is False
    assert script_run_env["journal"].get_run("runC") is None


def test_script_run_oversized_inputs_400_pre_journal(client, script_run_env):
    # A value pushing the compact-JSON payload past 32768 bytes is rejected at
    # the route BEFORE any journal write (ADR-5 cap, pre-journal).
    big = "x" * 40000
    resp = client.post(
        "/workflows/runs",
        json={"name_or_path": "scr", "inputs": {"topic": "t", "note": big}, "run_id": "runD"},
    )
    assert resp.status_code == 400
    assert "exceed" in resp.json()["detail"]
    assert script_run_env["spy"]["called"] is False
    assert script_run_env["journal"].get_run("runD") is None


def test_script_run_resolved_inputs_passed_to_runner(client, script_run_env):
    # A valid run reaches the runner with the RESOLVED map (defaults filled),
    # not the raw request body.
    resp = client.post(
        "/workflows/runs",
        json={"name_or_path": "scr", "inputs": {"topic": "birds"}, "run_id": "runE"},
    )
    assert resp.status_code == 200
    assert script_run_env["spy"]["called"] is True
    # ``note`` is optional with no default -> omitted; ``topic`` kept.
    assert script_run_env["spy"]["inputs"] == {"topic": "birds"}


# ---------------------------------------------------------------------------
# U2 (issue #505) — the async submission spine: POST /workflows/runs:submit
# ---------------------------------------------------------------------------
import sqlite3  # noqa: E402

from cli_agent_orchestrator.services import workflow_journal  # noqa: E402


@pytest.fixture
def async_yaml_env(client, monkeypatch, tmp_path):
    """A YAML-spec resolver + a real temp journal DB + a mocked prepared drive.

    The background drive is mocked at ``start_run_prepared`` so no real terminals
    spawn; it settles the journal to COMPLETED so the poll-to-terminal test can
    observe convergence. The durable insert (``insert_run_with_steps``) is REAL, so
    the durable-before-ack invariant is exercised against a real DB.
    """
    from cli_agent_orchestrator.clients.database import (
        _migrate_workflow_run,
        _migrate_workflow_run_step,
    )

    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_run()
    _migrate_workflow_run_step()

    workflow_service.run_registry.clear()
    workflow_service._active_drives.clear()

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.workflow_spec_service.get_workflow",
        lambda name_or_path, scan_dir=None: _SPEC,
    )

    drove = {"run_id": None}

    async def _fake_prepared(record):
        drove["run_id"] = record.run_id
        record.state = RunState.COMPLETED
        record.finished_at = workflow_service._now()
        workflow_journal.update_run_state(
            record.run_id, RunState.COMPLETED.value, record.finished_at
        )
        return _result(state=RunState.COMPLETED)

    monkeypatch.setattr(workflow_service, "start_run_prepared", _fake_prepared)
    yield {"drove": drove, "journal": workflow_journal, "db": db_path}
    workflow_service.run_registry.clear()
    workflow_service._active_drives.clear()


def test_submit_202_shape_and_all_five_links(client, async_yaml_env):
    """RR-1 + RR-2: a successful submit returns 202 with ``{run_id, state, links}``,
    state == "running", and all five relative-URL link roles."""
    resp = client.post(
        "/workflows/runs:submit", json={"name_or_path": "wf", "inputs": {}, "run_id": "async-1"}
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["run_id"] == "async-1"
    assert body["state"] == "running"
    links = body["links"]
    assert set(links) == {"self", "status", "events", "result", "cancel"}
    for role, url in links.items():
        assert url.startswith("/workflows/runs/"), f"{role} must be a relative path"
    assert links["cancel"] == "/workflows/runs/async-1/cancel"
    assert links["events"] == "/workflows/runs/async-1/events"
    assert links["result"] == "/workflows/runs/async-1/result"
    assert links["self"] == "/workflows/runs/async-1"
    assert links["status"] == "/workflows/runs/async-1"


def test_submit_run_id_allocated_before_ack(client, async_yaml_env):
    """INV-1/RR-1: the instant the 202 returns, the run is durably readable —
    the awaited insert (step 5) is complete before the ack (step 8)."""
    resp = client.post(
        "/workflows/runs:submit", json={"name_or_path": "wf", "inputs": {}, "run_id": "async-dur"}
    )
    assert resp.status_code == 202
    # Durable the instant the ack returned (not after the background drive).
    row = async_yaml_env["journal"].get_run("async-dur")
    assert row is not None
    assert row.tier == "yaml"
    # The seeded step row is durable too (atomic with the run row).
    steps = async_yaml_env["journal"].get_steps("async-dur")
    assert {s.step_id for s in steps} == {"s1"}


def test_submit_insert_failure_returns_5xx_and_no_row(client, async_yaml_env, monkeypatch):
    """INV-1/T3: when the atomic insert raises ``sqlite3.Error``, the submit returns
    5xx, emits NO 202, and NO row exists (the insert is a hard precondition)."""

    def _boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(workflow_journal, "insert_run_with_steps", _boom)
    resp = client.post(
        "/workflows/runs:submit", json={"name_or_path": "wf", "inputs": {}, "run_id": "async-fail"}
    )
    assert resp.status_code == 500
    assert resp.status_code != 202
    assert async_yaml_env["journal"].get_run("async-fail") is None
    # No orphan record was registered either.
    assert "async-fail" not in workflow_service.run_registry


def test_submit_reaches_terminal_in_journal(client, async_yaml_env):
    """CR-2/BR-3: a submitted run's background drive settles the journal to a
    terminal state without the client waiting for it at ack time."""
    resp = client.post(
        "/workflows/runs:submit", json={"name_or_path": "wf", "inputs": {}, "run_id": "async-term"}
    )
    assert resp.status_code == 202
    # Poll get_run to terminal; GET pumps the TestClient event loop so the
    # background task advances between polls.
    final = None
    for _ in range(100):
        client.get("/workflows/runs/async-term")
        row = async_yaml_env["journal"].get_run("async-term")
        if row is not None and row.state in ("completed", "failed", "cancelled"):
            final = row.state
            break
    assert final == "completed"
    assert async_yaml_env["drove"]["run_id"] == "async-term"


def test_submit_double_admission_own_id_does_not_409(client, async_yaml_env):
    """DR-1: an async-submitted run does NOT 409 on its OWN id — the background
    task drives via the prepared entry (which skips ``_check_run_id_available``),
    not the blocking path that would re-admit the just-registered id."""
    resp = client.post(
        "/workflows/runs:submit",
        json={"name_or_path": "wf", "inputs": {}, "run_id": "async-once"},
    )
    assert resp.status_code == 202
    # Drive to terminal — a double-admission would surface as the run being marked
    # FAILED by the background backstop instead of COMPLETED.
    final = None
    for _ in range(100):
        client.get("/workflows/runs/async-once")
        row = async_yaml_env["journal"].get_run("async-once")
        if row is not None and row.state in ("completed", "failed", "cancelled"):
            final = row.state
            break
    assert final == "completed"


def test_submit_colliding_caller_id_with_missing_spec_is_409_not_404(client, monkeypatch):
    """OR-4 (the pinning test): a caller-supplied run_id that collides AND a
    name_or_path naming a NONEXISTENT spec returns 409, not 404 — the duplicate-id
    admission gate runs BEFORE spec resolve, so a collision is never masked."""
    # The id is already claimed (live registry entry).
    import types

    monkeypatch.setattr(
        workflow_service, "run_registry", {"dup-id": types.SimpleNamespace(tier="yaml")}
    )

    def _missing(name_or_path, scan_dir=None):
        raise KeyError("nope")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.workflow_spec_service.get_workflow", _missing
    )
    resp = client.post("/workflows/runs:submit", json={"name_or_path": "ghost", "run_id": "dup-id"})
    assert resp.status_code == 409


def test_submit_invalid_inputs_400_no_orphan_row(client, async_yaml_env):
    """OR-1: bad inputs -> 400 and NO run row (no orphan RUNNING) and NO record."""
    resp = client.post(
        "/workflows/runs:submit",
        json={"name_or_path": "wf", "inputs": {"bogus": 1}, "run_id": "async-bad"},
    )
    assert resp.status_code == 400
    assert async_yaml_env["journal"].get_run("async-bad") is None
    assert "async-bad" not in workflow_service.run_registry


def test_submit_oversized_inputs_400_pre_journal(client, monkeypatch, tmp_path):
    """OR-1/NFR-4: inputs over the 32KiB cap -> 400 before any journal write."""
    from cli_agent_orchestrator.clients.database import (
        _migrate_workflow_run,
        _migrate_workflow_run_step,
    )
    from cli_agent_orchestrator.models.workflow import InputDecl, WorkflowSpec, WorkflowStep

    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_run()
    _migrate_workflow_run_step()

    spec = WorkflowSpec(
        name="wf",
        inputs={"note": InputDecl(type="string", required=False)},
        steps=[WorkflowStep(id="s1", provider="claude_code", agent="dev", prompt="go")],
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.workflow_spec_service.get_workflow",
        lambda name_or_path, scan_dir=None: spec,
    )
    big = "x" * 40000
    resp = client.post(
        "/workflows/runs:submit",
        json={"name_or_path": "wf", "inputs": {"note": big}, "run_id": "async-big"},
    )
    assert resp.status_code == 400
    assert "exceed" in resp.json()["detail"]
    assert workflow_journal.get_run("async-big") is None


@pytest.fixture
def async_script_env(client, monkeypatch, tmp_path):
    """A ScriptSpec resolver + real journal DB + mocked script prepared drive."""
    from cli_agent_orchestrator.clients.database import (
        _migrate_workflow_run,
        _migrate_workflow_run_step,
    )
    from cli_agent_orchestrator.models.workflow import ScriptSpec
    from cli_agent_orchestrator.services import script_runner

    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    workflow_service.run_registry.clear()
    workflow_service._active_drives.clear()

    spec = ScriptSpec(
        name="scr",
        path="/tmp/scr.py",
        source="def main():\n    pass\n",
        content_hash="deadbeef",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.workflow_spec_service.get_workflow",
        lambda name_or_path, scan_dir=None: spec,
    )

    prepared = {"called": False}

    async def _fake_prepared(record, spec_path, env):
        prepared["called"] = True
        workflow_journal.update_run_state(
            record.run_id, RunState.COMPLETED.value, workflow_service._now()
        )
        return _result(state=RunState.COMPLETED)

    monkeypatch.setattr(script_runner, "run_script_workflow_prepared", _fake_prepared)
    yield {"prepared": prepared, "spec": spec, "script_runner": script_runner}
    workflow_service.run_registry.clear()
    workflow_service._active_drives.clear()


def test_submit_script_tier_202_and_drives(client, async_script_env):
    """CR-2: a script spec is submittable async, journals tier=script, and reaches
    its prepared entry."""
    resp = client.post(
        "/workflows/runs:submit", json={"name_or_path": "scr", "inputs": {}, "run_id": "async-scr"}
    )
    assert resp.status_code == 202
    row = workflow_journal.get_run("async-scr")
    assert row is not None and row.tier == "script"
    final = None
    for _ in range(100):
        client.get("/workflows/runs/async-scr")
        r = workflow_journal.get_run("async-scr")
        if r is not None and r.state in ("completed", "failed", "cancelled"):
            final = r.state
            break
    assert final == "completed"
    assert async_script_env["prepared"]["called"] is True


def test_submit_script_lint_fail_422_no_row(client, async_script_env):
    """OR-2: a lint-failing script -> 422 with a findings body, NO row, NO 202."""
    bad = async_script_env["spec"].model_copy(update={"source": "def main(:\n"})
    # Re-point the resolver at the lint-failing spec.
    from cli_agent_orchestrator.services import workflow_spec_service

    workflow_spec_service.get_workflow = lambda name_or_path, scan_dir=None: bad  # type: ignore[assignment]
    resp = client.post(
        "/workflows/runs:submit",
        json={"name_or_path": "scr", "inputs": {}, "run_id": "async-lint"},
    )
    assert resp.status_code == 422
    assert "findings" in resp.json()["detail"]
    assert workflow_journal.get_run("async-lint") is None
    assert async_script_env["prepared"]["called"] is False


def test_submit_reserved_mode_501_no_row(client, monkeypatch, tmp_path):
    """OR-3: a reserved (non-sequential) YAML mode -> 501 pre-journal, NO row."""
    from cli_agent_orchestrator.clients.database import (
        _migrate_workflow_run,
        _migrate_workflow_run_step,
    )
    from cli_agent_orchestrator.models.workflow import WorkflowSpec, WorkflowStep

    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_run()
    _migrate_workflow_run_step()

    spec = WorkflowSpec(
        name="wf",
        mode="parallel",
        steps=[WorkflowStep(id="s1", provider="claude_code", agent="dev", prompt="go")],
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.workflow_spec_service.get_workflow",
        lambda name_or_path, scan_dir=None: spec,
    )
    resp = client.post(
        "/workflows/runs:submit",
        json={"name_or_path": "wf", "inputs": {}, "run_id": "async-parallel"},
    )
    assert resp.status_code == 501
    assert workflow_journal.get_run("async-parallel") is None


@pytest.mark.asyncio
async def test_run_in_background_never_reraises_and_marks_failed(monkeypatch, tmp_path):
    """BR-1/BR-2: if the prepared drive raises BEFORE settling the row,
    ``_run_in_background`` does NOT propagate (never re-raises into the loop) and
    best-effort marks the run FAILED so no run is left stuck RUNNING."""
    from cli_agent_orchestrator.api.main import _run_in_background
    from cli_agent_orchestrator.clients.database import (
        _migrate_workflow_run,
        _migrate_workflow_run_step,
    )

    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    # Seed the run row as RUNNING (as the submit handler would have).
    workflow_journal.insert_run(
        run_id="bg-fail",
        workflow_name="wf",
        spec_snapshot="{}",
        inputs_json="{}",
        state=RunState.RUNNING.value,
        started_at="2026-07-27T00:00:00Z",
    )

    async def _boom(record):
        raise RuntimeError("scheduling bug escaped before settle")

    monkeypatch.setattr(workflow_service, "start_run_prepared", _boom)

    record = workflow_service.RunRecord(
        run_id="bg-fail",
        workflow_name="wf",
        spec=_SPEC,
        inputs={},
        state=RunState.RUNNING,
        step_states={},
        started_at="2026-07-27T00:00:00Z",
    )
    # Must NOT raise (BR-1).
    await _run_in_background(record, _SPEC, "bg-fail", "yaml", {})
    # BR-2: best-effort FAILED backstop settled the run.
    row = workflow_journal.get_run("bg-fail")
    assert row is not None and row.state == "failed"


@pytest.mark.asyncio
async def test_run_in_background_backstop_write_failure_swallowed(monkeypatch, tmp_path):
    """BR-2: even the FAILED backstop write is best-effort — a failure there is
    logged and swallowed, never re-raised."""
    from cli_agent_orchestrator.api.main import _run_in_background

    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)

    async def _boom(record):
        raise RuntimeError("drive failed")

    def _boom_update(*a, **k):
        raise sqlite3.OperationalError("journal down")

    monkeypatch.setattr(workflow_service, "start_run_prepared", _boom)
    monkeypatch.setattr(workflow_journal, "update_run_state", _boom_update)

    record = workflow_service.RunRecord(
        run_id="bg-fail-2",
        workflow_name="wf",
        spec=_SPEC,
        inputs={},
        state=RunState.RUNNING,
        step_states={},
        started_at="2026-07-27T00:00:00Z",
    )
    # Must NOT raise despite BOTH the drive and the backstop write failing.
    await _run_in_background(record, _SPEC, "bg-fail-2", "yaml", {})


# ---------------------------------------------------------------------------
# U4 (issue #505) — journal-authoritative REST read surface:
#   GET /workflows/runs          (list, declared BEFORE the /workflows/{name}
#                                 catch-all — the RO-1 route-ordering hazard)
#   GET /workflows/runs/{id}/result  (full retained WorkflowRunResult)
# Real temp journal DB; run_registry EMPTY (reads never touch it).
# ---------------------------------------------------------------------------
@pytest.fixture
def read_surface_db(client, monkeypatch, tmp_path):
    """A real temp journal DB for the U4 read routes, with an EMPTY run_registry
    so every assertion proves the read is answerable from the journal alone."""
    from cli_agent_orchestrator.clients.database import (
        _migrate_workflow_run,
        _migrate_workflow_run_step,
    )

    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    workflow_service.run_registry.clear()
    yield workflow_journal
    workflow_service.run_registry.clear()


def _seed_run(
    run_id,
    state,
    started_at,
    *,
    workflow_name="wf",
    finished_at=None,
    tier="yaml",
    steps=None,
):
    """Seed a durable run (+ optional step rows) directly in the journal.

    ``steps`` is a list of dicts: ``{"id", "state", "attempts", "output_json",
    "error"}`` — everything is written straight into the durable tables so the
    read route assembles from real rows, not a mock.
    """
    workflow_journal.insert_run(
        run_id=run_id,
        workflow_name=workflow_name,
        spec_snapshot="{}",
        inputs_json="{}",
        state=RunState.RUNNING.value,
        started_at=started_at,
        tier=tier,
    )
    for s in steps or []:
        workflow_journal.insert_steps(run_id, [(s["id"], StepState.PENDING.value)], started_at)
        workflow_journal.update_step(
            run_id=run_id,
            step_id=s["id"],
            state=s.get("state", StepState.COMPLETED.value),
            attempts=s.get("attempts", 1),
            updated_at=started_at,
            output_json=s.get("output_json"),
            error=s.get("error"),
        )
    if state != RunState.RUNNING.value or finished_at is not None:
        workflow_journal.update_run_state(run_id, state, finished_at)


def test_list_runs_route_resolves_to_list_handler_not_name_runs(client, read_surface_db):
    """T1 (RO-1 / NFR-2a — THE load-bearing route-resolution guard): ``GET
    /workflows/runs`` MUST resolve to the LIST handler, NOT be captured by the
    ``GET /workflows/{name}`` catch-all as ``name="runs"``. Mirrors the #510
    ``/agents/profiles/search`` precedent: a future reorder that moves this route
    below the catch-all makes this test fail (the catch-all would try to resolve a
    workflow spec named 'runs' and return a dict/404, not a JSON array)."""
    _seed_run("r-only", RunState.COMPLETED.value, "2026-07-27T00:00:00Z")
    resp = client.get("/workflows/runs")
    assert resp.status_code == 200
    body = resp.json()
    # The discriminator: the list handler returns a JSON ARRAY; the {name}
    # catch-all would return a spec object (a dict) or a 404 for name="runs".
    assert isinstance(body, list)
    assert [row["run_id"] for row in body] == ["r-only"]


def test_list_runs_newest_first(client, read_surface_db):
    """List returns runs newest-first (started_at DESC, run_id DESC)."""
    _seed_run("old", RunState.COMPLETED.value, "2026-07-27T00:00:00Z")
    _seed_run("new", RunState.RUNNING.value, "2026-07-27T00:00:05Z")
    _seed_run("mid", RunState.FAILED.value, "2026-07-27T00:00:03Z")
    resp = client.get("/workflows/runs")
    assert resp.status_code == 200
    assert [row["run_id"] for row in resp.json()] == ["new", "mid", "old"]


def test_list_runs_state_filter_and_illegal_state_400(client, read_surface_db):
    """T2 (LR-1): a legal ``state`` filters the list; an illegal value is a 400."""
    _seed_run("run-run", RunState.RUNNING.value, "2026-07-27T00:00:00Z")
    _seed_run("done-run", RunState.COMPLETED.value, "2026-07-27T00:00:01Z")
    ok = client.get("/workflows/runs", params={"state": "running"})
    assert ok.status_code == 200
    assert [row["run_id"] for row in ok.json()] == ["run-run"]
    bad = client.get("/workflows/runs", params={"state": "bogus"})
    assert bad.status_code == 400
    assert "bogus" in bad.json()["detail"]


def test_list_runs_limit_clamped_at_boundary(client, read_surface_db):
    """T3 (LR-2): ``limit`` out of [1, 500] is rejected by FastAPI (422); an
    in-range ``limit`` caps the number of rows returned."""
    for i in range(3):
        _seed_run(f"r{i}", RunState.COMPLETED.value, f"2026-07-27T00:00:0{i}Z")
    assert client.get("/workflows/runs", params={"limit": 0}).status_code == 422
    assert client.get("/workflows/runs", params={"limit": 501}).status_code == 422
    capped = client.get("/workflows/runs", params={"limit": 2})
    assert capped.status_code == 200
    assert len(capped.json()) == 2


def test_list_runs_empty_is_200(client, read_surface_db):
    """T4 (LR-3): listing an empty table is a 200 with ``[]``, never a 404/error."""
    resp = client.get("/workflows/runs")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_runs_db_error_500(client, read_surface_db, monkeypatch):
    """T5 (LR-4): a ``sqlite3.Error`` from the DAL maps to 500, NOT a silent []."""

    def _boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(workflow_journal, "list_runs", _boom)
    resp = client.get("/workflows/runs")
    assert resp.status_code == 500


def test_list_runs_no_id_most_recent_floor(client, read_surface_db):
    """T10 (SR-1 / FR-4.8): ``?limit=1`` returns the most-recently-started run —
    the no-id ``status`` floor U5/U6 consume."""
    _seed_run("older", RunState.COMPLETED.value, "2026-07-27T00:00:00Z")
    _seed_run("newest", RunState.RUNNING.value, "2026-07-27T00:00:09Z")
    resp = client.get("/workflows/runs", params={"limit": 1})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["run_id"] == "newest"


def test_result_absent_run_404(client, read_surface_db):
    """T6 (RR-1): a result request for an unknown id is a 404."""
    resp = client.get("/workflows/runs/ghost/result")
    assert resp.status_code == 404


def test_result_answerable_from_journal_with_empty_registry(client, read_surface_db):
    """T7 (RR-2 / FR-6.2 / FR-7.2 — the detached path): with run_registry EMPTY,
    the full retained result (state, every step's state/attempts/output) is
    assembled purely from get_run + get_steps."""
    assert workflow_service.run_registry == {}
    _seed_run(
        "detached",
        RunState.COMPLETED.value,
        "2026-07-27T00:00:00Z",
        finished_at="2026-07-27T00:00:10Z",
        steps=[
            {
                "id": "s1",
                "state": StepState.COMPLETED.value,
                "attempts": 2,
                "output_json": '{"answer": 42}',
            }
        ],
    )
    resp = client.get("/workflows/runs/detached/result")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "detached"
    assert body["workflow_name"] == "wf"
    assert body["state"] == "completed"
    assert body["finished_at"] == "2026-07-27T00:00:10Z"
    assert len(body["steps"]) == 1
    step = body["steps"][0]
    assert step["id"] == "s1"
    assert step["state"] == "completed"
    assert step["attempts"] == 2
    assert step["output"] == {"answer": 42}
    assert body["kind"] is None


def test_result_per_row_corruption_degrades_not_fails(client, read_surface_db):
    """T8 (RR-3): a step with an unparseable OR non-object ``output_json`` degrades
    to ``output=None`` for that step — a full 200 result, not a 500. The non-object
    case is load-bearing: ``StepResult.output`` is an ``Optional[Dict]``, so a
    well-formed JSON array (e.g. ``[1, 2]``) would 500 the whole result under
    pydantic validation if it were not degraded to None first."""
    _seed_run(
        "corrupt",
        RunState.COMPLETED.value,
        "2026-07-27T00:00:00Z",
        steps=[
            {"id": "good", "state": StepState.COMPLETED.value, "output_json": '{"ok": true}'},
            {"id": "bad", "state": StepState.COMPLETED.value, "output_json": "{not json"},
            {"id": "nonobj", "state": StepState.COMPLETED.value, "output_json": "[1, 2, 3]"},
        ],
    )
    resp = client.get("/workflows/runs/corrupt/result")
    assert resp.status_code == 200
    by_id = {s["id"]: s for s in resp.json()["steps"]}
    assert by_id["good"]["output"] == {"ok": True}
    assert by_id["bad"]["output"] is None
    assert by_id["nonobj"]["output"] is None


def test_result_kind_inference_floor_via_seam(client, read_surface_db):
    """T9 (RR-4 + ADR-5 floor): the assembled ``kind`` is resolved through the
    single ``_resolve_error_kind`` seam. CANCELLED -> 'cancelled'; FAILED with a
    /timeout/i step error -> 'timeout'; FAILED otherwise -> 'error'; COMPLETED ->
    None. (U9 later adds column-first precedence in the same function.)"""
    _seed_run("c", RunState.CANCELLED.value, "2026-07-27T00:00:00Z", finished_at="t")
    assert client.get("/workflows/runs/c/result").json()["kind"] == "cancelled"

    _seed_run(
        "t",
        RunState.FAILED.value,
        "2026-07-27T00:00:01Z",
        finished_at="t",
        steps=[{"id": "s1", "state": StepState.FAILED.value, "error": "step Timeout after 60s"}],
    )
    assert client.get("/workflows/runs/t/result").json()["kind"] == "timeout"

    _seed_run(
        "e",
        RunState.FAILED.value,
        "2026-07-27T00:00:02Z",
        finished_at="t",
        steps=[{"id": "s1", "state": StepState.FAILED.value, "error": "provider returned 500"}],
    )
    assert client.get("/workflows/runs/e/result").json()["kind"] == "error"

    _seed_run("ok", RunState.COMPLETED.value, "2026-07-27T00:00:03Z", finished_at="t")
    assert client.get("/workflows/runs/ok/result").json()["kind"] is None


# ===========================================================================
# U9 (issue #505): _resolve_error_kind (column-first + inference floor) and the
# failure envelope assembled from journal-only state. The resolver is a pure
# unit (no client); the envelope is exercised through the result route so the
# journal-alone (empty registry) path is proven.
# ===========================================================================
_UNSET = object()  # sentinel: distinguishes "no error_kind attribute" from None


class _FakeRow:
    """A minimal ``RunRow`` stand-in for the resolver unit tests (state only)."""

    def __init__(self, state, current_step_id=None):
        self.state = state
        self.current_step_id = current_step_id


class _FakeStep:
    """A minimal step stand-in. ``error_kind`` is set ONLY when simulating #504's
    post-rebase durable column via monkeypatch; otherwise it is absent (getattr
    yields None), matching today's ``StepRow`` which has no such attribute."""

    def __init__(self, step_id="s1", state="failed", attempts=1, error=None, error_kind=_UNSET):
        self.step_id = step_id
        self.state = state
        self.attempts = attempts
        self.error = error
        if error_kind is not _UNSET:
            self.error_kind = error_kind


def test_resolve_error_kind_cancelled_branch():
    """U9-T1 (RP-3): a CANCELLED run resolves to 'cancelled'."""
    from cli_agent_orchestrator.api.main import _resolve_error_kind

    assert _resolve_error_kind(_FakeRow(RunState.CANCELLED.value), []) == "cancelled"


def test_resolve_error_kind_timeout_branch_case_insensitive():
    """U9-T1 (RP-3/RP-4): a FAILED run with a /timeout/i step error -> 'timeout'
    (case-insensitive substring, not a parse)."""
    from cli_agent_orchestrator.api.main import _resolve_error_kind

    steps = [_FakeStep(state="failed", error="step TIMEOUT after 60s")]
    assert _resolve_error_kind(_FakeRow(RunState.FAILED.value), steps) == "timeout"


def test_resolve_error_kind_error_branch():
    """U9-T1 (RP-3): a FAILED run with a non-timeout error -> 'error'."""
    from cli_agent_orchestrator.api.main import _resolve_error_kind

    steps = [_FakeStep(state="failed", error="provider returned 500")]
    assert _resolve_error_kind(_FakeRow(RunState.FAILED.value), steps) == "error"


def test_resolve_error_kind_completed_is_none():
    """U9-T1 (RP-3): a COMPLETED run resolves to None (no kind)."""
    from cli_agent_orchestrator.api.main import _resolve_error_kind

    assert _resolve_error_kind(_FakeRow(RunState.COMPLETED.value), []) is None


def test_resolve_error_kind_never_fabricates_on_completed_with_stray_error():
    """U9-T2 (RP-4, conservative): a COMPLETED run with a stray step error string
    still resolves to None — the resolver never fabricates a kind for a
    completed/non-terminal run."""
    from cli_agent_orchestrator.api.main import _resolve_error_kind

    steps = [_FakeStep(state="completed", error="timeout mentioned but run is done")]
    assert _resolve_error_kind(_FakeRow(RunState.COMPLETED.value), steps) is None
    # A non-terminal (unknown) state is also None.
    assert _resolve_error_kind(_FakeRow(RunState.RUNNING.value), steps) is None


def test_resolve_error_kind_column_first_precedence_when_present():
    """U9-T3 (RP-1/RP-2): when a durable ``error_kind`` is present on a step, the
    resolver returns it VERBATIM and does NOT consult the inference. Simulates
    #504's post-rebase column via a step object carrying ``error_kind``; without it
    the resolver falls back to inference (proving the swap is confined to the one
    function). Activates automatically once ``StepRow`` surfaces ``error_kind``."""
    from cli_agent_orchestrator.api.main import _resolve_error_kind

    # A FAILED run whose step carries a durable kind that DISAGREES with what the
    # inference would produce — the durable value must win (RP-1).
    durable_steps = [_FakeStep(state="failed", error="provider returned 500", error_kind="timeout")]
    assert _resolve_error_kind(_FakeRow(RunState.FAILED.value), durable_steps) == "timeout"

    # Same run WITHOUT the durable column -> the inference floor produces 'error'
    # (RP-2), proving the durable value above was authoritative, not the inference.
    inferred_steps = [_FakeStep(state="failed", error="provider returned 500")]
    assert _resolve_error_kind(_FakeRow(RunState.FAILED.value), inferred_steps) == "error"


def test_durable_error_kind_inert_on_real_steprow_today():
    """U9 (#504-rebase guard): today's ``StepRow`` has NO ``error_kind`` attribute,
    so ``_durable_error_kind`` is INERT (returns None) and the inference floor is in
    force. This pins the pre-rebase behavior; it flips to column-first the moment
    #504's column + ``StepRow`` field land, with no call-site change (RP-5)."""
    from cli_agent_orchestrator.api.main import _durable_error_kind
    from cli_agent_orchestrator.services.workflow_journal import StepRow

    row = StepRow(
        run_id="r",
        step_id="s1",
        state="failed",
        attempts=1,
        output_json=None,
        error="boom",
        updated_at="t",
    )
    assert not hasattr(row, "error_kind")
    assert _durable_error_kind([row]) is None


def test_failure_envelope_assembled_for_failed_run_from_journal(client, read_surface_db):
    """U9-T5/T6/T9 (EF-1..EF-4, JP-1): a FAILED run's result carries a failure
    envelope assembled from journal rows alone (empty registry). failing_step = the
    first FAILED step; attempt = its attempts; terminal_reference = run_id;
    next_command references the run id."""
    assert workflow_service.run_registry == {}
    _seed_run(
        "failrun",
        RunState.FAILED.value,
        "2026-07-27T00:00:00Z",
        finished_at="2026-07-27T00:00:09Z",
        steps=[
            {"id": "a", "state": StepState.COMPLETED.value, "attempts": 1},
            {"id": "b", "state": StepState.FAILED.value, "attempts": 3, "error": "boom"},
        ],
    )
    body = client.get("/workflows/runs/failrun/result").json()
    env = body["failure_envelope"]
    assert env["failing_step"] == "b"
    assert env["attempt"] == 3
    assert env["error_kind"] == "error"
    assert env["terminal_reference"] == "failrun"
    assert env["next_command"] == "cao workflow result failrun"


def test_failure_envelope_timeout_kind_and_hint(client, read_surface_db):
    """U9-T6 (EF-4): a timeout failure surfaces error_kind='timeout' and a stable
    next_command hint keyed on the run id."""
    _seed_run(
        "torun",
        RunState.FAILED.value,
        "2026-07-27T00:00:00Z",
        finished_at="t",
        steps=[{"id": "s1", "state": StepState.FAILED.value, "attempts": 2, "error": "Timeout!"}],
    )
    env = client.get("/workflows/runs/torun/result").json()["failure_envelope"]
    assert env["error_kind"] == "timeout"
    assert env["failing_step"] == "s1"
    assert env["attempt"] == 2
    assert env["next_command"] == "cao workflow result torun"


def test_failure_envelope_cancelled_falls_back_to_current_step(client, read_surface_db):
    """U9-T5 (EF-1): a CANCELLED run with NO failed step falls back to the run's
    current_step_id for failing_step, reading that step's attempts (EF-2)."""
    _seed_run(
        "canrun",
        RunState.CANCELLED.value,
        "2026-07-27T00:00:00Z",
        finished_at="t",
        steps=[{"id": "s1", "state": StepState.COMPLETED.value, "attempts": 1}],
    )
    workflow_journal.update_run_current_step("canrun", "s1")
    env = client.get("/workflows/runs/canrun/result").json()["failure_envelope"]
    assert env["error_kind"] == "cancelled"
    assert env["failing_step"] == "s1"
    assert env["attempt"] == 1
    assert env["terminal_reference"] == "canrun"


def test_completed_run_result_has_no_failure_envelope(client, read_surface_db):
    """U9-T8 (EF-5 / NFR-3): a COMPLETED run's result body omits ``failure_envelope``
    entirely — the successful-run shape is byte-identical (no drift)."""
    _seed_run("okrun", RunState.COMPLETED.value, "2026-07-27T00:00:00Z", finished_at="t")
    body = client.get("/workflows/runs/okrun/result").json()
    assert "failure_envelope" not in body


def test_failure_envelope_adds_no_persisted_column(client, read_surface_db):
    """U9-T7 (EF-5): the envelope is a presentation projection — U9 introduces NO
    migration. The ``workflow_run_step`` column set is unchanged after a failed run's
    result is assembled (the envelope never writes a column)."""
    _seed_run(
        "nocol",
        RunState.FAILED.value,
        "2026-07-27T00:00:00Z",
        finished_at="t",
        steps=[{"id": "s1", "state": StepState.FAILED.value, "attempts": 1, "error": "x"}],
    )
    client.get("/workflows/runs/nocol/result")  # assembles the envelope
    from cli_agent_orchestrator.constants import DATABASE_FILE

    with sqlite3.connect(str(DATABASE_FILE)) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(workflow_run_step)")}
    assert cols == {
        "run_id",
        "step_id",
        "state",
        "attempts",
        "output_json",
        "error",
        "updated_at",
        "call_fingerprint",
    }


def test_failure_envelope_json_shape_stable_across_surfaces(client, read_surface_db):
    """U9-T8 (ST-1 / NFR-3): the ``--json`` (REST body) envelope has the fixed field
    set and a next_command hint whose shape does not drift — the same shape the CLI
    and MCP surfaces spread verbatim."""
    _seed_run(
        "shape",
        RunState.FAILED.value,
        "2026-07-27T00:00:00Z",
        finished_at="t",
        steps=[{"id": "s1", "state": StepState.FAILED.value, "attempts": 1, "error": "x"}],
    )
    env = client.get("/workflows/runs/shape/result").json()["failure_envelope"]
    assert set(env) == {
        "failing_step",
        "attempt",
        "error_kind",
        "terminal_reference",
        "next_command",
    }
    assert env["next_command"] == "cao workflow result shape"
