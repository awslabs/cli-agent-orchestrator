"""Tests for database client."""

import pytest
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    create_terminal,
    get_terminal_metadata,
    list_terminals_by_session,
    update_last_active,
    delete_terminal,
    delete_terminals_by_session,
    create_inbox_message,
    get_pending_messages,
    get_inbox_messages,
    update_message_status,
    create_flow,
    get_flow,
    list_flows,
    update_flow_run_times,
    update_flow_enabled,
    delete_flow,
    get_flows_to_run,
)
from cli_agent_orchestrator.models.inbox import MessageStatus


class DatabaseClient:
    """Test database client using in-memory SQLite."""
    
    def __init__(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        
    def initialize(self):
        Base.metadata.create_all(bind=self.engine)
        
    def close(self):
        self.engine.dispose()


@pytest.fixture
def test_db():
    """Create isolated test database."""
    # Patch the module-level SessionLocal
    import cli_agent_orchestrator.clients.database as db_module
    
    client = DatabaseClient()
    client.initialize()
    
    # Replace module SessionLocal with test one
    original_session = db_module.SessionLocal
    db_module.SessionLocal = client.SessionLocal
    
    yield client
    
    # Restore original
    db_module.SessionLocal = original_session
    client.close()


class TestTerminalOperations:
    """Test terminal database operations."""
    
    def test_create_terminal(self, test_db):
        result = create_terminal("term1", "session1", "window1", "q_cli", "developer")
        
        assert result["id"] == "term1"
        assert result["tmux_session"] == "session1"
        assert result["tmux_window"] == "window1"
        assert result["provider"] == "q_cli"
        assert result["agent_profile"] == "developer"
    
    def test_get_terminal_metadata_success(self, test_db):
        create_terminal("term1", "session1", "window1", "q_cli", "developer")
        
        result = get_terminal_metadata("term1")
        
        assert result is not None
        assert result["id"] == "term1"
        assert result["tmux_session"] == "session1"
        assert result["provider"] == "q_cli"
        assert "last_active" in result
    
    def test_get_terminal_metadata_not_found(self, test_db):
        result = get_terminal_metadata("nonexistent")
        assert result is None
    
    def test_list_terminals_by_session(self, test_db):
        create_terminal("term1", "session1", "window1", "q_cli", "developer")
        create_terminal("term2", "session1", "window2", "kiro_cli", "reviewer")
        create_terminal("term3", "session2", "window3", "q_cli", "developer")
        
        result = list_terminals_by_session("session1")
        
        assert len(result) == 2
        assert {t["id"] for t in result} == {"term1", "term2"}
    
    def test_update_last_active(self, test_db):
        create_terminal("term1", "session1", "window1", "q_cli")
        
        result = update_last_active("term1")
        assert result is True
        
        result = update_last_active("nonexistent")
        assert result is False
    
    def test_delete_terminal(self, test_db):
        create_terminal("term1", "session1", "window1", "q_cli")
        
        result = delete_terminal("term1")
        assert result is True
        
        result = delete_terminal("term1")
        assert result is False
    
    def test_delete_terminals_by_session(self, test_db):
        create_terminal("term1", "session1", "window1", "q_cli")
        create_terminal("term2", "session1", "window2", "q_cli")
        create_terminal("term3", "session2", "window3", "q_cli")
        
        result = delete_terminals_by_session("session1")
        assert result == 2
        
        remaining = list_terminals_by_session("session1")
        assert len(remaining) == 0


class TestInboxOperations:
    """Test inbox database operations."""
    
    def test_create_inbox_message(self, test_db):
        result = create_inbox_message("sender1", "receiver1", "test message")
        
        assert result.sender_id == "sender1"
        assert result.receiver_id == "receiver1"
        assert result.message == "test message"
        assert result.status == MessageStatus.PENDING
        assert result.id is not None
    
    def test_get_pending_messages(self, test_db):
        create_inbox_message("sender1", "receiver1", "message1")
        create_inbox_message("sender2", "receiver1", "message2")
        
        result = get_pending_messages("receiver1", limit=10)
        
        assert len(result) == 2
        assert all(msg.status == MessageStatus.PENDING for msg in result)
    
    def test_get_inbox_messages_with_status(self, test_db):
        msg1 = create_inbox_message("sender1", "receiver1", "message1")
        create_inbox_message("sender2", "receiver1", "message2")
        
        update_message_status(msg1.id, MessageStatus.DELIVERED)
        
        pending = get_inbox_messages("receiver1", status=MessageStatus.PENDING)
        delivered = get_inbox_messages("receiver1", status=MessageStatus.DELIVERED)
        
        assert len(pending) == 1
        assert len(delivered) == 1
    
    def test_get_inbox_messages_with_limit(self, test_db):
        for i in range(5):
            create_inbox_message("sender1", "receiver1", f"message{i}")
        
        result = get_inbox_messages("receiver1", limit=3)
        assert len(result) == 3
    
    def test_update_message_status(self, test_db):
        msg = create_inbox_message("sender1", "receiver1", "test")
        
        result = update_message_status(msg.id, MessageStatus.DELIVERED)
        assert result is True
        
        result = update_message_status(99999, MessageStatus.DELIVERED)
        assert result is False


class TestFlowOperations:
    """Test flow database operations."""
    
    def test_create_flow(self, test_db):
        next_run = datetime.now() + timedelta(hours=1)
        
        result = create_flow(
            "test_flow",
            "/path/to/flow.md",
            "0 * * * *",
            "developer",
            "q_cli",
            "script.py",
            next_run
        )
        
        assert result.name == "test_flow"
        assert result.file_path == "/path/to/flow.md"
        assert result.schedule == "0 * * * *"
        assert result.agent_profile == "developer"
        assert result.provider == "q_cli"
        assert result.script == "script.py"
        assert result.next_run == next_run
        assert result.enabled is True
    
    def test_get_flow_success(self, test_db):
        next_run = datetime.now() + timedelta(hours=1)
        create_flow("test_flow", "/path/to/flow.md", "0 * * * *", "developer", "q_cli", "script.py", next_run)
        
        result = get_flow("test_flow")
        
        assert result is not None
        assert result.name == "test_flow"
    
    def test_get_flow_not_found(self, test_db):
        result = get_flow("nonexistent")
        assert result is None
    
    def test_list_flows(self, test_db):
        next_run1 = datetime.now() + timedelta(hours=1)
        next_run2 = datetime.now() + timedelta(hours=2)
        
        create_flow("flow1", "/path1", "0 * * * *", "dev1", "q_cli", "script1.py", next_run1)
        create_flow("flow2", "/path2", "0 * * * *", "dev2", "q_cli", "script2.py", next_run2)
        
        result = list_flows()
        
        assert len(result) == 2
        assert {f.name for f in result} == {"flow1", "flow2"}
    
    def test_update_flow_run_times(self, test_db):
        next_run = datetime.now() + timedelta(hours=1)
        create_flow("test_flow", "/path", "0 * * * *", "dev", "q_cli", "script.py", next_run)
        
        last_run = datetime.now()
        new_next_run = datetime.now() + timedelta(hours=2)
        
        result = update_flow_run_times("test_flow", last_run, new_next_run)
        assert result is True
        
        result = update_flow_run_times("nonexistent", last_run, new_next_run)
        assert result is False
    
    def test_update_flow_enabled(self, test_db):
        next_run = datetime.now() + timedelta(hours=1)
        create_flow("test_flow", "/path", "0 * * * *", "dev", "q_cli", "script.py", next_run)
        
        result = update_flow_enabled("test_flow", False)
        assert result is True
        
        flow = get_flow("test_flow")
        assert flow.enabled is False
        
        result = update_flow_enabled("nonexistent", True)
        assert result is False
    
    def test_delete_flow(self, test_db):
        next_run = datetime.now() + timedelta(hours=1)
        create_flow("test_flow", "/path", "0 * * * *", "dev", "q_cli", "script.py", next_run)
        
        result = delete_flow("test_flow")
        assert result is True
        
        result = delete_flow("test_flow")
        assert result is False
    
    def test_get_flows_to_run(self, test_db):
        past_time = datetime.now() - timedelta(hours=1)
        future_time = datetime.now() + timedelta(hours=1)
        
        create_flow("ready_flow", "/path1", "0 * * * *", "dev", "q_cli", "script.py", past_time)
        create_flow("future_flow", "/path2", "0 * * * *", "dev", "q_cli", "script.py", future_time)
        
        # Disable one flow
        update_flow_enabled("ready_flow", False)
        create_flow("ready_flow2", "/path3", "0 * * * *", "dev", "q_cli", "script.py", past_time)
        
        result = get_flows_to_run()
        
        assert len(result) == 1
        assert result[0].name == "ready_flow2"