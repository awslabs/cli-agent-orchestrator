"""Integration tests for Kiro CLI provider with real kiro-cli.

Tests permission prompt detection with real kiro-cli sessions.
Uses the real create_terminal() flow with FIFO pipeline and mocked DB.

Usage:
    # Headless
    pytest test/providers/test_kiro_cli_integration.py -v -o "addopts="

    # Watch mode (opens Terminal.app for each test)
    CAO_TEST_WATCH=1 pytest test/providers/test_kiro_cli_integration.py -v -o "addopts="
"""

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import pytest_asyncio

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.services.terminal_service import (
    create_terminal,
    delete_terminal,
    send_input,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

KIRO_AGENTS_DIR = Path.home() / ".kiro" / "agents"
TEST_AGENT_NAME = "agent-kiro-cli-integration-test"
WATCH_MODE = os.environ.get("CAO_TEST_WATCH", "") == "1"


@pytest.fixture(scope="session")
def kiro_cli_available():
    if not shutil.which("kiro-cli"):
        pytest.skip("kiro-cli not installed")
    return True


@pytest.fixture(scope="session")
def ensure_test_agent(kiro_cli_available):
    agent_file = KIRO_AGENTS_DIR / f"{TEST_AGENT_NAME}.json"
    if agent_file.exists():
        return TEST_AGENT_NAME

    KIRO_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    agent_config = {
        "name": TEST_AGENT_NAME,
        "description": "Integration test agent",
        "tools": ["fs_read", "execute_bash"],
        "allowedTools": ["fs_read"],
        "resources": [],
        "useLegacyMcpJson": False,
        "mcpServers": {},
    }
    with open(agent_file, "w") as f:
        json.dump(agent_config, f, indent=2)
    return TEST_AGENT_NAME


@pytest_asyncio.fixture
async def terminal(event_pipeline, mock_db, ensure_test_agent):
    """Create a real terminal via create_terminal() with full FIFO pipeline."""
    t = await create_terminal(
        provider="kiro_cli",
        agent_profile=ensure_test_agent,
        new_session=True,
    )
    yield t
    try:
        delete_terminal(t.id)
    except Exception:
        pass
    try:
        tmux_client.kill_session(t.session_name)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def dump_on_failure(request):
    """Dump terminal output when a test fails."""
    yield
    if getattr(request.node, "rep_call", None) and request.node.rep_call.failed:
        try:
            print(f"\n{'=' * 60}")
            print(f"TERMINAL DUMP for {request.node.name}")
            print(f"{'=' * 60}")
        except Exception:
            pass


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Store test result on the item for dump_on_failure fixture."""
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


@pytest.fixture(autouse=True)
def watch_session(request, terminal):
    """Open Terminal.app attached to test tmux session. Opt-in: CAO_TEST_WATCH=1"""
    if not WATCH_MODE:
        yield
        return
    proc = subprocess.Popen(
        [
            "osascript",
            "-e",
            f'tell application "Terminal" to do script '
            f'"tmux attach -t {terminal.session_name}"',
        ],
    )
    time.sleep(1)
    yield
    proc.terminate()


# --- Helpers ---

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
PERM_RE = re.compile(r"Allow this action\?.*?\[.*?y.*?/.*?n.*?/.*?t.*?\]:", re.DOTALL)


def _get_output(terminal_id):
    """Get terminal output from the status_monitor buffer."""
    return status_monitor.get_buffer(terminal_id)


def _clean(terminal_id):
    """Get terminal output with ANSI codes stripped."""
    return ANSI_RE.sub("", _get_output(terminal_id))


def _wait_for_permission(terminal_id, timeout=15):
    elapsed = 0
    while elapsed < timeout:
        if PERM_RE.search(_clean(terminal_id)):
            return True
        time.sleep(1)
        elapsed += 1
    return False


def _wait_for_status(terminal_id, target, timeout=30):
    elapsed = 0
    while elapsed < timeout:
        s = status_monitor.get_status(terminal_id)
        if s == target:
            return s
        time.sleep(1)
        elapsed += 1
    return status_monitor.get_status(terminal_id)


def _send(terminal_id, text):
    send_input(terminal_id, text)


def _log(tag, msg):
    print(f"[{tag}] {msg}")


# --- Tests ---


class TestKiroCliProviderIntegration:
    """Basic integration tests with real kiro-cli."""

    @pytest.mark.asyncio
    async def test_real_kiro_initialization_and_idle(self, terminal):
        """Covers N1/N2/N3: IDLE status after initialization."""
        _log("INIT", f"Terminal {terminal.id} initialized in {terminal.session_name}")
        status = status_monitor.get_status(terminal.id)
        _log("INIT", f"Status: {status}")
        assert status in {TerminalStatus.IDLE, TerminalStatus.COMPLETED}

    @pytest.mark.asyncio
    async def test_real_kiro_simple_query_and_completed(self, terminal):
        """Covers N6: COMPLETED status after response, message extraction."""
        _log("QUERY", "Sending: Say 'Hello, integration test!'")
        _send(terminal.id, "Say 'Hello, integration test!'")
        _log("QUERY", "Waiting for COMPLETED...")
        status = _wait_for_status(terminal.id, TerminalStatus.COMPLETED)
        _log("QUERY", f"Status: {status}")
        assert status == TerminalStatus.COMPLETED

        provider = provider_manager.get_provider(terminal.id)
        output = _get_output(terminal.id)
        msg = provider.extract_last_message_from_script(output)
        _log("QUERY", f"Extracted message length: {len(msg)}")
        assert len(msg) > 0
        assert "\x1b[" not in msg


class TestKiroCliPermissionPromptIntegration:
    """Integration tests for permission prompt detection with real kiro-cli."""

    @pytest.mark.asyncio
    async def test_p1_p2_active_permission_prompt(self, terminal):
        """P1/P2: Active permission prompt — must be WAITING_USER_ANSWER."""
        _log("P1", "Sending: Run this command: echo 'test'")
        _send(terminal.id, "Run this command: echo 'test'")
        _log("P1", "Waiting for permission prompt...")
        if not _wait_for_permission(terminal.id, timeout=30):
            pytest.skip("Permission prompt not triggered (tool may be pre-approved)")
        status = status_monitor.get_status(terminal.id)
        _log("P1", f"Status: {status}")
        assert status == TerminalStatus.WAITING_USER_ANSWER

    @pytest.mark.asyncio
    async def test_p3_p4_injection_during_active_prompt(self, terminal):
        """P3/P4: Invalid answer submitted during active prompt."""
        _log("P3", "Sending: Run: whoami")
        _send(terminal.id, "Run: whoami")
        if not _wait_for_permission(terminal.id):
            pytest.skip("Permission prompt not triggered")
        status = status_monitor.get_status(terminal.id)
        _log("P3", f"Status before injection: {status}")
        assert status == TerminalStatus.WAITING_USER_ANSWER
        _send(terminal.id, "[Test injection]")
        time.sleep(1)
        status = status_monitor.get_status(terminal.id)
        _log("P3", f"Status after injection: {status}")
        assert status == TerminalStatus.WAITING_USER_ANSWER

    @pytest.mark.asyncio
    async def test_p5_p6_stale_permission_after_answer(self, terminal):
        """P5/P6: Answered prompt — must NOT be WAITING_USER_ANSWER."""
        _send(terminal.id, "Run this bash command: echo 'stale test'")
        if not _wait_for_permission(terminal.id):
            pytest.skip("Permission prompt not triggered")
        _send(terminal.id, "y")
        status = _wait_for_status(terminal.id, TerminalStatus.COMPLETED)
        _log("P5", f"Status after answer: {status}")
        assert status != TerminalStatus.WAITING_USER_ANSWER
        assert PERM_RE.search(_clean(terminal.id))

    @pytest.mark.asyncio
    async def test_p7_multiple_permission_prompts(self, terminal):
        """P7: Second unanswered prompt after first answered."""
        _send(terminal.id, "Run: echo 'first'")
        if not _wait_for_permission(terminal.id):
            pytest.skip("Permission prompt not triggered")
        _send(terminal.id, "y")
        status = _wait_for_status(terminal.id, TerminalStatus.COMPLETED, timeout=30)
        assert status == TerminalStatus.COMPLETED, f"First command didn't complete ({status})"
        before_count = len(PERM_RE.findall(_clean(terminal.id)))
        _send(terminal.id, "Run: echo 'second'")
        elapsed = 0
        found_new = False
        while elapsed < 20:
            after_count = len(PERM_RE.findall(_clean(terminal.id)))
            if after_count > before_count:
                found_new = True
                break
            time.sleep(1)
            elapsed += 1
        if not found_new:
            pytest.skip("Second permission prompt not triggered (tool may be session-approved)")
        status = status_monitor.get_status(terminal.id)
        assert status == TerminalStatus.WAITING_USER_ANSWER

    @pytest.mark.asyncio
    async def test_n4_n5_processing_state(self, terminal):
        """N4/N5: No permission prompt during processing."""
        _send(terminal.id, "What is 2+2?")
        elapsed = 0
        status = status_monitor.get_status(terminal.id)
        while status == TerminalStatus.IDLE and elapsed < 10:
            time.sleep(0.5)
            elapsed += 0.5
            status = status_monitor.get_status(terminal.id)
        _log("N4", f"Status after {elapsed}s: {status}")
        assert status in [TerminalStatus.PROCESSING, TerminalStatus.COMPLETED]
