"""Tests for the database client."""

import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    FlowModel,
    InboxModel,
    ManagedLaunchV2TerminalModel,
    TerminalModel,
    backfill_terminal_identity_if_missing,
    create_flow,
    create_inbox_message,
    create_terminal,
    delete_flow,
    delete_terminal,
    delete_terminals_by_session,
    get_flow,
    get_inbox_messages,
    get_pending_messages,
    get_terminal_metadata,
    init_db,
    list_flows,
    list_pending_receiver_ids_by_provider,
    list_pending_receiver_ids_older_than,
    list_terminals_by_session,
    update_flow_enabled,
    update_flow_run_times,
    update_last_active,
    update_message_status,
    update_terminal_shell_command,
)
from cli_agent_orchestrator.models.inbox import MessageStatus


@pytest.fixture
def test_db():
    """Create an in-memory test database."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)
    return TestSession


class TestTerminalOperations:
    """Tests for terminal database operations."""

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_create_terminal(self, mock_session_class):
        """Test creating a terminal record."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session_class.return_value = mock_session

        result = create_terminal("test123", "cao-session", "window-0", "kiro_cli", "developer")

        assert result["id"] == "test123"
        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_get_terminal_metadata_found(self, mock_session_class):
        """Test getting terminal metadata that exists."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_terminal = MagicMock()
        mock_terminal.id = "test123"
        mock_terminal.tmux_session = "cao-session"
        mock_terminal.tmux_window = "window-0"
        mock_terminal.provider = "kiro_cli"
        mock_terminal.agent_profile = "developer"
        mock_terminal.allowed_tools = None
        mock_terminal.last_active = datetime.now()

        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = mock_terminal
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = get_terminal_metadata("test123")

        assert result is not None
        assert result["id"] == "test123"

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_get_terminal_metadata_not_found(self, mock_session_class):
        """Test getting terminal metadata that doesn't exist."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = None
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = get_terminal_metadata("nonexistent")

        assert result is None

    def test_backfill_terminal_identity_is_atomic_write_once(self, test_db):
        with test_db() as db:
            db.add(
                TerminalModel(
                    id="test123",
                    tmux_session="cao-session",
                    tmux_window="window-0",
                    provider="codex",
                )
            )
            db.commit()

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            assert backfill_terminal_identity_if_missing("test123", "%9", "@7") is True
            assert backfill_terminal_identity_if_missing("test123", "%10", "@8") is False

        with test_db() as db:
            terminal = db.query(TerminalModel).filter_by(id="test123").one()
            assert terminal.pane_id == "%9"
            assert terminal.window_id == "@7"

    def test_backfill_refuses_partial_legacy_row(self, test_db):
        with test_db() as db:
            db.add(
                TerminalModel(
                    id="test123",
                    tmux_session="cao-session",
                    tmux_window="window-0",
                    provider="codex",
                    pane_id="%existing",
                    window_id=None,
                )
            )
            db.commit()

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            assert backfill_terminal_identity_if_missing("test123", "%9", "@7") is False

        with test_db() as db:
            terminal = db.query(TerminalModel).filter_by(id="test123").one()
            assert terminal.pane_id == "%existing"
            assert terminal.window_id is None

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_last_active(self, mock_session_class):
        """Test updating last active timestamp."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_terminal = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = mock_terminal
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        update_last_active("test123")

        mock_session.commit.assert_called_once()

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_terminal_shell_command(self, mock_session_class):
        """Test updating shell_command baseline for a terminal."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_terminal = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = mock_terminal
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = update_terminal_shell_command("test123", "bash")

        assert result is True
        assert mock_terminal.shell_command == "bash"
        mock_session.commit.assert_called_once()

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_terminal_shell_command_not_found(self, mock_session_class):
        """Test updating shell_command for a terminal that doesn't exist."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = None
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = update_terminal_shell_command("nonexistent", "bash")

        assert result is False

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_delete_terminal(self, mock_session_class):
        """Test deleting a terminal."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.delete.return_value = 1
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = delete_terminal("test123")

        assert result is True
        mock_session.commit.assert_called_once()

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_delete_terminal_not_found(self, mock_session_class):
        """Test deleting a terminal that doesn't exist."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.delete.return_value = 0
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = delete_terminal("nonexistent")

        assert result is False

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_list_terminals_by_session(self, mock_session_class):
        """Test listing terminals by session."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_terminal = MagicMock()
        mock_terminal.id = "test123"
        mock_terminal.tmux_session = "cao-session"
        mock_terminal.tmux_window = "window-0"
        mock_terminal.provider = "kiro_cli"
        mock_terminal.agent_profile = "developer"
        mock_terminal.last_active = datetime.now()

        legacy_query = MagicMock()
        legacy_query.filter.return_value.all.return_value = [mock_terminal]
        managed_query = MagicMock()
        managed_query.filter.return_value.all.return_value = []
        mock_session.query.side_effect = [legacy_query, managed_query]
        mock_session_class.return_value = mock_session

        result = list_terminals_by_session("cao-session")

        assert len(result) == 1
        assert result[0]["id"] == "test123"

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_list_pending_receiver_ids_by_provider(self, mock_session_class):
        """Test listing pending receivers for a specific provider."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.join.return_value.filter.return_value.distinct.return_value.all.return_value = [
            ("receiver-1",),
            ("receiver-2",),
        ]
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = list_pending_receiver_ids_by_provider("opencode_cli")

        assert result == ["receiver-1", "receiver-2"]

    def test_list_pending_receiver_ids_older_than(self, test_db):
        """Only messages pending past the grace window — whose receiver
        terminal still exists — are returned for reconciliation (issue #131).

        Uses the real in-memory DB (not a mocked session) so the age cutoff,
        status filter, and terminal join are actually exercised.
        """
        old = datetime.now() - timedelta(seconds=120)
        fresh = datetime.now()

        with test_db() as seed:
            seed.add_all(
                [
                    TerminalModel(
                        id="term-old",
                        tmux_session="cao-s",
                        tmux_window="w",
                        provider="kiro_cli",
                    ),
                    TerminalModel(
                        id="term-fresh",
                        tmux_session="cao-s",
                        tmux_window="w",
                        provider="kiro_cli",
                    ),
                    # Stuck long enough to reconcile, receiver still alive — kept.
                    InboxModel(
                        sender_id="a",
                        receiver_id="term-old",
                        message="m",
                        status=MessageStatus.PENDING.value,
                        created_at=old,
                    ),
                    # Too recent — left to the immediate/watchdog paths.
                    InboxModel(
                        sender_id="a",
                        receiver_id="term-fresh",
                        message="m",
                        status=MessageStatus.PENDING.value,
                        created_at=fresh,
                    ),
                    # Already delivered — not pending.
                    InboxModel(
                        sender_id="a",
                        receiver_id="term-old",
                        message="m",
                        status=MessageStatus.DELIVERED.value,
                        created_at=old,
                    ),
                    # Receiver terminal is gone — dropped by the join.
                    InboxModel(
                        sender_id="a",
                        receiver_id="term-ghost",
                        message="m",
                        status=MessageStatus.PENDING.value,
                        created_at=old,
                    ),
                ]
            )
            seed.commit()

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            result = list_pending_receiver_ids_older_than(30)

        assert result == ["term-old"]

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_delete_terminals_by_session(self, mock_session_class):
        """Test deleting all terminals in a session."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.delete.return_value = 2
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = delete_terminals_by_session("cao-session")

        assert result == 2


class TestInboxOperations:
    """Tests for inbox database operations."""

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_message_status(self, mock_session_class):
        """Test updating message status."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_message = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = mock_message
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        update_message_status(1, MessageStatus.DELIVERED)

        mock_session.commit.assert_called_once()


class TestFlowOperations:
    """Tests for flow database operations."""

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_get_flow_not_found(self, mock_session_class):
        """Test getting a flow that doesn't exist."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = None
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = get_flow("nonexistent")

        assert result is None

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_flow_enabled(self, mock_session_class):
        """Test updating flow enabled status."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_flow = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = mock_flow
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        update_flow_enabled("test-flow", False)

        mock_session.commit.assert_called_once()

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_flow_run_times(self, mock_session_class):
        """Test updating flow run times."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_flow = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = mock_flow
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = update_flow_run_times("test-flow", datetime.now(), datetime.now())

        assert result is True
        mock_session.commit.assert_called_once()

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_flow_run_times_not_found(self, mock_session_class):
        """Test updating flow run times when flow doesn't exist."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = None
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = update_flow_run_times("nonexistent", datetime.now(), datetime.now())

        assert result is False

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_flow_enabled_not_found(self, mock_session_class):
        """Test updating flow enabled when flow doesn't exist."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = None
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = update_flow_enabled("nonexistent", False)

        assert result is False

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_flow_enabled_with_next_run(self, mock_session_class):
        """Test updating flow enabled with next_run."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_flow = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = mock_flow
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        next_run = datetime.now()
        result = update_flow_enabled("test-flow", True, next_run=next_run)

        assert result is True
        assert mock_flow.next_run == next_run

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_create_flow(self, mock_session_class):
        """Test creating a flow."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session_class.return_value = mock_session

        # Setup mock to update flow attributes on refresh
        def mock_refresh(flow):
            flow.name = "test-flow"
            flow.file_path = "/path/to/file.yaml"
            flow.schedule = "0 * * * *"
            flow.agent_profile = "developer"
            flow.provider = "kiro_cli"
            flow.script = "echo test"
            flow.next_run = datetime.now()
            flow.last_run = None
            flow.enabled = True

        mock_session.refresh.side_effect = mock_refresh

        from cli_agent_orchestrator.clients.database import get_flows_to_run

        next_run = datetime.now()
        result = create_flow(
            name="test-flow",
            file_path="/path/to/file.yaml",
            schedule="0 * * * *",
            agent_profile="developer",
            provider="kiro_cli",
            script="echo test",
            next_run=next_run,
        )

        assert result.name == "test-flow"
        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_get_flow_found(self, mock_session_class):
        """Test getting a flow that exists."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_flow = MagicMock()
        mock_flow.name = "test-flow"
        mock_flow.file_path = "/path/to/file.yaml"
        mock_flow.schedule = "0 * * * *"
        mock_flow.agent_profile = "developer"
        mock_flow.provider = "kiro_cli"
        mock_flow.script = "echo test"
        mock_flow.last_run = None
        mock_flow.next_run = datetime.now()
        mock_flow.enabled = True

        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = mock_flow
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = get_flow("test-flow")

        assert result is not None
        assert result.name == "test-flow"

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_list_flows(self, mock_session_class):
        """Test listing all flows."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_flow = MagicMock()
        mock_flow.name = "test-flow"
        mock_flow.file_path = "/path/to/file.yaml"
        mock_flow.schedule = "0 * * * *"
        mock_flow.agent_profile = "developer"
        mock_flow.provider = "kiro_cli"
        mock_flow.script = "echo test"
        mock_flow.last_run = None
        mock_flow.next_run = datetime.now()
        mock_flow.enabled = True

        mock_query = MagicMock()
        mock_query.order_by.return_value.all.return_value = [mock_flow]
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = list_flows()

        assert len(result) == 1
        assert result[0].name == "test-flow"

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_delete_flow(self, mock_session_class):
        """Test deleting a flow."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.delete.return_value = 1
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = delete_flow("test-flow")

        assert result is True
        mock_session.commit.assert_called_once()

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_delete_flow_not_found(self, mock_session_class):
        """Test deleting a flow that doesn't exist."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.delete.return_value = 0
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = delete_flow("nonexistent")

        assert result is False

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_get_flows_to_run(self, mock_session_class):
        """Test getting flows that are due to run."""
        from cli_agent_orchestrator.clients.database import get_flows_to_run

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_flow = MagicMock()
        mock_flow.name = "due-flow"
        mock_flow.file_path = "/path/to/file.yaml"
        mock_flow.schedule = "0 * * * *"
        mock_flow.agent_profile = "developer"
        mock_flow.provider = "kiro_cli"
        mock_flow.script = "echo test"
        mock_flow.last_run = None
        mock_flow.next_run = datetime.now()
        mock_flow.enabled = True

        mock_query = MagicMock()
        mock_query.filter.return_value.all.return_value = [mock_flow]
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = get_flows_to_run()

        assert len(result) == 1
        assert result[0].name == "due-flow"

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_last_active_not_found(self, mock_session_class):
        """Test updating last active when terminal doesn't exist."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = None
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = update_last_active("nonexistent")

        assert result is False

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_message_status_not_found(self, mock_session_class):
        """Test updating message status when message doesn't exist."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = None
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = update_message_status(999, MessageStatus.DELIVERED)

        assert result is False

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_create_inbox_message(self, mock_session_class):
        """Test creating an inbox message when receiver terminal exists."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session_class.return_value = mock_session

        # Receiver terminal exists in the legacy table; the managed-v2
        # surface has no row for this id, so this is a plain v1 admission.
        v1_query = MagicMock()
        v1_query.filter.return_value.first.return_value = MagicMock()
        v2_query = MagicMock()
        v2_query.filter.return_value.first.return_value = None
        mock_session.query.side_effect = lambda model: (
            v1_query if model is TerminalModel else v2_query
        )

        # Setup mock to update message attributes on refresh
        def mock_refresh(msg):
            msg.id = 1
            msg.sender_id = "sender-123"
            msg.receiver_id = "receiver-456"
            msg.message = "Hello"
            msg.status = MessageStatus.PENDING.value
            msg.created_at = datetime.now()

        mock_session.refresh.side_effect = mock_refresh

        result = create_inbox_message("sender-123", "receiver-456", "Hello")

        assert result.sender_id == "sender-123"
        assert result.receiver_id == "receiver-456"
        assert result.message == "Hello"
        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_create_inbox_message_receiver_not_found(self, mock_session_class):
        """create_inbox_message raises ValueError when receiver terminal does not exist."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session_class.return_value = mock_session

        # Receiver terminal exists in neither vintage surface (the second
        # filter level is the live/non-superseded managed-v2 predicate).
        missing_query = MagicMock()
        missing_query.filter.return_value.first.return_value = None
        missing_query.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.side_effect = lambda model: missing_query

        with pytest.raises(ValueError, match="not found"):
            create_inbox_message("sender-123", "dead-terminal", "Hello")


class TestInitDb:
    """Tests for init_db function."""

    @patch("cli_agent_orchestrator.clients.database.Base")
    @patch("cli_agent_orchestrator.clients.database._migrate_project_aliases_schema")
    def test_init_db(self, mock_alias_migrate, mock_base):
        """Test database initialization."""
        init_db()

        mock_base.metadata.create_all.assert_called_once()


class TestCallbackRecoveryMigrationReadiness:
    """Inbox and operation migrations are one fail-closed capability boundary."""

    @pytest.fixture
    def callback_migration_db(self, tmp_path, monkeypatch):
        from cli_agent_orchestrator import constants
        from cli_agent_orchestrator.clients import database as db_mod

        db_file = tmp_path / "callback-recovery.sqlite"
        engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
        monkeypatch.setattr(db_mod, "engine", engine)
        monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(bind=engine))
        monkeypatch.setattr(db_mod, "_callback_recovery_migration_ready", False)
        monkeypatch.setattr(constants, "DATABASE_FILE", db_file)
        try:
            yield db_file
        finally:
            engine.dispose()

    @staticmethod
    def _legacy_inbox(db_file, *, duplicate_callback_keys=False):
        import sqlite3

        with sqlite3.connect(str(db_file)) as conn:
            conn.execute(
                "CREATE TABLE inbox ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id TEXT NOT NULL, "
                "receiver_id TEXT NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL, "
                "created_at TEXT, callback_recovery_key TEXT, callback_completion_key TEXT)"
            )
            if duplicate_callback_keys:
                conn.executemany(
                    "INSERT INTO inbox "
                    "(sender_id, receiver_id, message, status, callback_recovery_key) "
                    "VALUES ('supervisor', 'worker', 'legacy', 'pending', 'duplicate-key')",
                    [(), ()],
                )

    @staticmethod
    def _inbox_indexes(db_file):
        import sqlite3

        with sqlite3.connect(str(db_file)) as conn:
            return {row[1] for row in conn.execute("PRAGMA index_list(inbox)")}

    def test_duplicate_legacy_callback_keys_keep_lifecycle_and_admission_disabled(
        self, callback_migration_db, monkeypatch
    ):
        from cli_agent_orchestrator.clients import database as db_mod
        from cli_agent_orchestrator.services import callback_recovery

        self._legacy_inbox(callback_migration_db, duplicate_callback_keys=True)

        inbox_ready = db_mod._migrate_callback_recovery_inbox_schema()
        db_mod._migrate_callback_recovery_schema(inbox_schema_ready=inbox_ready)

        monkeypatch.setenv("CAO_CALLBACK_RECOVERY_LIFECYCLE_V2_ENABLED", "true")
        assert inbox_ready is False
        assert self._inbox_indexes(callback_migration_db) == set()
        assert db_mod.callback_recovery_migration_ready() is False
        assert callback_recovery.lifecycle_v2_enabled() is False

    def test_generic_inbox_ddl_failure_keeps_lifecycle_disabled(
        self, callback_migration_db, monkeypatch
    ):
        import sqlite3

        from cli_agent_orchestrator.clients import database as db_mod
        from cli_agent_orchestrator.services import callback_recovery

        with sqlite3.connect(str(callback_migration_db)) as conn:
            conn.execute("CREATE VIEW inbox AS SELECT 'legacy' AS callback_recovery_key")

        inbox_ready = db_mod._migrate_callback_recovery_inbox_schema()
        db_mod._migrate_callback_recovery_schema(inbox_schema_ready=inbox_ready)

        monkeypatch.setenv("CAO_CALLBACK_RECOVERY_LIFECYCLE_V2_ENABLED", "true")
        assert inbox_ready is False
        assert db_mod.callback_recovery_migration_ready() is False
        assert callback_recovery.lifecycle_v2_enabled() is False

    def test_normal_upgrade_verifies_both_inbox_fences_idempotently(self, callback_migration_db):
        from cli_agent_orchestrator.clients import database as db_mod

        self._legacy_inbox(callback_migration_db)

        for _ in range(2):
            inbox_ready = db_mod._migrate_callback_recovery_inbox_schema()
            db_mod._migrate_callback_recovery_schema(inbox_schema_ready=inbox_ready)
            assert inbox_ready is True
            assert db_mod.callback_recovery_migration_ready() is True

        assert self._inbox_indexes(callback_migration_db) == {
            "ix_inbox_callback_completion_key",
            "ix_inbox_callback_recovery_key",
        }


