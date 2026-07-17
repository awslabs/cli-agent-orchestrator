"""End-to-end test for the headless CI runner example.

Invokes ``examples/headless-ci/run.sh`` against the ``ci_developer`` profile
and asserts the script exits 0 (agent reached IDLE/COMPLETED) within the
configured timeout.

Requires:
- tmux
- A working CLI provider on PATH and authenticated (defaults to kiro_cli)
- ``ci_developer`` agent profile installed
  (``cao install examples/headless-ci/ci_developer.md``)

The CAO server itself is started automatically by the session-scoped
``require_cao_server`` fixture in ``test/e2e/conftest.py`` — no manual
``cao-server`` is needed.

Run:
    uv run pytest -m e2e test/e2e/test_headless_ci.py -v
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_SCRIPT = REPO_ROOT / "examples" / "headless-ci" / "run.sh"


@pytest.mark.e2e
def test_headless_ci_run_script_exits_clean() -> None:
    """The runner script should exit 0 when the agent reaches a terminal state."""
    assert RUN_SCRIPT.exists(), f"missing {RUN_SCRIPT}"
    assert os.access(RUN_SCRIPT, os.X_OK), f"{RUN_SCRIPT} is not executable"
    assert shutil.which("cao") is not None, "cao CLI not on PATH"

    env = os.environ.copy()
    env.setdefault("CAO_CI_TIMEOUT", "180")
    env.setdefault("CAO_CI_POLL_INTERVAL", "3")

    result = subprocess.run(
        [str(RUN_SCRIPT), "Print the literal text HEADLESS_CI_OK and end your turn."],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )

    assert (
        result.returncode == 0
    ), f"run.sh exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
