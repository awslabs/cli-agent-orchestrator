"""Tests for the session service."""

import asyncio
import contextlib
import logging
import os
import shlex
import uuid
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.backends import registry as backend_registry
from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend
from cli_agent_orchestrator.clients import database as db_mod
from cli_agent_orchestrator.clients.database import get_terminal_metadata
from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.services import fifo_reader as fifo_reader_mod
from cli_agent_orchestrator.services import session_service as session_service_mod
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.services.session_service import (
    create_session,
    delete_session,
    get_session,
    list_sessions,
)
from cli_agent_orchestrator.services.status_monitor import status_monitor


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
    @patch("cli_agent_orchestrator.services.session_service.dispatch_plugin_event")
    @patch("cli_agent_orchestrator.services.session_service.create_terminal")
    async def test_create_session_forwards_use_worktree(self, mock_create_terminal, mock_dispatch):
        """Regression (review on PR #634): a fresh session used to drop
        use_worktree silently -- nothing threaded it this far, unlike the
        existing-session terminal-creation path."""
        mock_terminal = MagicMock()
        mock_terminal.session_name = "cao-test"
        mock_create_terminal.return_value = mock_terminal

        await create_session(provider="kiro_cli", agent_profile="my_agent", use_worktree=True)

        assert mock_create_terminal.call_args.kwargs["use_worktree"] is True

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.session_service.dispatch_plugin_event")
    @patch("cli_agent_orchestrator.services.session_service.create_terminal")
    async def test_create_session_use_worktree_defaults_to_false(
        self, mock_create_terminal, mock_dispatch
    ):
        mock_terminal = MagicMock()
        mock_terminal.session_name = "cao-test"
        mock_create_terminal.return_value = mock_terminal

        await create_session(provider="kiro_cli", agent_profile="my_agent")

        assert mock_create_terminal.call_args.kwargs["use_worktree"] is False

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.session_service.dispatch_plugin_event")
    @patch("cli_agent_orchestrator.services.session_service.create_terminal")
    async def test_create_session_forwards_idempotency_key(
        self, mock_create_terminal, mock_dispatch
    ):
        """Review on PR #634, issue #616: forwarded as-is to create_terminal."""
        mock_terminal = MagicMock()
        mock_terminal.session_name = "cao-test"
        mock_create_terminal.return_value = mock_terminal

        await create_session(
            provider="kiro_cli", agent_profile="my_agent", idempotency_key="retry-1"
        )

        assert mock_create_terminal.call_args.kwargs["idempotency_key"] == "retry-1"

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


def _swallowed_log(caplog, message):
    """Return the record ``session_service`` emitted for ``message``.

    Selecting by logger name AND message, not by level: a level-only scan
    (``any(r.exc_info for r in caplog.records if r.levelname == ...)``) passes
    when ANY logger emits a record with a traceback at that level, so the
    handler under test can lose its own ``exc_info`` and the assertion still
    holds. ``caplog.text`` does not save it either — traceback text is included
    in ``.text``, so a substring check is satisfiable by the traceback alone.
    """
    return next(
        r
        for r in caplog.records
        if r.name == "cli_agent_orchestrator.services.session_service"
        and r.getMessage().startswith(message)
    )


