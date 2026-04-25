"""Tests for U10.3 — Session context extraction validation.

Covers:
- Claude Code extract_session_context returns structured dict from fixture output
- Extract returns empty dict for missing/empty output (no exception)
- Kimi provider raises NotImplementedError
"""

import asyncio
import re
from unittest.mock import patch

import pytest


def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Fixture: Claude Code terminal output
# ---------------------------------------------------------------------------

CLAUDE_CODE_FIXTURE = """\
\x1b[32m❯\x1b[0m Refactor the authentication module to use JWT tokens
⏺ I'll refactor the authentication module. The main changes are:

  I've decided to use PyJWT with RS256 signing for the token implementation.
  Updated src/auth/jwt_handler.py and src/auth/middleware.py.

  The approach is to replace session cookies with bearer tokens.

\x1b[32m❯\x1b[0m Can we add refresh token support?
⏺ I'll add refresh token support to the JWT implementation.

  Going to use a separate refresh token with longer expiry.
  Modified src/auth/jwt_handler.py and test/test_jwt.py.

\x1b[32m❯\x1b[0m\x20
"""


# ---------------------------------------------------------------------------
# U10.3 — Claude Code extract_session_context
# ---------------------------------------------------------------------------


class TestClaudeCodeExtractContext:
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_returns_structured_dict_from_fixture(self, mock_tmux):
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        mock_tmux.get_history.return_value = CLAUDE_CODE_FIXTURE

        provider = ClaudeCodeProvider("t1", "s1", "w1")
        result = run_async(provider.extract_session_context())

        # Must be a dict with required keys
        assert isinstance(result, dict)
        assert result["provider"] == "claude_code"
        assert result["terminal_id"] == "t1"

        # last_task should be the most recent user message
        assert "refresh token" in result["last_task"].lower()

        # key_decisions should have extracted decision patterns
        assert isinstance(result["key_decisions"], list)
        assert len(result["key_decisions"]) > 0

        # files_changed should contain paths from the output
        assert isinstance(result["files_changed"], list)
        assert any("jwt_handler" in f for f in result["files_changed"])

        # open_questions should contain the question
        assert isinstance(result["open_questions"], list)
        assert any("refresh token" in q.lower() for q in result["open_questions"])

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_returns_empty_dict_for_empty_output(self, mock_tmux):
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        mock_tmux.get_history.return_value = ""

        provider = ClaudeCodeProvider("t1", "s1", "w1")
        result = run_async(provider.extract_session_context())

        assert result == {}

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_returns_empty_dict_for_none_output(self, mock_tmux):
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        mock_tmux.get_history.return_value = None

        provider = ClaudeCodeProvider("t1", "s1", "w1")
        result = run_async(provider.extract_session_context())

        assert result == {}


# ---------------------------------------------------------------------------
# U10.3 — Kimi raises NotImplementedError
# ---------------------------------------------------------------------------


class TestKimiNotImplemented:
    @patch("cli_agent_orchestrator.providers.kimi_cli.tmux_client")
    def test_kimi_raises_not_implemented(self, mock_tmux):
        from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider

        provider = KimiCliProvider("t1", "s1", "w1")

        with pytest.raises(NotImplementedError, match="Phase 3"):
            run_async(provider.extract_session_context())


# ---------------------------------------------------------------------------
# U10.3 — All providers return consistent structure
# ---------------------------------------------------------------------------


class TestProviderContextConsistency:
    """Verify all extract_session_context implementations return consistent keys."""

    REQUIRED_KEYS = {"provider", "terminal_id", "last_task", "key_decisions", "open_questions", "files_changed"}

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_claude_code_has_all_keys(self, mock_tmux):
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        mock_tmux.get_history.return_value = CLAUDE_CODE_FIXTURE
        provider = ClaudeCodeProvider("t1", "s1", "w1")
        result = run_async(provider.extract_session_context())
        assert self.REQUIRED_KEYS.issubset(result.keys())

    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_gemini_has_all_keys(self, mock_tmux):
        from cli_agent_orchestrator.providers.gemini_cli import GeminiCliProvider

        mock_tmux.get_history.return_value = (
            "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
            "> Fix the parser\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            "Responding with gemini-2.5-pro\n"
            "✦ I'll fix the parser. Decided to use regex.\n"
            "✦ Modified src/parser.py.\n"
            "* Type your message\n"
        )
        provider = GeminiCliProvider("t1", "s1", "w1")
        result = run_async(provider.extract_session_context())
        assert self.REQUIRED_KEYS.issubset(result.keys())
