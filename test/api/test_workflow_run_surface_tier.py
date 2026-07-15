"""Tests for U5 RunSurface tier dispatch (issue #312, Bolt 4).

Covers: per-verb tier routing (run/validate/resume/cancel), collision
rejection, error-status mapping for every U5-mapped error, the route-level
lint integration test (422 + zero journal rows + zero script code run),
unknown-tier -> YAML-arm default, and duplicate-run_id 409. The script
engine (``script_runner``) is mocked at the route boundary — no real
subprocess spawn.
"""

from __future__ import annotations

import types
import uuid
from pathlib import Path

import pytest

from cli_agent_orchestrator.clients.database import (
    _migrate_workflow_index,
    _migrate_workflow_run,
)
from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.workflow import ScriptSpec, TierCollisionError
from cli_agent_orchestrator.models.workflow_runtime import RunState
from cli_agent_orchestrator.services import script_runner, workflow_journal, workflow_service

_GOOD_SCRIPT = "def main():\n    pass\n"
_BAD_SCRIPT = "def main(:\n"  # syntax error -> lint fail


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_index()
    _migrate_workflow_run()
    return db_path


@pytest.fixture
def spec_dir(monkeypatch: pytest.MonkeyPatch):
    base = Path.home() / ".cao-test-wf-tier-api" / uuid.uuid4().hex
    base.mkdir(parents=True, exist_ok=True)
    tmux_client._resolve_and_validate_working_directory(str(base))
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


def _write_script(spec_dir: Path, name: str, body: str) -> Path:
    p = spec_dir / f"{name}.py"
    p.write_text(body)
    return p


def _write_yaml(spec_dir: Path, name: str) -> Path:
    p = spec_dir / f"{name}.yaml"
    p.write_text(
        "name: {name}\nmode: sequential\nsteps:\n"
        "  - id: only-step\n    provider: claude_code\n    agent: dev\n    prompt: go\n".format(
            name=name
        )
    )
    return p


class TestValidateTierDispatch:
    def test_py_arm_bypasses_get_workflow_and_returns_findings(self, client, isolated_db, spec_dir):
        path = _write_script(spec_dir, "good", _GOOD_SCRIPT)
        resp = client.post("/workflows/validate", json={"path": str(path)})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "pass"
        assert body["findings"] == []

    def test_py_arm_lint_fail_surfaces_findings(self, client, isolated_db, spec_dir):
        path = _write_script(spec_dir, "bad", _BAD_SCRIPT)
        resp = client.post("/workflows/validate", json={"path": str(path)})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "fail"
        assert any(f["rule_id"] == "syntax" for f in body["findings"])

    def test_unrecognized_extension_400(self, client, isolated_db, spec_dir):
        path = spec_dir / "bad.txt"
        path.write_text("hello")
        resp = client.post("/workflows/validate", json={"path": str(path)})
        assert resp.status_code == 400

    def test_yaml_arm_unchanged(self, client, isolated_db, spec_dir):
        path = _write_yaml(spec_dir, "yamlwf")
        resp = client.post("/workflows/validate", json={"path": str(path)})
        assert resp.status_code == 200
        assert resp.json()["status"] == "pass"

    def test_py_traversal_escaping_spec_dir_rejected_400(self, client, isolated_db, spec_dir):
        """A ``.py`` path reaching outside the configured spec dir via ``..``
        traversal is rejected with 400 before the file is ever opened
        (CodeQL py/path-injection sink at the ``.py`` arm's ``open()``,
        api/main.py's ``validate_workflow_endpoint``)."""
        traversal_path = str(spec_dir / ".." / "outside.py")
        resp = client.post("/workflows/validate", json={"path": traversal_path})
        assert resp.status_code == 400

    def test_py_arm_oversized_spec_rejected(self, client, isolated_db, spec_dir):
        """The ``.py`` validate arm enforces the same ``WORKFLOW_MAX_SPEC_BYTES``
        byte cap as ``workflow_spec_service.validate_only``/``load_and_validate`` —
        it must not read an unbounded file into memory before linting it."""
        from cli_agent_orchestrator.constants import WORKFLOW_MAX_SPEC_BYTES

        oversized = "# " + ("x" * (WORKFLOW_MAX_SPEC_BYTES + 1)) + "\ndef main():\n    pass\n"
        path = _write_script(spec_dir, "huge", oversized)
        resp = client.post("/workflows/validate", json={"path": str(path)})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "fail"
        assert any("bytes" in e for e in body["errors"])

    def test_py_arm_oversized_spec_read_is_capped_not_full_file(
        self, client, isolated_db, spec_dir, monkeypatch
    ):
        """The oversized-rejection above must come from a BOUNDED read (MAX+1
        bytes passed to ``fh.read()``), not from reading the whole file and
        checking its length afterward — a several-times-oversized file must
        still only ever have MAX+1 bytes pulled off disk."""
        import os as _os

        from cli_agent_orchestrator.constants import WORKFLOW_MAX_SPEC_BYTES

        oversized = "x" * (WORKFLOW_MAX_SPEC_BYTES * 4)
        path = _write_script(spec_dir, "reallyhuge", oversized)
        real_open = open
        captured = {}

        class _WrappedFile:
            def __init__(self, fh):
                self._fh = fh

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self._fh.__exit__(*exc)

            def read(self, size=-1):
                captured["size"] = size
                return self._fh.read(size)

        def wrapped_open(file, *a, **kw):
            fh = real_open(file, *a, **kw)
            if str(file) == _os.path.realpath(str(path)):
                return _WrappedFile(fh)
            return fh

        monkeypatch.setattr("builtins.open", wrapped_open)
        resp = client.post("/workflows/validate", json={"path": str(path)})
        assert resp.status_code == 200
        assert resp.json()["status"] == "fail"
        assert captured["size"] == WORKFLOW_MAX_SPEC_BYTES + 1


