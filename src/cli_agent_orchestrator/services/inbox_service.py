"""Delivers queued inbox messages when terminals become ready.

Consumer: terminal.{id}.status
"""

import logging

from cli_agent_orchestrator.clients.database import get_pending_messages, update_message_status
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.utils.event import terminal_id_from_topic

logger = logging.getLogger(__name__)


class InboxService:
    """Delivers one pending message per terminal per IDLE cycle."""

    async def run(self) -> None:
        queue = bus.subscribe("terminal.*.status")
        logger.info("InboxService started")

        while True:
            try:
                event = await queue.get()
                status_value = event["data"]["status"]
                if status_value in (TerminalStatus.IDLE.value, TerminalStatus.COMPLETED.value):
                    terminal_id = terminal_id_from_topic(event["topic"])
                    self.deliver_pending(terminal_id)
            except Exception as e:
                logger.error(f"Error in InboxService: {e}")

    def deliver_pending(self, terminal_id: str) -> None:
        """Deliver oldest pending message to terminal if it's ready."""
        messages = get_pending_messages(terminal_id, limit=1)
        if not messages:
            return

        message = messages[0]
        status = status_monitor.get_status(terminal_id)

        if status not in (TerminalStatus.IDLE, TerminalStatus.COMPLETED):
            return

        try:
            terminal_service.send_input(terminal_id, message.message)
            update_message_status(message.id, MessageStatus.DELIVERED)
            logger.info(f"Delivered message {message.id} to terminal {terminal_id}")
        except Exception as e:
            logger.error(f"Failed to deliver message {message.id} to {terminal_id}: {e}")
            update_message_status(message.id, MessageStatus.FAILED)


inbox_service = InboxService()
