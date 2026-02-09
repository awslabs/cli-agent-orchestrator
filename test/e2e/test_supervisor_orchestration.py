"""End-to-end supervisor orchestration tests for all providers.

Tests the REAL multi-agent flow where a supervisor agent uses MCP tools
(handoff, assign, send_message) to delegate work to sub-agents:

1. Launch supervisor with analysis_supervisor profile
2. Send a task requiring delegation
3. Verify supervisor calls handoff/assign (worker windows appear)
4. Verify supervisor produces a final combined response
5. Cleanup

This is distinct from test_handoff.py and test_assign.py which only test
individual worker agents receiving tasks directly via the API. Those tests
validate provider lifecycle (can receive input, produce output) but do NOT
test the MCP tool delegation flow.

Requires:
- Running CAO server
- Authenticated CLI tools
- Agent profiles installed: analysis_supervisor, data_analyst, report_generator
  (install with: cao install examples/assign/analysis_supervisor.md)

Run:
    uv run pytest -m e2e test/e2e/test_supervisor_orchestration.py -v -o "addopts="
    uv run pytest -m e2e test/e2e/test_supervisor_orchestration.py -v -o "addopts=" -k codex
    uv run pytest -m e2e test/e2e/test_supervisor_orchestration.py -v -o "addopts=" -k gemini_cli
"""

import time
import uuid
from test.e2e.conftest import (
    cleanup_terminal,
    create_terminal,
    extract_output,
    get_terminal_status,
)

import pytest
import requests

from cli_agent_orchestrator.constants import API_BASE_URL

# Longer timeout for supervisor orchestration — the supervisor must:
# 1. Process the task
# 2. Call handoff/assign MCP tools (which create new terminals)
# 3. Wait for workers to initialize and complete
# 4. Collect results and produce final output
SUPERVISOR_COMPLETION_TIMEOUT = 300


def _list_terminals_in_session(session_name: str) -> list:
    """List all terminals in a session via the API."""
    resp = requests.get(f"{API_BASE_URL}/sessions/{session_name}")
    if resp.status_code != 200:
        return []
    data = resp.json()
    return data.get("terminals", [])


def _wait_for_ready(terminal_id: str, timeout: float = 120.0, poll: float = 3.0) -> bool:
    """Wait for provider to be ready (idle or completed).

    After initialization, most providers reach 'idle'. However, providers
    that use an initial prompt (e.g. Gemini CLI with -i flag) reach
    'completed' because the prompt produces a response. Both states
    indicate the provider is ready to accept input.
    """
    start = time.time()
    while time.time() - start < timeout:
        status = get_terminal_status(terminal_id)
        if status in ("idle", "completed"):
            return True
        if status == "error":
            return False
        time.sleep(poll)
    return False


def _wait_for_supervisor_done(
    supervisor_id: str,
    session_name: str,
    min_terminals: int,
    timeout: float = SUPERVISOR_COMPLETION_TIMEOUT,
    poll: float = 5.0,
) -> tuple:
    """Wait for supervisor to reach COMPLETED AND spawn expected workers.

    Some providers (notably Gemini CLI) report COMPLETED after initial text
    output but before MCP tool calls (handoff/assign) finish creating worker
    terminals. Gemini's Ink TUI keeps the idle prompt visible at all times,
    so the status detector sees "response + idle prompt" = COMPLETED even
    while the model is between text output and the first MCP tool call.

    This function polls until BOTH conditions are true:
    - Terminal status is 'completed'
    - At least min_terminals exist in the session (supervisor + workers)

    Returns (last_status, terminals_list).
    """
    start = time.time()
    last_status = "unknown"
    terminals = []

    while time.time() - start < timeout:
        last_status = get_terminal_status(supervisor_id)
        terminals = _list_terminals_in_session(session_name)

        if last_status == "error":
            return last_status, terminals

        if last_status == "completed" and len(terminals) >= min_terminals:
            return last_status, terminals

        time.sleep(poll)

    return last_status, terminals


