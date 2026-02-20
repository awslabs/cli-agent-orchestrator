"""Integration tests for Kiro CLI provider with real kiro-cli.

Tests permission prompt detection with real kiro-cli sessions.

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

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider
from cli_agent_orchestrator.utils.terminal import wait_for_shell

pytestmark = [pytest.mark.integration, pytest.mark.slow]

KIRO_AGENTS_DIR = Path.home() / ".kiro" / "agents"
TEST_AGENT_NAME = "agent-kiro-cli-integration-test"
WATCH_MODE = os.environ.get("CAO_TEST_WATCH", "") == "1"
WINDOW_NAME = "window-0"
TERMINAL_ID = "test1234"


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


@pytest.fixture
def test_session_name():
    import uuid

    return f"test-kiro-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def cleanup_session(test_session_name):
    yield
    try:
        tmux_client.kill_session(test_session_name)
    except Exception:
        pass


@pytest.fixture
def provider(ensure_test_agent, test_session_name, cleanup_session):
    """Create tmux session and provider, ready for use."""
    tmux_client.create_session(test_session_name, WINDOW_NAME, TERMINAL_ID)
    return KiroCliProvider(TERMINAL_ID, test_session_name, WINDOW_NAME, ensure_test_agent)


@pytest.fixture(autouse=True)
def dump_on_failure(request, test_session_name):
    """Dump terminal output when a test fails."""
    yield
    if getattr(request.node, "rep_call", None) and request.node.rep_call.failed:
        try:
            output = _clean(test_session_name)
            print(f"\n{'=' * 60}")
            print(f"TERMINAL DUMP for {request.node.name}")
            print(f"{'=' * 60}")
            print(output[-1500:])
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
def watch_session(test_session_name, provider):
    """Open Terminal.app attached to test tmux session. Opt-in: CAO_TEST_WATCH=1"""
    if not WATCH_MODE:
        yield
        return
    proc = subprocess.Popen(
        [
            "osascript",
            "-e",
            f'tell application "Terminal" to do script "tmux attach -t {test_session_name}"',
        ],
    )
    time.sleep(1)
    yield
    proc.terminate()


# --- Helpers ---

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
PERM_RE = re.compile(r"Allow this action\?.*?\[.*?y.*?/.*?n.*?/.*?t.*?\]:", re.DOTALL)


def _clean(session):
    """Get terminal output with ANSI codes stripped."""
    raw = tmux_client.get_history(session, WINDOW_NAME)
    return ANSI_RE.sub("", raw)


def _wait_for_permission(test_session_name, timeout=15):
    elapsed = 0
    while elapsed < timeout:
        if PERM_RE.search(_clean(test_session_name)):
            return True
        time.sleep(1)
        elapsed += 1
    return False


def _wait_for_status(provider, target, timeout=30):
    elapsed = 0
    while elapsed < timeout:
        s = provider.get_status()
        if s == target:
            return s
        time.sleep(1)
        elapsed += 1
    return provider.get_status()


def _send(session, text):
    tmux_client.send_keys(session, WINDOW_NAME, text)


def _log(tag, msg):
    print(f"[{tag}] {msg}")


# --- Tests ---


class TestKiroCliProviderIntegration:
    """Basic integration tests with real kiro-cli.

    Also covers non-permission cases:
    - N1/N2/N3 (idle states): test_real_kiro_initialization verifies IDLE after init
    - N6 (completed response): test_real_kiro_simple_query verifies COMPLETED + message extraction
    """

    def test_real_kiro_initialization_and_idle(self, provider, test_session_name):
        """Covers N1/N2/N3: IDLE status after initialization, with or without trailing text."""
        _log("INIT", "Initializing kiro-cli...")
        assert provider.initialize() is True
        time.sleep(2)
        status = provider.get_status()
        _log("INIT", f"Status: {status}")
        assert status == TerminalStatus.IDLE

    def test_real_kiro_simple_query_and_completed(self, provider, test_session_name):
        """Covers N6: COMPLETED status after response, message extraction, ANSI stripping."""
        _log("QUERY", "Initializing...")
        provider.initialize()
        time.sleep(2)
        _log("QUERY", "Sending: Say 'Hello, integration test!'")
        _send(test_session_name, "Say 'Hello, integration test!'")
        _log("QUERY", "Waiting for COMPLETED...")
        status = _wait_for_status(provider, TerminalStatus.COMPLETED)
        _log("QUERY", f"Status: {status}")
        assert status == TerminalStatus.COMPLETED
        msg = provider.extract_last_message_from_script(_clean(test_session_name))
        _log("QUERY", f"Extracted message length: {len(msg)}")
        assert len(msg) > 0
        assert "\x1b[" not in msg


class TestKiroCliPermissionPromptIntegration:
    """Integration tests for permission prompt detection with real kiro-cli.

    Case IDs reference the permission prompt analysis from 605 terminal logs
    documented in ~/kb/cao/bugs/inbox_delivers_during_permission_prompt.md.

    P = permission prompt present, N = no permission prompt.
    """

    def test_p1_p2_active_permission_prompt(self, provider, test_session_name):
        """P1/P2: Active permission prompt — must be WAITING_USER_ANSWER.

        Triggers execute_bash which requires permission. Verifies the
        line-based counting detects the active prompt regardless of
        trailing text on the idle prompt line below.
        """
        _log("P1", "Initializing...")
        provider.initialize()
        time.sleep(2)
        _log("P1", "Sending: Run this command: echo 'test'")
        _send(test_session_name, "Run this command: echo 'test'")
        _log("P1", "Waiting for permission prompt...")
        if not _wait_for_permission(test_session_name, timeout=30):
            pytest.skip("Permission prompt not triggered (tool may be pre-approved)")
        _log("P1", "Permission prompt found, checking status...")
        status = provider.get_status()
        _log("P1", f"Status: {status}")
        assert status == TerminalStatus.WAITING_USER_ANSWER
        assert "Allow this action?" in _clean(test_session_name)

    def test_p3_p4_injection_during_active_prompt(self, provider, test_session_name):
        """P3/P4: Invalid answer submitted during active prompt.

        Sends '[Test injection]' as answer to [y/n/t]: prompt. kiro-cli
        rejects it (not y/n/t) and re-renders the prompt. Verifies status
        remains WAITING_USER_ANSWER — the re-rendered prompt is still active.

        Note: send_keys includes Enter, so this submits the text rather than
        typing without pressing Enter (P8 partial typing case would need
        tmux send-keys without Enter, which the API doesn't support yet).
        """
        _log("P3", "Initializing...")
        provider.initialize()
        time.sleep(2)
        _log("P3", "Sending: Run: whoami")
        _send(test_session_name, "Run: whoami")
        _log("P3", "Waiting for permission prompt...")
        if not _wait_for_permission(test_session_name):
            pytest.skip("Permission prompt not triggered")
        _log("P3", "Permission prompt found, checking status...")
        status = provider.get_status()
        _log("P3", f"Status before injection: {status}")
        assert status == TerminalStatus.WAITING_USER_ANSWER
        _log("P3", "Sending invalid answer: [Test injection]")
        _send(test_session_name, "[Test injection]")
        time.sleep(1)
        status = provider.get_status()
        _log("P3", f"Status after injection: {status}")
        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_p5_p6_stale_permission_after_answer(self, provider, test_session_name):
        """P5/P6: Answered prompt — must NOT be WAITING_USER_ANSWER.

        Answers 'y' to permission prompt, waits for tool to complete.
        Verifies the old [y/n/t]: in history is correctly identified as
        stale (>1 idle prompt lines after it) and doesn't block status.
        """
        _log("P5", "Initializing...")
        provider.initialize()
        time.sleep(2)
        _log("P5", "Sending: Run: echo 'stale test'")
        _send(test_session_name, "Run this bash command: echo 'stale test'")
        _log("P5", "Waiting for permission prompt...")
        if not _wait_for_permission(test_session_name):
            pytest.skip("Permission prompt not triggered")
        _log("P5", "Answering 'y'...")
        _send(test_session_name, "y")
        _log("P5", "Waiting for COMPLETED...")
        status = _wait_for_status(provider, TerminalStatus.COMPLETED)
        _log("P5", f"Status after answer: {status}")
        assert status != TerminalStatus.WAITING_USER_ANSWER
        assert PERM_RE.search(_clean(test_session_name))

    def test_p7_multiple_permission_prompts(self, provider, test_session_name):
        """P7: Second unanswered prompt after first answered.

        Answers first prompt, waits for completion, sends second command.
        Counts permission prompts to detect a genuinely new one (not the
        stale first). Verifies line-based counting uses the LAST prompt.
        """
        _log("P7", "Initializing...")
        provider.initialize()
        time.sleep(2)
        _log("P7", "Sending: Run: echo 'first'")
        _send(test_session_name, "Run: echo 'first'")
        _log("P7", "Waiting for first permission prompt...")
        if not _wait_for_permission(test_session_name):
            pytest.skip("Permission prompt not triggered")
        _log("P7", "Answering 'y'...")
        _send(test_session_name, "y")
        _log("P7", "Waiting for COMPLETED...")
        status = _wait_for_status(provider, TerminalStatus.COMPLETED, timeout=30)
        _log("P7", f"Status after first answer: {status}")
        assert (
            status == TerminalStatus.COMPLETED
        ), f"First command didn't complete (status={status}), can't test second prompt"
        before_count = len(PERM_RE.findall(_clean(test_session_name)))
        _log("P7", f"Permission prompts so far: {before_count}")
        _log("P7", "Sending: Run: echo 'second'")
        _send(test_session_name, "Run: echo 'second'")
        _log("P7", "Waiting for NEW permission prompt...")
        elapsed = 0
        found_new = False
        while elapsed < 20:
            after_count = len(PERM_RE.findall(_clean(test_session_name)))
            if after_count > before_count:
                found_new = True
                break
            time.sleep(1)
            elapsed += 1
        if not found_new:
            pytest.skip("Second permission prompt not triggered (tool may be session-approved)")
        status = provider.get_status()
        _log("P7", f"Status: {status}")
        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_n4_n5_processing_state(self, provider, test_session_name):
        """N4/N5: No permission prompt during processing.

        Sends a query and polls until kiro-cli leaves IDLE. Verifies
        status is PROCESSING or COMPLETED, never WAITING_USER_ANSWER.
        """
        _log("N4", "Initializing...")
        provider.initialize()
        time.sleep(2)
        _log("N4", "Sending: What is 2+2?")
        _send(test_session_name, "What is 2+2?")
        _log("N4", "Polling for non-IDLE status...")
        elapsed = 0
        status = provider.get_status()
        while status == TerminalStatus.IDLE and elapsed < 10:
            time.sleep(0.5)
            elapsed += 0.5
            status = provider.get_status()
        _log("N4", f"Status after {elapsed}s: {status}")
        assert status in [TerminalStatus.PROCESSING, TerminalStatus.COMPLETED]
