"""Pin code-owner coverage of release and security policy, and CI gate settings."""

import configparser
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OWNER = "@awslabs/multiq"
BLOCK_HEADER = "# Gate policy, release scripts, and trust-boundary code (#1350)"
OLD_ENTRIES = [
    "/.github/workflows/",
    "/.github/actions/",
    "/.github/CODEOWNERS",
    "/scripts/dependency_security.py",
    "/scripts/test-braces-security.cjs",
    "/patches/",
    "/vendor/",
    "/docusaurus/braces-compat/",
    "/web/braces-compat/",
    "/cao_mcp_apps/braces-compat/",
]
EXPECTED_PATHS = {
    "/.github/zizmor.yml",
    "/.github/dependabot.yml",
    "/.github/dependency-security/",
    "/.gitleaks.toml",
    "/tui/deny.toml",
    "/.coverage-baseline.json",
    "/mypy-baseline.json",
    "/mypy.ini",
    "/SECURITY.md",
    "/scripts/release_preflight.py",
    "/scripts/bump_version.py",
    "/scripts/assert_release_version.py",
    "/scripts/release_commit.py",
    "/scripts/assert_wheel_matrix.py",
    "/scripts/build_tui.py",
    "/scripts/build_agent_plugin.py",
    "/scripts/hatch_build_tui_tag.py",
    "/scripts/mypy_ratchet.py",
    "/cao_mcp_apps/scripts/coverage-ratchet.mjs",
    "/scripts/test-shipped-packages.mjs",
    "/scripts/assert_no_ffi.py",
    "/test/_must_run.py",
    "/test/test_codeowners_risk.py",
    "/src/cli_agent_orchestrator/security/",
    "/src/cli_agent_orchestrator/services/secret_gate.py",
    "/src/cli_agent_orchestrator/services/vault/",
    "/src/cli_agent_orchestrator/decisions/",
    "/.gitleaksignore",
    "/.trivyignore",
    "/trivy.yaml",
    "/trivy-secret.yaml",
    "/ruff.toml",
    "/.ruff.toml",
}
RESERVED_ABSENT = {
    "/.gitleaksignore",  # secret-scan.yml:73-79 loads root gitleaks ignore files.
    "/.trivyignore",  # ci.yml:1111-1148 loads root Trivy ignore files.
    "/trivy.yaml",  # ci.yml:1111-1148 loads root Trivy config files.
    "/trivy-secret.yaml",  # ci.yml:1111-1148 loads root Trivy secret config.
    "/ruff.toml",  # ci.yml:442 loads root ruff config files.
    "/.ruff.toml",  # ci.yml:442 loads root ruff config files.
}


def _entries(text):
    for line in text.splitlines():
        parts = line.split("#", 1)[0].split()
        if parts:
            yield parts[0], parts[1:]


def test_gate_settings_are_pinned():
    with (ROOT / "pyproject.toml").open("rb") as config:
        ruff = tomllib.load(config)["tool"]["ruff"]
    assert ruff == {
        "line-length": 100,
        "target-version": "py311",
        "lint": {"select": ["F401", "F821", "F822", "F841"]},
    }


def test_live_mypy_settings_are_pinned():
    config = configparser.ConfigParser(interpolation=None)
    config.read(ROOT / "mypy.ini")
    assert dict(config["mypy"]) == {
        "python_version": "3.11",
        "warn_return_any": "True",
        "warn_unused_configs": "True",
        "disallow_untyped_defs": "False",
        "ignore_missing_imports": "False",
    }
    assert {
        section: dict(config[section]) for section in config.sections() if section != "mypy"
    } == {
        "mypy-frontmatter.*": {"ignore_missing_imports": "True"},
        "mypy-apscheduler.*": {"ignore_missing_imports": "True"},
        "mypy-requests.*": {"ignore_missing_imports": "True"},
        "mypy-yaml.*": {"ignore_missing_imports": "True"},
        "mypy-jsonschema.*": {"ignore_missing_imports": "True"},
        "mypy-tomli.*": {"ignore_missing_imports": "True"},
        "mypy-cli_agent_orchestrator.adapters.database": {"ignore_missing_imports": "True"},
        "mypy-cli_agent_orchestrator.clients.database": {
            "disable_error_code": "misc,assignment,arg-type"
        },
        "mypy-cli_agent_orchestrator.cli.commands.install": {"disable_error_code": "assignment"},
        "mypy-cli_agent_orchestrator.mcp_server.server": {"disable_error_code": "no-any-return"},
    }


def test_ratchet_uses_default_mypy_config():
    assert "--config-file" not in (ROOT / "scripts/mypy_ratchet.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("path", sorted(EXPECTED_PATHS - set(OLD_ENTRIES)))
def test_each_gate_path_is_owned(path):
    text = (ROOT / ".github/CODEOWNERS").read_text(encoding="utf-8")
    assert any(entry == path and OWNER in owners for entry, owners in _entries(text)), path


def test_codeowner_entries_resolve():
    text = (ROOT / ".github/CODEOWNERS").read_text(encoding="utf-8")
    before, header, block = text.partition(BLOCK_HEADER)
    assert header == BLOCK_HEADER
    old_entries = list(_entries(before))
    assert old_entries == [(path, [OWNER]) for path in OLD_ENTRIES]
    new_entries = list(_entries(block))
    assert {path for path, _ in new_entries} == EXPECTED_PATHS
    for path, owners in old_entries + new_entries:
        assert OWNER in owners, path
        target = ROOT / path.lstrip("/")
        if path in RESERVED_ABSENT:
            assert not target.exists() or target.is_file(), path
        else:
            assert target.is_dir() if path.endswith("/") else target.is_file(), path


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ("# A policy comment", []),
        ("/mypy.ini @other/team @awslabs/multiq", [("/mypy.ini", ["@other/team", OWNER])]),
        ("/scripts/*.py @awslabs/multiq # glob", [("/scripts/*.py", [OWNER])]),
    ],
)
def test_parser_accepts_comments_multiple_owners_and_globs(extra, expected):
    assert list(_entries(f"\n{extra}\n")) == expected
