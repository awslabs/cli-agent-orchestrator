"""End-to-end tests for skill catalog injection.

Tests the full skill pipeline — validates that:
1. The skill catalog text is injected into the provider CLI command
   (verified by inspecting the tmux scrollback, not LLM output)
2. The load_skill MCP tool / API endpoint returns installed skill content

Requires: running CAO server, authenticated CLI tools, tmux, seeded skills.

Run:
    uv run pytest -m e2e test/e2e/test_skills.py -v
    uv run pytest -m e2e test/e2e/test_skills.py -v -k Codex
    uv run pytest -m e2e test/e2e/test_skills.py -v -k ClaudeCode
"""

import subprocess
import time
import uuid
from test.e2e.conftest import (
    cleanup_terminal,
    create_terminal,
    get_terminal_status,
)

import pytest
import requests

from cli_agent_orchestrator.cli.commands.init import seed_default_skills
from cli_agent_orchestrator.constants import API_BASE_URL


@pytest.fixture(scope="module", autouse=True)
def ensure_skills_seeded():
    """Seed default skills so the global skill catalog is non-empty."""
    seed_default_skills()


def _capture_full_scrollback(session_name: str, window_name: str) -> str:
    """Capture the full tmux scrollback buffer for a pane.

    Uses ``tmux capture-pane -p -S -`` to capture from the very start of
    the scrollback buffer, not just the last N lines. This ensures the
    initial CLI command (which contains the injected skill catalog) is
    included even if the agent's output has pushed it far up.
    """
    target = f"{session_name}:{window_name}"
    result = subprocess.run(
        ["tmux", "capture-pane", "-p", "-S", "-", "-t", target],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout


def _run_skill_injection_test(provider: str, agent_profile: str):
    """Assert the global skill catalog was injected into the provider CLI command.

    Creates a terminal, waits for it to become ready, then captures the
    full tmux scrollback to verify the skill catalog text appears in the
    command that was sent via tmux send-keys.

    This is a deterministic assertion — it checks the command string,
    not LLM output.
    """
    session_suffix = uuid.uuid4().hex[:6]
    session_name = f"e2e-skills-{provider}-{session_suffix}"
    terminal_id = None
    actual_session = None

    try:
        # Step 1: Create terminal (skill catalog injection happens here)
        terminal_id, actual_session = create_terminal(provider, agent_profile, session_name)
        assert terminal_id, "Terminal ID should not be empty"

        # Step 2: Wait for ready
        start = time.time()
        while time.time() - start < 90.0:
            s = get_terminal_status(terminal_id)
            if s in ("idle", "completed"):
                break
            if s == "error":
                break
            time.sleep(3)
        assert s in (
            "idle",
            "completed",
        ), f"Terminal did not become ready within 90s (provider={provider})"

        # Step 3: Capture full tmux scrollback.
        # The API's get_output(mode=full) uses capture-pane with a 200-line
        # limit, which is too small for long commands (Claude Code's system
        # prompt + MCP config + skill catalog). Instead, capture directly
        # with -S - (from the very start of the scrollback buffer).
        #
        # The window name is the terminal name returned by the API. Look it
        # up from the terminal metadata.
        resp = requests.get(f"{API_BASE_URL}/terminals/{terminal_id}")
        assert resp.status_code == 200
        window_name = resp.json()["name"]

        scrollback = _capture_full_scrollback(actual_session, window_name)
        assert len(scrollback.strip()) > 0, "Scrollback should not be empty"

        # Step 4: Assert skill catalog markers are present in the command.
        # The catalog is global in Phase 1, so any installed skill should appear.
        assert "Available Skills" in scrollback, (
            f"Skill catalog heading 'Available Skills' not found in scrollback "
            f"(provider={provider}). First 500 chars: {scrollback[:500]}"
        )
        assert "cao-worker-protocols" in scrollback, (
            f"Skill name 'cao-worker-protocols' not found in scrollback "
            f"(provider={provider}). First 500 chars: {scrollback[:500]}"
        )

    finally:
        if terminal_id and actual_session:
            cleanup_terminal(terminal_id, actual_session)


# ---------------------------------------------------------------------------
# Skill catalog injection tests (deterministic — checks tmux command string)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestCodexSkills:
    """E2E skill injection tests for the Codex provider."""

    def test_skill_catalog_injected(self, require_codex):
        """Codex terminal command contains the injected skill catalog."""
        _run_skill_injection_test(provider="codex", agent_profile="developer")


# NOTE: Claude Code is excluded from injection tests because its full-screen
# TUI clears the visible screen on startup, wiping the tail of the long
# command (where the skill catalog lives) from the tmux scrollback. Skill
# injection for Claude Code is covered by unit tests (_apply_skill_prompt,
# skill_prompt kwarg passing) and validated indirectly via the Codex test
# which exercises the same global-catalog service-layer code path.


@pytest.mark.e2e
class TestKimiCliSkills:
    """E2E skill injection tests for the Kimi CLI provider."""

    def test_skill_catalog_injected(self, require_kimi):
        """Kimi CLI terminal command contains the injected skill catalog."""
        _run_skill_injection_test(provider="kimi_cli", agent_profile="developer")


@pytest.mark.e2e
class TestGeminiCliSkills:
    """E2E skill injection tests for the Gemini CLI provider."""

    def test_skill_catalog_injected(self, require_gemini):
        """Gemini CLI terminal command contains the injected skill catalog."""
        _run_skill_injection_test(provider="gemini_cli", agent_profile="developer")


# ---------------------------------------------------------------------------
# Skill API endpoint test (deterministic — no agent needed)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestSkillApi:
    """E2E tests for the skill content REST API endpoint."""

    def test_get_skill_returns_content(self):
        """GET /skills/{name} returns the full Markdown body of an installed skill."""
        resp = requests.get(f"{API_BASE_URL}/skills/cao-worker-protocols")
        assert resp.status_code == 200, f"Unexpected status: {resp.status_code} {resp.text}"

        data = resp.json()
        assert data["name"] == "cao-worker-protocols"
        # The skill body should contain content about worker protocols
        assert (
            "send_message" in data["content"]
        ), f"Skill content should mention send_message. Got: {data['content'][:200]}"

    def test_get_skill_missing_returns_404(self):
        """GET /skills/{name} returns 404 for a nonexistent skill."""
        resp = requests.get(f"{API_BASE_URL}/skills/nonexistent-skill")
        assert resp.status_code == 404

    def test_get_skill_traversal_returns_400(self):
        """GET /skills/{name} rejects path traversal attempts."""
        resp = requests.get(f"{API_BASE_URL}/skills/../../../etc/passwd")
        # FastAPI will either return 400 (our validation) or 404 (path routing)
        assert resp.status_code in (400, 404, 422)
