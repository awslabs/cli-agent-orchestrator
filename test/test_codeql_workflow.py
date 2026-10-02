"""CodeQL PR coverage and least-privilege workflow contracts (#857)."""

from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "codeql.yml"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_main_changes_are_scanned_without_path_filters(workflow, event):
    # PyYAML uses YAML 1.1, where the unquoted Actions key `on` is a boolean.
    events = workflow.get("on", workflow.get(True))
    assert events[event] == {"branches": ["main"]}
    assert "pull_request_target" not in events


def test_scheduled_and_manual_scans_remain_available(workflow):
    events = workflow.get("on", workflow.get(True))
    assert events["schedule"] == [{"cron": "0 8 * * 1"}]
    assert "workflow_dispatch" in events


def test_all_configured_languages_run_without_a_fork_condition(workflow):
    job = workflow["jobs"]["analyze"]
    assert set(job["strategy"]["matrix"]["language"]) == {
        "actions",
        "javascript-typescript",
        "python",
        "rust",
    }
    assert job["strategy"]["fail-fast"] is False
    assert job["name"] == "CodeQL (${{ matrix.language }})"
    assert job["runs-on"] == "ubuntu-latest"
    assert "if" not in job
    assert "continue-on-error" not in job


def test_scan_uses_only_the_builtin_token_and_does_not_execute_project_code(workflow):
    job = workflow["jobs"]["analyze"]
    assert workflow["permissions"] == {"contents": "read"}
    assert job["permissions"] == {
        "actions": "read",
        "contents": "read",
        "security-events": "write",
    }
    steps = job["steps"]
    assert all("run" not in step for step in steps)
    assert all("if" not in step and "continue-on-error" not in step for step in steps)
    assert "secrets." not in WORKFLOW.read_text(encoding="utf-8")
    assert all("token" not in step.get("with", {}) for step in steps)

    checkout = next(step for step in steps if step["uses"].startswith("actions/checkout@"))
    assert checkout["with"] == {"persist-credentials": False}


def test_queries_run_and_upload_against_the_default_pr_merge_revision(workflow):
    steps = workflow["jobs"]["analyze"]["steps"]
    init = next(step for step in steps if step["uses"].startswith("github/codeql-action/init@"))
    assert init["with"] == {"languages": "${{ matrix.language }}", "build-mode": "none"}

    analyze = next(
        step for step in steps if step["uses"].startswith("github/codeql-action/analyze@")
    )
    assert analyze["with"] == {
        "category": "/language:${{ matrix.language }}",
        "wait-for-processing": True,
    }
