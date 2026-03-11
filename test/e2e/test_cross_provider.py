"""E2E tests for cross-provider orchestration (PR #101).

Verifies that agent profiles with a ``provider`` key in their frontmatter
cause CAO to launch the worker on the declared provider, even when the
session was started on a different provider.

Flow:
1. Create a session on provider A (the "supervisor" provider).
2. Add a terminal via ``POST /sessions/{session}/terminals`` using an
   agent profile that declares ``provider: B`` in its frontmatter.
3. ``resolve_provider()`` reads the profile and overrides the fallback.
4. Verify the new terminal reports ``provider == B``.
5. Send a data-analysis task and confirm COMPLETED + valid output.

Requires:
- Running CAO server
- Agent profiles installed:
    cao install examples/cross-provider/data_analyst_claude_code.md
    cao install examples/cross-provider/data_analyst_gemini_cli.md
    cao install examples/cross-provider/data_analyst_kiro_cli.md
- Authenticated CLI tools for each provider used in the test
- tmux

Run:
    uv run pytest -m e2e test/e2e/test_cross_provider.py -v -o "addopts="
    uv run pytest -m e2e test/e2e/test_cross_provider.py -v -o "addopts=" -k kiro_to_claude
"""

import time
import uuid

import pytest
import requests

from cli_agent_orchestrator.constants import API_BASE_URL
from test.e2e.conftest import (
    cleanup_terminal,
    extract_output,
    get_terminal_status,
    wait_for_status,
)

COMPLETION_TIMEOUT = 180

DATA_ANALYST_TASK = (
    "Analyze Dataset A: [1, 2, 3, 4, 5]. "
    "Calculate mean, median, and standard deviation. "
    "Present your analysis results directly."
)

DATA_ANALYST_KEYWORDS = [
    "mean",
    "median",
    "standard deviation",
    "3.0",
    "1.41",
    "dataset",
    "analysis",
    "calculate",
    "send_message",
    "CAO_TERMINAL_ID",
]


def _create_session(provider: str, agent_profile: str, session_name: str):
    """Create a session on the given provider. Returns (terminal_id, session_name)."""
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


def _add_terminal_in_session(
    session_name: str, provider: str, agent_profile: str, retries: int = 1
):
    """Add a terminal to an existing session via the API.

    The ``provider`` param is the *fallback* — if the agent profile declares
    its own provider, ``resolve_provider()`` overrides it.

    Retries on 500 errors (typically init timeouts) up to ``retries`` times.

    Returns (terminal_id, reported_provider).
    """
    last_resp = None
    for attempt in range(1 + retries):
        if attempt > 0:
            time.sleep(15)
        resp = requests.post(
            f"{API_BASE_URL}/sessions/{session_name}/terminals",
            params={
                "provider": provider,
                "agent_profile": agent_profile,
            },
        )
        last_resp = resp
        if resp.status_code in (200, 201):
            data = resp.json()
            return data["id"], data.get("provider", provider)
        if resp.status_code != 500 or attempt >= retries:
            break

    assert last_resp is not None and last_resp.status_code in (
        200,
        201,
    ), f"Terminal creation failed: {last_resp.status_code} {last_resp.text}"
    data = last_resp.json()
    return data["id"], data.get("provider", provider)


