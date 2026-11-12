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
OTHER_INSTANCE_ID = "i-0000000000000000b"  # a second made-up id, for mismatch tests


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


# A made-up EBS volume id; its NVMe serial is the id without the dash.
FAKE_VOLUME_ID = "vol-00000000feedface0"
OTHER_VOLUME_ID = "vol-0000000000000000b"  # a second made-up id, for mismatch tests
FAKE_SHA = "ab" * 32

# Every setting bootstrap.sh needs, with fake pins and checksums.
BOOTSTRAP_SETTINGS = {
    "BOX_USER": "boxuser",
    "BOX_UID": "2001",
    "BOX_HOME": "/Users/boxuser",
    "HERDR_VERSION": "v0.9.3",
    "HERDR_SHA256": FAKE_SHA,
    "CAO_REF": "0123456789abcdef0123456789abcdef01234567",
    "AWS_CLI_VERSION": "2.0.0",
    "AWS_CLI_SHA256": FAKE_SHA,
    "SAM_VERSION": "1.0.0",
    "SAM_SHA256": FAKE_SHA,
    "YAZI_SHA256": FAKE_SHA,
    "HELIX_SHA256": FAKE_SHA,
    "UV_VERSION": "0.1.0",
    "UV_SHA256": FAKE_SHA,
}

# Commands that change a real system. In a dry run none of them may run.
MUTATING = [
    "curl",
    "useradd",
    "groupadd",
    "usermod",
    "systemctl",
    "apt-get",
    "mkfs.ext4",
    "mount",
    "dpkg",
    "runuser",
    "chown",
    "sudo",
    "uv",
    "mkdir",
    "chmod",
    "cp",
    "mv",
    "ln",
    "tar",
    "unzip",
    "install",
]


class FakeSystem:
    """Stubbed system probes plus fake /dev and /sys trees for bootstrap.sh.

    Each attribute is one probe's answer; call build() after changing them.
    """

    def __init__(self, sandbox) -> None:
        self.sandbox = sandbox
        self.base = sandbox.root / "fakesys"
        self.disk = "nvme1n1"
        self.serial = FAKE_VOLUME_ID.replace("-", "")
        self.disk_type = "disk"
        self.root_source = "/dev/nvme0n1p1"
        self.root_parent = "nvme0n1"
        self.names = 1  # lsblk -nro NAME lines (1 = no partitions)
        self.mountpoint = ""
        self.mounted_source = ""  # findmnt answer for DATA_MOUNT
        self.blkid_rc = 2
        self.blkid_out = ""
        self.wipefs_out = ""
        self.holders: list[str] = []
        self.user_uid: str | None = None  # None: the user doesn't exist
        self.user_home = "/Users/boxuser"
        self.uid_taken = False
        self.home_owner = "0"
        self.groups = "boxuser"

    @property
    def devfs(self) -> Path:
        return self.base / "dev"

    @property
    def dev(self) -> str:
        return str(self.devfs / self.disk)

    def build(self) -> dict[str, str]:
        stubs = self.sandbox.stubs
        log = self.sandbox.calls
        dev = self.dev
        (self.devfs / "disk" / "by-id").mkdir(parents=True, exist_ok=True)
        Path(dev).touch()
        link = (
            self.devfs
            / "disk"
            / "by-id"
            / f"nvme-Amazon_Elastic_Block_Store_{FAKE_VOLUME_ID.replace('-', '')}"
        )
        if link.is_symlink():
            link.unlink()
        link.symlink_to(dev)
        block = self.base / "sys" / "block" / self.disk
        (block / "device").mkdir(parents=True, exist_ok=True)
        (block / "device" / "serial").write_text(self.serial + "\n", encoding="utf-8")
        (block / "holders").mkdir(exist_ok=True)
        for holder in self.holders:
            (block / "holders" / holder).touch()
        os_release = self.base / "os-release"
        os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n', encoding="utf-8")

        write_stub(stubs, "uname", 'case "$1" in -m) echo aarch64 ;; *) echo Linux ;; esac\n')
        names = "\\n".join([self.disk] + [f"{self.disk}p{n}" for n in range(1, self.names)])
        write_stub(
            stubs,
            "lsblk",
            'case "$*" in\n'
            f'  "-dno TYPE {dev}") echo "{self.disk_type}" ;;\n'
            f'  "-no PKNAME {self.root_source}") echo "{self.root_parent}" ;;\n'
            f'  "-nro NAME {dev}") printf "{names}\\n" ;;\n'
            f'  "-nro MOUNTPOINT {dev}") echo "{self.mountpoint}" ;;\n'
            '  *) echo "lsblk stub: unexpected $*" >&2; exit 64 ;;\n'
            "esac\n",
        )
        mounted = f'echo "{self.mounted_source}"' if self.mounted_source else "exit 1"
        write_stub(
            stubs,
            "findmnt",
            'case "$*" in\n'
            f'  "-no SOURCE /") echo "{self.root_source}" ;;\n'
            f"  -rno\\ SOURCE\\ *) {mounted} ;;\n"
            '  *) echo "findmnt stub: unexpected $*" >&2; exit 64 ;;\n'
            "esac\n",
        )
        blkid_out = self.blkid_out.replace("\n", "\\n")
        write_stub(
            stubs,
            "blkid",
            'case "$*" in\n'
            f'  "-p -o export {dev}") printf "{blkid_out}"; exit {self.blkid_rc} ;;\n'
            '  *) echo "blkid stub: unexpected $*" >&2; exit 64 ;;\n'
            "esac\n",
        )
        write_stub(stubs, "wipefs", f'[ "$1" = "-n" ] || exit 64\nprintf "{self.wipefs_out}"\n')
        if self.user_uid is None:
            id_user = "exit 1"
        else:
            id_user = f"echo {self.user_uid}"
        write_stub(
            stubs,
            "id",
            'case "$*" in\n'
            f'  "-u boxuser") {id_user} ;;\n'
            f'  "-nG boxuser") echo "{self.groups}" ;;\n'
            '  *) echo "id stub: unexpected $*" >&2; exit 64 ;;\n'
            "esac\n",
        )
        passwd_line = f"boxuser:x:{self.user_uid}:{self.user_uid}::{self.user_home}:/bin/bash"
        write_stub(
            stubs,
            "getent",
            'case "$*" in\n'
            f'  "passwd boxuser") echo "{passwd_line}" ;;\n'
            f'  "passwd {BOOTSTRAP_SETTINGS["BOX_UID"]}") {"echo taken" if self.uid_taken else "exit 2"} ;;\n'
            "  *) exit 2 ;;\n"
            "esac\n",
        )
        write_stub(stubs, "stat", f'echo "{self.home_owner}"\n')
        write_stub(stubs, "dpkg-query", 'printf "install ok installed"\n')
        for name in MUTATING:
            recording_stub(stubs, name, log)
        return {
            "BOX_TEST_OS_RELEASE": str(os_release),
            "BOX_TEST_DEVFS": str(self.devfs),
            "BOX_TEST_SYSFS": str(self.base / "sys"),
            "BOX_TEST_FSTAB": str(self.base / "fstab"),
        }
