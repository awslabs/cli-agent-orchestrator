"""Tests for U7 — Cache-Aware Injection.

Covers:
- U7.1: Static identity vs dynamic memory boundary is defined
- U7.2: Claude Code — static identity in --append-system-prompt, dynamic in first user message
- U7.3: Kiro CLI — static identity written to .kiro/steering/agent-identity.md
- U7.4: Gemini CLI — static identity in GEMINI.md, dynamic in first user message
- U7.5: Codex CLI — static identity in -c developer_instructions, dynamic in first user message
- U7.6: inject_memory_context() only touches first user message (never system prompt)
"""

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_profile(system_prompt: str = "You are a test agent.", name: str = "test-agent"):
    """Create a mock agent profile with the given system_prompt."""
    profile = MagicMock()
    profile.system_prompt = system_prompt
    profile.name = name
    profile.mcpServers = None
    profile.allowedTools = None
    profile.role = None
    return profile


# ---------------------------------------------------------------------------
# U7.2 — Claude Code: static identity in --append-system-prompt
# ---------------------------------------------------------------------------


class TestClaudeCodeCacheAwareInjection:
    """Claude Code uses --append-system-prompt for static identity."""

    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_system_prompt_in_append_flag(self, mock_load):
        """Static identity (system_prompt) should appear in --append-system-prompt flag."""
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        mock_load.return_value = _make_mock_profile("You are the supervisor agent.")
        provider = ClaudeCodeProvider(
            terminal_id="t1",
            session_name="s1",
            window_name="w1",
            agent_profile="supervisor",
        )

        command = provider._build_claude_command()

        assert "--append-system-prompt" in command
        assert "supervisor agent" in command

    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_no_cao_memory_in_system_prompt(self, mock_load):
        """<cao-memory> must NOT appear in the --append-system-prompt flag."""
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        mock_load.return_value = _make_mock_profile("You are the supervisor agent.")
        provider = ClaudeCodeProvider(
            terminal_id="t1",
            session_name="s1",
            window_name="w1",
            agent_profile="supervisor",
        )

        command = provider._build_claude_command()

        assert "cao-memory" not in command
        assert "<cao-memory>" not in command


# ---------------------------------------------------------------------------
# U7.3 — Kiro CLI: static identity in .kiro/steering/agent-identity.md
# ---------------------------------------------------------------------------


class TestKiroCliCacheAwareInjection:
    """Kiro CLI writes static identity to .kiro/steering/agent-identity.md."""

    def test_steering_file_written(self, tmp_path):
        """_write_kiro_steering_file creates the steering file with system_prompt content."""
        from cli_agent_orchestrator.services.terminal_service import _write_kiro_steering_file

        system_prompt = "You are a code reviewer. Review all PRs carefully."
        _write_kiro_steering_file(system_prompt, str(tmp_path))

        steering_file = tmp_path / ".kiro" / "steering" / "agent-identity.md"
        assert steering_file.exists()
        assert steering_file.read_text(encoding="utf-8") == system_prompt

    def test_steering_file_creates_directories(self, tmp_path):
        """Steering file writer should create .kiro/steering/ directories if missing."""
        from cli_agent_orchestrator.services.terminal_service import _write_kiro_steering_file

        _write_kiro_steering_file("test prompt", str(tmp_path))

        assert (tmp_path / ".kiro" / "steering").is_dir()

    def test_steering_file_skipped_when_empty(self, tmp_path):
        """Empty system_prompt should not create a steering file."""
        from cli_agent_orchestrator.services.terminal_service import _write_kiro_steering_file

        _write_kiro_steering_file("", str(tmp_path))

        assert not (tmp_path / ".kiro" / "steering" / "agent-identity.md").exists()

    def test_steering_file_containment_check(self, tmp_path):
        """Steering path must stay within working_directory after normalization."""
        from cli_agent_orchestrator.services.terminal_service import _write_kiro_steering_file

        # The function uses realpath + startswith containment check.
        # A valid working_directory won't escape, but we verify the guard
        # works by checking the file IS created inside the working dir.
        _write_kiro_steering_file("test identity", str(tmp_path))
        steering = tmp_path / ".kiro" / "steering" / "agent-identity.md"
        assert str(steering.resolve()).startswith(str(tmp_path.resolve()))

    def test_steering_file_null_byte_blocked(self):
        """Null bytes in working_directory should be rejected."""
        from cli_agent_orchestrator.services.terminal_service import _write_kiro_steering_file

        with pytest.raises(ValueError, match="null bytes"):
            _write_kiro_steering_file("prompt", "/tmp/test\x00evil")

    def test_steering_file_no_cao_memory(self, tmp_path):
        """Steering file must NOT contain <cao-memory> — that goes in the user message."""
        from cli_agent_orchestrator.services.terminal_service import _write_kiro_steering_file

        system_prompt = "You are an agent. Follow instructions carefully."
        _write_kiro_steering_file(system_prompt, str(tmp_path))

        content = (tmp_path / ".kiro" / "steering" / "agent-identity.md").read_text()
        assert "<cao-memory>" not in content

    def test_steering_file_overwrites_existing(self, tmp_path):
        """Writing the steering file should overwrite any existing content."""
        from cli_agent_orchestrator.services.terminal_service import _write_kiro_steering_file

        steering_dir = tmp_path / ".kiro" / "steering"
        steering_dir.mkdir(parents=True)
        (steering_dir / "agent-identity.md").write_text("old content")

        _write_kiro_steering_file("new identity", str(tmp_path))

        assert (steering_dir / "agent-identity.md").read_text() == "new identity"


# ---------------------------------------------------------------------------
# U7.4 — Gemini CLI: static identity in GEMINI.md
# ---------------------------------------------------------------------------


