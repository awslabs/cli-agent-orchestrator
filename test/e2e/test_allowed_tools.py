"""End-to-end tests for allowedTools enforcement across providers.

Tests that tool restrictions set via ``allowed_tools`` API parameter are
correctly translated to each provider's native mechanism and enforced at
runtime.

Test strategy:
1. Create a terminal with restricted allowedTools (supervisor role: @cao-mcp-server only)
2. Send the agent a task that requires bash execution (e.g., "run echo hello")
3. Verify the agent CANNOT execute bash — output should indicate refusal/blocked
4. Create a terminal with unrestricted allowedTools (wildcard *)
5. Send the same bash task
6. Verify the agent CAN execute bash — output should contain the command result

Provider coverage:
- Kiro CLI: Hard enforcement via agent JSON allowedTools (set at install time).
  Tests use the built-in code_supervisor profile (role=supervisor, no execute_bash).
- Claude Code: Hard enforcement via --disallowedTools flags.
  Tests pass allowed_tools=@cao-mcp-server to trigger Bash blocking.
- Gemini CLI: Hard enforcement via Policy Engine TOML deny rules.
  Tests pass allowed_tools=@cao-mcp-server to trigger run_shell_command deny policy.
- Codex: Soft enforcement via security system prompt.
  Tests verify the prompt-based restriction is present (best-effort).
- Kimi CLI: Soft enforcement via security system prompt.
  Tests verify the prompt-based restriction is present (best-effort).

Requires:
- Running CAO server
- Authenticated CLI tools (claude, kiro-cli, gemini, kimi)
- tmux
- Agent profiles installed: code_supervisor, developer

Run:
    uv run pytest -m e2e test/e2e/test_allowed_tools.py -v -o "addopts="
    uv run pytest -m e2e test/e2e/test_allowed_tools.py -v -o "addopts=" -k kiro
    uv run pytest -m e2e test/e2e/test_allowed_tools.py -v -o "addopts=" -k claude
    uv run pytest -m e2e test/e2e/test_allowed_tools.py -v -o "addopts=" -k gemini
    uv run pytest -m e2e test/e2e/test_allowed_tools.py -v -o "addopts=" -k kimi
"""

import time
import uuid
from pathlib import Path
from test.e2e.conftest import (
    cleanup_terminal,
    create_terminal,
    extract_output,
    get_terminal_status,
    wait_for_status,
)

import pytest
import requests

from cli_agent_orchestrator.constants import API_BASE_URL

COMPLETION_TIMEOUT = 180

# Generate a random token at test-import time that the agent cannot know.
# The restricted test checks whether this token appears in a file on disk
# (proof of bash execution), not in the agent's text output.
_RANDOM_TOKEN = uuid.uuid4().hex
BASH_MARKER_FILE = f"/tmp/cao_e2e_bash_test_{_RANDOM_TOKEN}.txt"
BASH_TASK = (
    f"Run this exact shell command: echo SUCCESS > {BASH_MARKER_FILE} " f"&& cat {BASH_MARKER_FILE}"
)

# Keywords indicating the agent was blocked from using bash
REFUSAL_KEYWORDS = [
    "cannot",
    "can't",
    "don't have access",
    "not allowed",
    "not available",
    "restricted",
    "unable",
    "wasn't able",
    "was not able",
    "disallowed",
    "blocked",
    "not permitted",
    "no access",
    "do not have",
    "permission",
    "unauthorized",
    "doesn't appear to be available",
    "isn't available",
    "is not available",
    "aren't available",
]


def _create_terminal_with_tools(
    provider: str,
    agent_profile: str,
    allowed_tools: str,
    session_name: str,
    retries: int = 1,
    retry_delay: float = 30.0,
):
    """Create a terminal with explicit allowed_tools via the API.

    Returns (terminal_id, actual_session_name).
    """
    last_resp = None
    for attempt in range(1 + retries):
        if attempt > 0:
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
                "allowed_tools": allowed_tools,
            },
        )
        last_resp = resp
        if resp.status_code in (200, 201):
            data = resp.json()
            return data["id"], data["session_name"]

        if resp.status_code != 500 or attempt >= retries:
            break

    assert last_resp is not None and last_resp.status_code in (
        200,
        201,
    ), f"Session creation failed: {last_resp.status_code} {last_resp.text}"


