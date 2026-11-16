"""Step params against profile declarations, through the HTTP surface (contract section 3).

- ``POST /workflows/validate``, path form and source form: each error is added to
  ``errors`` and makes ``status`` ``fail``. It runs only when grammar passed.
- ``POST /workflows/runs`` and ``POST /workflows/runs:submit``: 422 with the messages
  in ``detail``, before any journal row is written.
- ``POST /workflows`` (save) stays grammar-only, so a workflow saves on a machine where
  its profile is not installed.

The ``{{...}}`` grammar rule (contract section 2) is checked here at save, validate and
run as well. Profiles come from a real local store and the real loader.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.workflow import RunState, StepState, WorkflowSpec
from cli_agent_orchestrator.models.workflow_runtime import StepResult, WorkflowRunResult
from cli_agent_orchestrator.services import workflow_journal, workflow_service

ORDERS_PROFILE = (
    "---\n"
    "name: orders-agent\n"
    "description: Reads the orders table\n"
    "params:\n"
    "  table_name:\n"
    "    description: DynamoDB table name\n"
    "    required: true\n"
    "  key_type:\n"
    "    required: true\n"
    '    enum: ["S", "N", "B"]\n'
    "  limit:\n"
    "    type: int\n"
    "    default: 1\n"
    "---\n\nRead the orders table.\n"
)

MISSING_TABLE = (
    "step 'read-orders': missing required param 'table_name' "
    "(declared by profile 'orders-agent')"
)
PLACEHOLDER = (
    "step 'read-orders': param 'table_name' contains a {{...}} placeholder. "
    "Param values are fixed when the workflow is built."
)


def _spec_yaml(params: dict, *, name: str = "orders-check", agent: str = "orders-agent") -> str:
    lines = "".join(f"      {json.dumps(k)}: {json.dumps(v)}\n" for k, v in params.items())
    return (
        f"name: {name}\n"
        "description: Order table check\n"
        "steps:\n"
        "  - id: read-orders\n"
        "    provider: claude_code\n"
        f"    agent: {agent}\n"
        "    prompt: Read the newest order.\n" + (f"    params:\n{lines}" if params else "")
    )


FITS = {"table_name": "orders", "key_type": "S", "limit": "1"}
LACKS_TABLE = {"key_type": "S"}


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A local profile store holding ``orders-agent``, read by the real loader."""
    from cli_agent_orchestrator.services import profile_store
    from cli_agent_orchestrator.utils import agent_profiles

    target = tmp_path / "agent-store"
    target.mkdir()
    monkeypatch.setattr(profile_store, "LOCAL_AGENT_STORE_DIR", target)
    monkeypatch.setattr(agent_profiles, "LOCAL_AGENT_STORE_DIR", target)
    (target / "orders-agent.md").write_text(ORDERS_PROFILE, encoding="utf-8")
    return target


@pytest.fixture
def spec_dir(monkeypatch: pytest.MonkeyPatch):
    base = Path.home() / ".cao-test-wf-step-params" / uuid.uuid4().hex
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
        shutil.rmtree(base, ignore_errors=True)


def _validate_path(client, spec_dir: Path, source: str):
    path = spec_dir / "orders-check.yaml"
    path.write_text(source, encoding="utf-8")
    return client.post("/workflows/validate", json={"path": str(path)})


def _validate_source(client, source: str):
    return client.post("/workflows/validate", json={"source": source, "tier": "yaml"})


FORMS = {
    "path": lambda client, spec_dir, source: _validate_path(client, spec_dir, source),
    "source": lambda client, spec_dir, source: _validate_source(client, source),
}


