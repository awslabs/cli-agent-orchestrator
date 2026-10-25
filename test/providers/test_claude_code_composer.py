"""ClaudeCodeProvider.extract_current_composer (issue #78, PR #1004 review P2-1).

The inbox recovery can only send the bare Enter that fixes a swallowed submit
when the provider can read its own composer. These run the reader against real
``tmux capture-pane`` snapshots of Claude Code holding an unsubmitted paste
(see ``fixtures/COMPOSER_FIXTURES.md``) and against the existing idle fixtures.
"""

from pathlib import Path

import pytest

from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

FIXTURES = Path(__file__).parent / "fixtures"

MSG1 = "MSG1: refactor module A so the retry policy lives in one place"
MSG2_ROW1 = (
    "MSG2: write tests for module B covering the retry policy, the backoff schedule, "
    "the jitter bounds, the max-attempts ceiling, the circuit breaker"
)
MSG2_ROW2 = (
    "reset, and the metrics emitted on every attempt so the dashboard can show failures per minute"
)


@pytest.fixture
def provider():
    return ClaudeCodeProvider("term-1", "session", "window")


@pytest.mark.parametrize(
    "fixture, expected",
    [
        ("claude_code_composer_single_line.txt", MSG1),
        ("claude_code_composer_wrapped.txt", f"{MSG2_ROW1}\n{MSG2_ROW2}"),
        (
            "claude_code_composer_multi_line.txt",
            "Run the migration once.\nThen report the row counts.\nDo not retry on failure.",
        ),
    ],
)
def test_reads_the_unsubmitted_paste_from_a_real_capture(provider, fixture, expected):
    pane = (FIXTURES / fixture).read_text()
    assert provider.extract_current_composer(pane) == expected


@pytest.mark.parametrize(
    "fixture",
    [
        # Placeholder text in the box: ❯ Try "write a test for <filepath>"
        "claude_code_865_startup_idle.txt",
        # Bare ❯ after a finished turn.
        "claude_code_865_turn_ended.txt",
        "claude_code_865_short_turn_ended.txt",
    ],
)
def test_an_empty_box_reads_as_an_empty_composer(provider, fixture):
    from cli_agent_orchestrator.utils.text import strip_terminal_escapes

    pane = strip_terminal_escapes((FIXTURES / fixture).read_text())
    assert provider.extract_current_composer(pane) == ""


def test_no_input_box_means_the_composer_is_unknown(provider):
    # Mid-turn frames (and anything that is not the boxed prompt) must not be
    # mistaken for a composer: transcript text is not an input boundary.
    assert provider.extract_current_composer("⏺ Working on it\n  some output\n") is None
    assert provider.extract_current_composer("") is None
    # One rail with a quoted "> " line under it is the launch echo, not the box.
    assert provider.extract_current_composer("────────────\n> system prompt text\n") is None


def test_transcript_prompt_echo_above_the_box_is_not_the_composer(provider):
    # An earlier message echoed in the transcript sits ABOVE the box's top rail;
    # only what is between the two rails is the live composer.
    pane = (
        "❯ old message that was already submitted\n"
        "⏺ Done.\n"
        "────────────────────────────────────────\n"
        "❯\xa0\n"
        "────────────────────────────────────────\n"
        "  ⏵⏵ auto mode on\n"
    )
    assert provider.extract_current_composer(pane) == ""
