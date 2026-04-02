"""Tests for context injection utility."""
from unittest.mock import patch
import pytest


class TestInjectContextFiles:
    def test_sends_context_add_command(self):
        with patch("cli_agent_orchestrator.utils.context.terminal_service") as mock_ts:
            from cli_agent_orchestrator.utils.context import inject_context_files
            inject_context_files("term-1", ["/tmp/file1.md", "/tmp/file2.md"])
            mock_ts.send_input.assert_called_once()
            cmd = mock_ts.send_input.call_args[0][1]
            assert "/context add" in cmd
            assert '"/tmp/file1.md"' in cmd
            assert '"/tmp/file2.md"' in cmd

    def test_empty_list_no_op(self):
        with patch("cli_agent_orchestrator.utils.context.terminal_service") as mock_ts:
            from cli_agent_orchestrator.utils.context import inject_context_files
            result = inject_context_files("term-1", [])
            mock_ts.send_input.assert_not_called()
            assert result is True

    def test_returns_true_on_success(self):
        with patch("cli_agent_orchestrator.utils.context.terminal_service"):
            from cli_agent_orchestrator.utils.context import inject_context_files
            assert inject_context_files("term-1", ["/file.md"]) is True

    def test_returns_false_on_error(self):
        with patch("cli_agent_orchestrator.utils.context.terminal_service") as mock_ts:
            mock_ts.send_input.side_effect = Exception("fail")
            from cli_agent_orchestrator.utils.context import inject_context_files
            assert inject_context_files("term-1", ["/file.md"]) is False

    def test_quotes_file_paths(self):
        with patch("cli_agent_orchestrator.utils.context.terminal_service") as mock_ts:
            from cli_agent_orchestrator.utils.context import inject_context_files
            inject_context_files("term-1", ["/path with spaces/file.md"])
            cmd = mock_ts.send_input.call_args[0][1]
            assert '"/path with spaces/file.md"' in cmd
