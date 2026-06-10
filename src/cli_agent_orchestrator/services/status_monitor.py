"""Monitors terminal status by accumulating output and detecting changes.

Consumer: terminal.{id}.output
Publisher: terminal.{id}.status
"""

import logging
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
        if terminal_id not in self._buffers:
            self._buffers[terminal_id] = ""
        self._buffers[terminal_id] += chunk

        if len(self._buffers[terminal_id]) > STATE_BUFFER_MAX:
            self._buffers[terminal_id] = self._buffers[terminal_id][-STATE_BUFFER_MAX:]

        detected = self._detect_status(terminal_id, self._buffers[terminal_id])
        last = self._last_status.get(terminal_id)

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
        armed = self._allow_processing_revert.get(terminal_id, False)
        if not armed:
            if last in _STICKY_READY_STATUSES and detected in (
                TerminalStatus.PROCESSING,
                TerminalStatus.UNKNOWN,
            ):
                return
            if last == TerminalStatus.COMPLETED and detected == TerminalStatus.IDLE:
                return

        if detected != last:
            bus.publish(f"terminal.{terminal_id}.status", {"status": detected.value})
            logger.info(f"Terminal {terminal_id} status changed: {detected.value}")
            self._last_status[terminal_id] = detected
            # PROCESSING transition consumed; require a fresh notify before
            # the next revert is allowed. Fresh ready latches always re-arm
            # the gate to "blocked".
            if detected in _STICKY_READY_STATUSES:
                self._allow_processing_revert[terminal_id] = False
            elif detected == TerminalStatus.PROCESSING:
                self._allow_processing_revert[terminal_id] = False

    def notify_input_sent(self, terminal_id: str) -> None:
        """Arm the next PROCESSING transition.

        Call before any send_keys / paste that initiates a new processing
        cycle (terminal_service.send_input, provider.initialize warm-up
        and CLI-launch keystrokes). Without this, a previously-latched
        IDLE/COMPLETED would block the genuine PROCESSING transition.
        """
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
        self._buffers[terminal_id] = ""
        self._last_status.pop(terminal_id, None)
        self._allow_processing_revert.pop(terminal_id, None)

    def get_status(self, terminal_id: str) -> TerminalStatus:
        """Get current terminal status. Source of truth — derived from streaming output."""
        return self._last_status.get(terminal_id, TerminalStatus.UNKNOWN)

    def get_buffer(self, terminal_id: str) -> str:
        """Get accumulated output buffer for a terminal."""
        return self._buffers.get(terminal_id, "")


# Module-level singleton
status_monitor = StatusMonitor()
