"""Shared fixtures for end-to-end tests.

E2E tests require:
- A running CAO server (cao-server / uvicorn on localhost:9889)
- The provider CLI tool installed and authenticated (codex, claude, kiro-cli)
- tmux available on the system

Run with: uv run pytest -m e2e test/e2e/ -v
"""

import shutil
import time

import pytest
import requests

from cli_agent_orchestrator.constants import API_BASE_URL


@pytest.fixture(scope="session", autouse=True)
def require_cao_server():
    """Skip all E2E tests if CAO server is not reachable."""
    try:
        resp = requests.get(f"{API_BASE_URL}/health", timeout=5)
        if resp.status_code != 200:
            pytest.skip("CAO server not healthy")
    except requests.ConnectionError:
        pytest.skip("CAO server not running — start with: uv run cao-server")


@pytest.fixture(scope="session", autouse=True)
def require_tmux():
    """Skip all E2E tests if tmux is not installed."""
    if not shutil.which("tmux"):
        pytest.skip("tmux not installed")


def _cli_available(command: str) -> bool:
    """Check if a CLI tool is on PATH."""
    return shutil.which(command) is not None


@pytest.fixture()
def require_codex():
    """Skip test if codex CLI is not available."""
    if not _cli_available("codex"):
        pytest.skip("codex CLI not installed")


@pytest.fixture()
def require_claude():
    """Skip test if claude CLI is not available."""
    if not _cli_available("claude"):
        pytest.skip("claude CLI not installed")


@pytest.fixture()
def require_kiro():
    """Skip test if kiro-cli is not available."""
    if not _cli_available("kiro-cli"):
        pytest.skip("kiro-cli CLI not installed")


@pytest.fixture()
def require_kimi():
    """Skip test if kimi CLI is not available."""
    if not _cli_available("kimi"):
        pytest.skip("kimi CLI not installed")


def create_terminal(provider: str, agent_profile: str, session_name: str):
    """Create a CAO session + terminal via the API.

    Returns (terminal_id, actual_session_name).
    """
    resp = requests.post(
        f"{API_BASE_URL}/sessions",
        params={
            "provider": provider,
            "agent_profile": agent_profile,
            "session_name": session_name,
        },
    )
    assert resp.status_code in (
        200,
        201,
    ), f"Session creation failed: {resp.status_code} {resp.text}"
    data = resp.json()
    return data["id"], data["session_name"]


def get_terminal_status(terminal_id: str) -> str:
    """Get live terminal status via provider.get_status()."""
    resp = requests.get(f"{API_BASE_URL}/terminals/{terminal_id}")
    if resp.status_code != 200:
        return "unknown"
    return resp.json().get("status", "unknown")


def wait_for_status(
    terminal_id: str, target: str, timeout: float = 90.0, poll: float = 3.0
) -> bool:
    """Poll terminal status until target is reached or timeout."""
    start = time.time()
    while time.time() - start < timeout:
        status = get_terminal_status(terminal_id)
        if status == target:
            return True
        if status == "error":
            return False
        time.sleep(poll)
    return False


def send_handoff_message(terminal_id: str, message: str, provider: str) -> None:
    """Send a message to the terminal, with [CAO Handoff] prefix for codex."""
    if provider == "codex":
        full_message = (
            "[CAO Handoff] Supervisor terminal ID: test-supervisor-e2e. "
            "This is a blocking handoff \u2014 the orchestrator will automatically "
            "capture your response when you finish. Complete the task and output "
            "your results directly. Do NOT use send_message to notify the supervisor "
            "unless explicitly needed \u2014 just do the work and present your deliverables.\n\n"
            f"{message}"
        )
    else:
        full_message = message

    resp = requests.post(
        f"{API_BASE_URL}/terminals/{terminal_id}/input",
        params={"message": full_message},
    )
    assert resp.status_code == 200, f"Send message failed: {resp.status_code} {resp.text}"


def extract_output(terminal_id: str) -> str:
    """Extract the last assistant message from the terminal."""
    resp = requests.get(
        f"{API_BASE_URL}/terminals/{terminal_id}/output",
        params={"mode": "last"},
    )
    assert resp.status_code == 200, f"Output extraction failed: {resp.status_code} {resp.text}"
    return resp.json().get("output", "")


def cleanup_terminal(terminal_id: str, session_name: str) -> None:
    """Send /exit and delete the session."""
    try:
        requests.post(f"{API_BASE_URL}/terminals/{terminal_id}/exit")
    except Exception:
        pass
    time.sleep(2)
    try:
        requests.delete(f"{API_BASE_URL}/sessions/{session_name}")
    except Exception:
        pass
