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

import contextlib
import json
import stat
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.backends.base import TerminalBackend
from cli_agent_orchestrator.backends.registry import set_backend
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.tmux import TmuxLookupError
from cli_agent_orchestrator.constants import PIPE_LIVENESS_TAIL_LINES
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.services import fifo_reader, session_lock
from cli_agent_orchestrator.services import status_monitor as status_monitor_module
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.services.fifo_reader import FifoManager
from cli_agent_orchestrator.services.status_monitor import StatusMonitor
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
    # tmux has no native status; providers then classify the pane text.
    mock.get_native_status.return_value = None
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
        fifo_path = str(terminal_service.FIFO_DIR / "t1.fifo")
        fifo.create_reader.assert_called_once()
        assert fifo.create_reader.call_args.args == ("t1",)
        assert set(fifo.create_reader.call_args.kwargs) == {"pane_probe", "rearm"}
        # stop-then-start: a stalled pane still reports pane_pipe=1, so a bare
        # pipe_pane() toggle would switch the dead pipe OFF.
        pipe_calls = [c for c in backend.mock_calls if c[0] in ("stop_pipe_pane", "pipe_pane")]
        assert pipe_calls == [
            ("stop_pipe_pane", ("cao-s", "dev-1"), {}),
            ("pipe_pane", ("cao-s", "dev-1", fifo_path), {}),
        ]
        assert _row_ids() == ["t1"]

    def test_watchdog_callbacks_address_the_readopted_pane(self, registry_db, backend, fifo):
        _seed()
        readopt_terminals_at_startup(database.list_all_terminals())
        callbacks = fifo.create_reader.call_args.kwargs
        backend.reset_mock()

        callbacks["pane_probe"]()
        callbacks["rearm"]()

        fifo_path = str(terminal_service.FIFO_DIR / "t1.fifo")
        assert backend.mock_calls == [
            ("get_history", ("cao-s", "dev-1"), {"tail_lines": PIPE_LIVENESS_TAIL_LINES}),
            ("stop_pipe_pane", ("cao-s", "dev-1"), {}),
            ("pipe_pane", ("cao-s", "dev-1", fifo_path), {}),
        ]

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


class TestReadoptFinalizesThroughTeardown:
    """A finalized row is a terminal teardown whose session is already confirmed
    gone -- the same position session teardown is in after its kill-confirm
    (#498) -- so it runs the same halves: runtime dismantle, then the row."""

    def test_finalize_cleans_up_the_provider_and_the_stale_fifo(
        self, registry_db, backend, fifo, monkeypatch
    ):
        """Grok, MiniMax and Kimi Code keep private homes (Kimi's holds a copy
        of the operator's credentials) that ``cleanup_provider`` rebuilds from
        the row after a restart. Dropping the row first loses that route."""
        cleaned = []
        monkeypatch.setattr(
            terminal_service.provider_manager,
            "cleanup_provider",
            lambda terminal_id: cleaned.append(terminal_id) or True,
        )
        backend.session_exists_strict.return_value = False
        _seed("dead1", provider="grok_cli")

        counts = readopt_terminals_at_startup(database.list_all_terminals())

        assert counts == {"readopted": 0, "finalized": 1, "skipped": 0}
        assert cleaned == ["dead1"]
        # stop_reader also unlinks the previous server's <tid>.fifo; once the
        # row is gone, retention cleanup would never reach it.
        fifo.stop_reader.assert_called_once_with("dead1")
        assert _row_ids() == []
        # The session is confirmed gone: no tmux-facing teardown step runs.
        backend.kill_window.assert_not_called()
        backend.stop_pipe_pane.assert_not_called()

    def test_failed_scrollback_recovery_does_not_keep_a_dead_row(
        self, registry_db, backend, fifo, tmp_path
    ):
        """The recovery is a convenience: <tid>.log stays on disk regardless."""
        backend.session_exists_strict.return_value = False
        _seed("dead1")
        (tmp_path / "dead1.log").mkdir()  # unreadable as a file

        with patch.object(terminal_service, "TERMINAL_LOG_DIR", tmp_path):
            counts = readopt_terminals_at_startup(database.list_all_terminals())

        assert counts == {"readopted": 0, "finalized": 1, "skipped": 0}
        assert _row_ids() == []

    def test_deferred_provider_cleanup_keeps_the_row_for_a_retry(
        self, registry_db, backend, fifo, monkeypatch
    ):
        monkeypatch.setattr(
            terminal_service.provider_manager, "cleanup_provider", lambda terminal_id: False
        )
        backend.session_exists_strict.return_value = False
        _seed("dead1", provider="grok_cli")

        counts = readopt_terminals_at_startup(database.list_all_terminals())

        assert counts == {"readopted": 0, "finalized": 0, "skipped": 1}
        assert _row_ids() == ["dead1"]


