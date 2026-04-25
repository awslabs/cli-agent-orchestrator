"""Tests for U4 — extract_session_context() across providers.

Covers:
- U4.1: Abstract method defined in BaseProvider
- U4.2: Claude Code implementation
- U4.3: Gemini CLI implementation
- U4.4: Kiro CLI implementation
- U4.5: Codex CLI implementation
- U4.6: Copilot CLI implementation
- Kimi stub raises NotImplementedError
- Q CLI implementation
- Helper methods: _extract_decisions, _extract_questions, _extract_file_paths
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest


def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Fixtures: Terminal output samples per provider
# ---------------------------------------------------------------------------

CLAUDE_CODE_OUTPUT = """\
\x1b[32m❯\x1b[0m Fix the login bug in auth.py
⏺ I'll fix the login bug. The issue is in the password hashing function.

  I've decided to use bcrypt instead of md5 for password hashing.
  Let me update src/auth.py and test/test_auth.py.

  The approach is to replace the hash_password() function.

\x1b[32m❯\x1b[0m Should we add rate limiting?
⏺ I'll add rate limiting to the login endpoint.

  Going to use a token bucket algorithm.
  Updated src/middleware/rate_limit.py with the new implementation.

\x1b[32m❯\x1b[0m\x20
"""

GEMINI_OUTPUT = """\
▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
> Refactor the database module
▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄
Responding with gemini-2.5-pro
✦ I'll refactor the database module to use connection pooling.
✦ The decision is to use SQLAlchemy's connection pool with pool_size=5.
✦ Modified src/database/pool.py and src/database/models.py.
* Type your message
"""

KIRO_OUTPUT = """\
\x1b[38;5;33m[developer]\x1b[0m\x1b[38;5;33m>\x1b[0m Write unit tests for the parser
> I'll write comprehensive unit tests for the parser module.

  I decided to use pytest fixtures for test setup.
  Files changed: test/test_parser.py, test/conftest.py

\x1b[38;5;33m[developer]\x1b[0m\x1b[38;5;33m>\x1b[0m\x20
"""

CODEX_OUTPUT = """\
› Implement the search feature
• I'll implement full-text search using SQLite FTS5.

  I chose to use the rank function for relevance scoring.
  Let's create src/search/engine.py for the search logic.

❯\x20
"""

COPILOT_OUTPUT = """\
❯ Debug the memory leak in the worker process
assistant: I'll investigate the memory leak. The issue is in the event loop.

  I decided to use weakrefs for the callback registry.
  Updated src/worker/event_loop.py to fix the leak.

❯\x20
"""

Q_CLI_OUTPUT = """\
\x1b[38;5;13m[\x1b[0m\x1b[38;5;13manalyst\x1b[0m\x1b[38;5;13m]\x1b[0m\x1b[38;5;13m>\x1b[0m Analyze the log files
> I'll analyze the log files for error patterns.

  I've decided to group errors by HTTP status code.
  The plan is to generate a summary report.

\x1b[38;5;13m[\x1b[0m\x1b[38;5;13manalyst\x1b[0m\x1b[38;5;13m]\x1b[0m\x1b[38;5;13m>\x1b[0m\x20
"""


# ---------------------------------------------------------------------------
# U4.1 — Abstract method in BaseProvider
# ---------------------------------------------------------------------------


class TestBaseProviderAbstractMethod:
    def test_abstract_method_exists(self):
        from cli_agent_orchestrator.providers.base import BaseProvider

        assert hasattr(BaseProvider, "extract_session_context")

    def test_cannot_instantiate_base_directly(self):
        from cli_agent_orchestrator.providers.base import BaseProvider

        with pytest.raises(TypeError, match="abstract"):
            BaseProvider("t1", "s1", "w1")


# ---------------------------------------------------------------------------
# U4.2 — Claude Code
# ---------------------------------------------------------------------------


class TestClaudeCodeExtractSessionContext:
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_extracts_context_from_output(self, mock_tmux):
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        mock_tmux.get_history.return_value = CLAUDE_CODE_OUTPUT
        provider = ClaudeCodeProvider("t1", "s1", "w1")

        result = run_async(provider.extract_session_context())

        assert result["provider"] == "claude_code"
        assert result["terminal_id"] == "t1"
        assert "rate limiting" in result["last_task"].lower()
        assert len(result["key_decisions"]) > 0
        assert isinstance(result["files_changed"], list)

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_returns_empty_dict_on_no_output(self, mock_tmux):
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        mock_tmux.get_history.return_value = ""
        provider = ClaudeCodeProvider("t1", "s1", "w1")

        result = run_async(provider.extract_session_context())
        assert result == {}

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_extracts_open_questions(self, mock_tmux):
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        mock_tmux.get_history.return_value = CLAUDE_CODE_OUTPUT
        provider = ClaudeCodeProvider("t1", "s1", "w1")

        result = run_async(provider.extract_session_context())
        questions = result["open_questions"]
        assert any("rate limiting" in q.lower() for q in questions)

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_extracts_file_paths(self, mock_tmux):
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        mock_tmux.get_history.return_value = CLAUDE_CODE_OUTPUT
        provider = ClaudeCodeProvider("t1", "s1", "w1")

        result = run_async(provider.extract_session_context())
        files = result["files_changed"]
        assert any("auth.py" in f for f in files)


# ---------------------------------------------------------------------------
# U4.3 — Gemini CLI
# ---------------------------------------------------------------------------


class TestGeminiCliExtractSessionContext:
    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_extracts_context_from_output(self, mock_tmux):
        from cli_agent_orchestrator.providers.gemini_cli import GeminiCliProvider

        mock_tmux.get_history.return_value = GEMINI_OUTPUT
        provider = GeminiCliProvider("t1", "s1", "w1")

        result = run_async(provider.extract_session_context())

        assert result["provider"] == "gemini_cli"
        assert "database" in result["last_task"].lower()
        assert len(result["key_decisions"]) > 0

    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    def test_returns_empty_dict_on_no_output(self, mock_tmux):
        from cli_agent_orchestrator.providers.gemini_cli import GeminiCliProvider

        mock_tmux.get_history.return_value = None
        provider = GeminiCliProvider("t1", "s1", "w1")

        result = run_async(provider.extract_session_context())
        assert result == {}


# ---------------------------------------------------------------------------
# U4.4 — Kiro CLI
# ---------------------------------------------------------------------------


class TestKiroCliExtractSessionContext:
    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_extracts_context_from_output(self, mock_tmux):
        from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider

        mock_tmux.get_history.return_value = KIRO_OUTPUT
        provider = KiroCliProvider("t1", "s1", "w1", agent_profile="developer")

        result = run_async(provider.extract_session_context())

        assert result["provider"] == "kiro_cli"
        assert (
            "unit tests" in result["last_task"].lower() or "parser" in result["last_task"].lower()
        )

    @patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")
    def test_returns_empty_dict_on_no_output(self, mock_tmux):
        from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider

        mock_tmux.get_history.return_value = ""
        provider = KiroCliProvider("t1", "s1", "w1", agent_profile="developer")

        result = run_async(provider.extract_session_context())
        assert result == {}


# ---------------------------------------------------------------------------
# U4.5 — Codex CLI
# ---------------------------------------------------------------------------


class TestCodexExtractSessionContext:
    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    def test_extracts_context_from_output(self, mock_tmux):
        from cli_agent_orchestrator.providers.codex import CodexProvider

        mock_tmux.get_history.return_value = CODEX_OUTPUT
        provider = CodexProvider("t1", "s1", "w1")

        result = run_async(provider.extract_session_context())

        assert result["provider"] == "codex"
        assert "search" in result["last_task"].lower()

    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    def test_returns_empty_dict_on_no_output(self, mock_tmux):
        from cli_agent_orchestrator.providers.codex import CodexProvider

        mock_tmux.get_history.return_value = ""
        provider = CodexProvider("t1", "s1", "w1")

        result = run_async(provider.extract_session_context())
        assert result == {}


# ---------------------------------------------------------------------------
# U4.6 — Copilot CLI
# ---------------------------------------------------------------------------


class TestCopilotCliExtractSessionContext:
    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_extracts_context_from_output(self, mock_tmux):
        from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider

        mock_tmux.get_history.return_value = COPILOT_OUTPUT
        provider = CopilotCliProvider("t1", "s1", "w1")

        result = run_async(provider.extract_session_context())

        assert result["provider"] == "copilot_cli"
        assert "memory leak" in result["last_task"].lower()

    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_returns_empty_dict_on_no_output(self, mock_tmux):
        from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider

        mock_tmux.get_history.return_value = ""
        provider = CopilotCliProvider("t1", "s1", "w1")

        result = run_async(provider.extract_session_context())
        assert result == {}


# ---------------------------------------------------------------------------
# Kimi stub
# ---------------------------------------------------------------------------


class TestKimiExtractSessionContextStub:
    @patch("cli_agent_orchestrator.providers.kimi_cli.tmux_client")
    def test_raises_not_implemented(self, mock_tmux):
        from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider

        provider = KimiCliProvider("t1", "s1", "w1")

        with pytest.raises(NotImplementedError, match="Phase 3"):
            run_async(provider.extract_session_context())


# ---------------------------------------------------------------------------
# Q CLI
# ---------------------------------------------------------------------------


class TestQCliExtractSessionContext:
    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_extracts_context_from_output(self, mock_tmux):
        from cli_agent_orchestrator.providers.q_cli import QCliProvider

        mock_tmux.get_history.return_value = Q_CLI_OUTPUT
        provider = QCliProvider("t1", "s1", "w1", agent_profile="analyst")

        result = run_async(provider.extract_session_context())

        assert result["provider"] == "q_cli"
        assert "log" in result["last_task"].lower() or "analyze" in result["last_task"].lower()

    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_returns_empty_dict_on_no_output(self, mock_tmux):
        from cli_agent_orchestrator.providers.q_cli import QCliProvider

        mock_tmux.get_history.return_value = ""
        provider = QCliProvider("t1", "s1", "w1", agent_profile="analyst")

        result = run_async(provider.extract_session_context())
        assert result == {}


# ---------------------------------------------------------------------------
# Helper methods
# ---------------------------------------------------------------------------


class TestExtractDecisions:
    def test_finds_decision_patterns(self):
        from cli_agent_orchestrator.providers.base import BaseProvider

        text = (
            "I'll use pytest for testing.\n"
            "The approach is to mock the database.\n"
            "Some random line.\n"
            "Decided to switch to async.\n"
        )
        decisions = BaseProvider._extract_decisions(text)
        assert len(decisions) == 3
        assert any("pytest" in d for d in decisions)
        assert any("async" in d for d in decisions)

    def test_empty_text_returns_empty(self):
        from cli_agent_orchestrator.providers.base import BaseProvider

        assert BaseProvider._extract_decisions("") == []


class TestExtractQuestions:
    def test_finds_questions_in_messages(self):
        from cli_agent_orchestrator.providers.base import BaseProvider

        messages = [
            "Should we add rate limiting?",
            "Fix the bug",
            "What about error handling?",
        ]
        questions = BaseProvider._extract_questions(messages)
        assert len(questions) == 2
        assert any("rate limiting" in q for q in questions)


class TestExtractFilePaths:
    def test_finds_file_paths(self):
        from cli_agent_orchestrator.providers.base import BaseProvider

        text = (
            "Updated src/auth.py with the fix.\n"
            "Also modified test/test_auth.py for coverage.\n"
            "See https://example.com/docs for reference.\n"
        )
        paths = BaseProvider._extract_file_paths(text)
        assert any("src/auth.py" in p for p in paths)
        assert any("test/test_auth.py" in p for p in paths)
        # URLs should be excluded
        assert not any("https" in p for p in paths)

    def test_empty_text_returns_empty(self):
        from cli_agent_orchestrator.providers.base import BaseProvider

        assert BaseProvider._extract_file_paths("") == []
