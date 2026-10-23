"""KiroCliProvider.extract_current_composer (issue #78, PR #1004 review P2-1).

The inbox recovery can only send the bare Enter that fixes a swallowed submit
when the provider can read its own composer. These run the reader against real
``tmux capture-pane`` snapshots of the Kiro TUI holding an unsubmitted paste
(see ``fixtures/COMPOSER_FIXTURES.md``) and against the existing TUI fixtures.
"""

from pathlib import Path

import pytest

from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

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
    # The captures were taken with the default agent; the reader must not
    # depend on the profile name in the header row.
    return KiroCliProvider("term-1", "session", "window", "developer")


@pytest.mark.parametrize(
    "fixture, expected",
    [
        ("kiro_cli_tui_composer_single_line.txt", MSG1),
        ("kiro_cli_tui_composer_wrapped.txt", f"{MSG2_ROW1}\n{MSG2_ROW2}"),
        (
            "kiro_cli_tui_composer_multi_line.txt",
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
        # "›  ask a question or describe a task ↵" placeholder.
        "kiro_cli_tui_2_25_idle_output.txt",
        "kiro_cli_tui_2_25_completed_output.txt",
        # Earlier idle prompts are echoed in the transcript; only the last one counts.
        "kiro_cli_tui_2_25_multiturn_output.txt",
        # "›  Kiro is working · 10s · Type to steer · Ctrl+S to queue" is status, not text.
        "kiro_cli_tui_2_25_processing_output.txt",
    ],
)
def test_placeholder_and_status_rows_read_as_an_empty_composer(provider, fixture):
    pane = strip_terminal_escapes((FIXTURES / fixture).read_text())
    assert provider.extract_current_composer(pane) == ""


@pytest.mark.parametrize(
    "fixture",
    [
        # Legacy UI "[developer]>" prompt: no › row, the reader does not claim to know.
        "kiro_cli_idle_output.txt",
        "kiro_cli_completed_output.txt",
        # Older TUI frame without the › glyph.
        "kiro_cli_tui_idle_output.txt",
    ],
)
def test_frames_without_the_tui_prompt_row_are_unknown(provider, fixture):
    pane = strip_terminal_escapes((FIXTURES / fixture).read_text())
    assert provider.extract_current_composer(pane) is None


def test_boot_screen_queue_hint_is_not_text(provider):
    pane = (
        "──────────────────────────────────────────────\n"
        "Midway: 19h 43m\n"
        "›  Initializing · type to queue a message\n"
        "                               /copy to clipboard\n"
    )
    assert provider.extract_current_composer(pane) == ""


def test_transcript_prompt_echo_before_the_last_separator_is_not_the_composer(provider):
    pane = (
        "› old message already submitted\n"
        "• reply\n"
        "──────────────────────────────────────────────\n"
        "kiro_default · auto · ◔ 14% · auth ok\n"
        "\n"
        "› new text sitting unsubmitted\n"
    )
    assert provider.extract_current_composer(pane) == "new text sitting unsubmitted"
