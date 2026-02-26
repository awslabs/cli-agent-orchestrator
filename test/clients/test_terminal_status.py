"""Tests for terminal status database functions."""

import pytest

from cli_agent_orchestrator.clients.database import (
    create_terminal,
    delete_terminal,
    get_terminal_status,
    init_db,
    update_terminal_status,
)


@pytest.fixture(autouse=True)
def setup_db():
    """Initialize database before each test and clean up after."""
    init_db()
    yield
    # Clean up test terminals after each test
    from cli_agent_orchestrator.clients.database import SessionLocal, TerminalModel

    with SessionLocal() as db:
        db.query(TerminalModel).filter(
            TerminalModel.id.in_(
                ["test1234", "test5678", "test9999", "testaaaa", "testbbbb", "testcccc"]
            )
        ).delete(synchronize_session=False)
        db.commit()


class TestTerminalStatus:
    """Tests for terminal status functions."""

    def test_update_terminal_status_success(self):
        """Test updating terminal status successfully."""
        terminal_id = "test1234"
        create_terminal(terminal_id, "session1", "window1", "kiro_cli", "developer")

        result = update_terminal_status(terminal_id, "processing")

        assert result is True
        assert get_terminal_status(terminal_id) == "processing"

    def test_update_terminal_status_not_found(self):
        """Test updating status for non-existent terminal."""
        result = update_terminal_status("nonexist", "idle")

        assert result is False

    def test_update_terminal_status_updates_last_active(self):
        """Test that updating status also updates last_active timestamp."""
        from datetime import datetime, timedelta
        from cli_agent_orchestrator.clients.database import SessionLocal, TerminalModel

        terminal_id = "test5678"
        create_terminal(terminal_id, "session1", "window1", "kiro_cli", "developer")

        # Get initial last_active
        with SessionLocal() as db:
            terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
            initial_last_active = terminal.last_active

        # Wait a tiny bit and update status
        import time

        time.sleep(0.01)
        update_terminal_status(terminal_id, "processing")

        # Check last_active was updated
        with SessionLocal() as db:
            terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
            assert terminal.last_active > initial_last_active

    def test_get_terminal_status_success(self):
        """Test getting terminal status successfully."""
        terminal_id = "test9999"
        create_terminal(terminal_id, "session1", "window1", "kiro_cli", "developer")
        update_terminal_status(terminal_id, "idle")

        status = get_terminal_status(terminal_id)

        assert status == "idle"

    def test_get_terminal_status_not_found(self):
        """Test getting status for non-existent terminal."""
        status = get_terminal_status("nonexist")

        assert status is None

    def test_status_persists_across_updates(self):
        """Test that status persists correctly across multiple updates."""
        terminal_id = "testaaaa"
        create_terminal(terminal_id, "session1", "window1", "kiro_cli", "developer")

        # Update through different statuses
        update_terminal_status(terminal_id, "idle")
        assert get_terminal_status(terminal_id) == "idle"

        update_terminal_status(terminal_id, "processing")
        assert get_terminal_status(terminal_id) == "processing"

        update_terminal_status(terminal_id, "completed")
        assert get_terminal_status(terminal_id) == "completed"

        update_terminal_status(terminal_id, "error")
        assert get_terminal_status(terminal_id) == "error"

    def test_status_deleted_with_terminal(self):
        """Test that status is deleted when terminal is deleted."""
        terminal_id = "testbbbb"
        create_terminal(terminal_id, "session1", "window1", "kiro_cli", "developer")
        update_terminal_status(terminal_id, "idle")

        # Verify status exists
        assert get_terminal_status(terminal_id) == "idle"

        # Delete terminal
        delete_terminal(terminal_id)

        # Verify status is gone
        assert get_terminal_status(terminal_id) is None

    def test_update_status_with_special_values(self):
        """Test updating status with all valid enum values."""
        terminal_id = "testcccc"
        create_terminal(terminal_id, "session1", "window1", "kiro_cli", "developer")

        valid_statuses = ["idle", "processing", "completed", "waiting_user_answer", "error"]

        for status_value in valid_statuses:
            result = update_terminal_status(terminal_id, status_value)
            assert result is True
            assert get_terminal_status(terminal_id) == status_value
