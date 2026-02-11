"""Tests for session model."""

import pytest

from cli_agent_orchestrator.models.session import Session, SessionStatus


class TestSessionStatus:
    """Tests for SessionStatus enum."""

    def test_active_status(self):
        """Test ACTIVE status value."""
        assert SessionStatus.ACTIVE.value == "active"

    def test_detached_status(self):
        """Test DETACHED status value."""
        assert SessionStatus.DETACHED.value == "detached"

    def test_terminated_status(self):
        """Test TERMINATED status value."""
        assert SessionStatus.TERMINATED.value == "terminated"


class TestSession:
    """Tests for Session model."""

    def test_create_session(self):
        """Test creating a session."""
        session = Session(
            id="test-123",
            name="Test Session",
            status=SessionStatus.ACTIVE,
        )

        assert session.id == "test-123"
        assert session.name == "Test Session"
        assert session.status == "active"  # use_enum_values=True

    def test_create_session_with_string_status(self):
        """Test creating a session with string status."""
        session = Session(
            id="test-456",
            name="Another Session",
            status="detached",
        )

        assert session.status == "detached"

    def test_session_required_fields(self):
        """Test session requires all fields."""
        with pytest.raises(Exception):
            Session(id="test-123")

    def test_session_model_dump(self):
        """Test session model dump."""
        session = Session(
            id="test-789",
            name="Dump Test",
            status=SessionStatus.TERMINATED,
        )

        data = session.model_dump()
        assert data["id"] == "test-789"
        assert data["name"] == "Dump Test"
        assert data["status"] == "terminated"
