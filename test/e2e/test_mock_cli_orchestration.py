"""Keyless orchestration through a real cao-server, tmux, and mock_cli."""

import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest
import requests

pytestmark = pytest.mark.e2e
REPO_ROOT = Path(__file__).resolve().parents[2]


def _post(url, **kwargs):
    response = requests.post(url, timeout=45, **kwargs)
    assert response.ok, response.text
    return response.json()


def _wait_for(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.2)
    pytest.fail("mock_cli orchestration did not settle before the deadline")


def test_handoff_worker_round_trip(cao_server, tmp_path):
    """The actual run-step endpoint spawns, drives, and extracts a worker reply."""
    message = f"handoff-{uuid.uuid4().hex}"
    response = _post(
        f"{cao_server.url}/terminals/run-step",
        json={
            "provider": "mock_cli",
            "agent": "developer",
            "prompt": message,
            "working_directory": str(tmp_path),
            "timeout": 30,
        },
    )
    assert response["status"].lower() == "completed"
    assert message in response["last_message"]


def test_assign_and_send_message_in_shared_session(cao_server):
    """A second worker receives and processes a supervisor's inbox message."""
    session = f"mock-e2e-{uuid.uuid4().hex[:12]}"
    base = cao_server.url
    try:
        created = _post(
            f"{base}/sessions",
            params={"provider": "mock_cli", "agent_profile": "developer", "session_name": session},
        )
        supervisor = created["id"]
        session = created["session_name"]
        assign_message = f"assign-{uuid.uuid4().hex}"
        worker = _post(
            f"{base}/sessions/{session}/terminals",
            params={
                "provider": "mock_cli",
                "agent_profile": "developer",
                "caller_id": supervisor,
                "defer_init": "true",
            },
            json={
                "initial_message": assign_message,
                "initial_message_orchestration_type": "assign",
            },
        )["id"]

        def assigned_output():
            response = requests.get(
                f"{base}/terminals/{worker}/output", params={"mode": "last"}, timeout=5
            )
            response.raise_for_status()
            return assign_message in response.json()["output"]

        assert _wait_for(assigned_output)
        message = f"inbox-{uuid.uuid4().hex}"
        sent = _post(
            f"{base}/terminals/{worker}/inbox/messages",
            params={"sender_id": supervisor, "message": message},
        )
        assert sent["sender_id"] == supervisor
        assert sent["receiver_id"] == worker

        def delivered():
            response = requests.get(f"{base}/terminals/{worker}/inbox/messages", timeout=5)
            response.raise_for_status()
            return any(
                item["id"] == sent["message_id"] and item["status"] == "delivered"
                for item in response.json()
            )

        assert _wait_for(delivered)

        def inbox_processed():
            response = requests.get(
                f"{base}/terminals/{worker}/output", params={"mode": "last"}, timeout=5
            )
            response.raise_for_status()
            return message in response.json()["output"]

        assert _wait_for(inbox_processed)
    finally:
        requests.delete(f"{base}/sessions/{session}", timeout=45)


def test_headless_runner_with_mock_cli(cao_server):
    """The shipped headless shell runner reaches the mock worker's completion."""
    profile = cao_server.home_dir / ".aws" / "cli-agent-orchestrator" / "agent-store"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "ci_developer.md").write_text(
        "---\nname: ci_developer\nprovider: mock_cli\nrole: developer\n---\n"
        "Respond to the task.\n"
    )
    environment = os.environ.copy()
    environment.update(
        HOME=str(cao_server.home_dir),
        CAO_HOME_DIR=str(profile.parent),
        CAO_API_PORT=str(cao_server.port),
        CAO_CI_TIMEOUT="30",
        CAO_CI_POLL_INTERVAL="1",
    )
    result = subprocess.run(
        [str(REPO_ROOT / "examples/headless-ci/run.sh"), "mock headless runner"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "COMPLETED" in result.stdout + result.stderr
