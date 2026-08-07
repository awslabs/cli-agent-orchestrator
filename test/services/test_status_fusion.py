"""Status fusion — the join no single signal can make.

The measured fleet condition this exists for: 36 terminals, 15 ``dead``, 20
``not_fifo_monitored``, one actionable status. Two of the 20 had shown a
*processing* screen for three and a half days while rendering nothing new.

The tests that matter most are therefore the ones pinning that neither half of
the wedge join can be dropped, and that every absent signal degrades the answer
instead of being guessed at.
"""

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import status_fusion as sf


def _sig(name, value=None, state="available", detail=None):
    return sf.Signal(name, value=value, state=state, detail=detail)


def _fuse(**kw):
    base = dict(
        lifecycle="live",
        fifo=_sig("fifo", state="absent"),
        screen=_sig("screen", state="absent"),
        liveness=_sig("liveness", state="absent"),
        activity=_sig("activity", state="absent"),
    )
    base.update(kw)
    return sf.fuse(**base)


class TestLifecyclePrecedence:
    """A pane that is gone has no provider state to report."""

    @pytest.mark.parametrize("lifecycle", ["dead", "superseded", "unknown-liveness"])
    def test_non_live_lifecycle_answers_by_itself(self, lifecycle):
        out = _fuse(
            lifecycle=lifecycle,
            screen=_sig("screen", TerminalStatus.PROCESSING),
        )
        assert out.status == TerminalStatus.UNKNOWN
        assert out.confidence == "high"
        assert lifecycle in out.reason
        assert out.wedged is False

    def test_a_stale_screen_cannot_resurrect_a_dead_pane(self):
        # capture-pane against a dead pane can still return the last frame the
        # emulator held; believing it would report a worker that is not there.
        out = _fuse(
            lifecycle="dead",
            screen=_sig("screen", TerminalStatus.PROCESSING),
            fifo=_sig("fifo", TerminalStatus.PROCESSING),
        )
        assert out.status == TerminalStatus.UNKNOWN


class TestWedgeJoin:
    """`processing` on screen, contradicted by two independent quiet clocks."""

    def _wedge(self, **kw):
        base = dict(
            screen=_sig("screen", TerminalStatus.PROCESSING),
            liveness=_sig("liveness", 3 * 86400),
            activity=_sig("activity", 3 * 86400),
        )
        base.update(kw)
        return _fuse(**base)

    def test_the_measured_case_is_detected(self):
        out = self._wedge()
        assert out.wedged is True
        assert out.confidence == "high"
        assert "contradict" in out.reason

    def test_liveness_alone_is_not_enough(self):
        # All 15 live panes were byte-identical over 60s. A quiet pane whose
        # screen says idle is simply idle, and calling that wedged would
        # accuse the entire fleet.
        out = self._wedge(screen=_sig("screen", TerminalStatus.IDLE))
        assert out.wedged is False
        assert out.status == TerminalStatus.IDLE

    def test_activity_alone_is_not_enough(self):
        # A legitimately parked worker is inactive for days without being wedged.
        out = self._wedge(liveness=_sig("liveness", state="absent"))
        assert out.wedged is False
        assert out.status == TerminalStatus.PROCESSING

    def test_a_recently_changed_pane_is_not_wedged(self):
        out = self._wedge(liveness=_sig("liveness", 0))
        assert out.wedged is False
        assert out.status == TerminalStatus.PROCESSING

    def test_thresholds_are_generous_by_default(self):
        # A model can think without emitting. Ten quiet minutes must not
        # invite an operator to kill live work.
        out = self._wedge(liveness=_sig("liveness", 600), activity=_sig("activity", 600))
        assert out.wedged is False

    def test_both_clocks_must_agree(self):
        # Quiet screen but recent activity: something is driving the terminal
        # even if the viewport has not repainted.
        out = self._wedge(activity=_sig("activity", 60))
        assert out.wedged is False

    def test_a_fifo_working_claim_can_also_be_contradicted(self):
        out = self._wedge(
            screen=_sig("screen", state="absent"), fifo=_sig("fifo", TerminalStatus.PROCESSING)
        )
        assert out.wedged is True

    def test_thresholds_are_injectable(self):
        out = self._wedge(
            liveness=_sig("liveness", 90),
            activity=_sig("activity", 90),
            wedged_quiet_seconds=60,
            wedged_inactive_seconds=60,
        )
        assert out.wedged is True


