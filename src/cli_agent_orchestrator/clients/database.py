"""Minimal database client with only terminal metadata."""

from datetime import datetime
from typing import Optional, Dict, List
from sqlalchemy import Column, String, DateTime, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from cli_agent_orchestrator.constants import DATABASE_URL, DB_DIR

Base = declarative_base()


class TerminalModel(Base):
    """SQLAlchemy model for terminal metadata only."""
    
    __tablename__ = "terminals"
    
    id = Column(String, primary_key=True)  # "abc123ef"
    tmux_session = Column(String, nullable=False)  # "cao-session-name"
    tmux_window = Column(String, nullable=False)  # "window-name"
    provider = Column(String, nullable=False)  # "q_cli", "claude_code"
    agent_profile = Column(String)  # "developer", "reviewer" (optional)
    last_active = Column(DateTime, default=datetime.utcnow)


# Module-level singletons
DB_DIR.mkdir(parents=True, exist_ok=True)
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """Initialize database tables."""
    Base.metadata.create_all(bind=engine)


def create_terminal(terminal_id: str, tmux_session: str, tmux_window: str, 
                   provider: str, agent_profile: str = None) -> Dict:
    """Create terminal metadata record."""
    with SessionLocal() as db:
        terminal = TerminalModel(
            id=terminal_id,
            tmux_session=tmux_session,
            tmux_window=tmux_window,
            provider=provider,
            agent_profile=agent_profile
        )
        db.add(terminal)
        db.commit()
        return {
            "id": terminal.id,
            "tmux_session": terminal.tmux_session,
            "tmux_window": terminal.tmux_window,
            "provider": terminal.provider,
            "agent_profile": terminal.agent_profile
        }


def get_terminal_metadata(terminal_id: str) -> Optional[Dict]:
    """Get terminal metadata by ID."""
    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if not terminal:
            return None
        return {
            "id": terminal.id,
            "tmux_session": terminal.tmux_session,
            "tmux_window": terminal.tmux_window,
            "provider": terminal.provider,
            "agent_profile": terminal.agent_profile,
            "last_active": terminal.last_active
        }


def list_terminals_by_session(tmux_session: str) -> List[Dict]:
    """List all terminals in a tmux session."""
    with SessionLocal() as db:
        terminals = db.query(TerminalModel).filter(TerminalModel.tmux_session == tmux_session).all()
        return [
            {
                "id": t.id,
                "tmux_session": t.tmux_session,
                "tmux_window": t.tmux_window,
                "provider": t.provider,
                "agent_profile": t.agent_profile,
                "last_active": t.last_active
            }
            for t in terminals
        ]


def update_last_active(terminal_id: str) -> bool:
    """Update last active timestamp."""
    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if terminal:
            terminal.last_active = datetime.utcnow()
            db.commit()
            return True
        return False


def delete_terminal(terminal_id: str) -> bool:
    """Delete terminal metadata."""
    with SessionLocal() as db:
        deleted = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).delete()
        db.commit()
        return deleted > 0


def delete_terminals_by_session(tmux_session: str) -> int:
    """Delete all terminals in a session."""
    with SessionLocal() as db:
        deleted = db.query(TerminalModel).filter(TerminalModel.tmux_session == tmux_session).delete()
        db.commit()
        return deleted
