"""The §4.1 r11 execution-observation pins and the two-close observation.

Transport acceptance is not command execution: a declared command may
close ``accepted`` only when the provider/build-pinned execution signal
is observed above its pre-write baseline.  These tests pin the matchers
against the live-proven screen shapes (§10.3 evidence lane-a-10.3), the
stale-signal protection, and the three observation answers.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services import native_pane_input as npi
from cli_agent_orchestrator.services.control_input_contract import (
    SUBMISSION_SUBMITTED,
    SUBMISSION_UNKNOWN,
    SUBMISSION_UNSUBMITTED,
)

ESC = "\x1b"
RESET = f"{ESC}[0m"
DIM = f"{ESC}[2m"


def _kimi_screen(notice_rows):
    return [
        " ✨ Please reconsider this path",
        "   Interrupted by user",
        *notice_rows,
        " ╭────────────────────────────────────────────────────────────────────────────────────────────────╮",
        " │ >                                                                                              │",
        " ╰────────────────────────────────────────────────────────────────────────────────────────────────╯",
        " K2.7 Coding Highspeed thinking  /tmp/work  master",
        "                                                                          context: 10% (23.7k/256k)",
    ]


_KIMI_NOTICE = " ● Compacting context... · Tip: /goal next to queue follow-up work while the current goal keeps"


def _claude_screen(tail_rows):
    return [
        "⏺ Here is the response",
        f"{ESC}[38;2;136;136;136m────────────────────────{RESET}",
        *tail_rows,
        f"{ESC}[38;2;136;136;136m────────────────────────{RESET}",
        "❯ ",
        f"{ESC}[38;2;136;136;136m────────────────────────{RESET}",
        "  sonnet-5 │ xhigh │ /tmp/work │ master",
    ]


def _codex_screen(transcript_rows):
    return [
        *transcript_rows,
        "",
        f"{ESC}[1m›{RESET} {DIM}Explain this codebase{RESET}",
        "",
        f"  {DIM}gpt-5.6-terra xhigh · ~/project · branch{RESET}",
    ]


class TestExecutionPins:
    def test_the_pinned_builds_are_exactly_the_live_verified_ones(self):
        pin = npi.command_execution_pin_for("kimi_cli", "0.29.2")
        assert pin is not None and pin.rule == "kimi-compaction-signal" and not pin.styled
        assert pin.signal and pin.evidence
        pin = npi.command_execution_pin_for("claude_code", "2.1.220")
        assert pin is not None and pin.rule == "claude-command-echo-response" and pin.styled
        pin = npi.command_execution_pin_for("codex", "0.146.0")
        assert pin is not None and pin.rule == "codex-compaction-signal" and pin.styled

    def test_unpinned_builds_have_no_execution_pin(self):
        # Same honesty as the emptiness pins: no live evidence, no pin. The
        # text-proven 0.30.0 and 0.31.0 builds (cond-0310) and 0.32.0/0.33.0
        # (cond-0315) are still unpinned here — /compact execution-evidence
        # authority is live-verified on 0.29.2 only and is never inherited by
        # version-set membership.
        for build in ("0.29.0", "0.29.1", "0.30.0", "0.31.0", "0.32.0", "0.33.0"):
            assert npi.command_execution_pin_for("kimi_cli", build) is None, build
        assert npi.command_execution_pin_for("claude_code", "2.1.218") is None
        assert npi.command_execution_pin_for("codex", "0.145.0") is None
        assert npi.command_execution_pin_for(None, "0.29.2") is None

    def test_version_banners_normalize(self):
        assert npi.command_execution_pin_for("kimi_cli", "kimi 0.29.2") is not None
        assert npi.command_execution_pin_for("claude_code", "2.1.220 (Claude Code)") is not None
        assert npi.command_execution_pin_for("codex", "codex-cli 0.146.0") is not None


class TestSignalCount:
    def test_kimi_compaction_notice_is_counted_per_occurrence(self):
        pin = npi.command_execution_pin_for("kimi_cli", "0.29.2")
        assert npi._execution_signal_count(pin, _kimi_screen([_KIMI_NOTICE]), "/compact") == 1
        assert (
            npi._execution_signal_count(pin, _kimi_screen([_KIMI_NOTICE, _KIMI_NOTICE]), "/compact")
            == 2
        )
        assert npi._execution_signal_count(pin, _kimi_screen([]), "/compact") == 0

    def test_claude_echo_plus_response_row_is_one_pair(self):
        pin = npi.command_execution_pin_for("claude_code", "2.1.220")
        rows = _claude_screen(["❯ /compact", "  ⎿  Not enough messages to compact."])
        assert npi._execution_signal_count(pin, rows, "/compact") == 1

    def test_claude_echo_without_a_response_row_is_not_a_pair(self):
        pin = npi.command_execution_pin_for("claude_code", "2.1.220")
        assert npi._execution_signal_count(pin, _claude_screen(["❯ /compact"]), "/compact") == 0

    def test_claude_response_row_without_the_echo_is_not_a_pair(self):
        pin = npi.command_execution_pin_for("claude_code", "2.1.220")
        rows = _claude_screen(["  ⎿  Not enough messages to compact."])
        assert npi._execution_signal_count(pin, rows, "/compact") == 0

    def test_an_ordinary_prompt_echo_is_not_the_command_signal(self):
        pin = npi.command_execution_pin_for("claude_code", "2.1.220")
        rows = _claude_screen(["❯ hello there", "⏺ Here is the response"])
        assert npi._execution_signal_count(pin, rows, "/compact") == 0
        # And a different command's pair never answers for this command.
        rows = _claude_screen(["❯ /agents", "  ⎿  some other response"])
        assert npi._execution_signal_count(pin, rows, "/compact") == 0

    def test_two_pairs_are_two(self):
        pin = npi.command_execution_pin_for("claude_code", "2.1.220")
        rows = _claude_screen(
            [
                "❯ /compact",
                "  ⎿  Not enough messages to compact.",
                "❯ /compact",
                "  ⎿  Compacted.",
            ]
        )
        assert npi._execution_signal_count(pin, rows, "/compact") == 2

    def test_unparseable_styled_rows_count_nothing(self):
        pin = npi.command_execution_pin_for("claude_code", "2.1.220")
        assert npi._execution_signal_count(pin, ["❯ /compact" + ESC + "[38;2;1"], "/compact") == 0

    def test_codex_context_compacted_notice_is_counted_per_occurrence(self):
        pin = npi.command_execution_pin_for("codex", "0.146.0")
        notice = f"{DIM}• {RESET}Context compacted"
        assert npi._execution_signal_count(pin, _codex_screen([notice]), "/compact") == 1
        assert npi._execution_signal_count(pin, _codex_screen([notice, notice]), "/compact") == 2
        assert npi._execution_signal_count(pin, _codex_screen([]), "/compact") == 0

    def test_codex_busy_rejection_or_generic_working_is_not_completion(self):
        pin = npi.command_execution_pin_for("codex", "0.146.0")
        rows = _codex_screen(
            [
                "■ '/compact' is disabled while a task is in progress.",
                "• Working (9s • esc to interrupt)",
            ]
        )
        assert npi._execution_signal_count(pin, rows, "/compact") == 0


class TestObserveCommandExecution:
    def test_the_signal_above_baseline_closes_submitted_with_evidence(self):
        pin = npi.command_execution_pin_for("kimi_cli", "0.29.2")
        composer = npi.composer_emptiness_pin_for("kimi_cli", "0.29.2")
        observed, ref = npi.observe_command_execution(
            "%1",
            pin,
            command_text="/compact",
            composer_pin=composer,
            baseline_rows=_kimi_screen([]),
            screen=lambda: _kimi_screen([_KIMI_NOTICE]),
        )
        assert observed == SUBMISSION_SUBMITTED
        assert ref and ref.startswith("capture-pane:%1:")

    def test_a_stale_signal_from_an_earlier_command_never_closes(self):
        """The baseline count protection: a compaction notice already on
        screen before the write is not this command's execution."""
        pin = npi.command_execution_pin_for("kimi_cli", "0.29.2")
        composer = npi.composer_emptiness_pin_for("kimi_cli", "0.29.2")
        observed, ref = npi.observe_command_execution(
            "%1",
            pin,
            command_text="/compact",
            composer_pin=composer,
            baseline_rows=_kimi_screen([_KIMI_NOTICE]),
            deadline_monotonic=__import__("time").monotonic() + 0.1,
            screen=lambda: _kimi_screen([_KIMI_NOTICE]),
        )
        assert observed != SUBMISSION_SUBMITTED

    def test_a_second_identical_command_closes_on_the_new_occurrence(self):
        pin = npi.command_execution_pin_for("claude_code", "2.1.220")
        composer = npi.composer_emptiness_pin_for("claude_code", "2.1.220")
        baseline = _claude_screen(["❯ /compact", "  ⎿  Not enough messages to compact."])
        after = _claude_screen(
            [
                "❯ /compact",
                "  ⎿  Not enough messages to compact.",
                "❯ /compact",
                "  ⎿  Compacted.",
            ]
        )
        observed, ref = npi.observe_command_execution(
            "%1",
            pin,
            command_text="/compact",
            composer_pin=composer,
            baseline_rows=baseline,
            screen=lambda: after,
        )
        assert observed == SUBMISSION_SUBMITTED
        assert ref

    def test_an_expired_window_with_content_resting_is_unsubmitted(self):
        import time

        pin = npi.command_execution_pin_for("kimi_cli", "0.29.2")
        composer = npi.composer_emptiness_pin_for("kimi_cli", "0.29.2")

        def screen():
            rows = _kimi_screen([])
            rows[3] = (
                " │ > /compact                                                                                     │"
            )
            return rows

        observed, ref = npi.observe_command_execution(
            "%1",
            pin,
            command_text="/compact",
            composer_pin=composer,
            baseline_rows=_kimi_screen([]),
            deadline_monotonic=time.monotonic() + 0.1,
            screen=screen,
        )
        assert observed == SUBMISSION_UNSUBMITTED
        assert ref and ref.startswith("capture-pane:%1:")

    def test_an_expired_window_without_content_or_signal_is_unknown(self):
        import time

        pin = npi.command_execution_pin_for("kimi_cli", "0.29.2")
        composer = npi.composer_emptiness_pin_for("kimi_cli", "0.29.2")
        observed, ref = npi.observe_command_execution(
            "%1",
            pin,
            command_text="/compact",
            composer_pin=composer,
            baseline_rows=_kimi_screen([]),
            deadline_monotonic=time.monotonic() + 0.1,
            screen=lambda: _kimi_screen([]),
        )
        assert observed == SUBMISSION_UNKNOWN
        assert ref is None

    def test_claude_signal_above_baseline_closes_submitted(self):
        pin = npi.command_execution_pin_for("claude_code", "2.1.220")
        composer = npi.composer_emptiness_pin_for("claude_code", "2.1.220")
        observed, ref = npi.observe_command_execution(
            "%1",
            pin,
            command_text="/compact",
            composer_pin=composer,
            baseline_rows=_claude_screen([]),
            screen=lambda: _claude_screen(["❯ /compact", "  ⎿  Not enough messages to compact."]),
        )
        assert observed == SUBMISSION_SUBMITTED
        assert ref

    def test_codex_notice_above_baseline_closes_submitted(self):
        pin = npi.command_execution_pin_for("codex", "0.146.0")
        composer = npi.composer_emptiness_pin_for("codex", "0.146.0")
        observed, ref = npi.observe_command_execution(
            "%1",
            pin,
            command_text="/compact",
            composer_pin=composer,
            baseline_rows=_codex_screen([]),
            screen=lambda: _codex_screen([f"{DIM}• {RESET}Context compacted"]),
        )
        assert observed == SUBMISSION_SUBMITTED
        assert ref


