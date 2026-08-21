"""Tests for the Bolt-2 workflow endpoints (issue #312, N2 + N4).

Covers the four lifecycle endpoints (validate / list / get / delete) and the
structured-return output endpoint: 400/404 mapping, and the load-bearing rule
that a schema-invalid output is stored with a 200 + unvalidated state (does NOT
500).
"""

import sqlite3
import uuid
from pathlib import Path

import pytest

from cli_agent_orchestrator.clients.database import _migrate_workflow_index
from cli_agent_orchestrator.clients.tmux import tmux_client

_GOOD_SPEC = """\
name: {name}
description: a workflow
mode: sequential
steps:
  - id: only-step
    provider: claude_code
    agent: developer
    prompt: do it
"""


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_index()
    return db_path


@pytest.fixture
def spec_dir(monkeypatch: pytest.MonkeyPatch):
    base = Path.home() / ".cao-test-wf-api" / uuid.uuid4().hex
    base.mkdir(parents=True, exist_ok=True)
    tmux_client._resolve_and_validate_working_directory(str(base))  # NB-F1 guard
    # Make this dir the service's DEFAULT scan dir so name-based endpoints (get,
    # delete — which take no ?dir) resolve against it.
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.workflow_spec_service.WORKFLOW_SPEC_DIR",
        base,
        raising=True,
    )
    try:
        yield base
    finally:
        import shutil

        shutil.rmtree(base, ignore_errors=True)


def _write(spec_dir: Path, name: str, body: str = None) -> Path:
    p = spec_dir / f"{name}.yaml"
    p.write_text(body if body is not None else _GOOD_SPEC.format(name=name))
    return p


class TestValidateEndpoint:
    def test_valid_spec(self, client, spec_dir):
        path = _write(spec_dir, "okwf")
        resp = client.post("/workflows/validate", json={"path": str(path)})
        assert resp.status_code == 200
        assert resp.json()["status"] == "pass"

    def test_invalid_spec_returns_fail_status_200(self, client, spec_dir):
        # validate_only never raises; a grammar fail is a 200 with status=fail.
        path = _write(spec_dir, "bad", "name: bad\nsteps: []\n")
        resp = client.post("/workflows/validate", json={"path": str(path)})
        assert resp.status_code == 200
        assert resp.json()["status"] == "fail"

    def test_blocked_dir_maps_to_400(self, client):
        resp = client.post("/workflows/validate", json={"path": "/etc/whatever.yaml"})
        assert resp.status_code == 400

    def test_non_string_yaml_key_returns_fail_not_500(self, client, spec_dir):
        """A parseable spec with a non-string mapping key (``1: foo``) is a clean
        200 + status=fail — never an unhandled 500 from a leaked ``TypeError``
        (regression for the PR #320 never-raise finding, re-checked on Bolt 2)."""
        path = _write(spec_dir, "intkey", "1: foo\nname: intkey\nsteps: []\n")
        resp = client.post("/workflows/validate", json={"path": str(path)})
        assert resp.status_code == 200
        assert resp.json()["status"] == "fail"


class TestListEndpoint:
    def test_list_returns_rows(self, client, isolated_db, spec_dir):
        _write(spec_dir, "alpha")
        _write(spec_dir, "beta")
        resp = client.get("/workflows", params={"dir": str(spec_dir)})
        assert resp.status_code == 200
        names = [r["name"] for r in resp.json()]
        assert names == ["alpha", "beta"]

    def test_blocked_dir_maps_to_400(self, client, isolated_db):
        resp = client.get("/workflows", params={"dir": "/etc"})
        assert resp.status_code == 400


class TestGetEndpoint:
    def test_get_known(self, client, isolated_db, spec_dir):
        # spec_dir is the patched default scan dir, so the name-based GET resolves
        # against it without a ?dir param.
        _write(spec_dir, "known")
        resp = client.get("/workflows/known")
        assert resp.status_code == 200
        assert resp.json()["name"] == "known"

    def test_get_unknown_maps_to_404(self, client, isolated_db, spec_dir):
        resp = client.get("/workflows/doesnotexist")
        assert resp.status_code == 404


class TestDeleteEndpoint:
    def test_delete_unknown_maps_to_404(self, client, isolated_db, spec_dir):
        resp = client.delete("/workflows/ghostwf")
        assert resp.status_code == 404

    def test_delete_traversal_name_maps_to_400(self, client, isolated_db, spec_dir):
        # ".." fails the name validator -> ValueError -> 400.
        resp = client.delete("/workflows/..")
        assert resp.status_code in (400, 404, 405)


