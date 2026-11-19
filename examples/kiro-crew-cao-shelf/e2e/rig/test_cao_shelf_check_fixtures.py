"""check_fixtures.py passes on the committed fixtures and fails on a tampered copy."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLE = HERE.parents[1]


def run(root: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "CHECK_FIXTURES_ROOT": str(root)}
    return subprocess.run(
        [sys.executable, str(HERE / "check_fixtures.py")],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_committed_fixtures_match_the_generator() -> None:
    res = run(EXAMPLE)
    assert res.returncode == 0, res.stdout + res.stderr


def test_a_tampered_fixture_is_reported(tmp_path: Path) -> None:
    root = tmp_path / "example"
    shutil.copytree(
        EXAMPLE / "fixtures", root / "fixtures", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copytree(EXAMPLE / "schemas", root / "schemas")
    victim = root / "fixtures" / "cao" / "busy" / "sessions.json"
    victim.write_text(
        victim.read_text(encoding="utf-8").replace("payments-fix", "payments-fiX", 1),
        encoding="utf-8",
    )
    res = run(root)
    assert res.returncode == 1
    assert "differs: cao/busy/sessions.json" in res.stdout