class TestRunTierDispatch:
    def _script_spec(self, name="scriptwf"):
        return ScriptSpec(
            name=name, path=f"/tmp/{name}.py", source=_GOOD_SCRIPT, content_hash="deadbeef"
        )

    def test_script_happy_path_dispatches_to_run_script_workflow(self, client, monkeypatch):
        spec = self._script_spec()
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.workflow_spec_service.get_workflow",
            lambda name_or_path, scan_dir=None: spec,
        )
        monkeypatch.setattr(workflow_service, "_check_run_id_available", lambda rid: None)

        async def _fake_run(spec_arg, inputs, run_id):
            from cli_agent_orchestrator.models.workflow_runtime import (
                RunState,
                WorkflowRunResult,
            )

            return WorkflowRunResult(
                run_id=run_id,
                workflow_name=spec_arg.name,
                state=RunState.COMPLETED,
                started_at="2026-01-01T00:00:00Z",
            )

        monkeypatch.setattr(script_runner, "run_script_workflow", _fake_run)
        resp = client.post(
            "/workflows/runs", json={"name_or_path": "scriptwf", "inputs": {}, "run_id": "r1"}
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == "completed"

    def test_tier_collision_maps_to_409(self, client, monkeypatch):
        def _raise(name_or_path, scan_dir=None):
            raise TierCollisionError("dup")

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.workflow_spec_service.get_workflow", _raise
        )
        resp = client.post("/workflows/runs", json={"name_or_path": "dup", "inputs": {}})
        assert resp.status_code == 409

    def test_duplicate_run_id_precheck_409_and_engine_never_called(self, client, monkeypatch):
        spec = self._script_spec()
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.workflow_spec_service.get_workflow",
            lambda name_or_path, scan_dir=None: spec,
        )

        def _raise(run_id):
            raise KeyError(f"run_id '{run_id}' already exists")

        monkeypatch.setattr(workflow_service, "_check_run_id_available", _raise)

        called = {"hit": False}

        async def _should_not_run(spec_arg, inputs, run_id):
            called["hit"] = True
            raise AssertionError("run_script_workflow must not be called after a 409 pre-check")

        monkeypatch.setattr(script_runner, "run_script_workflow", _should_not_run)
        resp = client.post(
            "/workflows/runs", json={"name_or_path": "scriptwf", "inputs": {}, "run_id": "dup"}
        )
        assert resp.status_code == 409
        assert called["hit"] is False

    def test_lint_fail_maps_to_422_with_findings_zero_journal_rows(
        self, client, monkeypatch, isolated_db
    ):
        spec = self._script_spec()
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.workflow_spec_service.get_workflow",
            lambda name_or_path, scan_dir=None: spec,
        )
        monkeypatch.setattr(workflow_service, "_check_run_id_available", lambda rid: None)

        from cli_agent_orchestrator.models.workflow import LintFinding

        finding = LintFinding(rule_id="syntax", severity="error", line=1, message="bad")

        async def _raise(spec_arg, inputs, run_id):
            raise script_runner.ScriptLintError([finding])

        monkeypatch.setattr(script_runner, "run_script_workflow", _raise)
        resp = client.post(
            "/workflows/runs",
            json={"name_or_path": "scriptwf", "inputs": {}, "run_id": "r-lint"},
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["findings"][0]["rule_id"] == "syntax"
        # Zero journal rows / zero script code run: the run route never reached
        # anything past the mocked (raising) run_script_workflow call.
        assert workflow_journal.get_run("r-lint") is None

    def test_script_post_dispatch_key_error_maps_to_409(self, client, monkeypatch):
        spec = self._script_spec()
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.workflow_spec_service.get_workflow",
            lambda name_or_path, scan_dir=None: spec,
        )
        monkeypatch.setattr(workflow_service, "_check_run_id_available", lambda rid: None)

        async def _raise(spec_arg, inputs, run_id):
            raise KeyError("run_id became unavailable")

        monkeypatch.setattr(script_runner, "run_script_workflow", _raise)
        resp = client.post(
            "/workflows/runs",
            json={"name_or_path": "scriptwf", "inputs": {}, "run_id": "r-race"},
        )
        assert resp.status_code == 409

    def test_script_post_dispatch_value_error_maps_to_400(self, client, monkeypatch):
        spec = self._script_spec()
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.workflow_spec_service.get_workflow",
            lambda name_or_path, scan_dir=None: spec,
        )
        monkeypatch.setattr(workflow_service, "_check_run_id_available", lambda rid: None)

        async def _raise(spec_arg, inputs, run_id):
            raise ValueError("bad script run input")

        monkeypatch.setattr(script_runner, "run_script_workflow", _raise)
        resp = client.post(
            "/workflows/runs",
            json={"name_or_path": "scriptwf", "inputs": {}, "run_id": "r-invalid"},
        )
        assert resp.status_code == 400