class TestReadoptPerRowFailures:
    """A row that fails must not take the rest of the pass with it, must be
    counted as left untouched, and must not leave half-armed state behind."""

    @pytest.mark.parametrize("failing_step", ["stop_pipe_pane", "pipe_pane"])
    def test_reader_is_unregistered_when_arming_the_pipe_fails(
        self, registry_db, backend, fifo, failing_step
    ):
        """The reader is created first so it catches the pane from its first
        byte; if the pipe then fails to attach, nothing will ever write to it."""
        getattr(backend, failing_step).side_effect = RuntimeError("tmux went away")
        _seed()

        counts = readopt_terminals_at_startup(database.list_all_terminals())

        assert counts == {"readopted": 0, "finalized": 0, "skipped": 1}
        fifo.create_reader.assert_called_once()
        fifo.stop_reader.assert_called_once_with("t1")
        assert _row_ids() == ["t1"]

    def test_a_failing_live_row_does_not_stop_the_pass(self, registry_db, backend, fifo):
        def create_reader(terminal_id, **_callbacks):
            if terminal_id == "t-bad":
                raise OSError("mkfifo failed")

        fifo.create_reader.side_effect = create_reader
        _seed("t-bad", session="cao-a")
        _seed("t-good", session="cao-b")

        counts = readopt_terminals_at_startup(database.list_all_terminals())

        assert counts == {"readopted": 1, "finalized": 0, "skipped": 1}
        assert _row_ids() == ["t-bad", "t-good"]

    def test_a_failing_dead_row_does_not_stop_the_pass(
        self, registry_db, backend, fifo, monkeypatch
    ):
        backend.session_exists_strict.return_value = False
        real_delete = terminal_service.db_delete_terminal

        def delete(terminal_id):
            if terminal_id == "t-bad":
                raise RuntimeError("database is locked")
            return real_delete(terminal_id)

        monkeypatch.setattr(terminal_service, "db_delete_terminal", delete)
        _seed("t-bad", session="cao-a")
        _seed("t-good", session="cao-b")

        counts = readopt_terminals_at_startup(database.list_all_terminals())

        assert counts == {"readopted": 0, "finalized": 1, "skipped": 1}
        assert _row_ids() == ["t-bad"]


class TestReadoptNeverTypesIntoThePane:
    """A re-adopted pane holds a live agent in an arbitrary state, and a key is a
    submit: Enter on a permission prompt with "Yes" highlighted runs the command.
    Re-adoption therefore sends no keystroke at all. The status pipeline is
    seeded from the pane's own snapshot instead, by the pipe-liveness
    watchdog's cold-start replay.
    """

    _FIXTURE = Path(__file__).parent.parent / "providers" / "fixtures" / "codex_approval_modal.txt"

    def _waiting_pane(self) -> str:
        pane = self._FIXTURE.read_text(encoding="utf-8")
        # Pin the premise: CAO itself classifies this pane as waiting on a human.
        assert CodexProvider("t1", "cao-s", "dev-1").get_status(pane) == (
            TerminalStatus.WAITING_USER_ANSWER
        )
        return pane

    def test_pane_waiting_for_an_answer_receives_no_keystroke(self, registry_db, backend, fifo):
        backend.get_history.return_value = self._waiting_pane()
        _seed(provider="codex")

        counts = readopt_terminals_at_startup(database.list_all_terminals())

        assert counts == {"readopted": 1, "finalized": 0, "skipped": 0}
        backend.send_special_key.assert_not_called()
        backend.send_keys.assert_not_called()

    def test_waiting_pane_status_is_seeded_from_its_own_snapshot(
        self, registry_db, backend, fifo, tmp_path, monkeypatch
    ):
        backend.get_history.return_value = self._waiting_pane()
        _seed(provider="codex")
        readopt_terminals_at_startup(database.list_all_terminals())
        callbacks = fifo.create_reader.call_args.kwargs

        # Hand the re-adoption's callbacks to a real FIFO manager, exactly as the
        # singleton received them. A pane parked on a prompt prints nothing, so
        # its fresh FIFO never delivers a byte: the watchdog's cold-start case,
        # which re-arms the pipe and replays the pane's CURRENT content.
        monkeypatch.setattr(fifo_reader, "FIFO_DIR", tmp_path)
        monkeypatch.setattr(fifo_reader, "PIPE_LIVENESS_COLD_START_GRACE_S", 0.0)
        published: list = []
        monkeypatch.setattr(bus, "publish", lambda topic, data: published.append((topic, data)))
        manager = FifoManager()
        try:
            manager.create_reader("t1", **callbacks)
            manager._check_pipe_liveness("t1")
        finally:
            manager.stop_reader("t1")
            manager.stop_watchdog()

        replays = [data["data"] for topic, data in published if topic == "terminal.t1.output"]
        assert len(replays) == 1

        # That replay is what the StatusMonitor consumes from the bus.
        monitor = StatusMonitor()
        provider = CodexProvider("t1", "cao-s", "dev-1")
        with patch.object(status_monitor_module.provider_manager, "get_provider") as get_provider:
            get_provider.return_value = provider
            monitor._process_chunk("t1", replays[0])
            assert monitor.get_status("t1") == TerminalStatus.WAITING_USER_ANSWER

        backend.send_special_key.assert_not_called()
        backend.send_keys.assert_not_called()


