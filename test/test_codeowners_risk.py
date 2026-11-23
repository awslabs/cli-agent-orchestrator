"""Pin code-owner coverage of release and security policy, and mypy strict mode."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OWNER = "@awslabs/multiq"
BLOCK_HEADER = "# Gate policy, release scripts, and trust-boundary code (#1350)"
EXPECTED_PATHS = {
    "/.github/zizmor.yml",
    "/.github/dependabot.yml",
    "/.github/dependency-security/",
    "/.gitleaks.toml",
    "/tui/deny.toml",
    "/.coverage-baseline.json",
    "/mypy-baseline.json",
    "/SECURITY.md",
    "/scripts/release_preflight.py",
    "/scripts/bump_version.py",
    "/scripts/assert_release_version.py",
    "/scripts/release_commit.py",
    "/scripts/assert_wheel_matrix.py",
    "/scripts/build_tui.py",
    "/scripts/build_agent_plugin.py",
    "/src/cli_agent_orchestrator/security/",
    "/src/cli_agent_orchestrator/services/secret_gate.py",
    "/src/cli_agent_orchestrator/services/vault/",
    "/src/cli_agent_orchestrator/decisions/",
}


def test_mypy_strict_mode_is_enabled():
    with (ROOT / "pyproject.toml").open("rb") as config:
        assert tomllib.load(config)["tool"]["mypy"]["strict"] is True


def test_new_codeowner_paths_exist():
    owners = (ROOT / ".github/CODEOWNERS").read_text(encoding="utf-8")
    entries = [
        line.split() for line in owners.split(BLOCK_HEADER, 1)[1].splitlines() if line.strip()
    ]
    assert {path for path, owner in entries if owner == OWNER} == EXPECTED_PATHS
    for path, owner in entries:
        assert owner == OWNER
        target = ROOT / path.lstrip("/")
        assert target.is_dir() if path.endswith("/") else target.is_file(), path