def _wait_for_ready(terminal_id: str, timeout: float = 90.0):
    """Wait for terminal to reach idle or completed state."""
    start = time.time()
    s = "unknown"
    while time.time() - start < timeout:
        s = get_terminal_status(terminal_id)
        if s in ("idle", "completed"):
            return s
        if s == "error":
            return s
        time.sleep(3)
    return s


def _send_task_and_get_output(terminal_id: str, message: str) -> str:
    """Send a task message and wait for completion, then extract output."""
    resp = requests.post(
        f"{API_BASE_URL}/terminals/{terminal_id}/input",
        params={"message": message},
    )
    assert resp.status_code == 200, f"Send message failed: {resp.status_code}"

    assert wait_for_status(
        terminal_id, "completed", timeout=COMPLETION_TIMEOUT
    ), f"Terminal did not reach COMPLETED within {COMPLETION_TIMEOUT}s"

    # Stabilization
    time.sleep(5)
    recheck = get_terminal_status(terminal_id)
    if recheck != "completed":
        assert wait_for_status(terminal_id, "completed", timeout=COMPLETION_TIMEOUT)

    output = extract_output(terminal_id)
    assert len(output.strip()) > 0, "Output should not be empty"
    return output


def _run_restricted_tool_test(provider: str, agent_profile: str, allowed_tools: str):
    """Test that a terminal with restricted allowedTools cannot execute bash.

    Creates a terminal with the given allowed_tools restriction, sends a task
    that requires bash, and verifies the agent refuses or is blocked.
    """
    session_suffix = uuid.uuid4().hex[:6]
    session_name = f"e2e-tools-r-{provider[:5]}-{session_suffix}"
    terminal_id = None
    actual_session = None

    try:
        terminal_id, actual_session = _create_terminal_with_tools(
            provider, agent_profile, allowed_tools, session_name
        )
        assert terminal_id, "Terminal ID should not be empty"

        s = _wait_for_ready(terminal_id)
        assert s in (
            "idle",
            "completed",
        ), f"Terminal did not become ready within 90s (provider={provider})"
        time.sleep(2)

        # Clean up any leftover marker file from previous test runs
        marker_file = Path(BASH_MARKER_FILE)
        marker_file.unlink(missing_ok=True)

        # Send the bash task. For restricted agents, they may:
        # 1. Complete with a refusal message — PASS
        # 2. Time out / hang because they can't use any tools — PASS
        # 3. Actually execute bash (file created on disk) — FAIL
        resp = requests.post(
            f"{API_BASE_URL}/terminals/{terminal_id}/input",
            params={"message": BASH_TASK},
        )
        assert resp.status_code == 200, f"Send message failed: {resp.status_code}"

        completed = wait_for_status(terminal_id, "completed", timeout=COMPLETION_TIMEOUT)

        if not completed:
            # Agent timed out — it couldn't execute bash, which is the desired outcome.
            # Still verify the file wasn't created.
            pass

        # Ground truth: check whether the file was actually created on disk.
        # This cannot be faked by the agent's text output.
        time.sleep(2)
        assert not marker_file.exists(), (
            f"Agent executed bash despite restricted allowedTools! "
            f"Provider={provider}, allowed_tools={allowed_tools}. "
            f"Marker file {BASH_MARKER_FILE} was created on disk."
        )
        marker_file.unlink(missing_ok=True)

    finally:
        if terminal_id and actual_session:
            cleanup_terminal(terminal_id, actual_session)