class TestResumeTierDispatch:
    def _seed_row(self, monkeypatch, tier, run_id="run1"):
        row = workflow_journal.RunRow(
            run_id=run_id,
            workflow_name="wf",
            spec_snapshot="{}",
            inputs_json="{}",
            state="failed",
            current_step_id=None,
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
            tier=tier,
        )
        monkeypatch.setattr(workflow_journal, "get_run", lambda rid: row)

    def test_script_tier_dispatches_to_resume_script_run(self, client, monkeypatch):
        self._seed_row(monkeypatch, "script")

        async def _ok(run_id):
            from cli_agent_orchestrator.models.workflow_runtime import (
                RunState,
                WorkflowRunResult,
            )

            return WorkflowRunResult(
                run_id=run_id, workflow_name="wf", state=RunState.COMPLETED, started_at="t"
            )

        monkeypatch.setattr(script_runner, "resume_script_run", _ok)
        resp = client.post("/workflows/runs/run1/resume")
        assert resp.status_code == 200
        assert resp.json()["state"] == "completed"

    def test_script_tier_resume_not_allowed_409(self, client, monkeypatch):
        self._seed_row(monkeypatch, "script")

        async def _raise(run_id):
            raise workflow_service.ResumeNotAllowedError("live")

        monkeypatch.setattr(script_runner, "resume_script_run", _raise)
        resp = client.post("/workflows/runs/run1/resume")
        assert resp.status_code == 409

    def test_script_tier_resume_corrupt_422(self, client, monkeypatch):
        self._seed_row(monkeypatch, "script")

        async def _raise(run_id):
            raise workflow_service.ResumeCorruptError("corrupt")

        monkeypatch.setattr(script_runner, "resume_script_run", _raise)
        resp = client.post("/workflows/runs/run1/resume")
        assert resp.status_code == 422

    def test_script_tier_resume_key_error_maps_to_404(self, client, monkeypatch):
        self._seed_row(monkeypatch, "script")

        async def _raise(run_id):
            raise KeyError(run_id)

        monkeypatch.setattr(script_runner, "resume_script_run", _raise)
        resp = client.post("/workflows/runs/run1/resume")
        assert resp.status_code == 404

    def test_script_tier_resume_value_error_maps_to_400(self, client, monkeypatch):
        self._seed_row(monkeypatch, "script")

        async def _raise(run_id):
            raise ValueError("bad run id")

        monkeypatch.setattr(script_runner, "resume_script_run", _raise)
        resp = client.post("/workflows/runs/run1/resume")
        assert resp.status_code == 400

    def test_unknown_tier_value_routes_to_yaml_arm(self, client, monkeypatch):
        """U5-Q2=A: any tier value other than the literal 'script' is the YAML arm."""
        self._seed_row(monkeypatch, "some-future-tier")
        called = {"yaml": False, "script": False}

        async def _yaml_ok(run_id):
            called["yaml"] = True
            from cli_agent_orchestrator.models.workflow_runtime import (
                RunState,
                WorkflowRunResult,
            )

            return WorkflowRunResult(
                run_id=run_id, workflow_name="wf", state=RunState.COMPLETED, started_at="t"
            )

        async def _script_should_not_run(run_id):
            called["script"] = True
            raise AssertionError("must not dispatch to the script arm")

        monkeypatch.setattr(workflow_service, "resume_from_last_completed", _yaml_ok)
        monkeypatch.setattr(script_runner, "resume_script_run", _script_should_not_run)
        resp = client.post("/workflows/runs/run1/resume")
        assert resp.status_code == 200
        assert called["yaml"] is True
        assert called["script"] is False


