"""bootstrap.sh steps 1-5 (volume, user, packages, tools, CAO), reached with a
dry run against stubbed system probes and fake /dev and /sys trees.

Read-only probes (lsblk, blkid -p, wipefs -n, findmnt, id, getent, stat,
dpkg-query) answer from the FakeSystem; every command that would change the
system is a recording stub, and a dry run must never call one.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from remote_ec2_support import (
    BOOTSTRAP_SETTINGS,
    FAKE_SHA,
    FAKE_VOLUME_ID,
    SAMPLE_DIR,
    FakeSystem,
    tree_snapshot,
)

VOLUME = {**BOOTSTRAP_SETTINGS, "DATA_VOLUME_ID": FAKE_VOLUME_ID}


def dry_run(sandbox, fake: FakeSystem, *flags: str, **settings: str):
    env = fake.build()
    return sandbox.run("bootstrap.sh", "--dry-run", *flags, **{**env, **settings})


def assert_stopped_without_formatting(sandbox, result, message: str) -> None:
    assert result.returncode == 1, result.stdout + result.stderr
    assert message in result.stderr
    assert "mkfs" not in result.stdout
    assert "== 2." not in result.stdout
    assert sandbox.recorded_calls() == []


# ------------------------------------------------------------ the data volume


def test_a_blank_volume_is_formatted_only_with_the_flag(sandbox) -> None:
    fake = FakeSystem(sandbox)
    result = dry_run(sandbox, fake, "--format-new-volume", **VOLUME)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"[dry-run] would run: mkfs.ext4 -L cao-data {fake.dev}" in result.stdout
    assert "/Users ext4 defaults,nofail 0 2" in result.stdout
    assert "[dry-run] would run: mount /Users" in result.stdout
    assert sandbox.recorded_calls() == []


def test_the_env_form_of_the_flag_works_too(sandbox) -> None:
    fake = FakeSystem(sandbox)
    result = dry_run(sandbox, fake, FORMAT_DATA_DEVICE="yes", **VOLUME)
    assert result.returncode == 0, result.stderr
    assert "would run: mkfs.ext4" in result.stdout


def test_a_blank_volume_without_the_flag_stops(sandbox) -> None:
    result = dry_run(sandbox, FakeSystem(sandbox), **VOLUME)
    assert_stopped_without_formatting(sandbox, result, "re-run with --format-new-volume")


def test_a_partition_table_only_disk_stops(sandbox) -> None:
    fake = FakeSystem(sandbox)
    fake.blkid_rc = 0
    fake.blkid_out = "DEVNAME=/dev/nvme1n1\nPTUUID=0000\nPTTYPE=gpt\n"
    fake.wipefs_out = "DEVICE OFFSET TYPE\nnvme1n1 0x200 gpt\n"
    result = dry_run(sandbox, fake, "--format-new-volume", **VOLUME)
    assert_stopped_without_formatting(sandbox, result, "signatures but no filesystem")


@pytest.mark.parametrize("code", [1, 4, 8])
def test_any_other_blkid_exit_code_stops(sandbox, code) -> None:
    fake = FakeSystem(sandbox)
    fake.blkid_rc = code
    result = dry_run(sandbox, fake, "--format-new-volume", **VOLUME)
    assert_stopped_without_formatting(sandbox, result, f"blkid -p exited {code}")


def test_signatures_seen_by_wipefs_stop_even_when_blkid_says_blank(sandbox) -> None:
    fake = FakeSystem(sandbox)
    fake.wipefs_out = "DEVICE OFFSET TYPE\nnvme1n1 0x0 LVM2_member\n"
    result = dry_run(sandbox, fake, "--format-new-volume", **VOLUME)
    assert_stopped_without_formatting(sandbox, result, "wipefs found signatures")


def test_the_root_disk_stops(sandbox) -> None:
    fake = FakeSystem(sandbox)
    fake.root_parent = fake.disk
    result = dry_run(sandbox, fake, "--format-new-volume", **VOLUME)
    assert_stopped_without_formatting(sandbox, result, "holds the root filesystem")


def test_a_wrong_serial_stops(sandbox) -> None:
    fake = FakeSystem(sandbox)
    fake.serial = "vol0000000000000000b"
    result = dry_run(sandbox, fake, "--format-new-volume", **VOLUME)
    assert_stopped_without_formatting(sandbox, result, "refusing")
    assert "serial" in result.stderr


def test_a_volume_that_is_not_attached_stops(sandbox) -> None:
    fake = FakeSystem(sandbox)
    env = fake.build()
    settings = {**VOLUME, "DATA_VOLUME_ID": "vol-0000000000000000b"}
    result = sandbox.run("bootstrap.sh", "--dry-run", "--format-new-volume", **{**env, **settings})
    assert_stopped_without_formatting(sandbox, result, "is the volume attached")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"names": 2}, "has partitions"),
        ({"holders": ["dm-0"]}, "has holders"),
        ({"mountpoint": "/mnt/other"}, "mounted somewhere else"),
        ({"disk_type": "part"}, "not a whole disk"),
    ],
)
def test_disks_in_use_stop(sandbox, change, message) -> None:
    fake = FakeSystem(sandbox)
    for key, value in change.items():
        setattr(fake, key, value)
    result = dry_run(sandbox, fake, "--format-new-volume", **VOLUME)
    assert_stopped_without_formatting(sandbox, result, message)


def test_a_data_device_that_is_another_disk_stops(sandbox) -> None:
    fake = FakeSystem(sandbox)
    env = fake.build()
    other = sandbox.root / "fakesys" / "dev" / "nvme2n1"
    other.touch()
    result = sandbox.run(
        "bootstrap.sh",
        "--dry-run",
        "--format-new-volume",
        **{**env, **VOLUME, "DATA_DEVICE": str(other)},
    )
    assert_stopped_without_formatting(sandbox, result, "is not " + FAKE_VOLUME_ID)


@pytest.mark.parametrize("fs_type", ["ext4", "xfs"])
def test_an_existing_filesystem_is_mounted_not_formatted(sandbox, fs_type) -> None:
    fake = FakeSystem(sandbox)
    fake.blkid_rc = 0
    fake.blkid_out = f"DEVNAME=/dev/nvme1n1\nUUID=0000\nTYPE={fs_type}\n"
    result = dry_run(sandbox, fake, "--format-new-volume", **VOLUME)
    assert result.returncode == 0, result.stderr
    assert "mkfs" not in result.stdout
    assert f"already has an {fs_type} filesystem" in result.stdout
    assert f"/Users {fs_type} defaults,nofail 0 2" in result.stdout


def test_an_unsupported_filesystem_stops(sandbox) -> None:
    fake = FakeSystem(sandbox)
    fake.blkid_rc = 0
    fake.blkid_out = "TYPE=crypto_LUKS\n"
    result = dry_run(sandbox, fake, "--format-new-volume", **VOLUME)
    assert_stopped_without_formatting(sandbox, result, "crypto_LUKS")


def test_an_already_mounted_volume_is_left_alone(sandbox) -> None:
    fake = FakeSystem(sandbox)
    fake.mounted_source = fake.dev
    result = dry_run(sandbox, fake, **VOLUME)
    assert result.returncode == 0, result.stderr
    assert "already mounted" in result.stdout
    assert "mkfs" not in result.stdout and "would run: mount" not in result.stdout


def test_something_else_mounted_at_the_mount_point_stops(sandbox) -> None:
    fake = FakeSystem(sandbox)
    fake.mounted_source = "/dev/nvme9n1"
    result = dry_run(sandbox, fake, **VOLUME)
    assert_stopped_without_formatting(sandbox, result, "already has /dev/nvme9n1 mounted")


# ------------------------------------------------------------- user and home


def lines(result) -> list[str]:
    return result.stdout.splitlines()


def index_of(result, needle: str) -> int:
    for n, line in enumerate(lines(result)):
        if needle in line:
            return n
    raise AssertionError(f"not in the plan: {needle}")


def test_a_new_user_gets_a_pinned_uid_and_owns_local_before_user_installs(sandbox) -> None:
    result = dry_run(sandbox, FakeSystem(sandbox), **BOOTSTRAP_SETTINGS)
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert "would run: groupadd --gid 2001 boxuser" in out
    assert "would run: useradd --uid 2001 --gid 2001 --home-dir /Users/boxuser --create-home" in out
    # The user makes its own folders: root never runs `install -d -o` there.
    owned = index_of(
        result,
        "would run: runuser -u boxuser -- env HOME=/Users/boxuser PATH=/Users/boxuser/.local/bin:"
        "/usr/local/bin:/usr/bin:/bin install -d -m 0755 /Users/boxuser/.local "
        "/Users/boxuser/.local/bin /Users/boxuser/.config",
    )
    herdr = index_of(result, "<download-dir>/herdr /Users/boxuser/.local/bin/herdr")
    assert owned < herdr
    # User-level installs run as the user, never as root.
    for needle in [
        "install -m 0755 <download-dir>/herdr /Users/boxuser/.local/bin/herdr",
        "uv-aarch64-unknown-linux-gnu/uvx /Users/boxuser/.local/bin/",
        "uv tool install --force git+",
    ]:
        line = lines(result)[index_of(result, needle)]
        assert "would run: runuser -u boxuser -- env HOME=/Users/boxuser" in line, line
    # Step 7 writes the config files as the user, so root changes no owner.
    for path in [
        "/Users/boxuser/.config/herdr/config.toml",
        "/Users/boxuser/.aws/cli-agent-orchestrator/settings.json",
        "/Users/boxuser/.claude/settings.json",
    ]:
        assert f"would, as boxuser: keep {path} if it exists, else write it" in out, path
    assert "chown" not in out
    assert "would install config/" not in out
    assert sandbox.recorded_calls() == []


def test_a_freshly_mounted_root_owned_home_is_given_to_the_user(sandbox) -> None:
    home = sandbox.root / "home-mount" / "boxuser"
    home.mkdir(parents=True)
    fake = FakeSystem(sandbox)
    fake.home_owner = "0"
    result = dry_run(sandbox, fake, **{**BOOTSTRAP_SETTINGS, "BOX_HOME": str(home)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--no-create-home" in result.stdout
    assert f"would run: chown boxuser:boxuser {home}\n" in result.stdout
    assert f"would run: chmod 0750 {home}" in result.stdout
    assert f"would run: runuser -u boxuser -- cp -rn /etc/skel/. {home}/" in result.stdout


def test_a_home_owned_by_another_uid_stops(sandbox) -> None:
    home = sandbox.root / "home-mount" / "boxuser"
    home.mkdir(parents=True)
    fake = FakeSystem(sandbox)
    fake.home_owner = "1500"
    result = dry_run(sandbox, fake, **{**BOOTSTRAP_SETTINGS, "BOX_HOME": str(home)})
    assert result.returncode == 1
    assert "belongs to uid 1500, not BOX_UID 2001" in result.stderr
    assert "== 3." not in result.stdout


@pytest.mark.parametrize(
    ("uid", "home", "message"),
    [
        ("1999", "/Users/boxuser", "exists with uid 1999"),
        ("2001", "/home/boxuser", "home is /home/boxuser"),
    ],
)
def test_an_existing_user_must_match_uid_and_home(sandbox, uid, home, message) -> None:
    fake = FakeSystem(sandbox)
    fake.user_uid = uid
    fake.user_home = home
    result = dry_run(sandbox, fake, **BOOTSTRAP_SETTINGS)
    assert result.returncode == 1
    assert message in result.stderr


def test_a_taken_uid_stops(sandbox) -> None:
    fake = FakeSystem(sandbox)
    fake.uid_taken = True
    result = dry_run(sandbox, fake, **BOOTSTRAP_SETTINGS)
    assert result.returncode == 1
    assert "uid 2001 already belongs to another user" in result.stderr


# ------------------------------------------------------------ packages, tools


def test_docker_is_off_by_default(sandbox) -> None:
    result = dry_run(sandbox, FakeSystem(sandbox), **BOOTSTRAP_SETTINGS)
    assert result.returncode == 0
    assert "Docker not installed (INSTALL_DOCKER=no)" in result.stdout
    assert "docker" not in result.stdout.replace("Docker not installed (INSTALL_DOCKER=no)", "")


def test_docker_opt_in_warns_that_the_group_is_root(sandbox) -> None:
    fake = FakeSystem(sandbox)
    result = dry_run(sandbox, fake, INSTALL_DOCKER="yes", **BOOTSTRAP_SETTINGS)
    assert result.returncode == 0, result.stderr
    packages = lines(result)[index_of(result, "3. packages:")].split("3. packages:", 1)[1].split()
    assert any(p == "docker.io" for p in packages)  # the apt package name, compared exactly
    assert "That is root on this box for every agent" in result.stderr
    assert "would run: usermod -aG docker boxuser" in result.stdout


def test_every_download_is_verified_before_it_is_installed(sandbox) -> None:
    result = dry_run(sandbox, FakeSystem(sandbox), **BOOTSTRAP_SETTINGS)
    assert result.returncode == 0, result.stdout + result.stderr
    out = lines(result)
    downloads = [n for n, line in enumerate(out) if "would run: curl " in line]
    assert len(downloads) == 8  # herdr, gh (+ its checksums file), aws, sam, yazi, hx, uv
    assert all("--proto =https" in out[n] for n in downloads)
    herdr_verify = index_of(
        result, f"would verify sha256 of herdr-linux-aarch64 v0.9.3 = {FAKE_SHA}"
    )
    assert index_of(result, "releases/download/v0.9.3/herdr-linux-aarch64") < herdr_verify
    assert herdr_verify < index_of(result, "<download-dir>/herdr /Users/boxuser/.local/bin/herdr")
    assert index_of(result, "gh_2.103.3_checksums.txt") < index_of(result, "would run: dpkg -i")
    for label in [
        "awscli-exe-linux-aarch64-2.0.0.zip",
        "aws-sam-cli-linux-arm64.zip v1.0.0",
        "yazi-aarch64-unknown-linux-gnu.zip",
        "helix-25.07.1-aarch64-linux.tar.xz",
        "uv-aarch64-unknown-linux-gnu.tar.gz 0.1.0",
    ]:
        assert f"would verify sha256 of {label}" in result.stdout, label
    assert "releases/latest" not in result.stdout
    assert "install.sh" not in result.stdout


@pytest.mark.parametrize(
    ("drop", "listed"),
    [
        ("SAM_SHA256", "SAM_VERSION+SAM_SHA256"),
        ("UV_VERSION", "UV_VERSION+UV_SHA256"),
        ("YAZI_SHA256", "YAZI_SHA256"),
        ("AWS_CLI_SHA256", "AWS_CLI_VERSION+AWS_CLI_SHA256"),
    ],
)
def test_a_missing_pin_stops_before_any_download(sandbox, drop, listed) -> None:
    settings = {k: v for k, v in BOOTSTRAP_SETTINGS.items() if k != drop}
    result = dry_run(sandbox, FakeSystem(sandbox), **settings)
    assert result.returncode == 1
    assert listed in result.stderr
    assert "would run: curl" not in result.stdout


# ------------------------------------------ the box user's home (P1-1, #1316)
# Any agent can write in the box user's home. A re-run as root, also with
# --dry-run, must never run a file from it, write through a link in it, or
# change an owner through it.


def box_home(sandbox) -> Path:
    home = sandbox.root / "home-mount" / "boxuser"
    home.mkdir(parents=True)
    return home


def plant_herdr(home: Path, marker: Path) -> bytes:
    """A ~/.local/bin/herdr that leaves MARKER if anything runs it."""
    body = f'#!/bin/sh\necho ran > "{marker}"\necho "herdr 0.9.3"\n'.encode()
    herdr = home / ".local" / "bin" / "herdr"
    herdr.parent.mkdir(parents=True)
    herdr.write_bytes(body)
    herdr.chmod(0o755)
    return body


def test_a_planted_herdr_is_never_run_and_is_downloaded_again(sandbox) -> None:
    home = box_home(sandbox)
    marker = sandbox.root / "herdr-ran"
    plant_herdr(home, marker)
    fake = FakeSystem(sandbox)
    fake.home_owner = "2001"
    result = dry_run(sandbox, fake, **{**BOOTSTRAP_SETTINGS, "BOX_HOME": str(home)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists(), "root ran a herdr from the box user's home"
    assert "herdr v0.9.3 present" not in result.stdout
    assert "releases/download/v0.9.3/herdr-linux-aarch64" in result.stdout
    assert sandbox.recorded_calls() == []


def test_a_dry_run_never_opens_herdr_even_with_the_pinned_hash(sandbox) -> None:
    # A real run has the box user hash it (test_remote_ec2_guards.py); a dry
    # run doesn't look in the home at all, so it plans the download.
    home = box_home(sandbox)
    marker = sandbox.root / "herdr-ran"
    pinned = hashlib.sha256(plant_herdr(home, marker)).hexdigest()
    fake = FakeSystem(sandbox)
    fake.home_owner = "2001"
    settings = {**BOOTSTRAP_SETTINGS, "BOX_HOME": str(home), "HERDR_SHA256": pinned}
    result = dry_run(sandbox, fake, **settings)
    assert result.returncode == 0, result.stdout + result.stderr
    herdr = home / ".local" / "bin" / "herdr"
    assert f"would check, as boxuser, that {herdr} is a regular file with sha256" in result.stdout
    assert "releases/download/v0.9.3/herdr-linux-aarch64" in result.stdout
    assert not marker.exists(), "root ran a herdr from the box user's home"
    assert sandbox.recorded_calls() == []


def link_in_home(home: Path, name: str, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    (target / "keep.txt").write_text("not the box user's\n", encoding="utf-8")
    (home / name).symlink_to(target)


@pytest.mark.parametrize("folder", [".config", ".local"])
def test_a_config_folder_linked_to_a_root_folder_is_refused(sandbox, folder) -> None:
    home = box_home(sandbox)
    target = sandbox.root / "root-owned"
    link_in_home(home, folder, target)
    before = tree_snapshot(target)
    fake = FakeSystem(sandbox)
    fake.home_owner = "2001"
    result = dry_run(sandbox, fake, **{**BOOTSTRAP_SETTINGS, "BOX_HOME": str(home)})
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"{home}/{folder} is a symlink; refusing" in result.stderr
    assert "== 1." not in result.stdout, "the run must stop in step 0, before any change"
    assert "install -d" not in result.stdout and "chown" not in result.stdout
    assert tree_snapshot(target) == before
    assert sandbox.recorded_calls() == []


def test_a_claude_folder_linked_to_the_unit_folder_is_refused(sandbox) -> None:
    home = box_home(sandbox)
    units = sandbox.root / "fakeroot" / "etc" / "systemd" / "system"
    link_in_home(home, ".claude", units)
    before = tree_snapshot(units)
    fake = FakeSystem(sandbox)
    fake.home_owner = "2001"
    result = dry_run(sandbox, fake, **{**BOOTSTRAP_SETTINGS, "BOX_HOME": str(home)})
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"{home}/.claude is a symlink; refusing" in result.stderr
    assert "== 1." not in result.stdout, "the run must stop in step 0, before any change"
    # Nothing in the plan changes an owner, inside the home or out of it.
    assert "chown" not in result.stdout
    assert tree_snapshot(units) == before
    assert sandbox.recorded_calls() == []


def test_a_checkout_inside_the_box_users_home_is_refused(sandbox) -> None:
    # Root sources and runs the sample's files: in the home, any agent could
    # change them before the next re-run.
    home = str(SAMPLE_DIR.parent)
    fake = FakeSystem(sandbox)
    fake.home_owner = "2001"
    result = dry_run(sandbox, fake, **{**BOOTSTRAP_SETTINGS, "BOX_HOME": home})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "inside the box user's home" in result.stderr
    assert "/opt/cao-box-src" in result.stderr
    assert "== 1." not in result.stdout
    assert sandbox.recorded_calls() == []


# ---------------------------------------------------- session name and units


def test_the_real_branch_enables_but_never_starts(sandbox) -> None:
    result = dry_run(sandbox, FakeSystem(sandbox), **BOOTSTRAP_SETTINGS)
    assert result.returncode == 0, result.stderr
    runs = [line for line in lines(result) if "would run: systemctl" in line]
    assert runs == [
        "[dry-run] would run: systemctl daemon-reload",
        "[dry-run] would run: systemctl enable herdr-cao.service cao-server.service cao-backup.timer",
    ]
    assert "--now" not in result.stdout
    assert "systemctl start" not in "\n".join(l for l in lines(result) if "would run" in l)


def test_the_session_name_would_come_from_imds_and_go_to_the_volume(sandbox) -> None:
    result = dry_run(sandbox, FakeSystem(sandbox), **BOOTSTRAP_SETTINGS)
    assert result.returncode == 0
    assert (
        "would read the instance id from IMDSv2 and write /Users/.cao-box/box.env and /etc/cao-box/box.env"
        in result.stdout
    )
    assert sandbox.recorded_calls() == []
