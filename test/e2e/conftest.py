"""Shared fixtures for end-to-end tests.

E2E tests require:
- The provider CLI tool installed and authenticated (for example codex,
  claude, kiro-cli, gemini, copilot, or grok)
- tmux available on the system

The CAO server is started automatically by the ``cao_server`` fixture from
``test.fixtures.cao_server``. New tests should consume ``cao_server`` /
``cao_terminal`` directly. The ``API_BASE_URL`` patch loop below is a
back-compat shim for the 12 older modules that import the constant at
module top.

Run with: uv run pytest -m e2e test/e2e/ -v
"""

import os
import shutil
import time
from pathlib import Path
from test.fixtures.cao_server import CaoServer, _patch_api_base_url_for_e2e

import pytest
import requests

from cli_agent_orchestrator.constants import API_BASE_URL


@pytest.fixture(scope="session", autouse=True)
def require_cao_server(cao_server: CaoServer):
    """Delegates to the managed ``cao_server`` subprocess fixture.

    Also patches ``API_BASE_URL`` everywhere it has been bound at import
    time by the 12 older e2e modules, so their helper f-strings resolve
    to the dynamically-allocated port. Originals are restored on teardown.
    """
    restore = _patch_api_base_url_for_e2e(cao_server)
    try:
        yield cao_server
    finally:
        restore()


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


@pytest.fixture()
def require_gemini():
    """Skip test if gemini CLI is not available.

    Includes a post-test cooldown to avoid Gemini API rate limiting (429).
    Gemini CLI has known issues with rate limit retry logic (GitHub #6986,
    #9248) — sequential tests can exhaust the per-minute RPM quota, causing
    the CLI to hang during initialization or task processing.
    """
    if not _cli_available("gemini"):
        pytest.skip("gemini CLI not installed")
    yield
    # Cool down after each Gemini CLI test to stay within API rate limits.
    # Gemini's free-tier RPM limit is low; sequential tests exhaust the quota
    # and cause the CLI to hang in a retry loop during initialization.
    time.sleep(15)


@pytest.fixture()
def require_copilot():
    """Skip test if copilot CLI is not available."""
    if not _cli_available("copilot"):
        pytest.skip("copilot CLI not installed")


@pytest.fixture()
def require_opencode():
    """Skip test if opencode binary is not available."""
    if not _cli_available("opencode"):
        pytest.skip("opencode CLI not installed")


@pytest.fixture()
def require_omp():
    """Skip test if OMP CLI is not available."""
    if not _cli_available("omp"):
        pytest.skip("OMP CLI not installed")


@pytest.fixture()
def require_hermes():
    """Skip test if Hermes CLI is not available."""
    if not _cli_available("hermes"):
        pytest.skip("Hermes CLI not installed")


@pytest.fixture()
def require_cursor():
    """Skip test if Cursor CLI (agent or cursor-agent) is not available."""
    if _cli_available("agent") or _cli_available("cursor-agent"):
        return
    pytest.skip("Cursor CLI (agent / cursor-agent) not installed")


@pytest.fixture()
def require_grok(require_cao_server: CaoServer):
    """Skip unless the official Grok Build CLI is installed and authenticated."""
    if not _cli_available("grok"):
        pytest.skip("Grok Build CLI not installed; see docs/grok-cli.md for installation")

    default_home = str(Path.home() / ".grok")
    grok_home = Path(os.environ.get("GROK_HOME", default_home)).expanduser()
    auth_source = grok_home / "auth.json"
    if not os.environ.get("XAI_API_KEY") and not auth_source.is_file():
        pytest.skip(
            "Grok Build CLI is installed but not authenticated; run `grok login` "
            "or set XAI_API_KEY before running Grok e2e tests"
        )

    # The managed e2e server deliberately redirects HOME to isolate CAO state.
    # Mirror production's narrow auth reuse by linking only auth.json into that
    # disposable HOME; never copy credential contents into test artifacts.
    if auth_source.is_file():
        isolated_grok_home = require_cao_server.home_dir / ".grok"
        isolated_grok_home.mkdir(mode=0o700, exist_ok=True)
        auth_link = isolated_grok_home / "auth.json"
        if not auth_link.exists():
            auth_link.symlink_to(auth_source)

    # Orchestration e2e tests use the repository's assign example profiles.
    # Seed copies into the same disposable CAO home so the managed server can
    # resolve them without reading or modifying the developer's real store.
    repo_root = Path(__file__).resolve().parents[2]
    isolated_store = require_cao_server.home_dir / ".aws" / "cli-agent-orchestrator" / "agent-store"
    isolated_store.mkdir(parents=True, mode=0o700, exist_ok=True)
    for profile_name in ("analysis_supervisor", "data_analyst", "report_generator"):
        source = repo_root / "examples" / "assign" / f"{profile_name}.md"
        target = isolated_store / f"{profile_name}.md"
        if not target.exists():
            shutil.copy2(source, target)


