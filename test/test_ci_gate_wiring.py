"""Keep the advisory CI aggregate wired to every job it summarizes."""

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DEPENDENCY_REVIEW_RUN_IF = (
    "github.event_name == 'pull_request' "
    "&& github.repository == 'awslabs/cli-agent-orchestrator'"
)


def _ci_jobs() -> dict[str, dict]:
    spec = yaml.safe_load(CI_WORKFLOW.read_text())
    assert isinstance(spec, dict)
    jobs = spec.get("jobs")
    assert isinstance(jobs, dict)
    return jobs


def test_ci_gate_needs_every_other_ci_job():
    """A newly added job must not silently escape the aggregate verdict."""
    jobs = _ci_jobs()
    gate = jobs["ci-gate"]
    assert set(gate["needs"]) == set(jobs) - {"ci-gate"}


def test_ci_gate_dependency_review_skip_mirrors_its_job_condition():
    """The aggregate allows its skip exactly when its job-level condition is false."""
    jobs = _ci_jobs()
    gate = jobs["ci-gate"]
    assert jobs["dependency-review"]["if"] == DEPENDENCY_REVIEW_RUN_IF
    assert gate["env"]["REPOSITORY"] == "${{ github.repository }}"

    run = gate["steps"][0]["run"]
    assert 'job_id == "dependency-review"' in run
    assert 'event_name != "pull_request"' in run
    assert 'repository != "awslabs/cli-agent-orchestrator"' in run
