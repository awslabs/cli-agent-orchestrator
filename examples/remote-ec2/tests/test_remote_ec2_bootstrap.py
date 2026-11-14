"""bootstrap.sh under --root: a dry run changes nothing; a real run writes the
right files with the right modes and never calls IMDS or a system command."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from remote_ec2_support import (
    BOOTSTRAP_SETTINGS,
    FAKE_INSTANCE_ID,
    MUTATING,
    recording_stub,
    tree_snapshot,
    write_stub,
)

SETTINGS = {
    **BOOTSTRAP_SETTINGS,
    "BACKUP_S3_URI": "s3://example-bucket/cao-box/",
    "BACKUP_KMS_KEY_ARN": "arn:aws:kms:ap-southeast-2:0:key/test-key",
}
SESSION = f"cao-{FAKE_INSTANCE_ID}"
UNITS = ["herdr-cao.service", "cao-server.service", "cao-backup.service", "cao-backup.timer"]
# A real --root run writes files itself, so these must stay real commands.
FILE_TOOLS = {"mkdir", "chmod", "cp", "mv", "install", "ln"}


def make_root(base: Path, os_id: str = "ubuntu", version: str = "24.04") -> Path:
    root = base / "fakeroot"
    (root / "etc").mkdir(parents=True)
    (root / "etc" / "os-release").write_text(
        f'NAME="Ubuntu"\nID={os_id}\nVERSION_ID="{version}"\n', encoding="utf-8"
    )
    return root


def arm_stubs(sandbox, arch: str = "aarch64", skip: frozenset[str] = frozenset()) -> None:
    write_stub(sandbox.stubs, "uname", f'case "$1" in -m) echo {arch} ;; *) echo Linux ;; esac\n')
    for name in MUTATING:
        if name not in skip:
            recording_stub(sandbox.stubs, name, sandbox.calls)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_dry_run_in_a_fake_root_prints_the_plan_and_changes_nothing(sandbox) -> None:
    root = make_root(sandbox.root)
    arm_stubs(sandbox)
    tmpdir = sandbox.root / "dry-tmp"
    tmpdir.mkdir()
    before = tree_snapshot(root)

    result = sandbox.run(
        "bootstrap.sh", "--dry-run", "--root", str(root), TMPDIR=str(tmpdir), **SETTINGS
    )

    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    for n in range(9):
        assert f"== {n}." in out, f"step {n} missing from the plan"
    assert "== done" in out
    assert "OS: ubuntu 24.04 aarch64" in out
    for unit in UNITS:
        assert f"/etc/systemd/system/{unit}" in out
    assert "wait-herdr-socket.sh" in out
    assert "[dry-run] would run: systemctl --root=" in out
    assert "--now" not in out
    assert "IMDS is never called under --root" in out
    assert tree_snapshot(root) == before
    assert sandbox.recorded_calls() == []
    assert list(tmpdir.iterdir()) == [], "a dry run must not create temp folders"


def test_a_second_dry_run_is_identical(sandbox) -> None:
    root = make_root(sandbox.root)
    arm_stubs(sandbox)
    first = sandbox.run("bootstrap.sh", "--dry-run", "--root", str(root), **SETTINGS)
    second = sandbox.run("bootstrap.sh", "--dry-run", "--root", str(root), **SETTINGS)
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout


def seed_volume_box_env(root: Path) -> Path:
    vol = root / "Users" / ".cao-box" / "box.env"
    vol.parent.mkdir(parents=True)
    vol.write_text(f"CAO_BOX_SESSION={SESSION}\nCAO_HERDR_SESSION={SESSION}\n", encoding="utf-8")
    return vol


def test_a_real_root_run_writes_the_files_with_the_right_modes(sandbox) -> None:
    root = make_root(sandbox.root)
    vol = seed_volume_box_env(root)
    arm_stubs(sandbox, skip=frozenset(FILE_TOOLS))

    result = sandbox.run("bootstrap.sh", "--root", str(root), **SETTINGS)
    assert result.returncode == 0, result.stdout + result.stderr

    etc_env = root / "etc" / "cao-box" / "box.env"
    assert etc_env.read_text(encoding="utf-8") == vol.read_text(encoding="utf-8")
    assert mode(etc_env) == 0o644
    backup_env = root / "etc" / "cao-box" / "backup.env"
    assert mode(backup_env) == 0o644
    assert "BACKUP_KMS_KEY_ARN=arn:aws:kms:" in backup_env.read_text(encoding="utf-8")
    for unit in UNITS:
        path = root / "etc" / "systemd" / "system" / unit
        assert mode(path) == 0o644
        text = path.read_text(encoding="utf-8")
        assert "@BOX_" not in text
        if not unit.endswith(".timer"):
            assert "/Users/boxuser" in text
    for script in ["herdr-start.sh", "wait-herdr-socket.sh", "backup.sh", "restore.sh"]:
        assert mode(root / "usr" / "local" / "lib" / "cao-box" / script) == 0o755
    settings = root / "Users" / "boxuser" / ".aws" / "cli-agent-orchestrator" / "settings.json"
    assert f'"herdr_session": "{SESSION}"' in settings.read_text(encoding="utf-8")
    assert mode(root / "Users" / "boxuser" / ".claude" / "settings.json") == 0o600
    # The only system command a --root run may call is the offline enable.
    assert sandbox.recorded_calls() == [
        f"systemctl --root={root} enable herdr-cao.service cao-server.service cao-backup.timer"
    ]

    again = sandbox.run("bootstrap.sh", "--root", str(root), **SETTINGS)
    assert again.returncode == 0, again.stderr
    assert "installed:" not in again.stdout and "wrote:" not in again.stdout


def test_a_root_run_without_box_env_never_calls_imds(sandbox) -> None:
    root = make_root(sandbox.root)
    arm_stubs(sandbox, skip=frozenset(FILE_TOOLS))
    result = sandbox.run("bootstrap.sh", "--root", str(root), **SETTINGS)
    assert result.returncode == 0, result.stderr
    assert "IMDS is never called under --root" in result.stdout
    assert not (root / "etc" / "cao-box" / "box.env").exists()
    assert not any(call.startswith("curl") for call in sandbox.recorded_calls())


def test_settings_that_disagree_with_box_env_stop_the_run(sandbox) -> None:
    root = make_root(sandbox.root)
    seed_volume_box_env(root)
    settings = root / "Users" / "boxuser" / ".aws" / "cli-agent-orchestrator" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        '{"terminal": {"herdr_session": "cao-i-0000000000000000b"}}\n', encoding="utf-8"
    )
    arm_stubs(sandbox, skip=frozenset(FILE_TOOLS))
    result = sandbox.run("bootstrap.sh", "--root", str(root), **SETTINGS)
    assert result.returncode == 1
    assert "make them match" in result.stderr


def test_a_changed_backup_target_is_rewritten_and_an_unset_one_is_kept(sandbox) -> None:
    root = make_root(sandbox.root)
    seed_volume_box_env(root)
    arm_stubs(sandbox, skip=frozenset(FILE_TOOLS))
    backup_env = root / "etc" / "cao-box" / "backup.env"
    assert sandbox.run("bootstrap.sh", "--root", str(root), **SETTINGS).returncode == 0
    changed = {**SETTINGS, "BACKUP_S3_URI": "s3://example-bucket/other/"}
    assert sandbox.run("bootstrap.sh", "--root", str(root), **changed).returncode == 0
    assert "s3://example-bucket/other/" in backup_env.read_text(encoding="utf-8")
    unset = {k: v for k, v in SETTINGS.items() if not k.startswith("BACKUP_")}
    result = sandbox.run("bootstrap.sh", "--root", str(root), **unset)
    assert result.returncode == 0
    assert "kept" in result.stdout and "s3://example-bucket/other/" in result.stdout
    assert "s3://example-bucket/other/" in backup_env.read_text(encoding="utf-8")


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


@pytest.mark.parametrize(
    ("drop_or_change", "message"),
    [
        ({"CAO_REF": None}, "set CAO_REF"),
        ({"HERDR_SHA256": None}, "set HERDR_SHA256"),
        ({"BOX_UID": None}, "set BOX_UID"),
        ({"HERDR_SHA256": "abc"}, "64 lowercase hex"),
        ({"BOX_UID": "0"}, "BOX_UID must be"),
        ({"BOX_USER": "x; rm"}, "plain Linux user name"),
        ({"BOX_HOME": "/Users/boxuser/a b"}, "BOX_HOME must be"),
        ({"BOX_HOME": "/Users/boxuser/a|b"}, "BOX_HOME must be"),
        ({"BOX_HOME": "/Users/../etc"}, "contain .."),
        ({"DATA_VOLUME_ID": "vol-123"}, "DATA_VOLUME_ID looks like"),
        ({"DATA_DEVICE": "/dev/nvme1n1"}, "DATA_DEVICE alone is not accepted"),
        ({"BACKUP_KMS_KEY_ARN": None}, "BACKUP_KMS_KEY_ARN"),
        ({"INSTALL_DOCKER": "maybe"}, "INSTALL_DOCKER is yes or no"),
    ],
)
def test_bad_settings_stop_the_run(sandbox, drop_or_change, message) -> None:
    root = make_root(sandbox.root)
    arm_stubs(sandbox)
    settings = dict(SETTINGS)
    for key, value in drop_or_change.items():
        if value is None:
            settings.pop(key, None)
        else:
            settings[key] = value
    result = sandbox.run("bootstrap.sh", "--dry-run", "--root", str(root), **settings)
    assert result.returncode != 0
    assert message in result.stderr
    assert "== 1." not in result.stdout


def test_unknown_flag_is_an_error(sandbox) -> None:
    result = sandbox.run("bootstrap.sh", "--now")
    assert result.returncode == 2
    assert "unknown argument" in result.stderr


def test_help_prints_the_header(sandbox) -> None:
    result = sandbox.run("bootstrap.sh", "--help")
    assert result.returncode == 0
    assert "--format-new-volume" in result.stdout and "set -euo" not in result.stdout


# ------------------------------------------------- N1: test hooks are dry-only

HOOKS = [
    "BOX_TEST_OS_RELEASE",
    "BOX_TEST_DEVFS",
    "BOX_TEST_SYSFS",
    "BOX_TEST_FSTAB",
    "BOX_TEST_ANYTHING",
]


@pytest.mark.parametrize("hook", HOOKS)
@pytest.mark.parametrize("with_root", [False, True], ids=["real", "real-root"])
def test_a_real_run_with_a_test_hook_stops_before_any_step(sandbox, hook, with_root) -> None:
    root = make_root(sandbox.root)
    arm_stubs(sandbox, skip=frozenset(FILE_TOOLS))
    args = ["--root", str(root)] if with_root else []
    result = sandbox.run("bootstrap.sh", *args, **{**SETTINGS, hook: str(sandbox.root / "fake")})
    assert result.returncode == 1
    assert f"{hook} set: BOX_TEST_* variables are for --dry-run tests only" in result.stderr
    assert "== 0." not in result.stdout
    assert sandbox.recorded_calls() == []
    assert not (root / "etc" / "cao-box").exists()


def test_a_test_hook_with_dry_run_is_allowed(sandbox) -> None:
    root = make_root(sandbox.root)
    arm_stubs(sandbox)
    result = sandbox.run(
        "bootstrap.sh",
        "--dry-run",
        "--root",
        str(root),
        **{**SETTINGS, "BOX_TEST_FSTAB": str(sandbox.root / "fstab")},
    )
    assert result.returncode == 0, result.stderr


def test_the_env_form_of_the_format_flag_warns(sandbox) -> None:
    root = make_root(sandbox.root)
    arm_stubs(sandbox)
    result = sandbox.run(
        "bootstrap.sh", "--dry-run", "--root", str(root), FORMAT_DATA_DEVICE="yes", **SETTINGS
    )
    assert result.returncode == 0
    assert "remove it after the first run" in result.stderr


# ------------------------------------------- N2: box.env is parsed, not copied


@pytest.mark.parametrize(
    "extra",
    [
        "LD_PRELOAD=/dev/shm/x.so\n",
        "CAO_HERDR_SESSION=cao-other\n",
        "NODE_OPTIONS=--require /dev/shm/x.js\n",
    ],
    ids=["ld-preload", "duplicate-herdr-session", "node-options"],
)
@pytest.mark.parametrize("where", ["volume", "etc"])
def test_a_tampered_box_env_stops_the_run(sandbox, extra, where) -> None:
    root = make_root(sandbox.root)
    if where == "volume":
        target = seed_volume_box_env(root)
    else:
        target = root / "etc" / "cao-box" / "box.env"
        target.parent.mkdir(parents=True)
        target.write_text(
            f"CAO_BOX_SESSION={SESSION}\nCAO_HERDR_SESSION={SESSION}\n", encoding="utf-8"
        )
    target.write_text(target.read_text(encoding="utf-8") + extra, encoding="utf-8")
    arm_stubs(sandbox, skip=frozenset(FILE_TOOLS))
    result = sandbox.run("bootstrap.sh", "--root", str(root), **SETTINGS)
    assert result.returncode == 1
    assert "refusing" in result.stderr and "two session lines" in result.stderr
    etc_env = root / "etc" / "cao-box" / "box.env"
    if where == "volume":
        assert not etc_env.exists(), "nothing may be copied from a tampered volume file"
    assert not (root / "etc" / "systemd").exists(), "the run must stop before the units"


def test_a_box_env_whose_two_sessions_differ_stops_the_run(sandbox) -> None:
    root = make_root(sandbox.root)
    vol = seed_volume_box_env(root)
    vol.write_text(
        f"CAO_BOX_SESSION={SESSION}\nCAO_HERDR_SESSION=cao-i-0000000000000000b\n", encoding="utf-8"
    )
    arm_stubs(sandbox, skip=frozenset(FILE_TOOLS))
    result = sandbox.run("bootstrap.sh", "--root", str(root), **SETTINGS)
    assert result.returncode == 1
    assert "differ" in result.stderr


def test_a_world_writable_volume_box_env_is_refused(sandbox) -> None:
    root = make_root(sandbox.root)
    vol = seed_volume_box_env(root)
    vol.chmod(0o666)
    arm_stubs(sandbox, skip=frozenset(FILE_TOOLS))
    result = sandbox.run("bootstrap.sh", "--root", str(root), **SETTINGS)
    assert result.returncode == 1
    assert "mode 0644 or tighter" in result.stderr
    assert not (root / "etc" / "cao-box" / "box.env").exists()


@pytest.mark.parametrize("folder", ["state", "mount"])
def test_a_state_folder_others_can_write_is_refused(sandbox, folder) -> None:
    root = make_root(sandbox.root)
    vol = seed_volume_box_env(root)
    (vol.parent if folder == "state" else vol.parent.parent).chmod(0o777)
    arm_stubs(sandbox, skip=frozenset(FILE_TOOLS))
    result = sandbox.run("bootstrap.sh", "--root", str(root), **SETTINGS)
    assert result.returncode == 1
    assert "refusing" in result.stderr
    assert not (root / "etc" / "cao-box" / "box.env").exists()


def test_box_env_is_rendered_from_the_two_values(sandbox) -> None:
    root = make_root(sandbox.root)
    vol = seed_volume_box_env(root)
    # Same values, other order: still valid, and /etc gets the canonical text.
    vol.write_text(f"CAO_HERDR_SESSION={SESSION}\nCAO_BOX_SESSION={SESSION}\n", encoding="utf-8")
    arm_stubs(sandbox, skip=frozenset(FILE_TOOLS))
    result = sandbox.run("bootstrap.sh", "--root", str(root), **SETTINGS)
    assert result.returncode == 0, result.stderr
    canonical = f"CAO_BOX_SESSION={SESSION}\nCAO_HERDR_SESSION={SESSION}\n"
    assert (root / "etc" / "cao-box" / "box.env").read_text(encoding="utf-8") == canonical
    assert vol.read_text(encoding="utf-8") == canonical
    user_env = root / "etc" / "cao-box" / "box-user.env"
    assert (
        user_env.read_text(encoding="utf-8")
        == "BOX_USER=boxuser\nBOX_UID=2001\nBOX_HOME=/Users/boxuser\n"
    )
    assert mode(user_env) == 0o644


def test_a_home_layout_volume_keeps_box_env_in_etc_only(sandbox) -> None:
    # BOX_HOME=DATA_MOUNT: the volume is the user's home, so there is no
    # root-only folder on it. bootstrap must not read or write a copy there.
    root = make_root(sandbox.root)
    home = root / "home" / "boxuser"
    home.mkdir(parents=True)
    home.chmod(0o770)  # writable by the user's group, like a home could be
    etc = root / "etc" / "cao-box"
    etc.mkdir(parents=True)
    (etc / "box.env").write_text(
        f"CAO_BOX_SESSION={SESSION}\nCAO_HERDR_SESSION={SESSION}\n", encoding="utf-8"
    )
    arm_stubs(sandbox, skip=frozenset(FILE_TOOLS))
    settings = {**SETTINGS, "BOX_HOME": "/home/boxuser", "DATA_MOUNT": "/home/boxuser"}
    result = sandbox.run("bootstrap.sh", "--root", str(root), **settings)
    assert result.returncode == 0, result.stderr
    assert "box.env is kept in /etc/cao-box only" in result.stderr
    assert not (home / ".cao-box").exists()


def test_an_unsafe_empty_state_folder_is_refused_not_written_into(sandbox) -> None:
    # Someone made the volume's state folder writable and left it empty,
    # hoping bootstrap writes box.env there for them to swap later.
    root = make_root(sandbox.root)
    state = root / "Users" / ".cao-box"
    state.mkdir(parents=True)
    state.chmod(0o777)
    etc = root / "etc" / "cao-box"
    etc.mkdir(parents=True)
    (etc / "box.env").write_text(
        f"CAO_BOX_SESSION={SESSION}\nCAO_HERDR_SESSION={SESSION}\n", encoding="utf-8"
    )
    arm_stubs(sandbox, skip=frozenset(FILE_TOOLS))
    result = sandbox.run("bootstrap.sh", "--root", str(root), **SETTINGS)
    assert result.returncode == 1
    assert f"refusing {state}" in result.stderr
    assert list(state.iterdir()) == []


def test_a_home_layout_never_reads_a_planted_state_folder(sandbox) -> None:
    # F3: an agent creates ~/.cao-box with junk. bootstrap must not stop (that
    # would block upgrades) and must not read or write it.
    root = make_root(sandbox.root)
    home = root / "home" / "boxuser"
    planted = home / ".cao-box"
    planted.mkdir(parents=True)
    planted.chmod(0o777)
    (planted / "box.env").write_text("LD_PRELOAD=/dev/shm/x.so\n", encoding="utf-8")
    etc = root / "etc" / "cao-box"
    etc.mkdir(parents=True)
    (etc / "box.env").write_text(
        f"CAO_BOX_SESSION={SESSION}\nCAO_HERDR_SESSION={SESSION}\n", encoding="utf-8"
    )
    arm_stubs(sandbox, skip=frozenset(FILE_TOOLS))
    settings = {**SETTINGS, "BOX_HOME": "/home/boxuser", "DATA_MOUNT": "/home/boxuser"}
    result = sandbox.run("bootstrap.sh", "--root", str(root), **settings)
    assert result.returncode == 0, result.stderr
    assert "nothing on the volume is read" in result.stderr
    assert (planted / "box.env").read_text(encoding="utf-8") == "LD_PRELOAD=/dev/shm/x.so\n"
    assert (etc / "box.env").read_text(encoding="utf-8") == (
        f"CAO_BOX_SESSION={SESSION}\nCAO_HERDR_SESSION={SESSION}\n"
    )


# ------------------------------- links planted in the box user's home (P1-1)
# Under --root nobody else owns the tree, but step 7 must refuse a link in the
# home the same way: the code is the code a real box runs, as the box user.


@pytest.mark.parametrize(
    ("link", "target", "written"),
    [
        (".claude", "etc/systemd/system", "settings.json"),
        (".config", "root-owned", "herdr/config.toml"),
        (".aws/cli-agent-orchestrator/settings.json", "etc/systemd/system/x.service", ""),
    ],
    ids=[
        "claude-folder-to-units",
        "config-folder-to-root-folder",
        "cao-settings-dangling-to-a-unit",
    ],
)
def test_a_real_root_run_never_writes_through_a_link_in_the_home(
    sandbox, link, target, written
) -> None:
    root = make_root(sandbox.root)
    seed_volume_box_env(root)
    home = root / "Users" / "boxuser"
    outside = root / target
    if written:
        outside.mkdir(parents=True)
        (outside / "keep.txt").write_text("root's\n", encoding="utf-8")
    else:
        outside.parent.mkdir(parents=True)  # a dangling link: x.service doesn't exist
    planted = home / link
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.symlink_to(outside)
    watched = outside if written else outside.parent
    before = tree_snapshot(watched)
    arm_stubs(sandbox, skip=frozenset(FILE_TOOLS))

    result = sandbox.run("bootstrap.sh", "--root", str(root), **SETTINGS)

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"{planted} is a symlink; refusing" in result.stderr
    assert tree_snapshot(watched) == before, "the run wrote through the link"
    if written:
        assert not (outside / written).exists()
    else:
        assert not outside.exists()
    assert not any(c.startswith("chown") for c in sandbox.recorded_calls())
    assert not (root / "etc" / "systemd" / "system" / "herdr-cao.service").exists()
    # Refused in step 0: steps 1 to 6 changed nothing either.
    assert "== 1." not in result.stdout
    assert not (root / "etc" / "cao-box").exists()



# --------------------------------- --settings: parsed, never sourced (P2-1)
# The README used `sudo env $(cat box-settings.env) bash bootstrap.sh`, which
# makes every line root's environment (BASH_ENV=... runs a file as root).
# bootstrap.sh now reads the file itself and accepts only its own settings.


def write_settings(path: Path, lines: list[str], mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def settings_lines() -> list[str]:
    lines = [f"{k}={v}" for k, v in SETTINGS.items()]
    lines[1] += "                 # 1000-59999; pick one and never change it"
    return ["# box settings", "", *lines]


def test_a_settings_file_is_read_without_the_environment(sandbox) -> None:
    root = make_root(sandbox.root)
    arm_stubs(sandbox)
    kit = write_settings(sandbox.root / "kit" / "box-settings.env", settings_lines())
    result = sandbox.run("bootstrap.sh", "--dry-run", "--root", str(root), "--settings", str(kit))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "settings read from" in result.stdout
    assert "user boxuser (uid 2001) with home /Users/boxuser" in result.stdout
    assert sandbox.recorded_calls() == []


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("BASH_ENV={marker_script}", "BASH_ENV is not a bootstrap setting"),
        ("LD_PRELOAD=/dev/shm/x.so", "LD_PRELOAD is not a bootstrap setting"),
        ("FOO=1", "FOO is not a bootstrap setting"),
        ("BOX_USER=other", "BOX_USER is set twice"),
        ("BOX_HOME=/Users/<you>", "expected KEY=VALUE"),
        ("CAO_REPO=$(touch {marker})", "expected KEY=VALUE"),
        ("export PATH=/tmp", "expected KEY=VALUE"),
    ],
    ids=["bash-env", "ld-preload", "unknown", "twice", "placeholder", "command", "export"],
)
def test_a_settings_file_with_anything_else_is_refused(sandbox, extra, message) -> None:
    root = make_root(sandbox.root)
    arm_stubs(sandbox)
    marker = sandbox.root / "sourced"
    script = sandbox.root / "evil.sh"
    script.write_text(f'touch "{marker}"\n', encoding="utf-8")
    line = extra.format(marker_script=script, marker=marker)
    kit = write_settings(sandbox.root / "kit" / "box-settings.env", [*settings_lines(), line])
    result = sandbox.run("bootstrap.sh", "--dry-run", "--root", str(root), "--settings", str(kit))
    assert result.returncode == 1, result.stdout + result.stderr
    assert message in result.stderr
    assert "== 1." not in result.stdout
    assert not marker.exists(), "a settings line ran code"


def stat_says(sandbox, path: Path, answer: str) -> None:
    """A stat stub that answers ANSWER ("<uid> <mode>") for PATH only."""
    real = "/usr/bin/stat"
    write_stub(
        sandbox.stubs,
        "stat",
        f'case "$*" in\n  *"{path}"*) echo "{answer}" ;;\n  *) exec {real} "$@" ;;\nesac\n',
    )


@pytest.mark.parametrize("case", ["group-writable", "symlink", "in-the-home", "home-owned"])
def test_an_unsafe_settings_file_is_refused(sandbox, case) -> None:
    root = make_root(sandbox.root)
    arm_stubs(sandbox)
    kit = sandbox.root / "kit" / "box-settings.env"
    if case == "group-writable":
        write_settings(kit, settings_lines(), mode=0o620)
        message = "must be yours"
    elif case == "symlink":
        real = write_settings(sandbox.root / "elsewhere" / "s.env", settings_lines())
        kit.parent.mkdir(parents=True)
        kit.symlink_to(real)
        message = "is a symlink"
    elif case == "in-the-home":
        kit = write_settings(root / "Users" / "boxuser" / "box-settings.env", settings_lines())
        message = "inside the box user's home"
    else:
        write_settings(kit, settings_lines())
        stat_says(sandbox, kit, "2001 600")  # as if the box user (uid 2001) owned it
        message = "must be yours"
    result = sandbox.run("bootstrap.sh", "--dry-run", "--root", str(root), "--settings", str(kit))
    assert result.returncode == 1, result.stdout + result.stderr
    assert message in result.stderr
    assert "== 1." not in result.stdout
    assert sandbox.recorded_calls() == []


def test_settings_needs_a_file(sandbox) -> None:
    result = sandbox.run("bootstrap.sh", "--settings")
    assert result.returncode == 2
    assert "--settings needs a file" in result.stderr
