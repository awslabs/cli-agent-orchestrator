"""Guards that can't be reached end to end without real root, tested by
running the exact function text from the script.

- bootstrap.sh's verify(): with DRY_RUN=0, a real checksum mismatch must stop
  the run (F2).
- restore.sh's refuse_overrides_as_root(): as EUID 0, SYSTEMCTL and
  CAO_BOX_ETC are refused unless --dry-run (F4). EUID can't be faked, so the
  function is called with the uid it would see.
- bootstrap.sh's home helpers on a real box (P1-1): every file in the box
  user's home is written through runuser, and a planted link stops the run
  before runuser is called.
"""

from __future__ import annotations

import hashlib
import re
import stat
import subprocess
from pathlib import Path

import pytest
from remote_ec2_support import (
    BASH,
    FAKE_INSTANCE_ID,
    OTHER_INSTANCE_ID,
    SAMPLE_DIR,
    system_path,
    write_stub,
)

LIB = SAMPLE_DIR / "bin" / "lib-cao-box.sh"


def extract(script: str, name: str) -> str:
    text = (SAMPLE_DIR / script).read_text(encoding="utf-8")
    match = re.search(rf"^# >>> {name}\n(.*?)^# <<< {name}\n", text, re.S | re.M)
    assert match, f"{script} has no marked {name} block"
    return match.group(1)


def run(snippet: str, tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, "-c", f"set -euo pipefail\n{snippet}\necho reached-the-end\n"],
        env={"PATH": system_path(), "HOME": str(tmp_path), "LANG": "C", **env},
        capture_output=True,
        text=True,
        check=False,
    )


BOOTSTRAP_PRELUDE = (
    f'. "{LIB}"\n'
    "log() { printf '[bootstrap] %s\\n' \"$*\"; }\n"
    "die() { printf '[bootstrap] ERROR: %s\\n' \"$*\" >&2; exit 1; }\n"
)


def test_bootstrap_verify_stops_on_a_real_mismatch(tmp_path) -> None:
    f = tmp_path / "herdr-linux-aarch64"
    f.write_bytes(b"not the real herdr")
    wrong = hashlib.sha256(b"the real herdr").hexdigest()
    snippet = BOOTSTRAP_PRELUDE + "DRY_RUN=0\n" + extract("bootstrap.sh", "verify")
    result = run(snippet + f'verify "{f}" "{wrong}" "herdr-linux-aarch64 v0.9.3"', tmp_path)
    assert result.returncode == 1
    assert "sha256 mismatch for herdr-linux-aarch64 v0.9.3" in result.stderr
    assert "reached-the-end" not in result.stdout
    assert "sha256 ok" not in result.stdout


def test_bootstrap_verify_passes_the_right_hash(tmp_path) -> None:
    f = tmp_path / "herdr-linux-aarch64"
    f.write_bytes(b"the real herdr")
    right = hashlib.sha256(b"the real herdr").hexdigest()
    snippet = BOOTSTRAP_PRELUDE + "DRY_RUN=0\n" + extract("bootstrap.sh", "verify")
    result = run(snippet + f'verify "{f}" "{right}" herdr', tmp_path)
    assert result.returncode == 0, result.stderr
    assert "sha256 ok: herdr" in result.stdout


def test_bootstrap_verify_only_prints_in_a_dry_run(tmp_path) -> None:
    snippet = BOOTSTRAP_PRELUDE + "DRY_RUN=1\n" + extract("bootstrap.sh", "verify")
    result = run(snippet + 'verify "/nonexistent" "' + "ab" * 32 + '" herdr', tmp_path)
    assert result.returncode == 0
    assert "[dry-run] would verify sha256 of herdr" in result.stdout


GUARD = extract("restore.sh", "refuse_overrides_as_root")


@pytest.mark.parametrize("override", ["SYSTEMCTL", "CAO_BOX_ETC"])
def test_root_refuses_test_overrides_on_a_real_run(tmp_path, override) -> None:
    result = run(GUARD + "refuse_overrides_as_root 0 0", tmp_path, **{override: "/tmp/x"})
    assert result.returncode == 2
    assert "test overrides" in result.stderr
    assert "reached-the-end" not in result.stdout


