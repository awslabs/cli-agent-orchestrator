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


def test_cancel_run_200(client, monkeypatch):
    monkeypatch.setattr(workflow_service, "cancel_run", lambda rid: None)
    resp = client.post("/workflows/runs/run1/cancel")
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_cancel_run_unknown_404(client, monkeypatch):
    def _raise(rid):
        raise KeyError(rid)

    monkeypatch.setattr(workflow_service, "cancel_run", _raise)
    resp = client.post("/workflows/runs/ghost/cancel")
    assert resp.status_code == 404


def test_cancel_finished_run_409(client, monkeypatch):
    def _raise(rid):
        raise ValueError("run 'run1' is already completed; cannot cancel")

    monkeypatch.setattr(workflow_service, "cancel_run", _raise)
    resp = client.post("/workflows/runs/run1/cancel")
    assert resp.status_code == 409