class TestStepOutputEndpoint:
    _SCHEMA = {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
    }

    def test_schema_pass_stores_validated(self, client):
        resp = client.post(
            "/workflows/runs/runX/steps/stepX/output",
            json={"output": {"value": 5}, "output_schema": self._SCHEMA},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["validated"] is True
        assert body["state"] == "completed"

    def test_schema_fail_stores_unvalidated_not_500(self, client):
        resp = client.post(
            "/workflows/runs/runY/steps/stepY/output",
            json={"output": {"value": "nope"}, "output_schema": self._SCHEMA},
        )
        # The endpoint NEVER 500s on a schema-invalid output; it stores the flag.
        assert resp.status_code == 200
        body = resp.json()
        assert body["validated"] is False
        assert body["errors"]
        assert body["state"] == "completed_unvalidated"

    def test_no_schema_is_trivially_valid(self, client):
        resp = client.post(
            "/workflows/runs/runZ/steps/stepZ/output",
            json={"output": {"free": "form"}},
        )
        assert resp.status_code == 200
        assert resp.json()["validated"] is True

    def test_malformed_key_maps_to_400(self, client):
        resp = client.post(
            "/workflows/runs/../steps/stepX/output",
            json={"output": {"x": 1}},
        )
        # ".." in the path either fails the name regex (400) or is normalized by
        # the router; assert it never stores a traversal key as success.
        assert resp.status_code in (400, 404, 405)


class TestResumeEndpoint:
    """The resume route's exception disambiguation (N6, business-logic-model §5).

    The route must return four distinct status codes for the ValueError-family
    arms; assert each by mocking the engine function to raise the matching type.
    The engine itself is exercised by the service tests — here we pin the boundary
    mapping (validator->400, KeyError->404, ResumeNotAllowedError->409,
    ResumeCorruptError->422) and that the subtypes are caught before bare ValueError.
    """

    @staticmethod
    def _seed_yaml_row(monkeypatch, run_id="runX"):
        """Seed a journaled YAML-tier row so U5's resume dispatch (BR-11, reads
        the journal's tier column) routes into the (mocked)
        ``resume_from_last_completed`` — the same call the pre-U5 route made
        unconditionally."""
        from cli_agent_orchestrator.services import workflow_journal

        row = workflow_journal.RunRow(
            run_id=run_id,
            workflow_name="wf",
            spec_snapshot="{}",
            inputs_json="{}",
            state="failed",
            current_step_id=None,
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
            tier="yaml",
        )
        monkeypatch.setattr(workflow_journal, "get_run", lambda rid: row)

    def test_unknown_run_maps_to_404(self, client, monkeypatch):
        from cli_agent_orchestrator.services import workflow_journal

        # No journal row at all -> 404 before any tier dispatch (BR-11).
        monkeypatch.setattr(workflow_journal, "get_run", lambda rid: None)
        resp = client.post("/workflows/runs/runX/resume")
        assert resp.status_code == 404

    def test_terminal_or_live_run_maps_to_409(self, client, monkeypatch):
        from cli_agent_orchestrator.services import workflow_service as ws

        self._seed_yaml_row(monkeypatch)

        async def _raise(run_id):
            raise ws.ResumeNotAllowedError("already completed")

        monkeypatch.setattr(ws, "resume_from_last_completed", _raise)
        resp = client.post("/workflows/runs/runX/resume")
        assert resp.status_code == 409

    def test_corrupt_snapshot_maps_to_422(self, client, monkeypatch):
        from cli_agent_orchestrator.services import workflow_service as ws

        self._seed_yaml_row(monkeypatch)

        async def _raise(run_id):
            raise ws.ResumeCorruptError("corrupt")

        monkeypatch.setattr(ws, "resume_from_last_completed", _raise)
        resp = client.post("/workflows/runs/runX/resume")
        assert resp.status_code == 422

    def test_bad_run_id_maps_to_400(self, client, monkeypatch):
        from cli_agent_orchestrator.services import workflow_service as ws

        self._seed_yaml_row(monkeypatch)

        async def _raise(run_id):
            raise ValueError("bad run_id")

        monkeypatch.setattr(ws, "resume_from_last_completed", _raise)
        resp = client.post("/workflows/runs/runX/resume")
        assert resp.status_code == 400

    def test_engine_error_maps_to_500(self, client, monkeypatch):
        from cli_agent_orchestrator.services import workflow_service as ws

        self._seed_yaml_row(monkeypatch)

        async def _raise(run_id):
            raise ws.WorkflowEngineError("bad template reference")

        monkeypatch.setattr(ws, "resume_from_last_completed", _raise)
        resp = client.post("/workflows/runs/runX/resume")
        assert resp.status_code == 500

    def test_success_returns_result(self, client, monkeypatch):
        from cli_agent_orchestrator.models.workflow_runtime import (
            RunState,
            StepResult,
            StepState,
            WorkflowRunResult,
        )
        from cli_agent_orchestrator.services import workflow_service as ws

        self._seed_yaml_row(monkeypatch)

        async def _ok(run_id):
            return WorkflowRunResult(
                run_id=run_id,
                workflow_name="wf",
                state=RunState.COMPLETED,
                steps=[StepResult(id="s1", state=StepState.COMPLETED, attempts=1)],
                started_at="2026-01-01T00:00:00Z",
                finished_at="2026-01-01T00:00:05Z",
            )

        monkeypatch.setattr(ws, "resume_from_last_completed", _ok)
        resp = client.post("/workflows/runs/runX/resume")
        assert resp.status_code == 200
        assert resp.json()["state"] == "completed"
