"""Kimi Code 2.3 submission/trust changes and pre-spinner buffer eviction.

The SGR and chrome below are reduced from Kimi Code 2.3.3 captures. Payloads
and paths are synthetic; no account, workspace, or conversation is recorded.
"""

import io
import time
from unittest.mock import Mock

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers import kimi_cli as kimi_module
from cli_agent_orchestrator.providers import kimi_transcript as kt
from cli_agent_orchestrator.providers.base import OutputExtractionRejected
from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider, ProviderError

ECHO = " \x1b[38;5;222m❯ Read example.txt.\x1b[39m\n"
ANSWER = " \x1b[38;5;253m● \x1b[39mThe value is green.\n"
TOOL = " \x1b[38;5;253m● \x1b[1m\x1b[38;5;111mUsed Read\x1b[0m (example.txt) · 1 line\n"
FOOTER = "\n ╭────────────╮\n │ >          │\n ╰────────────╯\ncontext: 5% (100/2000)\n"
TRUST = """────────────────────────────────────────────────────
  Trust this folder?
  ↑↓ navigate · Enter select · Esc exit

  /example/project

  No project integrations or instructions to activate.

  Trust is remembered for this folder, including future project config changes.
  Tool approvals follow your permission settings.

   ❯ Trust and continue
     Exit
────────────────────────────────────────────────────
"""


@pytest.fixture
def provider(monkeypatch):
    # Retain generation bytes in memory, independent of tempfile cleanup.
    monkeypatch.setattr(kimi_module.tempfile, "SpooledTemporaryFile", lambda **_: io.StringIO())
    instance = KimiCliProvider("kimi-23", "session", "window")
    instance.restore_runtime_variant("code")
    return instance


@pytest.mark.parametrize("glyph", ["❯", "✨", "💫"])
def test_styled_submission_starts_a_turn(glyph):
    assert kt.is_user_input_start(
        f"\x1b[38;5;222m{glyph} request\x1b[39m", semantics=kt.SpinnerSemantics.CODE
    )


@pytest.mark.parametrize("text", ["Trust and continue", "Exit"])
def test_trust_choice_is_not_a_submission_but_same_text_can_be_a_prompt(text):
    assert not kt.is_user_input_start(f"❯ {text}", semantics=kt.SpinnerSemantics.CODE)
    assert not kt.is_user_input_start(
        f"\x1b[38;5;111m❯ {text}\x1b[39m", semantics=kt.SpinnerSemantics.CODE
    )
    assert kt.is_user_input_start(
        f"\x1b[38;5;222m❯ {text}\x1b[39m", semantics=kt.SpinnerSemantics.CODE
    )


def test_latest_answer_excludes_prior_turn_and_echo(provider):
    transcript = ECHO + ANSWER + FOOTER + ECHO + ANSWER.replace("green", "blue") + FOOTER
    assert provider.extract_last_message_from_script(transcript) == "● The value is blue."


def test_tool_only_turn_cannot_publish_previous_answer(provider):
    with pytest.raises(OutputExtractionRejected):
        provider.extract_last_message_from_script(ECHO + ANSWER + FOOTER + ECHO + TOOL + FOOTER)


def test_new_trust_labels_are_normalized():
    dialog = kt.detect_trust_dialog(TRUST.splitlines())
    assert dialog is not None
    assert dialog.workspace == "/example/project"
    assert dialog.selected_option == kt.TRUST_OPTION_TRUST
    assert dialog.options == [kt.TRUST_OPTION_TRUST, kt.TRUST_OPTION_REJECT]


@pytest.mark.asyncio
async def test_new_trust_dialog_still_requires_explicit_opt_in(provider, monkeypatch):
    monkeypatch.delenv("CAO_KIMI_CODE_TRUST_WORKSPACE", raising=False)
    backend = Mock()
    monkeypatch.setattr(kimi_module, "get_backend", lambda: backend)
    with pytest.raises(ProviderError, match="will not answer it"):
        await provider._handle_trust_dialog(TRUST)
    backend.send_special_key.assert_not_called()


