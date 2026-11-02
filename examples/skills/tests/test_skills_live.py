"""Gated live-provider test for the skills-lifecycle example (issue #593).

Runs ``run.sh --live`` end to end: the same deterministic lifecycle plus one
REAL claude_code session driven through cao-server, asserting the skill's
output contract line appears in the agent's captured output. It is expensive,
needs a real provider login, and must never run in CI or by default — gated
the same way as ``examples/workflow/tests/test_workflow_live.py``:

    CAO_RUN_LIVE_PROVIDER_TESTS=1 pytest examples/skills/tests/test_skills_live.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.live_provider,
    pytest.mark.skipif(
        os.environ.get("CAO_RUN_LIVE_PROVIDER_TESTS", "") != "1",
        reason="Live provider tests disabled. Set CAO_RUN_LIVE_PROVIDER_TESTS=1 to enable.",
    ),
]

EXAMPLE_DIR = Path(__file__).resolve().parent.parent


def test_run_sh_live_mode_completes_with_contract_line():
    venv_bin = str(Path(sys.executable).parent)
    proc = subprocess.run(
        ["bash", str(EXAMPLE_DIR / "run.sh"), "--live"],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{venv_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
        timeout=900,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK: full skill lifecycle verified" in proc.stderr
