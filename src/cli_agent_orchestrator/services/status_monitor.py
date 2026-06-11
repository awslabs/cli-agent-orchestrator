"""Monitors terminal status by accumulating output and detecting changes.

Consumer: terminal.{id}.output
Publisher: terminal.{id}.status
"""

import asyncio
import logging
import threading
from typing import Dict, List, Optional, Tuple

from cli_agent_orchestrator.constants import (
    CAO_PYTE_STATUS,
    PYTE_QUIESCENCE_DELAY_S,
    PYTE_SCREEN_COLS,
    PYTE_SCREEN_ROWS,
    STATE_BUFFER_MAX,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.utils.event import terminal_id_from_topic

logger = logging.getLogger(__name__)

# Statuses that represent a stable "ready" state — the agent has finished
# producing output and is waiting for further input. Once latched, the
# StatusMonitor will not regress to PROCESSING until ``notify_input_sent``
# is called (signalling that a new processing cycle is starting).
#
# Why: the event-driven pipeline derives status from a rolling 8KB buffer,
# and TUI redraws (cursor positioning, status-bar refreshes) routinely
# evict the idle/response markers that the per-provider get_status() relies
# on. That makes status flap rapidly between IDLE/COMPLETED and PROCESSING
# in the seconds following completion. Without stickiness, both
# wait_until_status (server-side) and the e2e tests' HTTP polling miss the
# brief "ready" windows and time out (PR #273 codex 60s init timeouts,
# gemini 240s init timeouts, completion-timeout failures).
_STICKY_READY_STATUSES = frozenset(
    {
        TerminalStatus.IDLE,
        TerminalStatus.COMPLETED,
        TerminalStatus.WAITING_USER_ANSWER,
        TerminalStatus.ERROR,
    }
)


class StatusMonitor:
    """Accumulates terminal output into rolling buffers and detects status changes."""

    def __init__(self):
        # Guards _buffers/_last_status/_allow_processing_revert. State is
        # touched from the asyncio consumer (_process_chunk), FastAPI's
        # threadpool (send_input → notify_input_sent, get_status), inbox
        # delivery worker threads, and cleanup_old_data's thread. Individual
        # dict ops are GIL-atomic, but the latch logic is a read-modify-write
        # sequence (read armed → decide transition → consume arm) that must
        # not interleave with notify_input_sent, or a freshly-armed gate can
        # be consumed by a decision taken against stale state.
        self._lock = threading.RLock()
        self._buffers: Dict[str, str] = {}
        self._last_status: Dict[str, TerminalStatus] = {}
        # Per-terminal flag: when True, the next provider-detected PROCESSING
        # is honored and stickiness reset. Set by notify_input_sent() whenever
        # external input is sent to the terminal (paste-bombed by send_input
        # or backend.send_keys via provider init). Without this, latched
        # IDLE/COMPLETED would freeze the terminal forever even when the
        # agent is genuinely processing new work.
        self._allow_processing_revert: Dict[str, bool] = {}
        # --- pyte rendered-screen detection state (only used when CAO_PYTE_STATUS
        # is on AND the provider opts in via supports_screen_detection) ---
        # Per-terminal pyte Screen+Stream that composites the raw byte stream
        # into a rendered viewport. Detection runs against the composited screen
        # on two edges only — rising (output resumed) and quiescence (output
        # stopped for PYTE_QUIESCENCE_DELAY_S) — never mid-burst, which is what
        # keeps status flap-free.
        self._screens: Dict[str, Tuple[object, object]] = {}
        self._bursting: Dict[str, bool] = {}
        # Pending quiescence-detect timer handle per terminal (loop.call_later).
        self._quiesce_handle: Dict[str, asyncio.TimerHandle] = {}

    async def run(self) -> None:
        """Subscribe to output events and detect status changes."""
        queue = bus.subscribe("terminal.*.output")
        logger.info("StatusMonitor started")

        while True:
            try:
                event = await queue.get()
                terminal_id = terminal_id_from_topic(event["topic"])
                self._process_chunk(terminal_id, event["data"]["data"])
            except Exception as e:
                logger.exception(f"Error in StatusMonitor: {e}")

    def _process_chunk(self, terminal_id: str, chunk: str) -> None:
        """Append chunk to the rolling buffer and (re)detect status.

        Two detection paths share one latch/publish backend (_apply_detection):
        - RAW (default, every provider): regex over the rolling 8KB byte
          buffer, run on every chunk. Unchanged legacy behavior.
        - SCREEN (pyte): when CAO_PYTE_STATUS is on AND the provider opts in
          via supports_screen_detection, the chunk is fed to a per-terminal
          pyte screen and detection runs only on the rising edge (output
          resumed) and at quiescence (output stopped) — see
          _schedule_screen_detection.
        """
        provider = provider_manager.get_provider(terminal_id)
        use_screen = (
            CAO_PYTE_STATUS
            and provider is not None
            and getattr(provider, "supports_screen_detection", False)
        )

        with self._lock:
            buffer = self._buffers.get(terminal_id, "") + chunk
            if len(buffer) > STATE_BUFFER_MAX:
                buffer = buffer[-STATE_BUFFER_MAX:]
            self._buffers[terminal_id] = buffer
            if use_screen:
                self._feed_screen_locked(terminal_id, chunk)

        if not use_screen:
            # Provider regex analysis can be slow — run it outside the lock.
            self._apply_detection(terminal_id, self._detect_status(terminal_id, buffer))
            return

        self._schedule_screen_detection(terminal_id, provider)

    def _apply_detection(self, terminal_id: str, detected: TerminalStatus) -> None:
        """Apply the sticky-latch rules to a freshly detected status and publish
        on change. Shared by the raw and pyte detection paths.

        Stickiness: once a ready status is latched, refuse downgrades unless
        notify_input_sent() armed a revert. Two kinds of downgrade are blocked:
        1. ready → PROCESSING/UNKNOWN — buffer-eviction / mid-redraw flap.
        2. COMPLETED → IDLE — the response marker evicts before the user marker.
        The arm is consumed only by a genuine PROCESSING transition or an
        init-style non-ready → ready upgrade, never by a ready → ready flap
        (which would block the input's real PROCESSING and let InboxService
        paste into a busy agent).
        """
        with self._lock:
            last = self._last_status.get(terminal_id)
            armed = self._allow_processing_revert.get(terminal_id, False)
            if not armed:
                if last in _STICKY_READY_STATUSES and detected in (
                    TerminalStatus.PROCESSING,
                    TerminalStatus.UNKNOWN,
                ):
                    return
                if last == TerminalStatus.COMPLETED and detected == TerminalStatus.IDLE:
                    return

            if detected == last:
                return

            self._last_status[terminal_id] = detected
            if detected == TerminalStatus.PROCESSING:
                self._allow_processing_revert[terminal_id] = False
            elif detected in _STICKY_READY_STATUSES and last not in _STICKY_READY_STATUSES:
                self._allow_processing_revert[terminal_id] = False

        # Publish outside the lock — subscribers must never be able to
        # re-enter StatusMonitor while the latch state is mid-update.
        bus.publish(f"terminal.{terminal_id}.status", {"status": detected.value})
        logger.info(f"Terminal {terminal_id} status changed: {detected.value}")

    # ----- pyte rendered-screen detection (edge-debounced) -------------------

    def _feed_screen_locked(self, terminal_id: str, chunk: str) -> None:
        """Feed a chunk into the terminal's pyte screen. Caller holds the lock.

        Lazily creates the Screen+Stream so pyte is only imported/used when the
        screen path is active for this terminal.
        """
        scr = self._screens.get(terminal_id)
        if scr is None:
            import pyte

            screen = pyte.Screen(PYTE_SCREEN_COLS, PYTE_SCREEN_ROWS)
            stream = pyte.Stream(screen)
            scr = (screen, stream)
            self._screens[terminal_id] = scr
        scr[1].feed(chunk)

    def _detect_screen(self, terminal_id: str, provider) -> TerminalStatus:
        """Detect status from the terminal's composited pyte screen."""
        with self._lock:
            scr = self._screens.get(terminal_id)
            lines: List[str] = list(scr[0].display) if scr is not None else []
        if not lines or provider is None:
            return TerminalStatus.UNKNOWN
        try:
            return provider.get_status_from_screen(lines)
        except Exception as e:
            logger.error(f"Error detecting screen status for {terminal_id}: {e}")
            return TerminalStatus.UNKNOWN

    def _schedule_screen_detection(self, terminal_id: str, provider) -> None:
        """Edge-debounce detection on the pyte screen.

        Rising edge (first chunk after quiet) → detect immediately (catches the
        PROCESSING transition the instant work resumes). Quiescence (no new
        chunk for PYTE_QUIESCENCE_DELAY_S) → detect again (the TUI repaint has
        settled, so the screen shows the true end state). Detection NEVER runs
        mid-burst, which is what eliminates the flaps naive per-chunk rendered
        detection produces.
        """
        loop = self._running_loop()
        if loop is None:
            # No event loop (unit tests / offline replay): detect immediately
            # on the current screen — deterministic, no timing.
            self._apply_detection(terminal_id, self._detect_screen(terminal_id, provider))
            return

        with self._lock:
            was_bursting = self._bursting.get(terminal_id, False)
            self._bursting[terminal_id] = True
            handle = self._quiesce_handle.pop(terminal_id, None)
        if handle is not None:
            handle.cancel()

        if not was_bursting:
            self._apply_detection(terminal_id, self._detect_screen(terminal_id, provider))

        new_handle = loop.call_later(
            PYTE_QUIESCENCE_DELAY_S, self._on_screen_quiescent, terminal_id, provider
        )
        with self._lock:
            self._quiesce_handle[terminal_id] = new_handle

    def _on_screen_quiescent(self, terminal_id: str, provider) -> None:
        """Quiescence timer fired: output stopped, so the screen has settled."""
        with self._lock:
            self._bursting[terminal_id] = False
            self._quiesce_handle.pop(terminal_id, None)
        self._apply_detection(terminal_id, self._detect_screen(terminal_id, provider))

    @staticmethod
    def _running_loop() -> Optional[asyncio.AbstractEventLoop]:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def notify_input_sent(self, terminal_id: str) -> None:
        """Arm the next PROCESSING transition.

        Call before any send_keys / paste that initiates a new processing
        cycle (terminal_service.send_input, provider.initialize warm-up
        and CLI-launch keystrokes). Without this, a previously-latched
        IDLE/COMPLETED would block the genuine PROCESSING transition.
        """
        with self._lock:
            self._allow_processing_revert[terminal_id] = True

    def _detect_status(self, terminal_id: str, buffer: str) -> TerminalStatus:
        """Detect status: provider-specific patterns or UNKNOWN if no provider."""
        provider = provider_manager.get_provider(terminal_id)
        if provider is None:
            return TerminalStatus.UNKNOWN

        try:
            return provider.get_status(buffer)
        except Exception as e:
            logger.error(f"Error detecting status for {terminal_id}: {e}")
            return TerminalStatus.UNKNOWN

    def clear_terminal(self, terminal_id: str) -> None:
        """Free buffer and status for a deleted terminal."""
        with self._lock:
            self._buffers.pop(terminal_id, None)
            self._last_status.pop(terminal_id, None)
            self._allow_processing_revert.pop(terminal_id, None)
            self._screens.pop(terminal_id, None)
            self._bursting.pop(terminal_id, None)
            handle = self._quiesce_handle.pop(terminal_id, None)
        if handle is not None:
            handle.cancel()

    def reset_buffer(self, terminal_id: str) -> None:
        """Clear the rolling buffer + last-known status WITHOUT forgetting the
        terminal.

        Used when a provider relaunches a different CLI mode on the SAME
        ``terminal_id`` (e.g. Kiro's TUI -> ``--legacy-ui`` fallback). Without
        this, the retry re-derives status from a buffer still full of stale bytes
        from the failed first attempt and can spuriously time out.
        """
        with self._lock:
            self._buffers[terminal_id] = ""
            self._last_status.pop(terminal_id, None)
            self._allow_processing_revert.pop(terminal_id, None)
            # Drop the rendered screen too so the relaunched CLI mode is
            # detected against a fresh viewport, not the failed attempt's.
            self._screens.pop(terminal_id, None)
            self._bursting.pop(terminal_id, None)
            handle = self._quiesce_handle.pop(terminal_id, None)
        if handle is not None:
            handle.cancel()

    def get_status(self, terminal_id: str) -> TerminalStatus:
        """Get current terminal status — the single source of truth for both backends.

        Pipe-pane backends (tmux) return the last status pushed by the FIFO →
        EventBus → _process_chunk pipeline. Event-inbox backends (herdr) don't
        feed that pipeline (no FIFO reader is started for them), so _last_status
        would stay UNKNOWN forever; for those we derive status on demand from the
        provider, whose get_status() consults backend.get_native_status(). Doing
        it here means every caller (API status, init waits, busy checks, curator
        liveness) works on herdr without each having to special-case the backend.
        """
        from cli_agent_orchestrator.backends.registry import get_backend

        if get_backend().supports_event_inbox():
            try:
                provider = provider_manager.get_provider(terminal_id)
            except Exception:
                provider = None
            if provider is not None:
                with self._lock:
                    buffer = self._buffers.get(terminal_id, "")
                try:
                    # The native (herdr) path ignores the buffer arg; pass the
                    # rolling buffer (empty for herdr) so the rare
                    # get_native_status()==None fallback still gets what we have.
                    # provider.get_status may shell out to the herdr CLI — call
                    # it outside the lock.
                    return provider.get_status(buffer)
                except Exception as e:
                    logger.error(f"Error deriving native status for {terminal_id}: {e}")
                    return TerminalStatus.UNKNOWN

        with self._lock:
            return self._last_status.get(terminal_id, TerminalStatus.UNKNOWN)

    def get_buffer(self, terminal_id: str) -> str:
        """Get accumulated output buffer for a terminal."""
        with self._lock:
            return self._buffers.get(terminal_id, "")


# Module-level singleton
status_monitor = StatusMonitor()
