"""Unit tests for the OMP CLI provider.

The provider's status / extraction regexes are environment-overridable
(``CAO_OMP_*_REGEX``). The fixtures below exercise the *default* placeholder
patterns; the develop / test stages refine the env vars against real ``omp``
output without editing these tests' expectations.

These tests use synthetic buffers that match the documented default patterns so
they pass in any environment regardless of whether ``omp`` is installed.
"""

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.omp_cli import OmpCliProvider, ProviderError


def make_provider(agent_profile=None, model=None) -> OmpCliProvider:
    return OmpCliProvider(
        terminal_id="test-tid",
        session_name="test-session",
        window_name="window-0",
        agent_profile=agent_profile,
        allowed_tools=None,
        model=model,
    )


OMP_IDLE_OUTPUT = """OMP CLI v1.0.0
Ready.

omp>
"""

OMP_PROCESSING_OUTPUT = """● Summarize this

 omp Thinking…
"""

# After a turn has been delivered and the agent returned to the idle prompt.
OMP_COMPLETED_OUTPUT = """● Summarize this

 OMP: Here is a short summary.

omp>
"""

OMP_WAITING_OUTPUT = """● Run a command

 Approve this action? [y/N]
"""

OMP_ERROR_OUTPUT = """● do thing
Error: omp connection failed:
"""


class TestOmpCliStatus:
    def test_empty_buffer_is_unknown(self):
        p = make_provider()
        assert p.get_status("") is TerminalStatus.UNKNOWN

    def test_idle_detected(self):
        p = make_provider()
        assert p.get_status(OMP_IDLE_OUTPUT) is TerminalStatus.IDLE

    def test_processing_detected(self):
        p = make_provider()
        assert p.get_status(OMP_PROCESSING_OUTPUT) is TerminalStatus.PROCESSING

    def test_completed_requires_a_delivered_turn(self):
        # Fresh spawn with a completed-looking buffer is still IDLE (no turn
        # delivered yet).
        p = make_provider()
        assert p.get_status(OMP_COMPLETED_OUTPUT) is TerminalStatus.IDLE

        p.mark_input_received()
        assert p.get_status(OMP_COMPLETED_OUTPUT) is TerminalStatus.COMPLETED

    def test_processing_with_following_idle_is_not_processing(self):
        # A stale processing indicator followed by an idle prompt must not flip
        # to PROCESSING (position guard).
        p = make_provider()
        buffer = "omp Thinking…\n\nomp>\n"
        assert p.get_status(buffer) in {TerminalStatus.IDLE, TerminalStatus.COMPLETED}

    def test_waiting_user_answer(self):
        p = make_provider()
        assert p.get_status(OMP_WAITING_OUTPUT) is TerminalStatus.WAITING_USER_ANSWER

    def test_error_detected(self):
        p = make_provider()
        assert p.get_status(OMP_ERROR_OUTPUT) is TerminalStatus.ERROR

    def test_idle_pattern_for_log(self):
        p = make_provider()
        assert p.get_idle_pattern_for_log()  # non-empty pattern


class TestOmpCliExtraction:
    def test_extract_last_message_with_assistant_header(self):
        p = make_provider()
        output = """● Summarize this

 OMP: Here is the answer.

omp>
"""
        result = p.extract_last_message_from_script(output)
        assert "Here is the answer." in result

    def test_extract_ansi_stripped(self):
        p = make_provider()
        output = "● q\n OMP: \x1b[32mGreen answer\x1b[0m\nomp>\n"
        result = p.extract_last_message_from_script(output)
        assert "Green answer" in result
        assert "\x1b[" not in result

    def test_extract_raises_when_no_response(self):
        p = make_provider()
        with pytest.raises(ValueError):
            p.extract_last_message_from_script("omp>\n")


class TestOmpCliLifecycle:
    def test_paste_enter_count_is_one(self):
        assert make_provider().paste_enter_count == 1

    def test_exit_cli(self):
        assert make_provider().exit_cli() == "/exit"

    def test_mark_input_received_increments_turns(self):
        p = make_provider()
        assert p._turns == 0
        p.mark_input_received()
        assert p._turns == 1

    def test_cleanup_resets_initialized(self):
        p = make_provider()
        p._initialized = True
        p.cleanup()
        assert p._initialized is False

    def test_build_launch_command_requires_binary(self):
        p = make_provider()
        with patch("cli_agent_orchestrator.providers.omp_cli.shutil.which", return_value=None):
            with pytest.raises(ProviderError):
                p._build_launch_command()

    def test_build_launch_command_with_model(self):
        p = make_provider(model="gpt-5")
        with patch("cli_agent_orchestrator.providers.omp_cli.shutil.which", return_value="/usr/local/bin/omp"):
            cmd = p._build_launch_command()
        assert "omp" in cmd
        assert "--model" in cmd
        assert "gpt-5" in cmd

    def test_build_launch_command_without_model(self):
        p = make_provider()
        with patch("cli_agent_orchestrator.providers.omp_cli.shutil.which", return_value="/usr/local/bin/omp"):
            cmd = p._build_launch_command()
        assert "omp" in cmd
        assert "--model" not in cmd