def _run_supervisor_handoff_test(provider: str):
    """Test supervisor uses handoff MCP tool to delegate to a worker.

    Flow:
    1. Launch supervisor with analysis_supervisor profile
    2. Send a simple task that requires handoff to report_generator
    3. Wait for supervisor to complete
    4. Verify worker terminal(s) were created in the session
    5. Verify supervisor output contains report-related content
    """
    session_suffix = uuid.uuid4().hex[:6]
    session_name = f"e2e-super-{provider}-{session_suffix}"
    supervisor_id = None
    actual_session = None

    try:
        # Step 1: Create supervisor terminal
        supervisor_id, actual_session = create_terminal(
            provider, "analysis_supervisor", session_name
        )
        assert supervisor_id, "Supervisor terminal ID should not be empty"

        # Step 2: Wait for provider to be ready (idle or completed).
        # Providers with initial prompts (Gemini CLI -i) reach 'completed'
        # after processing the system prompt; others reach 'idle'.
        assert _wait_for_ready(
            supervisor_id, timeout=120.0
        ), f"Supervisor did not become ready within 120s (provider={provider})"
        time.sleep(2)

        # Step 3: Send task that requires delegation.
        # Use a simple handoff-only task to keep the test focused.
        task_message = (
            "Use the handoff tool to delegate this task to the report_generator agent: "
            "Create a simple report template with sections for Summary, Analysis, and Conclusions. "
            "Then present the report template you received back from the handoff."
        )
        resp = requests.post(
            f"{API_BASE_URL}/terminals/{supervisor_id}/input",
            params={"message": task_message},
        )
        assert resp.status_code == 200, f"Send message failed: {resp.status_code}"

        # Step 4+5: Wait for supervisor to complete AND create worker terminal.
        # Uses combined polling because some providers (Gemini CLI) report
        # COMPLETED from initial text output before MCP tool calls finish.
        status, terminals = _wait_for_supervisor_done(
            supervisor_id, actual_session, min_terminals=2
        )
        assert status == "completed", (
            f"Supervisor did not reach COMPLETED within {SUPERVISOR_COMPLETION_TIMEOUT}s "
            f"(provider={provider}). Last status: {status}"
        )
        assert len(terminals) >= 2, (
            f"Expected at least 2 terminals (supervisor + worker), got {len(terminals)}. "
            f"The supervisor may not have called the handoff MCP tool."
        )

        # Step 6: Extract and validate supervisor output.
        output = extract_output(supervisor_id)
        assert len(output.strip()) > 0, "Supervisor output should not be empty"

        # The supervisor should have presented the report template from the worker.
        output_lower = output.lower()
        report_keywords = ["summary", "analysis", "conclusion", "report", "template"]
        matched = [kw for kw in report_keywords if kw in output_lower]
        assert matched, (
            f"Supervisor output should contain report-related content. "
            f"Expected at least one of {report_keywords}, got: {output[:300]}"
        )

    finally:
        # Cleanup all terminals in the session
        if actual_session:
            terminals = _list_terminals_in_session(actual_session)
            for term in terminals:
                tid = term.get("id", "")
                if tid:
                    try:
                        requests.post(f"{API_BASE_URL}/terminals/{tid}/exit")
                    except Exception:
                        pass
            time.sleep(3)
            try:
                requests.delete(f"{API_BASE_URL}/sessions/{actual_session}")
            except Exception:
                pass


