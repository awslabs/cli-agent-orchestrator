"""Tests for CAO plugin event dataclasses."""

from datetime import timezone

from cli_agent_orchestrator.plugins.events import (
    CaoEvent,
    MessageSentEvent,
    SessionCreatedEvent,
    SessionKilledEvent,
    TerminalCreatedEvent,
    TerminalKilledEvent,
)


class TestEventDefaults:
    """Tests for default plugin event values."""

    def test_message_sent_event_defaults(self) -> None:
        """MessageSentEvent defaults to the message_sent type."""

        event = MessageSentEvent()

        assert event.event_type == "message_sent"
        assert event.session_id is None
        assert isinstance(event, CaoEvent)

    def test_session_created_event_defaults(self) -> None:
        """SessionCreatedEvent defaults to the session_created type."""

        event = SessionCreatedEvent()

        assert event.event_type == "session_created"
        assert event.session_id is None

    def test_session_killed_event_defaults(self) -> None:
        """SessionKilledEvent defaults to the session_killed type."""

        event = SessionKilledEvent()

        assert event.event_type == "session_killed"
        assert event.session_id is None

    def test_terminal_created_event_defaults(self) -> None:
        """TerminalCreatedEvent defaults to the terminal_created type."""

        event = TerminalCreatedEvent()

        assert event.event_type == "terminal_created"
        assert event.session_id is None

    def test_terminal_killed_event_defaults(self) -> None:
        """TerminalKilledEvent defaults to the terminal_killed type."""

        event = TerminalKilledEvent()

        assert event.event_type == "terminal_killed"
        assert event.session_id is None

    def test_base_event_has_utc_timestamp(self) -> None:
        """CaoEvent auto-populates a timezone-aware UTC timestamp."""

        event = CaoEvent()

        assert event.timestamp.tzinfo is not None
        assert event.timestamp.utcoffset() == timezone.utc.utcoffset(event.timestamp)
        assert event.event_type == ""
        assert event.session_id is None


class TestEventFields:
    """Tests for event-specific payload fields."""

    def test_message_sent_event_accepts_orchestration_fields(self) -> None:
        """MessageSentEvent accepts all messaging payload fields."""

        event = MessageSentEvent(
            session_id="session-123",
            sender="supervisor",
            receiver="worker-1",
            message="Process this task",
            orchestration_type="assign",
        )

        assert event.session_id == "session-123"
        assert event.sender == "supervisor"
        assert event.receiver == "worker-1"
        assert event.message == "Process this task"
        assert event.orchestration_type == "assign"

    def test_session_events_carry_session_identifier_fields(self) -> None:
        """Session lifecycle events carry their session name payload."""

        created_event = SessionCreatedEvent(session_id="session-1", session_name="Build")
        killed_event = SessionKilledEvent(session_id="session-1", session_name="Build")

        assert created_event.session_id == "session-1"
        assert created_event.session_name == "Build"
        assert killed_event.session_id == "session-1"
        assert killed_event.session_name == "Build"

    def test_terminal_events_carry_terminal_identifier_fields(self) -> None:
        """Terminal lifecycle events carry terminal-specific identifiers."""

        created_event = TerminalCreatedEvent(
            session_id="session-2",
            terminal_id="term-1",
            agent_name="worker",
            provider="codex",
        )
        killed_event = TerminalKilledEvent(
            session_id="session-2",
            terminal_id="term-1",
            agent_name="worker",
        )

        assert created_event.session_id == "session-2"
        assert created_event.terminal_id == "term-1"
        assert created_event.agent_name == "worker"
        assert created_event.provider == "codex"
        assert killed_event.session_id == "session-2"
        assert killed_event.terminal_id == "term-1"
        assert killed_event.agent_name == "worker"
