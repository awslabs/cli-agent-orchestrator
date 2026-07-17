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
