"""The decision-home session guard (``test/conftest.py``) fails a run under pytest-xdist.

Under ``-n`` each worker has its own fake home and its exit status does not reach
the controller, so the guard's worker copy hands its findings over through
``workeroutput`` and the controller fails the session (#1408). A session-level
failure can only be observed from outside, so these run a nested pytest on the
probes in ``test_decision_home_probe.py``.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBES = "test/decisions/test_decision_home_probe.py"
MESSAGE = "Decision state changed under startup CAO home"


def _run_probe(
    tmp_path: Path, nodeid: str, *pytest_args: str, enabled: bool = True, pythonpath: str = ""
):
    real_home = tmp_path / "probe-real-home"
    real_home.mkdir()
    temp_root = tmp_path / "probe-tmp"
    temp_root.mkdir()
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("CAO_", "_CAO_", "HERDR_", "PYTEST_XDIST_", "TMUX", "COV_CORE"))
    }
    env.update(
        {
            "HOME": str(real_home),
            "TMPDIR": str(temp_root),
            "TMUX_TMPDIR": str(temp_root / "tmux"),
        }
    )
    if enabled:
        env["CAO_TEST_DECISION_HOME_PROBE"] = "1"
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            nodeid,
            "-p",
            "no:cacheprovider",
            "--no-cov",
            "--basetemp",
            str(temp_root / "bt"),
            *pytest_args,
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize(
    ("probe", "changed"),
    [
        ("test_probe_a_writes_the_halt_marker_in_process", "decisions.halt"),
        ("test_probe_b_writes_the_egress_lock_from_a_child_process", "decisions-egress.lock"),
    ],
)
@pytest.mark.parametrize("mode", ["serial", "xdist"])
def test_guard_fails_the_session_when_a_test_changes_the_startup_home(
    tmp_path, probe, changed, mode
):
    args = ("-n", "2") if mode == "xdist" else ("-p", "no:xdist")
    result = _run_probe(tmp_path, f"{PROBES}::{probe}", *args)
    out = result.stdout + result.stderr

    assert result.returncode != 0, out
    assert f"{MESSAGE}: {changed}" in out
    if mode == "xdist":
        assert "(pytest-xdist worker gw" in out


def test_guard_passes_a_run_that_leaves_the_startup_home_alone(tmp_path):
    result = _run_probe(tmp_path, PROBES, "-n", "2", enabled=False)
    out = result.stdout + result.stderr

    assert result.returncode == 0, out
    assert MESSAGE not in out


def test_guard_names_every_worker_that_changed_the_startup_home(tmp_path):
    """``--dist each`` runs probe A on both workers, in two separate fake homes, so
    the controller must report both workers rather than keep only one report."""
    result = _run_probe(
        tmp_path,
        f"{PROBES}::test_probe_a_writes_the_halt_marker_in_process",
        "-n",
        "2",
        "--dist",
        "each",
    )
    out = result.stdout + result.stderr

    assert result.returncode != 0, out
    for worker in ("gw0", "gw1"):
        assert f"{MESSAGE}: decisions.halt (pytest-xdist worker {worker})" in out


DIE_AT_SESSIONFINISH = """
import os


def pytest_sessionfinish(session, exitstatus):
    # On an xdist worker only: die after the last test, before the trylast guard
    # hands its findings to the controller.
    if hasattr(session.config, "workerinput"):
        os._exit(1)
"""


def test_guard_fails_closed_when_a_worker_is_lost_after_its_last_test(tmp_path):
    """Every test passed, but no worker reported its decision state. xdist alone
    exits 0 here, so the guard must fail the run itself."""
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "die_at_sessionfinish.py").write_text(DIE_AT_SESSIONFINISH)
    result = _run_probe(
        tmp_path,
        f"{PROBES}::test_probe_a_writes_the_halt_marker_in_process",
        "-n",
        "2",
        "--max-worker-restart=0",
        "-p",
        "die_at_sessionfinish",
        pythonpath=str(plugins),
    )
    out = result.stdout + result.stderr

    assert result.returncode != 0, out
    assert "Decision-home guard: only 0 of 2 pytest-xdist worker(s) reported" in out
