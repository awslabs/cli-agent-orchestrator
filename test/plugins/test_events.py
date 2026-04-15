"""Tests for CAO plugin event dataclasses."""

from datetime import timedelta

from cli_agent_orchestrator.plugins.events import (
    CaoEvent,
    PostCreateSessionEvent,
    PostCreateTerminalEvent,
    PostKillSessionEvent,
    PostKillTerminalEvent,
    PostSendMessageEvent,
)


class TestEventDefaults:
    """Tests for default plugin event values."""

    def test_post_send_message_event_defaults(self) -> None:
        """PostSendMessageEvent defaults to the post_send_message type."""

        event = PostSendMessageEvent()

        assert event.event_type == "post_send_message"
        assert event.session_id is None
        assert isinstance(event, CaoEvent)

    def test_post_create_session_event_defaults(self) -> None:
        """PostCreateSessionEvent defaults to the post_create_session type."""

        event = PostCreateSessionEvent()

        assert event.event_type == "post_create_session"
        assert event.session_id is None

    def test_post_kill_session_event_defaults(self) -> None:
        """PostKillSessionEvent defaults to the post_kill_session type."""

        event = PostKillSessionEvent()

        assert event.event_type == "post_kill_session"
        assert event.session_id is None

    def test_post_create_terminal_event_defaults(self) -> None:
        """PostCreateTerminalEvent defaults to the post_create_terminal type."""

        event = PostCreateTerminalEvent()

        assert event.event_type == "post_create_terminal"
        assert event.session_id is None

    def test_post_kill_terminal_event_defaults(self) -> None:
        """PostKillTerminalEvent defaults to the post_kill_terminal type."""

        event = PostKillTerminalEvent()

        assert event.event_type == "post_kill_terminal"
        assert event.session_id is None

    def test_base_event_has_utc_timestamp(self) -> None:
        """CaoEvent auto-populates a timezone-aware UTC timestamp."""

        event = CaoEvent()

        assert event.timestamp.tzinfo is not None
        assert event.timestamp.utcoffset() == timedelta(0)
        assert event.event_type == ""
        assert event.session_id is None


class TestEventFields:
    """Tests for event-specific payload fields."""

    def test_post_send_message_event_accepts_orchestration_fields(self) -> None:
        """PostSendMessageEvent accepts all messaging payload fields."""

        event = PostSendMessageEvent(
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

        created_event = PostCreateSessionEvent(session_id="session-1", session_name="Build")
        killed_event = PostKillSessionEvent(session_id="session-1", session_name="Build")

        assert created_event.session_id == "session-1"
        assert created_event.session_name == "Build"
        assert killed_event.session_id == "session-1"
        assert killed_event.session_name == "Build"

    def test_terminal_events_carry_terminal_identifier_fields(self) -> None:
        """Terminal lifecycle events carry terminal-specific identifiers."""

        created_event = PostCreateTerminalEvent(
            session_id="session-2",
            terminal_id="term-1",
            agent_name="worker",
            provider="codex",
        )
        killed_event = PostKillTerminalEvent(
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
