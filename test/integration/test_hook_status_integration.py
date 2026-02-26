"""Integration tests for hook-based status tracking."""

import pytest
from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.clients.database import (
    create_terminal,
    get_terminal_status,
    update_terminal_status,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider


class TestHookStatusIntegration:
    """Integration tests for hook-based status tracking."""

    @pytest.fixture
    def mock_tmux_client(self):
        """Mock tmux client."""
        with patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client") as mock:
            yield mock

    @pytest.fixture
    def test_terminal(self):
        """Create test terminal in database."""
        terminal_id = "abc12345"
        create_terminal(
            terminal_id=terminal_id,
            tmux_session="test-session",
            tmux_window="test-window",
            provider="kiro_cli",
            agent_profile="developer",
        )
        yield terminal_id
        # Cleanup
        from cli_agent_orchestrator.clients.database import delete_terminal

        delete_terminal(terminal_id)

    def test_hook_status_takes_priority_over_polling(self, test_terminal, mock_tmux_client):
        """Test that database status from hooks overrides tmux polling."""
        # Set hook status to "processing"
        update_terminal_status(test_terminal, "processing")

        # Mock tmux output showing IDLE prompt
        mock_tmux_client.get_history.return_value = "[developer] > "

        # Create provider
        provider = KiroCliProvider(
            terminal_id=test_terminal,
            session_name="test-session",
            window_name="test-window",
            agent_profile="developer",
        )

        # Get status - should return PROCESSING from hook, not IDLE from tmux
        status = provider.get_status()
        assert status == TerminalStatus.PROCESSING, "Hook status should take priority"

    def test_fallback_to_polling_when_no_hook_status(self, test_terminal, mock_tmux_client):
        """Test that polling works when no hook status is set."""
        # Don't set any hook status (status column is None)

        # Mock tmux output showing IDLE prompt
        mock_tmux_client.get_history.return_value = "[developer] > "

        # Create provider
        provider = KiroCliProvider(
            terminal_id=test_terminal,
            session_name="test-session",
            window_name="test-window",
            agent_profile="developer",
        )

        # Get status - should fall back to tmux polling
        status = provider.get_status()
        assert status == TerminalStatus.IDLE, "Should fall back to polling when no hook status"

    def test_fallback_to_polling_on_invalid_hook_status(self, test_terminal, mock_tmux_client):
        """Test that polling works when hook status is invalid."""
        # Set invalid hook status (shouldn't happen, but test resilience)
        from cli_agent_orchestrator.clients.database import SessionLocal, TerminalModel

        with SessionLocal() as db:
            terminal = db.query(TerminalModel).filter(TerminalModel.id == test_terminal).first()
            terminal.status = "invalid_status"
            db.commit()

        # Mock tmux output showing PROCESSING (no prompt)
        mock_tmux_client.get_history.return_value = "Thinking..."

        # Create provider
        provider = KiroCliProvider(
            terminal_id=test_terminal,
            session_name="test-session",
            window_name="test-window",
            agent_profile="developer",
        )

        # Get status - should fall back to tmux polling
        status = provider.get_status()
        assert status == TerminalStatus.PROCESSING, "Should fall back to polling on invalid status"

    def test_hook_status_update_and_retrieval(self, test_terminal):
        """Test that hook status can be updated and retrieved."""
        # Update status via hook
        success = update_terminal_status(test_terminal, "processing")
        assert success, "Status update should succeed"

        # Retrieve status
        status = get_terminal_status(test_terminal)
        assert status == "processing", "Should retrieve updated status"

        # Update to different status
        success = update_terminal_status(test_terminal, "idle")
        assert success, "Status update should succeed"

        # Retrieve updated status
        status = get_terminal_status(test_terminal)
        assert status == "idle", "Should retrieve newly updated status"

    def test_hook_status_for_nonexistent_terminal(self):
        """Test that status update fails for nonexistent terminal."""
        nonexistent_id = "deadbeef"

        # Try to update status
        success = update_terminal_status(nonexistent_id, "idle")
        assert not success, "Status update should fail for nonexistent terminal"

        # Try to retrieve status
        status = get_terminal_status(nonexistent_id)
        assert status is None, "Should return None for nonexistent terminal"

    def test_concurrent_status_updates(self, test_terminal):
        """Test multiple concurrent status updates."""
        import concurrent.futures

        def update_status(status_value):
            return update_terminal_status(test_terminal, status_value)

        # Simulate concurrent updates
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(update_status, "processing") for _ in range(50)]
            results = [f.result() for f in futures]

        # All updates should succeed
        assert all(results), "All concurrent updates should succeed"

        # Final status should be "processing"
        final_status = get_terminal_status(test_terminal)
        assert final_status == "processing"

    def test_status_update_updates_last_active(self, test_terminal):
        """Test that status update also updates last_active timestamp."""
        from cli_agent_orchestrator.clients.database import get_terminal_metadata
        from datetime import datetime
        import time

        # Get initial last_active
        metadata = get_terminal_metadata(test_terminal)
        initial_last_active = metadata["last_active"]

        # Wait a bit
        time.sleep(0.1)

        # Update status
        update_terminal_status(test_terminal, "processing")

        # Get updated last_active
        metadata = get_terminal_metadata(test_terminal)
        updated_last_active = metadata["last_active"]

        # Should be updated
        assert updated_last_active > initial_last_active, "last_active should be updated"

    def test_all_valid_status_values(self, test_terminal):
        """Test that all valid TerminalStatus values can be set via hooks."""
        valid_statuses = [
            TerminalStatus.IDLE,
            TerminalStatus.PROCESSING,
            TerminalStatus.COMPLETED,
            TerminalStatus.WAITING_USER_ANSWER,
            TerminalStatus.ERROR,
        ]

        for status_enum in valid_statuses:
            # Update status
            success = update_terminal_status(test_terminal, status_enum.value)
            assert success, f"Should be able to set status to {status_enum.value}"

            # Retrieve and verify
            retrieved = get_terminal_status(test_terminal)
            assert retrieved == status_enum.value, f"Should retrieve {status_enum.value}"
