"""Tests for plugin event emission from service-layer operations."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.terminal import Terminal, TerminalStatus
from cli_agent_orchestrator.plugins import (
    PostCreateSessionEvent,
    PostCreateTerminalEvent,
    PostKillSessionEvent,
    PostKillTerminalEvent,
    PostSendMessageEvent,
)
from cli_agent_orchestrator.services.inbox_service import inbox_service
from cli_agent_orchestrator.services.session_service import create_session, delete_session
from cli_agent_orchestrator.services.terminal_service import (
    create_terminal,
    delete_terminal,
    send_input,
)


def _registry_mock() -> MagicMock:
    """Build a registry double whose async dispatch can be asserted directly."""

    registry = MagicMock()
    registry.dispatch = AsyncMock()
    return registry


class TestSessionPluginEvents:
    """Verify session lifecycle events are emitted correctly."""

    @pytest.mark.asyncio
    @patch(
        "cli_agent_orchestrator.services.session_service.create_terminal",
        new_callable=AsyncMock,
    )
    async def test_create_session_dispatches_post_create_session_event(self, mock_create_terminal):
        """Successful session creation should emit exactly one post_create_session event."""
        registry = _registry_mock()
        mock_create_terminal.return_value = Terminal(
            id="abcd1234",
            name="developer-abcd",
            session_name="cao-demo",
            provider="kiro_cli",
            agent_profile="developer",
        )

        result = await create_session(
            provider="kiro_cli",
            agent_profile="developer",
            session_name="cao-demo",
            registry=registry,
        )

        # dispatch_plugin_event schedules the hook as a background task when a
        # loop is running (the now-async create_session path); yield so it runs
        # before we assert it fired.
        await asyncio.sleep(0)

        assert result.session_name == "cao-demo"
        registry.dispatch.assert_awaited_once()
        event_type, event = registry.dispatch.await_args.args
        assert event_type == "post_create_session"
        assert isinstance(event, PostCreateSessionEvent)
        assert event.session_id == "cao-demo"
        assert event.session_name == "cao-demo"

    @pytest.mark.asyncio
    @patch(
        "cli_agent_orchestrator.services.session_service.create_terminal",
        new_callable=AsyncMock,
    )
    async def test_create_session_does_not_dispatch_on_failure(self, mock_create_terminal):
        """Session creation failures must not emit plugin events."""
        registry = _registry_mock()
        mock_create_terminal.side_effect = RuntimeError("tmux failed")

        with pytest.raises(RuntimeError, match="tmux failed"):
            await create_session(provider="kiro_cli", agent_profile="developer", registry=registry)

        registry.dispatch.assert_not_awaited()

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_dispatches_post_kill_session_event_after_cleanup(
        self, mock_tmux, mock_list_terminals, mock_delete_terminal
    ):
        """Session kill should emit after per-terminal cleanup and the tmux kill succeed."""
        registry = _registry_mock()
        call_order: list[str] = []

        async def record_dispatch(*_args):
            call_order.append("dispatch")

        mock_tmux.return_value.session_exists.return_value = True
        mock_tmux.return_value.kill_session.side_effect = lambda *_: call_order.append(
            "kill_session"
        )
        # One contained terminal so we can assert it is torn down before the
        # session is killed and the event is emitted.
        mock_list_terminals.return_value = [{"id": "abcd1234"}]
        mock_delete_terminal.side_effect = lambda *_args, **_kwargs: call_order.append(
            "delete_terminal"
        )
        registry.dispatch.side_effect = record_dispatch

        result = delete_session("cao-demo", registry=registry)

        assert result == {"deleted": ["cao-demo"], "errors": []}
        assert call_order == ["delete_terminal", "kill_session", "dispatch"]
        # Each contained terminal is cleaned up via the event-driven teardown path.
        mock_delete_terminal.assert_called_once_with("abcd1234", registry=registry)
        event_type, event = registry.dispatch.await_args.args
        assert event_type == "post_kill_session"
        assert isinstance(event, PostKillSessionEvent)
        assert event.session_id == "cao-demo"
        assert event.session_name == "cao-demo"

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_does_not_dispatch_on_failure(self, mock_tmux, mock_list_terminals):
        """Session deletion failures must not emit events."""
        registry = _registry_mock()
        mock_tmux.return_value.session_exists.return_value = True
        mock_list_terminals.side_effect = RuntimeError("db error")

        with pytest.raises(RuntimeError, match="db error"):
            delete_session("cao-missing", registry=registry)

        registry.dispatch.assert_not_awaited()


class TestTerminalPluginEvents:
    """Verify terminal lifecycle events are emitted correctly."""

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.build_skill_catalog", return_value="")
    @patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_window_name")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.FIFO_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    async def test_create_terminal_dispatches_post_create_terminal_event_after_setup(
        self,
        mock_status_monitor,
        mock_fifo_manager,
        mock_fifo_dir,
        mock_provider_manager,
        mock_db_create_terminal,
        mock_tmux,
        mock_generate_window_name,
        mock_generate_terminal_id,
        mock_load_agent_profile,
        mock_build_skill_catalog,
        mock_log_dir,
    ):
        """Terminal creation should emit only after persistence and startup complete."""
        registry = _registry_mock()
        call_order: list[str] = []

        async def record_dispatch(*_args):
            call_order.append("dispatch")

        mock_generate_terminal_id.return_value = "abcd1234"
        mock_generate_window_name.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = False
        # Default tmux backend has no event inbox, so create_terminal wires up the
        # FIFO/pipe-pane path; a bare MagicMock would report a truthy value and skip it.
        mock_tmux.supports_event_inbox.return_value = False
        mock_db_create_terminal.side_effect = lambda *_args, **_kwargs: call_order.append(
            "db_create"
        )
        mock_load_agent_profile.return_value = AgentProfile(name="developer", description="Dev")
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

        provider = MagicMock()
        # provider.initialize() is awaited inside create_terminal, so it must be
        # an AsyncMock; its side effect records ordering relative to the dispatch.
        provider.initialize = AsyncMock(
            side_effect=lambda: call_order.append("provider_initialize")
        )
        provider.shell_baseline = None
        mock_provider_manager.create_provider.return_value = provider

        log_path = MagicMock()
        mock_log_dir.__truediv__.return_value = log_path
        mock_tmux.pipe_pane.side_effect = lambda *_args, **_kwargs: call_order.append("pipe_pane")
        registry.dispatch.side_effect = record_dispatch

        terminal = await create_terminal(
            provider="kiro_cli",
            agent_profile="developer",
            session_name="demo",
            new_session=True,
            allowed_tools=["*"],
            registry=registry,
        )

        assert terminal.id == "abcd1234"
        # dispatch_plugin_event schedules the hook as a background task when a
        # loop is running (the now-async create_terminal path); yield so it runs
        # before we assert ordering.
        await asyncio.sleep(0)
        # pipe-pane is wired up before the provider initializes in the merged
        # event-driven flow; the dispatch still fires last, after all setup.
        assert call_order == ["db_create", "pipe_pane", "provider_initialize", "dispatch"]
        event_type, event = registry.dispatch.await_args.args
        assert event_type == "post_create_terminal"
        assert isinstance(event, PostCreateTerminalEvent)
        assert event.session_id == "cao-demo"
        assert event.terminal_id == "abcd1234"
        assert event.agent_name == "developer"
        assert event.provider == "kiro_cli"

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.build_skill_catalog", return_value="")
    @patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_window_name")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.FIFO_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    async def test_create_terminal_does_not_dispatch_on_failure(
        self,
        mock_status_monitor,
        mock_fifo_manager,
        mock_fifo_dir,
        mock_provider_manager,
        mock_db_create_terminal,
        mock_tmux,
        mock_generate_window_name,
        mock_generate_terminal_id,
        mock_load_agent_profile,
        mock_build_skill_catalog,
        mock_log_dir,
    ):
        """Terminal creation failures must not emit post_create_terminal."""
        registry = _registry_mock()
        mock_generate_terminal_id.return_value = "abcd1234"
        mock_generate_window_name.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = False
        mock_load_agent_profile.return_value = AgentProfile(name="developer", description="Dev")
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

        provider = MagicMock()
        # initialize() is awaited; the failure must surface through the await.
        provider.initialize = AsyncMock(side_effect=RuntimeError("provider init failed"))
        mock_provider_manager.create_provider.return_value = provider
        mock_log_dir.__truediv__.return_value = MagicMock()

        with pytest.raises(RuntimeError, match="provider init failed"):
            await create_terminal(
                provider="kiro_cli",
                agent_profile="developer",
                session_name="demo",
                new_session=True,
                allowed_tools=["*"],
                registry=registry,
            )

        registry.dispatch.assert_not_awaited()

    @patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal", return_value=True)
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_delete_terminal_dispatches_post_kill_terminal_event_after_delete(
        self, mock_get_metadata, mock_tmux, mock_provider_manager, mock_db_delete_terminal
    ):
        """Terminal kill should emit only after deletion succeeds."""
        registry = _registry_mock()
        call_order: list[str] = []

        async def record_dispatch(*_args):
            call_order.append("dispatch")

        mock_get_metadata.return_value = {
            "tmux_session": "cao-demo",
            "tmux_window": "developer-abcd",
            "agent_profile": "developer",
        }
        mock_provider_manager.cleanup_provider.side_effect = lambda *_: call_order.append("cleanup")
        mock_db_delete_terminal.side_effect = lambda *_: call_order.append("db_delete") or True
        registry.dispatch.side_effect = record_dispatch

        deleted = delete_terminal("abcd1234", registry=registry)

        assert deleted is True
        assert call_order[-2:] == ["db_delete", "dispatch"]
        event_type, event = registry.dispatch.await_args.args
        assert event_type == "post_kill_terminal"
        assert isinstance(event, PostKillTerminalEvent)
        assert event.session_id == "cao-demo"
        assert event.terminal_id == "abcd1234"
        assert event.agent_name == "developer"

    @patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_delete_terminal_does_not_dispatch_on_failure(
        self, mock_get_metadata, mock_tmux, mock_provider_manager, mock_db_delete_terminal
    ):
        """Deletion failures must not emit post_kill_terminal."""
        registry = _registry_mock()
        mock_get_metadata.return_value = {
            "tmux_session": "cao-demo",
            "tmux_window": "developer-abcd",
            "agent_profile": "developer",
        }
        mock_db_delete_terminal.side_effect = RuntimeError("db delete failed")

        with pytest.raises(RuntimeError, match="db delete failed"):
            delete_terminal("abcd1234", registry=registry)

        registry.dispatch.assert_not_awaited()


class TestMessagePluginEvents:
    """Verify message delivery emits the correct event payloads."""

    @pytest.mark.parametrize("orchestration_type", ["send_message", "assign", "handoff"])
    @patch("cli_agent_orchestrator.services.terminal_service.update_last_active")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_input_dispatches_post_send_message_event_for_each_orchestration_mode(
        self,
        mock_get_metadata,
        mock_provider_manager,
        mock_tmux,
        mock_update_last_active,
        orchestration_type,
    ):
        """Every successful delivery should emit one post_send_message event."""
        registry = _registry_mock()
        call_order: list[str] = []

        async def record_dispatch(*_args):
            call_order.append("dispatch")

        mock_get_metadata.return_value = {
            "tmux_session": "cao-demo",
            "tmux_window": "developer-abcd",
        }
        provider = MagicMock()
        provider.paste_enter_count = 2
        provider.mark_input_received.side_effect = lambda: call_order.append("mark_input_received")
        mock_provider_manager.get_provider.return_value = provider
        mock_tmux.send_keys.side_effect = lambda *_args, **_kwargs: call_order.append("send_keys")
        mock_update_last_active.side_effect = lambda *_: call_order.append("update_last_active")
        registry.dispatch.side_effect = record_dispatch

        delivered = send_input(
            "abcd1234",
            "Hello from supervisor",
            registry=registry,
            sender_id="supervisor-1",
            orchestration_type=orchestration_type,
        )

        assert delivered is True
        assert call_order[-1] == "dispatch"
        event_type, event = registry.dispatch.await_args.args
        assert event_type == "post_send_message"
        assert isinstance(event, PostSendMessageEvent)
        assert event.session_id == "cao-demo"
        assert event.sender == "supervisor-1"
        assert event.receiver == "abcd1234"
        assert event.message == "Hello from supervisor"
        assert event.orchestration_type == orchestration_type

    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_input_does_not_dispatch_on_failure(
        self, mock_get_metadata, mock_provider_manager, mock_tmux
    ):
        """Message delivery failures must not emit post_send_message."""
        registry = _registry_mock()
        mock_get_metadata.return_value = {
            "tmux_session": "cao-demo",
            "tmux_window": "developer-abcd",
        }
        provider = MagicMock()
        provider.paste_enter_count = 1
        mock_provider_manager.get_provider.return_value = provider
        mock_tmux.send_keys.side_effect = RuntimeError("send failed")

        with pytest.raises(RuntimeError, match="send failed"):
            send_input(
                "abcd1234",
                "Hello from supervisor",
                registry=registry,
                sender_id="supervisor-1",
                orchestration_type="assign",
            )

        registry.dispatch.assert_not_awaited()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_inbox_delivery_threads_send_message_context_to_terminal_service(
        self,
        mock_get_pending_messages,
        mock_provider_manager,
        mock_status_monitor,
        mock_terminal_service,
        mock_update_message_status,
    ):
        """Queued inbox delivery should forward sender context and hardcode send_message."""
        registry = _registry_mock()
        message = MagicMock()
        message.id = 17
        message.sender_id = "supervisor-1"
        message.message = "Please review this"
        mock_get_pending_messages.return_value = [message]
        # Status is sourced from the event-driven StatusMonitor, not the provider.
        mock_status_monitor.get_status.return_value = TerminalStatus.IDLE

        inbox_service.deliver_pending("abcd1234", registry=registry)

        mock_terminal_service.send_input.assert_called_once_with(
            "abcd1234",
            "Please review this",
            registry=registry,
            sender_id="supervisor-1",
            orchestration_type=OrchestrationType.SEND_MESSAGE,
        )
        mock_update_message_status.assert_called_once_with(17, MessageStatus.DELIVERED)
