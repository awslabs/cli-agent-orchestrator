"""Monitors terminal status by accumulating output and detecting changes.

Consumer: terminal.{id}.output
Publisher: terminal.{id}.status
"""

import logging
import threading
from typing import Dict

from cli_agent_orchestrator.constants import STATE_BUFFER_MAX
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
        """Append chunk to rolling buffer and check for status changes."""
        with self._lock:
            buffer = self._buffers.get(terminal_id, "") + chunk
            if len(buffer) > STATE_BUFFER_MAX:
                buffer = buffer[-STATE_BUFFER_MAX:]
            self._buffers[terminal_id] = buffer

        # Provider regex analysis can be slow — run it outside the lock.
        detected = self._detect_status(terminal_id, buffer)

        # Stickiness: once a ready status is latched, refuse downgrades unless
        # notify_input_sent() armed a revert.
        #
        # Two kinds of downgrade are blocked:
        # 1. ready → PROCESSING/UNKNOWN — the typical buffer-eviction flap
        #    (TUI redraws push the idle/response markers out of the 8KB
        #    window, so the per-provider get_status() falls through to
        #    PROCESSING). This is what wait_until_status loses.
        # 2. COMPLETED → IDLE — the assistant-response evicts before the
        #    user-message marker does, so the next chunk loses ``last_user``
        #    and providers like codex fall back to IDLE. Without this guard,
        #    IDLE silently overwrites COMPLETED and tests that wait
        #    specifically for COMPLETED time out.
        #
        # Why: the per-provider get_status() detects PROCESSING/IDLE/COMPLETED
        # by scanning the rolling 8KB buffer. TUI redraws keep emitting bytes
        # for seconds AFTER the agent has settled, eventually evicting the
        # response/idle markers from the 8KB window. Without this latch,
        # status flaps rapidly between ready and PROCESSING/UNKNOWN/IDLE, and
        # both wait_until_status (server-side) and the e2e tests' HTTP
        # polling miss the brief ready windows — manifesting as PR #273 codex
        # 60s init timeouts, gemini 240s init timeouts, and completion
        # timeouts.
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
            # Consume the arm on the transitions that mean "the cycle the
            # input started has been observed":
            # - PROCESSING: the intended consumption — the agent picked up
            #   the input; subsequent ready latches re-block flaps.
            # - non-ready → ready: init-style upgrade (UNKNOWN/PROCESSING →
            #   IDLE); the cycle completed without a visible PROCESSING
            #   window.
            # A ready → ready transition while armed must KEEP the arm: it is
            # an eviction flap (e.g. COMPLETED → IDLE when a large paste
            # evicts the response markers, or WAITING_USER_ANSWER → IDLE
            # after a permission keystroke), and the input's genuine
            # PROCESSING transition hasn't been seen yet. Consuming the arm
            # here would block that PROCESSING, leaving the terminal reading
            # "ready" while the agent is busy — and InboxService delivers on
            # IDLE/COMPLETED, so a queued message could be pasted into the
            # middle of an active response.
            if detected == TerminalStatus.PROCESSING:
                self._allow_processing_revert[terminal_id] = False
            elif detected in _STICKY_READY_STATUSES and last not in _STICKY_READY_STATUSES:
                self._allow_processing_revert[terminal_id] = False

        # Publish outside the lock — subscribers must never be able to
        # re-enter StatusMonitor while the latch state is mid-update.
        bus.publish(f"terminal.{terminal_id}.status", {"status": detected.value})
        logger.info(f"Terminal {terminal_id} status changed: {detected.value}")

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