class TestClassifierPrecedence:
    def test_fifo_beats_screen(self):
        # The stream detector sees bytes rather than a composited frame, so a
        # redraw cannot have evicted the marker it needed.
        out = _fuse(
            fifo=_sig("fifo", TerminalStatus.IDLE), screen=_sig("screen", TerminalStatus.COMPLETED)
        )
        assert out.status == TerminalStatus.IDLE
        assert "output stream" in out.reason

    def test_screen_is_used_when_no_fifo_exists(self):
        # The whole point: native TUIs have no FIFO by construction.
        out = _fuse(screen=_sig("screen", TerminalStatus.COMPLETED))
        assert out.status == TerminalStatus.COMPLETED
        assert out.confidence == "medium"

    def test_a_corroborating_liveness_sample_raises_confidence(self):
        out = _fuse(screen=_sig("screen", TerminalStatus.COMPLETED), liveness=_sig("liveness", 120))
        assert out.confidence == "high"

    def test_nothing_available_is_not_fifo_monitored_not_a_guess(self):
        out = _fuse()
        assert out.status == TerminalStatus.NOT_FIFO_MONITORED
        assert out.confidence == "none"

    def test_an_unreadable_classifier_is_unknown_not_unmonitored(self):
        # `absent` is architecture, `unreadable` is a fault, and an operator
        # must be able to tell them apart.
        out = _fuse(screen=_sig("screen", state="unreadable"))
        assert out.status == TerminalStatus.UNKNOWN
        assert out.confidence == "none"
        assert "failed" in out.reason

    def test_every_signal_is_carried_into_the_result(self):
        out = _fuse(screen=_sig("screen", TerminalStatus.IDLE))
        assert {s.name for s in out.signals} == {"fifo", "screen", "liveness", "activity"}
        assert out.to_dict()["status"] == "idle"


class _Provider:
    supports_screen_detection = True

    def __init__(self, result=None, raises=None):
        self._result, self._raises = result, raises

    def get_status_from_screen(self, lines):
        if self._raises:
            raise self._raises
        return self._result


class _NoScreenProvider:
    supports_screen_detection = False

    def get_status_from_screen(self, lines):  # pragma: no cover - never called
        raise AssertionError("must not be consulted")


class TestScreenSignal:
    def test_a_classified_frame_is_available(self):
        s = sf.screen_signal(_Provider(TerminalStatus.PROCESSING), ["a", "b"])
        assert s.state == "available"
        assert s.value == TerminalStatus.PROCESSING

    def test_a_provider_without_a_viewport_detector_is_absent_not_unreadable(self):
        s = sf.screen_signal(_NoScreenProvider(), ["a"])
        assert s.state == "absent"
        assert "no viewport-calibrated detector" in s.detail

    def test_an_uncapturable_pane_is_unreadable(self):
        s = sf.screen_signal(_Provider(TerminalStatus.IDLE), None)
        assert s.state == "unreadable"

    def test_a_detector_that_raises_is_unreadable_not_fatal(self):
        s = sf.screen_signal(_Provider(raises=RuntimeError("boom")), ["a"])
        assert s.state == "unreadable"
        assert "RuntimeError" in s.detail

    def test_an_unmatched_frame_is_absent_rather_than_unknown_as_a_value(self):
        s = sf.screen_signal(_Provider(TerminalStatus.UNKNOWN), ["a"])
        assert s.state == "absent"

    def test_a_missing_provider_is_absent(self):
        assert sf.screen_signal(None, ["a"]).state == "absent"


class TestLivenessSignal:
    def test_unchanged_reports_a_duration_not_a_boolean(self):
        # "unchanged for 4s" and "unchanged for three days" are different
        # observations even though a diff returns the same bit.
        s = sf.liveness_signal("abc", "abc", unchanged_for_seconds=9000)
        assert s.state == "available"
        assert s.value == 9000

    def test_a_change_resets_to_zero(self):
        s = sf.liveness_signal("abc", "def", unchanged_for_seconds=9000)
        assert s.value == 0

    def test_no_prior_sample_is_absent(self):
        assert sf.liveness_signal(None, "abc", unchanged_for_seconds=None).state == "absent"

    def test_an_uncapturable_pane_is_unreadable(self):
        assert sf.liveness_signal("abc", None, unchanged_for_seconds=1).state == "unreadable"


class TestFifoSignal:
    def test_an_unmonitored_native_tui_is_absent_by_architecture(self):
        s = sf.fifo_signal(TerminalStatus.IDLE, monitored=False)
        assert s.state == "absent"
        assert "no FIFO stream exists" in s.detail

    def test_a_monitored_classification_is_available(self):
        s = sf.fifo_signal("processing", monitored=True)
        assert s.state == "available"
        assert s.value == TerminalStatus.PROCESSING

    def test_an_unmatched_stream_is_absent(self):
        assert sf.fifo_signal(TerminalStatus.UNKNOWN, monitored=True).state == "absent"
        assert sf.fifo_signal(None, monitored=True).state == "absent"

    def test_an_unrecognised_status_string_does_not_crash(self):
        assert sf.fifo_signal("brand-new-state", monitored=True).state == "absent"