def _run_supervisor_assign_test(provider: str):
    """Test supervisor uses assign MCP tool to spawn parallel workers.

    Flow:
    1. Launch supervisor with analysis_supervisor profile
    2. Send a task that requires assigning data analysts
    3. Wait for supervisor to complete
    4. Verify multiple worker terminals were created
    5. Verify supervisor output contains analysis results
    """
    session_suffix = uuid.uuid4().hex[:6]
    session_name = f"e2e-super-{provider}-{session_suffix}"
    supervisor_id = None
    actual_session = None

    try:
        # Step 1: Create supervisor terminal
        supervisor_id, actual_session = create_terminal(
            provider, "analysis_supervisor", session_name
        )
        assert supervisor_id, "Supervisor terminal ID should not be empty"

        # Step 2: Wait for provider to be ready (idle or completed).
        # Providers with initial prompts (Gemini CLI -i) reach 'completed'
        # after processing the system prompt; others reach 'idle'.
        assert _wait_for_ready(
            supervisor_id, timeout=120.0
        ), f"Supervisor did not become ready within 120s (provider={provider})"
        time.sleep(2)

        # Step 3: Send task requiring assign + handoff.
        # Keep it simple: 1 dataset to analyze + 1 report to generate.
        task_message = (
            "Analyze this dataset and create a report:\n"
            "- Dataset A: [1, 2, 3, 4, 5]\n\n"
            "Use assign to spawn a data_analyst for Dataset A, "
            "and use handoff to get a report template from report_generator. "
            "Then combine the results and present the final report."
        )
        resp = requests.post(
            f"{API_BASE_URL}/terminals/{supervisor_id}/input",
            params={"message": task_message},
        )
        assert resp.status_code == 200, f"Send message failed: {resp.status_code}"

        # Step 4+5: Wait for supervisor to complete AND create worker terminals.
        # assign(data_analyst) + handoff(report_generator) = at least 3 terminals.
        # Uses combined polling because some providers (Gemini CLI) report
        # COMPLETED from initial text output before MCP tool calls finish.
        status, terminals = _wait_for_supervisor_done(
            supervisor_id, actual_session, min_terminals=3
        )
        assert status == "completed", (
            f"Supervisor did not reach COMPLETED within {SUPERVISOR_COMPLETION_TIMEOUT}s "
            f"(provider={provider}). Last status: {status}"
        )
        assert len(terminals) >= 3, (
            f"Expected at least 3 terminals (supervisor + data_analyst + report_generator), "
            f"got {len(terminals)}. The supervisor may not have called assign/handoff tools."
        )

        # Step 6: Validate supervisor output.
        output = extract_output(supervisor_id)
        assert len(output.strip()) > 0, "Supervisor output should not be empty"

        output_lower = output.lower()
        # Should contain both analysis results and report structure
        analysis_keywords = ["mean", "median", "dataset", "analysis", "3.0"]
        matched = [kw for kw in analysis_keywords if kw in output_lower]
        assert matched, (
            f"Supervisor output should contain analysis content. "
            f"Expected at least one of {analysis_keywords}, got: {output[:300]}"
        )

    finally:
        if actual_session:
            terminals = _list_terminals_in_session(actual_session)
            for term in terminals:
                tid = term.get("id", "")
                if tid:
                    try:
                        requests.post(f"{API_BASE_URL}/terminals/{tid}/exit")
                    except Exception:
                        pass
            time.sleep(3)
            try:
                requests.delete(f"{API_BASE_URL}/sessions/{actual_session}")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Codex provider
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestCodexSupervisorOrchestration:
    """E2E supervisor orchestration tests for the Codex provider."""

    def test_supervisor_handoff(self, require_codex):
        """Supervisor uses handoff MCP tool to delegate to report_generator."""
        _run_supervisor_handoff_test(provider="codex")

    def test_supervisor_assign_and_handoff(self, require_codex):
        """Supervisor uses assign + handoff to orchestrate multi-agent workflow."""
        _run_supervisor_assign_test(provider="codex")


# ---------------------------------------------------------------------------
# Claude Code provider
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestClaudeCodeSupervisorOrchestration:
    """E2E supervisor orchestration tests for the Claude Code provider."""

    def test_supervisor_handoff(self, require_claude):
        """Supervisor uses handoff MCP tool to delegate to report_generator."""
        _run_supervisor_handoff_test(provider="claude_code")

    def test_supervisor_assign_and_handoff(self, require_claude):
        """Supervisor uses assign + handoff to orchestrate multi-agent workflow."""
        _run_supervisor_assign_test(provider="claude_code")


# ---------------------------------------------------------------------------
# Kiro CLI provider
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestKiroCliSupervisorOrchestration:
    """E2E supervisor orchestration tests for the Kiro CLI provider."""

    def test_supervisor_handoff(self, require_kiro):
        """Supervisor uses handoff MCP tool to delegate to report_generator."""
        _run_supervisor_handoff_test(provider="kiro_cli")

    def test_supervisor_assign_and_handoff(self, require_kiro):
        """Supervisor uses assign + handoff to orchestrate multi-agent workflow."""
        _run_supervisor_assign_test(provider="kiro_cli")


# ---------------------------------------------------------------------------
# Kimi CLI provider
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestKimiCliSupervisorOrchestration:
    """E2E supervisor orchestration tests for the Kimi CLI provider."""

    def test_supervisor_handoff(self, require_kimi):
        """Supervisor uses handoff MCP tool to delegate to report_generator."""
        _run_supervisor_handoff_test(provider="kimi_cli")

    def test_supervisor_assign_and_handoff(self, require_kimi):
        """Supervisor uses assign + handoff to orchestrate multi-agent workflow."""
        _run_supervisor_assign_test(provider="kimi_cli")


# ---------------------------------------------------------------------------
# Gemini CLI provider
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestGeminiCliSupervisorOrchestration:
    """E2E supervisor orchestration tests for the Gemini CLI provider."""

    def test_supervisor_handoff(self, require_gemini):
        """Supervisor uses handoff MCP tool to delegate to report_generator."""
        _run_supervisor_handoff_test(provider="gemini_cli")

    def test_supervisor_assign_and_handoff(self, require_gemini):
        """Supervisor uses assign + handoff to orchestrate multi-agent workflow."""
        _run_supervisor_assign_test(provider="gemini_cli")
