"""Tests for StatusMonitor — focus on backend-aware get_status().

get_status() is the single source of truth for terminal status. For pipe-pane
backends (tmux) it returns the pushed pipeline status; for event-inbox backends
(herdr), which never feed the pipeline, it derives status on demand from the
provider's native status. These tests pin both paths.
"""

import threading
import time
from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.status_monitor import (
    STALE_PROCESSING_BUFFER_QUIET_S,
    STALE_PROCESSING_CONFIRM_TTL_S,
    StatusMonitor,
)


def _backend(event_inbox):
    backend = MagicMock()
    backend.supports_event_inbox.return_value = event_inbox
    return backend


class TestGetStatusTmux:
    """Pipe-pane backend: get_status returns the pushed _last_status, unchanged."""

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_returns_pushed_status(self, mock_get_backend):
        mock_get_backend.return_value = _backend(event_inbox=False)
        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.PROCESSING

        assert sm.get_status("t1") == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_unknown_when_never_seen(self, mock_get_backend):
        mock_get_backend.return_value = _backend(event_inbox=False)
        sm = StatusMonitor()

        assert sm.get_status("missing") == TerminalStatus.UNKNOWN


class TestGetStatusEventInbox:
    """Event-inbox backend (herdr): derive status on demand from the provider."""

    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_derives_from_provider_native_status(self, mock_get_backend, mock_pm):
        mock_get_backend.return_value = _backend(event_inbox=True)
        provider = MagicMock()
        provider.get_status.return_value = TerminalStatus.IDLE
        mock_pm.get_provider.return_value = provider

        sm = StatusMonitor()
        # _last_status is empty (herdr never feeds the pipeline) — the old code
        # would return UNKNOWN here.
        assert sm.get_status("t1") == TerminalStatus.IDLE
        provider.get_status.assert_called_once()

    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_unknown_when_no_provider(self, mock_get_backend, mock_pm):
        mock_get_backend.return_value = _backend(event_inbox=True)
        mock_pm.get_provider.return_value = None

        sm = StatusMonitor()
        assert sm.get_status("t1") == TerminalStatus.UNKNOWN

    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_unknown_when_provider_lookup_raises(self, mock_get_backend, mock_pm):
        mock_get_backend.return_value = _backend(event_inbox=True)
        mock_pm.get_provider.side_effect = ValueError("terminal not in db")

        sm = StatusMonitor()
        assert sm.get_status("t1") == TerminalStatus.UNKNOWN

    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_unknown_when_provider_get_status_raises(self, mock_get_backend, mock_pm):
        mock_get_backend.return_value = _backend(event_inbox=True)
        provider = MagicMock()
        provider.get_status.side_effect = RuntimeError("herdr cli failed")
        mock_pm.get_provider.return_value = provider

        sm = StatusMonitor()
        assert sm.get_status("t1") == TerminalStatus.UNKNOWN


