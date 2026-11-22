"""Keep the advisory CI aggregate wired to every job it summarizes."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

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


def _gate_exit_code(
    needs: dict[str, str], event_name: str, repository: str, workflow: Path = CI_WORKFLOW
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


def _expected_exit_code(needs: dict[str, str], event_name: str, repository: str) -> int:
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
    assert set(gate["needs"]) == set(jobs) - {"ci-gate"}


def test_ci_gate_must_run_with_minimal_permissions_and_no_checkout():
    """A required future gate must still report after a failed or cancelled job."""
    gate = _ci_jobs()["ci-gate"]
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
            'and job_id == "dependency-review"',
            "and True",
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
    assert source.count(before) == 1
    mutant.write_text(source.replace(before, after))

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
