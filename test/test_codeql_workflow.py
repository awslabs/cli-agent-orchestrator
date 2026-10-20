"""CodeQL PR coverage and least-privilege workflow contracts (#857)."""

from pathlib import Path

import pytest
import yaml

GITHUB = Path(__file__).resolve().parents[1] / ".github"
WORKFLOWS = GITHUB / "workflows"
SCAN_ACTION = GITHUB / "actions" / "codeql" / "action.yml"


@pytest.fixture(scope="module")
def ci_workflow():
    return yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def scheduled_workflow():
    return yaml.safe_load((WORKFLOWS / "codeql.yml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module", params=[("ci.yml", "codeql"), ("codeql.yml", "analyze")])
def scan_job(request):
    filename, job_id = request.param
    workflow = yaml.safe_load((WORKFLOWS / filename).read_text(encoding="utf-8"))
    return workflow["jobs"][job_id]


@pytest.fixture(scope="module")
def scan_action():
    return yaml.safe_load(SCAN_ACTION.read_text(encoding="utf-8"))


@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_main_changes_are_scanned_without_path_filters(ci_workflow, event):
    # PyYAML uses YAML 1.1, where the unquoted Actions key `on` is a boolean.
    events = ci_workflow.get("on", ci_workflow.get(True))
    assert events[event] == {"branches": ["main"]}
    assert "pull_request_target" not in events


def test_codeql_is_part_of_each_ci_run(ci_workflow):
    assert "codeql" in ci_workflow["jobs"]
    job = ci_workflow["jobs"]["codeql"]
    assert "uses" not in job
    assert "needs" not in job
    assert "if" not in job


def test_scheduled_and_manual_scans_do_not_duplicate_ci(scheduled_workflow):
    events = scheduled_workflow.get("on", scheduled_workflow.get(True))
    assert set(events) == {"schedule", "workflow_dispatch"}
    assert events["schedule"] == [{"cron": "0 8 * * 1"}]
    assert scheduled_workflow["permissions"] == {"contents": "read"}


def test_ci_and_scheduled_scans_have_the_same_contract(ci_workflow, scheduled_workflow):
    assert ci_workflow["jobs"]["codeql"] == scheduled_workflow["jobs"]["analyze"]


def test_all_configured_languages_run_without_a_fork_condition(scan_job):
    assert set(scan_job["strategy"]["matrix"]["language"]) == {
        "actions",
        "javascript-typescript",
        "python",
        "rust",
    }
    assert scan_job["strategy"]["fail-fast"] is False
    assert scan_job["name"] == "CodeQL (${{ matrix.language }})"
    assert scan_job["runs-on"] == "ubuntu-latest"
    assert scan_job["timeout-minutes"] == 30
    assert "if" not in scan_job
    assert "needs" not in scan_job
    assert "continue-on-error" not in scan_job


def test_scan_uses_only_the_builtin_token_and_does_not_execute_project_code(scan_job, scan_action):
    assert scan_job["permissions"] == {
        "actions": "read",
        "contents": "read",
        "security-events": "write",
    }
    steps = scan_job["steps"] + scan_action["runs"]["steps"]
    assert all("run" not in step for step in steps)
    assert all("if" not in step and "continue-on-error" not in step for step in steps)
    assert "secrets." not in yaml.safe_dump(scan_job)
    assert "secrets." not in SCAN_ACTION.read_text(encoding="utf-8")
    assert all("token" not in step.get("with", {}) for step in steps)

    checkout, scan = scan_job["steps"]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {"persist-credentials": False}
    assert scan["uses"] == "./.github/actions/codeql"
    assert scan["with"] == {"language": "${{ matrix.language }}"}


def test_queries_run_and_upload_against_the_default_pr_merge_revision(scan_action):
    assert scan_action["runs"]["using"] == "composite"
    assert set(scan_action["inputs"]) == {"language"}
    assert scan_action["inputs"]["language"]["required"] is True
    steps = scan_action["runs"]["steps"]
    assert len(steps) == 2
    init = next(step for step in steps if step["uses"].startswith("github/codeql-action/init@"))
    assert init["with"] == {"languages": "${{ inputs.language }}", "build-mode": "none"}

    analyze = next(
        step for step in steps if step["uses"].startswith("github/codeql-action/analyze@")
    )
    assert analyze["with"] == {
        "category": "/language:${{ inputs.language }}",
        "wait-for-processing": True,
    }


@pytest.mark.parametrize(
    "pattern", ["/.github/workflows/", "/.github/actions/", "/.github/CODEOWNERS"]
)
def test_ci_definitions_have_repository_maintainer_ownership(pattern):
    codeowners = (GITHUB / "CODEOWNERS").read_text(encoding="utf-8")
    entries = [line.split("#", 1)[0].split() for line in codeowners.splitlines()]
    assert [pattern, "@awslabs/multiq"] in entries
