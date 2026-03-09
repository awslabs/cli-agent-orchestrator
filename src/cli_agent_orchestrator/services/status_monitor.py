"""Monitors terminal status by accumulating output and detecting changes.

Consumer: terminal.{id}.output
Publisher: terminal.{id}.status
"""

import logging
import re
from typing import Dict

from cli_agent_orchestrator.constants import SHELL_PROMPT_PATTERN, STATE_BUFFER_MAX
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.utils.event import terminal_id_from_topic

logger = logging.getLogger(__name__)


class StatusMonitor:
    """Accumulates terminal output into rolling buffers and detects status changes."""

    def __init__(self):
        self._buffers: Dict[str, str] = {}
        self._last_status: Dict[str, TerminalStatus] = {}

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

        new_status = self._detect_status(terminal_id, self._buffers[terminal_id])

        if new_status != self._last_status.get(terminal_id):
            bus.publish(f"terminal.{terminal_id}.status", {"status": new_status.value})
            logger.info(f"Terminal {terminal_id} status changed: {new_status.value}")
            self._last_status[terminal_id] = new_status

    def _detect_status(self, terminal_id: str, buffer: str) -> TerminalStatus:
        """Detect status: generic shell prompt if no provider, else provider-specific."""
        provider = provider_manager.get_provider(terminal_id)
        if provider is None:
            if re.search(SHELL_PROMPT_PATTERN, buffer[-500:]):
                return TerminalStatus.IDLE
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

    def get_status(self, terminal_id: str) -> TerminalStatus:
        """Get current terminal status. Source of truth — derived from streaming output."""
        return self._last_status.get(terminal_id, TerminalStatus.UNKNOWN)

    def get_buffer(self, terminal_id: str) -> str:
        """Get accumulated output buffer for a terminal."""
        return self._buffers.get(terminal_id, "")


# Module-level singleton
status_monitor = StatusMonitor()
