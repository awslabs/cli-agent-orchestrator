"""bin/lib-cao-box.sh: checksum, ownership and box.env parsing, called directly."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest
from remote_ec2_support import (
    BASH,
    FAKE_INSTANCE_ID,
    OTHER_INSTANCE_ID,
    SAMPLE_DIR,
    system_path,
)

LIB = SAMPLE_DIR / "bin" / "lib-cao-box.sh"
SESSION = f"cao-{FAKE_INSTANCE_ID}"


def call(snippet: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    script = f'set -euo pipefail\n. "{LIB}"\n{snippet}\necho reached-the-end\n'
    return subprocess.run(
        [BASH, "-c", script],
        env={"PATH": system_path(), "HOME": str(tmp_path), "LANG": "C"},
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------- checksums


def test_require_sha_passes_on_the_right_hash(tmp_path) -> None:
    f = tmp_path / "download"
    f.write_bytes(b"herdr")
    good = hashlib.sha256(b"herdr").hexdigest()
    result = call(f'cb_require_sha "{f}" "{good}" herdr', tmp_path)
    assert result.returncode == 0, result.stderr
    assert "reached-the-end" in result.stdout


@pytest.mark.parametrize(
    "want",
    [hashlib.sha256(b"something else").hexdigest(), "", "ab" * 31, ("AB" * 32), " " + "ab" * 32],
    ids=["mismatch", "empty", "short", "uppercase", "space"],
)
def test_require_sha_stops_the_caller_on_a_real_mismatch(tmp_path, want) -> None:
    f = tmp_path / "download"
    f.write_bytes(b"herdr")
    result = call(f'cb_require_sha "{f}" "{want}" herdr', tmp_path)
    assert result.returncode == 1
    assert "reached-the-end" not in result.stdout
    assert "failed its checksum" in result.stderr


# ---------------------------------------------------------------- box.env


@pytest.mark.parametrize(
    "text",
    [
        f"CAO_BOX_SESSION={SESSION}\nCAO_HERDR_SESSION={SESSION}\n",
        f"CAO_HERDR_SESSION={SESSION}\nCAO_BOX_SESSION={SESSION}\n",
        f"CAO_BOX_SESSION={SESSION}\nCAO_HERDR_SESSION={SESSION}",  # no final newline
    ],
    ids=["box-first", "herdr-first", "no-final-newline"],
)
def test_read_session_accepts_exactly_the_two_equal_lines(tmp_path, text) -> None:
    f = tmp_path / "box.env"
    f.write_text(text, encoding="utf-8")
    result = call(f'cb_read_session "{f}"', tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == SESSION


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (
            f"CAO_BOX_SESSION={SESSION}\nCAO_HERDR_SESSION={SESSION}\nLD_PRELOAD=/dev/shm/x.so\n",
            "unexpected line 3",
        ),
        (
            f"LD_PRELOAD=/dev/shm/x.so\nCAO_BOX_SESSION={SESSION}\nCAO_HERDR_SESSION={SESSION}\n",
            "unexpected line 1",
        ),
        (
            f"CAO_BOX_SESSION={SESSION}\nCAO_HERDR_SESSION={SESSION}\nCAO_HERDR_SESSION=cao-other\n",
            "appears twice",
        ),
        (f"CAO_BOX_SESSION={SESSION}\nCAO_HERDR_SESSION=cao-{OTHER_INSTANCE_ID}\n", "differ"),
        (f"CAO_BOX_SESSION={SESSION}\n", "needs exactly"),
        (f"CAO_BOX_SESSION={SESSION}\nCAO_HERDR_SESSION=cao-other\n", "needs exactly"),
        (f"CAO_BOX_SESSION={SESSION}\n\nCAO_HERDR_SESSION={SESSION}\n", "unexpected line 2"),
        (f"CAO_BOX_SESSION={SESSION} \nCAO_HERDR_SESSION={SESSION}\n", "needs exactly"),
    ],
    ids=[
        "ld-preload-after",
        "ld-preload-before",
        "duplicate",
        "mismatch",
        "one-line",
        "bad-herdr-value",
        "blank-line",
        "trailing-space",
    ],
)
def test_read_session_refuses_anything_else(tmp_path, text, reason) -> None:
    f = tmp_path / "box.env"
    f.write_text(text, encoding="utf-8")
    result = call(f'cb_read_session "{f}"', tmp_path)
    assert result.returncode == 1
    assert reason in result.stderr
    assert SESSION not in result.stdout


def test_render_session_is_what_read_session_accepts(tmp_path) -> None:
    f = tmp_path / "box.env"
    result = call(f'cb_render_session "{SESSION}" > "{f}"; cb_read_session "{f}"', tmp_path)
    assert result.returncode == 0, result.stderr
    assert f.read_text(encoding="utf-8") == (
        f"CAO_BOX_SESSION={SESSION}\nCAO_HERDR_SESSION={SESSION}\n"
    )


# ------------------------------------------------------------- ownership


def test_safe_path_accepts_own_0755_and_0644(tmp_path) -> None:
    d = tmp_path / "state"
    d.mkdir(mode=0o755)
    d.chmod(0o755)
    f = d / "box.env"
    f.write_text("x", encoding="utf-8")
    f.chmod(0o644)
    uid = os.getuid()
    result = call(f'cb_safe_path "{d}" {uid}; cb_safe_path "{f}" {uid}', tmp_path)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(os.getuid() == 0, reason="needs a non-root owner to compare against")
def test_safe_path_refuses_a_folder_owned_by_someone_else(tmp_path) -> None:
    # The folder is owned by the test user; demanding root (uid 0) must fail,
    # exactly as a box-user-owned state folder fails when bootstrap runs as root.
    d = tmp_path / "state"
    d.mkdir()
    d.chmod(0o755)
    result = call(f'cb_safe_path "{d}" 0', tmp_path)
    assert result.returncode == 1
    assert "not 0" in result.stderr


@pytest.mark.parametrize("mode", [0o775, 0o757, 0o777, 0o666, 0o646])
def test_safe_path_refuses_group_or_other_write(tmp_path, mode) -> None:
    p = tmp_path / "thing"
    p.mkdir()
    p.chmod(mode)
    result = call(f'cb_safe_path "{p}" {os.getuid()}', tmp_path)
    assert result.returncode == 1
    assert "writable by group or others" in result.stderr


def test_safe_dir_checks_every_parent_up_to_stop(tmp_path) -> None:
    top = tmp_path / "top"
    (top / "mid" / "state").mkdir(parents=True)
    for p in [top, top / "mid", top / "mid" / "state"]:
        p.chmod(0o755)
    uid = os.getuid()
    ok = call(f'cb_safe_dir "{top}/mid/state" "{top}" {uid}', tmp_path)
    assert ok.returncode == 0, ok.stderr
    (top / "mid").chmod(0o777)
    bad = call(f'cb_safe_dir "{top}/mid/state" "{top}" {uid}', tmp_path)
    assert bad.returncode == 1
    assert "mid is writable" in bad.stderr


def test_safe_path_refuses_a_symlink_even_to_a_safe_target(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    real.chmod(0o755)
    link = tmp_path / "link"
    link.symlink_to(real)
    result = call(f'cb_safe_path "{link}" {os.getuid()}', tmp_path)
    assert result.returncode == 1
    assert "is a symlink" in result.stderr


def test_safe_dir_refuses_a_symlink_in_the_chain(tmp_path) -> None:
    top = tmp_path / "top"
    real = tmp_path / "real-state"
    top.mkdir()
    real.mkdir()
    for p in (top, real):
        p.chmod(0o755)
    (top / "state").symlink_to(real)
    result = call(f'cb_safe_dir "{top}/state" "{top}" {os.getuid()}', tmp_path)
    assert result.returncode == 1
    assert "is a symlink" in result.stderr
