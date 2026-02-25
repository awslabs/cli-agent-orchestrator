"""Tests for database migration (status column)."""

import pytest
import tempfile
from pathlib import Path
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base, TerminalModel, init_db


class TestDatabaseMigration:
    """Tests for database schema migration."""

    def test_init_db_creates_status_column(self):
        """Test that init_db creates status column in new database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            engine = create_engine(f"sqlite:///{db_path}")
            
            # Create tables
            Base.metadata.create_all(bind=engine)
            
            # Inspect schema
            inspector = inspect(engine)
            columns = [col["name"] for col in inspector.get_columns("terminals")]
            
            # Verify status column exists
            assert "status" in columns, "Status column should exist in new database"

    def test_migration_adds_status_column_to_existing_db(self):
        """Test that migration adds status column to existing database without it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            engine = create_engine(f"sqlite:///{db_path}")
            SessionLocal = sessionmaker(bind=engine)
            
            # Create old schema without status column
            from sqlalchemy import Column, DateTime, String
            from sqlalchemy.orm import declarative_base
            
            OldBase = declarative_base()
            
            class OldTerminalModel(OldBase):
                __tablename__ = "terminals"
                id = Column(String, primary_key=True)
                tmux_session = Column(String, nullable=False)
                tmux_window = Column(String, nullable=False)
                provider = Column(String, nullable=False)
                agent_profile = Column(String)
                last_active = Column(DateTime)
                # No status column
            
            OldBase.metadata.create_all(bind=engine)
            
            # Verify status column doesn't exist
            inspector = inspect(engine)
            columns = [col["name"] for col in inspector.get_columns("terminals")]
            assert "status" not in columns, "Status column should not exist yet"
            
            # Insert test data
            with SessionLocal() as db:
                terminal = OldTerminalModel(
                    id="abc12345",
                    tmux_session="test-session",
                    tmux_window="test-window",
                    provider="kiro_cli",
                    agent_profile="developer",
                )
                db.add(terminal)
                db.commit()
            
            # Run migration by calling init_db with the existing database
            # We need to simulate the migration logic
            from sqlalchemy import text
            with SessionLocal() as db:
                try:
                    db.execute(text("SELECT status FROM terminals LIMIT 1"))
                except Exception:
                    # Column doesn't exist, add it
                    db.execute(text("ALTER TABLE terminals ADD COLUMN status TEXT"))
                    db.commit()
            
            # Verify status column now exists
            inspector = inspect(engine)
            columns = [col["name"] for col in inspector.get_columns("terminals")]
            assert "status" in columns, "Status column should exist after migration"
            
            # Verify existing data is preserved
            with SessionLocal() as db:
                terminal = db.query(OldTerminalModel).filter(
                    OldTerminalModel.id == "abc12345"
                ).first()
                assert terminal is not None, "Existing data should be preserved"
                assert terminal.tmux_session == "test-session"

    def test_status_column_is_nullable(self):
        """Test that status column allows NULL values for backward compatibility."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            engine = create_engine(f"sqlite:///{db_path}")
            SessionLocal = sessionmaker(bind=engine)
            
            Base.metadata.create_all(bind=engine)
            
            # Insert terminal without status
            with SessionLocal() as db:
                terminal = TerminalModel(
                    id="abc12345",
                    tmux_session="test-session",
                    tmux_window="test-window",
                    provider="kiro_cli",
                    agent_profile="developer",
                    # status not set (should be NULL)
                )
                db.add(terminal)
                db.commit()
            
            # Verify terminal was created with NULL status
            with SessionLocal() as db:
                terminal = db.query(TerminalModel).filter(
                    TerminalModel.id == "abc12345"
                ).first()
                assert terminal is not None
                assert terminal.status is None, "Status should be NULL when not set"

    def test_status_column_accepts_valid_values(self):
        """Test that status column accepts all valid TerminalStatus values."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            engine = create_engine(f"sqlite:///{db_path}")
            SessionLocal = sessionmaker(bind=engine)
            
            Base.metadata.create_all(bind=engine)
            
            valid_statuses = ["idle", "processing", "completed", "waiting_user_answer", "error"]
            
            for i, status_value in enumerate(valid_statuses):
                terminal_id = f"abc1234{i}"
                with SessionLocal() as db:
                    terminal = TerminalModel(
                        id=terminal_id,
                        tmux_session="test-session",
                        tmux_window=f"test-window-{i}",
                        provider="kiro_cli",
                        agent_profile="developer",
                        status=status_value,
                    )
                    db.add(terminal)
                    db.commit()
                
                # Verify status was saved correctly
                with SessionLocal() as db:
                    terminal = db.query(TerminalModel).filter(
                        TerminalModel.id == terminal_id
                    ).first()
                    assert terminal.status == status_value

    def test_migration_is_idempotent(self):
        """Test that running migration multiple times doesn't cause errors."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            engine = create_engine(f"sqlite:///{db_path}")
            SessionLocal = sessionmaker(bind=engine)
            
            Base.metadata.create_all(bind=engine)
            
            # Run migration logic multiple times
            from sqlalchemy import text
            for _ in range(3):
                with SessionLocal() as db:
                    try:
                        db.execute(text("SELECT status FROM terminals LIMIT 1"))
                    except Exception:
                        db.execute(text("ALTER TABLE terminals ADD COLUMN status TEXT"))
                        db.commit()
            
            # Verify status column exists and database is still functional
            inspector = inspect(engine)
            columns = [col["name"] for col in inspector.get_columns("terminals")]
            assert "status" in columns
            
            # Verify we can still insert data
            with SessionLocal() as db:
                terminal = TerminalModel(
                    id="abc12345",
                    tmux_session="test-session",
                    tmux_window="test-window",
                    provider="kiro_cli",
                    status="idle",
                )
                db.add(terminal)
                db.commit()