class TestValidate:
    @pytest.mark.parametrize("form", list(FORMS))
    def test_a_missing_required_param_fails(self, client, store, spec_dir, form) -> None:
        resp = FORMS[form](client, spec_dir, _spec_yaml(LACKS_TABLE))

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "fail", "reserved_notes": [], "errors": [MISSING_TABLE]}

    @pytest.mark.parametrize("form", list(FORMS))
    def test_values_of_the_wrong_shape_fail(self, client, store, spec_dir, form) -> None:
        source = _spec_yaml({"table_name": "orders", "key_type": "X", "limit": "many"})

        resp = FORMS[form](client, spec_dir, source)

        assert resp.json()["status"] == "fail"
        assert resp.json()["errors"] == [
            "step 'read-orders': param 'key_type' must be one of: S, N, B",
            "step 'read-orders': param 'limit' must be a whole number",
        ]

    @pytest.mark.parametrize("form", list(FORMS))
    def test_a_step_that_fits_passes(self, client, store, spec_dir, form) -> None:
        resp = FORMS[form](client, spec_dir, _spec_yaml(FITS))

        assert resp.json() == {"status": "pass", "reserved_notes": [], "errors": []}

    @pytest.mark.parametrize("form", list(FORMS))
    def test_a_profile_that_is_not_installed_gives_no_findings(
        self, client, store, spec_dir, form
    ) -> None:
        resp = FORMS[form](client, spec_dir, _spec_yaml({}, agent="not-installed"))

        assert resp.json()["status"] == "pass"

    @pytest.mark.parametrize("form", list(FORMS))
    def test_a_profile_that_fails_to_load_gives_no_findings(
        self, client, store, spec_dir, form
    ) -> None:
        (store / "broken-agent.md").write_text(
            "---\nname: broken-agent\ndescription: d\nparams:\n  t:\n    required: maybe\n"
            "---\n\nBody.\n",
            encoding="utf-8",
        )

        resp = FORMS[form](client, spec_dir, _spec_yaml({}, agent="broken-agent"))

        assert resp.json()["status"] == "pass"

    @pytest.mark.parametrize("form", list(FORMS))
    def test_it_runs_only_when_grammar_passed(
        self, client, store, spec_dir, form, monkeypatch
    ) -> None:
        from cli_agent_orchestrator.services import step_params

        loads = []
        monkeypatch.setattr(
            step_params.agent_profiles, "load_agent_profile", lambda name: loads.append(name)
        )

        resp = FORMS[form](client, spec_dir, _spec_yaml({"table_name": 7}))

        assert resp.json()["status"] == "fail"
        assert loads == []

    @pytest.mark.parametrize("form", list(FORMS))
    def test_reserved_notes_survive_a_param_failure(self, client, store, spec_dir, form) -> None:
        source = _spec_yaml(LACKS_TABLE).replace("    prompt:", "    call: assign\n    prompt:")

        body = FORMS[form](client, spec_dir, source).json()

        assert body["status"] == "fail"
        assert body["reserved_notes"] == ["construct 'assign' is reserved (not built yet)"]
        assert body["errors"] == [MISSING_TABLE]

    @pytest.mark.parametrize("form", list(FORMS))
    def test_a_placeholder_value_fails(self, client, store, spec_dir, form) -> None:
        source = _spec_yaml({**FITS, "table_name": "{{workflow.inputs.table}}"})

        body = FORMS[form](client, spec_dir, source).json()

        assert body["status"] == "fail"
        assert body["errors"] == [PLACEHOLDER]

    @pytest.mark.parametrize("form", list(FORMS))
    def test_single_braces_pass(self, client, store, spec_dir, form) -> None:
        source = _spec_yaml({**FITS, "note": '{"a": 1}'})

        assert FORMS[form](client, spec_dir, source).json()["status"] == "pass"


class TestSave:
    def test_save_does_not_check_profiles(self, client, store, spec_dir) -> None:
        resp = client.post(
            "/workflows",
            json={"name": "orders-check", "source": _spec_yaml(LACKS_TABLE), "tier": "yaml"},
        )

        assert resp.status_code == 201, resp.text
        assert resp.json()["status"] == "pass"

    def test_save_refuses_a_placeholder_value(self, client, store, spec_dir) -> None:
        source = _spec_yaml({**FITS, "table_name": "{{workflow.inputs.table}}"})

        resp = client.post(
            "/workflows", json={"name": "orders-check", "source": source, "tier": "yaml"}
        )

        assert resp.status_code == 400, resp.text
        assert PLACEHOLDER in json.dumps(resp.json())
        assert not (spec_dir / "orders-check.yaml").exists()


ROUTES = ["/workflows/runs", "/workflows/runs:submit"]


def _result(run_id: str) -> WorkflowRunResult:
    return WorkflowRunResult(
        run_id=run_id,
        workflow_name="orders-check",
        state=RunState.COMPLETED,
        steps=[StepResult(id="read-orders", state=StepState.COMPLETED, attempts=1)],
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:00:01Z",
    )


@pytest.fixture
def run_env(monkeypatch, tmp_path, store):
    """A real journal and the real engine entry, with the steps and background drive stubbed."""
    from cli_agent_orchestrator.clients.database import (
        _migrate_workflow_run,
        _migrate_workflow_run_step,
    )

    monkeypatch.setattr(
        "cli_agent_orchestrator.constants.DATABASE_FILE", tmp_path / "wf.db", raising=True
    )
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    workflow_service.run_registry.clear()
    workflow_service._active_drives.clear()

    reached = {"drive": 0, "prepared": 0}

    async def _drive(record, order):
        reached["drive"] += 1
        return _result(record.run_id)

    async def _prepared(record):
        reached["prepared"] += 1

    monkeypatch.setattr(workflow_service, "_drive", _drive)
    monkeypatch.setattr(workflow_service, "start_run_prepared", _prepared)

    def _use(source: str) -> None:
        import yaml

        spec = WorkflowSpec(**yaml.safe_load(source))
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.workflow_spec_service.get_workflow",
            lambda name_or_path, scan_dir=None: spec,
        )

    yield SimpleNamespace(use=_use, reached=reached)
    workflow_service.run_registry.clear()
    workflow_service._active_drives.clear()