class TestStaleProcessingCapturePane:
    """Stuck-PROCESSING self-heal (#558): a terminal that goes genuinely idle can leave
    get_status() reporting PROCESSING forever, because the cheap re-check re-derives from
    the SAME rolling buffer that stopped changing the moment the process stopped emitting
    output. These pin the fresh capture-pane fallback that self-heals this without waiting
    for a manual nudge, and the gates that keep it from firing during ordinary turns:

    1. Buffer-quiet gate: the fallback only even attempts once no new chunk has landed for
       STALE_PROCESSING_BUFFER_QUIET_S. Most tests set _buffer_changed_at directly rather
       than mocking the ``time`` module wholesale — real time.monotonic() is always far
       past its arbitrary reference point, so a large negative value is unconditionally
       quiet.
    2. Detector routing: a capture-pane snapshot is rendered content, so it only feeds a
       detector declared safe for that shape — get_status_from_screen() for
       supports_screen_detection providers, get_status() for supports_direct_status_probe
       providers, and NOTHING for providers with neither flag (fail closed).
    3. Two-read confirm with expiry: a ready verdict must repeat on the next eligible read
       within STALE_PROCESSING_CONFIRM_TTL_S, and a new turn (notify_input_sent) drops the
       pending candidate.
    """

    @staticmethod
    def _quiet_since():
        """A _buffer_changed_at value old enough to satisfy the quiet gate unconditionally."""
        return -1000.0

    @staticmethod
    def _probe_provider(status=TerminalStatus.IDLE):
        """A provider stub routed through get_status(): direct-status-probe capable.

        Flags are set explicitly — a bare MagicMock's auto-attributes are truthy, which
        would silently route through the screen path instead.
        """
        provider = MagicMock()
        provider.session_name = "s1"
        provider.window_name = "w1"
        provider.supports_screen_detection = False
        provider.supports_direct_status_probe = True
        provider.get_status.return_value = status
        return provider

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_stale_processing_self_heals_via_capture_pane_after_two_confirming_reads(
        self, mock_pm, mock_get_backend
    ):
        provider = self._probe_provider(TerminalStatus.IDLE)
        mock_pm.get_provider.return_value = provider
        backend = _backend(event_inbox=False)
        backend.get_history.return_value = "the real pane -- idle composer, fully rendered"
        mock_get_backend.return_value = backend

        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.PROCESSING
        # Empty buffer -- as if the process stopped emitting output entirely, exactly the
        # shape that leaves the cheap re-check (which requires a truthy buffer) unable to
        # help at all.
        sm._buffers["t1"] = ""
        sm._buffer_changed_at["t1"] = self._quiet_since()

        # First read: a genuine ready candidate, but a single sample is never trusted --
        # must NOT self-heal yet (a lone capture can catch an Ink repaint mid-clear/rewrite
        # and read the wrong turn's response box).
        assert sm.get_status("t1") == TerminalStatus.PROCESSING
        assert sm._last_status["t1"] == TerminalStatus.PROCESSING
        assert backend.get_history.call_count == 1

        # Second, matching read confirms it. Reset the internal rate-limit gate directly
        # instead of waiting out STALE_PROCESSING_CAPTURE_INTERVAL_S for real.
        sm._last_stale_capture_check["t1"] = None
        assert sm.get_status("t1") == TerminalStatus.IDLE
        assert backend.get_history.call_count == 2
        provider.get_status.assert_called_with("the real pane -- idle composer, fully rendered")
        # Self-healing must actually update the latched status, not just this one return
        # value -- otherwise the very next poll would go right back through the same stale
        # path.
        assert sm._last_status["t1"] == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_capture_is_viewport_only_and_escape_free(self, mock_pm, mock_get_backend):
        """The pane read must be exactly the rendered viewport, escape-free -- the input
        shape the routed detectors are calibrated for. A tail_lines read is NOT that:
        capture-pane's ``-S -N`` starts N lines of scrollback ABOVE the viewport, so text
        from finished turns rides along, and detectors that match anywhere in their input
        resurrect it (a stale kimi/kiro error string in scrollback parses as ERROR, which
        is sticky and makes agent_step raise -- while the actual visible pane is IDLE)."""
        provider = self._probe_provider(TerminalStatus.IDLE)
        mock_pm.get_provider.return_value = provider
        backend = _backend(event_inbox=False)
        backend.get_history.return_value = "pane"
        mock_get_backend.return_value = backend

        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.PROCESSING
        sm._buffers["t1"] = ""
        sm._buffer_changed_at["t1"] = self._quiet_since()

        sm.get_status("t1")

        backend.get_history.assert_called_once_with(
            "s1", "w1", strip_escapes=True, visible_only=True
        )

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_differing_second_read_never_confirms(self, mock_pm, mock_get_backend):
        """Two DIFFERENT ready candidates in a row must never be honored -- only two
        IDENTICAL consecutive reads count as confirmed."""
        provider = self._probe_provider()
        mock_pm.get_provider.return_value = provider
        backend = _backend(event_inbox=False)
        backend.get_history.return_value = "some pane content"
        mock_get_backend.return_value = backend

        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.PROCESSING
        sm._buffers["t1"] = ""
        sm._buffer_changed_at["t1"] = self._quiet_since()

        provider.get_status.return_value = TerminalStatus.IDLE
        assert sm.get_status("t1") == TerminalStatus.PROCESSING  # 1st read: pending=IDLE

        sm._last_stale_capture_check["t1"] = None
        provider.get_status.return_value = TerminalStatus.COMPLETED
        # 2nd read differs -> not confirmed; pending is now COMPLETED, not IDLE.
        assert sm.get_status("t1") == TerminalStatus.PROCESSING

        assert sm._last_status["t1"] == TerminalStatus.PROCESSING
        assert backend.get_history.call_count == 2

        # A THIRD read matching the second (COMPLETED) now confirms it.
        sm._last_stale_capture_check["t1"] = None
        assert sm.get_status("t1") == TerminalStatus.COMPLETED
        assert sm._last_status["t1"] == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_capture_pane_still_processing_stays_processing_no_crash(
        self, mock_pm, mock_get_backend
    ):
        provider = self._probe_provider(TerminalStatus.PROCESSING)
        mock_pm.get_provider.return_value = provider
        backend = _backend(event_inbox=False)
        backend.get_history.return_value = "• Working (12s • esc to interrupt)"
        mock_get_backend.return_value = backend

        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.PROCESSING
        sm._buffers["t1"] = ""
        sm._buffer_changed_at["t1"] = self._quiet_since()

        assert sm.get_status("t1") == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_capture_pane_read_failure_stays_processing_no_crash(self, mock_pm, mock_get_backend):
        provider = self._probe_provider()
        mock_pm.get_provider.return_value = provider
        backend = _backend(event_inbox=False)
        backend.get_history.side_effect = RuntimeError("tmux not reachable")
        mock_get_backend.return_value = backend

        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.PROCESSING
        sm._buffers["t1"] = ""
        sm._buffer_changed_at["t1"] = self._quiet_since()

        assert sm.get_status("t1") == TerminalStatus.PROCESSING
        provider.get_status.assert_not_called()

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_no_provider_stays_processing_and_skips_capture_pane_entirely(
        self, mock_pm, mock_get_backend
    ):
        mock_pm.get_provider.return_value = None
        backend = _backend(event_inbox=False)
        mock_get_backend.return_value = backend

        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.PROCESSING
        sm._buffers["t1"] = ""
        sm._buffer_changed_at["t1"] = self._quiet_since()

        assert sm.get_status("t1") == TerminalStatus.PROCESSING
        backend.get_history.assert_not_called()

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_get_provider_raising_stays_processing_no_crash(self, mock_pm, mock_get_backend):
        # get_provider() raises (not returns None) for a terminal it no longer recognizes
        # -- matches get_status()'s own event-inbox branch, which already defends against
        # this.
        mock_pm.get_provider.side_effect = ValueError("terminal not in db")
        backend = _backend(event_inbox=False)
        mock_get_backend.return_value = backend

        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.PROCESSING
        sm._buffers["t1"] = ""
        sm._buffer_changed_at["t1"] = self._quiet_since()

        assert sm.get_status("t1") == TerminalStatus.PROCESSING
        backend.get_history.assert_not_called()

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_capture_pane_fallback_is_rate_limited(self, mock_pm, mock_get_backend):
        # get_status() is a hot path (every poll, across the whole fleet) -- the
        # capture-pane fallback is a real tmux subprocess call and must not fire on every
        # single poll while a terminal is stuck. Two calls back-to-back (real
        # time.monotonic(), so well within the rate-limit window) must only shell out once.
        provider = self._probe_provider(TerminalStatus.PROCESSING)
        mock_pm.get_provider.return_value = provider
        backend = _backend(event_inbox=False)
        backend.get_history.return_value = "still working"
        mock_get_backend.return_value = backend

        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.PROCESSING
        sm._buffers["t1"] = ""
        sm._buffer_changed_at["t1"] = self._quiet_since()

        sm.get_status("t1")
        sm.get_status("t1")

        backend.get_history.assert_called_once()

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_capture_pane_fallback_retried_after_rate_limit_window(self, mock_pm, mock_get_backend):
        provider = self._probe_provider(TerminalStatus.PROCESSING)
        mock_pm.get_provider.return_value = provider
        backend = _backend(event_inbox=False)
        backend.get_history.return_value = "still working"
        mock_get_backend.return_value = backend

        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.PROCESSING
        sm._buffers["t1"] = ""
        sm._buffer_changed_at["t1"] = self._quiet_since()

        sm.get_status("t1")
        # Simulate the rate-limit window having elapsed, without a real sleep.
        sm._last_stale_capture_check["t1"] = None
        sm.get_status("t1")

        assert backend.get_history.call_count == 2

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_buffer_recheck_resolving_skips_capture_pane_entirely(self, mock_pm, mock_get_backend):
        # When the existing cheap buffer re-check already resolves the status, the (more
        # expensive) capture-pane fallback must not run at all -- no regression in the
        # common case where the original mechanism already worked.
        provider = self._probe_provider(TerminalStatus.COMPLETED)
        mock_pm.get_provider.return_value = provider
        backend = _backend(event_inbox=False)
        mock_get_backend.return_value = backend

        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.PROCESSING
        sm._buffers["t1"] = "a real, non-empty buffer that resolves cleanly"

        assert sm.get_status("t1") == TerminalStatus.COMPLETED
        backend.get_history.assert_not_called()

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_recently_changed_buffer_skips_capture_pane_entirely(self, mock_pm, mock_get_backend):
        """A terminal mid-burst -- new chunks still actively arriving -- is not the stuck
        case this fallback exists for. Without the buffer-quiet gate, every ordinary busy
        turn would eat a real tmux subprocess call every ~3s for its entire duration."""
        provider = self._probe_provider()
        mock_pm.get_provider.return_value = provider
        backend = _backend(event_inbox=False)
        backend.get_history.return_value = "some content"
        mock_get_backend.return_value = backend

        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.PROCESSING
        sm._buffers["t1"] = ""
        # A chunk "just arrived" -- well within STALE_PROCESSING_BUFFER_QUIET_S.
        sm._buffer_changed_at["t1"] = time.monotonic()

        assert sm.get_status("t1") == TerminalStatus.PROCESSING
        backend.get_history.assert_not_called()

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_no_buffer_changed_at_recorded_skips_capture_pane_entirely(
        self, mock_pm, mock_get_backend
    ):
        """A terminal that has never had _process_chunk record a change must not be
        treated as "quiet since forever" -- the gate requires a real recorded quiet
        duration, not the absence of one."""
        provider = self._probe_provider()
        mock_pm.get_provider.return_value = provider
        backend = _backend(event_inbox=False)
        mock_get_backend.return_value = backend

        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.PROCESSING
        sm._buffers["t1"] = ""
        # _buffer_changed_at deliberately left unset.

        assert sm.get_status("t1") == TerminalStatus.PROCESSING
        backend.get_history.assert_not_called()

    @patch("cli_agent_orchestrator.services.status_monitor.get_server_settings")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_quiet_gate_tracks_real_chunk_arrivals(self, mock_pm, mock_get_backend, mock_settings):
        """The quiet gate must be driven by _process_chunk's own timestamp bump -- its
        real write site -- not by state the tests hand-set. Chunks streaming in keep the
        gate closed (no capture mid-burst); once the last recorded arrival ages past the
        window, exactly one capture fires. If _process_chunk ever stops recording
        arrivals, the aging step here has nothing to age and this fails."""
        mock_settings.return_value = {"state_buffer_max": 32768}
        provider = self._probe_provider(TerminalStatus.PROCESSING)
        mock_pm.get_provider.return_value = provider
        backend = _backend(event_inbox=False)
        backend.get_history.return_value = "pane content"
        mock_get_backend.return_value = backend

        sm = StatusMonitor()
        # Drive the REAL ingestion path: each chunk lands in the rolling buffer and (with
        # no event loop) detects inline -- provider reports PROCESSING throughout.
        for chunk in ("spinner ", "frame ", "spinner ", "frame ", "spinner"):
            sm._process_chunk("t1", chunk)
        assert sm._last_status["t1"] == TerminalStatus.PROCESSING

        # Mid-burst: the last chunk just landed, so the gate must hold the fallback shut.
        assert sm.get_status("t1") == TerminalStatus.PROCESSING
        assert backend.get_history.call_count == 0

        # Age the recorded arrival past the quiet window (KeyError here means the bump
        # never happened) and poll again: exactly one capture.
        sm._buffer_changed_at["t1"] = sm._buffer_changed_at["t1"] - (
            STALE_PROCESSING_BUFFER_QUIET_S + 0.5
        )
        assert sm.get_status("t1") == TerminalStatus.PROCESSING
        assert backend.get_history.call_count == 1

    @patch("cli_agent_orchestrator.services.status_monitor.get_server_settings")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_real_chunk_between_reads_invalidates_the_candidate(
        self, mock_pm, mock_get_backend, mock_settings
    ):
        """Real output arriving between the candidate read and the confirming read means
        the terminal is demonstrably alive -- the earlier ready verdict no longer
        describes it, and it must not be confirmable even after the buffer goes quiet
        again. Otherwise a candidate from before the burst pairs with a read from after
        it, and the two-read confirm degrades back to a single-frame latch."""
        mock_settings.return_value = {"state_buffer_max": 32768}
        provider = self._probe_provider(TerminalStatus.IDLE)
        mock_pm.get_provider.return_value = provider
        backend = _backend(event_inbox=False)
        backend.get_history.return_value = "idle-looking pane"
        mock_get_backend.return_value = backend

        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.PROCESSING
        sm._buffers["t1"] = ""
        sm._buffer_changed_at["t1"] = self._quiet_since()

        # Read 1 records the IDLE candidate.
        assert sm.get_status("t1") == TerminalStatus.PROCESSING
        assert sm._pending_stale_capture["t1"][0] == TerminalStatus.IDLE

        # A real chunk lands via the real ingestion path -- provider reports PROCESSING
        # for the buffer-driven detection so only the candidate bookkeeping is exercised.
        provider.get_status.return_value = TerminalStatus.PROCESSING
        sm._process_chunk("t1", "fresh spinner frame")
        assert "t1" not in sm._pending_stale_capture

        # Quiet returns, rate limit reset, and the pane reads IDLE again: this must be a
        # FRESH candidate (returns PROCESSING), never a confirmation of the pre-chunk one.
        # Empty the rolling buffer again so the cheap buffer re-check can't resolve the
        # status on its own -- this test is about the capture path's candidate bookkeeping.
        provider.get_status.return_value = TerminalStatus.IDLE
        sm._buffers["t1"] = ""
        sm._buffer_changed_at["t1"] = self._quiet_since()
        sm._last_stale_capture_check["t1"] = None
        assert sm.get_status("t1") == TerminalStatus.PROCESSING
        assert sm._last_status["t1"] == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_new_turn_during_the_confirming_read_discards_the_verdict(
        self, mock_pm, mock_get_backend
    ):
        """notify_input_sent deliberately leaves _last_status == PROCESSING while arming
        the revert, so a status re-check alone cannot see a new turn. If new input lands
        while the confirming capture is in flight, the confirmed-but-stale verdict must
        be discarded: applying it would stamp a pre-turn IDLE over the new turn AND
        consume the revert arm it just set, latch-blocking the new turn's genuine
        PROCESSING -- the exact failure the self-heal must never introduce."""
        provider = self._probe_provider(TerminalStatus.IDLE)
        mock_pm.get_provider.return_value = provider
        backend = _backend(event_inbox=False)
        backend.get_history.return_value = "idle-looking pane"
        mock_get_backend.return_value = backend

        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.PROCESSING
        sm._buffers["t1"] = ""
        sm._buffer_changed_at["t1"] = self._quiet_since()

        # Land the new input in the EXACT window: after _fresh_capture_pane_status has
        # confirmed and popped the candidate, before get_status applies the verdict.
        # (Input during the capture read itself is pinned separately by
        # test_new_turn_during_the_seeding_read_rejects_the_candidate; this pins the
        # later, narrower window.)
        real_probe = sm._fresh_capture_pane_status

        def probe_then_new_turn(terminal_id, generation):
            verdict = real_probe(terminal_id, generation)
            if verdict == TerminalStatus.IDLE:
                sm.notify_input_sent("t1")
            return verdict

        sm._fresh_capture_pane_status = probe_then_new_turn  # type: ignore[method-assign]

        assert sm.get_status("t1") == TerminalStatus.PROCESSING  # read 1: candidate only
        sm._last_stale_capture_check["t1"] = None
        result = sm.get_status("t1")  # read 2: confirmed, then the new turn intervenes

        assert result == TerminalStatus.PROCESSING
        assert sm._last_status["t1"] == TerminalStatus.PROCESSING
        # The arm set by notify_input_sent must survive untouched for the new turn.
        assert sm._allow_processing_revert["t1"] is True

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_new_turn_during_the_seeding_read_rejects_the_candidate(
        self, mock_pm, mock_get_backend
    ):
        """New input landing while the FIRST (candidate-seeding) capture is in flight
        must reject that candidate outright, not merely fail to confirm it.
        notify_input_sent's own candidate pop cannot help here -- the pop runs while the
        read is still in flight, and the seeding write lands AFTER it. If that
        pre-boundary IDLE were seeded anyway, the next poll would pin the new (by then
        stable) generation, take its own IDLE read as the "second" of the pair, confirm,
        and apply -- consuming the revert arm the new turn just set and latch-blocking
        its genuine PROCESSING. The seeding mutation itself must be generation-gated."""
        provider = self._probe_provider(TerminalStatus.IDLE)
        mock_pm.get_provider.return_value = provider
        backend = _backend(event_inbox=False)
        mock_get_backend.return_value = backend

        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.PROCESSING
        sm._buffers["t1"] = ""
        sm._buffer_changed_at["t1"] = self._quiet_since()

        # New input arrives DURING the first pane read -- inside the unlocked
        # subprocess window, after the caller pinned its generation.
        def new_turn_then_return_history(*args, **kwargs):
            sm.notify_input_sent("t1")
            return "idle-looking pane"

        backend.get_history.side_effect = new_turn_then_return_history

        assert sm.get_status("t1") == TerminalStatus.PROCESSING
        # The straddling read must not have (re)seeded the candidate map.
        assert "t1" not in sm._pending_stale_capture

        # Next poll reads the same idle-looking pane cleanly: with the stale candidate
        # correctly rejected this is a FIRST sighting, so nothing confirms, nothing is
        # applied, and the new turn's revert arm is still intact.
        backend.get_history.side_effect = None
        backend.get_history.return_value = "idle-looking pane"
        sm._last_stale_capture_check["t1"] = None
        assert sm.get_status("t1") == TerminalStatus.PROCESSING
        assert sm._last_status["t1"] == TerminalStatus.PROCESSING
        assert sm._allow_processing_revert["t1"] is True

    @patch("cli_agent_orchestrator.services.status_monitor.get_server_settings")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_real_chunk_during_the_seeding_read_rejects_the_candidate(
        self, mock_pm, mock_get_backend, mock_settings
    ):
        """Same straddling window as above, other boundary source: a real chunk through
        _process_chunk while the seeding capture is in flight. The chunk's candidate pop
        runs before the seeding write lands, so without the generation gate the
        pre-burst IDLE would be seeded after it and confirmable by the next quiet
        poll."""
        mock_settings.return_value = {"state_buffer_max": 32768}
        provider = self._probe_provider(TerminalStatus.IDLE)
        mock_pm.get_provider.return_value = provider
        backend = _backend(event_inbox=False)
        mock_get_backend.return_value = backend

        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.PROCESSING
        sm._buffers["t1"] = ""
        sm._buffer_changed_at["t1"] = self._quiet_since()

        def chunk_then_return_history(*args, **kwargs):
            # The buffer-driven detection must keep reporting busy so only the
            # capture path's candidate bookkeeping is exercised.
            provider.get_status.return_value = TerminalStatus.PROCESSING
            sm._process_chunk("t1", "fresh spinner frame")
            provider.get_status.return_value = TerminalStatus.IDLE
            return "idle-looking pane"

        backend.get_history.side_effect = chunk_then_return_history

        assert sm.get_status("t1") == TerminalStatus.PROCESSING
        assert "t1" not in sm._pending_stale_capture

        # Quiet again; the next clean read is a FIRST sighting, never a confirmation.
        backend.get_history.side_effect = None
        backend.get_history.return_value = "idle-looking pane"
        sm._buffers["t1"] = ""
        sm._buffer_changed_at["t1"] = self._quiet_since()
        sm._last_stale_capture_check["t1"] = None
        assert sm.get_status("t1") == TerminalStatus.PROCESSING
        assert sm._last_status["t1"] == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_screen_provider_routes_through_get_status_from_screen(self, mock_pm, mock_get_backend):
        """A supports_screen_detection provider owns a detector calibrated for rendered
        viewport lines -- the fallback must feed the capture to THAT, never to the
        raw-stream get_status() (whose ordering heuristics invert on rendered frames)."""
        provider = MagicMock()
        provider.session_name = "s1"
        provider.window_name = "w1"
        provider.supports_screen_detection = True
        provider.get_status_from_screen.return_value = TerminalStatus.IDLE
        mock_pm.get_provider.return_value = provider
        backend = _backend(event_inbox=False)
        backend.get_history.return_value = "line one\nline two\nline three"
        mock_get_backend.return_value = backend

        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.PROCESSING
        sm._buffers["t1"] = ""
        sm._buffer_changed_at["t1"] = self._quiet_since()

        assert sm.get_status("t1") == TerminalStatus.PROCESSING  # 1st read: candidate
        sm._last_stale_capture_check["t1"] = None
        assert sm.get_status("t1") == TerminalStatus.IDLE  # 2nd read: confirmed

        provider.get_status_from_screen.assert_called_with(["line one", "line two", "line three"])
        provider.get_status.assert_not_called()

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_unroutable_provider_fails_closed_busy_kiro_frame_never_latches_completed(
        self, mock_pm, mock_get_backend
    ):
        """The regression pin for the detector routing. kiro_cli sets neither
        supports_screen_detection nor supports_direct_status_probe: its raw-stream
        detector misreads a RENDERED busy frame as COMPLETED, because on screen the
        always-drawn composer placeholder sits physically below the working line and the
        credits line -- the opposite of the byte-stream ordering its checks were tuned
        against. Fed through the fallback, that deterministic misread would confirm
        itself (both reads see the same bytes), sticky-latch COMPLETED, disarm
        _allow_processing_revert, and block the agent's genuine PROCESSING for the rest
        of the turn. The fallback must therefore not consult a capture for this provider
        at all."""
        from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider

        # A plausible rendered pane of a BUSY kiro: finished previous turn (credits) with
        # the live working indicator, and the composer placeholder drawn at the bottom as
        # always.
        busy_pane = "\n".join(
            [
                "> summarize the repo layout",
                "",
                "────────────────────────────────",
                "The repository is organised into providers, services and backends.",
                "────────────────────────────────",
                "",
                "▸ Credits: 0.42 • Time: 12s",
                "",
                "⠧ Kiro is working... (esc to interrupt)",
                "",
                "Ask a question or describe a task ↵",
            ]
        )

        provider = KiroCliProvider("test1234", "s1", "w1", "developer")
        provider._initialized = True
        mock_pm.get_provider.return_value = provider
        backend = _backend(event_inbox=False)
        backend.get_native_status.return_value = None
        backend.get_history.return_value = busy_pane
        mock_get_backend.return_value = backend

        # Canary: the raw detector really does misread this rendered busy frame as
        # COMPLETED. If kiro's detector ever changes and this stops holding, the fixture
        # no longer exercises the hazard and needs refreshing.
        assert provider.get_status(busy_pane) == TerminalStatus.COMPLETED
        backend.get_history.reset_mock()

        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.PROCESSING
        sm._buffers["t1"] = ""
        sm._buffer_changed_at["t1"] = self._quiet_since()

        # Two eligible polls (rate limit reset in between): the fallback must not
        # capture, must not apply, and the terminal must still read PROCESSING.
        assert sm.get_status("t1") == TerminalStatus.PROCESSING
        sm._last_stale_capture_check["t1"] = None
        assert sm.get_status("t1") == TerminalStatus.PROCESSING
        backend.get_history.assert_not_called()
        assert sm._last_status["t1"] == TerminalStatus.PROCESSING
        # The revert arm is untouched -- a later genuine ready detection from the real
        # pipeline is still free to land.
        assert sm._allow_processing_revert.get("t1") is None

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_stale_pending_candidate_expires_instead_of_confirming(self, mock_pm, mock_get_backend):
        """A pending candidate must be confirmed within STALE_PROCESSING_CONFIRM_TTL_S or
        dropped. Without the expiry, a candidate recorded long ago -- e.g. during a
        different phase of the same turn -- could be "confirmed" by one lone mid-repaint
        read much later, which is exactly the single-frame latch the confirm exists to
        prevent."""
        provider = self._probe_provider(TerminalStatus.IDLE)
        mock_pm.get_provider.return_value = provider
        backend = _backend(event_inbox=False)
        backend.get_history.return_value = "idle pane"
        mock_get_backend.return_value = backend

        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.PROCESSING
        sm._buffers["t1"] = ""
        sm._buffer_changed_at["t1"] = self._quiet_since()

        assert sm.get_status("t1") == TerminalStatus.PROCESSING  # candidate recorded

        # Age the candidate past the TTL, as if the confirming read never arrived on time.
        pending_status, pending_ts, pending_gen = sm._pending_stale_capture["t1"]
        sm._pending_stale_capture["t1"] = (
            pending_status,
            pending_ts - (STALE_PROCESSING_CONFIRM_TTL_S + 0.5),
            pending_gen,
        )

        # The next matching read must NOT confirm the expired candidate -- it starts a
        # fresh one instead.
        sm._last_stale_capture_check["t1"] = None
        assert sm.get_status("t1") == TerminalStatus.PROCESSING
        assert sm._last_status["t1"] == TerminalStatus.PROCESSING

        # The fresh candidate confirms normally on the read after that.
        sm._last_stale_capture_check["t1"] = None
        assert sm.get_status("t1") == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_notify_input_sent_invalidates_pending_candidate(self, mock_pm, mock_get_backend):
        """A new turn invalidates whatever the pane showed before it: after
        notify_input_sent, one lone ready read must NOT be honored off the pre-turn
        candidate -- confirmation starts over."""
        provider = self._probe_provider(TerminalStatus.IDLE)
        mock_pm.get_provider.return_value = provider
        backend = _backend(event_inbox=False)
        backend.get_history.return_value = "idle pane"
        mock_get_backend.return_value = backend

        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.PROCESSING
        sm._buffers["t1"] = ""
        sm._buffer_changed_at["t1"] = self._quiet_since()

        assert sm.get_status("t1") == TerminalStatus.PROCESSING  # candidate recorded

        # Turn boundary: new input goes to the terminal.
        sm.notify_input_sent("t1")

        # One post-input ready read is a FIRST sighting again, not a confirmation.
        sm._last_stale_capture_check["t1"] = None
        assert sm.get_status("t1") == TerminalStatus.PROCESSING
        assert sm._last_status["t1"] == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_toctou_stale_capture_discarded_if_status_changed_meanwhile(
        self, mock_pm, mock_get_backend
    ):
        """The capture-pane read runs OUTSIDE the lock (a real subprocess call). If the
        real pipeline independently resolves the terminal to something else WHILE that
        read is in flight, the stale capture result must be discarded rather than applied
        over the fresher, real status."""
        provider = self._probe_provider(TerminalStatus.IDLE)
        mock_pm.get_provider.return_value = provider
        backend = _backend(event_inbox=False)
        backend.get_history.return_value = "idle pane"
        mock_get_backend.return_value = backend

        sm = StatusMonitor()
        sm._last_status["t1"] = TerminalStatus.PROCESSING
        sm._buffers["t1"] = ""
        sm._buffer_changed_at["t1"] = self._quiet_since()

        # First read establishes the pending candidate.
        assert sm.get_status("t1") == TerminalStatus.PROCESSING
        sm._last_stale_capture_check["t1"] = None

        # Simulate the real pipeline resolving this terminal to ERROR WHILE the second,
        # confirming capture-pane read is "in flight" -- mutate _last_status from inside
        # the mocked backend call, which is where the real (slow, unlocked) subprocess
        # call happens.
        def mutate_then_return_history(*args, **kwargs):
            sm._last_status["t1"] = TerminalStatus.ERROR
            return "idle pane"

        backend.get_history.side_effect = mutate_then_return_history

        result = sm.get_status("t1")

        # The stale IDLE confirmation must be discarded, not applied over the real ERROR
        # that arrived while the capture-pane read was in flight.
        assert result == TerminalStatus.ERROR
        assert sm._last_status["t1"] == TerminalStatus.ERROR


