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
- bootstrap.sh's herdr check on a real box: the box user hashes herdr, under
  a deadline, so root never opens it (a FIFO or a slow file can't hang root).
- bootstrap.sh's checkout guard: as root, the sample must sit in folders that
  only root can write. EUID can't be faked, so it runs with the test's uid.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import time
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


# --------------------------------------- the herdr check on a real box (P3-1)

HERDR_CHECK = extract("bootstrap.sh", "herdr-check")
TIMEOUT_SHIM = f"""#!{sys.executable}
# timeout SECONDS CMD...: coreutils' timeout, for hosts that lack it (macOS).
import subprocess, sys
proc = subprocess.Popen(sys.argv[2:])
try:
    sys.exit(proc.wait(timeout=float(sys.argv[1])))
except subprocess.TimeoutExpired:
    proc.kill()
    proc.wait()
    sys.exit(124)
"""


def herdr_box(tmp_path: Path, seconds: int = 20, slow_hash: bool = False):
    """Run herdr_is_pinned on FILE with a runuser stub that logs, then runs."""
    stubs = tmp_path / "stubs"
    user_bin = tmp_path / "user-bin"  # the box user's SYS_PATH: the shim first
    calls = tmp_path / "calls.log"
    write_stub(
        stubs,
        "runuser",
        f'echo "runuser $1 $2 $3" >> "{calls}"\n'
        '[ "$1" = "-u" ] && [ "$3" = "--" ] || exit 64\n'
        'shift 3\nexec "$@"\n',
    )
    shim = user_bin / "timeout"
    user_bin.mkdir()
    shim.write_text(TIMEOUT_SHIM, encoding="utf-8")
    shim.chmod(0o755)
    if slow_hash:
        write_stub(user_bin, "sha256sum", "exec sleep 60\n")
    # It prints the pinned version, so asking it would make a mutant keep it.
    body = f'#!/bin/sh\necho ran > "{tmp_path / "herdr-ran"}"\necho "herdr 0.9.3"\n'.encode()
    pinned = hashlib.sha256(body).hexdigest()

    def check(file: Path, dry: int = 0) -> tuple[subprocess.CompletedProcess[str], float]:
        snippet = (
            BOOTSTRAP_PRELUDE
            + f"BOX_USER=boxuser\nHERDR_SHA256={pinned}\nDRY_RUN={dry}\n"
            + HERDR_CHECK
            + f'SYS_PATH="{user_bin}:{system_path()}"\nHASH_SECONDS={seconds}\n'
            + f'if herdr_keep "{file}"; then echo PINNED; else echo NOT-PINNED; fi\n'
        )
        start = time.monotonic()
        result = subprocess.run(
            [BASH, "-c", f"set -euo pipefail\n{snippet}"],
            env={"PATH": f"{stubs}:{system_path()}", "HOME": str(tmp_path), "LANG": "C"},
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        return result, time.monotonic() - start

    def logged() -> list[str]:
        return calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []

    return body, check, logged


def herdr_file(tmp_path: Path, body: bytes, mode: int = 0o755) -> Path:
    f = tmp_path / "home" / ".local" / "bin" / "herdr"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(body)
    f.chmod(mode)
    return f


def test_the_pinned_herdr_is_kept_and_hashed_by_the_box_user(tmp_path) -> None:
    body, check, logged = herdr_box(tmp_path)
    result, _ = check(herdr_file(tmp_path, body))
    assert result.stdout.strip() == "PINNED", result.stderr
    assert logged() == ["runuser -u boxuser --"]
    assert not (tmp_path / "herdr-ran").exists()


@pytest.mark.parametrize("kind", ["planted", "symlink-to-pinned", "not-executable", "folder"])
def test_anything_but_the_pinned_regular_file_is_downloaded_again(tmp_path, kind) -> None:
    body, check, logged = herdr_box(tmp_path)
    if kind == "planted":
        target = herdr_file(tmp_path, body + b"# planted\n")
    elif kind == "symlink-to-pinned":
        real = tmp_path / "pinned-herdr"
        real.write_bytes(body)
        real.chmod(0o755)
        target = tmp_path / "home" / ".local" / "bin" / "herdr"
        target.parent.mkdir(parents=True)
        target.symlink_to(real)
    elif kind == "not-executable":
        target = herdr_file(tmp_path, body, mode=0o644)
    else:
        target = tmp_path / "home" / ".local" / "bin" / "herdr"
        target.mkdir(parents=True)
    result, _ = check(target)
    assert result.stdout.strip() == "NOT-PINNED", result.stderr
    assert not (tmp_path / "herdr-ran").exists()


def test_a_dry_run_never_looks_at_herdr(tmp_path) -> None:
    body, check, logged = herdr_box(tmp_path)
    result, _ = check(herdr_file(tmp_path, body), dry=1)
    assert result.stdout.splitlines()[-1] == "NOT-PINNED", result.stderr
    assert "[dry-run] would check, as boxuser, that" in result.stdout
    assert logged() == []
    assert not (tmp_path / "herdr-ran").exists()


def test_bootstrap_never_runs_the_installed_herdr() -> None:
    # "$herdr_bin" may only be checked by herdr_keep or be install's target.
    text = (SAMPLE_DIR / "bootstrap.sh").read_text(encoding="utf-8")
    uses = [line.strip() for line in text.splitlines() if '"$herdr_bin"' in line]
    assert uses == [
        'if herdr_keep "$herdr_bin"; then',
        'as_user install -m 0755 "$TMP_DL/herdr" "$herdr_bin"',
    ]


def test_a_fifo_never_hangs_the_check(tmp_path) -> None:
    _, check, logged = herdr_box(tmp_path, seconds=20)
    fifo = tmp_path / "home" / ".local" / "bin" / "herdr"
    fifo.parent.mkdir(parents=True)
    os.mkfifo(fifo)
    result, took = check(fifo)
    assert result.stdout.strip() == "NOT-PINNED", result.stderr
    assert took < 10, f"the check waited {took:.1f}s on a FIFO"


def test_a_hash_that_never_ends_is_given_up(tmp_path) -> None:
    # What a FIFO or a device swapped in after the user's checks would do.
    body, check, _ = herdr_box(tmp_path, seconds=1, slow_hash=True)
    result, took = check(herdr_file(tmp_path, body))
    assert result.stdout.strip() == "NOT-PINNED", result.stderr
    assert took < 15, f"the check waited {took:.1f}s"


# -------------------------------------------- the checkout guard (P2-1)

CHECKOUT_GUARD = extract("bootstrap.sh", "checkout-guard")


def checkout(tmp_path: Path) -> Path:
    here = tmp_path / "kit" / "remote-ec2"
    (here / "bin").mkdir(parents=True)
    (here / "bin" / "lib-cao-box.sh").write_text("# lib\n", encoding="utf-8")
    for folder in (tmp_path / "kit", here, here / "bin"):
        folder.chmod(0o755)
    return here


def guard(tmp_path: Path, here: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return run(
        CHECKOUT_GUARD + f'safe_checkout "{here}" "{os.getuid()}" "{tmp_path}"', tmp_path, **env
    )


def test_a_checkout_only_its_owner_can_write_passes(tmp_path) -> None:
    result = guard(tmp_path, checkout(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "reached-the-end" in result.stdout


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("group-writable-checkout", "is writable by group or others"),
        ("group-writable-parent", "is writable by group or others"),
        ("other-writable-file", "is not owned by uid"),
        ("linked-checkout", "is not a real folder"),
        ("another-owner", "is owned by uid 2001"),
    ],
)
def test_a_checkout_someone_else_can_change_is_refused(tmp_path, change, message) -> None:
    here = checkout(tmp_path)
    env: dict[str, str] = {}
    if change == "group-writable-checkout":
        here.chmod(0o775)
    elif change == "group-writable-parent":
        here.parent.chmod(0o775)
    elif change == "other-writable-file":
        (here / "bin" / "lib-cao-box.sh").chmod(0o646)
    elif change == "linked-checkout":
        link = tmp_path / "linked"
        link.symlink_to(here)
        here = link
    else:
        stubs = tmp_path / "stubs"
        write_stub(
            stubs,
            "stat",
            f'case "$*" in\n  *"{here.parent}") echo "2001 755" ;;\n  *) exec /usr/bin/stat "$@" ;;\nesac\n',
        )
        env = {"PATH": f"{stubs}:{system_path()}"}
    result = guard(tmp_path, here, **env)
    assert result.returncode != 0
    assert message in result.stderr
    assert "reached-the-end" not in result.stdout


def test_bootstrap_guards_its_checkout_as_root_before_sourcing_anything() -> None:
    text = (SAMPLE_DIR / "bootstrap.sh").read_text(encoding="utf-8")
    here = text.index('HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"\n')
    call = text.index('if [ "$EUID" -eq 0 ] && ! safe_checkout "$HERE" 0 /; then\n')
    source = text.index('. "$HERE/bin/lib-cao-box.sh"')
    assert here < call < source
    assert "\n. " not in text[:call], "something is sourced before the guard"