class TestListSessions:
    """Tests for list_sessions function."""

    class _FakeTmuxClient:
        def __init__(self, sessions, working_directories):
            self._sessions = sessions
            self._working_directories = working_directories
            self.cwd_calls = []

        def list_sessions(self):
            return self._sessions

        def get_pane_working_directory(self, session_name, window_name):
            self.cwd_calls.append((session_name, window_name))
            value = self._working_directories[(session_name, window_name)]
            if isinstance(value, Exception):
                raise value
            return value

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_in_sessions")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_success(self, mock_get_backend, mock_list_in_sessions):
        """Test listing sessions successfully."""
        mock_get_backend.return_value.list_sessions.return_value = [
            {"id": "cao-session1", "name": "Session 1"},
            {"id": "cao-session2", "name": "Session 2"},
            {"id": "other-session", "name": "Other"},
        ]
        mock_list_in_sessions.return_value = []

        result = list_sessions()

        assert len(result) == 2
        assert all(s["id"].startswith("cao-") for s in result)
        assert all("working_directory" in s for s in result)
        assert all("agent_profile" in s for s in result)

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_empty(self, mock_get_backend):
        """Test listing sessions when none exist."""
        mock_get_backend.return_value.list_sessions.return_value = []

        result = list_sessions()

        assert result == []

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_in_sessions")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_no_cao_sessions(self, mock_get_backend, mock_list_in_sessions):
        """Test listing sessions when no CAO sessions exist."""
        mock_get_backend.return_value.list_sessions.return_value = [
            {"id": "other-session1", "name": "Other 1"},
            {"id": "other-session2", "name": "Other 2"},
        ]

        result = list_sessions()

        assert result == []
        mock_list_in_sessions.assert_not_called()

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_error(self, mock_get_backend):
        """Test listing sessions with error."""
        mock_get_backend.return_value.list_sessions.side_effect = Exception("Tmux error")

        result = list_sessions()

        assert result == []

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_in_sessions")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_prefers_persisted_working_directory(
        self, mock_get_backend, mock_list_in_sessions
    ):
        """Launch-time cwd from terminal metadata is the preferred ownership signal."""
        fake_client = self._FakeTmuxClient(
            [{"id": "cao-owned", "name": "cao-owned", "status": "detached"}],
            {("cao-owned", "developer-abcd"): AssertionError("pane cwd should not be used")},
        )
        mock_get_backend.return_value = TmuxBackend(client=fake_client)
        mock_list_in_sessions.return_value = [
            {
                "id": "term1",
                "tmux_session": "cao-owned",
                "tmux_window": "developer-abcd",
                "agent_profile": "developer",
                "working_directory": "/launch/project",
            }
        ]

        result = list_sessions()

        assert result == [
            {
                "id": "cao-owned",
                "name": "cao-owned",
                "status": "detached",
                "agent_profile": "developer",
                "working_directory": "/launch/project",
            }
        ]
        assert fake_client.cwd_calls == []

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_in_sessions")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_falls_back_to_pane_working_directory(
        self, mock_get_backend, mock_list_in_sessions
    ):
        """When no launch cwd is stored, list_sessions resolves the pane cwd."""
        fake_client = self._FakeTmuxClient(
            [{"id": "cao-owned", "name": "cao-owned", "status": "detached"}],
            {("cao-owned", "developer-abcd"): "/pane/project"},
        )
        mock_get_backend.return_value = TmuxBackend(client=fake_client)
        mock_list_in_sessions.return_value = [
            {
                "id": "term1",
                "tmux_session": "cao-owned",
                "tmux_window": "developer-abcd",
                "agent_profile": "developer",
                "working_directory": None,
            }
        ]

        result = list_sessions()

        assert result[0]["working_directory"] == "/pane/project"
        assert result[0]["agent_profile"] == "developer"
        assert fake_client.cwd_calls == [("cao-owned", "developer-abcd")]

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_in_sessions")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_keeps_session_when_working_directory_unresolvable(
        self, mock_get_backend, mock_list_in_sessions
    ):
        """A cwd resolution failure affects only that field, not the session list."""
        fake_client = self._FakeTmuxClient(
            [{"id": "cao-owned", "name": "cao-owned", "status": "detached"}],
            {("cao-owned", "developer-abcd"): RuntimeError("pane unavailable")},
        )
        mock_get_backend.return_value = TmuxBackend(client=fake_client)
        mock_list_in_sessions.return_value = [
            {
                "id": "term1",
                "tmux_session": "cao-owned",
                "tmux_window": "developer-abcd",
                "agent_profile": "developer",
                "working_directory": None,
            }
        ]

        result = list_sessions()

        assert len(result) == 1
        assert result[0]["id"] == "cao-owned"
        assert result[0]["working_directory"] is None
        assert result[0]["agent_profile"] == "developer"

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_in_sessions")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_handles_orphaned_tmux_session(
        self, mock_get_backend, mock_list_in_sessions
    ):
        """A tmux session with no DB terminals still lists (null metadata)."""
        fake_client = self._FakeTmuxClient(
            [{"id": "cao-orphaned", "name": "Orphaned", "status": "active"}],
            {},
        )
        mock_get_backend.return_value = TmuxBackend(client=fake_client)
        mock_list_in_sessions.return_value = []

        result = list_sessions()

        assert len(result) == 1
        assert result[0]["id"] == "cao-orphaned"
        assert result[0]["working_directory"] is None
        assert result[0]["agent_profile"] is None

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_in_sessions")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_lists_a_session_with_no_terminal_rows(
        self, mock_get_backend, mock_list_in_sessions
    ):
        """A session absent from the terminal read still lists, with null metadata.

        Previously this mocked a per-session query that raised for the second
        session. With one bulk read there is no per-session failure to simulate:
        the read either succeeds (this test — ``cao-bad`` simply has no rows) or
        fails wholesale (``test_list_sessions_survives_a_failed_bulk_terminal_read``).
        """
        fake_client = self._FakeTmuxClient(
            [
                {"id": "cao-good", "name": "Good", "status": "active"},
                {"id": "cao-bad", "name": "Bad", "status": "active"},
            ],
            {("cao-good", "win-good"): "/home/user/project"},
        )
        mock_get_backend.return_value = TmuxBackend(client=fake_client)
        mock_list_in_sessions.return_value = [
            {
                "id": "term-good",
                "tmux_session": "cao-good",
                "tmux_window": "win-good",
                "agent_profile": "developer",
                "working_directory": None,
            }
        ]

        result = list_sessions()

        assert len(result) == 2
        assert result[0]["id"] == "cao-good"
        assert result[0]["working_directory"] == "/home/user/project"
        assert result[0]["agent_profile"] == "developer"
        assert result[1]["id"] == "cao-bad"
        assert result[1]["working_directory"] is None
        assert result[1]["agent_profile"] is None

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_in_sessions")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_ignores_none_id_without_blanking_result(
        self, mock_get_backend, mock_list_in_sessions
    ):
        """A backend row with id=None should be skipped without blanking valid rows."""
        mock_get_backend.return_value.list_sessions.return_value = [
            {"id": None, "name": "Bad"},
            {"id": "cao-good", "name": "Good"},
        ]
        mock_list_in_sessions.return_value = []

        result = list_sessions()

        assert result == [
            {
                "id": "cao-good",
                "name": "Good",
                "working_directory": None,
                "agent_profile": None,
            }
        ]

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_in_sessions")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_uses_one_terminal_for_profile_and_directory(
        self, mock_get_backend, mock_list_in_sessions
    ):
        """Ownership metadata should not mix profile and cwd from different terminals."""
        fake_client = self._FakeTmuxClient(
            [{"id": "cao-owned", "name": "cao-owned", "status": "detached"}],
            {("cao-owned", "developer-abcd"): "/pane/developer"},
        )
        mock_get_backend.return_value = TmuxBackend(client=fake_client)
        mock_list_in_sessions.return_value = [
            {
                "id": "term1",
                "tmux_session": "cao-owned",
                "tmux_window": "developer-abcd",
                "agent_profile": "developer",
                "working_directory": None,
            },
            {
                "id": "term2",
                "tmux_session": "cao-owned",
                "tmux_window": "reviewer-efgh",
                "agent_profile": None,
                "working_directory": "/launch/reviewer",
            },
        ]

        result = list_sessions()

        assert result[0]["agent_profile"] == "developer"
        assert result[0]["working_directory"] == "/pane/developer"
        assert fake_client.cwd_calls == [("cao-owned", "developer-abcd")]

    # ── The terminal read scales with neither session count nor table size ──
    #
    # Issue #629. These assert the PROPERTY, not which function gets called: a
    # pin on `list_terminals_in_sessions.call_count == 1` would have to be
    # deleted by anyone changing the read's shape again, which makes it a
    # restatement of the implementation rather than a guard on its behaviour.

    @pytest.mark.parametrize("session_count", [1, 3, 30])
    def test_terminal_read_count_does_not_grow_with_session_count(self, session_count, monkeypatch):
        """Listing N sessions must not cost O(N) terminal reads.

        Regression guard for the N+1: enrichment used to issue one
        ``list_terminals_by_session`` per tmux session, so a shared cao-server
        paid a query per session every time this path was polled. Counting reads
        across several session counts is what catches a reintroduction, however
        it is spelled.
        """
        reads: list = []

        def _counting_read(names):
            reads.append(list(names))
            return [
                {
                    "id": f"term{n}",
                    "tmux_session": f"cao-session{n}",
                    "tmux_window": f"developer-{n}",
                    "agent_profile": "developer",
                    "working_directory": f"/project/{n}",
                }
                for n in range(session_count)
            ]

        def _boom(session_name):
            raise AssertionError(f"per-session query reintroduced for {session_name}")

        monkeypatch.setattr(session_service_mod, "list_terminals_in_sessions", _counting_read)
        monkeypatch.setattr(session_service_mod, "list_terminals_by_session", _boom)
        monkeypatch.setattr(
            session_service_mod,
            "get_backend",
            lambda: MagicMock(
                list_sessions=lambda: [
                    {"id": f"cao-session{n}", "name": f"Session {n}"} for n in range(session_count)
                ]
            ),
        )

        result = list_sessions()

        assert len(result) == session_count
        # One read, whatever N is -- and no per-session query (``_boom``).
        assert len(reads) == 1
        # Each session gets ITS OWN terminal's metadata, not the first row of
        # the batch: the grouping has to key on tmux_session.
        assert [s["working_directory"] for s in result] == [
            f"/project/{n}" for n in range(session_count)
        ]

    def test_terminal_read_is_bounded_to_the_live_sessions(self, monkeypatch):
        """The read must ask only for sessions tmux actually reports.

        Rows for sessions tmux no longer reports are only swept by
        ``cleanup_service.cleanup_old_data``, which runs at server startup, so a
        long-uptime server accumulates them indefinitely. Reading the whole
        table would make this path scale with that accumulation instead of with
        the sessions being listed -- which measured *slower* than the
        per-session version it replaced once enough rows had piled up.
        """
        requested: list = []

        def _capturing_read(names):
            requested.append(sorted(names))
            return []

        monkeypatch.setattr(session_service_mod, "list_terminals_in_sessions", _capturing_read)
        monkeypatch.setattr(
            session_service_mod,
            "get_backend",
            lambda: MagicMock(
                list_sessions=lambda: [
                    {"id": "cao-alpha", "name": "cao-alpha"},
                    {"id": "cao-beta", "name": "cao-beta"},
                    {"id": "other-not-ours", "name": "other-not-ours"},
                ]
            ),
        )

        list_sessions()

        # Only the live CAO sessions -- not the foreign session, and crucially
        # not an unbounded "everything in the table" read.
        assert requested == [["cao-alpha", "cao-beta"]]

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_in_sessions")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_groups_bulk_read_by_session(
        self, mock_get_backend, mock_list_in_sessions
    ):
        """A flat terminal read is bucketed per session, ignoring foreign rows."""
        mock_get_backend.return_value.list_sessions.return_value = [
            {"id": "cao-alpha", "name": "cao-alpha"},
            {"id": "cao-beta", "name": "cao-beta"},
        ]
        mock_list_in_sessions.return_value = [
            # Interleaved, and including a row for a session tmux no longer
            # reports plus one with no tmux_session at all.
            {
                "id": "t-beta",
                "tmux_session": "cao-beta",
                "tmux_window": "w",
                "agent_profile": "reviewer",
                "working_directory": "/beta",
            },
            {
                "id": "t-orphan",
                "tmux_session": "cao-gone",
                "tmux_window": "w",
                "agent_profile": "ghost",
                "working_directory": "/gone",
            },
            {
                "id": "t-alpha",
                "tmux_session": "cao-alpha",
                "tmux_window": "w",
                "agent_profile": "developer",
                "working_directory": "/alpha",
            },
            {
                "id": "t-nosession",
                "tmux_session": None,
                "tmux_window": "w",
                "agent_profile": "nobody",
                "working_directory": "/nowhere",
            },
        ]

        result = list_sessions()

        by_id = {s["id"]: s for s in result}
        assert by_id["cao-alpha"]["agent_profile"] == "developer"
        assert by_id["cao-alpha"]["working_directory"] == "/alpha"
        assert by_id["cao-beta"]["agent_profile"] == "reviewer"
        assert by_id["cao-beta"]["working_directory"] == "/beta"
        assert "cao-gone" not in by_id

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_in_sessions")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_survives_a_failed_bulk_terminal_read(
        self, mock_get_backend, mock_list_in_sessions, caplog
    ):
        """A DB failure degrades ownership fields, it does not blank the list.

        Collapsing N queries into one concentrates the failure, so the swallow
        matters more than it did: losing the metadata read must still return
        every session rather than an empty list.
        """
        mock_get_backend.return_value.list_sessions.return_value = [
            {"id": "cao-one", "name": "cao-one"},
            {"id": "cao-two", "name": "cao-two"},
        ]
        mock_list_in_sessions.side_effect = RuntimeError("database is locked")

        with caplog.at_level(logging.WARNING):
            result = list_sessions()

        assert [s["id"] for s in result] == ["cao-one", "cao-two"]
        assert all(s["agent_profile"] is None for s in result)
        assert all(s["working_directory"] is None for s in result)
        # The traceback must survive the swallow, or a DB problem here is
        # undiagnosable without reproducing locally.
        assert "database is locked" in caplog.text
        assert _swallowed_log(caplog, "Failed to load terminal metadata").exc_info

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_in_sessions")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_unresolvable_pane_directory_logs_a_traceback(
        self, mock_get_backend, mock_list_in_sessions, caplog
    ):
        """The pane-cwd fallback's swallowed failure also keeps its traceback."""
        fake_client = self._FakeTmuxClient(
            [{"id": "cao-owned", "name": "cao-owned"}],
            {("cao-owned", "developer-abcd"): OSError("pane vanished")},
        )
        mock_get_backend.return_value = TmuxBackend(client=fake_client)
        mock_list_in_sessions.return_value = [
            {
                "id": "term1",
                "tmux_session": "cao-owned",
                "tmux_window": "developer-abcd",
                "agent_profile": "developer",
                "working_directory": None,
            }
        ]

        with caplog.at_level(logging.WARNING):
            result = list_sessions()

        assert result[0]["working_directory"] is None
        assert result[0]["agent_profile"] == "developer"
        assert "pane vanished" in caplog.text
        assert _swallowed_log(caplog, "Failed to resolve working directory").exc_info

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_whole_listing_failure_logs_a_traceback(self, mock_get_backend, caplog):
        """The outer handler keeps its traceback too — it blanks the ENTIRE response.

        The two tests above cover the WARNING-level handlers, which degrade one
        session's metadata. This one covers the ERROR-level net around the whole
        function: callers get `[]` and cannot tell "no sessions" from "the
        backend failed", so the traceback in the log is the only evidence the
        failure happened at all.
        """
        mock_get_backend.return_value.list_sessions.side_effect = RuntimeError("tmux is gone")

        with caplog.at_level(logging.ERROR):
            result = list_sessions()

        assert result == []
        assert "tmux is gone" in caplog.text
        assert _swallowed_log(caplog, "Failed to list sessions").exc_info


