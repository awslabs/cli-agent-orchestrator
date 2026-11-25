"""Probe nodes for the decision-home guard tests in test_decision_home_guard_xdist.py.

Each probe changes decision state in the startup ``CAO_HOME_DIR`` past the per-test
redirect in ``_hermetic_cao_env``, which is what the session guard in
``test/conftest.py`` exists to catch (#955). They only act inside a nested session
that sets ``CAO_TEST_DECISION_HOME_PROBE=1``; in an ordinary run they skip.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_ENABLED = os.environ.get("CAO_TEST_DECISION_HOME_PROBE") == "1"


def _startup_home() -> Path:
    from cli_agent_orchestrator.constants import CAO_HOME_DIR

    home = Path(CAO_HOME_DIR).resolve()
    tmp = Path(tempfile.gettempdir()).resolve()
    # Never touch a home outside the temp dir, whatever the session decided.
    if not home.is_relative_to(tmp):
        pytest.skip(f"startup CAO home is not under the temp dir: {home}")
    return home


@pytest.mark.skipif(not _ENABLED, reason="nested decision-home guard probe only")
def test_probe_a_writes_the_halt_marker_in_process():
    home = _startup_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "decisions.halt").write_text("probe\n")


@pytest.mark.skipif(not _ENABLED, reason="nested decision-home guard probe only")
def test_probe_b_writes_the_egress_lock_from_a_child_process():
    _startup_home()
    # The child resolves CAO_HOME_DIR from the inherited environment, as a
    # spawned agent or helper would.
    code = (
        "from cli_agent_orchestrator.constants import CAO_HOME_DIR\n"
        "CAO_HOME_DIR.mkdir(parents=True, exist_ok=True)\n"
        "(CAO_HOME_DIR / 'decisions-egress.lock').write_text('probe')\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True, timeout=60)
