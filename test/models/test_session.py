"""Tests for Session model with parent_session field."""
import pytest
from cli_agent_orchestrator.models.session import Session, SessionStatus


class TestSessionParentSession:
    """Test parent_session field in Session model."""

    def test_session_has_parent_session_field(self):
        """Session model should have optional parent_session field."""
        session = Session(
            id="worker-1",
            name="worker-session",
            status=SessionStatus.ACTIVE
        )
        # parent_session should exist and default to None
        assert hasattr(session, 'parent_session')
        assert session.parent_session is None

    def test_session_with_parent_session(self):
        """Session can be created with parent_session."""
        session = Session(
            id="worker-1",
            name="worker-session",
            status=SessionStatus.ACTIVE,
            parent_session="supervisor-1"
        )
        assert session.parent_session == "supervisor-1"

    def test_session_serialization_includes_parent_session(self):
        """Session serialization should include parent_session."""
        session = Session(
            id="worker-1",
            name="worker-session",
            status=SessionStatus.ACTIVE,
            parent_session="supervisor-1"
        )
        data = session.model_dump()
        assert 'parent_session' in data
        assert data['parent_session'] == "supervisor-1"

    def test_session_without_parent_is_supervisor_or_standalone(self):
        """Session without parent_session is either supervisor or standalone."""
        supervisor = Session(
            id="supervisor-1",
            name="supervisor-session",
            status=SessionStatus.ACTIVE
        )
        assert supervisor.parent_session is None
