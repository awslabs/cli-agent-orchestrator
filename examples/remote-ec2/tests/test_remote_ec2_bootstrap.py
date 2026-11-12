"""bootstrap.sh: a dry run in a fake root changes nothing; the OS guard refuses."""

from __future__ import annotations

from pathlib import Path

import pytest
from remote_ec2_support import recording_stub, tree_snapshot, write_stub

MUTATING = [
    "curl",
    "useradd",
    "usermod",
    "systemctl",
    "apt-get",
    "mkfs.ext4",
    "mount",
    "dpkg",
    "runuser",
    "chown",
    "install",
    "sudo",
    "uv",
]
SETTINGS = {
    "BOX_USER": "boxuser",
    "BOX_HOME": "/Users/boxuser",
    "DATA_DEVICE": "/dev/nvme1n1",
    "HERDR_VERSION": "v0.9.3",
    "CAO_REF": "0123456789abcdef0123456789abcdef01234567",
    "BACKUP_S3_URI": "s3://example-bucket/cao-box/",
}


def make_root(base: Path, os_id: str = "ubuntu", version: str = "24.04") -> Path:
    root = base / "fakeroot"
    (root / "etc").mkdir(parents=True)
    (root / "etc" / "os-release").write_text(
        f'NAME="Ubuntu"\nID={os_id}\nVERSION_ID="{version}"\n', encoding="utf-8"
    )
    return root


def arm_stubs(sandbox, arch: str = "aarch64") -> None:
    write_stub(sandbox.stubs, "uname", f'case "$1" in -m) echo {arch} ;; *) echo Linux ;; esac\n')
    for name in MUTATING:
        recording_stub(sandbox.stubs, name, sandbox.calls)


def test_dry_run_in_a_fake_root_prints_the_plan_and_changes_nothing(sandbox) -> None:
    root = make_root(sandbox.root)
    arm_stubs(sandbox)
    before = tree_snapshot(root)

    result = sandbox.run("bootstrap.sh", "--dry-run", "--root", str(root), **SETTINGS)

    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    for n in range(9):
        assert f"== {n}." in out, f"step {n} missing from the plan"
    assert "== done" in out
    assert "OS: ubuntu 24.04 aarch64" in out
    for unit in [
        "herdr-cao.service",
        "cao-server.service",
        "cao-backup.service",
        "cao-backup.timer",
    ]:
        assert f"/etc/systemd/system/{unit}" in out
    assert "[dry-run] would run: systemctl --root=" in out
    assert "enable --now" not in out
    assert "CAO_BOX_SESSION=cao-<instance-id>" in out
    assert tree_snapshot(root) == before
    assert sandbox.recorded_calls() == []


def test_a_second_dry_run_is_identical(sandbox) -> None:
    root = make_root(sandbox.root)
    arm_stubs(sandbox)
    first = sandbox.run("bootstrap.sh", "--dry-run", "--root", str(root), **SETTINGS)
    second = sandbox.run("bootstrap.sh", "--dry-run", "--root", str(root), **SETTINGS)
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout


def test_an_existing_box_env_is_kept(sandbox) -> None:
    root = make_root(sandbox.root)
    (root / "etc" / "cao-box").mkdir(parents=True)
    (root / "etc" / "cao-box" / "box.env").write_text(
        "CAO_BOX_SESSION=cao-i-00000000feedface0\n", encoding="utf-8"
    )
    arm_stubs(sandbox)
    result = sandbox.run("bootstrap.sh", "--dry-run", "--root", str(root), **SETTINGS)
    assert result.returncode == 0, result.stderr
    assert "kept existing" in result.stdout and "cao-i-00000000feedface0" in result.stdout
    assert "would read the instance id" not in result.stdout


@pytest.mark.parametrize(
    ("os_id", "version", "arch"),
    [("debian", "12", "aarch64"), ("ubuntu", "22.04", "aarch64"), ("ubuntu", "24.04", "x86_64")],
)
def test_refuses_anything_but_ubuntu_2404_arm64(sandbox, os_id, version, arch) -> None:
    root = make_root(sandbox.root, os_id, version)
    arm_stubs(sandbox, arch)
    result = sandbox.run("bootstrap.sh", "--dry-run", "--root", str(root), **SETTINGS)
    assert result.returncode != 0
    assert "supports Ubuntu 24.04 on aarch64 only" in result.stderr
    assert "== 1." not in result.stdout
    assert sandbox.recorded_calls() == []


def test_refuses_without_required_settings(sandbox) -> None:
    root = make_root(sandbox.root)
    arm_stubs(sandbox)
    partial = {k: v for k, v in SETTINGS.items() if k != "CAO_REF"}
    result = sandbox.run("bootstrap.sh", "--dry-run", "--root", str(root), **partial)
    assert result.returncode != 0
    assert "set CAO_REF" in result.stderr


def test_rejects_a_bad_user_name(sandbox) -> None:
    root = make_root(sandbox.root)
    arm_stubs(sandbox)
    result = sandbox.run(
        "bootstrap.sh", "--dry-run", "--root", str(root), **{**SETTINGS, "BOX_USER": "x; rm"}
    )
    assert result.returncode != 0
    assert "plain Linux user name" in result.stderr


def test_unknown_flag_is_an_error(sandbox) -> None:
    result = sandbox.run("bootstrap.sh", "--now")
    assert result.returncode == 2
    assert "unknown argument" in result.stderr


def test_help_prints_the_header(sandbox) -> None:
    result = sandbox.run("bootstrap.sh", "--help")
    assert result.returncode == 0
    assert "--dry-run" in result.stdout and "set -euo" not in result.stdout