class TestRun:
    @pytest.mark.parametrize("route", ROUTES)
    def test_a_missing_required_param_is_422_before_any_journal_row(
        self, client, run_env, route
    ) -> None:
        run_env.use(_spec_yaml(LACKS_TABLE))
        run_id = f"params-{uuid.uuid4().hex[:8]}"

        resp = client.post(route, json={"name_or_path": "orders-check", "run_id": run_id})

        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == MISSING_TABLE
        assert workflow_journal.get_run(run_id) is None, "no durable row for a refused run"
        assert run_id not in workflow_service.run_registry
        assert run_env.reached == {"drive": 0, "prepared": 0}

    @pytest.mark.parametrize("route", ROUTES)
    def test_every_message_is_in_detail(self, client, run_env, route) -> None:
        run_env.use(_spec_yaml({"key_type": "X", "limit": "many"}))

        resp = client.post(route, json={"name_or_path": "orders-check"})

        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == "; ".join(
            [
                MISSING_TABLE,
                "step 'read-orders': param 'key_type' must be one of: S, N, B",
                "step 'read-orders': param 'limit' must be a whole number",
            ]
        )

    @pytest.mark.parametrize("route", ROUTES)
    def test_a_step_that_fits_runs(self, client, run_env, route) -> None:
        run_env.use(_spec_yaml(FITS))
        run_id = f"fits-{uuid.uuid4().hex[:8]}"

        resp = client.post(route, json={"name_or_path": "orders-check", "run_id": run_id})

        assert resp.status_code in (200, 202), resp.text
        assert workflow_journal.get_run(run_id) is not None
        if route == "/workflows/runs":
            assert run_env.reached["drive"] == 1

    @pytest.mark.parametrize("route", ROUTES)
    def test_a_profile_that_is_not_installed_still_runs(self, client, run_env, route) -> None:
        """Today's behaviour stays: the launch reports a missing profile."""
        run_env.use(_spec_yaml({}, agent="not-installed"))

        resp = client.post(route, json={"name_or_path": "orders-check"})

        assert resp.status_code in (200, 202), resp.text

    @pytest.mark.parametrize("route", ROUTES)
    def test_no_default_is_filled_in(self, client, run_env, route) -> None:
        """A run sends exactly the values in the file: ``limit`` is left out, not set to 1."""
        params = {"table_name": "orders", "key_type": "S"}
        run_env.use(_spec_yaml(params))
        run_id = f"nodefault-{uuid.uuid4().hex[:8]}"

        resp = client.post(route, json={"name_or_path": "orders-check", "run_id": run_id})

        assert resp.status_code in (200, 202), resp.text
        snapshot = json.loads(workflow_journal.get_run(run_id).spec_snapshot)
        assert snapshot["steps"][0]["params"] == params

    @pytest.mark.parametrize("route", ROUTES)
    def test_a_placeholder_value_is_refused_when_the_spec_loads(
        self, client, spec_dir, run_env, route
    ) -> None:
        """Grammar runs when the run route reads the file, so the run never starts."""
        from cli_agent_orchestrator.clients.database import _migrate_workflow_index

        _migrate_workflow_index()
        source = _spec_yaml({**FITS, "table_name": "{{workflow.inputs.table}}"})
        path = spec_dir / "orders-check.yaml"
        path.write_text(source, encoding="utf-8")
        run_id = f"placeholder-{uuid.uuid4().hex[:8]}"

        # By path: the name index leaves out a spec that fails grammar, so by name it is a 404.
        resp = client.post(route, json={"name_or_path": str(path), "run_id": run_id})

        assert resp.status_code == 400, resp.text
        assert PLACEHOLDER in resp.json()["detail"]
        assert workflow_journal.get_run(run_id) is None
        assert run_env.reached == {"drive": 0, "prepared": 0}


class TestNotAtResumeOrReplay:
    """Resume and replay use the journaled spec snapshot, so they do not re-check."""

    @pytest.mark.parametrize(
        "function", ["resume_from_last_completed", "replay_single_step", "start_run_prepared"]
    )
    def test_the_check_is_not_called(self, function: str) -> None:
        import inspect

        source = inspect.getsource(getattr(workflow_service, function))

        assert "step_params" not in source

    def test_start_run_calls_it(self) -> None:
        import inspect

        assert "step_params.refuse_step_param_errors" in inspect.getsource(
            workflow_service.start_run
        )
