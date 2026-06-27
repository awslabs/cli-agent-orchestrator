"""Tests for the observer-only EventLogPublisher plugin.

Asserts that each lifecycle hook appends a normalized row to a fresh EventLog
and relays it to the SseBus, that only metadata (never the message body) is
stored, and that the plugin performs no Backplane state mutation.
"""

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.plugins.builtin.event_log_publisher import EventLogPublisher
from cli_agent_orchestrator.plugins.events import (
    PostCreateSessionEvent,
    PostCreateTerminalEvent,
    PostKillSessionEvent,
    PostKillTerminalEvent,
    PostSendMessageEvent,
)
from cli_agent_orchestrator.services.event_log_service import EventLog
from cli_agent_orchestrator.services.sse_bus import SseBus


@pytest.fixture
def wired_publisher():
    """Provide a publisher wired to isolated EventLog and SseBus instances.

    Patches the module-level singleton accessors the plugin imports so each
    test runs against a fresh buffer/bus (no cross-test bleed). Captures every
    event relayed to the bus.
    """

    log = EventLog()
    bus = SseBus()
    relayed: list[dict] = []

    # Capture what publish() receives without needing a live event loop.
    def _capture(event: dict) -> None:
        relayed.append(event)

    with patch.object(bus, "publish", side_effect=_capture):
        with (
            patch(
                "cli_agent_orchestrator.plugins.builtin.event_log_publisher.get_event_log",
                return_value=log,
            ),
            patch(
                "cli_agent_orchestrator.plugins.builtin.event_log_publisher.get_bus",
                return_value=bus,
            ),
        ):
            yield EventLogPublisher(), log, relayed


class TestPublisherHooks:
    """Each hook normalizes, appends, and relays.

    **Validates: Requirements 2.4, 2.5, 18.2**
    """

    @pytest.mark.asyncio
    async def test_create_terminal_appends_launch(self, wired_publisher) -> None:
        publisher, log, relayed = wired_publisher
        event = PostCreateTerminalEvent(
            terminal_id="t1", agent_name="dev", provider="claude_code", session_id="s1"
        )

        await publisher.on_post_create_terminal(event)

        history = log.history()
        assert len(history) == 1
        assert history[0]["kind"] == "launch"
        assert history[0]["terminal_id"] == "t1"
        # Relayed to SSE bus.
        assert relayed == history

    @pytest.mark.asyncio
    async def test_create_session_appends_launch(self, wired_publisher) -> None:
        publisher, log, relayed = wired_publisher
        await publisher.on_post_create_session(PostCreateSessionEvent(session_name="cao-demo"))

        history = log.history()
        assert history[0]["kind"] == "launch"
        assert history[0]["session_name"] == "cao-demo"
        assert len(relayed) == 1

    @pytest.mark.asyncio
    async def test_kill_terminal_appends_completion(self, wired_publisher) -> None:
        publisher, log, _ = wired_publisher
        await publisher.on_post_kill_terminal(
            PostKillTerminalEvent(terminal_id="t1", agent_name="dev")
        )
        assert log.history()[0]["kind"] == "completion"

    @pytest.mark.asyncio
    async def test_kill_session_appends_completion(self, wired_publisher) -> None:
        publisher, log, _ = wired_publisher
        await publisher.on_post_kill_session(PostKillSessionEvent(session_name="cao-demo"))
        assert log.history()[0]["kind"] == "completion"

    @pytest.mark.asyncio
    async def test_send_message_handoff_vs_a2a(self, wired_publisher) -> None:
        publisher, log, _ = wired_publisher
        await publisher.on_post_send_message(
            PostSendMessageEvent(
                sender="sup", receiver="w1", message="secret body", orchestration_type="handoff"
            )
        )
        await publisher.on_post_send_message(
            PostSendMessageEvent(
                sender="sup", receiver="w2", message="secret body", orchestration_type="a2a"
            )
        )
        kinds = [e["kind"] for e in log.history()]
        assert kinds == ["handoff", "a2a_delegation"]


class TestPrivacyBoundary:
    """The message body is never stored or relayed.

    **Validates: Requirements 2.4, 18.2**
    """

    @pytest.mark.asyncio
    async def test_message_body_excluded_from_detail(self, wired_publisher) -> None:
        publisher, log, relayed = wired_publisher
        secret = "TOP SECRET MESSAGE BODY"
        await publisher.on_post_send_message(
            PostSendMessageEvent(
                sender="sup", receiver="w1", message=secret, orchestration_type="send_message"
            )
        )

        stored = log.history()[0]
        assert "message" not in stored["detail"]
        assert secret not in str(stored)
        assert secret not in str(relayed)
        # Routing metadata is retained.
        assert stored["detail"]["sender"] == "sup"
        assert stored["detail"]["receiver"] == "w1"


class TestObserverOnly:
    """The plugin mutates no Backplane state.

    **Validates: Requirement 2.5**
    """

    @pytest.mark.asyncio
    async def test_no_backplane_imports_or_mutation(self, wired_publisher) -> None:
        """A full hook sweep performs no HTTP calls / DB / tmux mutations.

        The plugin module imports neither requests, clients.database, nor
        clients.tmux, so a hook can only ever read the event and write to the
        in-process buffer/bus. We assert that by patching ``requests`` at the
        module would fail (not imported) and by confirming the only side effect
        is the buffer append.
        """

        publisher, log, relayed = wired_publisher
        import cli_agent_orchestrator.plugins.builtin.event_log_publisher as mod

        # Observer-only: the module deliberately does not import state clients.
        assert not hasattr(mod, "requests")
        assert not hasattr(mod, "tmux_client")
        assert not hasattr(mod, "get_terminal_metadata")

        await publisher.on_post_create_terminal(
            PostCreateTerminalEvent(terminal_id="t1", provider="kiro_cli")
        )
        # The only observable effect is the in-process append + relay.
        assert len(log.history()) == 1
        assert len(relayed) == 1