class TestScreenDetection:
    """Rendered-screen detection should fail soft and keep monitoring alive."""

    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_render_error_falls_back_to_raw_buffer_detection(self, mock_pm):
        class BrokenScreen:
            @property
            def display(self):
                raise RuntimeError("torn pyte frame")

        provider = MagicMock()
        provider.get_status.return_value = TerminalStatus.IDLE
        mock_pm.get_provider.side_effect = AssertionError("provider should not be refetched")

        sm = StatusMonitor()
        sm._screens["t1"] = (BrokenScreen(), MagicMock())
        sm._buffers["t1"] = "raw buffer with idle footer"

        assert sm._detect_screen("t1", provider) == TerminalStatus.IDLE
        provider.get_status.assert_called_once_with("raw buffer with idle footer")
        mock_pm.get_provider.assert_not_called()


class _SequencedMonitor:
    """Drive _process_chunk with a scripted sequence of detected statuses.

    Patches provider get_status to pop from the script and the event bus to
    record published status events, so each test reads as: feed detections,
    assert latched status + published transitions.
    """

    def __init__(self):
        self.sm = StatusMonitor()
        self.published = []

    def feed(self, status):
        provider = MagicMock()
        provider.get_status.return_value = status
        # These tests exercise the RAW detection path's latch logic. Pin
        # supports_screen_detection False so they are independent of the
        # CAO_PYTE_STATUS default (a bare MagicMock would be truthy and route
        # through the pyte screen path).
        provider.supports_screen_detection = False
        bus = MagicMock()
        bus.publish.side_effect = lambda topic, data: self.published.append(data["status"])
        with (
            patch("cli_agent_orchestrator.services.status_monitor.provider_manager") as mock_pm,
            patch("cli_agent_orchestrator.services.status_monitor.bus", bus),
        ):
            mock_pm.get_provider.return_value = provider
            self.sm._process_chunk("t1", "x")

    def status(self):
        return self.sm._last_status.get("t1")


