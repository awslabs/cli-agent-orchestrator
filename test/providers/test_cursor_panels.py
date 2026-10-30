"""Reduced, synthetic payloads in the SGR layout of Cursor 2026.10.22."""

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import OutputExtractionRejected
from cli_agent_orchestrator.providers.cursor_cli import CursorCliProvider


def user_panel(text):
    return f" \x1b[48;2;36;36;40m {text} \x1b[49m\n"


def composer(text="Add a follow-up"):
    return f" \x1b[48;2;21;21;21m → {text} \x1b[49m\n Run Everything\n"


@pytest.fixture
def provider():
    return CursorCliProvider("cursor-panels", "session", "window")


def test_extract_latest_panel_answer(provider):
    output = user_panel("First question") + "  First answer\n" + composer()
    output += user_panel("Second question") + "  Second answer\n" + composer()
    assert provider.extract_last_message_from_script(output) == "Second answer"


def test_composer_panel_padding_is_not_part_of_answer(provider):
    output = user_panel("Question") + "  Answer\n"
    output += " \x1b[48;2;21;21;21m composer padding \x1b[49m\n" + composer()
    assert provider.extract_last_message_from_script(output) == "Answer"


def test_strip_tool_badge_preserve_answer_indentation(provider):
    output = user_panel("Read example.txt")
    output += "  \x1b[2m\x1b[37m \x1b[0m \x1b[1mRead\x1b[0m \x1b[2mexample.txt\x1b[0m\n"
    output += "  Result:\n      indented value\n" + composer()
    assert provider.extract_last_message_from_script(output) == "Result:\n    indented value"


def test_echo_alone_is_not_an_answer(provider):
    with pytest.raises(OutputExtractionRejected):
        provider.extract_last_message_from_script(user_panel("Echo this") + composer())


@pytest.mark.parametrize("hint", ["  ctrl+c to stop", "\n ctrl+c to stop"])
def test_busy_composer_cannot_publish_partial_answer(provider, hint):
    with pytest.raises(OutputExtractionRejected):
        provider.extract_last_message_from_script(
            user_panel("Question") + "  Partial answer\n" + composer("Add a follow-up" + hint)
        )


def test_missing_composer_requests_more_history(provider):
    with pytest.raises(ValueError):
        provider.extract_last_message_from_script(user_panel("Question") + "  Partial answer\n")


def test_new_trust_dialog_blocks(provider):
    assert (
        provider.get_status(
            "Workspace Trust Required\nDo you trust the contents of this directory?"
        )
        is TerminalStatus.WAITING_USER_ANSWER
    )


def test_resolved_startup_trust_does_not_mask_ready_frame(provider):
    provider.mark_input_received()
    frame = "Workspace Trust Required\nCursor Agent\nv2026.10.22\n" + composer()
    assert provider.get_status(frame) is TerminalStatus.COMPLETED


def test_current_approval_above_composer_still_blocks(provider):
    frame = "Cursor Agent\nv2026.10.22\nApprove this action\n" + composer()
    assert provider.get_status(frame) is TerminalStatus.WAITING_USER_ANSWER


def test_no_raw_transcript_fallback(provider):
    assert provider.allow_raw_transcript_fallback is False
