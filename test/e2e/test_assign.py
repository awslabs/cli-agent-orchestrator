"""End-to-end assign (async/non-blocking) tests for all providers.

Uses the real agent profiles from examples/assign/:
- data_analyst: receives a dataset, performs statistical analysis
- report_generator: creates report templates (tested via handoff in test_handoff.py)

Tests the assign flow that _assign_impl() executes:
1. Create worker terminal with data_analyst profile
2. Wait for IDLE
3. Send analysis task (matching the examples/assign/ workflow)
4. Poll for COMPLETED
5. Extract and validate output contains analysis results
6. Cleanup

Requires:
- Running CAO server
- Authenticated CLI tools (codex, claude, kiro-cli, kimi, gemini)
- tmux
- Agent profiles installed: data_analyst, report_generator
  (install with: cao install examples/assign/data_analyst.md)

Run:
    uv run pytest -m e2e test/e2e/test_assign.py -v
    uv run pytest -m e2e test/e2e/test_assign.py -v -k codex
    uv run pytest -m e2e test/e2e/test_assign.py -v -k claude_code
    uv run pytest -m e2e test/e2e/test_assign.py -v -k kiro_cli
    uv run pytest -m e2e test/e2e/test_assign.py -v -k kimi_cli
    uv run pytest -m e2e test/e2e/test_assign.py -v -k gemini_cli
"""

import time
import uuid
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

# Task message matching the examples/assign/ workflow.
# The data_analyst profile expects: dataset values, metrics to calculate,
# and a callback terminal ID. We omit the send_message callback here
# to avoid MCP tool invocation side effects during testing.
# The send_message callback is validated separately in test_send_message.py.
DATA_ANALYST_TASK = (
    "Analyze Dataset A: [1, 2, 3, 4, 5]. "
    "Calculate mean, median, and standard deviation. "
    "Present your analysis results directly."
)

DATA_ANALYST_KEYWORDS = ["mean", "median", "standard deviation", "3.0", "1.41", "dataset"]

REPORT_GENERATOR_TASK = (
    "Create a report template for data analysis with sections for: "
    "Summary of 3 datasets, Statistical analysis results, Conclusions."
)

REPORT_GENERATOR_KEYWORDS = ["summary", "analysis", "conclusion", "template", "dataset", "report"]


def _run_assign_test(provider: str, agent_profile: str, task_message: str, content_keywords: list):
    """Core assign test: create worker terminal, send task, verify completion.

    Unlike handoff, assign is non-blocking. This test validates the worker
    side of the flow: the worker receives a task, completes it, and the
    output can be extracted.
    """
    session_suffix = uuid.uuid4().hex[:6]
    session_name = f"e2e-assign-{provider}-{session_suffix}"
    terminal_id = None
    actual_session = None

    try:
        # Step 1: Create worker terminal (simulates what _assign_impl does)
        terminal_id, actual_session = create_terminal(provider, agent_profile, session_name)
        assert terminal_id, "Terminal ID should not be empty"

        # Step 2: Wait for IDLE
        assert wait_for_status(
            terminal_id, "idle", timeout=90.0
        ), f"Worker terminal did not reach IDLE within 90s (provider={provider})"
        time.sleep(2)

        # Step 3: Send task to worker
        resp = requests.post(
            f"{API_BASE_URL}/terminals/{terminal_id}/input",
            params={"message": task_message},
        )
        assert resp.status_code == 200, f"Send message failed: {resp.status_code}"

        # Step 4: Poll for COMPLETED
        assert wait_for_status(
            terminal_id, "completed", timeout=COMPLETION_TIMEOUT
        ), f"Worker did not reach COMPLETED within {COMPLETION_TIMEOUT}s (provider={provider})"

        # Step 5: Validate output
        output = extract_output(terminal_id)
        assert len(output.strip()) > 0, "Output should not be empty"

        # No TUI chrome leaking
        assert "? for shortcuts" not in output, "TUI footer leaked into output"
        assert "context left" not in output, "TUI status bar leaked into output"

        output_lower = output.lower()
        matched = [kw for kw in content_keywords if kw.lower() in output_lower]
        assert (
            matched
        ), f"Expected at least one of {content_keywords} in output, got: {output[:300]}"

    finally:
        if terminal_id and actual_session:
            cleanup_terminal(terminal_id, actual_session)


