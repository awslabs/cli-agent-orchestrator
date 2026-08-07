"""Tests for the session service."""

from unittest.mock import ANY, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.services.session_service import (
    create_session,
    delete_session,
    get_session,
    list_sessions,
)


class TestCreateSession:
    """Tests for create_session function."""

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.session_service.dispatch_plugin_event")
    @patch("cli_agent_orchestrator.services.session_service.create_terminal")
    @patch("cli_agent_orchestrator.services.session_service.resolve_provider")
    async def test_create_session_resolves_provider_when_omitted(
        self, mock_resolve, mock_create_terminal, mock_dispatch
    ):
        """When provider is None, resolve_provider is called and its result forwarded."""
        mock_resolve.return_value = "claude_code"
        mock_terminal = MagicMock()
        mock_terminal.session_name = "cao-test"
        mock_create_terminal.return_value = mock_terminal

        await create_session(provider=None, agent_profile="my_agent")

        mock_resolve.assert_called_once_with("my_agent", fallback_provider="kiro_cli")
        call_kwargs = mock_create_terminal.call_args.kwargs
        assert call_kwargs["provider"] == "claude_code"
        assert call_kwargs["defer_init"] is False
        assert call_kwargs["initial_message"] is None
        assert call_kwargs["model"] is None

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.session_service.dispatch_plugin_event")
    @patch("cli_agent_orchestrator.services.session_service.create_terminal")
    @patch("cli_agent_orchestrator.services.session_service.resolve_provider")
    async def test_create_session_uses_explicit_provider(
        self, mock_resolve, mock_create_terminal, mock_dispatch
    ):
        """When provider is explicitly passed, resolve_provider is NOT called."""
        mock_terminal = MagicMock()
        mock_terminal.session_name = "cao-test"
        mock_create_terminal.return_value = mock_terminal

        await create_session(provider="kiro_cli", agent_profile="my_agent")

        mock_resolve.assert_not_called()
        assert mock_create_terminal.call_args.kwargs["provider"] == "kiro_cli"

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.session_service.dispatch_plugin_event")
    @patch("cli_agent_orchestrator.services.session_service.create_terminal")
    async def test_create_session_forwards_launch_payload(
        self, mock_create_terminal, mock_dispatch
    ):
        """A first task selects the existing deferred-init path and reaches
        terminal creation alongside the model override."""
        mock_terminal = MagicMock()
        mock_terminal.session_name = "cao-test"
        mock_create_terminal.return_value = mock_terminal

        await create_session(
            provider="codex",
            agent_profile="my_agent",
            session_name="cao-test",
            initial_message="Review the current change",
            initial_message_orchestration_type=OrchestrationType.SEND_MESSAGE,
            model="gpt-5.1-codex",
        )

        call_kwargs = mock_create_terminal.call_args.kwargs
        assert call_kwargs["new_session"] is True
        assert call_kwargs["defer_init"] is True
        assert call_kwargs["initial_message"] == "Review the current change"
        assert call_kwargs["initial_message_orchestration_type"] == OrchestrationType.SEND_MESSAGE
        assert call_kwargs["model"] == "gpt-5.1-codex"

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.session_service.create_terminal")
    async def test_create_session_rejects_orchestration_type_without_message(
        self, mock_create_terminal
    ):
        """An incomplete initial-message payload fails instead of being dropped."""
        with pytest.raises(
            ValueError, match="initial_message_orchestration_type requires initial_message"
        ):
            await create_session(
                provider="codex",
                agent_profile="my_agent",
                initial_message_orchestration_type=OrchestrationType.SEND_MESSAGE,
            )

        mock_create_terminal.assert_not_called()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.session_service.create_terminal")
    async def test_create_session_rejects_empty_initial_message(self, mock_create_terminal):
        """Direct callers cannot turn an empty first task into deferred initialization."""
        with pytest.raises(ValueError, match="initial_message must not be empty"):
            await create_session(
                provider="codex",
                agent_profile="my_agent",
                initial_message="",
            )

        mock_create_terminal.assert_not_called()


