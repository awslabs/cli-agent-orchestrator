"""End-to-end provider lifecycle tests (handoff worker simulation).

Tests the worker side of the handoff flow — validates that each provider can:
1. Create session + terminal with a developer agent profile
2. Reach IDLE state (CLI tool initialized)
3. Receive a task message via the API
4. Process the task and reach COMPLETED
5. Return extractable output

NOTE: These tests do NOT test a supervisor agent calling the handoff() MCP tool.
For real supervisor→worker delegation tests, see test_supervisor_orchestration.py.

Requires: running CAO server, authenticated CLI tools (codex, claude, kiro-cli, kimi, gemini), tmux.

Run:
    uv run pytest -m e2e test/e2e/test_handoff.py -v
    uv run pytest -m e2e test/e2e/test_handoff.py -v -k codex
    uv run pytest -m e2e test/e2e/test_handoff.py -v -k claude_code
    uv run pytest -m e2e test/e2e/test_handoff.py -v -k kiro_cli
    uv run pytest -m e2e test/e2e/test_handoff.py -v -k kimi_cli
    uv run pytest -m e2e test/e2e/test_handoff.py -v -k gemini_cli
"""

import time
import uuid
from test.e2e.conftest import (
    cleanup_terminal,
    create_terminal,
    extract_output,
    send_handoff_message,
    wait_for_status,
)

import pytest

# Timeout for waiting for agent completion (seconds).
# Agents may take varying amounts of time depending on model and task complexity.
COMPLETION_TIMEOUT = 180


def _run_handoff_test(provider: str, agent_profile: str, task_message: str, content_keywords: list):
    """Core handoff test logic shared across providers.

    Args:
        provider: Provider name ("codex", "claude_code", "kiro_cli", "kimi_cli", "gemini_cli")
        agent_profile: Agent profile name ("developer")
        task_message: The task to send to the agent
        content_keywords: Words expected in the output (at least one must match)
    """
    session_suffix = uuid.uuid4().hex[:6]
    session_name = f"e2e-handoff-{provider}-{session_suffix}"
    terminal_id = None
    actual_session = None

    try:
        # Step 1: Create terminal
        terminal_id, actual_session = create_terminal(provider, agent_profile, session_name)
        assert terminal_id, "Terminal ID should not be empty"

        # Step 2: Wait for IDLE
        assert wait_for_status(
            terminal_id, "idle", timeout=90.0
        ), f"Terminal did not reach IDLE within 90s (provider={provider})"

        # Settle time before sending message
        time.sleep(2)

        # Step 3: Send handoff message
        send_handoff_message(terminal_id, task_message, provider)

        # Step 4: Poll for COMPLETED
        assert wait_for_status(
            terminal_id, "completed", timeout=COMPLETION_TIMEOUT
        ), f"Terminal did not reach COMPLETED within {COMPLETION_TIMEOUT}s (provider={provider})"

        # Step 5: Extract output
        output = extract_output(terminal_id)

        # Step 6: Validate output
        assert len(output.strip()) > 0, "Output should not be empty"

        # No TUI chrome leaking into output
        assert "? for shortcuts" not in output, "TUI footer leaked into output"
        assert "context left" not in output, "TUI status bar leaked into output"
        assert "esc to interrupt" not in output, "TUI spinner leaked into output"

        # Handoff prefix should not appear in extracted output
        assert "[CAO Handoff]" not in output, "Handoff prefix leaked into output"

        # At least one content keyword should be present
        output_lower = output.lower()
        matched = [kw for kw in content_keywords if kw.lower() in output_lower]
        assert (
            matched
        ), f"Expected at least one of {content_keywords} in output, got: {output[:200]}"

    finally:
        if terminal_id and actual_session:
            cleanup_terminal(terminal_id, actual_session)


# ---------------------------------------------------------------------------
# Codex provider tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestCodexHandoff:
    """E2E handoff tests for the Codex provider."""

    def test_handoff_simple_function(self, require_codex):
        """Codex developer creates a simple Python function and returns output."""
        _run_handoff_test(
            provider="codex",
            agent_profile="developer",
            task_message=(
                "Create a Python function called 'greet' that takes a name parameter "
                "and returns 'Hello, {name}!'. Output only the function code."
            ),
            content_keywords=["greet", "hello", "def"],
        )

    def test_handoff_second_task(self, require_codex):
        """Codex developer handles a second independent task (validates no state leakage)."""
        _run_handoff_test(
            provider="codex",
            agent_profile="developer",
            task_message=(
                "Create a Python function called 'add_numbers' that takes two parameters "
                "a and b and returns their sum. Output only the function code."
            ),
            content_keywords=["add", "sum", "return", "def"],
        )