class TestCancelTierDispatch:
    def test_script_tier_live_record_dispatches_to_cancel_script_run(self, client, monkeypatch):
        record = types.SimpleNamespace(tier="script")
        monkeypatch.setattr(workflow_service, "run_registry", {"run1": record})

        called = {"hit": False}

        async def _cancel(rec):
            called["hit"] = True
            assert rec is record

        monkeypatch.setattr(script_runner, "cancel_script_run", _cancel)
        resp = client.post("/workflows/runs/run1/cancel")
        assert resp.status_code == 200
        assert called["hit"] is True

    @pytest.mark.parametrize(
        "terminal_state", [RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED]
    )
    def test_script_tier_live_terminal_record_returns_409(
        self, client, monkeypatch, terminal_state
    ):
        record = types.SimpleNamespace(tier="script", state=terminal_state)
        monkeypatch.setattr(workflow_service, "run_registry", {"run1": record})

        async def _must_not_cancel(rec):
            raise AssertionError("terminal script record must not be cancelled")

        monkeypatch.setattr(script_runner, "cancel_script_run", _must_not_cancel)
        resp = client.post("/workflows/runs/run1/cancel")
        assert resp.status_code == 409
        assert terminal_state.value in resp.json()["detail"]

    def test_no_live_record_falls_back_to_journal(self, client, monkeypatch):
        monkeypatch.setattr(workflow_service, "run_registry", {})
        row = workflow_journal.RunRow(
            run_id="run1",
            workflow_name="wf",
            spec_snapshot="{}",
            inputs_json="{}",
            state="running",
            current_step_id=None,
            started_at="2026-01-01T00:00:00Z",
            finished_at=None,
            tier="script",
        )
        monkeypatch.setattr(workflow_journal, "get_run", lambda rid: row)
        monkeypatch.setattr(workflow_service, "cancel_run", lambda rid: None)
        resp = client.post("/workflows/runs/run1/cancel")
        assert resp.status_code == 200

    def test_no_live_record_no_journal_row_404(self, client, monkeypatch):
        monkeypatch.setattr(workflow_service, "run_registry", {})
        monkeypatch.setattr(workflow_journal, "get_run", lambda rid: None)
        resp = client.post("/workflows/runs/ghost/cancel")
        assert resp.status_code == 404

    def test_journaled_not_live_running_row_cancelled_via_journal(self, client, monkeypatch):
        """A journaled-but-not-live run (crash remnant: no ``run_registry``
        entry, journal row still RUNNING) must be cancellable — the route must
        NOT dispatch to ``workflow_service.cancel_run`` here, since that
        function only ever consults ``run_registry`` and would unconditionally
        raise ``KeyError`` for a run with no live record (Copilot review,
        main.py:1793)."""
        monkeypatch.setattr(workflow_service, "run_registry", {})
        row = workflow_journal.RunRow(
            run_id="run1",
            workflow_name="wf",
            spec_snapshot="{}",
            inputs_json="{}",
            state="running",
            current_step_id=None,
            started_at="2026-01-01T00:00:00Z",
            finished_at=None,
            tier="yaml",
        )
        monkeypatch.setattr(workflow_journal, "get_run", lambda rid: row)

        def _raise(*a, **k):
            raise AssertionError("cancel_run must not be called for a journal-only run")

        monkeypatch.setattr(workflow_service, "cancel_run", _raise)

        recorded = {}

        def _update_run_state(run_id, state, finished_at):
            recorded["run_id"] = run_id
            recorded["state"] = state
            recorded["finished_at"] = finished_at

        monkeypatch.setattr(workflow_journal, "update_run_state", _update_run_state)
        resp = client.post("/workflows/runs/run1/cancel")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert recorded["run_id"] == "run1"
        assert recorded["state"] == "cancelled"
        assert recorded["finished_at"] is not None

    def test_journaled_not_live_terminal_row_409(self, client, monkeypatch):
        """A journal-only row already in a terminal state (completed) is a 409,
        not a silent 200 or an unconditional 404 from a doomed ``cancel_run``
        call."""
        monkeypatch.setattr(workflow_service, "run_registry", {})
        row = workflow_journal.RunRow(
            run_id="run1",
            workflow_name="wf",
            spec_snapshot="{}",
            inputs_json="{}",
            state="completed",
            current_step_id=None,
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
            tier="yaml",
        )
        monkeypatch.setattr(workflow_journal, "get_run", lambda rid: row)
        resp = client.post("/workflows/runs/run1/cancel")
        assert resp.status_code == 409