class TestListSessions:
    """Tests for list_sessions function."""

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_success(self, mock_get_backend):
        """Test listing sessions successfully."""
        mock_get_backend.return_value.list_sessions.return_value = [
            {"id": "cao-session1", "name": "Session 1"},
            {"id": "cao-session2", "name": "Session 2"},
            {"id": "other-session", "name": "Other"},
        ]

        result = list_sessions()

        assert len(result) == 2
        assert all(s["id"].startswith("cao-") for s in result)

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_empty(self, mock_get_backend):
        """Test listing sessions when none exist."""
        mock_get_backend.return_value.list_sessions.return_value = []

        result = list_sessions()

        assert result == []

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_no_cao_sessions(self, mock_get_backend):
        """Test listing sessions when no CAO sessions exist."""
        mock_get_backend.return_value.list_sessions.return_value = [
            {"id": "other-session1", "name": "Other 1"},
            {"id": "other-session2", "name": "Other 2"},
        ]

        result = list_sessions()

        assert result == []

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_error(self, mock_get_backend):
        """Test listing sessions with error."""
        mock_get_backend.return_value.list_sessions.side_effect = Exception("Tmux error")

        result = list_sessions()

        assert result == []


class TestGetSession:
    """Tests for get_session function."""

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_success(self, mock_get_backend, mock_list_terminals):
        """Test getting session successfully."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_get_backend.return_value.list_sessions.return_value = [
            {"id": "cao-test", "name": "Test Session"}
        ]
        mock_list_terminals.return_value = [{"id": "terminal1", "session": "cao-test"}]

        result = get_session("cao-test")

        assert result["session"]["id"] == "cao-test"
        assert len(result["terminals"]) == 1
        mock_get_backend.return_value.session_exists.assert_called_once_with("cao-test")

    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor.get_status")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_enriches_terminals_with_live_status(
        self, mock_get_backend, mock_list_terminals, mock_get_status
    ):
        """Each terminal should carry its live status (consumed by the web UI
        and the cao-ops-mcp get_session_info tool an external supervisor polls)."""
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        mock_get_backend.return_value.session_exists.return_value = True
        mock_get_backend.return_value.list_sessions.return_value = [{"id": "cao-test"}]
        mock_list_terminals.return_value = [
            {"id": "term-a", "tmux_session": "cao-test"},
            {"id": "term-b", "tmux_session": "cao-test"},
        ]
        mock_get_status.side_effect = lambda tid: {
            "term-a": TerminalStatus.PROCESSING,
            "term-b": TerminalStatus.COMPLETED,
        }[tid]

        result = get_session("cao-test")

        assert result["terminals"][0]["status"] == "processing"
        assert result["terminals"][1]["status"] == "completed"

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_not_found(self, mock_get_backend):
        """Test getting non-existent session."""
        mock_get_backend.return_value.session_exists.return_value = False

        with pytest.raises(ValueError, match="Session 'cao-nonexistent' not found"):
            get_session("cao-nonexistent")

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_not_in_list(self, mock_get_backend):
        """Test getting session that exists but not in list."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_get_backend.return_value.list_sessions.return_value = []

        with pytest.raises(ValueError, match="Session 'cao-test' not found"):
            get_session("cao-test")

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_error(self, mock_get_backend):
        """Test getting session with error."""
        mock_get_backend.return_value.session_exists.side_effect = Exception("Tmux error")

        with pytest.raises(Exception, match="Tmux error"):
            get_session("cao-test")