@pytest.fixture
def real_session_db(tmp_path, monkeypatch):
    """Route terminal metadata to a per-test real SQLite database."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'session-ownership.db'}",
        connect_args={"check_same_thread": False},
    )
    db_mod.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(
        db_mod,
        "SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def real_tmux_backend(tmp_path, monkeypatch):
    """Use a real tmux backend while keeping FIFO files in pytest's temp area."""
    fifo_dir = Path(os.path.realpath(tmp_path / "fifos"))
    fifo_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(terminal_service, "FIFO_DIR", fifo_dir)
    monkeypatch.setattr(fifo_reader_mod, "FIFO_DIR", fifo_dir)

    backend = TmuxBackend()
    monkeypatch.setattr(backend_registry, "_backend", backend)
    return backend


@pytest_asyncio.fixture
async def running_status_monitor():
    """Run the in-process status monitor used by mock_cli initialization."""
    loop = asyncio.get_running_loop()
    bus.set_loop(loop)
    task = asyncio.create_task(status_monitor.run())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        bus.set_loop(None)


def _session_suffix() -> str:
    return f"ownership-{uuid.uuid4().hex[:8]}"


async def _wait_for_pane_directory(backend, session_name: str, window_name: str, expected: str):
    deadline = asyncio.get_running_loop().time() + 8
    while asyncio.get_running_loop().time() < deadline:
        if backend.get_pane_working_directory(session_name, window_name) == expected:
            return
        await asyncio.sleep(0.2)
    assert backend.get_pane_working_directory(session_name, window_name) == expected