def _run_unrestricted_tool_test(provider: str, agent_profile: str):
    """Test that a terminal with wildcard allowedTools CAN execute bash.

    Creates a terminal with allowed_tools=* (unrestricted), sends a bash task,
    and verifies the agent executes it successfully.
    """
    session_suffix = uuid.uuid4().hex[:6]
    session_name = f"e2e-tools-u-{provider[:5]}-{session_suffix}"
    terminal_id = None
    actual_session = None

    try:
        terminal_id, actual_session = _create_terminal_with_tools(
            provider, agent_profile, "*", session_name
        )
        assert terminal_id, "Terminal ID should not be empty"

        s = _wait_for_ready(terminal_id)
        assert s in (
            "idle",
            "completed",
        ), f"Terminal did not become ready within 90s (provider={provider})"
        time.sleep(2)

        # Clean up any leftover marker file
        marker_file = Path(BASH_MARKER_FILE)
        marker_file.unlink(missing_ok=True)

        output = _send_task_and_get_output(terminal_id, BASH_TASK)

        # Ground truth: the file should have been created on disk
        assert marker_file.exists(), (
            f"Agent did not execute bash despite unrestricted allowedTools. "
            f"Expected marker file {BASH_MARKER_FILE} to be created. "
            f"Provider={provider}, output: {output[:500]}"
        )
        marker_file.unlink(missing_ok=True)

    finally:
        if terminal_id and actual_session:
            cleanup_terminal(terminal_id, actual_session)


def _run_allowed_tools_stored_test(provider: str, agent_profile: str, allowed_tools: str):
    """Test that allowed_tools is stored in the database and returned by GET /terminals.

    Verifies the API correctly persists and returns the allowed_tools metadata.
    """
    session_suffix = uuid.uuid4().hex[:6]
    session_name = f"e2e-tools-s-{provider[:5]}-{session_suffix}"
    terminal_id = None
    actual_session = None

    try:
        terminal_id, actual_session = _create_terminal_with_tools(
            provider, agent_profile, allowed_tools, session_name
        )
        assert terminal_id, "Terminal ID should not be empty"

        s = _wait_for_ready(terminal_id)
        assert s in (
            "idle",
            "completed",
        ), f"Terminal did not become ready within 90s (provider={provider})"

        # Query the terminal metadata via API
        resp = requests.get(f"{API_BASE_URL}/terminals/{terminal_id}")
        assert resp.status_code == 200, f"GET terminal failed: {resp.status_code}"
        terminal_data = resp.json()

        # Verify allowed_tools is stored and returned
        stored_tools = terminal_data.get("allowed_tools")
        assert stored_tools is not None, (
            f"allowed_tools should be stored in terminal metadata. " f"Got: {terminal_data}"
        )

        # Verify the stored tools match what we sent
        expected_tools = allowed_tools.split(",")
        assert stored_tools == expected_tools, (
            f"Stored allowed_tools should match. "
            f"Expected: {expected_tools}, got: {stored_tools}"
        )

    finally:
        if terminal_id and actual_session:
            cleanup_terminal(terminal_id, actual_session)


# ---------------------------------------------------------------------------
# Kiro CLI provider — hard enforcement via agent JSON allowedTools
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestKiroCliAllowedTools:
    """E2E allowedTools tests for the Kiro CLI provider.

    Kiro CLI enforces tool restrictions at INSTALL time via the agent JSON's
    allowedTools field. Runtime allowed_tools API params are stored in the DB
    for auditing/inheritance but don't directly affect Kiro's behavior —
    enforcement happens when ``cao install`` writes the agent JSON.

    To test Kiro's actual tool blocking, first run:
        cao install src/cli_agent_orchestrator/agent_store/code_supervisor.md --provider kiro_cli
    This writes allowedTools: ["@cao-mcp-server"] into the agent JSON.
    """

    def test_unrestricted_developer_can_bash(self, require_kiro):
        """Developer with wildcard allowedTools can execute bash."""
        _run_unrestricted_tool_test(
            provider="kiro_cli",
            agent_profile="developer",
        )

    def test_allowed_tools_stored_in_metadata(self, require_kiro):
        """allowed_tools is persisted and returned by GET /terminals."""
        _run_allowed_tools_stored_test(
            provider="kiro_cli",
            agent_profile="developer",
            allowed_tools="@builtin,fs_read,@cao-mcp-server",
        )

    def test_restricted_supervisor_cannot_bash(self, require_kiro):
        """Supervisor with install-time restrictions should not execute bash.

        NOTE: This test only passes if code_supervisor was installed with:
            cao install code_supervisor --provider kiro_cli
        which writes allowedTools: ["@cao-mcp-server"] to the agent JSON.
        If the profile was installed before the allowedTools feature, the
        agent JSON won't have restrictions and this test will fail.
        Reinstall the profile to fix.
        """
        _run_restricted_tool_test(
            provider="kiro_cli",
            agent_profile="code_supervisor",
            allowed_tools="@cao-mcp-server",
        )


