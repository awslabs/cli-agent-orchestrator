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


def test_keyless_e2e_is_unfiltered_even_for_unrelated_changes():
    workflow = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())
    job = workflow["jobs"]["e2e-mock"]
    assert "if" not in job and "needs" not in job
    assert "e2e-mock" not in FILTERS


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


def test_pr_uses_checkout_merge_commit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_SHA", "merge")
    assert changed_revisions("pull_request", {"pull_request": {"base": {"sha": "base"}}}) == (
        "base",
        "merge",
    )


def test_filter_matches_nested_docs_and_does_not_match_unrelated():
    assert matching_filters(["docusaurus/src/index.ts"])["docs"]
    assert not any(matching_filters(["README.md"]).values())


@pytest.mark.parametrize(
    ("event_name", "payload"),
    [
        ("push", {"before": "base", "after": "head", "commits": [{"modified": ["README.md"]}]}),
        ("push", {"before": "0" * 40, "after": "head"}),
        ("merge_group", {"merge_group": {"base_sha": "base", "head_sha": "head"}}),
        ("merge_group", {"merge_group": {"head_sha": "head"}}),
    ],
)
def test_non_pr_events_select_all_without_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event_name: str, payload: dict
):
    event = tmp_path / "event.json"
    event.write_text(json.dumps(payload))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)

    def unexpected_diff(*args: object, **kwargs: object) -> bytes:
        pytest.fail("push and merge_group must not diff paths")

    monkeypatch.setattr(ci_changed_paths.subprocess, "check_output", unexpected_diff)
    ci_changed_paths.main()
    assert output.read_text().splitlines() == [f"{name}=true" for name in FILTERS]


def test_pull_request_zero_base_refuses_to_write_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # The zero-SHA range guard is reachable only for pull_request events.
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"base": {"sha": "0" * 40}}}))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_SHA", "head")
    with pytest.raises(ValueError, match="no valid change range"):
        ci_changed_paths.main()
    assert not output.exists()


@pytest.mark.parametrize(
    ("event_name", "payload", "head"),
    [
        ("pull_request", {"pull_request": {"base": {"sha": ""}}}, "head"),
        ("pull_request", {"pull_request": {"base": {"sha": "base"}}}, ""),
        ("pull_request", {"pull_request": {"base": {}}}, "head"),
        ("workflow_dispatch", {}, "head"),
        ("unknown", {}, "head"),
    ],
)
def test_invalid_ranges_and_events_never_emit_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event_name: str, payload: dict, head: str
):
    event = tmp_path / "event.json"
    event.write_text(json.dumps(payload))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.setenv("GITHUB_SHA", head)
    with pytest.raises((ValueError, KeyError)):
        ci_changed_paths.main()
    assert not output.exists()


def test_deleted_file_selects_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"base": {"sha": "base"}}}))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_SHA", "head")
    monkeypatch.setattr(
        ci_changed_paths.subprocess,
        "check_output",
        lambda *args, **kwargs: b"src/cli_agent_orchestrator/providers/kiro_cli.py\0",
    )
    ci_changed_paths.main()
    assert "kiro=true\n" in output.read_text()
    assert "docs=false\n" in output.read_text()


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


def test_pull_request_emits_path_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"base": {"sha": "base"}}}))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_SHA", "head")

    def changed_files(command: list[str], **kwargs: object) -> bytes:
        assert command == ["git", "diff", "--name-only", "--no-renames", "-z", "base", "head"]
        return b"docusaurus/src/index.ts\0"

    monkeypatch.setattr(ci_changed_paths.subprocess, "check_output", changed_files)
    ci_changed_paths.main()
    assert "docs=true\n" in output.read_text()
    assert "kiro=false\n" in output.read_text()


def test_unreadable_range_does_not_emit_skip_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"base": {"sha": "base"}}}))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_SHA", "head")

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
    event.write_text(json.dumps({"pull_request": {"base": {"sha": base}}}))
    output = tmp_path / "output"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_SHA", head)
    ci_changed_paths.main()
    assert "codex=true\n" in output.read_text()
