"""Keep the path-aware gate's change range and filters trustworthy."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import ci_changed_paths
from scripts.ci_changed_paths import FILTERS, changed_revisions, matching_filters

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
SOURCES = {
    "antigravity": "test-antigravity-cli-provider.yml",
    "claude": "test-claude-code-provider.yml",
    "codex": "test-codex-provider.yml",
    "kiro": "test-kiro-cli-provider.yml",
    "docs": "gh-pages.yml",
}


@pytest.mark.parametrize(("name", "filename"), SOURCES.items())
def test_filters_cover_original_workflow_paths(name: str, filename: str):
    spec = yaml.safe_load((WORKFLOWS / filename).read_text())
    triggers = spec.get("on", spec.get(True))
    for trigger in ("push", "pull_request"):
        if trigger in triggers:
            assert set(triggers[trigger]["paths"]) <= set(FILTERS[name])


@pytest.mark.parametrize("name", ("transport", "factory"))
def test_robotics_filters_cover_example_and_shared_sources(name: str):
    assert any(
        pattern.startswith(
            f"examples/robotics/{'cross-zone-transport' if name == 'transport' else 'hierarchical-factory'}/"
        )
        for pattern in FILTERS[name]
    )
    assert "src/cli_agent_orchestrator/models/provider.py" in FILTERS[name]


def test_changes_to_detector_or_ci_trigger_every_filtered_job():
    assert all(matching_filters(["scripts/ci_changed_paths.py"]).values())
    assert all(matching_filters([".github/workflows/ci.yml"]).values())


def test_merge_group_uses_group_commit_range():
    assert changed_revisions(
        "merge_group", {"merge_group": {"base_sha": "base", "head_sha": "group"}}
    ) == ("base", "group")


def test_pr_uses_checkout_merge_commit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_SHA", "merge")
    assert changed_revisions("pull_request", {"pull_request": {"base": {"sha": "base"}}}) == (
        "base",
        "merge",
    )


def test_push_uses_before_and_after():
    assert changed_revisions("push", {"before": "before", "after": "after"}) == ("before", "after")


def test_filter_matches_nested_docs_and_does_not_match_unrelated():
    assert matching_filters(["docusaurus/src/index.ts"])["docs"]
    assert not any(matching_filters(["README.md"]).values())


@pytest.mark.parametrize("filename", ("ci.yml", "secret-scan.yml", "zizmor.yml", "cargo-deny.yml"))
def test_required_workflows_run_for_merge_groups(filename: str):
    spec = yaml.safe_load((WORKFLOWS / filename).read_text())
    triggers = spec.get("on", spec.get(True))
    assert "merge_group" in triggers


def test_secret_scan_covers_the_merge_group_commit_range():
    scan = (WORKFLOWS / "secret-scan.yml").read_text()
    assert "github.event.merge_group.base_sha" in scan
    assert "github.event.merge_group.head_sha" in scan
    assert "github.event_name == 'merge_group'" in scan


def test_merge_group_emits_path_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"merge_group": {"base_sha": "base", "head_sha": "group"}}))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "merge_group")

    def changed_files(command: list[str], **kwargs: object) -> bytes:
        assert command == ["git", "diff", "--name-only", "-z", "base", "group"]
        return b"docusaurus/src/index.ts\0"

    monkeypatch.setattr(ci_changed_paths.subprocess, "check_output", changed_files)
    ci_changed_paths.main()
    assert "docs=true\n" in output.read_text()
    assert "kiro=false\n" in output.read_text()


def test_unreadable_range_does_not_emit_skip_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"merge_group": {"base_sha": "base", "head_sha": "group"}}))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "merge_group")

    def unreadable_range(command: list[str], **kwargs: object) -> bytes:
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(ci_changed_paths.subprocess, "check_output", unreadable_range)
    with pytest.raises(subprocess.CalledProcessError):
        ci_changed_paths.main()
    assert not output.exists()