# ---------------------------------------------------------------------------
# Claude Code provider tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestClaudeCodeHandoff:
    """E2E handoff tests for the Claude Code provider."""

    def test_handoff_simple_function(self, require_claude):
        """Claude Code developer creates a simple Python function and returns output."""
        _run_handoff_test(
            provider="claude_code",
            agent_profile="developer",
            task_message=(
                "Create a Python function called 'greet' that takes a name parameter "
                "and returns 'Hello, {name}!'. Output only the function code."
            ),
            content_keywords=["greet", "hello", "def"],
        )

    def test_handoff_second_task(self, require_claude):
        """Claude Code developer handles a second independent task."""
        _run_handoff_test(
            provider="claude_code",
            agent_profile="developer",
            task_message=(
                "Create a Python function called 'multiply' that takes two parameters "
                "a and b and returns their product. Output only the function code."
            ),
            content_keywords=["multiply", "product", "return", "def"],
        )


# ---------------------------------------------------------------------------
# Kiro CLI provider tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestKiroCliHandoff:
    """E2E handoff tests for the Kiro CLI provider."""

    def test_handoff_simple_function(self, require_kiro):
        """Kiro CLI developer creates a simple Python function and returns output."""
        _run_handoff_test(
            provider="kiro_cli",
            agent_profile="developer",
            task_message=(
                "Create a Python function called 'greet' that takes a name parameter "
                "and returns 'Hello, {name}!'. Output only the function code."
            ),
            content_keywords=["greet", "hello", "def"],
        )

    def test_handoff_second_task(self, require_kiro):
        """Kiro CLI developer handles a second independent task."""
        _run_handoff_test(
            provider="kiro_cli",
            agent_profile="developer",
            task_message=(
                "Create a Python function called 'subtract' that takes two parameters "
                "a and b and returns a minus b. Output only the function code."
            ),
            content_keywords=["subtract", "return", "def"],
        )


# ---------------------------------------------------------------------------
# Kimi CLI provider tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestKimiCliHandoff:
    """E2E handoff tests for the Kimi CLI provider."""

    def test_handoff_simple_function(self, require_kimi):
        """Kimi CLI developer creates a simple Python function and returns output."""
        _run_handoff_test(
            provider="kimi_cli",
            agent_profile="developer",
            task_message=(
                "Create a Python function called 'greet' that takes a name parameter "
                "and returns 'Hello, {name}!'. Output only the function code."
            ),
            content_keywords=["greet", "hello", "def"],
        )

    def test_handoff_second_task(self, require_kimi):
        """Kimi CLI developer handles a second independent task."""
        _run_handoff_test(
            provider="kimi_cli",
            agent_profile="developer",
            task_message=(
                "Create a Python function called 'divide' that takes two parameters "
                "a and b and returns a divided by b. Output only the function code."
            ),
            content_keywords=["divide", "return", "def"],
        )


# ---------------------------------------------------------------------------
# Gemini CLI provider tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestGeminiCliHandoff:
    """E2E handoff tests for the Gemini CLI provider."""

    def test_handoff_simple_function(self, require_gemini):
        """Gemini CLI developer creates a simple Python function and returns output."""
        _run_handoff_test(
            provider="gemini_cli",
            agent_profile="developer",
            task_message=(
                "Create a Python function called 'greet' that takes a name parameter "
                "and returns 'Hello, {name}!'. Output only the function code."
            ),
            content_keywords=["greet", "hello", "def"],
        )

    def test_handoff_second_task(self, require_gemini):
        """Gemini CLI developer handles a second independent task."""
        _run_handoff_test(
            provider="gemini_cli",
            agent_profile="developer",
            task_message=(
                "Create a Python function called 'square' that takes a parameter n "
                "and returns n squared. Output only the function code."
            ),
            content_keywords=["square", "return", "def"],
        )
