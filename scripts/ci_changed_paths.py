"""Decide which path-filtered CI jobs must run for the current event."""

import fnmatch
import json
import os
import subprocess
from pathlib import Path

FILTERS = {
    "antigravity": (
        "src/cli_agent_orchestrator/providers/antigravity_cli.py",
        "test/providers/test_antigravity_cli_*.py",
        "test/providers/fixtures/agy_*.txt",
        "pyproject.toml",
        ".github/workflows/test-antigravity-cli-provider.yml",
    ),
    "claude": (
        "src/cli_agent_orchestrator/providers/claude_code.py",
        "test/providers/test_claude_code_*.py",
        "test/providers/test_busy_pattern_settled_output_guard.py",
        "pyproject.toml",
        ".github/workflows/test-claude-code-provider.yml",
    ),
    "codex": (
        "src/cli_agent_orchestrator/providers/codex.py",
        "test/providers/test_codex_*.py",
        "test/providers/fixtures/codex_*.txt",
        "pyproject.toml",
        ".github/workflows/test-codex-provider.yml",
    ),
    "kiro": (
        "src/cli_agent_orchestrator/providers/kiro_cli.py",
        "test/providers/test_kiro_cli_*.py",
        "test/providers/fixtures/kiro_cli_*.txt",
        "pyproject.toml",
        ".github/workflows/test-kiro-cli-provider.yml",
    ),
    "transport": (
        "examples/robotics/cross-zone-transport/**",
        "src/cli_agent_orchestrator/schemas/agent_profile.schema.json",
        "src/cli_agent_orchestrator/models/provider.py",
        "src/cli_agent_orchestrator/utils/enforcement.py",
        ".github/workflows/robotics-transport.yml",
    ),
    "factory": (
        "examples/robotics/hierarchical-factory/**",
        "src/cli_agent_orchestrator/schemas/agent_profile.schema.json",
        "src/cli_agent_orchestrator/models/provider.py",
        "src/cli_agent_orchestrator/utils/enforcement.py",
        ".github/workflows/robotics-factory.yml",
    ),
    "docs": (
        "docusaurus/**",
        "patches/**",
        "vendor/**",
        "scripts/test-braces-security.cjs",
        "examples/robotics/cross-zone-transport/images/**",
        "examples/robotics/hierarchical-factory/images/**",
        ".github/workflows/gh-pages.yml",
    ),
}

for name in FILTERS:
    FILTERS[name] += (".github/workflows/ci.yml", "scripts/ci_changed_paths.py")


def changed_revisions(event_name: str, event: dict) -> tuple[str, str]:
    if event_name == "pull_request":
        return event["pull_request"]["base"]["sha"], os.environ["GITHUB_SHA"]
    if event_name == "merge_group":
        return event["merge_group"]["base_sha"], event["merge_group"]["head_sha"]
    if event_name == "push":
        return event["before"], event["after"]
    raise ValueError(f"unsupported event: {event_name}")


def matching_filters(paths: list[str]) -> dict[str, bool]:
    return {
        name: any(fnmatch.fnmatchcase(path, pattern) for path in paths for pattern in patterns)
        for name, patterns in FILTERS.items()
    }


def main() -> None:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    base, head = changed_revisions(os.environ["GITHUB_EVENT_NAME"], event)
    if not base or not head or base == "0" * 40:
        raise ValueError("no valid change range; refusing to skip jobs")
    changes = (
        subprocess.check_output(
            ["git", "diff", "--name-only", "-z", base, head], stderr=subprocess.STDOUT
        )
        .decode()
        .split("\0")
    )
    results = matching_filters(changes)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        for name, matched in results.items():
            output.write(f"{name}={str(matched).lower()}\n")
            print(f"{name}: {'run' if matched else 'skip'}")


if __name__ == "__main__":
    main()
