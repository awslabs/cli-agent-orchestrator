"""Regression checks for the CAO devcontainer feature files."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FEATURE_DIR = REPO_ROOT / ".devcontainer" / "features" / "cao"


def test_install_script_uses_official_repo_default() -> None:
    """Ensure the feature installer defaults to the official upstream repository."""
    install_script = (FEATURE_DIR / "install.sh").read_text(encoding="utf-8")

    assert "https://github.com/ThePlenkov/cli-agent-orchestrator" not in install_script
    assert "https://github.com/awslabs/cli-agent-orchestrator.git" in install_script


def test_feature_manifest_version_matches_project_version() -> None:
    """Keep devcontainer feature version aligned with the project version."""
    feature_manifest = json.loads(
        (FEATURE_DIR / "devcontainer-feature.json").read_text(encoding="utf-8")
    )
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert feature_manifest["version"] == pyproject["project"]["version"]


def test_feature_declares_python_dependency() -> None:
    """Feature must depend on the Python devcontainer feature for pip availability."""
    feature_manifest = json.loads(
        (FEATURE_DIR / "devcontainer-feature.json").read_text(encoding="utf-8")
    )

    assert feature_manifest["dependsOn"]["ghcr.io/devcontainers/features/python:1"] == {}
