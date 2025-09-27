"""Database models and configuration using SQLAlchemy."""

from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

Base = declarative_base()


class SessionModel(Base):
    """SQLAlchemy model for Session."""
    
    __tablename__ = "sessions"
    
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    status = Column(String, nullable=False)  # active, detached, terminated
    
    # Relationships
    terminals = relationship("TerminalModel", back_populates="session", cascade="all, delete-orphan")


class TerminalModel(Base):
    """SQLAlchemy model for Terminal."""
    
    __tablename__ = "terminals"
    
    id = Column(String, primary_key=True)
    session_id = Column(String, ForeignKey("sessions.id"), nullable=False)
    name = Column(String)
    provider = Column(String)  # q_cli, claude_code, etc.
    status = Column(String, nullable=False)  # created, running, completed, failed, terminated
    
    # Relationships
    session = relationship("SessionModel", back_populates="terminals")


# Database configuration
from cli_agent_orchestrator.constants import DB_DIR, DATABASE_URL

DB_DIR.mkdir(parents=True, exist_ok=True)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def create_tables():
    """Create all database tables."""
    Base.metadata.create_all(bind=engine)


def get_db():
    """Get database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
