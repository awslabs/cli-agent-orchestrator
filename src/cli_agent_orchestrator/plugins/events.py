"""Typed plugin event dataclasses for CAO lifecycle and messaging hooks."""

from dataclasses import dataclass, field
from datetime import datetime, timezone


def _utc_now() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""

    return datetime.now(timezone.utc)


@dataclass
class CaoEvent:
    """Base class for all CAO plugin events."""

    # Empty by default so the base dataclass is zero-arg constructible for Phase 1 tests.
    event_type: str = ""
    timestamp: datetime = field(default_factory=_utc_now)
    session_id: str | None = None


@dataclass
class PostSendMessageEvent(CaoEvent):
    """Emitted after a message is dispatched to an agent's inbox.

    Fired for all three orchestration methods:
    - send_message: direct message to an existing terminal
    - handoff: message sent as part of a synchronous handoff
    - assign: message sent as part of an asynchronous assign

    Orchestration methods like assign span multiple steps and may therefore
    emit more than one PostSendMessageEvent across their lifecycle.
    """

    event_type: str = "post_send_message"
    sender: str = ""
    receiver: str = ""
    message: str = ""
    orchestration_type: str = ""


@dataclass
class PostCreateSessionEvent(CaoEvent):
    """Emitted after a CAO session is created."""

    event_type: str = "post_create_session"
    session_name: str = ""


@dataclass
class PostKillSessionEvent(CaoEvent):
    """Emitted after a CAO session is killed."""

    event_type: str = "post_kill_session"
    session_name: str = ""


@dataclass
class PostCreateTerminalEvent(CaoEvent):
    """Emitted after a CAO terminal is created."""

    event_type: str = "post_create_terminal"
    terminal_id: str = ""
    agent_name: str | None = None
    provider: str = ""


@dataclass
class PostKillTerminalEvent(CaoEvent):
    """Emitted after a CAO terminal is killed."""

    event_type: str = "post_kill_terminal"
    terminal_id: str = ""
    agent_name: str | None = None
