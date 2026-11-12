"""herdr-start.sh and herdr-clean-layout.sh: empty layout, minimal environment."""

from __future__ import annotations

import json
from pathlib import Path

from remote_ec2_support import FAKE_INSTANCE_ID, write_stub

SESSION = f"cao-{FAKE_INSTANCE_ID}"
ALLOWED = {"HOME", "USER", "PATH", "LANG", "TERM"}
# Names a POSIX shell sets for itself when it starts; they come from the fake
# herdr (a /bin/sh script), not from herdr-start.sh. macOS adds the CF one.
SHELL_NOISE = {"PWD", "SHLVL", "_", "OLDPWD", "__CF_USER_TEXT_ENCODING"}


def session_dir(home: Path) -> Path:
    return home / ".config" / "herdr" / "sessions" / SESSION


def seed_layout(home: Path) -> Path:
    folder = session_dir(home)
    (folder / "session-snapshots").mkdir(parents=True)
    (folder / "session-snapshots" / "snap-1.json").write_text("{}", encoding="utf-8")
    (folder / "session.json").write_text(json.dumps({"panes": ["old"]}), encoding="utf-8")
    return folder


def test_clean_layout_archives_the_saved_layout_and_keeps_snapshots(sandbox) -> None:
    folder = seed_layout(sandbox.home)
    result = sandbox.run("bin/herdr-clean-layout.sh", SESSION)
    assert result.returncode == 0, result.stderr
    assert not (folder / "session.json").exists()
    [archived] = list((folder / "restore-archive").glob("*/session.json"))
    assert json.loads(archived.read_text(encoding="utf-8")) == {"panes": ["old"]}
    assert (folder / "session-snapshots" / "snap-1.json").exists()

    again = sandbox.run("bin/herdr-clean-layout.sh", SESSION)
    assert again.returncode == 0
    assert "nothing to do" in again.stdout
    assert len(list((folder / "restore-archive").glob("*/session.json"))) == 1


def test_clean_layout_rejects_unsafe_session_names(sandbox) -> None:
    for name in ["", "../escape", "Cao Box", "a/b"]:
        result = sandbox.run("bin/herdr-clean-layout.sh", name)
        assert result.returncode == 2, name


def herdr_capture(sandbox) -> tuple[Path, Path]:
    """A fake herdr that records its argv and its environment NAMES (never values)."""
    names = sandbox.root / "herdr-env-names.txt"
    argv = sandbox.root / "herdr-argv.txt"
    fake = write_stub(
        sandbox.stubs,
        "fake-herdr",
        f'echo "$*" > "{argv}"\n/usr/bin/env | /usr/bin/cut -d= -f1 | /usr/bin/sort > "{names}"\n',
    )
    return fake, names


def test_herdr_start_runs_the_session_with_only_the_allowed_names(sandbox) -> None:
    folder = seed_layout(sandbox.home)
    box_env = sandbox.root / "box.env"
    box_env.write_text(
        f"CAO_BOX_SESSION={SESSION}\nCAO_HERDR_SESSION={SESSION}\n", encoding="utf-8"
    )
    fake, names_file = herdr_capture(sandbox)

    result = sandbox.run(
        "bin/herdr-start.sh",
        BOX_ENV_FILE=str(box_env),
        HERDR_BIN=str(fake),
        USER="boxuser",
        TERM="xterm-256color",
        # Things a careless unit might leak; none may reach herdr.
        SECRET_TOKEN_CANARY="x",
        CAO_AUTH_LOCAL_TOKEN="CANARY",
        INVOCATION_ID="CANARY",
        AWS_PROFILE="CANARY",
    )
    assert result.returncode == 0, result.stderr
    assert (sandbox.root / "herdr-argv.txt").read_text(encoding="utf-8").split() == [
        "--session",
        SESSION,
        "server",
    ]
    names = set(names_file.read_text(encoding="utf-8").split())
    assert names - SHELL_NOISE == ALLOWED
    assert not (folder / "session.json").exists(), "the layout must be archived before herdr starts"


def test_herdr_start_refuses_a_missing_or_bad_box_env(sandbox) -> None:
    fake, _ = herdr_capture(sandbox)
    missing = sandbox.run(
        "bin/herdr-start.sh", BOX_ENV_FILE=str(sandbox.root / "nope"), HERDR_BIN=str(fake)
    )
    assert missing.returncode == 1
    bad = sandbox.root / "box.env"
    bad.write_text("CAO_BOX_SESSION=$(touch /tmp/x)\n", encoding="utf-8")
    result = sandbox.run("bin/herdr-start.sh", BOX_ENV_FILE=str(bad), HERDR_BIN=str(fake))
    assert result.returncode == 1
    assert "malformed" in result.stderr
    assert not (sandbox.root / "herdr-argv.txt").exists()
