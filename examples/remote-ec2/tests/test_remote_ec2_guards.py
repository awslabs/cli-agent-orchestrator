"""Guards that can't be reached end to end without real root, tested by
running the exact function text from the script.

- bootstrap.sh's verify(): with DRY_RUN=0, a real checksum mismatch must stop
  the run (F2).
- restore.sh's refuse_overrides_as_root(): as EUID 0, SYSTEMCTL and
  CAO_BOX_ETC are refused unless --dry-run (F4). EUID can't be faked, so the
  function is called with the uid it would see.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest
from remote_ec2_support import BASH, SAMPLE_DIR

LIB = SAMPLE_DIR / "bin" / "lib-cao-box.sh"


def extract(script: str, name: str) -> str:
    text = (SAMPLE_DIR / script).read_text(encoding="utf-8")
    match = re.search(rf"^# >>> {name}\n(.*?)^# <<< {name}\n", text, re.S | re.M)
    assert match, f"{script} has no marked {name} block"
    return match.group(1)


def run(snippet: str, tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, "-c", f"set -euo pipefail\n{snippet}\necho reached-the-end\n"],
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(tmp_path), "LANG": "C", **env},
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
