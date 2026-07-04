"""Tests for StatusMonitor — focus on backend-aware get_status().

get_status() is the single source of truth for terminal status. For pipe-pane
backends (tmux) it returns the pushed pipeline status; for event-inbox backends
(herdr), which never feed the pipeline, it derives status on demand from the
provider's native status. These tests pin both paths.
"""

import threading
from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.status_monitor import StatusMonitor


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