@pytest.mark.parametrize(
    ("euid", "dry", "env"),
    [("0", "1", {"SYSTEMCTL": "/tmp/x"}), ("1000", "0", {"SYSTEMCTL": "/tmp/x"}), ("0", "0", {})],
    ids=["root-dry-run", "not-root", "root-no-overrides"],
)
def test_overrides_are_fine_otherwise(tmp_path, euid, dry, env) -> None:
    result = run(GUARD + f"refuse_overrides_as_root {euid} {dry}", tmp_path, **env)
    assert result.returncode == 0, result.stderr
    assert "reached-the-end" in result.stdout


def test_restore_calls_the_guard_with_the_real_euid() -> None:
    text = (SAMPLE_DIR / "restore.sh").read_text(encoding="utf-8")
    assert '\nrefuse_overrides_as_root "${EUID}" "$dry"\n' in text


# ------------------------------------ the home helpers on a real box (P1-1)
# A real run (not --root) needs root, so it can't run in a test. The exact
# helper text runs here instead, with system_step true and a runuser stub that
# logs its arguments and then runs the command (the tests are one user).

HOME_HELPERS = extract("bootstrap.sh", "home-helpers")
CLAUDE_EXAMPLE = SAMPLE_DIR / "config" / "claude-settings.box.json.example"


def real_box(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    stubs = tmp_path / "stubs"
    calls = tmp_path / "calls.log"
    write_stub(
        stubs,
        "runuser",
        # Only the first 8 arguments: the 9th is the multi-line program.
        f'echo "runuser $1 $2 $3 $4 $5 $6 $7 $8" >> "{calls}"\n'
        '[ "$1" = "-u" ] && [ "$3" = "--" ] || exit 64\n'
        'shift 3\nexec "$@"\n',
    )
    prelude = (
        BOOTSTRAP_PRELUDE
        + 'DRY_RUN=0\nsystem_step() { true; }\nrun() { "$@"; }\n'
        + f'BOX_USER=boxuser\nBOX_HOME="{home}"\nHOME_DIR="{home}"\n'
        + HOME_HELPERS
    )

    def go(snippet: str) -> subprocess.CompletedProcess[str]:
        return run(prelude + snippet, tmp_path, PATH=f"{stubs}:{system_path()}")

    def logged() -> list[str]:
        return calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []

    return home, go, logged


def test_a_real_box_writes_home_files_only_as_the_box_user(tmp_path) -> None:
    home, go, logged = real_box(tmp_path)
    dest = home / ".claude" / "settings.json"
    put = f'put_home_file claude "{dest}" 0600 "" example < "{CLAUDE_EXAMPLE}"\n'
    result = go(put)
    assert result.returncode == 0, result.stderr
    assert dest.read_bytes() == CLAUDE_EXAMPLE.read_bytes()
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert logged() == [
        f"runuser -u boxuser -- env HOME={home} "
        f"PATH={home}/.local/bin:/usr/local/bin:/usr/bin:/bin bash -c"
    ]
    # A second run keeps the user's file as it is.
    dest.write_text("{}\n", encoding="utf-8")
    again = go(put)
    assert again.returncode == 0, again.stderr
    assert "kept existing" in again.stdout
    assert dest.read_text(encoding="utf-8") == "{}\n"


def test_a_real_box_refuses_a_planted_link_before_runuser(tmp_path) -> None:
    home, go, logged = real_box(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / ".claude").symlink_to(outside)
    dest = home / ".claude" / "settings.json"
    result = go(f'put_home_file claude "{dest}" 0600 "" example < "{CLAUDE_EXAMPLE}"\n')
    assert result.returncode == 1
    assert f"{home}/.claude is a symlink; refusing" in result.stderr
    assert "reached-the-end" not in result.stdout
    assert list(outside.iterdir()) == []
    assert logged() == []


def test_a_real_box_stops_on_cao_settings_for_another_session(tmp_path) -> None:
    home, go, logged = real_box(tmp_path)
    dest = home / ".aws" / "cli-agent-orchestrator" / "settings.json"
    dest.parent.mkdir(parents=True)
    text = f'{{"terminal": {{"herdr_session": "cao-{OTHER_INSTANCE_ID}"}}}}\n'
    dest.write_text(text, encoding="utf-8")
    result = go(f'put_home_file cao "{dest}" 0644 cao-{FAKE_INSTANCE_ID} x < /dev/null\n')
    assert result.returncode == 1
    assert "make them match" in result.stderr
    assert "reached-the-end" not in result.stdout
    assert dest.read_text(encoding="utf-8") == text
    assert len(logged()) == 1
