"""Reduced, synthetic payloads in the SGR layout of Cursor 2026.10.22."""

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import OutputExtractionRejected
from cli_agent_orchestrator.providers.cursor_cli import CursorCliProvider

WORKSPACE_TRUST_DIALOG = """╭──────────────────────────────────────────────────────╮
│ ⚠ Workspace Trust Required                           │
│                                                      │
│ Do you trust the contents of this directory?          │
│ /example/project                                     │
│                                                      │
│ ▶ [a] Trust this workspace                            │
│   [q] Quit                                            │
╰──────────────────────────────────────────────────────╯
"""


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


def test_legacy_separator_transcript_falls_through_panel_parser(provider):
    output = (
        "──────────────────────\n"
        "❯ First question\n"
        "──────────────────────\n"
        "First answer\n"
        "──────────────────────\n"
        "❯\n"
        "──────────────────────\n"
        "❯ Second question\n"
        "──────────────────────\n"
        "\x1b[32mSecond answer\x1b[0m\n"
        "──────────────────────\n"
        "❯\n"
    )
    assert provider._extract_panel_response(output) is None
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


@pytest.mark.parametrize("prefix", ["", "old-composer"])
@pytest.mark.parametrize("wrapped_question", [False, True])
def test_new_trust_dialog_blocks(provider, prefix, wrapped_question):
    dialog = WORKSPACE_TRUST_DIALOG
    if wrapped_question:
        dialog = dialog.replace(
            "Do you trust the contents of this directory?",
            "Do you trust the contents of │\n│ this directory?",
        )
    output = (composer() if prefix else "") + dialog
    assert provider.get_status(output) is TerminalStatus.WAITING_USER_ANSWER


def test_new_trust_dialog_requires_action_choices(provider):
    assert (
        provider.get_status(
            "⚠ Workspace Trust Required\nDo you trust the contents of this directory?"
        )
        is TerminalStatus.UNKNOWN
    )


@pytest.mark.parametrize(
    "phrase", ["Workspace Trust Required", "Do you trust the contents of this directory?"]
)
@pytest.mark.parametrize("location", ["answer", "user-echo"])
@pytest.mark.parametrize("processing", [False, True])
def test_trust_phrase_in_transcript_does_not_request_approval(
    provider, phrase, location, processing
):
    provider.mark_input_received()
    if location == "answer":
        output = user_panel("Explain workspace trust") + f"  The dialog says {phrase}\n"
    else:
        output = user_panel(f"Explain {phrase}") + "  This asks about workspace trust.\n"
    output += composer("Add a follow-up ctrl+c to stop" if processing else "Add a follow-up")
    expected = TerminalStatus.PROCESSING if processing else TerminalStatus.COMPLETED
    assert provider.get_status(output) is expected


@pytest.mark.parametrize(
    "phrase", ["Workspace Trust Required", "Do you trust the contents of this directory?"]
)
def test_standalone_trust_phrase_in_answer_is_not_a_dialog(provider, phrase):
    provider.mark_input_received()
    output = user_panel("Quote the dialog text") + f"  {phrase}\n" + composer()
    assert provider.get_status(output) is TerminalStatus.COMPLETED


@pytest.mark.parametrize("processing", [False, True])
def test_quoted_full_trust_dialog_before_composer_is_not_active(provider, processing):
    provider.mark_input_received()
    output = user_panel("Show the workspace trust dialog") + WORKSPACE_TRUST_DIALOG
    output += composer("Add a follow-up ctrl+c to stop" if processing else "Add a follow-up")
    expected = TerminalStatus.PROCESSING if processing else TerminalStatus.COMPLETED
    assert provider.get_status(output) is expected


def test_resolved_startup_trust_does_not_mask_ready_frame(provider):
    provider.mark_input_received()
    frame = WORKSPACE_TRUST_DIALOG + "Cursor Agent\nv2026.10.22\n" + composer()
    assert provider.get_status(frame) is TerminalStatus.COMPLETED


def test_current_approval_above_composer_still_blocks(provider):
    frame = "Cursor Agent\nv2026.10.22\nApprove this action\n" + composer()
    assert provider.get_status(frame) is TerminalStatus.WAITING_USER_ANSWER


def test_no_raw_transcript_fallback(provider):
    assert provider.allow_raw_transcript_fallback is False