class TestStickyLatching:
    """Pin the sticky ready-status latch + notify_input_sent state machine."""

    def test_idle_to_processing_blocked_without_arm(self):
        m = _SequencedMonitor()
        m.feed(TerminalStatus.IDLE)
        m.feed(TerminalStatus.PROCESSING)  # eviction flap
        assert m.status() == TerminalStatus.IDLE
        assert m.published == ["idle"]

    def test_ready_to_unknown_blocked_without_arm(self):
        m = _SequencedMonitor()
        m.feed(TerminalStatus.COMPLETED)
        m.feed(TerminalStatus.UNKNOWN)
        assert m.status() == TerminalStatus.COMPLETED

    def test_completed_to_idle_blocked_without_arm(self):
        """Codex-style: user marker evicts before assistant bullet."""
        m = _SequencedMonitor()
        m.feed(TerminalStatus.COMPLETED)
        m.feed(TerminalStatus.IDLE)
        assert m.status() == TerminalStatus.COMPLETED

    def test_idle_to_completed_always_allowed(self):
        m = _SequencedMonitor()
        m.feed(TerminalStatus.IDLE)
        m.feed(TerminalStatus.COMPLETED)
        assert m.status() == TerminalStatus.COMPLETED

    def test_arm_allows_processing_then_reblocks(self):
        """The normal cycle: input → PROCESSING accepted → COMPLETED → flap blocked."""
        m = _SequencedMonitor()
        m.feed(TerminalStatus.IDLE)
        m.sm.notify_input_sent("t1")
        m.feed(TerminalStatus.PROCESSING)
        assert m.status() == TerminalStatus.PROCESSING
        m.feed(TerminalStatus.COMPLETED)
        m.feed(TerminalStatus.PROCESSING)  # post-completion eviction flap
        assert m.status() == TerminalStatus.COMPLETED

    def test_dispatch_can_publish_processing_before_first_tui_redraw(self):
        m = _SequencedMonitor()
        m.feed(TerminalStatus.COMPLETED)

        m.sm.notify_input_sent("t1", assume_processing=True)

        assert m.status() == TerminalStatus.PROCESSING
        assert m.sm._allow_processing_revert["t1"] is False

    def test_arm_survives_ready_to_ready_flap(self):
        """A large paste can evict the response markers BEFORE the agent
        starts working, flapping COMPLETED → IDLE. That flap must not consume
        the arm — otherwise the genuine PROCESSING that follows is blocked,
        the terminal reads IDLE while the agent is busy, and InboxService
        (which delivers on IDLE/COMPLETED) can paste a queued message into
        the middle of an active response."""
        m = _SequencedMonitor()
        m.feed(TerminalStatus.COMPLETED)
        m.sm.notify_input_sent("t1")
        m.feed(TerminalStatus.IDLE)  # paste evicted markers — flap
        assert m.status() == TerminalStatus.IDLE
        m.feed(TerminalStatus.PROCESSING)  # genuine cycle start
        assert m.status() == TerminalStatus.PROCESSING
        m.feed(TerminalStatus.COMPLETED)
        m.feed(TerminalStatus.PROCESSING)  # post-completion flap re-blocked
        assert m.status() == TerminalStatus.COMPLETED

    def test_arm_survives_waiting_user_answer_to_idle(self):
        """Answering a permission prompt (send_special_key arms the gate)
        can flap WAITING_USER_ANSWER → IDLE before the agent resumes."""
        m = _SequencedMonitor()
        m.feed(TerminalStatus.WAITING_USER_ANSWER)
        m.sm.notify_input_sent("t1")
        m.feed(TerminalStatus.IDLE)  # prompt cleared, redraw flap
        m.feed(TerminalStatus.PROCESSING)  # agent resumes the task
        assert m.status() == TerminalStatus.PROCESSING

    def test_arm_consumed_by_init_style_upgrade(self):
        """non-ready → ready latch consumes the arm (CLI launch reaching its
        first idle prompt without a visible PROCESSING window)."""
        m = _SequencedMonitor()
        m.sm.notify_input_sent("t1")  # launch keystroke
        m.feed(TerminalStatus.IDLE)  # TUI ready
        m.feed(TerminalStatus.PROCESSING)  # redraw flap — must be blocked
        assert m.status() == TerminalStatus.IDLE

    def test_processing_consumes_arm_once(self):
        m = _SequencedMonitor()
        m.feed(TerminalStatus.IDLE)
        m.sm.notify_input_sent("t1")
        m.feed(TerminalStatus.PROCESSING)
        m.feed(TerminalStatus.IDLE)
        m.feed(TerminalStatus.PROCESSING)  # no new input — blocked
        assert m.status() == TerminalStatus.IDLE

    def test_reset_buffer_clears_arm(self):
        m = _SequencedMonitor()
        m.feed(TerminalStatus.IDLE)
        m.sm.notify_input_sent("t1")
        m.sm.reset_buffer("t1")
        m.feed(TerminalStatus.IDLE)
        m.feed(TerminalStatus.PROCESSING)  # arm gone — blocked
        assert m.status() == TerminalStatus.IDLE

    def test_clear_rolling_buffer_preserves_arm(self):
        """clear_rolling_buffer is byte-only — arm survives so the next
        IDLE→PROCESSING transition (after send_input) is honored.

        Regression guard for test_supervisor_assign_and_handoff: send_input
        must clear the rolling buffer to drop stale idle placeholders, but
        the arm must survive so the agent's PROCESSING signal isn't blocked
        by stickiness.
        """
        m = _SequencedMonitor()
        m.feed(TerminalStatus.IDLE)
        m.sm.notify_input_sent("t1")
        m.sm.clear_rolling_buffer("t1")
        # Arm and last-status preserved
        assert m.sm._allow_processing_revert.get("t1") is True
        assert m.sm._last_status.get("t1") == TerminalStatus.IDLE
        # PROCESSING transition honored (arm consumed on genuine PROCESSING)
        m.feed(TerminalStatus.PROCESSING)
        assert m.status() == TerminalStatus.PROCESSING

    def test_clear_terminal_clears_arm(self):
        m = _SequencedMonitor()
        m.feed(TerminalStatus.IDLE)
        m.sm.notify_input_sent("t1")
        m.sm.clear_terminal("t1")
        assert "t1" not in m.sm._allow_processing_revert

    def test_no_event_published_for_blocked_downgrade(self):
        """Blocked flaps must not publish status events — InboxService
        subscribes to them and a spurious ready event could double-deliver."""
        m = _SequencedMonitor()
        m.feed(TerminalStatus.COMPLETED)
        m.feed(TerminalStatus.PROCESSING)
        m.feed(TerminalStatus.UNKNOWN)
        m.feed(TerminalStatus.IDLE)
        assert m.published == ["completed"]

    def test_unknown_does_not_overwrite_known_processing(self):
        """UNKNOWN is 'no signal', not a state: a mid-turn UNKNOWN (e.g. the
        screen momentarily shows neither spinner nor prompt while a tool runs)
        must not downgrade a known PROCESSING. Observed live as a spurious
        processing→unknown→completed blip."""
        m = _SequencedMonitor()
        m.feed(TerminalStatus.IDLE)
        m.sm.notify_input_sent("t1")
        m.feed(TerminalStatus.PROCESSING)
        m.feed(TerminalStatus.UNKNOWN)
        assert m.status() == TerminalStatus.PROCESSING

    def test_armed_unknown_then_ready_rerender_keeps_processing(self):
        """Guards against a tempting-but-wrong "suppress UNKNOWN only when not
        armed" change (so an armed new turn could clear a stale ready status).

        If an armed terminal's rising-edge frame reads UNKNOWN (a torn paste
        frame) and then re-renders the PRIOR turn's COMPLETED before the new
        spinner draws, letting that UNKNOWN through would make the
        UNKNOWN->COMPLETED bounce a non-ready->ready upgrade that CONSUMES the
        revert arm. The genuine PROCESSING that follows would then be latch-
        blocked, stranding the terminal at COMPLETED for the whole busy turn —
        and InboxService (delivers on IDLE/COMPLETED) would paste into a working
        agent. Suppressing UNKNOWN unconditionally keeps the arm intact so the
        real PROCESSING wins."""
        m = _SequencedMonitor()
        m.feed(TerminalStatus.COMPLETED)
        m.sm.notify_input_sent("t1")
        m.feed(TerminalStatus.UNKNOWN)  # torn rising-edge frame after the paste
        m.feed(TerminalStatus.COMPLETED)  # prior turn re-rendered at quiescence
        m.feed(TerminalStatus.PROCESSING)  # genuine new-turn processing
        assert m.status() == TerminalStatus.PROCESSING
        assert m.published == ["completed", "processing"]

    def test_initial_unknown_is_published(self):
        """The first detection (last is None) may legitimately be UNKNOWN —
        e.g. a freshly created terminal before any marker renders."""
        m = _SequencedMonitor()
        m.feed(TerminalStatus.UNKNOWN)
        assert m.status() == TerminalStatus.UNKNOWN
        assert m.published == ["unknown"]


