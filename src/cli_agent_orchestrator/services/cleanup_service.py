"""Cleanup service for old terminals, messages, and logs."""

import logging
from datetime import datetime, timedelta
from pathlib import Path

from cli_agent_orchestrator.constants import RETENTION_DAYS, LOG_DIR, TERMINAL_LOG_DIR
from cli_agent_orchestrator.clients.database import SessionLocal, TerminalModel, InboxModel

logger = logging.getLogger(__name__)


def cleanup_old_data():
    """Clean up terminals, inbox messages, and log files older than RETENTION_DAYS."""
    try:
        cutoff_date = datetime.now() - timedelta(days=RETENTION_DAYS)
        logger.info(f"Starting cleanup of data older than {RETENTION_DAYS} days (before {cutoff_date})")
        
        # Clean up old terminals
        with SessionLocal() as db:
            deleted_terminals = db.query(TerminalModel).filter(
                TerminalModel.last_active < cutoff_date
            ).delete()
            db.commit()
            logger.info(f"Deleted {deleted_terminals} old terminals from database")
        
        # Clean up old inbox messages
        with SessionLocal() as db:
            deleted_messages = db.query(InboxModel).filter(
                InboxModel.created_at < cutoff_date
            ).delete()
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