class TestInitDbOldBinaryGate:
    """The configured exact-old-binary gate precedes any v2 surface creation.

    Production regression (P1): ``init_db`` must never create the v2 ORM
    surface through the unconditional ``create_all`` — v2 tables come only
    from the gated transactional migration — and a REQUIRED gate refusal
    must propagate out of ``init_db`` instead of being logged and
    swallowed.
    """

    @pytest.fixture
    def isolated_init_db(self, tmp_path, monkeypatch):
        """Bind the module engine/session/DB file to a per-test SQLite file."""
        import sqlite3

        from cli_agent_orchestrator import constants
        from cli_agent_orchestrator.clients import database as db_mod

        db_file = tmp_path / "init.sqlite"
        engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
        monkeypatch.setattr(db_mod, "engine", engine)
        monkeypatch.setattr(
            db_mod,
            "SessionLocal",
            sessionmaker(autocommit=False, autoflush=False, bind=engine),
        )
        monkeypatch.setattr(db_mod, "DB_DIR", tmp_path)
        monkeypatch.setattr(constants, "DATABASE_FILE", db_file)

        def v2_tables():
            with sqlite3.connect(str(db_file)) as conn:
                return sorted(
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name LIKE 'managed_launch_v2%'"
                    )
                )

        return v2_tables

    def test_required_gate_refusal_propagates_before_v2_surface(
        self, isolated_init_db, monkeypatch
    ):
        from cli_agent_orchestrator.services import old_binary_rig, vintage_migration

        monkeypatch.setenv("CAO_OLD_BINARY_GATE", "require")
        hostile = old_binary_rig.RigVerdict(
            zero_visibility=False, violations=("v2 access",), surfaces_checked=7
        )
        monkeypatch.setattr(
            old_binary_rig, "prove_old_binary_invisibility", lambda **_kwargs: hostile
        )

        with pytest.raises(vintage_migration.OldBinaryGateRefused):
            init_db()

        assert isolated_init_db() == [], (
            "a required gate refusal must abort initialization BEFORE any "
            "v2-capable metadata operation creates the v2 surface"
        )

    def test_required_gate_pass_creates_v2_only_through_migration(
        self, isolated_init_db, monkeypatch, tmp_path
    ):
        import sqlite3

        from cli_agent_orchestrator.services import old_binary_rig

        monkeypatch.setenv("CAO_OLD_BINARY_GATE", "require")
        passing = old_binary_rig.RigVerdict(zero_visibility=True, violations=(), surfaces_checked=7)
        monkeypatch.setattr(
            old_binary_rig, "prove_old_binary_invisibility", lambda **_kwargs: passing
        )

        init_db()

        assert isolated_init_db() == [
            "managed_launch_v2_reservations",
            "managed_launch_v2_terminals",
        ]
        # The v2 surface came through the gated transactional migration,
        # journaled with the gate outcome — never the bare create_all.
        from cli_agent_orchestrator import constants

        with sqlite3.connect(str(constants.DATABASE_FILE)) as conn:
            detail = conn.execute(
                "SELECT detail FROM v2_migration_journal ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0]
        assert '"zero_visibility": true' in detail

    def test_ungated_init_creates_v2_through_migration(self, isolated_init_db, monkeypatch):
        monkeypatch.delenv("CAO_OLD_BINARY_GATE", raising=False)

        init_db()

        assert isolated_init_db() == [
            "managed_launch_v2_reservations",
            "managed_launch_v2_terminals",
        ]

    def test_create_all_excludes_v2_orm_tables(self):
        """The unconditional create_all metadata list names no v2 table."""
        from cli_agent_orchestrator.clients import database as db_mod

        names = {
            table.name
            for table in Base.metadata.sorted_tables
            if table.name not in db_mod._V2_ORM_TABLE_NAMES
        }
        assert "managed_launch_v2_reservations" not in names
        assert "managed_launch_v2_terminals" not in names
        assert db_mod._V2_ORM_TABLE_NAMES == {
            "managed_launch_v2_reservations",
            "managed_launch_v2_terminals",
        }


class TestTerminalsSchemaMigration:
    """Tests for the terminals-table column-add migration (caller_id, issue #284)."""

    def test_caller_id_column_added_to_legacy_table(self, tmp_path, monkeypatch):
        """A pre-#284 terminals table gains the caller_id column."""
        import sqlite3

        from cli_agent_orchestrator.clients import database as db_mod

        db_file = tmp_path / "legacy.db"
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute(
                "CREATE TABLE terminals ("
                "id TEXT PRIMARY KEY, tmux_session TEXT NOT NULL, "
                "tmux_window TEXT NOT NULL, provider TEXT NOT NULL, "
                "agent_profile TEXT, allowed_tools TEXT, shell_command TEXT, "
                "last_active TIMESTAMP)"
            )
            conn.execute(
                "INSERT INTO terminals (id, tmux_session, tmux_window, provider) "
                "VALUES ('abc12345', 'cao-s', 'w-0', 'kiro_cli')"
            )
            conn.commit()

        # _migrate_terminals_schema reads DATABASE_FILE from constants at call time
        monkeypatch.setattr(
            "cli_agent_orchestrator.constants.DATABASE_FILE", db_file, raising=False
        )

        db_mod._migrate_terminals_schema()

        with sqlite3.connect(str(db_file)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(terminals)")}
            rows = conn.execute("SELECT id, caller_id FROM terminals").fetchall()
        assert "caller_id" in columns
        assert rows == [("abc12345", None)], "existing rows must get NULL caller_id"

    def test_migration_is_idempotent(self, tmp_path, monkeypatch):
        """Running the migration twice must not fail or duplicate columns."""
        import sqlite3

        from cli_agent_orchestrator.clients import database as db_mod

        db_file = tmp_path / "current.db"
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute(
                "CREATE TABLE terminals ("
                "id TEXT PRIMARY KEY, tmux_session TEXT NOT NULL, "
                "tmux_window TEXT NOT NULL, provider TEXT NOT NULL)"
            )
            conn.commit()

        # _migrate_terminals_schema reads DATABASE_FILE from constants at call time
        monkeypatch.setattr(
            "cli_agent_orchestrator.constants.DATABASE_FILE", db_file, raising=False
        )

        db_mod._migrate_terminals_schema()
        db_mod._migrate_terminals_schema()

        with sqlite3.connect(str(db_file)) as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(terminals)")]
        assert columns.count("caller_id") == 1
        assert columns.count("allowed_tools") == 1


class TestCallerIdRoundTrip:
    """caller_id must round-trip create→read (issue #284): a write path that
    persists it and a read path that drops it would silently break callback
    routing for every worker."""

    def test_caller_id_round_trips_through_real_db(self, tmp_path, monkeypatch):
        """create_terminal persists caller_id; get_terminal_metadata returns it."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from cli_agent_orchestrator.clients import database as db_mod

        engine = create_engine(f"sqlite:///{tmp_path / 'rt.db'}")
        Base.metadata.create_all(bind=engine)
        monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(bind=engine))

        created = create_terminal(
            "abc12345", "cao-s", "w-0", "kiro_cli", "developer", caller_id="def67890"
        )
        assert created["caller_id"] == "def67890"

        fetched = get_terminal_metadata("abc12345")
        assert fetched is not None
        assert fetched["caller_id"] == "def67890"

    def test_caller_id_defaults_to_none(self, tmp_path, monkeypatch):
        """Operator-launched terminals (no caller) round-trip NULL."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from cli_agent_orchestrator.clients import database as db_mod

        engine = create_engine(f"sqlite:///{tmp_path / 'rt2.db'}")
        Base.metadata.create_all(bind=engine)
        monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(bind=engine))

        created = create_terminal("abc12345", "cao-s", "w-0", "kiro_cli")
        assert created["caller_id"] is None

        fetched = get_terminal_metadata("abc12345")
        assert fetched is not None
        assert fetched["caller_id"] is None


class TestProjectAliasMigration:
    """Tests for the project_aliases alias-only primary-key migration."""

    def test_legacy_composite_pk_table_is_rebuilt(self, tmp_path, monkeypatch):
        """A legacy table with composite PK (project_id, alias) is dropped."""
        import sqlite3

        from cli_agent_orchestrator.clients import database as db_mod

        db_file = tmp_path / "legacy.db"
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute(
                "CREATE TABLE project_aliases ("
                "project_id TEXT NOT NULL, alias TEXT NOT NULL, kind TEXT NOT NULL, "
                "created_at TEXT, PRIMARY KEY (project_id, alias))"
            )
            conn.execute("INSERT INTO project_aliases VALUES ('p1', 'a1', 'cwd_hash', NULL)")
            conn.commit()

        monkeypatch.setattr(db_mod, "DATABASE_FILE", db_file, raising=False)
        monkeypatch.setattr(
            "cli_agent_orchestrator.constants.DATABASE_FILE", db_file, raising=False
        )

        db_mod._migrate_project_aliases_schema()

        with sqlite3.connect(str(db_file)) as conn:
            exists = conn.execute(
                "SELECT name FROM sqlite_master " "WHERE type='table' AND name='project_aliases'"
            ).fetchone()
        assert exists is None, "legacy table should be dropped for create_all to rebuild"

    def test_alias_only_pk_table_is_left_intact(self, tmp_path, monkeypatch):
        """A table already keyed on alias alone is not touched."""
        import sqlite3

        from cli_agent_orchestrator.clients import database as db_mod

        db_file = tmp_path / "current.db"
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute(
                "CREATE TABLE project_aliases ("
                "alias TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL, "
                "created_at TEXT)"
            )
            conn.execute("INSERT INTO project_aliases VALUES ('a1', 'p1', 'cwd_hash', NULL)")
            conn.commit()

        monkeypatch.setattr(db_mod, "DATABASE_FILE", db_file, raising=False)
        monkeypatch.setattr(
            "cli_agent_orchestrator.constants.DATABASE_FILE", db_file, raising=False
        )

        db_mod._migrate_project_aliases_schema()

        with sqlite3.connect(str(db_file)) as conn:
            rows = conn.execute("SELECT alias, project_id FROM project_aliases").fetchall()
        assert rows == [("a1", "p1")], "current-schema table must be left intact"


class TestSessionEnvMigration:
    """Tests for the session_env table migration (issue #248 durability)."""

    def _legacy_db(self, tmp_path):
        """A DB created before the session_env table existed (other migrated
        tables present, no session_env)."""
        import sqlite3

        db_file = tmp_path / "legacy.db"
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute("CREATE TABLE terminals (id TEXT PRIMARY KEY, tmux_session TEXT NOT NULL)")
            conn.commit()
        return db_file

    def test_migration_creates_table_with_exact_schema(self, tmp_path, monkeypatch):
        import sqlite3

        from cli_agent_orchestrator.clients import database as db_mod

        db_file = self._legacy_db(tmp_path)
        monkeypatch.setattr(
            "cli_agent_orchestrator.constants.DATABASE_FILE", db_file, raising=False
        )

        db_mod._migrate_session_env()

        with sqlite3.connect(str(db_file)) as conn:
            cols = conn.execute("PRAGMA table_info(session_env)").fetchall()
        # PRAGMA row: (cid, name, type, notnull, dflt_value, pk).
        schema = {c[1]: (c[2].upper(), c[3], c[5]) for c in cols}
        assert schema == {
            "session_name": ("TEXT", 0, 1),
            "env_vars": ("TEXT", 1, 0),
            "updated_at": ("TEXT", 1, 0),
        }

    def test_migration_is_idempotent_and_preserves_rows(self, tmp_path, monkeypatch):
        import sqlite3

        from cli_agent_orchestrator.clients import database as db_mod

        db_file = self._legacy_db(tmp_path)
        monkeypatch.setattr(
            "cli_agent_orchestrator.constants.DATABASE_FILE", db_file, raising=False
        )

        db_mod._migrate_session_env()
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute(
                "INSERT INTO session_env VALUES ('cao-x', '{\"A\": \"1\"}', '2026-07-21T00:00:00Z')"
            )
            conn.commit()
        db_mod._migrate_session_env()  # second run — must not raise or clobber

        with sqlite3.connect(str(db_file)) as conn:
            rows = conn.execute("SELECT session_name, env_vars FROM session_env").fetchall()
        assert rows == [("cao-x", '{"A": "1"}')]

    def test_fresh_model_schema_matches_raw_migration_ddl(self):
        """create_all (fresh DBs) and the raw migration (old DBs) must produce
        the same columns/types so reads behave identically on both paths."""
        from sqlalchemy.schema import CreateTable

        from cli_agent_orchestrator.clients.database import SessionEnvModel

        ddl = str(CreateTable(SessionEnvModel.__table__).compile())
        assert "session_name TEXT" in ddl
        assert "env_vars TEXT NOT NULL" in ddl
        assert "updated_at TEXT NOT NULL" in ddl
        assert "PRIMARY KEY (session_name)" in ddl

    def test_downgrade_harmless_new_db_with_old_migrations(self, tmp_path, monkeypatch):
        """Old code on a new DB: the extra session_env table is ignored by the
        pre-existing migrations and its rows are left untouched."""
        import sqlite3

        from cli_agent_orchestrator.clients import database as db_mod

        db_file = self._legacy_db(tmp_path)
        monkeypatch.setattr(
            "cli_agent_orchestrator.constants.DATABASE_FILE", db_file, raising=False
        )
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute(
                "CREATE TABLE session_env ("
                "session_name TEXT PRIMARY KEY, env_vars TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO session_env VALUES ('cao-x', '{\"A\": \"1\"}', '2026-07-21T00:00:00Z')"
            )
            conn.commit()

        # The pre-existing raw migrations (what "old code" runs) ignore the table.
        db_mod._migrate_terminals_schema()
        db_mod._migrate_memory_indexes()
        db_mod._migrate_project_aliases_schema()

        with sqlite3.connect(str(db_file)) as conn:
            rows = conn.execute("SELECT session_name, env_vars FROM session_env").fetchall()
        assert rows == [("cao-x", '{"A": "1"}')]


def _seed_v2_terminal(
    seed,
    terminal_id,
    *,
    lifecycle_state="live",
    superseded_by_terminal_id=None,
    superseded_by_generation=None,
    provider="kimi_cli",
    generation="gen-v2-1",
):
    """Seed one managed-v2 terminal row in the exact lifecycle state under test."""
    seed.add(
        ManagedLaunchV2TerminalModel(
            id=terminal_id,
            tmux_session="cao-v2",
            tmux_window="worker",
            provider=provider,
            generation=generation,
            protocol_vintage="v2",
            v2_lifecycle_state=lifecycle_state,
            v2_superseded_by_terminal_id=superseded_by_terminal_id,
            v2_superseded_by_generation=superseded_by_generation,
        )
    )


class TestCrossVintageInboxEligibility:
    """create_inbox_message admits exactly one current, live,
    non-superseded managed-v2 identity and refuses everything else, while
    legacy-v1 behavior stays byte-for-byte unchanged."""

    def test_live_managed_v2_receiver_accepted_creates_pending_row(self, test_db):
        with test_db() as seed:
            _seed_v2_terminal(seed, "v2live01")
            seed.commit()

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            result = create_inbox_message("supervisor-1", "v2live01", "ordinary follow-up")

            assert result.receiver_id == "v2live01"
            assert result.status == MessageStatus.PENDING
            # The v2 identity is NEVER copied into legacy ownership.
            with test_db() as db:
                assert db.query(TerminalModel).count() == 0

    def test_legacy_v1_receiver_accepted_unchanged(self, test_db):
        with test_db() as seed:
            seed.add(
                TerminalModel(
                    id="term-v1", tmux_session="cao-s", tmux_window="w", provider="kiro_cli"
                )
            )
            seed.commit()

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            result = create_inbox_message("sender-1", "term-v1", "hello")
            assert result.status == MessageStatus.PENDING

    def test_unknown_receiver_refused_zero_rows(self, test_db):
        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            with pytest.raises(ValueError, match="not found"):
                create_inbox_message("sender-1", "no-such-terminal", "hello")
            with test_db() as db:
                assert db.query(InboxModel).count() == 0

    def test_superseded_by_terminal_pointer_refused(self, test_db):
        with test_db() as seed:
            _seed_v2_terminal(seed, "v2sup01", superseded_by_terminal_id="v2new01")
            seed.commit()

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            with pytest.raises(ValueError, match="not found"):
                create_inbox_message("sender-1", "v2sup01", "hello")
            with test_db() as db:
                assert db.query(InboxModel).count() == 0

    def test_superseded_by_generation_pointer_refused(self, test_db):
        with test_db() as seed:
            _seed_v2_terminal(seed, "v2sup02", superseded_by_generation="gen-v2-2")
            seed.commit()

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            with pytest.raises(ValueError, match="not found"):
                create_inbox_message("sender-1", "v2sup02", "hello")
            with test_db() as db:
                assert db.query(InboxModel).count() == 0

    def test_null_lifecycle_state_refused(self, test_db):
        """A registered-but-never-activated v2 row (state NULL) is not live."""
        with test_db() as seed:
            _seed_v2_terminal(seed, "v2null1", lifecycle_state=None)
            seed.commit()

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            with pytest.raises(ValueError, match="not found"):
                create_inbox_message("sender-1", "v2null1", "hello")
            with test_db() as db:
                assert db.query(InboxModel).count() == 0

    def test_non_live_lifecycle_state_refused(self, test_db):
        with test_db() as seed:
            _seed_v2_terminal(seed, "v2stale", lifecycle_state="stale")
            seed.commit()

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            with pytest.raises(ValueError, match="not found"):
                create_inbox_message("sender-1", "v2stale", "hello")
            with test_db() as db:
                assert db.query(InboxModel).count() == 0

    def test_collected_receiver_refused(self, test_db):
        """Collection deletes the v2 row; a previously live receiver goes absent."""
        with test_db() as seed:
            _seed_v2_terminal(seed, "v2gone1")
            seed.commit()

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            first = create_inbox_message("sender-1", "v2gone1", "before collect")
            assert first.status == MessageStatus.PENDING
            with test_db() as db:
                db.query(ManagedLaunchV2TerminalModel).filter(
                    ManagedLaunchV2TerminalModel.id == "v2gone1"
                ).delete()
                db.commit()
            with pytest.raises(ValueError, match="not found"):
                create_inbox_message("sender-1", "v2gone1", "after collect")

    def test_cross_vintage_ambiguous_refused(self, test_db):
        """An id present in BOTH vintage tables refuses as ambiguous (mirrors
        managed_control_identity's ManagedLaunchConflict), with zero rows."""
        with test_db() as seed:
            seed.add(
                TerminalModel(id="dualid1", tmux_session="cao-s", tmux_window="w", provider="codex")
            )
            _seed_v2_terminal(seed, "dualid1", provider="codex")
            seed.commit()

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            with pytest.raises(ValueError, match="ambiguous"):
                create_inbox_message("sender-1", "dualid1", "hello")
            with test_db() as db:
                assert db.query(InboxModel).count() == 0

    def test_ambiguous_even_when_v2_row_not_live(self, test_db):
        """Presence in both vintages is ambiguous regardless of v2 liveness."""
        with test_db() as seed:
            seed.add(
                TerminalModel(id="dualid2", tmux_session="cao-s", tmux_window="w", provider="codex")
            )
            _seed_v2_terminal(
                seed, "dualid2", provider="codex", superseded_by_terminal_id="dualid3"
            )
            seed.commit()

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            with pytest.raises(ValueError, match="ambiguous"):
                create_inbox_message("sender-1", "dualid2", "hello")
            with test_db() as db:
                assert db.query(InboxModel).count() == 0


class TestListPendingReceiverIdsOlderThanCrossVintage:
    """The reconciliation sweep enumerates stale PENDING rows for
    live managed-v2 receivers (server bounce cannot strand them) while dead
    receivers of either vintage stay dropped."""

    def _seed_pending(self, seed, receiver_id, *, age_seconds=120):
        seed.add(
            InboxModel(
                sender_id="a",
                receiver_id=receiver_id,
                message="m",
                status=MessageStatus.PENDING.value,
                created_at=datetime.now() - timedelta(seconds=age_seconds),
            )
        )

    def test_live_v2_receiver_enumerated(self, test_db):
        with test_db() as seed:
            _seed_v2_terminal(seed, "v2rcv01")
            self._seed_pending(seed, "v2rcv01")
            seed.commit()

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            assert list_pending_receiver_ids_older_than(30) == ["v2rcv01"]

    def test_superseded_v2_receiver_excluded(self, test_db):
        with test_db() as seed:
            _seed_v2_terminal(seed, "v2rcv02", superseded_by_terminal_id="v2rcv99")
            self._seed_pending(seed, "v2rcv02")
            seed.commit()

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            assert list_pending_receiver_ids_older_than(30) == []

    def test_non_live_v2_receiver_excluded(self, test_db):
        with test_db() as seed:
            _seed_v2_terminal(seed, "v2rcv03", lifecycle_state="stale")
            self._seed_pending(seed, "v2rcv03")
            seed.commit()

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            assert list_pending_receiver_ids_older_than(30) == []

    def test_distinct_union_of_v1_and_v2_branches(self, test_db):
        with test_db() as seed:
            seed.add(
                TerminalModel(
                    id="term-old", tmux_session="cao-s", tmux_window="w", provider="kiro_cli"
                )
            )
            self._seed_pending(seed, "term-old")
            self._seed_pending(seed, "term-old")
            _seed_v2_terminal(seed, "v2rcv04")
            self._seed_pending(seed, "v2rcv04")
            seed.commit()

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            result = list_pending_receiver_ids_older_than(30)

        assert sorted(result) == ["term-old", "v2rcv04"]
        assert len(result) == len(set(result))

    def test_pre_v2_schema_v1_behavior_bit_identical(self):
        """A schema with no v2 tables at all: the v2 surface reads as absent
        (OperationalError guard) at BOTH query sites; v1 behavior is unchanged."""
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(
            bind=engine, tables=[TerminalModel.__table__, InboxModel.__table__]
        )
        pre_v2_db = sessionmaker(bind=engine)
        old = datetime.now() - timedelta(seconds=120)
        with pre_v2_db() as seed:
            seed.add(
                TerminalModel(
                    id="term-v1", tmux_session="cao-s", tmux_window="w", provider="kiro_cli"
                )
            )
            seed.add(
                InboxModel(
                    sender_id="a",
                    receiver_id="term-v1",
                    message="m",
                    status=MessageStatus.PENDING.value,
                    created_at=old,
                )
            )
            seed.commit()

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", pre_v2_db):
            assert list_pending_receiver_ids_older_than(30) == ["term-v1"]
            result = create_inbox_message("sender-1", "term-v1", "hello")
            assert result.status == MessageStatus.PENDING
            with pytest.raises(ValueError, match="not found"):
                create_inbox_message("sender-1", "no-such-terminal", "hello")
