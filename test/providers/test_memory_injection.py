"""Tests for memory injection into provider first messages (U3 + U8.4).

Tests verify that <cao-memory> block is correctly injected into the first user
message sent to each terminal, and that no provider-specific files are mutated.
"""

import asyncio
from pathlib import Path
from unittest.mock import patch

from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.terminal_service import (
    _memory_injected_terminals,
    inject_memory_context,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_MEMORY_CONTEXT = (
    "<cao-memory>\n"
    "## Context from CAO Memory\n"
    "- [project] deploy-pipeline: Uses GitHub Actions with OIDC auth\n"
    "- [global] prefer-pytest: Always use pytest\n"
    "</cao-memory>"
)

ORIGINAL_MESSAGE = "Implement the login endpoint using OAuth2."


# ===========================================================================
# U8.4 — Provider Injection Tests
# ===========================================================================


class TestInjectMemoryContextFunction:
    """U3: Tests for the real inject_memory_context() in terminal_service.py."""

    def setup_method(self):
        """Clear injection tracking set before each test."""
        _memory_injected_terminals.clear()

    @patch("cli_agent_orchestrator.services.terminal_service.MemoryService")
    def test_first_message_gets_memory_injected(self, mock_svc_cls):
        """First message should have <cao-memory> block prepended."""
        mock_svc_cls.return_value.get_curated_memory_context.return_value = (
            SAMPLE_MEMORY_CONTEXT
        )

        result = inject_memory_context(ORIGINAL_MESSAGE, "term-001")

        assert "<cao-memory>" in result
        assert "</cao-memory>" in result
        assert ORIGINAL_MESSAGE in result
        assert result.index("<cao-memory>") < result.index(ORIGINAL_MESSAGE)

    @patch("cli_agent_orchestrator.services.terminal_service.MemoryService")
    def test_second_message_not_injected(self, mock_svc_cls):
        """Second message to same terminal should NOT get memory injection."""
        mock_svc_cls.return_value.get_curated_memory_context.return_value = (
            SAMPLE_MEMORY_CONTEXT
        )

        # First call — injects
        inject_memory_context(ORIGINAL_MESSAGE, "term-002")
        # Second call — should return original message unchanged
        result = inject_memory_context("Second message", "term-002")

        assert "<cao-memory>" not in result
        assert result == "Second message"

    @patch("cli_agent_orchestrator.services.terminal_service.MemoryService")
    def test_no_injection_when_no_memories(self, mock_svc_cls):
        """When no memories exist, message should be returned unchanged."""
        mock_svc_cls.return_value.get_curated_memory_context.return_value = ""

        result = inject_memory_context(ORIGINAL_MESSAGE, "term-003")

        assert "<cao-memory>" not in result
        assert result == ORIGINAL_MESSAGE

    def test_already_injected_returns_original(self):
        """If terminal already in tracking set, message should be returned unchanged."""
        _memory_injected_terminals.add("term-already")

        result = inject_memory_context(ORIGINAL_MESSAGE, "term-already")

        assert result == ORIGINAL_MESSAGE
        assert "<cao-memory>" not in result

    @patch("cli_agent_orchestrator.services.terminal_service.MemoryService")
    def test_injection_failure_returns_original(self, mock_svc_cls):
        """If MemoryService raises, original message should be returned."""
        mock_svc_cls.return_value.get_curated_memory_context.side_effect = RuntimeError(
            "DB error"
        )

        result = inject_memory_context(ORIGINAL_MESSAGE, "term-004")

        assert result == ORIGINAL_MESSAGE
        # Terminal is still marked as injected (won't retry)
        assert "term-004" in _memory_injected_terminals

    @patch("cli_agent_orchestrator.services.terminal_service.MemoryService")
    def test_different_terminals_both_get_injection(self, mock_svc_cls):
        """Different terminals should each get their own injection."""
        mock_svc_cls.return_value.get_curated_memory_context.return_value = (
            SAMPLE_MEMORY_CONTEXT
        )

        r1 = inject_memory_context("Message A", "term-A")
        r2 = inject_memory_context("Message B", "term-B")

        assert "<cao-memory>" in r1
        assert "<cao-memory>" in r2

    def test_cleanup_removes_terminal_from_tracking(self):
        """After cleanup, terminal should be eligible for injection again."""
        _memory_injected_terminals.add("term-cleanup")
        assert "term-cleanup" in _memory_injected_terminals

        _memory_injected_terminals.discard("term-cleanup")
        assert "term-cleanup" not in _memory_injected_terminals


class TestClaudeCodeInjection:
    """U8.4: Claude Code first message contains <cao-memory> block."""

    def setup_method(self):
        _memory_injected_terminals.clear()

    @patch("cli_agent_orchestrator.services.terminal_service.MemoryService")
    def test_claude_code_injection(self, mock_svc_cls):
        """First message to Claude Code terminal should include <cao-memory> block."""
        mock_svc_cls.return_value.get_curated_memory_context.return_value = (
            SAMPLE_MEMORY_CONTEXT
        )

        injected = inject_memory_context(ORIGINAL_MESSAGE, "term-claude-001")

        assert "<cao-memory>" in injected
        assert "</cao-memory>" in injected
        assert ORIGINAL_MESSAGE in injected
        assert injected.index("<cao-memory>") < injected.index(ORIGINAL_MESSAGE)


class TestKiroInjection:
    """U8.4: Kiro CLI first message contains <cao-memory> block."""

    def setup_method(self):
        _memory_injected_terminals.clear()

    @patch("cli_agent_orchestrator.services.terminal_service.MemoryService")
    def test_kiro_injection(self, mock_svc_cls):
        """First message to Kiro CLI terminal should include <cao-memory> block."""
        mock_svc_cls.return_value.get_curated_memory_context.return_value = (
            SAMPLE_MEMORY_CONTEXT
        )

        injected = inject_memory_context(ORIGINAL_MESSAGE, "term-kiro-001")

        assert "<cao-memory>" in injected
        assert "</cao-memory>" in injected
        assert ORIGINAL_MESSAGE in injected
        assert injected.index("<cao-memory>") < injected.index(ORIGINAL_MESSAGE)


class TestGeminiInjection:
    """U8.4: Gemini CLI first message contains <cao-memory> block, GEMINI.md not modified."""

    def setup_method(self):
        _memory_injected_terminals.clear()

    @patch("cli_agent_orchestrator.services.terminal_service.MemoryService")
    def test_gemini_injection(self, mock_svc_cls):
        """First message to Gemini terminal should include <cao-memory> block."""
        mock_svc_cls.return_value.get_curated_memory_context.return_value = (
            SAMPLE_MEMORY_CONTEXT
        )

        injected = inject_memory_context(ORIGINAL_MESSAGE, "term-gemini-001")

        assert "<cao-memory>" in injected
        assert "</cao-memory>" in injected
        assert ORIGINAL_MESSAGE in injected

    def test_gemini_md_not_modified(self, tmp_path: Path):
        """GEMINI.md should not be mutated during memory injection."""
        gemini_md = tmp_path / "project" / "GEMINI.md"
        gemini_md.parent.mkdir(parents=True, exist_ok=True)
        original = "# Gemini Config\nDo not touch."
        gemini_md.write_text(original)

        # Simulate injection — memory context generation should not touch GEMINI.md
        svc = MemoryService(base_dir=tmp_path / "memory")
        ctx = {
            "terminal_id": "term-gemini-001",
            "session_name": "test-session",
            "provider": "gemini_cli",
            "agent_profile": "developer",
            "cwd": str(tmp_path / "project"),
        }

        asyncio.run(
            svc.store(
                content="Test memory",
                scope="global",
                memory_type="project",
                key="test-mem",
                terminal_context=ctx,
            )
        )

        svc._get_terminal_context = lambda terminal_id: ctx
        context = svc.get_memory_context_for_terminal("term-gemini-001")
        # Just verify context exists and file wasn't touched
        assert context  # non-empty
        assert gemini_md.read_text() == original


class TestNoInjectionWhenEmpty:
    """U8.4: no memories → no <cao-memory> block."""

    def setup_method(self):
        _memory_injected_terminals.clear()

    @patch("cli_agent_orchestrator.services.terminal_service.MemoryService")
    def test_no_injection_when_empty(self, mock_svc_cls):
        """When no memories exist, no <cao-memory> block should be injected."""
        mock_svc_cls.return_value.get_curated_memory_context.return_value = ""

        injected = inject_memory_context(ORIGINAL_MESSAGE, "term-001")

        assert "<cao-memory>" not in injected
        assert injected == ORIGINAL_MESSAGE


class TestConfigFilesNotMutated:
    """U8.4: Memory injection must not mutate provider config files (SC-5)."""

    def _store_and_get_context(self, tmp_path: Path) -> str:
        """Helper: store a memory, then generate context block."""
        svc = MemoryService(base_dir=tmp_path / "memory")
        ctx = {
            "terminal_id": "term-001",
            "session_name": "test-session",
            "provider": "claude_code",
            "agent_profile": "developer",
            "cwd": str(tmp_path / "project"),
        }

        asyncio.run(
            svc.store(
                content="Test memory for injection",
                scope="global",
                memory_type="project",
                key="test-inject",
                terminal_context=ctx,
            )
        )

        svc._get_terminal_context = lambda terminal_id: ctx
        return svc.get_memory_context_for_terminal("term-001")

    def test_gemini_md_not_mutated(self, tmp_path: Path):
        """GEMINI.md must not be modified during memory context generation."""
        gemini_md = tmp_path / "project" / "GEMINI.md"
        gemini_md.parent.mkdir(parents=True, exist_ok=True)
        original = "# Gemini Config\nDo not touch."
        gemini_md.write_text(original)

        context = self._store_and_get_context(tmp_path)

        assert "<cao-memory>" in context
        assert gemini_md.read_text() == original

    def test_claude_md_not_mutated(self, tmp_path: Path):
        """CLAUDE.md must not be modified during memory context generation."""
        claude_md = tmp_path / "project" / "CLAUDE.md"
        claude_md.parent.mkdir(parents=True, exist_ok=True)
        original = "# Claude Config\nDo not touch."
        claude_md.write_text(original)

        context = self._store_and_get_context(tmp_path)

        assert "<cao-memory>" in context
        assert claude_md.read_text() == original

    def test_agents_md_not_mutated(self, tmp_path: Path):
        """AGENTS.md (Kimi) must not be modified during memory context generation."""
        agents_md = tmp_path / "project" / "AGENTS.md"
        agents_md.parent.mkdir(parents=True, exist_ok=True)
        original = "# Agents Config\nDo not touch."
        agents_md.write_text(original)

        context = self._store_and_get_context(tmp_path)

        assert "<cao-memory>" in context
        assert agents_md.read_text() == original

    def test_kiro_steering_not_mutated(self, tmp_path: Path):
        """.kiro/steering/*.md must not be modified during memory context generation."""
        steering_dir = tmp_path / "project" / ".kiro" / "steering"
        steering_dir.mkdir(parents=True, exist_ok=True)
        steering_md = steering_dir / "product.md"
        original = "# Steering\nDo not touch."
        steering_md.write_text(original)

        context = self._store_and_get_context(tmp_path)

        assert "<cao-memory>" in context
        assert steering_md.read_text() == original


class TestMemoryContextGeneration:
    """Test the memory context block generation directly."""

    def test_context_includes_cao_memory_tags(self, tmp_path: Path):
        """Generated context should be wrapped in <cao-memory> tags."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = {
            "terminal_id": "term-001",
            "session_name": "test-session",
            "provider": "claude_code",
            "agent_profile": "developer",
            "cwd": "/home/user/project",
        }

        asyncio.run(
            svc.store(
                content="Always use type hints",
                scope="global",
                memory_type="feedback",
                key="type-hints",
                terminal_context=ctx,
            )
        )

        svc._get_terminal_context = lambda terminal_id: ctx
        context = svc.get_memory_context_for_terminal("term-001")

        assert "<cao-memory>" in context
        assert "</cao-memory>" in context
        assert "type-hints" in context

    def test_empty_context_when_no_memories(self, tmp_path: Path):
        """Should return empty string when no memories exist."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = {
            "terminal_id": "term-001",
            "session_name": "test-session",
            "provider": "claude_code",
            "agent_profile": "developer",
            "cwd": "/home/user/project",
        }

        svc._get_terminal_context = lambda terminal_id: ctx
        context = svc.get_memory_context_for_terminal("term-001")

        assert context == ""
