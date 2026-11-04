"""Regression tests for the named contributor and CI Python suites."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _collect(path: Path, *args: str) -> str:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(path),
            "-c",
            str(REPO_ROOT / "pyproject.toml"),
            "--collect-only",
            "-q",
            "--no-cov",
            *args,
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_contributor_and_ci_suites_select_the_same_tests(tmp_path: Path) -> None:
    """The default marker filter in pyproject.toml matches the `-m "not e2e"` CI passes
    explicitly, so a contributor's plain `pytest` runs the same set CI runs (#1005). Before
    this alignment, integration-marked tests ran only in CI, where no author was looking.
    Only e2e stays excluded on both; e2e needs the live providers under test/e2e/."""
    regression = tmp_path / "test_selection.py"
    regression.write_text(
        "import pytest\n\n"
        "def test_unit():\n"
        "    pass\n\n"
        "@pytest.mark.integration\n"
        "def test_integration():\n"
        "    pass\n\n"
        "@pytest.mark.e2e\n"
        "def test_e2e_outside_e2e_directory():\n"
        "    pass\n",
        encoding="utf-8",
    )

    contributor = _collect(regression)
    ci = _collect(regression, "-m", "not e2e")
    integration = _collect(regression, "-m", "integration")

    assert "test_unit" in contributor
    assert "test_integration" in contributor
    assert "test_e2e_outside_e2e_directory" not in contributor
    assert "test_unit" in ci
    assert "test_integration" in ci
    assert "test_e2e_outside_e2e_directory" not in ci
    assert "test_unit" not in integration
    assert "test_integration" in integration
    assert "test_e2e_outside_e2e_directory" not in integration
