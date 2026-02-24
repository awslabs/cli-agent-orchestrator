"""Tests for the database client."""

import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    FlowModel,
    InboxModel,
    TerminalModel,
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
    list_terminals_by_session,
    update_flow_enabled,
    update_flow_run_times,
    update_last_active,
    update_message_status,
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

        mock_query = MagicMock()
        mock_query.filter.return_value.all.return_value = [mock_terminal]
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = list_terminals_by_session("cao-session")

        assert len(result) == 1
        assert result[0]["id"] == "test123"

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
        """Test creating an inbox message."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session_class.return_value = mock_session

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


class TestInitDb:
    """Tests for init_db function."""

    @patch("cli_agent_orchestrator.clients.database.Base")
    def test_init_db(self, mock_base):
        """Test database initialization."""
        init_db()

        mock_base.metadata.create_all.assert_called_once()
