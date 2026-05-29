"""Regression checks for the CAO devcontainer feature files."""

from __future__ import annotations

import json
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


REPO_ROOT = Path(__file__).resolve().parents[1]
FEATURE_DIR = REPO_ROOT / ".devcontainer" / "features" / "cao"


def test_install_script_uses_official_repo_default() -> None:
    """Ensure the feature installer defaults to the official upstream repository."""
    install_script = (FEATURE_DIR / "install.sh").read_text(encoding="utf-8")

    assert "https://github.com/awslabs/cli-agent-orchestrator.git" in install_script


def test_feature_manifest_version_matches_project_version() -> None:
    """Keep devcontainer feature version aligned with the project version."""
    feature_manifest = json.loads(
        (FEATURE_DIR / "devcontainer-feature.json").read_text(encoding="utf-8")
    )
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    feature_version = feature_manifest["version"]
    project_version = pyproject["project"]["version"]
    if feature_version != project_version:
        raise AssertionError(
            f"Feature version {feature_version} does not match project version {project_version}"
        )


def test_feature_declares_python_dependency() -> None:
    """Feature must depend on the Python devcontainer feature for pip availability."""
    feature_manifest = json.loads(
        (FEATURE_DIR / "devcontainer-feature.json").read_text(encoding="utf-8")
    )

    python_dependency = feature_manifest["dependsOn"]["ghcr.io/devcontainers/features/python:1"]
    if python_dependency != {}:
        raise AssertionError("Python feature dependency must be declared as an empty object")


def test_feature_webui_defaults_to_false() -> None:
    """Default config should not require npm/node to complete installation."""
    feature_manifest = json.loads(
        (FEATURE_DIR / "devcontainer-feature.json").read_text(encoding="utf-8")
    )

    assert feature_manifest["options"]["webui"]["default"] is False


def test_feature_installs_after_uses_versioned_feature_ids() -> None:
    """Keep installsAfter references aligned with explicit major versions."""
    feature_manifest = json.loads(
        (FEATURE_DIR / "devcontainer-feature.json").read_text(encoding="utf-8")
    )

    required_installs_after = {
        "ghcr.io/devcontainers/features/node:1",
        "ghcr.io/devcontainers/features/python:1",
    }
    missing_from_installs_after = required_installs_after.difference(
        feature_manifest["installsAfter"]
    )
    if missing_from_installs_after:
        raise AssertionError(
            f"installsAfter is missing required features: {sorted(missing_from_installs_after)}"
        )


def test_install_script_generates_runtime_safe_entrypoint() -> None:
    """Entrypoint template should preserve runtime env expansion in output script."""
    install_script = (FEATURE_DIR / "install.sh").read_text(encoding="utf-8")

    assert "cat << 'EOF'" in install_script
    assert "set -euo pipefail" in install_script
    assert 'AUTOSTART_VALUE="${AUTOSTART:-$AUTOSTART_DEFAULT}"' in install_script
    assert 'PORT_VALUE="${PORT:-$PORT_DEFAULT}"' in install_script


def test_install_script_supports_apt_and_apk() -> None:
    """Installer should support both Debian and Alpine package managers."""
    install_script = (FEATURE_DIR / "install.sh").read_text(encoding="utf-8")

    assert "command -v apt-get" in install_script
    assert "command -v apk" in install_script
    assert "pip_install_editable" in install_script
    assert "--no-cache-dir" in install_script
    assert "break-system-packages" in install_script
