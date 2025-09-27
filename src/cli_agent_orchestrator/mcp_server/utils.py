"""MCP server utilities."""
from typing import Optional

from cli_agent_orchestrator.adapters.database import SessionLocal, TerminalModel


def get_session_for_terminal(terminal_id: str) -> Optional[str]:
    """Get session_id for a given terminal_id from database."""
    db = SessionLocal()
    try:
        terminal_record = db.query(TerminalModel).filter(
            TerminalModel.id == terminal_id
        ).first()
        
        if terminal_record:
            return terminal_record.session_id
        return None
    finally:
        db.close()
