"""Static checks on every shell script in the sample (bash -n, shellcheck),
backup.sh's flag parsing, and the tests' hermetic PATH."""

from __future__ import annotations

import shutil
import subprocess

import pytest
from remote_ec2_support import (
    BASH,
    HOST_DIRS,
    SAMPLE_DIR,
    link_host_tools,
    shell_scripts,
    write_stub,
)

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
        "bin/lib-cao-box.sh",
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
    if script.name.startswith("lib-"):
        # A sourced library: it must not change the caller's shell options.
        assert "set -" not in text
        return
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


@pytest.mark.parametrize("flag", ["--db", "--dest", "--keep"])
def test_backup_flags_without_a_value_are_usage_errors(sandbox, flag) -> None:
    result = sandbox.run("backup.sh", "--no-upload", flag)
    assert result.returncode == 2, result.stderr
    assert f"backup: {flag} needs a value" in result.stderr
    assert "unbound variable" not in result.stderr


PROBED = ["aws", "gh", "sam", "yazi", "ya", "hx", "uv", "uvx", "herdr", "systemctl", "runuser"]


def test_the_test_path_never_has_the_hosts_probed_tools(tmp_path) -> None:
    # A host with aws, gh, ... in a system folder (gh is in /usr/bin on GitHub's
    # Ubuntu runners): none of them may reach the scripts under test.
    host = tmp_path / "host-bin"
    for name in [*PROBED, "cat"]:
        write_stub(host, name, "exit 0\n")
    folder = link_host_tools(tmp_path / "bin", (str(host), *HOST_DIRS))
    names = {p.name for p in folder.iterdir()}
    assert names.isdisjoint(PROBED), sorted(names & set(PROBED))
    assert (folder / "cat").resolve() == (host / "cat").resolve()
    assert {"bash", "sed", "awk", "grep", "mktemp"} <= names
