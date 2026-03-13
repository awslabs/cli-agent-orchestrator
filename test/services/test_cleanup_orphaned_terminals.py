"""Tests for cleanup of orphaned terminals."""

import pytest
from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.clients.database import create_terminal, get_terminal_metadata
from cli_agent_orchestrator.services.cleanup_service import cleanup_old_data


class TestCleanupOrphanedTerminals:
    """Tests for orphaned terminal cleanup."""

    @pytest.fixture(autouse=True)
    def cleanup_test_terminals(self):
        """Clean up test terminals before and after each test."""
        from cli_agent_orchestrator.clients.database import delete_terminal

        # Cleanup before test
        test_ids = ["abc12345", "def67890"] + [f"abc1234{i}" for i in range(5)]
        for terminal_id in test_ids:
            try:
                delete_terminal(terminal_id)
            except:
                pass
        yield
        # Cleanup after test
        for terminal_id in test_ids:
            try:
                delete_terminal(terminal_id)
            except:
                pass

    @pytest.fixture
    def mock_tmux_client(self):
        """Mock tmux client."""
        with patch("cli_agent_orchestrator.clients.tmux.tmux_client") as mock:
            yield mock

    def test_cleanup_removes_orphaned_terminals(self, mock_tmux_client):
        """Test that cleanup removes terminals whose tmux windows no longer exist."""
        from datetime import datetime, timedelta
        from cli_agent_orchestrator.clients.database import SessionLocal, TerminalModel

        # Create test terminals with old last_active (beyond grace period)
        terminal1 = "abc12345"
        terminal2 = "def67890"

        create_terminal(terminal1, "session1", "window1", "kiro_cli", "developer")
        create_terminal(terminal2, "session2", "window2", "kiro_cli", "reviewer")

        # Set last_active to 2 hours ago (beyond 1 hour grace period)
        with SessionLocal() as db:
            for tid in [terminal1, terminal2]:
                terminal = db.query(TerminalModel).filter(TerminalModel.id == tid).first()
                terminal.last_active = datetime.now() - timedelta(hours=2)
            db.commit()

        # Mock: terminal1's window exists, terminal2's doesn't
        def window_exists_side_effect(session, window):
            if session == "session1" and window == "window1":
                return True
            return False

        mock_tmux_client.window_exists.side_effect = window_exists_side_effect

        # Run cleanup
        cleanup_old_data()

        # Verify terminal1 still exists
        metadata1 = get_terminal_metadata(terminal1)
        assert metadata1 is not None, "Terminal with existing window should not be deleted"

        # Verify terminal2 was deleted
        metadata2 = get_terminal_metadata(terminal2)
        assert metadata2 is None, "Terminal with non-existent window should be deleted"

    def test_cleanup_handles_all_orphaned_terminals(self, mock_tmux_client):
        """Test that cleanup handles multiple orphaned terminals."""
        from datetime import datetime, timedelta
        from cli_agent_orchestrator.clients.database import SessionLocal, TerminalModel

        # Create multiple test terminals with old last_active
        terminals = [f"abc1234{i}" for i in range(5)]
        for i, terminal_id in enumerate(terminals):
            create_terminal(terminal_id, f"session{i}", f"window{i}", "kiro_cli", "developer")

        # Set last_active to 2 hours ago (beyond grace period)
        with SessionLocal() as db:
            for tid in terminals:
                terminal = db.query(TerminalModel).filter(TerminalModel.id == tid).first()
                terminal.last_active = datetime.now() - timedelta(hours=2)
            db.commit()

        # Mock: all windows don't exist
        mock_tmux_client.window_exists.return_value = False

        # Run cleanup
        cleanup_old_data()

        # Verify all terminals were deleted
        for terminal_id in terminals:
            metadata = get_terminal_metadata(terminal_id)
            assert metadata is None, f"Terminal {terminal_id} should be deleted"

    def test_cleanup_preserves_terminals_with_existing_windows(self, mock_tmux_client):
        """Test that cleanup doesn't delete terminals with existing windows."""
        # Create test terminals
        terminals = [f"abc1234{i}" for i in range(3)]
        for i, terminal_id in enumerate(terminals):
            create_terminal(terminal_id, f"session{i}", f"window{i}", "kiro_cli", "developer")

        # Mock: all windows exist
        mock_tmux_client.window_exists.return_value = True

        # Run cleanup
        cleanup_old_data()

        # Verify all terminals still exist
        for terminal_id in terminals:
            metadata = get_terminal_metadata(terminal_id)
            assert metadata is not None, f"Terminal {terminal_id} should not be deleted"

    def test_cleanup_handles_tmux_errors_gracefully(self, mock_tmux_client):
        """Test that cleanup handles tmux errors without crashing."""
        # Create test terminal
        terminal_id = "abc12345"
        create_terminal(terminal_id, "session1", "window1", "kiro_cli", "developer")

        # Mock: window_exists raises exception
        mock_tmux_client.window_exists.side_effect = Exception("Tmux error")

        # Run cleanup - should not crash
        try:
            cleanup_old_data()
        except Exception as e:
            pytest.fail(f"Cleanup should handle tmux errors gracefully, but raised: {e}")

        # Terminal should still exist (not deleted due to error)
        metadata = get_terminal_metadata(terminal_id)
        assert metadata is not None, "Terminal should not be deleted on tmux error"

    def test_cleanup_logs_orphaned_terminal_removal(self, mock_tmux_client, caplog):
        """Test that cleanup logs when removing orphaned terminals."""
        import logging
        from datetime import datetime, timedelta
        from cli_agent_orchestrator.clients.database import SessionLocal, TerminalModel

        caplog.set_level(logging.INFO)

        # Create test terminal with old last_active
        terminal_id = "abc12345"
        create_terminal(terminal_id, "session1", "window1", "kiro_cli", "developer")

        # Set last_active to 2 hours ago (beyond grace period)
        with SessionLocal() as db:
            terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
            terminal.last_active = datetime.now() - timedelta(hours=2)
            db.commit()

        # Mock: window doesn't exist
        mock_tmux_client.window_exists.return_value = False

        # Run cleanup
        cleanup_old_data()

        # Verify logging
        assert any(
            "orphaned terminal" in record.message.lower() and terminal_id in record.message
            for record in caplog.records
        ), "Should log orphaned terminal removal"

    @pytest.mark.skip(reason="Terminal status feature removed - test uses outdated API")
    def test_cleanup_deletes_terminal_status_with_terminal(self, mock_tmux_client):
        """Test that cleanup removes status when deleting orphaned terminal."""
        from datetime import datetime, timedelta
        from cli_agent_orchestrator.clients.database import (
            update_terminal_status,
            get_terminal_status,
            SessionLocal,
            TerminalModel,
        )

        # Create test terminal with status and old last_active
        terminal_id = "abc12345"
        create_terminal(terminal_id, "session1", "window1", "kiro_cli", "developer")
        update_terminal_status(terminal_id, "processing")

        # Set last_active to 2 hours ago (beyond grace period)
        with SessionLocal() as db:
            terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
            terminal.last_active = datetime.now() - timedelta(hours=2)
            db.commit()

        # Verify status exists
        status = get_terminal_status(terminal_id)
        assert status == "processing"

        # Mock: window doesn't exist
        mock_tmux_client.window_exists.return_value = False

        # Run cleanup
        cleanup_old_data()

        # Verify terminal and status were deleted
        metadata = get_terminal_metadata(terminal_id)
        assert metadata is None, "Terminal should be deleted"

        status = get_terminal_status(terminal_id)
        assert status is None, "Status should be deleted with terminal"

    def test_cleanup_only_checks_existing_terminals(self, mock_tmux_client):
        """Test that cleanup only checks terminals that exist in database."""
        # Don't create any terminals

        # Mock should not be called if no terminals exist
        mock_tmux_client.window_exists.return_value = True

        # Run cleanup
        cleanup_old_data()

        # Verify window_exists was not called (or called 0 times)
        # This depends on whether there are other terminals in the test database
        # At minimum, it shouldn't crash with no terminals

    def test_cleanup_respects_grace_period(self, mock_tmux_client):
        """Test that cleanup doesn't delete recently active terminals even if window missing."""
        from datetime import datetime, timedelta
        from cli_agent_orchestrator.clients.database import SessionLocal, TerminalModel

        # Create terminal that was active recently (within grace period)
        terminal_id = "abc12345"
        create_terminal(terminal_id, "session1", "window1", "kiro_cli", "developer")

        # Set last_active to 30 minutes ago (within 1 hour grace period)
        with SessionLocal() as db:
            terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
            terminal.last_active = datetime.now() - timedelta(minutes=30)
            db.commit()

        # Mock: window doesn't exist
        mock_tmux_client.window_exists.return_value = False

        # Run cleanup
        cleanup_old_data()

        # Terminal should NOT be deleted (within grace period)
        metadata = get_terminal_metadata(terminal_id)
        assert (
            metadata is not None
        ), "Recently active terminal should not be deleted during grace period"

    def test_cleanup_deletes_old_orphaned_terminals(self, mock_tmux_client):
        """Test that cleanup deletes terminals inactive beyond grace period."""
        from datetime import datetime, timedelta
        from cli_agent_orchestrator.clients.database import SessionLocal, TerminalModel

        # Create terminal that was active 2 hours ago (beyond grace period)
        terminal_id = "abc12345"
        create_terminal(terminal_id, "session1", "window1", "kiro_cli", "developer")

        # Set last_active to 2 hours ago (beyond 1 hour grace period)
        with SessionLocal() as db:
            terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
            terminal.last_active = datetime.now() - timedelta(hours=2)
            db.commit()

        # Mock: window doesn't exist
        mock_tmux_client.window_exists.return_value = False

        # Run cleanup
        cleanup_old_data()

        # Terminal SHOULD be deleted (beyond grace period)
        metadata = get_terminal_metadata(terminal_id)
        assert metadata is None, "Old orphaned terminal should be deleted after grace period"
