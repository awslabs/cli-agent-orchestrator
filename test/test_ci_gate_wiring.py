"""Keep the advisory CI aggregate wired to every job it summarizes."""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.ci_changed_paths import FILTERS

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
UPSTREAM_REPOSITORY = "awslabs/cli-agent-orchestrator"


def _ci_jobs(workflow: Path = CI_WORKFLOW) -> dict[str, dict]:
    spec = yaml.safe_load(workflow.read_text())
    assert isinstance(spec, dict)
    jobs = spec.get("jobs")
    assert isinstance(jobs, dict)
    return jobs


def _gate_python(workflow: Path = CI_WORKFLOW) -> str:
    """Extract the inline Python program, without running the shell wrapper."""
    run = _ci_jobs(workflow)["ci-gate"]["steps"][0]["run"]
    _, marker, program = run.partition("python3 - <<'PY'\n")
    assert marker, "ci-gate must run its inline Python through python3 -"
    program, marker, _ = program.partition("\nPY")
    assert marker, "ci-gate's inline Python heredoc must end with PY"
    return program


def _filtered_python(workflow: Path = CI_WORKFLOW) -> str:
    run = _ci_jobs(workflow)["filtered-gate"]["steps"][0]["run"]
    return run.split("python3 - <<'PY'\n", 1)[1].split("\nPY", 1)[0]


def _gate_exit_code(
    needs: dict[str, str],
    event_name: str,
    repository: str,
    workflow: Path = CI_WORKFLOW,
) -> int:
    """Run the gate program against the supplied GitHub Actions inputs."""
    environment = {
        "NEEDS_JSON": json.dumps({job_id: {"result": result} for job_id, result in needs.items()}),
        "EVENT_NAME": event_name,
        "REPOSITORY": repository,
    }
    result = subprocess.run(
        [sys.executable, "-"],
        input=_gate_python(workflow),
        text=True,
        env={**os.environ, **environment},
        capture_output=True,
        check=False,
    )
    return result.returncode


def _filtered_exit_code(needs: dict[str, str], paths: dict[str, str]) -> int:
    outcomes = {
        "changed-paths": {"result": needs.get("changed-paths", "success"), "outputs": paths},
        **{
            job_id: {"result": result}
            for job_id, result in needs.items()
            if job_id != "changed-paths"
        },
    }
    result = subprocess.run(
        [sys.executable, "-"],
        input=_filtered_python(),
        text=True,
        env={**os.environ, "NEEDS_JSON": json.dumps(outcomes)},
        capture_output=True,
        check=False,
    )
    return result.returncode


def _expected_exit_code(
    needs: dict[str, str],
    event_name: str,
    repository: str,
) -> int:
    """The documented gate rule, deliberately separate from ci.yml's program."""
    for job_id, result in needs.items():
        dependency_review_skip_is_allowed = (
            job_id == "dependency-review"
            and result == "skipped"
            and (event_name != "pull_request" or repository != UPSTREAM_REPOSITORY)
        )
        if result != "success" and not dependency_review_skip_is_allowed:
            return 1
    return 0


GATE_CASE_DATA = [
    ({}, "push", UPSTREAM_REPOSITORY),
    ({"test": "success"}, "push", UPSTREAM_REPOSITORY),
    ({"test": "success", "lint": "success"}, "pull_request", UPSTREAM_REPOSITORY),
    *[
        ({"test": result}, "pull_request", UPSTREAM_REPOSITORY)
        for result in ("failure", "cancelled", "timed_out", "skipped")
    ],
    ({"test": "skipped"}, "push", UPSTREAM_REPOSITORY),
    ({"filtered-gate": "success"}, "merge_group", UPSTREAM_REPOSITORY),
    ({"filtered-gate": "failure"}, "merge_group", UPSTREAM_REPOSITORY),
    *[
        ({"dependency-review": result}, event_name, repository)
        for result in ("success", "failure", "cancelled", "timed_out", "skipped")
        for event_name, repository in (
            ("push", UPSTREAM_REPOSITORY),
            ("pull_request", UPSTREAM_REPOSITORY),
            ("pull_request", "fork-owner/cli-agent-orchestrator"),
        )
    ],
]


def test_ci_gate_needs_every_other_ci_job():
    """A newly added job must not silently escape the aggregate verdict."""
    jobs = _ci_jobs()
    gate = jobs["ci-gate"]
    filtered = set(jobs["filtered-gate"]["needs"]) - {"changed-paths"}
    assert set(gate["needs"]) == set(jobs) - filtered - {"ci-gate"}
    assert filtered == {
        job_id for job_id, job in jobs.items() if job.get("needs") == "changed-paths"
    }
    assert (
        len(gate["needs"]) <= 20
    ), "Put a new top-level CI job behind an intermediate gate so ci-gate's needs stays bounded and readable."
    assert (
        len(jobs["filtered-gate"]["needs"]) <= 20
    ), "Put a new top-level CI job behind an intermediate gate so ci-gate's needs stays bounded and readable."


def _filtered_jobs_mapping() -> dict[str, str]:
    program = ast.parse(_filtered_python())
    return next(
        ast.literal_eval(statement.value)
        for statement in program.body
        if isinstance(statement, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "filtered_jobs"
            for target in statement.targets
        )
    )


