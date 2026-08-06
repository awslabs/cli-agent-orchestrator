"""Unit tests for the Muse Code CLI provider.

Covers command building (yolo + model/effort routing), TUI status detection
(⟩ idle prompt, ◆ response, in-flight processing, crash error), response
extraction, and provider-manager dispatch. No network, no real Muse Code.
"""

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.manager import ProviderManager
from cli_agent_orchestrator.providers.muse_cli import MuseCliProvider


def _provider(expected_model=None, expected_effort=None, agent_profile=None):
    return MuseCliProvider(
        terminal_id="term-1",
        session_name="sess",
        window_name="win",
        agent_profile=agent_profile,
        expected_model=expected_model,
        expected_effort=expected_effort,
    )


class TestBuildCommand:
    def test_yolo_and_model_and_effort(self):
        p = _provider(expected_model="muse-spark-1.2", expected_effort="high")
        cmd = p._build_command()
        assert cmd == "muse --yolo --model muse-spark-1.2 --reasoning-effort high"

    def test_contributor_model_wins_over_none(self):
        p = _provider(expected_model="muse-spark-1.2-contributor")
        assert "--model muse-spark-1.2-contributor" in p._build_command()

    def test_no_model_leaves_flag_out(self):
        p = _provider()
        assert p._build_command() == "muse --yolo"


class TestGetStatus:
    def test_idle_when_bare_prompt_and_no_task(self):
        assert _provider().get_status("Muse Code 0.1.0\n⟩\n") == TerminalStatus.IDLE

    def test_processing_when_spinner_present_even_with_prompt(self):
        # Empirically the ⟩ prompt stays rendered through a turn; the spinner
        # ("esc to interrupt") is the in-flight signal.
        p = _provider()
        p._has_received_input = True
        assert p.get_status("⟩\n◆ Thinking (2s · esc to interrupt)\n") == TerminalStatus.PROCESSING

    def test_processing_when_no_idle_prompt(self):
        assert _provider().get_status("Muse Code 0.1.0\nworking…") == TerminalStatus.PROCESSING

    def test_completed_after_task_with_idle_prompt(self):
        p = _provider()
        p._has_received_input = True
        assert p.get_status("⟩ q\n◆ hello\n⟩\n") == TerminalStatus.COMPLETED

    def test_error_on_crash_marker(self):
        assert _provider().get_status("muse: crash report written") == TerminalStatus.ERROR

    def test_empty_buffer_unknown(self):
        assert _provider().get_status("") == TerminalStatus.UNKNOWN


class TestExtract:
    def test_last_response_line(self):
        out = "⟩ q\n◆ first\n◆ second\n⟩\n"
        assert _provider().extract_last_message_from_script(out) == "second"

    def test_multi_line_reply_keeps_continuations(self):
        out = "⟩ q\n◆ LINE_ONE\n  LINE_TWO\n  LINE_THREE\n── Voice input ─\n⟩\n"
        assert _provider().extract_last_message_from_script(out) == "LINE_ONE\nLINE_TWO\nLINE_THREE"

    def test_spinner_line_is_not_a_reply(self):
        out = "◆ Thinking (3s · esc to interrupt)\n◆ real reply\n"
        assert _provider().extract_last_message_from_script(out) == "real reply"


class TestManagerDispatch:
    def test_create_provider_dispatch(self):
        manager = ProviderManager()
        provider = manager.create_provider(
            "muse_cli", "term-1", "sess", "win",
            agent_profile="implementer-muse",
            allowed_tools=["@builtin", "execute_bash"],
            expected_model="muse-spark-1.2",
        )
        assert isinstance(provider, MuseCliProvider)