@pytest.mark.integration
class TestSessionOwnershipIntegration:
    """Regression tests for list_sessions ownership metadata persistence."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "working_directory",
        [None, "."],
        ids=["omitted-working-directory", "relative-dot-working-directory"],
    )
    async def test_create_terminal_persists_effective_cwd_and_list_sessions_does_not_drift(
        self,
        working_directory,
        tmp_path,
        monkeypatch,
        real_session_db,
        real_tmux_backend,
        running_status_monitor,
    ):
        project = tmp_path / "project"
        drift = tmp_path / "drift"
        project.mkdir()
        drift.mkdir()
        monkeypatch.chdir(project)
        expected = os.path.realpath(project)
        session_name = _session_suffix()
        terminal = None

        try:
            terminal = await terminal_service.create_terminal(
                provider="mock_cli",
                agent_profile="developer",
                session_name=session_name,
                new_session=True,
                working_directory=working_directory,
            )

            metadata = get_terminal_metadata(terminal.id)
            assert metadata is not None
            assert metadata["working_directory"] == expected

            real_tmux_backend.send_keys(terminal.session_name, terminal.name, "/exit")
            await asyncio.sleep(0.5)
            real_tmux_backend.send_keys(
                terminal.session_name,
                terminal.name,
                f"cd {shlex.quote(str(drift))}",
            )
            await _wait_for_pane_directory(
                real_tmux_backend,
                terminal.session_name,
                terminal.name,
                os.path.realpath(drift),
            )

            sessions = {s["id"]: s for s in list_sessions()}
            assert sessions[terminal.session_name]["working_directory"] == expected
            assert sessions[terminal.session_name]["agent_profile"] == "developer"
        finally:
            if terminal is not None:
                with contextlib.suppress(Exception):
                    delete_session(terminal.session_name)

    @pytest.mark.asyncio
    async def test_same_name_relaunch_purges_stale_terminal_metadata(
        self,
        tmp_path,
        real_session_db,
        real_tmux_backend,
        running_status_monitor,
    ):
        old_project = tmp_path / "old-project"
        new_project = tmp_path / "new-project"
        old_project.mkdir()
        new_project.mkdir()
        session_name = _session_suffix()
        live_session_name = f"cao-{session_name}"

        first = await create_session(
            provider="mock_cli",
            agent_profile="developer",
            session_name=session_name,
            working_directory=str(old_project),
        )
        real_tmux_backend.kill_session(first.session_name)
        terminal_service.fifo_manager.stop_reader(first.id)
        terminal_service.status_monitor.clear_terminal(first.id)
        terminal_service.provider_manager.cleanup_provider(first.id)

        second = None
        try:
            second = await create_session(
                provider="mock_cli",
                agent_profile="reviewer",
                session_name=session_name,
                working_directory=str(new_project),
            )

            sessions = {s["id"]: s for s in list_sessions()}
            assert sessions[live_session_name]["working_directory"] == os.path.realpath(new_project)
            assert sessions[live_session_name]["agent_profile"] == "reviewer"
        finally:
            if second is not None:
                with contextlib.suppress(Exception):
                    delete_session(second.session_name)


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
    def test_delete_session_reports_deferred_terminal_cleanup(
        self,
        mock_get_backend,
        mock_list_terminals,
        mock_capture,
        mock_dismantle,
        mock_delete_row,
        mock_delete_terminals_by_ids,
    ):
        """An explicit retryable teardown result must not be reported deleted.

        A deferred runtime teardown (Grok has not released its private home yet,
        #596) keeps the terminal's registry row: the row is the only retry handle,
        so neither the per-terminal delete nor the by-id sweep may drop it, and
        the session is reported in ``errors`` rather than ``deleted``. The tmux
        session itself is still killed — the deferral is about on-disk provider
        state, not the session.
        """
        mock_get_backend.return_value.session_exists_strict.return_value = True
        mock_get_backend.return_value.kill_session.return_value = True
        mock_list_terminals.return_value = [{"id": "grok-terminal"}]
        mock_dismantle.return_value = False

        result = delete_session("cao-grok")

        assert result["deleted"] == []
        assert result["errors"] == [
            {"terminal_id": "grok-terminal", "error": "cleanup deferred; retry delete_session"}
        ]
        mock_get_backend.return_value.kill_session.assert_called_once_with("cao-grok")
        # The retry handle survives both row-deletion paths.
        mock_delete_row.assert_not_called()
        mock_delete_terminals_by_ids.assert_called_once_with([])

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


def test_list_sessions_reports_the_creator_as_owner(real_session_db, monkeypatch):
    """End-to-end guard for #629's regression, at the layer users actually see.

    The regression that prompted this test was caught by review, not by CI: the
    unit test covering the batched read was GREEN while asserting the wrong
    contract, because it pinned ``ORDER BY id`` — the very thing that broke
    ownership. A test one layer up, on ``list_sessions()`` itself, would have
    failed instead of endorsing it.

    The ids matter: the creator's uuid4 prefix sorts ABOVE its worker's, so
    ordering by ``id`` reports the worker's profile and worktree as the
    session's, while creation order reports the creator's.
    """
    session_name = "cao-owner"
    db_mod.create_terminal(
        terminal_id="f0000000",
        tmux_session=session_name,
        tmux_window="supervisor-aaaa",
        provider="kiro_cli",
        agent_profile="supervisor",
        working_directory="/repo/owner",
    )
    db_mod.create_terminal(
        terminal_id="10000000",
        tmux_session=session_name,
        tmux_window="developer-bbbb",
        provider="kiro_cli",
        agent_profile="developer",
        working_directory="/tmp/child-worktree",
    )

    backend = MagicMock()
    backend.list_sessions.return_value = [{"id": session_name, "name": session_name}]
    backend.get_pane_working_directory.side_effect = AssertionError(
        "working_directory is persisted; the pane fallback must not be reached"
    )
    monkeypatch.setattr(session_service_mod, "get_backend", lambda: backend)

    reported = list_sessions()[0]

    assert reported["agent_profile"] == "supervisor"
    assert reported["working_directory"] == "/repo/owner"
