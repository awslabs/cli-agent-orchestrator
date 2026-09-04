"""Gated live-provider test for the workflow example (issue #591).

Exercises a REAL ``cao-server`` and a REAL, authenticated ``claude_code`` CLI
end to end — the one thing ``test_workflow_example.py`` deliberately does
NOT do (it fakes the ``/terminals/run-step`` transport so it can run without
credentials). This test is expensive, needs a real provider login, and must
never run in CI or by default — it is gated the same way the existing
provider integration suites are:

    CAO_RUN_LIVE_PROVIDER_TESTS=1 pytest examples/workflow/tests/test_workflow_live.py -v

Same env var and skip pattern as ``test/providers/test_kiro_cli_integration.py``
/ ``test/providers/test_codex_provider_unit.py``, plus the ``live_provider``
marker (registered in pyproject.toml) so it is identifiable independent of
the env-var gate.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from contextlib import closing
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.live_provider,
    pytest.mark.skipif(
        os.environ.get("CAO_RUN_LIVE_PROVIDER_TESTS", "") != "1",
        reason="Live provider tests disabled. Set CAO_RUN_LIVE_PROVIDER_TESTS=1 to enable.",
    ),
]

_WORKFLOW_PATH = Path(__file__).resolve().parents[1] / "workflow.py"
_SERVER_READY_TIMEOUT = 30.0
_RUN_TIMEOUT = 900.0  # a real plan step + up to 2 concurrent checks, headless


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_server(base_url: str, proc: subprocess.Popen) -> None:
    deadline = time.monotonic() + _SERVER_READY_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"cao-server exited early (rc={proc.returncode}): {proc.stdout.read()}")
        try:
            urllib.request.urlopen(f"{base_url}/workflows", timeout=1)  # noqa: S310
            return
        except OSError:
            time.sleep(0.5)
    pytest.fail(f"cao-server did not become ready within {_SERVER_READY_TIMEOUT}s")


@pytest.fixture(scope="module")
def claude_code_available():
    if not shutil.which("claude"):
        pytest.skip("claude (the claude_code provider CLI) is not installed")
    return True


@pytest.fixture(scope="module")
def live_cao_server(tmp_path_factory, claude_code_available):
    """A real ``cao-server`` subprocess, isolated in a scratch CAO_HOME_DIR."""
    home = tmp_path_factory.mktemp("cao-home")
    port = _free_port()
    env = {**os.environ, "CAO_HOME_DIR": str(home), "CAO_API_HOST": "127.0.0.1"}
    env["CAO_API_PORT"] = str(port)
    proc = subprocess.Popen(
        ["cao-server"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_server(base_url, proc)
        yield home, env
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def test_workflow_runs_end_to_end_with_a_real_provider(live_cao_server):
    """validate -> run --wait against the real server + a real claude_code step."""
    home, cli_env = live_cao_server
    workflows_dir = home / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    installed_path = workflows_dir / "workflow.py"
    installed_path.write_text(_WORKFLOW_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    validate = subprocess.run(
        ["cao", "workflow", "validate", str(installed_path)],
        env=cli_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert validate.returncode == 0, validate.stdout + validate.stderr

    run = subprocess.run(
        [
            "cao",
            "workflow",
            "run",
            "workflow",
            "--run-id",
            "live-workflow-example",
            "--input",
            "target=myapp",
            "--wait",
            "--json",
        ],
        env=cli_env,
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    result = json.loads(run.stdout.strip().splitlines()[-1])
    assert result["state"] == "completed"
    assert result["output"]["target"] == "myapp"
    assert result["output"]["failed_checks"] == []