def replay(provider, raw, epoch=1):
    buffer = ""
    for start in range(0, len(raw), 4096):
        chunk = raw[start : start + 4096]
        provider.record_status_chunk(chunk, epoch)
        buffer += chunk
        provider.observe_execution_output(buffer, epoch, truncated=len(buffer) > 32768)
        buffer = buffer[-32768:]


def begin(provider):
    provider.mark_input_received()
    provider._last_dispatch_time = time.time() - 20
    provider.notify_status_buffer_reset(1)


def test_activity_after_large_prompt_is_not_permanently_ambiguous(provider):
    begin(provider)
    replay(provider, ECHO + " \x1b[38;5;222m" + "long prompt " * 4000 + "\x1b[39m\n")
    assert not provider._execution_observed
    assert provider.execution_evidence_ambiguous
    replay(provider, "\n⠋ Thinking…\n")
    assert provider._execution_observed
    assert not provider.execution_evidence_ambiguous
    assert provider._new_tui_ready_status() is TerminalStatus.COMPLETED


def test_evicted_fence_still_owns_quoted_spinner(provider):
    begin(provider)
    replay(provider, ECHO + ANSWER + "```text\n" + "quoted content\n" * 3000)
    replay(provider, "⠋ Thinking…\n```\n" + FOOTER)
    assert not provider._execution_observed
    assert provider._new_tui_ready_status() is TerminalStatus.PROCESSING


@pytest.mark.parametrize("failed", [False, True])
def test_incomplete_or_failed_spool_cannot_clear_ambiguity(provider, failed):
    begin(provider)
    provider._swarm_stream = io.StringIO("⠋ Thinking…\n")
    provider._swarm_stream_complete = failed
    provider._swarm_storage_failed = failed
    provider.execution_evidence_ambiguous = True
    provider.observe_execution_output("⠋ Thinking…\n", 1, truncated=True)
    assert not provider._execution_observed
    assert provider.execution_evidence_ambiguous


def test_previous_generation_cannot_accept_new_turn(provider):
    begin(provider)
    replay(provider, ECHO + "⠋ Thinking…\n", epoch=0)
    assert not provider._execution_observed


@pytest.mark.parametrize("screen_path", [False, True])
@pytest.mark.parametrize("final_answer", [False, True])
def test_completion_waits_for_answer_after_tool(provider, monkeypatch, screen_path, final_answer):
    begin(provider)
    replay(provider, ECHO + "⠋ Thinking…\n")
    capture = ECHO + TOOL + (ANSWER if final_answer else "") + FOOTER
    backend = Mock()
    backend.get_native_status.return_value = None
    backend.get_history.return_value = capture
    monkeypatch.setattr(kimi_module, "get_backend", lambda: backend)
    if screen_path:
        result = provider.get_status_from_screen(kt.strip_sgr(capture).splitlines())
    else:
        result = provider.get_status(capture)
    assert result is (TerminalStatus.COMPLETED if final_answer else TerminalStatus.PROCESSING)


def test_live_spinner_overrides_stale_composite_footer(provider, monkeypatch):
    begin(provider)
    replay(provider, ECHO + "⠋ Thinking…\n")
    backend = Mock()
    backend.get_history.return_value = "⠋ Thinking…\n" + FOOTER
    monkeypatch.setattr(kimi_module, "get_backend", lambda: backend)
    assert (
        provider.get_status_from_screen(kt.strip_sgr(ECHO + ANSWER + FOOTER).splitlines())
        is TerminalStatus.PROCESSING
    )


def test_completion_probe_failure_is_unknown(provider, monkeypatch):
    begin(provider)
    replay(provider, ECHO + "⠋ Thinking…\n")
    backend = Mock()
    backend.get_history.side_effect = RuntimeError("pane unavailable")
    monkeypatch.setattr(kimi_module, "get_backend", lambda: backend)
    assert (
        provider.get_status_from_screen(kt.strip_sgr(ECHO + ANSWER + FOOTER).splitlines())
        is TerminalStatus.UNKNOWN
    )


def test_bare_exit_in_answer_is_not_a_trust_dialog(provider):
    output = ECHO + ANSWER + "   Exit\n   More explanation.\n" + FOOTER
    assert "Exit\nMore explanation." in provider.extract_last_message_from_script(output)
