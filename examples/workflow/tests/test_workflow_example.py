"""Deterministic transport-level tests for the workflow example (issue #591).

Each example script under docs/examples/ has a matching e2e test that spawns
it as a real subprocess via ``run_script_workflow`` against a fake
``/terminals/run-step`` server (test/e2e/examples/test_examples_gallery_e2e.py).
This module applies the same proof to examples/workflow/workflow.py, plus the
input-validation gate that runs before any of that: ``_validate_inputs`` is
the same function the ``/workflows/runs`` and ``/workflows/runs:submit``
routes call BEFORE creating a journal row or a subprocess (script_runner.py's
``ScriptLintError``/``run_script_workflow`` docstrings), so testing it
directly proves rejection happens before any worker/step exists.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from cli_agent_orchestrator.models.workflow_runtime import RunState
from cli_agent_orchestrator.services.script_lint import lint_script
from cli_agent_orchestrator.services.script_runner import build_env, run_script_workflow
from cli_agent_orchestrator.services.workflow_service import _validate_inputs
from cli_agent_orchestrator.services.workflow_spec_service import _extract_inputs

_WORKFLOW_PATH = Path(__file__).resolve().parents[1] / "workflow.py"
_WORKFLOW_SOURCE = _WORKFLOW_PATH.read_text(encoding="utf-8")


def _load_workflow_module():
    """Import workflow.py from its path, for the few pure helpers worth unit-testing.

    Everything else here drives the script as a real subprocess; this exists so a
    pure function can be tested as one without putting examples/ on sys.path.
    """
    spec = importlib.util.spec_from_file_location("_workflow_under_test", _WORKFLOW_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _SpecStub:
    """Minimal ``_HasInputs`` duck type — just enough for ``_validate_inputs``."""

    def __init__(self, inputs):
        self.inputs = inputs


class _RealSpec:
    """Duck-typed ScriptSpec pointing at the real workflow.py on disk."""

    def __init__(self, path: Path, name: str):
        self.path = str(path)
        self.source = path.read_text(encoding="utf-8")
        self.name = name
        self.content_hash = "workflow-example-test"


def _resolved_inputs(overrides):
    """Run the real ``_extract_inputs`` + ``_validate_inputs`` pipeline."""
    spec = _SpecStub(_extract_inputs(_WORKFLOW_SOURCE))
    return _validate_inputs(spec, overrides)


def test_validate_passes_lint():
    """``cao workflow validate`` calls ``lint_script`` for a .py spec (the
    ``/workflows/validate`` route's .py arm) — it must report ``pass``."""
    result = lint_script(_WORKFLOW_SOURCE, str(_WORKFLOW_PATH))

    assert result.status == "pass"
    assert result.errors == []


def test_missing_required_input_rejected():
    """A run with no ``target`` is rejected before any worker/step exists."""
    with pytest.raises(ValueError, match="missing required input 'target'"):
        _resolved_inputs({})


def test_valid_inputs_resolve_with_declared_defaults():
    resolved = _resolved_inputs({"target": "myapp"})

    assert resolved == {"target": "myapp", "concurrency": 2, "strict": False}


@pytest.mark.asyncio
async def test_concurrent_fanout_uses_pairwise_distinct_step_ids(
    api_base_url, fake_run_step_server
):
    spec = _RealSpec(_WORKFLOW_PATH, "workflow-example")
    inputs = _resolved_inputs({"target": "myapp"})

    result = await run_script_workflow(spec, inputs, "test-fanout")

    assert result.state == RunState.COMPLETED
    step_ids = [c["env_vars"]["CAO_WORKFLOW_STEP_ID"] for c in fake_run_step_server.recorded_calls]
    assert step_ids[0] == "plan-myapp"  # the sequential step runs before the fan-out
    assert sorted(step_ids[1:]) == [
        "check-myapp-performance",
        "check-myapp-security",
        "check-myapp-style",
    ]
    assert len(set(step_ids)) == len(step_ids)  # pairwise distinct across the whole run
    # The fake server doesn't enforce this (unlike the real RunStepRequest
    # validator), so assert it directly — every step_id must be a value the
    # real /terminals/run-step route's env_vars validator would accept.
    for step_id in step_ids:
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", step_id), step_id
    assert result.output["checks"].keys() == {"style", "security", "performance"}
    assert result.output["failed_checks"] == []
    # concurrency defaulted to 2 and the cap is a no-op at the default itself.
    assert result.output["max_workers"] == 2


@pytest.mark.asyncio
async def test_overlong_target_stays_inside_the_step_id_length_bound(
    api_base_url, fake_run_step_server
):
    """The 1-64 half of the step_id contract, which a "myapp" target never reaches.

    The charset assertion above is already written as ``{1,64}``, but only a
    target long enough to overflow the budget actually exercises the length half.
    """
    target = "svc/" + ("long-target-name" * 8)  # unsafe chars AND far over the bound
    spec = _RealSpec(_WORKFLOW_PATH, "workflow-example")
    inputs = _resolved_inputs({"target": target})

    result = await run_script_workflow(spec, inputs, "test-longtarget")

    assert result.state == RunState.COMPLETED
    step_ids = [c["env_vars"]["CAO_WORKFLOW_STEP_ID"] for c in fake_run_step_server.recorded_calls]
    assert len(step_ids) == 4  # one plan + three checks
    for step_id in step_ids:
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", step_id), step_id
    assert len(set(step_ids)) == len(step_ids)


def test_slug_truncation_is_collision_safe_and_stable():
    """Bare truncation would collapse targets sharing a prefix onto one step_id.

    That is why ``_slug`` spends its tail on a digest. Both properties matter:
    distinctness (a fan-out must not reuse a step_id) and stability across
    processes (replay keys on the step_id, so a per-process value would break it).
    """
    slug = _load_workflow_module()._slug
    prefix = "p" * 60

    assert slug(prefix + "AAAA") != slug(prefix + "BBBB")
    assert slug(prefix + "AAAA") == slug(prefix + "AAAA")
    # Short targets keep the plain readable slug — no digest, so this change
    # cannot alter step_ids for any target that already fit.
    assert slug("myapp") == "myapp"
    assert slug("my app/v2") == "my_app_v2"


@pytest.mark.asyncio
async def test_one_failed_check_does_not_lose_survivors(api_base_url, fake_run_step_server):
    fake_run_step_server.fail_step_ids.add("check-myapp-security")
    spec = _RealSpec(_WORKFLOW_PATH, "workflow-example")
    # concurrency=3 is a VALID input, but the script conservatively caps
    # max_workers at the declared default (2) regardless.
    inputs = _resolved_inputs({"target": "myapp", "concurrency": 3})

    result = await run_script_workflow(spec, inputs, "test-fanout-failure")

    assert result.state == RunState.COMPLETED  # a per-unit failure never fails the run
    assert result.output["max_workers"] == 2
    assert result.output["failed_checks"] == ["security"]
    assert set(result.output["checks"].keys()) == {"style", "performance"}
    assert result.output["checks"]["style"] == "ack:check-myapp-style"


def test_emit_output_sentinel_in_subprocess_stdout(api_base_url, fake_run_step_server):
    """Spawn workflow.py exactly as ``script_runner._drive_process`` does
    (same interpreter, same constructed env) and check the sentinel is on
    stdout literally — the property ``run_script_workflow``'s parsed
    ``.output`` proves indirectly, asserted directly here."""
    inputs = _resolved_inputs({"target": "myapp"})
    env = build_env("test-stdout", "1", inputs)

    proc = subprocess.run(
        [sys.executable, str(_WORKFLOW_PATH)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    sentinel_lines = [
        line for line in proc.stdout.splitlines() if line.startswith("CAO_WORKFLOW_OUTPUT:")
    ]
    assert len(sentinel_lines) == 1
    payload = json.loads(sentinel_lines[0][len("CAO_WORKFLOW_OUTPUT:") :])
    assert payload["target"] == "myapp"