class TestQuiescenceTimerCancel:
    """The pyte quiescence timer is an asyncio.TimerHandle owned by the
    StatusMonitor's loop. clear_terminal/reset_buffer can run off that loop
    thread (cleanup_old_data is dispatched via asyncio.to_thread), and
    TimerHandle.cancel() is not thread-safe, so the cancel must be marshaled
    onto the owning loop, never called directly cross-thread."""

    def test_cancel_marshaled_when_off_loop_thread(self):
        sm = StatusMonitor()
        loop = MagicMock()
        sm._loop = loop
        handle = MagicMock()
        sm._quiesce_handle["t1"] = handle

        # clear_terminal from a worker thread (which has no running loop).
        t = threading.Thread(target=sm.clear_terminal, args=("t1",))
        t.start()
        t.join()

        handle.cancel.assert_not_called()
        loop.call_soon_threadsafe.assert_called_once_with(handle.cancel)

    def test_reset_buffer_cancel_marshaled_when_off_loop_thread(self):
        sm = StatusMonitor()
        loop = MagicMock()
        sm._loop = loop
        handle = MagicMock()
        sm._quiesce_handle["t1"] = handle

        t = threading.Thread(target=sm.reset_buffer, args=("t1",))
        t.start()
        t.join()

        handle.cancel.assert_not_called()
        loop.call_soon_threadsafe.assert_called_once_with(handle.cancel)

    def test_cancel_direct_when_no_loop_captured(self):
        """Offline/unit path (no loop ever scheduled a timer): a direct cancel
        is correct because there is no foreign loop to race."""
        sm = StatusMonitor()
        handle = MagicMock()
        sm._quiesce_handle["t1"] = handle
        sm.clear_terminal("t1")  # sm._loop is None
        handle.cancel.assert_called_once()

    def test_no_handle_is_a_noop(self):
        sm = StatusMonitor()
        sm._loop = MagicMock()
        # No timer scheduled for this terminal — must not blow up.
        sm.clear_terminal("missing")
        sm._loop.call_soon_threadsafe.assert_not_called()


