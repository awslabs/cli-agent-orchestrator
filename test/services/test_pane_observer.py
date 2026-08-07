"""Pane observation — bounded, cached, and honest about failure.

COND-0242 is the reason this file is strict about two things: one tmux call per
observation, and a failed capture that leaves as None rather than as an empty
frame. A swallowed failure read as a quiet screen would let the fusion accuse a
healthy worker of being wedged.
"""

from cli_agent_orchestrator.services.pane_observer import PaneObserver


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class _Capture:
    def __init__(self, *frames):
        self.frames = list(frames)
        self.calls = []

    def __call__(self, pane_id):
        self.calls.append(pane_id)
        if not self.frames:
            return None
        return self.frames[0] if len(self.frames) == 1 else self.frames.pop(0)


def _observer(capture, clock):
    return PaneObserver(min_resample_seconds=5.0, clock=clock, capture=capture)


class TestResampleBudget:
    def test_repeated_polls_inside_the_window_make_one_tmux_call(self):
        # A dashboard refreshing every 2s must not become 20 capture-pane
        # calls every 2s.
        clock, cap = _Clock(), _Capture("frame")
        obs = _observer(cap, clock)
        for _ in range(5):
            obs.observe("%1")
            clock.advance(1)
        assert len(cap.calls) == 1

    def test_a_new_capture_happens_once_the_window_elapses(self):
        clock, cap = _Clock(), _Capture("frame")
        obs = _observer(cap, clock)
        obs.observe("%1")
        clock.advance(6)
        obs.observe("%1")
        assert len(cap.calls) == 2

    def test_no_pane_id_makes_no_call(self):
        clock, cap = _Clock(), _Capture("frame")
        assert _observer(cap, clock).observe(None) == (None, None)
        assert cap.calls == []


class TestUnchangedClock:
    def test_first_sight_reports_no_duration(self):
        # There is no prior frame, so no duration exists; reporting 0 would
        # claim the pane had just changed.
        clock, cap = _Clock(), _Capture("frame")
        lines, unchanged = _observer(cap, clock).observe("%1")
        assert lines == ["frame"]
        assert unchanged is None

    def test_a_stable_pane_accumulates_its_age(self):
        clock, cap = _Clock(), _Capture("same")
        obs = _observer(cap, clock)
        obs.observe("%1")
        clock.advance(600)
        _, unchanged = obs.observe("%1")
        assert unchanged == 600
        clock.advance(600)
        _, unchanged = obs.observe("%1")
        assert unchanged == 1200

    def test_a_changed_pane_restarts_the_clock(self):
        clock, cap = _Clock(), _Capture("one", "two")
        obs = _observer(cap, clock)
        obs.observe("%1")
        clock.advance(600)
        _, unchanged = obs.observe("%1")
        assert unchanged == 0.0

    def test_the_clock_tracks_first_seen_not_last_seen(self):
        # Anchoring to last_checked would reset the age on every poll and the
        # duration would never exceed the resample interval.
        clock, cap = _Clock(), _Capture("same")
        obs = _observer(cap, clock)
        obs.observe("%1")
        for _ in range(10):
            clock.advance(6)
            _, unchanged = obs.observe("%1")
        assert unchanged == 60

    def test_panes_are_tracked_independently(self):
        clock, cap = _Clock(), _Capture("same")
        obs = _observer(cap, clock)
        obs.observe("%1")
        clock.advance(300)
        obs.observe("%2")
        clock.advance(300)
        _, a = obs.observe("%1")
        _, b = obs.observe("%2")
        assert a == 600
        assert b == 300


class TestFailureIsNotAnEmptyFrame:
    def test_a_failed_capture_returns_none_not_a_quiet_screen(self):
        clock, cap = _Clock(), _Capture()
        lines, unchanged = _observer(cap, clock).observe("%1")
        assert lines is None
        assert unchanged is None

    def test_a_failure_does_not_poison_an_existing_clock(self):
        # The next successful read must continue the same unchanged-for clock
        # rather than restart it.
        clock = _Clock()

        class Flaky:
            def __init__(self):
                self.n = 0

            def __call__(self, pane_id):
                self.n += 1
                return None if self.n == 2 else "same"

        obs = _observer(Flaky(), clock)
        obs.observe("%1")
        clock.advance(600)
        assert obs.observe("%1")[0] is None
        clock.advance(600)
        _, unchanged = obs.observe("%1")
        assert unchanged == 1200


class TestViewportShape:
    def test_trailing_blank_rows_are_preserved(self):
        # get_status_from_screen is calibrated for a fixed-height, right-padded
        # viewport; get_history trims blanks for the RAW-stream detectors only.
        clock = _Clock()
        obs = _observer(_Capture("row\n\n\n"), clock)
        lines, _ = obs.observe("%1")
        assert lines == ["row", "", "", ""]


class TestPrune:
    def test_vanished_panes_are_dropped(self):
        clock, cap = _Clock(), _Capture("same")
        obs = _observer(cap, clock)
        obs.observe("%1")
        obs.observe("%2")
        assert obs.prune(["%1"]) == 1
        clock.advance(600)
        _, unchanged = obs.observe("%2")
        assert unchanged is None


class TestPruneIsWiredIn:
    """The cache must be pruned by the pass that already knows the live set.

    Found by independent review: `prune()` and `forget()` existed and nothing
    called them. Two consequences — a slow leak on a weeks-long server, and a
    recycled pane id (tmux reissues them after a server restart) inheriting the
    dead pane's quiet clock, which can accuse a genuinely fresh pane of being
    wedged about twenty minutes in.
    """

    def test_projection_prunes_vanished_panes(self, monkeypatch):
        from cli_agent_orchestrator.services import terminal_projection as tp

        pruned = {}
        monkeypatch.setattr(tp, "_observed_panes", lambda: {"%1": {}, "%2": {}})
        monkeypatch.setattr(tp, "list_terminals_by_session", lambda name: [])
        monkeypatch.setattr(
            tp.observer, "prune", lambda live: pruned.setdefault("live", list(live))
        )
        tp.project_session("s")
        assert sorted(pruned["live"]) == ["%1", "%2"]

    def test_an_unreadable_enumeration_prunes_nothing(self, monkeypatch):
        # None means "this pass could not enumerate", not "no pane is live".
        # Pruning on it would evict every clock and reset the wedge detector.
        from cli_agent_orchestrator.services import terminal_projection as tp

        called = []
        monkeypatch.setattr(tp, "_observed_panes", lambda: None)
        monkeypatch.setattr(tp, "list_terminals_by_session", lambda name: [])
        monkeypatch.setattr(tp.observer, "prune", lambda live: called.append(live))
        tp.project_session("s")
        assert called == []
