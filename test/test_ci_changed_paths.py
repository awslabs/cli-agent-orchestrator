"""Keep the path-aware gate's change range and filters trustworthy."""

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import ci_changed_paths
from scripts.ci_changed_paths import FILTERS, changed_revisions, matching_filters

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
PROVIDER_PATHS = {
    "antigravity": {
        "src/cli_agent_orchestrator/providers/antigravity_cli.py",
        "test/providers/test_antigravity_cli_*.py",
        "test/providers/fixtures/agy_*.txt",
        "pyproject.toml",
    },
    "claude": {
        "src/cli_agent_orchestrator/providers/claude_code.py",
        "test/providers/test_claude_code_*.py",
        "test/providers/test_busy_pattern_settled_output_guard.py",
        "pyproject.toml",
    },
    "codex": {
        "src/cli_agent_orchestrator/providers/codex.py",
        "test/providers/test_codex_*.py",
        "test/providers/fixtures/codex_*.txt",
        "pyproject.toml",
    },
    "kiro": {
        "src/cli_agent_orchestrator/providers/kiro_cli.py",
        "test/providers/test_kiro_cli_*.py",
        "test/providers/fixtures/kiro_cli_*.txt",
        "pyproject.toml",
    },
}
PROVIDER_WORKFLOWS = {
    "antigravity": "test-antigravity-cli-provider.yml",
    "claude": "test-claude-code-provider.yml",
    "codex": "test-codex-provider.yml",
    "kiro": "test-kiro-cli-provider.yml",
}


@pytest.mark.parametrize(("name", "paths"), PROVIDER_PATHS.items())
def test_provider_filters_cover_source_paths_without_dead_workflows(name: str, paths: set[str]):
    assert set(FILTERS[name]) == paths | {
        ".github/workflows/ci.yml",
        "scripts/ci_changed_paths.py",
    }
    assert not (WORKFLOWS / PROVIDER_WORKFLOWS[name]).exists()


def test_filters_do_not_reference_deleted_robotics_workflows():
    assert all(
        not pattern.endswith(("robotics-transport.yml", "robotics-factory.yml"))
        for patterns in FILTERS.values()
        for pattern in patterns
    )


def test_filters_do_not_use_double_star_slash():
    assert all(
        "**/" not in pattern for patterns in FILTERS.values() for pattern in patterns
    ), "GitHub paths treats **/ as zero or more folders, but Python fnmatch does not"


@pytest.mark.parametrize("name", ("transport", "factory"))
def test_robotics_filters_cover_example_and_shared_sources(name: str):
    example = "cross-zone-transport" if name == "transport" else "hierarchical-factory"
    assert {
        f"examples/robotics/{example}/**",
        "src/cli_agent_orchestrator/models/provider.py",
        "src/cli_agent_orchestrator/schemas/agent_profile.schema.json",
        "src/cli_agent_orchestrator/utils/enforcement.py",
    } <= set(FILTERS[name])


def test_changes_to_detector_or_ci_trigger_every_filtered_job():
    assert all(matching_filters(["scripts/ci_changed_paths.py"]).values())
    assert all(matching_filters([".github/workflows/ci.yml"]).values())


def test_changed_paths_detector_requires_workflow_code_owner_review():
    entries = [
        line.split("#", 1)[0].split()
        for line in (WORKFLOWS.parent / "CODEOWNERS").read_text().splitlines()
    ]
    assert ["/.github/workflows/", "@awslabs/multiq"] in entries
    assert ["/scripts/ci_changed_paths.py", "@awslabs/multiq"] in entries


def test_docs_pr_build_does_not_share_production_deploy_concurrency_group():
    doc = yaml.safe_load((WORKFLOWS / "gh-pages.yml").read_text())
    assert doc["concurrency"]["group"] == (
        "${{ github.event_name == 'pull_request' && format('docs-pr-{0}', github.ref) || 'pages' }}"
    )
    assert doc["concurrency"]["cancel-in-progress"] is False


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


def _required_workflow_files() -> set[str]:
    skill = WORKFLOWS.parents[1] / "skills/cao-contributing/references/pr-gating-workflows.md"
    rows = dict(
        re.findall(
            r"^\| \*\*(Secret Scan|cargo-deny|Actions Security)\*\* \(`([^`]+)`\)",
            skill.read_text(),
            re.MULTILINE,
        )
    )
    assert set(rows) == {"Secret Scan", "cargo-deny", "Actions Security"}
    return {"ci.yml", *rows.values()}


@pytest.mark.parametrize("filename", sorted(_required_workflow_files()))
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
        assert command == ["git", "diff", "--name-only", "--no-renames", "-z", "base", "group"]
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


def test_renamed_away_provider_file_still_selects_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    original = tmp_path / "src/cli_agent_orchestrator/providers/codex.py"
    original.parent.mkdir(parents=True)
    original.write_text("original provider contents\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    commit = [
        "git",
        "-c",
        "user.name=CI Test",
        "-c",
        "user.email=ci@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        "test",
    ]
    subprocess.run(commit, cwd=tmp_path, check=True)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path).decode().strip()
    subprocess.run(
        [
            "git",
            "mv",
            str(original.relative_to(tmp_path)),
            str(original.with_name("codex_old.py").relative_to(tmp_path)),
        ],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(commit, cwd=tmp_path, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path).decode().strip()
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"merge_group": {"base_sha": base, "head_sha": head}}))
    output = tmp_path / "output"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "merge_group")
    ci_changed_paths.main()
    assert "codex=true\n" in output.read_text()