class TestDeleteSession:
    """Tests for delete_session function.

    delete_session (#498) runs its whole critical section under the
    per-session-name lifecycle lock, captures each terminal's scrollback
    (read-only) first, checks session liveness with a STRICT existence check
    (a lookup error is not "gone"), disambiguates a False kill via a strict
    follow-up (a session gone-before-kill is success, not failure), and only
    THEN dismantles the per-terminal runtime and deletes registry rows — scoped
    BY ID to the incarnation it started tearing down. Faithful-fake, real-DB
    reconciliation and concurrency tests live in test_session_teardown_atomic.py.
    """

    @patch("cli_agent_orchestrator.services.session_service.delete_terminals_by_ids")
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal_row")
    @patch("cli_agent_orchestrator.services.terminal_service.dismantle_terminal_runtime")
    @patch("cli_agent_orchestrator.services.terminal_service.capture_terminal_snapshot")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_success(
        self,
        mock_get_backend,
        mock_list_terminals,
        mock_capture,
        mock_dismantle,
        mock_delete_row,
        mock_delete_terminals_by_ids,
    ):
        """Test deleting session successfully.

        delete_session captures each terminal's snapshot, kills the backend
        session through the verified backend primitive, and only after that
        confirmation dismantles the runtime (FIFO reader, status buffer,
        provider) and deletes the rows + sweeps by id.
        """
        mock_get_backend.return_value.session_exists_strict.return_value = True
        mock_get_backend.return_value.kill_session.return_value = True
        mock_list_terminals.return_value = [
            {"id": "terminal1"},
            {"id": "terminal2"},
        ]

        result = delete_session("cao-test")

        assert result == {"deleted": ["cao-test"], "errors": []}
        mock_get_backend.return_value.kill_session.assert_called_once_with("cao-test")
        # Registry rows are reconciled after kill_session confirms the session
        # is gone — scoped to the incarnation's ids, not the whole session name.
        mock_delete_terminals_by_ids.assert_called_once_with(["terminal1", "terminal2"])
        # Snapshots are captured while the panes still exist ...
        assert mock_capture.call_count == 2
        mock_capture.assert_any_call("terminal1")
        mock_capture.assert_any_call("terminal2")
        # ... and the runtime + row are only touched after the kill was confirmed.
        assert mock_dismantle.call_count == 2
        mock_dismantle.assert_any_call("terminal1", ANY, kill_window=False)
        mock_dismantle.assert_any_call("terminal2", ANY, kill_window=False)
        assert mock_delete_row.call_count == 2
        mock_delete_row.assert_any_call("terminal1", ANY, registry=ANY)
        mock_delete_row.assert_any_call("terminal2", ANY, registry=ANY)

    @patch("cli_agent_orchestrator.services.session_service.delete_terminals_by_ids")
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal_row")
    @patch("cli_agent_orchestrator.services.terminal_service.dismantle_terminal_runtime")
    @patch("cli_agent_orchestrator.services.terminal_service.capture_terminal_snapshot")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_when_backend_session_already_gone(
        self,
        mock_get_backend,
        mock_list_terminals,
        mock_capture,
        mock_dismantle,
        mock_delete_row,
        mock_delete_terminals_by_ids,
    ):
        """Backend session already gone — delete_session should not raise and not
        call kill_session, but still tear down each terminal and reconcile the
        registry."""
        mock_get_backend.return_value.session_exists_strict.return_value = False
        mock_list_terminals.return_value = [{"id": "terminal1"}]

        result = delete_session("cao-test")

        assert result == {"deleted": ["cao-test"], "errors": []}
        mock_get_backend.return_value.kill_session.assert_not_called()
        mock_capture.assert_called_once_with("terminal1")
        mock_dismantle.assert_called_once_with("terminal1", ANY, kill_window=False)
        mock_delete_row.assert_called_once_with("terminal1", ANY, registry=ANY)
        mock_delete_terminals_by_ids.assert_called_once_with(["terminal1"])

    @patch("cli_agent_orchestrator.services.session_service.delete_terminals_by_ids")
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal_row")
    @patch("cli_agent_orchestrator.services.terminal_service.dismantle_terminal_runtime")
    @patch("cli_agent_orchestrator.services.terminal_service.capture_terminal_snapshot")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_no_terminals(
        self,
        mock_get_backend,
        mock_list_terminals,
        mock_capture,
        mock_dismantle,
        mock_delete_row,
        mock_delete_terminals_by_ids,
    ):
        """Test deleting session with no terminals."""
        mock_get_backend.return_value.session_exists_strict.return_value = True
        mock_get_backend.return_value.kill_session.return_value = True
        mock_list_terminals.return_value = []

        result = delete_session("cao-test")

        assert result == {"deleted": ["cao-test"], "errors": []}
        mock_get_backend.return_value.kill_session.assert_called_once_with("cao-test")
        mock_capture.assert_not_called()
        mock_dismantle.assert_not_called()
        mock_delete_row.assert_not_called()
        mock_delete_terminals_by_ids.assert_called_once_with([])

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_error(self, mock_get_backend, mock_list_terminals):
        """Test deleting session with error."""
        mock_get_backend.return_value.session_exists_strict.return_value = True
        mock_list_terminals.side_effect = Exception("Database error")

        with pytest.raises(Exception, match="Database error"):
            delete_session("cao-test")

    @patch("cli_agent_orchestrator.services.session_service.delete_terminals_by_ids")
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal_row")
    @patch("cli_agent_orchestrator.services.terminal_service.dismantle_terminal_runtime")
    @patch("cli_agent_orchestrator.services.terminal_service.capture_terminal_snapshot")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_continues_when_terminal_cleanup_fails(
        self,
        mock_get_backend,
        mock_list_terminals,
        mock_capture,
        mock_dismantle,
        mock_delete_row,
        mock_delete_terminals_by_ids,
    ):
        """delete_session continues when one terminal's snapshot capture fails.

        A failed capture yields no metadata but must not abort the teardown, drop
        the terminal from the incarnation, or skip the session kill.
        """
        mock_get_backend.return_value.session_exists_strict.return_value = True
        mock_get_backend.return_value.kill_session.return_value = True
        mock_list_terminals.return_value = [
            {"id": "terminal1"},
            {"id": "terminal2"},
            {"id": "terminal3"},
        ]

        # First terminal's snapshot capture fails, others succeed
        mock_capture.side_effect = [
            Exception("Snapshot error for terminal1"),
            None,  # terminal2 succeeds
            None,  # terminal3 succeeds
        ]

        result = delete_session("cao-test")

        # Session should still be deleted despite per-terminal teardown failure
        assert result == {"deleted": ["cao-test"], "errors": []}
        mock_get_backend.return_value.kill_session.assert_called_once_with("cao-test")
        # All three captures were attempted ...
        assert mock_capture.call_count == 3
        # ... and every terminal is still dismantled and row-deleted: a failed
        # capture only costs its metadata (passed as None), never its teardown.
        assert mock_dismantle.call_count == 3
        assert mock_delete_row.call_count == 3
        mock_dismantle.assert_any_call("terminal1", None, kill_window=False)
        mock_delete_row.assert_any_call("terminal1", None, registry=ANY)
        # The by-id sweep still backstops any row a failed delete left behind.
        mock_delete_terminals_by_ids.assert_called_once_with(
            ["terminal1", "terminal2", "terminal3"]
        )

    @patch("cli_agent_orchestrator.services.session_service.delete_terminals_by_ids")
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal_row")
    @patch("cli_agent_orchestrator.services.terminal_service.dismantle_terminal_runtime")
    @patch("cli_agent_orchestrator.services.terminal_service.capture_terminal_snapshot")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_cleans_up_each_terminal(
        self,
        mock_get_backend,
        mock_list_terminals,
        mock_capture,
        mock_dismantle,
        mock_delete_row,
        mock_delete_terminals_by_ids,
    ):
        """Test that delete_session tears down every terminal in the session."""
        mock_get_backend.return_value.session_exists_strict.return_value = True
        mock_get_backend.return_value.kill_session.return_value = True
        mock_list_terminals.return_value = [
            {"id": "term-aaa"},
            {"id": "term-bbb"},
            {"id": "term-ccc"},
            {"id": "term-ddd"},
        ]

        result = delete_session("cao-multi-terminal")

        assert result == {"deleted": ["cao-multi-terminal"], "errors": []}
        # Verify all three teardown phases ran for each terminal id
        assert mock_capture.call_count == 4
        assert mock_dismantle.call_count == 4
        assert mock_delete_row.call_count == 4
        for tid in ("term-aaa", "term-bbb", "term-ccc", "term-ddd"):
            mock_capture.assert_any_call(tid)
            mock_dismantle.assert_any_call(tid, ANY, kill_window=False)
            mock_delete_row.assert_any_call(tid, ANY, registry=ANY)
