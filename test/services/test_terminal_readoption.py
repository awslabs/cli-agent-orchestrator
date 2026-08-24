"""Tests for terminal durability: startup re-adoption + early snapshots.

create_terminal is the only place the FIFO -> EventBus logging pipeline is
armed, so a cao-server restart used to leave live tmux agents half-adopted
(pane alive, <tid>.log frozen, no status detection) and a crash left no
snapshot at all. These tests cover readopt_terminals_at_startup() and the
early-snapshot write.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.terminal_service import (
    _write_terminal_snapshot,
    readopt_terminals_at_startup,
)


def _row(tid="t1", session="cao-s", window="dev-1"):
    return {
        "id": tid,
        "tmux_session": session,
        "tmux_window": window,
        "provider": "claude_code",
        "agent_profile": "developer",
        "working_directory": "/tmp",
        "engine": None,
        "last_active": None,
    }


class TestReadoptTerminalsAtStartup:
    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.list_all_terminals")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    async def test_rearms_live_terminal(
        self, mock_backend, mock_list, mock_fifo, mock_db_delete
    ):
        mock_backend.supports_event_inbox.return_value = False
        mock_backend.session_exists.return_value = True
        mock_backend.get_history.return_value = "$ "
        mock_list.return_value = [_row()]

        counts = await readopt_terminals_at_startup()

        assert counts == {"readopted": 1, "finalized": 0}
        mock_fifo.create_reader.assert_called_once()
        assert mock_fifo.create_reader.call_args[0][0] == "t1"
        # stop-then-start: a stalled pane still reports pane_pipe=1, so a bare
        # pipe_pane() toggle would switch the dead pipe OFF.
        mock_backend.stop_pipe_pane.assert_called_once_with("cao-s", "dev-1")
        mock_backend.pipe_pane.assert_called_once()
        # Post-pipe repaint nudge: without it the fresh rolling buffer stays
        # empty and the re-adopted terminal reads UNKNOWN until it speaks.
        mock_backend.send_special_key.assert_called_once_with("cao-s", "dev-1", "Enter")
        mock_db_delete.assert_not_called()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.list_all_terminals")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    async def test_finalizes_dead_terminal_with_scrollback_from_log(
        self, mock_backend, mock_list, mock_fifo, mock_db_delete, tmp_path
    ):
        mock_backend.supports_event_inbox.return_value = False
        mock_backend.session_exists.return_value = False
        mock_list.return_value = [_row(tid="dead1")]

        log = tmp_path / "dead1.log"
        log.write_text("hello \x1b[31mworld\x1b[0m\n", encoding="utf-8")

        with patch.object(terminal_service, "TERMINAL_LOG_DIR", tmp_path):
            counts = await readopt_terminals_at_startup()

        assert counts == {"readopted": 0, "finalized": 1}
        scrollback = (tmp_path / "dead1.scrollback").read_text(encoding="utf-8")
        assert "hello" in scrollback and "world" in scrollback
        assert "\x1b" not in scrollback  # ANSI-stripped
        mock_db_delete.assert_called_once_with("dead1")
        mock_fifo.create_reader.assert_not_called()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.list_all_terminals")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    async def test_existing_scrollback_not_overwritten(
        self, mock_backend, mock_list, mock_fifo, mock_db_delete, tmp_path
    ):
        """A clean-delete scrollback (full pane capture) is better than the
        log-derived one — finalization must not clobber it."""
        mock_backend.supports_event_inbox.return_value = False
        mock_backend.session_exists.return_value = False
        mock_list.return_value = [_row(tid="dead2")]

        (tmp_path / "dead2.log").write_text("from-log", encoding="utf-8")
        (tmp_path / "dead2.scrollback").write_text("from-clean-delete", encoding="utf-8")

        with patch.object(terminal_service, "TERMINAL_LOG_DIR", tmp_path):
            counts = await readopt_terminals_at_startup()

        assert counts == {"readopted": 0, "finalized": 1}
        assert (tmp_path / "dead2.scrollback").read_text() == "from-clean-delete"

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.list_all_terminals")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    async def test_event_inbox_backend_is_noop(self, mock_backend, mock_list):
        mock_backend.supports_event_inbox.return_value = True

        counts = await readopt_terminals_at_startup()

        assert counts == {"readopted": 0, "finalized": 0}
        mock_list.assert_not_called()


class TestEarlySnapshot:
    def test_write_terminal_snapshot(self, tmp_path):
        with patch.object(terminal_service, "TERMINAL_LOG_DIR", tmp_path):
            _write_terminal_snapshot(
                "t9",
                session_name="cao-s",
                window_name="dev-9",
                agent_profile="developer",
                provider="claude_code",
                working_directory="/repo",
                allowed_tools=["fs_read"],
                caller_id=None,
            )

        snapshot = json.loads((tmp_path / "t9.snapshot.json").read_text())
        assert snapshot["terminal_id"] == "t9"
        assert snapshot["session_name"] == "cao-s"
        assert snapshot["provider"] == "claude_code"
        assert snapshot["allowed_tools"] == ["fs_read"]

    def test_write_terminal_snapshot_never_raises(self, tmp_path):
        """Best-effort contract: a bad log dir must not break the caller."""
        with patch.object(
            terminal_service, "TERMINAL_LOG_DIR", tmp_path / "missing" / "nested"
        ):
            _write_terminal_snapshot(
                "t10",
                session_name="s",
                window_name="w",
                agent_profile=None,
                provider="claude_code",
                working_directory=None,
                allowed_tools=None,
                caller_id=None,
            )  # no exception
