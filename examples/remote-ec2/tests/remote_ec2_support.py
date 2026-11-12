"""Shared helpers for the remote-EC2 sample's tests.

Every test runs the sample's shell scripts as subprocesses with an explicit,
minimal environment: a temp HOME, and a PATH that starts with a folder of
stub commands (fake curl, systemctl, uname, ...). Nothing here touches the
network, AWS, root-owned paths or a real service manager.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SAMPLE_DIR = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") or "/bin/bash"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

# A made-up instance id, used by every test that needs one. The leak test
# allows exactly this value and nothing else that looks like an instance id.
FAKE_INSTANCE_ID = "i-00000000feedface0"


def write_stub(folder: Path, name: str, body: str) -> Path:
    """Write an executable /bin/sh stub called NAME into FOLDER."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def recording_stub(folder: Path, name: str, log: Path, exit_code: int = 0) -> Path:
    """A stub that appends `NAME ARGS...` to LOG and exits with EXIT_CODE."""
    return write_stub(folder, name, f'echo "{name} $*" >> "{log}"\nexit {exit_code}\n')


@pytest.fixture()
def sandbox(tmp_path: Path):
    """A temp HOME and stub folder, plus a runner for the sample's scripts."""
    home = tmp_path / "home"
    home.mkdir()
    stubs = tmp_path / "stubs"
    stubs.mkdir()

    class Sandbox:
        def __init__(self) -> None:
            self.root = tmp_path
            self.home = home
            self.stubs = stubs
            self.calls = tmp_path / "calls.log"

        def env(self, **extra: str) -> dict[str, str]:
            env = {
                "HOME": str(home),
                "PATH": f"{stubs}:{SYSTEM_PATH}",
                "LANG": "C",
                "TMPDIR": str(tmp_path),
            }
            env.update(extra)
            return env

        def run(self, script: str, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [BASH, str(SAMPLE_DIR / script), *args],
                env=self.env(**extra),
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )

        def recorded_calls(self) -> list[str]:
            if not self.calls.exists():
                return []
            return self.calls.read_text(encoding="utf-8").splitlines()

    return Sandbox()


def shell_scripts() -> list[Path]:
    return sorted(p for p in SAMPLE_DIR.rglob("*.sh") if "tests" not in p.parts)


def tree_snapshot(folder: Path) -> dict[str, tuple[int, bytes]]:
    """Every path under FOLDER with its mode and content (folders: empty)."""
    snap: dict[str, tuple[int, bytes]] = {}
    for dirpath, dirnames, filenames in os.walk(folder):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            rel = str(path.relative_to(folder))
            st = path.lstat()
            data = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
            snap[rel] = (st.st_mode, data)
    return snap
