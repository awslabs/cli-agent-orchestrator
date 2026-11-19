"""Run scripts/herdr_tui_to_app.py --self-test in CI so its offline checks can't go stale."""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "herdr_tui_to_app.py"


def test_migration_script_self_test_passes():
    # Its own interpreter, as a user runs it; the checks are offline (no herdr, no hc).
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--self-test"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, f"stdout:\n{done.stdout}\nstderr:\n{done.stderr}"
    assert "self-test: all checks passed" in done.stdout