class TestRawDebounceArmedDetection:
    """Regression: raw debounce must detect PROCESSING on later chunks while armed."""

    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_armed_ready_detects_processing_on_second_chunk(self, mock_get_backend, mock_pm):
        """When terminal is IDLE (armed), chunk 1 is UNKNOWN, chunk 2 has PROCESSING
        marker — PROCESSING must be detected immediately, not deferred to quiescence."""
        mock_get_backend.return_value = _backend(event_inbox=False)
        provider = MagicMock()
        provider.supports_screen_detection = False
        mock_pm.get_provider.return_value = provider

        sm = StatusMonitor()
        # Simulate terminal already at IDLE (armed state)
        sm._last_status["t1"] = TerminalStatus.IDLE
        sm._allow_processing_revert["t1"] = True

        # Mock _detect_status: first call returns UNKNOWN, second returns PROCESSING
        detect_results = iter([TerminalStatus.UNKNOWN, TerminalStatus.PROCESSING])
        sm._detect_status = lambda tid, buf: next(detect_results)

        # Chunk 1: UNKNOWN — should still attempt detection (terminal is ready)
        sm._process_chunk("t1", "neutral output")
        # Chunk 2: PROCESSING — must detect immediately, not wait for quiescence
        sm._process_chunk("t1", "● Working on task...")

        assert sm._last_status["t1"] == TerminalStatus.PROCESSING