def test_every_filtered_condition_and_gate_mapping_refer_to_the_same_filter():
    jobs = _ci_jobs()
    mapping = _filtered_jobs_mapping()
    expected = {
        **{
            job_id: name
            for name in ("antigravity", "claude", "codex", "kiro")
            for job_id in (f"provider-{name}", f"provider-{name}-lint")
        },
        "robotics-transport": "transport",
        "robotics-factory": "factory",
        "docs-build": "docs",
    }
    assert mapping == expected
    assert set(jobs["filtered-gate"]["needs"]) == set(mapping) | {"changed-paths"}
    assert set(jobs["changed-paths"]["outputs"]) == set(FILTERS)
    for job_id, filter_name in mapping.items():
        assert jobs[job_id]["if"] == (
            "${{ needs.changed-paths.outputs." + filter_name + " == 'true' }}"
        )


def test_all_new_jobs_hold_only_the_permissions_they_need():
    jobs = _ci_jobs()
    for job_id in _filtered_jobs_mapping() | {"changed-paths": ""}:
        assert jobs[job_id]["permissions"] == {"contents": "read"}
    for gate_id in ("filtered-gate", "ci-gate"):
        assert jobs[gate_id]["permissions"] == {}


@pytest.mark.parametrize(
    ("job_id", "target"),
    [
        ("provider-antigravity", "test/providers/test_antigravity_cli_*.py"),
        ("provider-claude", "test/providers/test_claude_code_*.py"),
        ("provider-claude", "test/providers/test_busy_pattern_settled_output_guard.py"),
        ("provider-codex", "test/providers/test_codex_*.py"),
        ("provider-kiro", "test/providers/test_kiro_cli_*.py"),
        ("robotics-transport", "examples/robotics/cross-zone-transport"),
        ("robotics-factory", "examples/robotics/hierarchical-factory"),
    ],
)
def test_moved_jobs_keep_the_filtered_test_targets(job_id: str, target: str):
    job = _ci_jobs()[job_id]
    runs = "\n".join(str(step.get("run", "")) for step in job["steps"])
    assert target in runs or any(step.get("working-directory") == target for step in job["steps"])


def test_ci_gate_must_run_with_minimal_permissions_and_no_checkout():
    """A required future gate must still report after a failed or cancelled job."""
    for gate_id in ("ci-gate", "filtered-gate"):
        gate = _ci_jobs()[gate_id]
        assert gate["if"] == "${{ always() }}"
        assert gate["permissions"] == {}
        assert all(
            not step.get("uses", "").startswith("actions/checkout@")
            for step in gate["steps"]
            if isinstance(step, dict)
        )
        assert "${{" not in gate["steps"][0]["run"]


@pytest.mark.parametrize(("needs", "event_name", "repository"), GATE_CASE_DATA)
def test_ci_gate_exit_code_matches_the_documented_rule(
    needs: dict[str, str], event_name: str, repository: str
):
    assert _gate_exit_code(needs, event_name, repository) == _expected_exit_code(
        needs, event_name, repository
    )


@pytest.mark.parametrize("job_id, filter_name", _filtered_jobs_mapping().items())
def test_filtered_job_verdict(job_id: str, filter_name: str):
    for result, path_value, expected in (
        ("success", "true", 0),
        ("failure", "true", 1),
        ("skipped", "false", 0),
        ("skipped", "true", 1),
        ("skipped", "", 1),
        ("failure", "false", 1),
        ("cancelled", "false", 1),
    ):
        assert _filtered_exit_code({job_id: result}, {filter_name: path_value}) == expected


def test_changed_paths_failure_never_allows_a_skip():
    assert (
        _filtered_exit_code(
            {"changed-paths": "failure", "docs-build": "skipped"}, {"docs": "false"}
        )
        == 1
    )


@pytest.mark.parametrize(
    ("name", "before", "after"),
    [
        (
            "dependency-review condition requires both exceptions",
            'or repository != "awslabs/cli-agent-orchestrator"',
            'and repository != "awslabs/cli-agent-orchestrator"',
        ),
        ("failures exit nonzero", "sys.exit(1)", "sys.exit(0)"),
        (
            "cancelled and skipped jobs fail",
            'result != "success"',
            'result == "failure"',
        ),
        (
            "only dependency-review may skip",
            'job_id == "dependency-review"',
            "True",
        ),
        ("always gate condition is retained", "if: ${{ always() }}", ""),
        (
            "always gate condition is not replaced",
            "if: ${{ always() }}",
            "if: ${{ !cancelled() }}",
        ),
    ],
)
def test_ci_gate_mutants_fail_the_gate_contract(tmp_path: Path, name: str, before: str, after: str):
    """Each review mutant must violate an assertion above on a scratch workflow."""
    mutant = tmp_path / f"{name}.yml"
    source = CI_WORKFLOW.read_text()
    prefix, separator, gate_source = source.partition("  ci-gate:\n")
    assert separator and gate_source.count(before) == 1
    mutant.write_text(prefix + separator + gate_source.replace(before, after))

    if name.startswith("always gate"):
        assert _ci_jobs(mutant)["ci-gate"].get("if") != "${{ always() }}"
        return

    mismatches = [
        (needs, event_name, repository)
        for needs, event_name, repository in GATE_CASE_DATA
        if _gate_exit_code(needs, event_name, repository, mutant)
        != _expected_exit_code(needs, event_name, repository)
    ]
    assert mismatches, f"{name} unexpectedly passed the independent rule grid"
