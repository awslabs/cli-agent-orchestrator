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

from cli_agent_orchestrator.clients.database import (
    get_pending_messages,
    list_pending_receiver_ids_by_provider,
    list_pending_receiver_ids_older_than,
    update_message_status,
)
from cli_agent_orchestrator.constants import (
    EAGER_INBOX_DELIVERY,
    INBOX_RECONCILE_GRACE_SECONDS,
    TERMINAL_LOG_DIR,
)
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services import terminal_service

logger = logging.getLogger(__name__)


def _get_log_tail(terminal_id: str, lines: int = 100) -> str:
    """Get last N lines from terminal log file.

    Default of 100 lines covers full-screen TUI providers where the idle
    prompt sits mid-screen with 30+ padding lines below it.
    Reading 100 lines via tail is still sub-millisecond.
    """
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


def check_and_send_pending_messages(
    terminal_id: str, registry: PluginRegistry | None = None
) -> bool:
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
    # many lines it needs to reliably detect the idle prompt (TUI providers
    # need 50 lines due to TUI padding). Previously this passed
    # INBOX_SERVICE_TAIL_LINES=5, which was too few for TUI-based providers —
    # the idle prompt was never found, so messages stayed PENDING forever.
    status = provider.get_status()

    eager_eligible = (
        EAGER_INBOX_DELIVERY
        and provider.accepts_input_while_processing
        and status in (TerminalStatus.PROCESSING, TerminalStatus.WAITING_USER_ANSWER)
    )

    if status not in (TerminalStatus.IDLE, TerminalStatus.COMPLETED) and not eager_eligible:
        logger.debug(f"Terminal {terminal_id} not ready (status={status})")
        return False

    # Send message. Inbox-queued delivery is only reached via the send_message
    # MCP tool, so the orchestration_type is always "send_message" here — the
    # synchronous handoff/assign paths bypass the inbox and pass their own
    # orchestration_type directly to send_input().
    try:
        if registry is None:
            terminal_service.send_input(terminal_id, message.message)
        else:
            terminal_service.send_input(
                terminal_id,
                message.message,
                registry=registry,
                sender_id=message.sender_id,
                orchestration_type=OrchestrationType.SEND_MESSAGE,
            )
        update_message_status(message.id, MessageStatus.DELIVERED)
        logger.info(f"Delivered message {message.id} to terminal {terminal_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to send message {message.id} to {terminal_id}: {e}")
        update_message_status(message.id, MessageStatus.FAILED)
        raise


def poll_opencode_pending_messages(registry: PluginRegistry | None = None) -> None:
    """Poll OpenCode terminals for pending inbox messages.

    This is a temporary OpenCode-specific wakeup path for providers whose
    pipe-pane logs do not change after the TUI settles. It intentionally reuses
    the existing delivery helper and inherits its known duplicate-wakeup race
    with immediate and watchdog delivery paths. GH #115 tracks replacing these
    paths with a single coordinated delivery engine.
    """
    receiver_ids = list_pending_receiver_ids_by_provider(ProviderType.OPENCODE_CLI.value)

    for terminal_id in receiver_ids:
        try:
            check_and_send_pending_messages(terminal_id, registry=registry)
        except Exception as e:
            logger.debug(f"OpenCode inbox poll failed for {terminal_id}: {e}")


def reconcile_orphaned_messages(registry: PluginRegistry | None = None) -> None:
    """Re-attempt delivery for messages stuck in PENDING past the grace window.

    Provider-agnostic safety net for the gap described in issue #131: when a
    receiving terminal is already idle, the immediate (on POST) delivery path
    may miss on a stale status and the log-watching observer never fires again
    (an idle agent produces no new log output), leaving the message orphaned.
    This sweep finds any such message and routes it back through the normal
    delivery gate.

    Only messages older than ``INBOX_RECONCILE_GRACE_SECONDS`` are considered,
    so the sweep never competes with the immediate and watchdog paths for
    freshly queued messages — it only adopts ones they have already missed.

    Like ``poll_opencode_pending_messages``, this reuses ``check_and_send_pending_messages``
    and so inherits its known duplicate-wakeup race; the grace window keeps the
    sweep from overlapping the fast paths in practice, and GH #115 tracks the
    single coordinated delivery engine that would make delivery atomic.
    """
    receiver_ids = list_pending_receiver_ids_older_than(INBOX_RECONCILE_GRACE_SECONDS)

    for terminal_id in receiver_ids:
        try:
            check_and_send_pending_messages(terminal_id, registry=registry)
        except Exception as e:
            logger.debug(f"Inbox reconciliation failed for {terminal_id}: {e}")


class LogFileHandler(FileSystemEventHandler):
    """Handler for terminal log file changes."""

    def __init__(self, registry: PluginRegistry | None = None) -> None:
        """Initialize the log file handler with an optional plugin registry."""

        super().__init__()
        self._registry = registry

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
            # Skip for eager-delivery-capable providers — they have no idle pattern
            # during PROCESSING but can still accept input.
            skip_idle_check = False
            if EAGER_INBOX_DELIVERY:
                try:
                    provider = provider_manager.get_provider(terminal_id)
                    if provider and provider.accepts_input_while_processing:
                        skip_idle_check = True
                except Exception as e:
                    logger.debug(f"Eager delivery check failed for {terminal_id}: {e}")

            if not skip_idle_check and not _has_idle_pattern(terminal_id):
                logger.debug(
                    f"Terminal {terminal_id} not idle (no idle pattern in log tail), skipping"
                )
                return

            # Attempt delivery
            check_and_send_pending_messages(terminal_id, registry=self._registry)

        except Exception as e:
            logger.error(f"Error handling log change for {terminal_id}: {e}")
