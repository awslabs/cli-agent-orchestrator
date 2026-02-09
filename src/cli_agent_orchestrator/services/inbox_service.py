"""Inbox service with watchdog for automatic message delivery.

This module provides the inbox functionality for agent-to-agent communication,
using file system monitoring to detect when agents become idle and can receive messages.

Architecture:
- Messages are queued in the database (inbox table) via send_message MCP tool
- LogFileHandler monitors terminal log files for changes using watchdog
- When a terminal becomes idle (detected via log patterns), pending messages are delivered
- Messages are sent via terminal_service.send_input() which types into the tmux pane

Message Flow:
1. Agent A calls send_message(terminal_id, message) → message queued in DB
2. Agent B's terminal log file updates (via tmux pipe-pane)
3. LogFileHandler.on_modified() triggered → checks for pending messages
4. If terminal is IDLE and has pending messages → deliver via send_input()
5. Message status updated to DELIVERED or FAILED

Performance Optimization:
- Uses fast log tail check before expensive tmux status queries
- Only queries full provider status when idle pattern detected in log
"""

import logging
import re
import subprocess
from pathlib import Path

from watchdog.events import FileModifiedEvent, FileSystemEventHandler

from cli_agent_orchestrator.clients.database import get_pending_messages, update_message_status
from cli_agent_orchestrator.constants import TERMINAL_LOG_DIR
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services import terminal_service

logger = logging.getLogger(__name__)


def _get_log_tail(terminal_id: str, lines: int = 5) -> str:
    """Get last N lines from terminal log file."""
    log_path = TERMINAL_LOG_DIR / f"{terminal_id}.log"
    try:
        result = subprocess.run(
            ["tail", "-n", str(lines), str(log_path)], capture_output=True, text=True, timeout=1
        )
        return result.stdout
    except Exception:
        return ""


def _has_idle_pattern(terminal_id: str) -> bool:
    """Check if log tail contains idle pattern without expensive tmux calls."""
    tail = _get_log_tail(terminal_id)
    if not tail:
        return False

    try:
        provider = provider_manager.get_provider(terminal_id)
        if provider is None:
            return False
        idle_pattern = provider.get_idle_pattern_for_log()
        return bool(re.search(idle_pattern, tail))
    except Exception:
        return False


def check_and_send_pending_messages(terminal_id: str) -> bool:
    """Check for pending messages and send if terminal is ready.

    Args:
        terminal_id: Terminal ID to check messages for

    Returns:
        bool: True if a message was sent, False otherwise

    Raises:
        ValueError: If provider not found for terminal
    """
    # Check for pending messages
    messages = get_pending_messages(terminal_id, limit=1)
    if not messages:
        return False

    message = messages[0]

    # Get provider and check status
    provider = provider_manager.get_provider(terminal_id)
    if provider is None:
        raise ValueError(f"Provider not found for terminal {terminal_id}")
    # Let the provider use its own default tail_lines. Each provider knows how
    # many lines it needs to reliably detect the idle prompt (e.g. Kimi CLI and
    # Gemini CLI need 50 lines due to TUI padding). Previously this passed
    # INBOX_SERVICE_TAIL_LINES=5, which was too few for TUI-based providers —
    # the idle prompt was never found, so messages stayed PENDING forever.
    status = provider.get_status()

    if status not in (TerminalStatus.IDLE, TerminalStatus.COMPLETED):
        logger.debug(f"Terminal {terminal_id} not ready (status={status})")
        return False

    # Send message
    try:
        terminal_service.send_input(terminal_id, message.message)
        update_message_status(message.id, MessageStatus.DELIVERED)
        logger.info(f"Delivered message {message.id} to terminal {terminal_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to send message {message.id} to {terminal_id}: {e}")
        update_message_status(message.id, MessageStatus.FAILED)
        raise


class LogFileHandler(FileSystemEventHandler):
    """Handler for terminal log file changes."""

    def on_modified(self, event):
        """Handle file modification events."""
        if isinstance(event, FileModifiedEvent) and event.src_path.endswith(".log"):
            log_path = Path(event.src_path)
            terminal_id = log_path.stem
            logger.debug(f"Log file modified: {terminal_id}.log")
            self._handle_log_change(terminal_id)

    def _handle_log_change(self, terminal_id: str):
        """Handle log file change and attempt message delivery."""
        try:
            # Check for pending messages first
            messages = get_pending_messages(terminal_id, limit=1)
            if not messages:
                logger.debug(f"No pending messages for {terminal_id}, skipping")
                return

            # Fast check: does log tail have idle pattern?
            if not _has_idle_pattern(terminal_id):
                logger.debug(
                    f"Terminal {terminal_id} not idle (no idle pattern in log tail), skipping"
                )
                return

            # Attempt delivery
            check_and_send_pending_messages(terminal_id)

        except Exception as e:
            logger.error(f"Error handling log change for {terminal_id}: {e}")
