"""Database utilities and initialization."""

import logging

from cli_agent_orchestrator.adapters.database import create_tables
from cli_agent_orchestrator.constants import DATABASE_FILE

logger = logging.getLogger(__name__)

# TODO: this file seems very thin. Can we simplify?
def init_database():
    """Initialize database with tables if not already created."""
    try:
        if not DATABASE_FILE.exists():
            logger.info("Database not found, creating tables...")
            create_tables()
            logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise
