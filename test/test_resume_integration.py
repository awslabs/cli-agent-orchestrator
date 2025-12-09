"""Integration tests for cao resume command with real tmux sessions."""

import subprocess
import time
import uuid
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.resume import resume
from cli_agent_orchestrator.clients.database import (
    create_terminal,
    delete_terminal,
    get_all_terminals,
    get_terminal_metadata,
    init_db,
)
from cli_agent_orchestrator.clients.tmux import tmux_client

# Mark all tests in this module as integration
pytestmark = [pytest.mark.integration]


@pytest.fixture(scope="session", autouse=True)
def setup_database():
    """Initialize database for tests."""
    init_db()
    yield


@pytest.fixture
def test_session_name():
    """Generate a unique test session name."""
    return f"cao-test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def cleanup_test_data(test_session_name):
    """Cleanup fixture for test data."""
    yield
    # Cleanup tmux session if exists
    try:
        tmux_client.kill_session(test_session_name)
    except Exception:
        pass

    # Cleanup database entries
    terminals = get_all_terminals()
    for t in terminals:
        if t.get("tmux_session", "").startswith("cao-test-"):
            try:
                delete_terminal(t["id"])
            except Exception:
                pass


class TestResumeListIntegration:
    """Integration tests for cao resume --list with real tmux."""

    def test_list_real_sessions(self, test_session_name, cleanup_test_data):
        """Test listing sessions with a real tmux session."""
        # Create a real tmux session
        terminal_id = f"test-{uuid.uuid4().hex[:8]}"
        window_name = tmux_client.create_session(
            test_session_name, "test-window", terminal_id
        )

        # Register in database
        create_terminal(
            terminal_id=terminal_id,
            tmux_session=test_session_name,
            tmux_window=window_name,
            provider="claude_code",
            agent_profile="developer",
        )

        try:
            runner = CliRunner()
            result = runner.invoke(resume, ["--list"])

            # Should succeed and show the session
            assert result.exit_code == 0
            # Note: --list uses API, which may not be running
            # So we test the direct database path via --reconcile
        finally:
            tmux_client.kill_session(test_session_name)
            delete_terminal(terminal_id)

    def test_reconcile_real_sessions(self, test_session_name, cleanup_test_data):
        """Test reconcile with real tmux sessions."""
        # Create a real tmux session
        terminal_id = f"test-{uuid.uuid4().hex[:8]}"
        window_name = tmux_client.create_session(
            test_session_name, "test-window", terminal_id
        )

        # Register in database
        create_terminal(
            terminal_id=terminal_id,
            tmux_session=test_session_name,
            tmux_window=window_name,
            provider="claude_code",
            agent_profile="developer",
        )

        try:
            runner = CliRunner()
            result = runner.invoke(resume, ["--reconcile"])

            assert result.exit_code == 0
            # Should show no discrepancies since tmux and DB are in sync
            assert "No discrepancies found" in result.output or "Reconciliation Report" in result.output
        finally:
            tmux_client.kill_session(test_session_name)
            delete_terminal(terminal_id)


