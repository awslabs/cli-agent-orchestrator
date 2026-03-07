"""Cleanup service for old terminals, messages, and logs."""

import logging
from datetime import datetime, timedelta
from pathlib import Path

from cli_agent_orchestrator.clients.database import InboxModel, SessionLocal, TerminalModel
from cli_agent_orchestrator.constants import LOG_DIR, RETENTION_DAYS, TERMINAL_LOG_DIR

logger = logging.getLogger(__name__)


def cleanup_old_data():
    """Clean up terminals, inbox messages, and log files older than RETENTION_DAYS.

    Also cleans up orphaned terminal records where tmux windows no longer exist.
    """
    try:
        cutoff_date = datetime.now() - timedelta(days=RETENTION_DAYS)
        logger.info(
            f"Starting cleanup of data older than {RETENTION_DAYS} days (before {cutoff_date})"
        )

        # Clean up old terminals
        with SessionLocal() as db:
            deleted_terminals = (
                db.query(TerminalModel).filter(TerminalModel.last_active < cutoff_date).delete()
            )
            db.commit()
            logger.info(f"Deleted {deleted_terminals} old terminals from database")

        # Clean up orphaned terminals (tmux window no longer exists)
        # Use grace period to avoid deleting terminals during temporary tmux unavailability
        from cli_agent_orchestrator.clients.database import delete_terminal
        from cli_agent_orchestrator.clients.tmux import tmux_client

        orphaned_count = 0
        grace_period_hours = 1  # Only delete if inactive for > 1 hour
        grace_cutoff = datetime.now() - timedelta(hours=grace_period_hours)

        with SessionLocal() as db:
            all_terminals = db.query(TerminalModel).all()
            for terminal in all_terminals:
                # Only check terminals that have been inactive for the grace period
                if terminal.last_active and terminal.last_active > grace_cutoff:
                    continue

                # Check if tmux window still exists
                try:
                    if not tmux_client.window_exists(terminal.tmux_session, terminal.tmux_window):
                        logger.info(
                            f"Cleaning up orphaned terminal {terminal.id} - "
                            f"tmux window no longer exists (inactive since {terminal.last_active})"
                        )
                        delete_terminal(terminal.id)
                        orphaned_count += 1
                except Exception as e:
                    # Don't delete on tmux errors - could be temporary unavailability
                    logger.debug(f"Skipping orphan check for {terminal.id} due to tmux error: {e}")

        if orphaned_count > 0:
            logger.info(f"Deleted {orphaned_count} orphaned terminal records")

        # Clean up old inbox messages
        with SessionLocal() as db:
            deleted_messages = (
                db.query(InboxModel).filter(InboxModel.created_at < cutoff_date).delete()
            )
            db.commit()
            logger.info(f"Deleted {deleted_messages} old inbox messages from database")

        # Clean up old terminal log files
        terminal_logs_deleted = 0
        if TERMINAL_LOG_DIR.exists():
            for log_file in TERMINAL_LOG_DIR.glob("*.log"):
                if log_file.stat().st_mtime < cutoff_date.timestamp():
                    log_file.unlink()
                    terminal_logs_deleted += 1
        logger.info(f"Deleted {terminal_logs_deleted} old terminal log files")

        # Clean up old server log files
        server_logs_deleted = 0
        if LOG_DIR.exists():
            for log_file in LOG_DIR.glob("*.log"):
                if log_file.stat().st_mtime < cutoff_date.timestamp():
                    log_file.unlink()
                    server_logs_deleted += 1
        logger.info(f"Deleted {server_logs_deleted} old server log files")

        logger.info("Cleanup completed successfully")

    except Exception as e:
        logger.error(f"Error during cleanup: {e}")
