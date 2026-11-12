"""Static checks on every shell script in the sample: bash -n and shellcheck."""

from __future__ import annotations

import shutil
import subprocess

import pytest
from remote_ec2_support import BASH, SAMPLE_DIR, shell_scripts

SCRIPTS = shell_scripts()


def test_the_sample_has_the_scripts_the_readme_names() -> None:
    names = {p.relative_to(SAMPLE_DIR).as_posix() for p in SCRIPTS}
    assert {
        "bootstrap.sh",
        "backup.sh",
        "restore.sh",
        "bin/box-session-name.sh",
        "bin/herdr-start.sh",
        "bin/herdr-clean-layout.sh",
        "bin/wait-herdr-socket.sh",
        "bin/resource-probe-linux.sh",
        "bin/box-resume.sh",
    } <= names


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.relative_to(SAMPLE_DIR).as_posix())
def test_bash_parses(script) -> None:
    result = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.relative_to(SAMPLE_DIR).as_posix())
def test_strict_mode_and_shebang(script) -> None:
    text = script.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    # box-resume.sh keeps going after a failed check on purpose (-uo, no -e).
    assert "set -euo pipefail" in text or "set -uo pipefail" in text


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.relative_to(SAMPLE_DIR).as_posix())
def test_shellcheck_is_clean(script) -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")
    result = subprocess.run(
        [shellcheck, "--shell=bash", "--external-sources", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_scripts_are_executable() -> None:
    for script in SCRIPTS:
        assert script.stat().st_mode & 0o111, f"{script.name} is not executable"
