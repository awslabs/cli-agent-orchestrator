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

import os
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

    Creates a terminal, waits for it to become ready, then verifies the
    skill catalog text reached the provider.

    For most providers the catalog is embedded in the CLI command string
    sent via tmux send-keys and is therefore visible in tmux scrollback.
    Gemini CLI is an exception: its system prompt is written to
    ``GEMINI.md`` in the working directory because passing the catalog via
    ``-i`` causes Gemini to treat it as a task to execute. For Gemini we
    assert against the on-disk ``GEMINI.md`` instead.

    This is a deterministic assertion — it checks the injected payload,
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

        # Step 3: Assert skill catalog markers are present in the injection payload.
        # The catalog is global in Phase 1, so any installed skill should appear.
        if provider == "gemini_cli":
            # Gemini writes the full system prompt (including the skill catalog)
            # to GEMINI.md in the working directory rather than embedding it in
            # the CLI command — the command carries only a short role-acknowledge
            # prompt via -i. Read GEMINI.md directly.
            payload = _read_gemini_md()
            source = "GEMINI.md"
        else:
            # Capture the full tmux scrollback (capture-pane -S - reads from the
            # very start of the buffer so the initial CLI command with the
            # injected catalog is included).
            resp = requests.get(f"{API_BASE_URL}/terminals/{terminal_id}")
            assert resp.status_code == 200
            window_name = resp.json()["name"]
            payload = _capture_full_scrollback(actual_session, window_name)
            source = "tmux scrollback"

        assert len(payload.strip()) > 0, f"{source} should not be empty"

        assert "Available Skills" in payload, (
            f"Skill catalog heading 'Available Skills' not found in {source} "
            f"(provider={provider}). First 500 chars: {payload[:500]}"
        )
        assert "cao-worker-protocols" in payload, (
            f"Skill name 'cao-worker-protocols' not found in {source} "
            f"(provider={provider}). First 500 chars: {payload[:500]}"
        )

    finally:
        if terminal_id and actual_session:
            cleanup_terminal(terminal_id, actual_session)


def _read_gemini_md() -> str:
    """Read GEMINI.md from the current working directory.

    The Gemini CLI provider writes its system prompt (including the injected
    skill catalog) to ``GEMINI.md`` in the pane's working directory, which is
    the same directory the test runs from. Returns empty string if the file
    does not exist so the caller can produce a useful assertion error.
    """
    path = os.path.join(os.getcwd(), "GEMINI.md")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


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
