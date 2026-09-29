"""Tests for terminal durability: startup re-adoption + early snapshots.

create_terminal is the only place the FIFO -> EventBus logging pipeline is
armed, so a cao-server restart used to leave live tmux agents half-adopted
(pane alive, <tid>.log frozen, no status detection) and a crash left no
snapshot at all. These tests cover readopt_terminals_at_startup() and the
early-snapshot write.

The backend is installed through the public ``set_backend`` seam (the root
conftest restores the registry afterwards) and rows live in a real per-test
SQLite registry, so "the row was kept" and "the row was deleted" are observed
in the store itself rather than inferred from a mocked delete.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.backends.base import TerminalBackend
from cli_agent_orchestrator.backends.registry import set_backend
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.tmux import TmuxLookupError
from cli_agent_orchestrator.services import session_lock, terminal_service
from cli_agent_orchestrator.services.terminal_service import (
    _write_terminal_snapshot,
    readopt_terminals_at_startup,
)


@pytest.fixture
def registry_db(isolated_memory_db):
    """A real per-test SQLite registry (the fixture creates every table)."""
    return isolated_memory_db


@pytest.fixture
def backend():
    """A tmux-shaped backend: live sessions, readable windows, no event inbox."""
    mock = MagicMock(spec=TerminalBackend)
    mock.supports_event_inbox.return_value = False
    mock.session_exists.return_value = True
    mock.session_exists_strict.return_value = True
    mock.get_history.return_value = "$ "
    set_backend(mock)
    return mock


@pytest.fixture
def fifo(monkeypatch):
    """Record the FIFO reader registrations re-adoption makes."""
    recorder = MagicMock()
    monkeypatch.setattr(terminal_service, "fifo_manager", recorder)
    return recorder


def _seed(terminal_id="t1", session="cao-s", window="dev-1", provider="claude_code"):
    database.create_terminal(
        terminal_id=terminal_id,
        tmux_session=session,
        tmux_window=window,
        provider=provider,
        agent_profile="developer",
    )


def _row_ids():
    return sorted(row["id"] for row in database.list_all_terminals())


class TestReadoptTerminalsAtStartup:
    def test_rearms_live_terminal(self, registry_db, backend, fifo):
        _seed()

        counts = readopt_terminals_at_startup(database.list_all_terminals())

        assert counts == {"readopted": 1, "finalized": 0, "skipped": 0}
        fifo.create_reader.assert_called_once()
        assert fifo.create_reader.call_args[0][0] == "t1"
        # stop-then-start: a stalled pane still reports pane_pipe=1, so a bare
        # pipe_pane() toggle would switch the dead pipe OFF.
        backend.stop_pipe_pane.assert_called_once_with("cao-s", "dev-1")
        backend.pipe_pane.assert_called_once()
        # Post-pipe repaint nudge: without it the fresh rolling buffer stays
        # empty and the re-adopted terminal reads UNKNOWN until it speaks.
        backend.send_special_key.assert_called_once_with("cao-s", "dev-1", "Enter")
        assert _row_ids() == ["t1"]

    def test_finalizes_dead_terminal_with_scrollback_from_log(
        self, registry_db, backend, fifo, tmp_path
    ):
        backend.session_exists.return_value = False
        backend.session_exists_strict.return_value = False
        _seed("dead1")
        (tmp_path / "dead1.log").write_text("hello \x1b[31mworld\x1b[0m\n", encoding="utf-8")

        with patch.object(terminal_service, "TERMINAL_LOG_DIR", tmp_path):
            counts = readopt_terminals_at_startup(database.list_all_terminals())

        assert counts == {"readopted": 0, "finalized": 1, "skipped": 0}
        scrollback = (tmp_path / "dead1.scrollback").read_text(encoding="utf-8")
        assert "hello" in scrollback and "world" in scrollback
        assert "\x1b" not in scrollback  # ANSI-stripped
        assert _row_ids() == []
        fifo.create_reader.assert_not_called()

    def test_existing_scrollback_not_overwritten(self, registry_db, backend, fifo, tmp_path):
        """A clean-delete scrollback (full pane capture) is better than the
        log-derived one — finalization must not clobber it."""
        backend.session_exists.return_value = False
        backend.session_exists_strict.return_value = False
        _seed("dead2")
        (tmp_path / "dead2.log").write_text("from-log", encoding="utf-8")
        (tmp_path / "dead2.scrollback").write_text("from-clean-delete", encoding="utf-8")

        with patch.object(terminal_service, "TERMINAL_LOG_DIR", tmp_path):
            counts = readopt_terminals_at_startup(database.list_all_terminals())

        assert counts == {"readopted": 0, "finalized": 1, "skipped": 0}
        assert (tmp_path / "dead2.scrollback").read_text() == "from-clean-delete"

    def test_event_inbox_backend_is_noop(self, registry_db, backend, fifo):
        backend.supports_event_inbox.return_value = True
        _seed()

        counts = readopt_terminals_at_startup(database.list_all_terminals())

        assert counts == {"readopted": 0, "finalized": 0, "skipped": 0}
        backend.session_exists_strict.assert_not_called()
        backend.session_exists.assert_not_called()
        assert _row_ids() == ["t1"]

    def test_empty_snapshot_never_touches_the_backend(self, backend, fifo):
        counts = readopt_terminals_at_startup([])

        assert counts == {"readopted": 0, "finalized": 0, "skipped": 0}
        assert backend.mock_calls == []


class TestReadoptOnlyFinalizesConfirmedAbsence:
    """Deleting a row is teardown confirmation (#498): only a CONFIRMED absence
    may finalize. "Could not tell" must leave the row exactly as it was — the
    next restart re-evaluates it, and retention cleanup still collects rows that
    really are dead, whereas a wrongly deleted row orphans a live agent for good.
    """

    def test_transient_probe_failure_on_a_live_session_keeps_the_row(
        self, registry_db, backend, fifo
    ):
        backend.get_history.side_effect = TmuxLookupError("list-panes did not parse")
        _seed()

        counts = readopt_terminals_at_startup(database.list_all_terminals())

        assert counts == {"readopted": 0, "finalized": 0, "skipped": 1}
        assert _row_ids() == ["t1"]
        fifo.create_reader.assert_not_called()

    def test_probe_timeout_on_a_live_session_keeps_the_row(self, registry_db, backend, fifo):
        backend.get_history.side_effect = TimeoutError("capture-pane timed out")
        _seed()

        counts = readopt_terminals_at_startup(database.list_all_terminals())

        assert counts == {"readopted": 0, "finalized": 0, "skipped": 1}
        assert _row_ids() == ["t1"]

    def test_unreadable_window_in_a_live_session_keeps_the_row(self, registry_db, backend, fifo):
        """No strict window-level check exists, and a window lookup rides on
        libtmux listings; a renamed or momentarily unlistable window reads as
        "not found" without being gone."""
        backend.get_history.side_effect = ValueError("Window 'dev-1' not found in session")
        _seed()

        counts = readopt_terminals_at_startup(database.list_all_terminals())

        assert counts == {"readopted": 0, "finalized": 0, "skipped": 1}
        assert _row_ids() == ["t1"]

    def test_undecidable_session_liveness_keeps_the_row(self, registry_db, backend, fifo):
        backend.session_exists_strict.side_effect = TmuxLookupError("socket unreadable")
        _seed()

        counts = readopt_terminals_at_startup(database.list_all_terminals())

        assert counts == {"readopted": 0, "finalized": 0, "skipped": 1}
        assert _row_ids() == ["t1"]
        fifo.create_reader.assert_not_called()

    def test_lenient_session_lookup_is_never_consulted(self, registry_db, backend, fifo):
        """``session_exists`` collapses a lookup error into False — exactly the
        shape that deleted live rows. It must not be what decides."""
        backend.session_exists.return_value = False
        _seed()

        counts = readopt_terminals_at_startup(database.list_all_terminals())

        assert counts == {"readopted": 1, "finalized": 0, "skipped": 0}
        backend.session_exists.assert_not_called()
        assert _row_ids() == ["t1"]

    def test_mixed_rows_are_decided_one_by_one(self, registry_db, backend, fifo):
        liveness = {"cao-alive": True, "cao-dead": False}

        def strict(session_name):
            if session_name not in liveness:
                raise TmuxLookupError("socket unreadable")
            return liveness[session_name]

        backend.session_exists_strict.side_effect = strict
        _seed("t-alive", session="cao-alive")
        _seed("t-dead", session="cao-dead")
        _seed("t-unknown", session="cao-unknown")

        counts = readopt_terminals_at_startup(database.list_all_terminals())

        assert counts == {"readopted": 1, "finalized": 1, "skipped": 1}
        assert _row_ids() == ["t-alive", "t-unknown"]
        assert [c.args[0] for c in fifo.create_reader.call_args_list] == ["t-alive"]

    def test_each_row_is_decided_under_its_session_lifecycle_lock(self, registry_db, backend, fifo):
        """Session teardown holds this lock across kill-confirm + sweep (#498),
        so re-arming or finalizing a row cannot interleave with it."""
        held = []

        def strict(session_name):
            with session_lock._registry_guard:
                entry = session_lock._session_locks.get(session_name)
            held.append(entry is not None and entry[0].locked())
            return True

        backend.session_exists_strict.side_effect = strict
        _seed()

        readopt_terminals_at_startup(database.list_all_terminals())

        assert held == [True]


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
        with patch.object(terminal_service, "TERMINAL_LOG_DIR", tmp_path / "missing" / "nested"):
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
