"""Tests for U8 — Pre-Compaction Flush.

Covers:
- U8.1: get_context_usage_percentage() default returns None in BaseProvider
- U8.2: Claude Code implementation parses JSONL transcript
- U8.3: wait_until_status() sends flush instruction at threshold
- U8.3: Flush triggers at most once per terminal session
- U8.3: Non-Claude Code providers return None (no flush triggered)
- U8.4: flush_threshold setting read/write
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider


class _ConcreteProvider(BaseProvider):
    """Minimal concrete subclass for testing BaseProvider defaults."""

    def initialize(self) -> bool:
        return True

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        return self._status

    def get_idle_pattern_for_log(self) -> str:
        return r"\[test\]>"

    def extract_last_message_from_script(self, script_output: str) -> str:
        return ""

    async def extract_session_context(self) -> Dict[str, Any]:
        return {}

    def exit_cli(self) -> str:
        return "/exit"

    def cleanup(self) -> None:
        pass


# ---------------------------------------------------------------------------
# U8.1 — Default get_context_usage_percentage()
# ---------------------------------------------------------------------------


class TestBaseProviderContextUsage:
    def test_default_returns_none(self):
        provider = _ConcreteProvider("t1", "s1", "w1")
        assert provider.get_context_usage_percentage() is None


# ---------------------------------------------------------------------------
# U8.2 — Claude Code JSONL parsing
# ---------------------------------------------------------------------------


class TestClaudeCodeContextUsage:
    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_parses_context_usage_from_jsonl(self, mock_tmux, tmp_path):
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        # Set up mock working directory
        mock_tmux.get_pane_working_directory.return_value = str(tmp_path / "myproject")

        # Create the Claude projects directory structure
        sanitized = str(tmp_path / "myproject").replace("/", "-")
        claude_projects = Path.home() / ".claude" / "projects" / sanitized
        claude_projects.mkdir(parents=True, exist_ok=True)

        # Write a fake JSONL transcript with context_usage_percentage
        transcript = claude_projects / "session-001.jsonl"
        lines = [
            json.dumps({"type": "user", "message": "hello"}),
            json.dumps({"type": "assistant", "message": "hi", "context_usage_percentage": 0.45}),
            json.dumps({"type": "assistant", "message": "done", "context_usage_percentage": 0.72}),
        ]
        transcript.write_text("\n".join(lines) + "\n")

        provider = ClaudeCodeProvider("t1", "s1", "w1")
        result = provider.get_context_usage_percentage()

        assert result == 0.72

        # Cleanup
        transcript.unlink()
        claude_projects.rmdir()

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_returns_none_when_no_working_dir(self, mock_tmux):
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        mock_tmux.get_pane_working_directory.return_value = None

        provider = ClaudeCodeProvider("t1", "s1", "w1")
        assert provider.get_context_usage_percentage() is None

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_returns_none_when_no_jsonl_files(self, mock_tmux, tmp_path):
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        mock_tmux.get_pane_working_directory.return_value = str(tmp_path / "empty-project")

        # Directory exists but no JSONL files
        sanitized = str(tmp_path / "empty-project").replace("/", "-")
        claude_projects = Path.home() / ".claude" / "projects" / sanitized
        claude_projects.mkdir(parents=True, exist_ok=True)

        provider = ClaudeCodeProvider("t1", "s1", "w1")
        assert provider.get_context_usage_percentage() is None

        # Cleanup
        claude_projects.rmdir()

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_returns_none_when_no_projects_dir(self, mock_tmux, tmp_path):
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        # Point to a non-existent working directory
        mock_tmux.get_pane_working_directory.return_value = "/nonexistent/project/path/xxx"

        provider = ClaudeCodeProvider("t1", "s1", "w1")
        assert provider.get_context_usage_percentage() is None

    @patch("cli_agent_orchestrator.providers.claude_code.tmux_client")
    def test_ignores_invalid_percentage_values(self, mock_tmux, tmp_path):
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        mock_tmux.get_pane_working_directory.return_value = str(tmp_path / "proj")

        sanitized = str(tmp_path / "proj").replace("/", "-")
        claude_projects = Path.home() / ".claude" / "projects" / sanitized
        claude_projects.mkdir(parents=True, exist_ok=True)

        # Write invalid values: > 1.0 and negative
        transcript = claude_projects / "session-002.jsonl"
        lines = [
            json.dumps({"type": "assistant", "context_usage_percentage": 1.5}),
            json.dumps({"type": "assistant", "context_usage_percentage": -0.1}),
            json.dumps({"type": "assistant", "context_usage_percentage": "not_a_number"}),
        ]
        transcript.write_text("\n".join(lines) + "\n")

        provider = ClaudeCodeProvider("t1", "s1", "w1")
        assert provider.get_context_usage_percentage() is None

        # Cleanup
        transcript.unlink()
        claude_projects.rmdir()


# ---------------------------------------------------------------------------
# U8.3 — Pre-compaction flush in wait_until_status()
# ---------------------------------------------------------------------------


class TestPreCompactionFlush:
    def setup_method(self):
        """Clear flush tracking between tests."""
        from cli_agent_orchestrator.utils.terminal import _flush_triggered_terminals

        _flush_triggered_terminals.clear()

    @patch("cli_agent_orchestrator.utils.terminal._get_flush_threshold", return_value=0.85)
    @patch("cli_agent_orchestrator.services.terminal_service.send_input")
    def test_sends_flush_when_above_threshold(self, mock_send_input, mock_threshold):
        from cli_agent_orchestrator.models.terminal import TerminalStatus
        from cli_agent_orchestrator.utils.terminal import FLUSH_MESSAGE, wait_until_status

        mock_provider = MagicMock()
        mock_provider.terminal_id = "t-flush-1"
        # First call: PROCESSING (not target), second call: COMPLETED (target)
        mock_provider.get_status.side_effect = [TerminalStatus.PROCESSING, TerminalStatus.COMPLETED]
        mock_provider.get_context_usage_percentage.return_value = 0.87

        result = wait_until_status(mock_provider, TerminalStatus.COMPLETED, timeout=5.0, polling_interval=0.01)

        assert result is True
        mock_send_input.assert_called_once_with("t-flush-1", FLUSH_MESSAGE)

    @patch("cli_agent_orchestrator.utils.terminal._get_flush_threshold", return_value=0.85)
    @patch("cli_agent_orchestrator.services.terminal_service.send_input")
    def test_flush_triggers_only_once(self, mock_send_input, mock_threshold):
        from cli_agent_orchestrator.models.terminal import TerminalStatus
        from cli_agent_orchestrator.utils.terminal import wait_until_status

        mock_provider = MagicMock()
        mock_provider.terminal_id = "t-flush-2"
        # Three PROCESSING polls, then COMPLETED
        mock_provider.get_status.side_effect = [
            TerminalStatus.PROCESSING,
            TerminalStatus.PROCESSING,
            TerminalStatus.PROCESSING,
            TerminalStatus.COMPLETED,
        ]
        mock_provider.get_context_usage_percentage.return_value = 0.90

        wait_until_status(mock_provider, TerminalStatus.COMPLETED, timeout=5.0, polling_interval=0.01)

        # Should only send flush once despite 3 PROCESSING polls above threshold
        assert mock_send_input.call_count == 1

    @patch("cli_agent_orchestrator.utils.terminal._get_flush_threshold", return_value=0.85)
    @patch("cli_agent_orchestrator.services.terminal_service.send_input")
    def test_no_flush_when_below_threshold(self, mock_send_input, mock_threshold):
        from cli_agent_orchestrator.models.terminal import TerminalStatus
        from cli_agent_orchestrator.utils.terminal import wait_until_status

        mock_provider = MagicMock()
        mock_provider.terminal_id = "t-flush-3"
        mock_provider.get_status.side_effect = [TerminalStatus.PROCESSING, TerminalStatus.COMPLETED]
        mock_provider.get_context_usage_percentage.return_value = 0.50

        wait_until_status(mock_provider, TerminalStatus.COMPLETED, timeout=5.0, polling_interval=0.01)

        mock_send_input.assert_not_called()

    @patch("cli_agent_orchestrator.utils.terminal._get_flush_threshold", return_value=0.85)
    @patch("cli_agent_orchestrator.services.terminal_service.send_input")
    def test_no_flush_when_usage_is_none(self, mock_send_input, mock_threshold):
        from cli_agent_orchestrator.models.terminal import TerminalStatus
        from cli_agent_orchestrator.utils.terminal import wait_until_status

        mock_provider = MagicMock()
        mock_provider.terminal_id = "t-flush-4"
        mock_provider.get_status.side_effect = [TerminalStatus.PROCESSING, TerminalStatus.COMPLETED]
        mock_provider.get_context_usage_percentage.return_value = None

        wait_until_status(mock_provider, TerminalStatus.COMPLETED, timeout=5.0, polling_interval=0.01)

        mock_send_input.assert_not_called()

    @patch("cli_agent_orchestrator.utils.terminal._get_flush_threshold", return_value=0.85)
    @patch("cli_agent_orchestrator.services.terminal_service.send_input")
    def test_flush_failure_does_not_break_wait(self, mock_send_input, mock_threshold):
        from cli_agent_orchestrator.models.terminal import TerminalStatus
        from cli_agent_orchestrator.utils.terminal import wait_until_status

        mock_provider = MagicMock()
        mock_provider.terminal_id = "t-flush-5"
        mock_provider.get_status.side_effect = [TerminalStatus.PROCESSING, TerminalStatus.COMPLETED]
        mock_provider.get_context_usage_percentage.return_value = 0.90
        mock_send_input.side_effect = RuntimeError("send_input failed")

        # Should not raise — flush failure is non-blocking
        result = wait_until_status(mock_provider, TerminalStatus.COMPLETED, timeout=5.0, polling_interval=0.01)
        assert result is True


# ---------------------------------------------------------------------------
# U8.4 — flush_threshold settings
# ---------------------------------------------------------------------------


class TestFlushThresholdSettings:
    def test_get_default_threshold(self):
        from cli_agent_orchestrator.services.settings_service import get_memory_settings

        settings = get_memory_settings()
        assert settings["flush_threshold"] == 0.85

    def test_set_and_get_threshold(self, tmp_path):
        from cli_agent_orchestrator.services import settings_service

        # Temporarily override settings file
        original_file = settings_service.SETTINGS_FILE
        settings_service.SETTINGS_FILE = tmp_path / "settings.json"
        try:
            result = settings_service.set_memory_setting("flush_threshold", 0.90)
            assert result["flush_threshold"] == 0.90

            # Verify persistence
            loaded = settings_service.get_memory_settings()
            assert loaded["flush_threshold"] == 0.90
        finally:
            settings_service.SETTINGS_FILE = original_file

    def test_rejects_invalid_threshold(self, tmp_path):
        from cli_agent_orchestrator.services import settings_service

        original_file = settings_service.SETTINGS_FILE
        settings_service.SETTINGS_FILE = tmp_path / "settings.json"
        try:
            with pytest.raises(ValueError, match="flush_threshold must be between"):
                settings_service.set_memory_setting("flush_threshold", 1.5)

            with pytest.raises(ValueError, match="flush_threshold must be between"):
                settings_service.set_memory_setting("flush_threshold", 0.0)
        finally:
            settings_service.SETTINGS_FILE = original_file

    def test_rejects_unknown_setting(self):
        from cli_agent_orchestrator.services.settings_service import set_memory_setting

        with pytest.raises(ValueError, match="Unknown memory setting"):
            set_memory_setting("nonexistent_key", 42)