# ---------------------------------------------------------------------------
# Claude Code provider — hard enforcement via --disallowedTools flags
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestClaudeCodeAllowedTools:
    """E2E allowedTools tests for the Claude Code provider.

    Claude Code enforces restrictions via --disallowedTools flags alongside
    --dangerously-skip-permissions. When allowed_tools=@cao-mcp-server,
    the Bash tool is added to --disallowedTools.
    """

    def test_restricted_supervisor_cannot_bash(self, require_claude):
        """Supervisor with only @cao-mcp-server should have Bash disallowed."""
        _run_restricted_tool_test(
            provider="claude_code",
            agent_profile="code_supervisor",
            allowed_tools="@cao-mcp-server",
        )

    def test_unrestricted_developer_can_bash(self, require_claude):
        """Developer with wildcard allowedTools can execute bash."""
        _run_unrestricted_tool_test(
            provider="claude_code",
            agent_profile="developer",
        )

    def test_reviewer_cannot_write(self, require_claude):
        """Reviewer with fs_read only should not be able to write files."""
        session_suffix = uuid.uuid4().hex[:6]
        session_name = f"e2e-tools-rev-claude-{session_suffix}"
        terminal_id = None
        actual_session = None

        try:
            terminal_id, actual_session = _create_terminal_with_tools(
                "claude_code",
                "reviewer",
                "@builtin,fs_read,fs_list,@cao-mcp-server",
                session_name,
            )
            assert terminal_id, "Terminal ID should not be empty"

            s = _wait_for_ready(terminal_id)
            assert s in ("idle", "completed")
            time.sleep(2)

            # Ask the agent to write a file — should be blocked
            write_task = (
                "Create a file called /tmp/cao_e2e_test_write.txt with the content "
                "'WRITE_TEST_MARKER'. Write the file now."
            )
            output = _send_task_and_get_output(terminal_id, write_task)
            output_lower = output.lower()

            # Agent should not have written the file
            assert "WRITE_TEST_MARKER" not in output or any(
                kw in output_lower for kw in REFUSAL_KEYWORDS
            ), f"Reviewer should not be able to write files. Output: {output[:500]}"

        finally:
            if terminal_id and actual_session:
                cleanup_terminal(terminal_id, actual_session)

    def test_allowed_tools_stored_in_metadata(self, require_claude):
        """allowed_tools is persisted and returned by GET /terminals."""
        _run_allowed_tools_stored_test(
            provider="claude_code",
            agent_profile="developer",
            allowed_tools="@builtin,fs_*,execute_bash,@cao-mcp-server",
        )


# ---------------------------------------------------------------------------
# Codex provider — soft enforcement via security system prompt
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestCodexAllowedTools:
    """E2E allowedTools tests for the Codex provider.

    Codex has no native tool restriction mechanism. Enforcement is via
    security system prompt prepended to the agent's instructions. This is
    soft enforcement — the agent may still attempt restricted actions.

    Tests verify that:
    1. The security prompt is effective (agent acknowledges restrictions)
    2. Unrestricted mode works normally
    """

    @pytest.mark.xfail(
        reason="Soft enforcement — Codex may ignore prompt-based restrictions",
        strict=False,
    )
    def test_restricted_supervisor_refuses_bash(self, require_codex):
        """Supervisor with only @cao-mcp-server should refuse bash (soft enforcement).

        Codex's enforcement is prompt-based. The agent should acknowledge it
        cannot run shell commands, though this is not a hard guarantee.
        Marked xfail because soft enforcement may be bypassed.
        """
        _run_restricted_tool_test(
            provider="codex",
            agent_profile="code_supervisor",
            allowed_tools="@cao-mcp-server",
        )

    def test_unrestricted_developer_can_bash(self, require_codex):
        """Developer with wildcard allowedTools can execute bash."""
        _run_unrestricted_tool_test(
            provider="codex",
            agent_profile="developer",
        )

    def test_allowed_tools_stored_in_metadata(self, require_codex):
        """allowed_tools is persisted and returned by GET /terminals."""
        _run_allowed_tools_stored_test(
            provider="codex",
            agent_profile="developer",
            allowed_tools="@cao-mcp-server",
        )


