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
    uv run pytest -m e2e test/e2e/test_supervisor_orchestration.py -v -o "addopts=" -k copilot
"""

import re
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
from cli_agent_orchestrator.utils.text import strip_terminal_escapes


def _get_full_output(terminal_id: str) -> str:
    """Get full terminal output (entire scrollback), escape-stripped.

    ``mode=full`` returns the raw StatusMonitor rolling buffer verbatim — for a
    TUI provider (kiro, codex) that buffer is dominated by cursor-movement /
    erase CSI sequences, so plain-text keyword checks must strip escapes first.
    We do NOT strip at the service layer because the web UI renders ``mode=full``
    as a live terminal and needs the escapes for colour/layout; stripping is the
    right thing only here, where the test greps for human-readable content.
    """
    resp = requests.get(
        f"{API_BASE_URL}/terminals/{terminal_id}/output",
        params={"mode": "full"},
    )
    if resp.status_code != 200:
        return ""
    return strip_terminal_escapes(resp.json().get("output", ""))


def _get_inbox_messages(terminal_id: str, status_filter: str = None):
    """Get inbox messages for a terminal."""
    params = {"limit": 50}
    if status_filter:
        params["status"] = status_filter
    resp = requests.get(
        f"{API_BASE_URL}/terminals/{terminal_id}/inbox/messages",
        params=params,
    )
    if resp.status_code != 200:
        return []
    return resp.json()


# Longer timeout for supervisor orchestration — the supervisor must:
# 1. Process the task
# 2. Call handoff/assign MCP tools (which create new terminals)
# 3. Wait for workers to initialize and complete
# 4. Collect results and produce final output
# 1200s: a kimi worker alone takes ~3m20s on the report-template handoff, and
# multi-step supervisor flows (assign + handoff, or supervisor + 2 workers with
# Opus 4.8) commonly need ~15 min end-to-end. Earlier 600s appeared sufficient
# while status detection reported a false early COMPLETED mid-turn — with
# honest detection the slowest provider needs more headroom for the real turn.
SUPERVISOR_COMPLETION_TIMEOUT = 1200


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
    that use an initial prompt reach 'completed' because the prompt produces
    a response. Both states indicate the provider is ready to accept input.
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


# A supervisor is "done" when it is in a ready state (COMPLETED or IDLE). Both
# are accepted because kiro 2.11 legitimately finishes a turn at IDLE with no
# Credits marker (the marker is intermittent in TUI mode; verified by a run
# where the supervisor produced a full combined report yet never emitted
# COMPLETED — a COMPLETED-only gate hung until timeout on it). But IDLE alone is
# ambiguous: a kiro supervisor is ALSO idle *between* firing an async assign and
# receiving the worker's callback, so a bare idle+workers check can declare
# "done" mid-orchestration and tear the session down before results are combined
# (the original hollow-pass regression). The status alone cannot tell the two
# idles apart — the mid-dispatch idle observed in one run lasted ~32s, longer
# than any stable-window guard. So for async assign flows the gate additionally
# requires the worker callback to have actually landed in the supervisor's inbox
# (see ``require_inbox_callback``): that delivery is impossible during the
# mid-dispatch idle (no worker has reported back yet) and is the assign
# protocol's real completion signal.
_SUPERVISOR_DONE_STATES = {"completed", "idle"}


def _wait_for_supervisor_done(
    supervisor_id: str,
    session_name: str,
    min_terminals: int,
    timeout: float = SUPERVISOR_COMPLETION_TIMEOUT,
    poll: float = 5.0,
    stable_count: int = 2,
    require_inbox_callback: bool = False,
    require_worker_completed: bool = False,
) -> tuple:
    """Wait for the supervisor to reach a stable ready state AND spawn workers.

    Some providers report a ready status after initial text output but before
    MCP tool calls (handoff/assign) finish creating worker terminals. A provider
    whose TUI keeps the idle prompt visible at all times can make the status
    detector see "response + idle prompt" = ready even while the model is
    between text output and the first MCP tool call.

    Similarly, Codex can briefly show ready between consecutive MCP tool calls
    (e.g., after assign returns but before handoff starts). Requiring
    ``stable_count`` consecutive ready readings avoids these false positives.

    "Done" means the supervisor has finished RENDERING its final output — not
    merely that it reached a ready status. kiro's IDLE is ambiguous (it is idle
    mid-dispatch, idle while awaiting a worker callback, and idle mid-repaint
    while presenting), so a status-only check races the presentation turn and
    reads a half-drawn TUI frame. The deterministic completion signal is OUTPUT
    QUIESCENCE: the ``mode=full`` buffer is byte-for-byte unchanged across
    ``stable_count`` consecutive polls. While the supervisor works, dispatches,
    or repaints (its spinner animates ~10 fps, text streams), the buffer keeps
    changing, so quiescence cannot hold mid-turn; once the report is fully
    rendered and the turn ends, it goes static. This removes the need to poll
    extraction with retries — by the time this returns, the output is complete.

    Because a genuine end-of-turn idle and an inter-turn idle can both be briefly
    quiescent, orchestration flows add a structural completion guard:
    - ``require_inbox_callback`` (async assign): a DELIVERED callback exists and
      nothing is still PENDING — the supervisor has received every worker result.
    - ``require_worker_completed`` (sync handoff): a seen worker has left the
      session (handoff auto-deletes its worker on completion).

    The ``handoff`` MCP tool auto-deletes its worker terminal as soon as the
    worker finishes. A point-in-time session listing taken after the supervisor
    reports ready can therefore miss workers that came and went during the run.
    To avoid this race we accumulate the union of every unique terminal id
    observed across all polls — once seen, a worker "counts" even if it has
    since been cleaned up.

    This function polls until ALL of these are true:
    - Terminal status is ready (idle/completed)
    - The output buffer is unchanged for ``stable_count`` consecutive polls
    - At least min_terminals unique terminals have been observed in the session
    - If ``require_inbox_callback``: a delivered callback exists, none pending
    - If ``require_worker_completed``: a seen worker has left the live session

    Returns (last_status, terminals_list) where terminals_list is the union
    of all unique terminals seen during polling.
    """
    start = time.time()
    last_status = "unknown"
    seen_terminals: dict = {}
    worker_completed = False
    consecutive_ready = 0
    prev_output = None

    while time.time() - start < timeout:
        last_status = get_terminal_status(supervisor_id)
        live_ids = set()
        for t in _list_terminals_in_session(session_name):
            tid = t.get("id")
            if tid:
                seen_terminals[tid] = t
                live_ids.add(tid)

        # A worker "completed" once a previously-seen worker (any non-supervisor
        # terminal) has left the live session — handoff auto-deletes its worker
        # on completion. Latch True; stays true afterward even though the union
        # in seen_terminals keeps the worker for the min_terminals count.
        seen_workers = set(seen_terminals) - {supervisor_id}
        if seen_workers and not seen_workers & live_ids:
            worker_completed = True

        if last_status == "error":
            return last_status, list(seen_terminals.values())

        # "Done" = the supervisor has finished RENDERING its final output, not
        # merely reached a ready status. kiro's IDLE is ambiguous (idle mid-
        # dispatch, idle awaiting a callback, idle mid-presentation-repaint), so
        # status alone races the presentation turn and reads a half-drawn frame.
        # The deterministic signal is OUTPUT QUIESCENCE: while the supervisor is
        # working, dispatching, or repainting its TUI, the output buffer keeps
        # changing; once the report is fully rendered and the turn ends, it goes
        # static. We require a ready status AND the output byte-for-byte
        # unchanged across stable_count consecutive polls — that quiescence
        # cannot hold mid-turn (spinner animates, text streams).
        current_output = _get_full_output(supervisor_id)
        output_quiesced = current_output == prev_output and bool(current_output.strip())
        prev_output = current_output

        ready = (
            last_status in _SUPERVISOR_DONE_STATES
            and len(seen_terminals) >= min_terminals
            and output_quiesced
        )
        if ready and require_inbox_callback:
            # assign is async: the analyst reports back via send_message. Require
            # a DELIVERED callback with nothing still PENDING — i.e. the
            # supervisor has actually received every worker result, not that a
            # worker merely queued one. This rejects the idle-awaiting-delivery
            # window (a pending-but-undelivered message) as well as the mid-
            # dispatch idle (no message at all).
            delivered = _get_inbox_messages(supervisor_id, status_filter="delivered")
            pending = _get_inbox_messages(supervisor_id, status_filter="pending")
            ready = bool(delivered) and not pending
        if ready and require_worker_completed:
            # handoff is synchronous with no inbox callback: the worker result
            # returns inline. Require the worker to have finished (left the
            # session) so we don't accept the mid-handoff idle.
            ready = worker_completed

        if ready:
            consecutive_ready += 1
            if consecutive_ready >= stable_count:
                return last_status, list(seen_terminals.values())
        else:
            consecutive_ready = 0

        time.sleep(poll)

    return last_status, list(seen_terminals.values())


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
        # Providers with initial prompts reach 'completed' after processing
        # the system prompt; others reach 'idle'.
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

        # Step 4+5: Wait for supervisor to finish rendering its combined output
        # (output-quiescence gate) AND create the worker terminal.
        # Handoff is synchronous — the worker result returns inline to the
        # supervisor's turn, there is no async inbox callback — so the structural
        # guard is require_worker_completed: the worker must have finished and
        # left the session before we accept the supervisor's idle. Combined with
        # output quiescence, this ensures the report is fully rendered by the
        # time this returns, so extract_output below reads a settled frame.
        status, terminals = _wait_for_supervisor_done(
            supervisor_id, actual_session, min_terminals=2, require_worker_completed=True
        )
        assert status in _SUPERVISOR_DONE_STATES, (
            f"Supervisor did not reach a ready state (idle/completed) within "
            f"{SUPERVISOR_COMPLETION_TIMEOUT}s (provider={provider}). Last status: {status}"
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
        # Providers with initial prompts reach 'completed' after processing
        # the system prompt; others reach 'idle'.
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
        # Uses combined polling because some providers report COMPLETED from
        # initial text output before MCP tool calls finish.
        # assign is async — the data_analyst reports back via send_message to the
        # supervisor's inbox. Require that callback before accepting idle so we
        # don't grab the mid-dispatch idle (which precedes any callback).
        status, terminals = _wait_for_supervisor_done(
            supervisor_id, actual_session, min_terminals=3, require_inbox_callback=True
        )
        assert status in _SUPERVISOR_DONE_STATES, (
            f"Supervisor did not reach a ready state (idle/completed) within "
            f"{SUPERVISOR_COMPLETION_TIMEOUT}s (provider={provider}). Last status: {status}"
        )
        assert len(terminals) >= 3, (
            f"Expected at least 3 terminals (supervisor + data_analyst + report_generator), "
            f"got {len(terminals)}. The supervisor may not have called assign/handoff tools."
        )

        # Step 6: Wait for inbox delivery and supervisor reprocessing.
        # The assign flow creates a worker that calls send_message() to the
        # supervisor's inbox.  The inbox service delivers the message when the
        # supervisor becomes idle, which causes the supervisor to start
        # processing again (showing "Working...").  We must wait for the
        # supervisor to finish processing the inbox message before extracting
        # output, otherwise we get the spinner text instead of the report.
        inbox_verified = False
        for _ in range(12):  # up to 60s
            delivered = _get_inbox_messages(supervisor_id, status_filter="delivered")
            if delivered:
                inbox_verified = True
                break
            pending = _get_inbox_messages(supervisor_id, status_filter="pending")
            if not pending:
                # No pending and no delivered — workers may have used handoff
                # instead of assign+send_message, which is acceptable
                inbox_verified = True
                break
            time.sleep(5)

        # If there ARE pending messages that were never delivered, that's the bug
        still_pending = _get_inbox_messages(supervisor_id, status_filter="pending")
        assert not still_pending, (
            f"Inbox messages stuck as PENDING — inbox delivery pipeline broken! "
            f"Pending messages: {[(m.get('sender_id', '?'), m.get('message', '')[:80]) for m in still_pending]}"
        )

        # Step 7: Wait for supervisor to re-stabilize after processing inbox
        # messages.  The delivered message may have triggered reprocessing.
        for _ in range(24):  # up to 120s
            s = get_terminal_status(supervisor_id)
            if s in ("completed", "idle"):
                break
            time.sleep(5)

        # Step 8: Validate supervisor output.
        # Use FULL output (entire scrollback) because the analysis keywords
        # may appear in earlier messages — the supervisor's last message may
        # be a summary that doesn't repeat the raw stats, or the supervisor
        # may have received an inbox message that triggered further processing.
        output = _get_full_output(supervisor_id)
        assert len(output.strip()) > 0, "Supervisor output should not be empty"

        output_lower = output.lower()
        # Should contain both analysis results and report structure
        analysis_keywords = ["mean", "median", "dataset", "analysis", "3.0"]
        matched = [kw for kw in analysis_keywords if kw in output_lower]
        assert matched, (
            f"Supervisor output should contain analysis content. "
            f"Expected at least one of {analysis_keywords}, got: {output[:500]}"
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


def _run_supervisor_assign_three_analysts_test(provider: str):
    """Test supervisor assigns A/B/C analysts, receives callbacks, and finalizes report."""
    session_suffix = uuid.uuid4().hex[:6]
    session_name = f"e2e-super-3a-{provider}-{session_suffix}"
    supervisor_id = None
    actual_session = None

    try:
        supervisor_id, actual_session = create_terminal(
            provider, "analysis_supervisor", session_name
        )
        assert supervisor_id, "Supervisor terminal ID should not be empty"

        assert _wait_for_ready(
            supervisor_id, timeout=120.0
        ), f"Supervisor did not become ready within 120s (provider={provider})"
        time.sleep(2)

        task_message = (
            "Run the full workflow exactly.\n"
            "Dataset A: [1, 2, 3, 4, 5]\n"
            "Dataset B: [10, 20, 30, 40, 50]\n"
            "Dataset C: [2, 2, 3, 3, 4]\n"
            "Use assign to dispatch one data_analyst per dataset (A, B, C), "
            "then use handoff to report_generator for the template, then combine "
            "all three analyst results into the final report."
        )
        resp = requests.post(
            f"{API_BASE_URL}/terminals/{supervisor_id}/input",
            params={"message": task_message},
        )
        assert resp.status_code == 200, f"Send message failed: {resp.status_code}"

        # Expected minimum: supervisor + 3 analysts + report generator
        status, terminals = _wait_for_supervisor_done(
            supervisor_id,
            actual_session,
            min_terminals=5,
            timeout=420,
            require_inbox_callback=True,
        )
        assert status in _SUPERVISOR_DONE_STATES, (
            f"Supervisor did not reach a ready state (idle/completed) within timeout "
            f"(provider={provider}). Last status: {status}"
        )
        assert len(terminals) >= 5, (
            "Expected at least 5 terminals " "(supervisor + analyst A/B/C + report_generator)"
        )

        # Verify 3 analyst callbacks were delivered to supervisor inbox.
        delivered_messages = []
        for _ in range(36):  # up to 180s
            delivered_messages = _get_inbox_messages(supervisor_id, status_filter="delivered")
            unique_senders = {m.get("sender_id") for m in delivered_messages if m.get("sender_id")}
            if len(unique_senders) >= 3:
                break
            time.sleep(5)

        unique_senders = {m.get("sender_id") for m in delivered_messages if m.get("sender_id")}
        assert len(unique_senders) >= 3, (
            "Expected delivered callbacks from at least 3 distinct worker terminals. "
            f"Got {len(unique_senders)} senders: {sorted(unique_senders)}"
        )

        # Ensure final output reflects combined multi-dataset report.
        # After callbacks are delivered, the supervisor may need extra time to
        # synthesize and emit a final narrative response.
        output = ""
        cleaned = ""
        for _ in range(24):  # up to 120s
            candidate = extract_output(supervisor_id)
            if not candidate.strip():
                candidate = _get_full_output(supervisor_id)

            if candidate.strip():
                candidate_cleaned = re.sub(r"\x1b\[[0-9;]*m", "", candidate).lower()
                has_report = bool(re.search(r"\b(summary|report)\b", candidate_cleaned))
                has_synthesis = bool(
                    re.search(
                        r"\b(conclusions?|recommendations?|overall|synthesis|final)\b",
                        candidate_cleaned,
                    )
                )
                output = candidate
                cleaned = candidate_cleaned
                if has_report and has_synthesis:
                    break
            time.sleep(5)

        assert len(output.strip()) > 0, "Supervisor output should not be empty"
        assert re.search(
            r"\b(summary|report)\b", cleaned
        ), "Expected report-style summary content in final output"
        assert re.search(
            r"\b(conclusions?|recommendations?|overall|synthesis|final)\b", cleaned
        ), f"Expected final synthesis/conclusion content in final report output. Got: {output[-500:]}"

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
# Copilot CLI provider
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestCopilotCliSupervisorOrchestration:
    """E2E supervisor orchestration tests for the Copilot CLI provider."""

    def test_supervisor_handoff(self, require_copilot):
        """Supervisor uses handoff MCP tool to delegate to report_generator."""
        _run_supervisor_handoff_test(provider="copilot_cli")

    def test_supervisor_assign_and_handoff(self, require_copilot):
        """Supervisor uses assign + handoff to orchestrate multi-agent workflow."""
        _run_supervisor_assign_test(provider="copilot_cli")

    def test_supervisor_assign_three_analysts(self, require_copilot):
        """Supervisor assigns A/B/C analysts, receives callbacks, and finalizes report."""
        _run_supervisor_assign_three_analysts_test(provider="copilot_cli")


# ---------------------------------------------------------------------------
# Cursor CLI provider
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestCursorCliSupervisorOrchestration:
    """E2E supervisor orchestration tests for the Cursor CLI provider.

    Validates that a Cursor CLI supervisor agent can autonomously drive
    the assign + handoff + send_message flow via the cao-mcp-server
    tools — the canonical multi-agent e2e test from the
    ``examples/assign/`` scenario.

    Requires the ``agent`` (or legacy ``cursor-agent``) binary on PATH
    and the agent profiles installed for cursor_cli::

        cao install examples/assign/analysis_supervisor.md --provider cursor_cli
        cao install examples/assign/data_analyst.md --provider cursor_cli
        cao install examples/assign/report_generator.md --provider cursor_cli
    """

    def test_supervisor_handoff(self, require_cursor):
        """Cursor CLI supervisor uses handoff MCP tool to delegate to report_generator."""
        _run_supervisor_handoff_test(provider="cursor_cli")

    def test_supervisor_assign_and_handoff(self, require_cursor):
        """Cursor CLI supervisor uses assign + handoff to orchestrate multi-agent workflow."""
        _run_supervisor_assign_test(provider="cursor_cli")

    def test_supervisor_assign_three_analysts(self, require_cursor):
        """Cursor CLI supervisor assigns 3 analysts, receives callbacks, finalizes report.

        The canonical ``examples/assign/`` smoke test: parallel assign
        of three data analysts, sequential handoff to report generator,
        inbox delivery of worker results, supervisor final assembly
        without doing the analysis work itself.
        """
        _run_supervisor_assign_three_analysts_test(provider="cursor_cli")


# ---------------------------------------------------------------------------
# Antigravity CLI provider
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestAntigravityCliSupervisorOrchestration:
    """E2E supervisor orchestration tests for the Antigravity CLI provider."""

    def test_supervisor_handoff(self, require_antigravity):
        """Supervisor uses handoff MCP tool to delegate to report_generator."""
        _run_supervisor_handoff_test(provider="antigravity_cli")

    def test_supervisor_assign_and_handoff(self, require_antigravity):
        """Supervisor uses assign + handoff to orchestrate multi-agent workflow."""
        _run_supervisor_assign_test(provider="antigravity_cli")
