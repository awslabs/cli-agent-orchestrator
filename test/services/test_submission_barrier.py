"""Tests for the provider-pinned submission barrier (cond-0026).

The barrier is the seam between "tmux acknowledged the bytes" and "the
provider visibly took the control".  These tests pin its two halves: the
compose-visibility classifier, which decides whether the composer is
holding the control text, and the bounded polls that walk a Codex control
across the text -> Enter -> observation sequence without ever sending a
second, blind Enter.
"""

from __future__ import annotations

import time

import pytest

from cli_agent_orchestrator.services import native_pane_input
from cli_agent_orchestrator.services.native_pane_input import (
    NativePaneInputUnavailable,
    SubmissionBarrier,
    await_compose_visible,
    composed_text_visible,
    observe_submission,
    submission_barrier_for,
)

TAIL = 6


def _screen(*rows: str) -> list[str]:
    return [f"transcript row {index}" for index in range(10)] + list(rows)


def _fast_barrier(**overrides) -> SubmissionBarrier:
    fields = {
        "compose_settle_seconds": 0.2,
        "post_enter_seconds": 0.2,
        "poll_interval_seconds": 0.01,
        "composer_tail_rows": TAIL,
    }
    fields.update(overrides)
    return SubmissionBarrier(**fields)


class TestBarrierPinning:
    def test_codex_has_a_pinned_barrier(self):
        barrier = submission_barrier_for("codex")
        assert barrier is not None
        assert barrier.compose_settle_seconds > 0
        assert barrier.post_enter_seconds > 0
        assert barrier.poll_interval_seconds > 0
        assert barrier.composer_tail_rows > 0

    def test_kimi_has_a_pinned_barrier(self):
        barrier = submission_barrier_for("kimi_cli")
        assert barrier is not None
        assert barrier.compose_settle_seconds > 0
        assert barrier.post_enter_seconds > 0
        assert barrier.poll_interval_seconds > 0
        assert barrier.composer_tail_rows == 5

    def test_unpinned_provider_keeps_fused_submit(self):
        assert submission_barrier_for("claude_code") is None

    def test_an_unknown_or_absent_provider_has_no_barrier(self):
        assert submission_barrier_for(None) is None
        assert submission_barrier_for("some_future_provider") is None


class TestComposedTextVisible:
    def test_text_in_the_composer_region_is_visible(self):
        rows = _screen("│ > /compact                                                    │")
        assert composed_text_visible(rows, "/compact", composer_tail_rows=TAIL)

    def test_text_above_the_composer_region_is_not_visible(self):
        # A transcript echo of an earlier copy renders above the composer
        # box; the region cut is what stops it counting as composed text.
        rows = ["/compact"] + [f"row {index}" for index in range(12)]
        assert not composed_text_visible(rows, "/compact", composer_tail_rows=TAIL)

    def test_a_wrapped_composer_line_still_counts(self):
        text = "please summarise the campaign state"
        rows = _screen(
            "│ > please summarise the                                       │",
            "│   campaign state                                             │",
        )
        assert composed_text_visible(rows, text, composer_tail_rows=TAIL)

    def test_a_long_control_matches_on_its_tail(self):
        # Longer than the composer is tall: the box's top rows scroll out
        # of the region, but the wrap's last line carries the ending.
        text = "x" * 200 + "UNIQUE-ENDING"
        rows = _screen("│   " + "x" * 40 + "UNIQUE-ENDING" + " " * 10 + "│")
        assert composed_text_visible(rows, text, composer_tail_rows=TAIL)

    def test_whitespace_reflow_does_not_hide_the_text(self):
        rows = _screen("│ > /model    opus                                             │")
        assert composed_text_visible(rows, "/model opus", composer_tail_rows=TAIL)

    def test_an_empty_capture_is_not_visible(self):
        assert not composed_text_visible([], "/compact", composer_tail_rows=TAIL)

    def test_a_different_text_is_not_visible(self):
        rows = _screen("│ > /compact                                                    │")
        assert not composed_text_visible(rows, "/clear", composer_tail_rows=TAIL)


class TestAwaitComposeVisible:
    def test_text_visible_on_the_first_poll_settles_immediately(self):
        rows = _screen("│ > /compact │")
        assert await_compose_visible("%1", "/compact", barrier=_fast_barrier(), screen=lambda: rows)

    def test_text_that_never_appears_fails_the_settle(self):
        rows = _screen("│ > │")
        started = time.monotonic()
        assert not await_compose_visible(
            "%1", "/compact", barrier=_fast_barrier(), screen=lambda: rows
        )
        assert time.monotonic() - started >= 0.2

    def test_a_transient_capture_failure_is_retried(self):
        rows = _screen("│ > /compact │")
        calls = {"count": 0}

        def flaky():
            calls["count"] += 1
            if calls["count"] == 1:
                raise NativePaneInputUnavailable("tmux hiccup")
            return rows

        assert await_compose_visible("%1", "/compact", barrier=_fast_barrier(), screen=flaky)

    def test_the_write_deadline_shortens_the_settle(self):
        rows = _screen("│ > │")
        deadline = time.monotonic() + 0.05
        assert not await_compose_visible(
            "%1",
            "/compact",
            barrier=_fast_barrier(compose_settle_seconds=30.0),
            deadline_monotonic=deadline,
            screen=lambda: rows,
        )
        assert time.monotonic() - deadline < 0.5


class TestObserveSubmission:
    def test_composer_cleared_is_submitted_with_evidence(self):
        composed = _screen("│ > /compact │")
        cleared = _screen("│ > │")
        calls = {"count": 0}

        def clearing():
            calls["count"] += 1
            return composed if calls["count"] == 1 else cleared

        observed, evidence = observe_submission(
            "%1", "/compact", barrier=_fast_barrier(), screen=clearing
        )
        assert observed == "submitted"
        assert evidence is not None and evidence.startswith("capture-pane:%1:")

    def test_text_persisting_through_the_window_is_unsubmitted(self):
        composed = _screen("│ > /compact │")
        observed, evidence = observe_submission(
            "%1", "/compact", barrier=_fast_barrier(), screen=lambda: composed
        )
        assert observed == "unsubmitted"
        assert evidence is not None and evidence.startswith("capture-pane:%1:")

    def test_a_window_where_every_capture_fails_is_unknown(self):
        def blind():
            raise NativePaneInputUnavailable("pane gone")

        observed, evidence = observe_submission(
            "%1", "/compact", barrier=_fast_barrier(), screen=blind
        )
        assert observed == "unknown"
        assert evidence is None

    def test_a_deadline_cut_window_is_unknown_not_unsubmitted(self):
        # The text was still visible, but the window was cut short: no
        # positive "it persisted" claim may be made from a partial watch.
        composed = _screen("│ > /compact │")
        observed, evidence = observe_submission(
            "%1",
            "/compact",
            barrier=_fast_barrier(post_enter_seconds=30.0),
            deadline_monotonic=time.monotonic() + 0.05,
            screen=lambda: composed,
        )
        assert observed == "unknown"
        assert evidence is None

    def test_evidence_references_the_decisive_capture(self):
        import hashlib

        cleared = _screen("│ > │")
        observed, evidence = observe_submission(
            "%9", "/compact", barrier=_fast_barrier(), screen=lambda: cleared
        )
        assert observed == "submitted"
        digest = hashlib.sha256("\n".join(cleared).encode("utf-8")).hexdigest()[:16]
        assert evidence is not None and evidence.endswith(f"sha256:{digest}")
