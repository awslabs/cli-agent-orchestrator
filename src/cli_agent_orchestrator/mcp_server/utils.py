"""MCP server utilities."""
from typing import Optional

from cli_agent_orchestrator.adapters.database import SessionLocal, TerminalModel


def get_terminal_record(terminal_id: str) -> Optional[TerminalModel]:
    """Get full terminal record for a given terminal_id from database."""
    db = SessionLocal()
    try:
        terminal_record = db.query(TerminalModel).filter(
            TerminalModel.id == terminal_id
        ).first()
        return terminal_record
    finally:
        db.close()