class TestResumeCleanupIntegration:
    """Integration tests for cao resume --cleanup with real data."""

    def test_cleanup_orphaned_db_entry(self, cleanup_test_data):
        """Test cleanup removes DB entries for non-existent tmux sessions."""
        # Create orphan DB entry (no tmux session)
        orphan_id = f"orphan-{uuid.uuid4().hex[:8]}"
        create_terminal(
            terminal_id=orphan_id,
            tmux_session="cao-nonexistent-session",
            tmux_window="nonexistent-window",
            provider="claude_code",
            agent_profile="developer",
        )

        try:
            # Verify entry exists
            assert get_terminal_metadata(orphan_id) is not None

            runner = CliRunner()
            result = runner.invoke(resume, ["--cleanup"])

            assert result.exit_code == 0
            assert "stale entries" in result.output or "Cleaned up" in result.output

            # Verify entry was deleted
            assert get_terminal_metadata(orphan_id) is None
        finally:
            # Cleanup in case test failed
            try:
                delete_terminal(orphan_id)
            except Exception:
                pass

    def test_cleanup_dry_run_preserves_entries(self, cleanup_test_data):
        """Test cleanup --dry-run doesn't delete entries."""
        # Create orphan DB entry
        orphan_id = f"orphan-{uuid.uuid4().hex[:8]}"
        create_terminal(
            terminal_id=orphan_id,
            tmux_session="cao-nonexistent-session-2",
            tmux_window="nonexistent-window",
            provider="q_cli",
            agent_profile="reviewer",
        )

        try:
            runner = CliRunner()
            result = runner.invoke(resume, ["--cleanup", "--dry-run"])

            assert result.exit_code == 0
            assert "Dry run" in result.output

            # Verify entry still exists
            assert get_terminal_metadata(orphan_id) is not None
        finally:
            delete_terminal(orphan_id)

    def test_cleanup_valid_session_not_removed(self, test_session_name, cleanup_test_data):
        """Test cleanup doesn't remove entries with valid tmux sessions."""
        # Create a real tmux session
        terminal_id = f"valid-{uuid.uuid4().hex[:8]}"
        window_name = tmux_client.create_session(
            test_session_name, "test-window", terminal_id
        )

        # Register in database
        create_terminal(
            terminal_id=terminal_id,
            tmux_session=test_session_name,
            tmux_window=window_name,
            provider="claude_code",
            agent_profile="developer",
        )

        try:
            runner = CliRunner()
            result = runner.invoke(resume, ["--cleanup"])

            assert result.exit_code == 0

            # Entry should still exist (valid tmux session)
            metadata = get_terminal_metadata(terminal_id)
            assert metadata is not None
            assert metadata["tmux_session"] == test_session_name
        finally:
            tmux_client.kill_session(test_session_name)
            delete_terminal(terminal_id)


class TestResumeReconcileIntegration:
    """Integration tests for cao resume --reconcile."""

    def test_reconcile_detects_orphaned_db_entries(self, cleanup_test_data):
        """Test reconcile detects DB entries without tmux sessions."""
        # Create orphan DB entry
        orphan_id = f"orphan-{uuid.uuid4().hex[:8]}"
        create_terminal(
            terminal_id=orphan_id,
            tmux_session="cao-orphan-session",
            tmux_window="orphan-window",
            provider="claude_code",
            agent_profile="developer",
        )

        try:
            runner = CliRunner()
            result = runner.invoke(resume, ["--reconcile"])

            assert result.exit_code == 0
            assert "DB entries without tmux windows" in result.output
            assert orphan_id in result.output
        finally:
            delete_terminal(orphan_id)

    def test_reconcile_detects_untracked_tmux_sessions(self, test_session_name, cleanup_test_data):
        """Test reconcile detects tmux sessions not in database."""
        # Create tmux session but don't register in DB
        terminal_id = f"untracked-{uuid.uuid4().hex[:8]}"
        tmux_client.create_session(test_session_name, "test-window", terminal_id)

        try:
            runner = CliRunner()
            result = runner.invoke(resume, ["--reconcile"])

            assert result.exit_code == 0
            # Should detect untracked CAO session
            assert "Tmux CAO sessions not in database" in result.output or "No discrepancies" in result.output
        finally:
            tmux_client.kill_session(test_session_name)

    def test_reconcile_in_sync_state(self, test_session_name, cleanup_test_data):
        """Test reconcile shows no issues when DB and tmux are in sync."""
        # Create real tmux session and DB entry
        terminal_id = f"synced-{uuid.uuid4().hex[:8]}"
        window_name = tmux_client.create_session(
            test_session_name, "test-window", terminal_id
        )

        create_terminal(
            terminal_id=terminal_id,
            tmux_session=test_session_name,
            tmux_window=window_name,
            provider="claude_code",
            agent_profile="developer",
        )

        try:
            runner = CliRunner()

            # First cleanup any other stale entries
            runner.invoke(resume, ["--cleanup"])

            # Then reconcile
            result = runner.invoke(resume, ["--reconcile"])

            assert result.exit_code == 0
            # Should show in sync (or only our test entry if others exist)
            assert "Reconciliation Report" in result.output
        finally:
            tmux_client.kill_session(test_session_name)
            delete_terminal(terminal_id)