class TestTheDeadlineBoundaryIsAuthoritative:
    """The r11 bounded window: a capture completing after the deadline
    proves nothing, and its signal can never close accepted — even when
    it shows a brand-new occurrence."""

    def test_a_late_capture_with_a_new_signal_never_closes_submitted(self):
        import time

        pin = npi.command_execution_pin_for("kimi_cli", "0.29.2")
        composer = npi.composer_emptiness_pin_for("kimi_cli", "0.29.2")
        deadline = time.monotonic() + 0.05

        def late_capture():
            # The floored capture timeout lets a capture begun just before
            # expiry return after it; the boundary must reject the signal.
            time.sleep(0.15)
            return _kimi_screen([_KIMI_NOTICE])

        observed, ref = npi.observe_command_execution(
            "%1",
            pin,
            command_text="/compact",
            composer_pin=composer,
            baseline_rows=_kimi_screen([]),
            deadline_monotonic=deadline,
            screen=late_capture,
        )
        assert observed != SUBMISSION_SUBMITTED

    def test_a_signal_seen_only_after_expiry_closes_unknown(self):
        import time

        pin = npi.command_execution_pin_for("kimi_cli", "0.29.2")
        composer = npi.composer_emptiness_pin_for("kimi_cli", "0.29.2")
        deadline = time.monotonic() + 0.05

        def late_capture():
            time.sleep(0.15)
            return _kimi_screen([_KIMI_NOTICE])

        observed, ref = npi.observe_command_execution(
            "%1",
            pin,
            command_text="/compact",
            composer_pin=composer,
            baseline_rows=_kimi_screen([]),
            deadline_monotonic=deadline,
            screen=late_capture,
        )
        # The composer is empty in the late capture (no resting text), so
        # the close is unknown — never the late signal, and no evidence.
        assert observed == SUBMISSION_UNKNOWN
        assert ref is None

    def test_a_late_capture_still_informs_the_resting_text_check(self):
        """A late capture never accepts, but the resting-text fact it
        carries may still be recorded honestly as unsubmitted (an
        ambiguous close either way — it licenses nothing)."""
        import time

        pin = npi.command_execution_pin_for("kimi_cli", "0.29.2")
        composer = npi.composer_emptiness_pin_for("kimi_cli", "0.29.2")
        deadline = time.monotonic() + 0.05

        def late_capture():
            time.sleep(0.15)
            rows = _kimi_screen([_KIMI_NOTICE])
            rows[4] = (
                " │ > /compact                                                                                     │"
            )
            return rows

        observed, ref = npi.observe_command_execution(
            "%1",
            pin,
            command_text="/compact",
            composer_pin=composer,
            baseline_rows=_kimi_screen([]),
            deadline_monotonic=deadline,
            screen=late_capture,
        )
        assert observed == SUBMISSION_UNSUBMITTED
        assert ref is not None
