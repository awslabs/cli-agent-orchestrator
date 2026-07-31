"""Tests for the deterministic PR-health workflow example."""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path
from types import ModuleType

from cli_agent_orchestrator.services.flow_service import _parse_flow_file
from cli_agent_orchestrator.services.script_lint import lint_script

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_DIR = REPO_ROOT / "examples" / "workflows" / "pr-health"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_workflow_passes_static_validation_and_embedded_cases() -> None:
    path = EXAMPLE_DIR / "pr_health.py"
    result = lint_script(path.read_text(encoding="utf-8"), str(path))

    assert result.status == "pass"
    assert result.findings == []

    workflow = _load_module("pr_health_example", path)
    workflow._run_self_tests()
    assert workflow._repo_storage_key("a--b/c") != workflow._repo_storage_key("a/b--c")

    try:
        workflow._validate_as_of("2026-07-31T00:00:00Z")
    except ValueError as exc:
        assert "YYYY-MM-DD" in str(exc)
    else:
        raise AssertionError("timestamp-shaped as_of input must be rejected")


def test_guard_enforces_exact_fourteen_day_cadence() -> None:
    guard = _load_module(
        "pr_health_biweekly_guard",
        EXAMPLE_DIR / "pr_health_biweekly_guard.py",
    )

    assert guard.is_due(date(2026, 1, 5))
    assert not guard.is_due(date(2026, 1, 12))
    assert guard.is_due(date(2026, 1, 19))
    assert guard.is_due(date(2027, 1, 18))


def test_scheduled_flow_defaults_to_non_mutating_mode() -> None:
    flow_path = EXAMPLE_DIR / "pr-health-biweekly.md"
    metadata, prompt = _parse_flow_file(flow_path)

    assert metadata["schedule"] == "0 9 * * 0"
    assert metadata["script"] == "./pr_health_biweekly_guard.py"
    assert (flow_path.parent / metadata["script"]).is_file()
    assert "--input mode=dry_run" in prompt
    assert "--input close_allowlist=" in prompt
    assert "--input mode=apply" not in prompt


def test_apply_schedule_requires_explicit_template_and_disables_closure() -> None:
    flow_path = EXAMPLE_DIR / "pr-health-biweekly-apply.md"
    metadata, prompt = _parse_flow_file(flow_path)

    assert metadata["schedule"] == "0 9 * * 0"
    assert metadata["script"] == "./pr_health_biweekly_guard.py"
    assert "--input mode=apply" in prompt
    assert "--input close_allowlist=" in prompt
    assert "Closure is not authorized" in prompt


def test_enforcement_skips_non_open_pr_before_mutation(monkeypatch, tmp_path: Path) -> None:
    workflow = _load_module("pr_health_enforcement_example", EXAMPLE_DIR / "pr_health.py")
    commands = []
    monkeypatch.setattr(
        workflow,
        "_fetch_pr",
        lambda _repo, _number: {"state": "CLOSED", "comments": []},
    )
    monkeypatch.setattr(workflow, "_run_gh_command", lambda args: commands.append(args))

    results = workflow._apply_recommendations(
        "owner/repo",
        "2026-07-31",
        [{"number": 7, "score": 40, "recommended_action": "warn_owner"}],
        {"prs": {}},
        set(),
        tmp_path / "enforcement.json",
    )

    assert results == [
        {
            "number": 7,
            "planned_action": "warn_owner",
            "status": "skipped_not_open",
        }
    ]
    assert commands == []