# ---------------------------------------------------------------------------
# Kimi CLI provider — soft enforcement via security system prompt
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestKimiCliAllowedTools:
    """E2E allowedTools tests for the Kimi CLI provider.

    Kimi CLI has no native tool restriction mechanism. Enforcement is via
    security system prompt prepended to the agent's instructions. This is
    soft enforcement — the agent may still attempt restricted actions.

    Tests verify that:
    1. The security prompt is effective (agent acknowledges restrictions)
    2. Unrestricted mode works normally
    """

    @pytest.mark.xfail(
        reason="Soft enforcement — Kimi CLI may ignore prompt-based restrictions",
        strict=False,
    )
    def test_restricted_supervisor_refuses_bash(self, require_kimi):
        """Supervisor with only @cao-mcp-server should refuse bash (soft enforcement).

        Kimi's enforcement is prompt-based. The agent should acknowledge it
        cannot run shell commands, though this is not a hard guarantee.
        Marked xfail because soft enforcement may be bypassed.
        """
        _run_restricted_tool_test(
            provider="kimi_cli",
            agent_profile="code_supervisor",
            allowed_tools="@cao-mcp-server",
        )

    def test_unrestricted_developer_can_bash(self, require_kimi):
        """Developer with wildcard allowedTools can execute bash."""
        _run_unrestricted_tool_test(
            provider="kimi_cli",
            agent_profile="developer",
        )

    def test_allowed_tools_stored_in_metadata(self, require_kimi):
        """allowed_tools is persisted and returned by GET /terminals."""
        _run_allowed_tools_stored_test(
            provider="kimi_cli",
            agent_profile="developer",
            allowed_tools="@cao-mcp-server",
        )


# ---------------------------------------------------------------------------
# Gemini CLI provider — hard enforcement via Policy Engine TOML deny rules
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestGeminiCliAllowedTools:
    """E2E allowedTools tests for the Gemini CLI provider.

    Gemini CLI enforces restrictions via Policy Engine TOML deny rules
    written to ~/.gemini/policies/cao-{terminal_id}.toml. Deny rules
    completely exclude tools from the model's memory, providing hard
    enforcement even in --yolo mode. When allowed_tools=@cao-mcp-server,
    run_shell_command is denied via policy.
    """

    def test_restricted_supervisor_cannot_bash(self, require_gemini):
        """Supervisor with only @cao-mcp-server should not execute bash.

        Enforcement: Policy Engine TOML deny rules in ~/.gemini/policies/.
        Deny rules completely exclude tools from the model's memory, providing
        hard enforcement even in --yolo mode.
        """
        _run_restricted_tool_test(
            provider="gemini_cli",
            agent_profile="code_supervisor",
            allowed_tools="@cao-mcp-server",
        )

    def test_unrestricted_developer_can_bash(self, require_gemini):
        """Developer with wildcard allowedTools can execute bash."""
        _run_unrestricted_tool_test(
            provider="gemini_cli",
            agent_profile="developer",
        )

    def test_allowed_tools_stored_in_metadata(self, require_gemini):
        """allowed_tools is persisted and returned by GET /terminals."""
        _run_allowed_tools_stored_test(
            provider="gemini_cli",
            agent_profile="developer",
            allowed_tools="@builtin,fs_read,@cao-mcp-server",
        )