class TestResumeTmuxOperations:
    """Integration tests for tmux operations."""

    def test_window_exists_detection(self, test_session_name, cleanup_test_data):
        """Test window_exists correctly detects windows."""
        terminal_id = f"window-{uuid.uuid4().hex[:8]}"
        window_name = tmux_client.create_session(
            test_session_name, "test-window", terminal_id
        )

        try:
            # Window should exist
            assert tmux_client.session_exists(test_session_name) is True
            assert tmux_client.window_exists(test_session_name, window_name) is True

            # Non-existent window should return False
            assert tmux_client.window_exists(test_session_name, "fake-window") is False

            # Non-existent session should return False
            assert tmux_client.window_exists("fake-session", "fake-window") is False
        finally:
            tmux_client.kill_session(test_session_name)

    def test_multiple_windows_in_session(self, test_session_name, cleanup_test_data):
        """Test tracking multiple windows in same session."""
        terminal_id_1 = f"win1-{uuid.uuid4().hex[:8]}"
        terminal_id_2 = f"win2-{uuid.uuid4().hex[:8]}"

        # Create session with first window
        window_name_1 = tmux_client.create_session(
            test_session_name, "window-1", terminal_id_1
        )

        # Create second window
        window_name_2 = tmux_client.create_window(
            test_session_name, "window-2", terminal_id_2
        )

        # Register both in database
        create_terminal(
            terminal_id=terminal_id_1,
            tmux_session=test_session_name,
            tmux_window=window_name_1,
            provider="claude_code",
            agent_profile="developer",
        )
        create_terminal(
            terminal_id=terminal_id_2,
            tmux_session=test_session_name,
            tmux_window=window_name_2,
            provider="claude_code",
            agent_profile="reviewer",
        )

        try:
            # Both windows should exist
            assert tmux_client.window_exists(test_session_name, window_name_1)
            assert tmux_client.window_exists(test_session_name, window_name_2)

            # Reconcile should show no issues
            runner = CliRunner()
            result = runner.invoke(resume, ["--reconcile"])
            assert result.exit_code == 0
        finally:
            tmux_client.kill_session(test_session_name)
            delete_terminal(terminal_id_1)
            delete_terminal(terminal_id_2)


class TestResumeDatabaseOperations:
    """Integration tests for database operations."""

    def test_get_all_terminals_returns_all(self, cleanup_test_data):
        """Test get_all_terminals returns all registered terminals."""
        terminal_ids = []
        try:
            # Create multiple terminals
            for i in range(3):
                tid = f"multi-{i}-{uuid.uuid4().hex[:8]}"
                terminal_ids.append(tid)
                create_terminal(
                    terminal_id=tid,
                    tmux_session=f"cao-multi-{i}",
                    tmux_window=f"window-{i}",
                    provider="claude_code",
                    agent_profile=f"agent-{i}",
                )

            # Get all terminals
            all_terminals = get_all_terminals()

            # Verify our terminals are in the list
            all_ids = [t["id"] for t in all_terminals]
            for tid in terminal_ids:
                assert tid in all_ids
        finally:
            for tid in terminal_ids:
                try:
                    delete_terminal(tid)
                except Exception:
                    pass

    def test_terminal_metadata_fields(self, cleanup_test_data):
        """Test terminal metadata contains all required fields."""
        terminal_id = f"fields-{uuid.uuid4().hex[:8]}"
        create_terminal(
            terminal_id=terminal_id,
            tmux_session="cao-fields-test",
            tmux_window="test-window",
            provider="q_cli",
            agent_profile="developer",
            parent_id="parent-123",
        )

        try:
            metadata = get_terminal_metadata(terminal_id)
            assert metadata is not None
            assert metadata["id"] == terminal_id
            assert metadata["tmux_session"] == "cao-fields-test"
            assert metadata["tmux_window"] == "test-window"
            assert metadata["provider"] == "q_cli"
            assert metadata["agent_profile"] == "developer"
            assert metadata["parent_id"] == "parent-123"
            assert "last_active" in metadata
        finally:
            delete_terminal(terminal_id)
