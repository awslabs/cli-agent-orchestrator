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

import contextlib
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from test.fixtures.cao_server import CaoServer, skip_if_provider_unusable

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_SCRIPT = REPO_ROOT / "examples" / "headless-ci" / "run.sh"
PROFILE = REPO_ROOT / "examples" / "headless-ci" / "ci_developer.md"
PROVIDER = "kiro_cli"


def _skip_unless_provider_boots(cao_server: CaoServer) -> None:
    """Skip when the provider CLI cannot start a session on this host.

    ``run.sh`` reports a provider that never booted as a bare non-zero exit and
    a server stack trace, which reads exactly like a regression in the runner.
    Asking the server for one throwaway session first separates the two: a 5xx
    that names the provider is this machine's login state, not the example's
    behaviour, and every provider wrapper here reads that state from ``$HOME``
    — which the fixture deliberately redirects. Same classifier the
    ``cao_terminal`` fixture uses, so the two agree on what counts as a skip.
    """
    session_name = f"caoci-preflight-{uuid.uuid4().hex[:8]}"
    resp = requests.post(
        f"{cao_server.url}/sessions",
        params={
            "provider": PROVIDER,
            "agent_profile": PROFILE.stem,
            "session_name": session_name,
        },
        timeout=300,
    )
    if resp.status_code not in (200, 201):
        skip_if_provider_unusable(resp.status_code, resp.text, PROVIDER)
        raise RuntimeError(f"pre-flight POST /sessions failed: {resp.status_code} {resp.text}")

    # The probe proves the provider boots; run.sh owns the session under test,
    # so this one is torn down rather than handed over.
    actual_session = resp.json().get("session_name", session_name)
    with contextlib.suppress(Exception):
        requests.delete(f"{cao_server.url}/sessions/{actual_session}", timeout=60)


@pytest.mark.e2e
def test_headless_ci_run_script_exits_clean(cao_server: CaoServer) -> None:
    """The runner script should exit 0 when the agent reaches a terminal state.

    The child is pointed at the managed server explicitly. The module docstring
    has always said the fixture supplies the server, but ``require_cao_server``
    patches ``API_BASE_URL`` *in this process* — a subprocess re-imports the
    constant and, with nothing in its env, dialled the default ``:9889``. That
    passed on a laptop with a personal ``cao-server`` running and failed, as a
    ``400``, on any machine where something else answered that port. ``HOME``
    comes from the fixture for the same reason: the profile the runner launches
    has to be installed in the state directory the server is actually reading.

    ``PATH`` leads with this interpreter's ``bin`` so ``run.sh`` runs the ``cao``
    under test. Bare ``cao`` resolved to whatever the developer last installed
    globally — here a published 2.2.0 from ``uv tool install``, which predates
    ``CAO_API_BASE_URL`` and therefore ignored the redirect above and kept
    dialling ``:9889``. An e2e test that shells out to a CLI has to name which
    CLI, or it silently reports on a different build than the one in the tree.

    A pre-flight session creation runs after the install and before the runner:
    see ``_skip_unless_provider_boots``.
    """
    assert RUN_SCRIPT.exists(), f"missing {RUN_SCRIPT}"
    assert os.access(RUN_SCRIPT, os.X_OK), f"{RUN_SCRIPT} is not executable"

    env = os.environ.copy()
    bin_dir = Path(sys.executable).parent
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["HOME"] = str(cao_server.home_dir)
    env["CAO_API_BASE_URL"] = cao_server.url
    env.setdefault("CAO_CI_TIMEOUT", "180")
    env.setdefault("CAO_CI_POLL_INTERVAL", "3")
    # ``POST /sessions`` initializes the provider inline, and a cold isolated
    # HOME is the slow case: no warmed caches, a first-run consent dialog to
    # answer. The CLI's 30s default expires while the server is still working,
    # so run.sh reported a read timeout for a session that went on to start.
    env.setdefault("CAO_MCP_REQUEST_TIMEOUT", "180")

    cao_bin = shutil.which("cao", path=env["PATH"])
    assert cao_bin is not None, "cao CLI not on PATH"
    assert Path(cao_bin).parent == bin_dir, (
        f"resolved cao is {cao_bin}, not the one in {bin_dir}; the test would "
        "report on a different build than the working tree"
    )

    install = subprocess.run(
        [cao_bin, "install", str(PROFILE)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert install.returncode == 0, (
        f"cao install {PROFILE.name} exited {install.returncode}\n"
        f"stdout:\n{install.stdout}\nstderr:\n{install.stderr}"
    )

    _skip_unless_provider_boots(cao_server)

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
