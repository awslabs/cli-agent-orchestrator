"""Unit tests for MockCliProvider and the fixture mock_cli binary.

The MockCliProvider is the credential-free orchestration-testing
primitive — see ``docs/mock-cli-provider.md`` for design + motivation.

These tests cover three surfaces:
  1. State detection on captured mock_cli output (mocked tmux).
  2. Last-message extraction from script output.
  3. Smoke checks that the fixture binary ships and is executable.
"""

import os
import pathlib
import re
import subprocess

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.mock_cli import MockCliProvider

_PROMPT = "❯ "


class TestMockCliProviderStatus:
    """State detection on the buffer the StatusMonitor hands to get_status().

    Post event-driven refactor, ``get_status(buffer)`` receives the captured
    output string directly rather than reading tmux itself.
    """

    def test_idle_when_only_banner_and_prompt(self):
        provider = MockCliProvider("t1", "sess", "win")
        assert provider.get_status(f"MockCli ready.\n{_PROMPT}") == TerminalStatus.IDLE

    def test_processing_when_prompt_not_visible(self):
        provider = MockCliProvider("t1", "sess", "win")
        assert provider.get_status("MockCli ready.\nhello") == TerminalStatus.PROCESSING

    def test_completed_after_response_with_trailing_prompt(self):
        provider = MockCliProvider("t1", "sess", "win")
        buffer = f"MockCli ready.\n{_PROMPT}hello\n> MOCK: hello\n{_PROMPT}"
        assert provider.get_status(buffer) == TerminalStatus.COMPLETED

    def test_error_when_indicator_present(self):
        provider = MockCliProvider("t1", "sess", "win")
        buffer = f"{_PROMPT}__mock_error__\nERROR: mock failure injected\n{_PROMPT}"
        assert provider.get_status(buffer) == TerminalStatus.ERROR

    def test_unknown_when_buffer_empty(self):
        # Empty buffer is indeterminate, not an error, per the base contract.
        provider = MockCliProvider("t1", "sess", "win")
        assert provider.get_status("") == TerminalStatus.UNKNOWN

    def test_ansi_codes_do_not_break_state_detection(self):
        # Real tmux capture often interleaves ANSI; the provider strips them.
        provider = MockCliProvider("t1", "sess", "win")
        buffer = f"\x1b[1mMockCli ready.\x1b[0m\n{_PROMPT}hello\n> MOCK: hello\n{_PROMPT}"
        assert provider.get_status(buffer) == TerminalStatus.COMPLETED


class TestMockCliProviderExtraction:
    """Last-message extraction from full script output."""

    def test_extracts_last_response_when_multiple_turns(self):
        script = f"{_PROMPT}first\n> MOCK: first\n" f"{_PROMPT}second\n> MOCK: second\n{_PROMPT}"
        provider = MockCliProvider("t1", "sess", "win")
        assert provider.extract_last_message_from_script(script) == "second"

    def test_raises_value_error_when_no_response_present(self):
        provider = MockCliProvider("t1", "sess", "win")
        with pytest.raises(ValueError, match="No mock_cli response"):
            provider.extract_last_message_from_script(f"{_PROMPT}")


class TestMockCliProviderContract:
    """Provider contract surface (exit cmd, log pattern, defaults)."""

    def test_exit_cli_returns_slash_exit(self):
        provider = MockCliProvider("t1", "sess", "win")
        assert provider.exit_cli() == "/exit"

    def test_idle_log_pattern_matches_prompt(self):
        provider = MockCliProvider("t1", "sess", "win")
        assert re.search(provider.get_idle_pattern_for_log(), f"MockCli ready.\n{_PROMPT}")

    def test_initial_status_is_idle(self):
        provider = MockCliProvider("t1", "sess", "win")
        assert provider.status == TerminalStatus.IDLE


class TestMockCliBinary:
    """Smoke checks that the fixture binary ships with the repo and works."""

    @pytest.fixture
    def bin_path(self) -> pathlib.Path:
        return pathlib.Path(__file__).parent / "fixtures" / "bin" / "mock_cli"

    def test_binary_exists(self, bin_path):
        assert bin_path.exists(), f"missing fixture binary: {bin_path}"

    def test_binary_is_executable(self, bin_path):
        assert os.access(bin_path, os.X_OK), f"binary not executable: {bin_path}"

    def test_binary_version_flag(self, bin_path):
        result = subprocess.run(
            [str(bin_path), "--version"], capture_output=True, text=True, timeout=5
        )
        assert result.returncode == 0
        assert "mock_cli" in result.stdout

    def test_binary_repl_echoes_input(self, bin_path):
        result = subprocess.run(
            [str(bin_path), "--delay-ms", "1"],
            input="hello world\n/exit\n",
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0
        assert "MOCK: hello world" in result.stdout

    def test_binary_repl_emits_error_indicator(self, bin_path):
        result = subprocess.run(
            [str(bin_path), "--delay-ms", "1"],
            input="__mock_error__\n/exit\n",
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0
        assert "ERROR: mock failure injected" in result.stdout