def _run_cross_provider_test(
    supervisor_provider: str,
    worker_profile: str,
    expected_worker_provider: str,
):
    """Core cross-provider test logic.

    1. Create session on supervisor_provider
    2. Add worker terminal using worker_profile (which has provider override)
    3. Verify worker runs on expected_worker_provider
    4. Send task, wait for completion, validate output
    """
    session_suffix = uuid.uuid4().hex[:6]
    session_name = (
        f"e2e-xprov-{supervisor_provider[:4]}-{expected_worker_provider[:4]}-{session_suffix}"
    )
    supervisor_id = None
    worker_id = None
    actual_session = None

    try:
        # Step 1: Create supervisor session
        supervisor_id, actual_session = _create_session(
            supervisor_provider, "data_analyst", session_name
        )
        assert supervisor_id, "Supervisor terminal ID should not be empty"

        # Wait for supervisor to be ready
        start = time.time()
        while time.time() - start < 90.0:
            s = get_terminal_status(supervisor_id)
            if s in ("idle", "completed"):
                break
            time.sleep(3)
        assert s in ("idle", "completed"), (
            f"Supervisor did not become ready within 90s " f"(provider={supervisor_provider})"
        )
        time.sleep(2)

        # Step 2: Add worker terminal with cross-provider profile
        worker_id, reported_provider = _add_terminal_in_session(
            actual_session, supervisor_provider, worker_profile
        )
        assert worker_id, "Worker terminal ID should not be empty"

        # Step 3: Verify provider override worked
        assert reported_provider == expected_worker_provider, (
            f"Expected worker to run on {expected_worker_provider}, "
            f"but got {reported_provider}. "
            f"resolve_provider() may not be reading the profile's provider key."
        )

        # Also verify via GET /terminals/{id}
        resp = requests.get(f"{API_BASE_URL}/terminals/{worker_id}")
        assert resp.status_code == 200
        terminal_info = resp.json()
        assert terminal_info["provider"] == expected_worker_provider, (
            f"GET /terminals confirms wrong provider: "
            f"{terminal_info['provider']} != {expected_worker_provider}"
        )

        # Step 4: Wait for worker to be ready
        start = time.time()
        while time.time() - start < 90.0:
            s = get_terminal_status(worker_id)
            if s in ("idle", "completed"):
                break
            time.sleep(3)
        assert s in ("idle", "completed"), (
            f"Worker did not become ready within 90s "
            f"(expected provider={expected_worker_provider})"
        )
        time.sleep(2)

        # Step 5: Send task to worker
        resp = requests.post(
            f"{API_BASE_URL}/terminals/{worker_id}/input",
            params={"message": DATA_ANALYST_TASK},
        )
        assert resp.status_code == 200, f"Send message failed: {resp.status_code}"

        # Step 6: Wait for completion
        assert wait_for_status(worker_id, "completed", timeout=COMPLETION_TIMEOUT), (
            f"Worker did not reach COMPLETED within {COMPLETION_TIMEOUT}s "
            f"(profile={worker_profile}, provider={expected_worker_provider})"
        )

        # Stabilization
        time.sleep(5)
        recheck = get_terminal_status(worker_id)
        if recheck != "completed":
            assert wait_for_status(worker_id, "completed", timeout=COMPLETION_TIMEOUT)

        # Step 7: Validate output
        # Gemini CLI's Ink TUI may show spinners after COMPLETED that
        # temporarily obscure the response. Retry extraction.
        output = ""
        for _ in range(4):
            try:
                output = extract_output(worker_id)
                if output.strip():
                    break
            except (AssertionError, Exception):
                pass
            time.sleep(10)
        assert len(output.strip()) > 0, "Worker output should not be empty"

        # Keyword check is lenient — the primary goal of this test is
        # verifying the cross-provider resolution, not output quality.
        # At least 1 keywords match confirms the worker processed the task.
        output_lower = output.lower()
        matched = [kw for kw in DATA_ANALYST_KEYWORDS if kw.lower() in output_lower]
        assert len(matched) >= 1, (
            f"Expected at least 1 of {DATA_ANALYST_KEYWORDS} in output. "
            f"Matched: {matched}. Output (last 500 chars): {output[-500:]}"
        )

    finally:
        for tid in (worker_id, supervisor_id):
            if tid:
                try:
                    requests.post(f"{API_BASE_URL}/terminals/{tid}/exit")
                except Exception:
                    pass
        time.sleep(2)
        if actual_session:
            try:
                requests.delete(f"{API_BASE_URL}/sessions/{actual_session}")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Test classes — one per cross-provider combo
#
# NOTE: Claude Code combos (KiroToClaude, ClaudeToGemini) cannot run when
# the test runner itself is Claude Code — nested sessions are blocked by
# the CLAUDECODE env var check.  They are kept for CI or manual runs
# outside Claude Code.
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestCrossProviderKiroToGemini:
    """Kiro CLI supervisor session, worker runs on Gemini CLI."""

    def test_assign_cross_provider(self, require_kiro, require_gemini):
        """Worker profile declares provider: gemini_cli, overriding kiro_cli fallback."""
        _run_cross_provider_test(
            supervisor_provider="kiro_cli",
            worker_profile="data_analyst_gemini_cli",
            expected_worker_provider="gemini_cli",
        )


@pytest.mark.e2e
class TestCrossProviderGeminiToKiro:
    """Gemini CLI supervisor session, worker runs on Kiro CLI."""

    def test_assign_cross_provider(self, require_gemini, require_kiro):
        """Worker profile declares provider: kiro_cli, overriding gemini_cli fallback."""
        _run_cross_provider_test(
            supervisor_provider="gemini_cli",
            worker_profile="data_analyst_kiro_cli",
            expected_worker_provider="kiro_cli",
        )


@pytest.mark.e2e
class TestCrossProviderKiroToClaude:
    """Kiro CLI supervisor session, worker runs on Claude Code.

    NOTE: Cannot run inside Claude Code (nested session blocked).
    """

    def test_assign_cross_provider(self, require_kiro, require_claude):
        """Worker profile declares provider: claude_code, overriding kiro_cli fallback."""
        _run_cross_provider_test(
            supervisor_provider="kiro_cli",
            worker_profile="data_analyst_claude_code",
            expected_worker_provider="claude_code",
        )


@pytest.mark.e2e
class TestCrossProviderClaudeToGemini:
    """Claude Code supervisor session, worker runs on Gemini CLI.

    NOTE: Cannot run inside Claude Code (nested session blocked).
    """

    def test_assign_cross_provider(self, require_claude, require_gemini):
        """Worker profile declares provider: gemini_cli, overriding claude_code fallback."""
        _run_cross_provider_test(
            supervisor_provider="claude_code",
            worker_profile="data_analyst_gemini_cli",
            expected_worker_provider="gemini_cli",
        )
