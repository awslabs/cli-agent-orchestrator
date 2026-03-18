"""Writes terminal output to per-terminal log files for debugging.

Consumer: terminal.{id}.output
"""

import asyncio
import logging

from cli_agent_orchestrator.constants import TERMINAL_LOG_DIR
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.utils.event import terminal_id_from_topic

logger = logging.getLogger(__name__)


class LogWriter:
    """Appends terminal output chunks to log files."""

    async def run(self) -> None:
        queue = bus.subscribe("terminal.*.output")
        logger.info("LogWriter started")

        while True:
            try:
                event = await queue.get()
                terminal_id = terminal_id_from_topic(event["topic"])
                log_path = TERMINAL_LOG_DIR / f"{terminal_id}.log"
                await asyncio.to_thread(self._write, log_path, event["data"]["data"])
            except Exception as e:
                logger.error(f"Failed to write log: {e}")

    @staticmethod
    def _write(path, data: str) -> None:
        with open(path, "a") as f:
            f.write(data)


log_writer = LogWriter()