@pytest.fixture()
def require_minimax_code(require_cao_server: CaoServer):
    """Skip unless MiniMax Code is installed and has reusable local authentication."""
    if not _cli_available("mcode"):
        pytest.skip("MiniMax Code CLI not installed; see docs/minimax-code.md for installation")

    configured = os.environ.get("MINIMAX_DATA_DIR", "").strip()
    source_home = Path(configured).expanduser() if configured else Path.home() / ".minimax"
    auth_names = ("config.yaml", "local-runtime.auth.json", "cli-auth")
    if not any((source_home / name).exists() for name in auth_names):
        pytest.skip("MiniMax Code is installed but not authenticated; run `mcode login`")

    isolated_home = require_cao_server.home_dir / ".minimax"
    isolated_home.mkdir(mode=0o700, exist_ok=True)
    for name in ("config.yaml", "local-runtime.auth.json"):
        source = source_home / name
        target = isolated_home / name
        if source.is_file() and not target.exists():
            shutil.copy2(source, target)
            target.chmod(0o600)
    source_cli_auth = source_home / "cli-auth"
    target_cli_auth = isolated_home / "cli-auth"
    if source_cli_auth.is_dir() and not target_cli_auth.exists():
        shutil.copytree(source_cli_auth, target_cli_auth)

    repo_root = Path(__file__).resolve().parents[2]
    isolated_store = require_cao_server.home_dir / ".aws" / "cli-agent-orchestrator" / "agent-store"
    isolated_store.mkdir(parents=True, mode=0o700, exist_ok=True)
    for profile_name in ("analysis_supervisor", "data_analyst", "report_generator"):
        source = repo_root / "examples" / "assign" / f"{profile_name}.md"
        target = isolated_store / f"{profile_name}.md"
        if not target.exists():
            shutil.copy2(source, target)


def create_terminal(
    provider: str,
    agent_profile: str,
    session_name: str,
    retries: int = 1,
    retry_delay: float = 30.0,
):
    """Create a CAO session + terminal via the API.

    Returns (terminal_id, actual_session_name).

    If creation fails with a 500 error (typically an init timeout caused by
    API rate limiting), retries up to ``retries`` times with ``retry_delay``
    seconds between attempts. The retry uses a fresh session name to avoid
    conflicts with partially-created resources from the failed attempt.
    """
    last_resp = None
    for attempt in range(1 + retries):
        # Use a fresh session name suffix on retries to avoid collisions
        # with partially-created sessions from failed attempts.
        if attempt > 0:
            import uuid

            retry_suffix = uuid.uuid4().hex[:6]
            attempt_session_name = f"{session_name}-r{retry_suffix}"
            time.sleep(retry_delay)
        else:
            attempt_session_name = session_name

        resp = requests.post(
            f"{API_BASE_URL}/sessions",
            params={
                "provider": provider,
                "agent_profile": agent_profile,
                "session_name": attempt_session_name,
            },
        )
        last_resp = resp
        if resp.status_code in (200, 201):
            data = resp.json()
            return data["id"], data["session_name"]

        # Only retry on server errors (500) — likely rate-limit-induced init timeout
        if resp.status_code != 500 or attempt >= retries:
            break

    assert last_resp is not None and last_resp.status_code in (
        200,
        201,
    ), f"Session creation failed: {last_resp.status_code} {last_resp.text}"


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