# ---------------------------------------------------------------------------
# Codex provider
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestCodexAssign:
    """E2E assign tests for the Codex provider using examples/assign/ profiles."""

    def test_assign_data_analyst(self, require_codex):
        """Codex data_analyst receives dataset, performs statistical analysis."""
        _run_assign_test(
            provider="codex",
            agent_profile="data_analyst",
            task_message=DATA_ANALYST_TASK,
            content_keywords=DATA_ANALYST_KEYWORDS,
        )

    def test_assign_report_generator(self, require_codex):
        """Codex report_generator creates a report template."""
        _run_assign_test(
            provider="codex",
            agent_profile="report_generator",
            task_message=REPORT_GENERATOR_TASK,
            content_keywords=REPORT_GENERATOR_KEYWORDS,
        )


# ---------------------------------------------------------------------------
# Claude Code provider
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestClaudeCodeAssign:
    """E2E assign tests for the Claude Code provider using examples/assign/ profiles."""

    def test_assign_data_analyst(self, require_claude):
        """Claude Code data_analyst receives dataset, performs statistical analysis."""
        _run_assign_test(
            provider="claude_code",
            agent_profile="data_analyst",
            task_message=DATA_ANALYST_TASK,
            content_keywords=DATA_ANALYST_KEYWORDS,
        )

    def test_assign_report_generator(self, require_claude):
        """Claude Code report_generator creates a report template."""
        _run_assign_test(
            provider="claude_code",
            agent_profile="report_generator",
            task_message=REPORT_GENERATOR_TASK,
            content_keywords=REPORT_GENERATOR_KEYWORDS,
        )


# ---------------------------------------------------------------------------
# Kiro CLI provider
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestKiroCliAssign:
    """E2E assign tests for the Kiro CLI provider using examples/assign/ profiles."""

    def test_assign_data_analyst(self, require_kiro):
        """Kiro CLI data_analyst receives dataset, performs statistical analysis."""
        _run_assign_test(
            provider="kiro_cli",
            agent_profile="data_analyst",
            task_message=DATA_ANALYST_TASK,
            content_keywords=DATA_ANALYST_KEYWORDS,
        )

    def test_assign_report_generator(self, require_kiro):
        """Kiro CLI report_generator creates a report template."""
        _run_assign_test(
            provider="kiro_cli",
            agent_profile="report_generator",
            task_message=REPORT_GENERATOR_TASK,
            content_keywords=REPORT_GENERATOR_KEYWORDS,
        )


# ---------------------------------------------------------------------------
# Kimi CLI provider
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestKimiCliAssign:
    """E2E assign tests for the Kimi CLI provider using examples/assign/ profiles."""

    def test_assign_data_analyst(self, require_kimi):
        """Kimi CLI data_analyst receives dataset, performs statistical analysis."""
        _run_assign_test(
            provider="kimi_cli",
            agent_profile="data_analyst",
            task_message=DATA_ANALYST_TASK,
            content_keywords=DATA_ANALYST_KEYWORDS,
        )

    def test_assign_report_generator(self, require_kimi):
        """Kimi CLI report_generator creates a report template."""
        _run_assign_test(
            provider="kimi_cli",
            agent_profile="report_generator",
            task_message=REPORT_GENERATOR_TASK,
            content_keywords=REPORT_GENERATOR_KEYWORDS,
        )


# ---------------------------------------------------------------------------
# Gemini CLI provider
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestGeminiCliAssign:
    """E2E assign tests for the Gemini CLI provider using examples/assign/ profiles."""

    def test_assign_data_analyst(self, require_gemini):
        """Gemini CLI data_analyst receives dataset, performs statistical analysis."""
        _run_assign_test(
            provider="gemini_cli",
            agent_profile="data_analyst",
            task_message=DATA_ANALYST_TASK,
            content_keywords=DATA_ANALYST_KEYWORDS,
        )

    def test_assign_report_generator(self, require_gemini):
        """Gemini CLI report_generator creates a report template."""
        _run_assign_test(
            provider="gemini_cli",
            agent_profile="report_generator",
            task_message=REPORT_GENERATOR_TASK,
            content_keywords=REPORT_GENERATOR_KEYWORDS,
        )