class TestProcessChunkBufferTruncation:
    """_process_chunk truncates the rolling buffer to the live
    state_buffer_max server setting, not a fixed constant."""

    @patch("cli_agent_orchestrator.services.status_monitor.get_server_settings")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_truncates_to_configured_state_buffer_max(
        self, mock_get_backend, mock_pm, mock_get_settings
    ):
        mock_get_backend.return_value = _backend(event_inbox=False)
        provider = MagicMock()
        provider.supports_screen_detection = False
        mock_pm.get_provider.return_value = provider
        mock_get_settings.return_value = {"state_buffer_max": 10}

        sm = StatusMonitor()
        sm._detect_status = lambda tid, buf: TerminalStatus.UNKNOWN

        sm._process_chunk("t1", "0123456789ABCDEF")  # 16 bytes, cap is 10

        assert sm.get_buffer("t1") == "6789ABCDEF"

    @patch("cli_agent_orchestrator.services.status_monitor.get_server_settings")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_marker_evicted_at_small_cap_survives_at_larger_cap(
        self, mock_get_backend, mock_pm, mock_get_settings
    ):
        """Same real mechanism the live rig test proved end-to-end: a marker
        near the start of a chunk is evicted once enough trailing bytes
        follow it past the configured cap, and survives when the cap is
        raised — driven here purely by the configured setting, not a
        hardcoded 8192."""
        mock_get_backend.return_value = _backend(event_inbox=False)
        provider = MagicMock()
        provider.supports_screen_detection = False
        mock_pm.get_provider.return_value = provider

        payload = "MARKER" + "x" * 20  # 26 bytes total, marker is the first 6

        mock_get_settings.return_value = {"state_buffer_max": 10}
        sm_small = StatusMonitor()
        sm_small._detect_status = lambda tid, buf: TerminalStatus.UNKNOWN
        sm_small._process_chunk("t1", payload)
        assert "MARKER" not in sm_small.get_buffer("t1")

        mock_get_settings.return_value = {"state_buffer_max": 32768}
        sm_large = StatusMonitor()
        sm_large._detect_status = lambda tid, buf: TerminalStatus.UNKNOWN
        sm_large._process_chunk("t1", payload)
        assert "MARKER" in sm_large.get_buffer("t1")