class TestGeminiCliCacheAwareInjection:
    """Gemini CLI writes static identity to GEMINI.md."""

    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    @patch("cli_agent_orchestrator.providers.gemini_cli.load_agent_profile")
    def test_system_prompt_written_to_gemini_md(self, mock_load, mock_tmux, tmp_path):
        """Static identity should be written to GEMINI.md in the working directory."""
        from cli_agent_orchestrator.providers.gemini_cli import GeminiCliProvider

        mock_load.return_value = _make_mock_profile("You are the supervisor.")
        mock_tmux.get_pane_working_directory.return_value = str(tmp_path)

        provider = GeminiCliProvider(
            terminal_id="t1",
            session_name="s1",
            window_name="w1",
            agent_profile="supervisor",
        )
        provider._build_gemini_command()

        gemini_md = tmp_path / "GEMINI.md"
        assert gemini_md.exists()
        content = gemini_md.read_text()
        assert "supervisor" in content

    @patch("cli_agent_orchestrator.providers.gemini_cli.tmux_client")
    @patch("cli_agent_orchestrator.providers.gemini_cli.load_agent_profile")
    def test_no_cao_memory_in_gemini_md(self, mock_load, mock_tmux, tmp_path):
        """GEMINI.md must NOT contain <cao-memory>."""
        from cli_agent_orchestrator.providers.gemini_cli import GeminiCliProvider

        mock_load.return_value = _make_mock_profile("You are the supervisor.")
        mock_tmux.get_pane_working_directory.return_value = str(tmp_path)

        provider = GeminiCliProvider(
            terminal_id="t1",
            session_name="s1",
            window_name="w1",
            agent_profile="supervisor",
        )
        provider._build_gemini_command()

        content = (tmp_path / "GEMINI.md").read_text()
        assert "<cao-memory>" not in content


# ---------------------------------------------------------------------------
# U7.5 — Codex CLI: static identity in -c developer_instructions
# ---------------------------------------------------------------------------


class TestCodexCliCacheAwareInjection:
    """Codex CLI uses -c developer_instructions for static identity."""

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_system_prompt_in_developer_instructions(self, mock_load):
        """Static identity should appear in -c developer_instructions flag."""
        from cli_agent_orchestrator.providers.codex import CodexProvider

        mock_load.return_value = _make_mock_profile("You are the code reviewer.")
        provider = CodexProvider(
            terminal_id="t1",
            session_name="s1",
            window_name="w1",
            agent_profile="reviewer",
        )

        command = provider._build_codex_command()

        assert "developer_instructions" in command
        assert "code reviewer" in command

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_no_cao_memory_in_developer_instructions(self, mock_load):
        """<cao-memory> must NOT appear in developer_instructions."""
        from cli_agent_orchestrator.providers.codex import CodexProvider

        mock_load.return_value = _make_mock_profile("You are the code reviewer.")
        provider = CodexProvider(
            terminal_id="t1",
            session_name="s1",
            window_name="w1",
            agent_profile="reviewer",
        )

        command = provider._build_codex_command()

        assert "cao-memory" not in command
        assert "<cao-memory>" not in command


# ---------------------------------------------------------------------------
# U7.6 — inject_memory_context only touches first user message
# ---------------------------------------------------------------------------


class TestInjectMemoryContextBoundary:
    """inject_memory_context() puts <cao-memory> in user message, never system prompt."""

    @patch("cli_agent_orchestrator.services.terminal_service.MemoryService")
    def test_memory_prepended_to_user_message(self, MockService):
        """Dynamic memory block should be prepended to the first user message."""
        from cli_agent_orchestrator.services.terminal_service import (
            _memory_injected_terminals,
            inject_memory_context,
        )

        # Reset state
        _memory_injected_terminals.discard("test-inject-1")

        instance = MockService.return_value
        instance.get_curated_memory_context.return_value = (
            "<cao-memory>\nProject uses pytest.\n</cao-memory>"
        )

        result = inject_memory_context("Write a test for foo", "test-inject-1")

        assert result.startswith("<cao-memory>")
        assert "Write a test for foo" in result
        assert "pytest" in result

        # Cleanup
        _memory_injected_terminals.discard("test-inject-1")

    @patch("cli_agent_orchestrator.services.terminal_service.MemoryService")
    def test_memory_injected_only_once(self, MockService):
        """inject_memory_context should only inject on the first call per terminal."""
        from cli_agent_orchestrator.services.terminal_service import (
            _memory_injected_terminals,
            inject_memory_context,
        )

        _memory_injected_terminals.discard("test-inject-2")

        instance = MockService.return_value
        instance.get_curated_memory_context.return_value = "<cao-memory>data</cao-memory>"

        # First call — should inject
        result1 = inject_memory_context("first message", "test-inject-2")
        assert "<cao-memory>" in result1

        # Second call — should NOT inject
        result2 = inject_memory_context("second message", "test-inject-2")
        assert result2 == "second message"
        assert "<cao-memory>" not in result2

        _memory_injected_terminals.discard("test-inject-2")

    def test_no_memory_in_empty_context(self):
        """When no memories exist, user message should pass through unchanged."""
        from cli_agent_orchestrator.services.terminal_service import (
            _memory_injected_terminals,
            inject_memory_context,
        )

        _memory_injected_terminals.discard("test-inject-3")

        with patch("cli_agent_orchestrator.services.terminal_service.MemoryService") as MockService:
            instance = MockService.return_value
            instance.get_curated_memory_context.return_value = ""

            result = inject_memory_context("Hello agent", "test-inject-3")
            assert result == "Hello agent"

        _memory_injected_terminals.discard("test-inject-3")