_SNAPSHOT_FIELDS = dict(
    session_name="cao-s",
    window_name="dev-9",
    agent_profile="developer",
    provider="claude_code",
    working_directory="/repo",
    allowed_tools=["fs_read"],
    caller_id=None,
)


class TestEarlySnapshot:
    def test_write_terminal_snapshot(self, tmp_path):
        with patch.object(terminal_service, "TERMINAL_LOG_DIR", tmp_path):
            _write_terminal_snapshot("t9", **_SNAPSHOT_FIELDS)

        snapshot = json.loads((tmp_path / "t9.snapshot.json").read_text())
        assert snapshot == {"terminal_id": "t9", **_SNAPSHOT_FIELDS}

    def test_snapshot_is_written_owner_only(self, tmp_path):
        """Defense in depth: the log dir is 0700 today, but the file should not
        rely on that alone."""
        with patch.object(terminal_service, "TERMINAL_LOG_DIR", tmp_path):
            _write_terminal_snapshot("t9", **_SNAPSHOT_FIELDS)

        assert stat.S_IMODE((tmp_path / "t9.snapshot.json").stat().st_mode) == 0o600

    def test_refreshing_a_snapshot_tightens_an_existing_file(self, tmp_path):
        """O_CREAT's mode only applies to new files; a snapshot written by an
        older release (or refreshed at delete) must end up 0600 too."""
        existing = tmp_path / "t9.snapshot.json"
        existing.write_text("{}")
        existing.chmod(0o644)

        with patch.object(terminal_service, "TERMINAL_LOG_DIR", tmp_path):
            _write_terminal_snapshot("t9", **_SNAPSHOT_FIELDS)

        assert stat.S_IMODE(existing.stat().st_mode) == 0o600
        assert json.loads(existing.read_text())["terminal_id"] == "t9"

    def test_write_terminal_snapshot_never_raises(self, tmp_path):
        """Best-effort contract: a bad log dir must not break the caller."""
        with patch.object(terminal_service, "TERMINAL_LOG_DIR", tmp_path / "missing" / "nested"):
            _write_terminal_snapshot("t10", **_SNAPSHOT_FIELDS)  # no exception

    def test_delete_path_refreshes_the_snapshot_through_the_same_writer(self, registry_db, backend):
        """One writer of the snapshot format: the delete-time capture refreshes
        it with the pane's live working directory via the same helper."""
        _seed("t1")
        backend.get_pane_working_directory.return_value = "/live/cwd"
        backend.get_history.return_value = "scrollback"

        with patch.object(terminal_service, "_write_terminal_snapshot") as writer:
            metadata = terminal_service.capture_terminal_snapshot("t1")

        assert metadata["live_working_directory"] == "/live/cwd"
        writer.assert_called_once_with(
            "t1",
            session_name="cao-s",
            window_name="dev-1",
            agent_profile="developer",
            provider="claude_code",
            working_directory="/live/cwd",
            allowed_tools=None,
            caller_id=None,
        )


_TS = "cli_agent_orchestrator.services.terminal_service."


@pytest.mark.asyncio
async def test_create_terminal_writes_the_early_snapshot_with_the_resolved_cwd():
    """Step 3d snapshots the EFFECTIVE launch directory (after worktree
    substitution and path resolution), not whatever the caller passed."""
    with contextlib.ExitStack() as stack:

        def p(name, **kwargs):
            return stack.enter_context(patch(_TS + name, **kwargs))

        for name in (
            "get_herdr_inbox_service",
            "db_create_terminal",
            "generate_session_name",
            "build_skill_catalog",
            "dispatch_plugin_event",
            "update_terminal_shell_command",
        ):
            p(name)
        p("generate_terminal_id", return_value="t-new")
        p("generate_window_name", return_value="developer-base")
        p("load_agent_profile", return_value=None)
        p("_resolve_working_directory", return_value="/resolved/launch/cwd")
        writer = p("_write_terminal_snapshot")
        backend = p("get_backend").return_value
        backend.session_exists.return_value = True
        backend.create_window.return_value = "developer-wxyz"
        backend.supports_event_inbox.return_value = True
        provider_manager = p("provider_manager")
        provider_manager.create_provider.return_value.initialize = AsyncMock(return_value=True)

        await terminal_service.create_terminal(
            provider="claude_code",
            agent_profile="developer",
            session_name="cao-s",
            working_directory="relative/dir",
        )

    writer.assert_called_once()
    assert writer.call_args.args == ("t-new",)
    assert writer.call_args.kwargs["working_directory"] == "/resolved/launch/cwd"
    assert writer.call_args.kwargs["session_name"] == "cao-s"
    assert writer.call_args.kwargs["window_name"] == "developer-wxyz"
