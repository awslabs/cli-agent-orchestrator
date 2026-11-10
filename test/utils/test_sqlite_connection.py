"""Tests for ``utils.sqlite_connection.closing_transaction`` and the paths that use it.

``with sqlite3.connect(...) as conn:`` commits but never closes, and the connection is
in a reference cycle with its own statement cache, so only a full cyclic GC releases its
file descriptor. These tests run with the GC disabled, so a connection that is not
closed explicitly shows up as an open descriptor instead of being hidden by a collection.
"""

import gc
import os
import sqlite3
import sys
import warnings

import pytest

from cli_agent_orchestrator.utils.sqlite_connection import closing_transaction

pytestmark = [
    pytest.mark.skipif(
        not os.path.isdir("/dev/fd"), reason="counts open descriptors through /dev/fd"
    ),
    # Python 3.13 warns when it finalizes an unclosed sqlite3 connection.
    pytest.mark.filterwarnings("error::ResourceWarning"),
]


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


@pytest.fixture
def gc_disabled():
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


@pytest.fixture
def private_db(tmp_path, monkeypatch):
    """Point every module that reads ``DATABASE_FILE`` at one temporary database."""
    from cli_agent_orchestrator.services import approval_store

    path = tmp_path / "cao.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path, raising=True)
    monkeypatch.setattr(approval_store, "DATABASE_FILE", path, raising=True)
    return path


class TestClosingTransaction:
    def test_commits_on_a_normal_exit_and_closes(self, tmp_path):
        path = tmp_path / "t.db"
        with closing_transaction(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE t (v INTEGER)")
            conn.execute("INSERT INTO t VALUES (1)")
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
        with closing_transaction(sqlite3.connect(path)) as check:
            assert check.execute("SELECT v FROM t").fetchall() == [(1,)]

    def test_rolls_back_on_an_exception_closes_and_reraises(self, tmp_path):
        path = tmp_path / "t.db"
        with closing_transaction(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE t (v INTEGER)")
        with pytest.raises(RuntimeError, match="boom"):
            with closing_transaction(sqlite3.connect(path)) as conn:
                conn.execute("INSERT INTO t VALUES (1)")
                raise RuntimeError("boom")
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
        with closing_transaction(sqlite3.connect(path)) as check:
            assert check.execute("SELECT COUNT(*) FROM t").fetchone() == (0,)

    def test_leaves_the_connection_settings_as_the_caller_set_them(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 1234")
        with closing_transaction(conn) as same:
            assert same is conn
            assert same.isolation_level is None
            assert same.execute("PRAGMA busy_timeout").fetchone() == (1234,)

    def test_a_with_block_alone_keeps_the_descriptor_open(self, tmp_path, gc_disabled):
        """The behaviour the helper exists for, so the fd checks below can be trusted."""
        path = tmp_path / "t.db"
        sqlite3.connect(path).close()
        before = _open_fds()
        for _ in range(20):
            with sqlite3.connect(path) as conn:
                conn.execute("SELECT 1")
        assert _open_fds() - before == 20

    def test_the_helper_does_not_grow_the_descriptor_count(self, tmp_path, gc_disabled):
        path = tmp_path / "t.db"
        sqlite3.connect(path).close()
        before = _open_fds()
        for _ in range(200):
            with closing_transaction(sqlite3.connect(path)) as conn:
                conn.execute("SELECT 1")
        assert _open_fds() == before


def _exercise_db_paths(i: int, scan_dir: str) -> None:
    """One pass over the journal, event log, approval store, spec index and migrators."""
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.services import (
        approval_store,
        workflow_journal,
        workflow_spec_service,
    )

    database._migrate_workflow_run()
    database._migrate_workflow_run_step()
    database._migrate_workflow_index()
    database._migrate_decision_records_match()
    run_id = f"run-{i}"
    ts = "2026-01-01T00:00:00Z"
    workflow_journal.insert_run(run_id, "wf", "{}", "{}", "running", ts)
    workflow_journal.begin_step(run_id, "s1", ts, "fp")
    workflow_journal.settle_step(run_id, "s1", "completed", ts, None, None, None)
    workflow_journal.append_event(run_id, 1, "run.started", event_schema_version=1, ts=ts)
    workflow_journal.read_events(run_id)
    workflow_journal.get_run(run_id)
    workflow_journal.get_steps(run_id)
    workflow_journal.list_runs()
    workflow_journal.update_run_state(run_id, "completed", ts)
    workflow_journal.delete_run(run_id)
    approval_store.grant(f"plan-{i}", "test")
    approval_store.is_approved(f"plan-{i}")
    workflow_spec_service.list_workflows(scan_dir=scan_dir)


class TestTheDatabasePathsClose:
    def test_journal_approval_and_index_paths_do_not_grow_the_descriptor_count(
        self, private_db, tmp_path, gc_disabled
    ):
        scan_dir = str(tmp_path / "workflows")
        os.mkdir(scan_dir)
        # Counted from before the first pass, so the first-call-only paths (the
        # migrations each factory runs once per database path) are covered too.
        before = _open_fds()
        for i in range(51):
            _exercise_db_paths(i, scan_dir)
        assert _open_fds() == before

    @pytest.mark.skipif(
        sys.version_info < (3, 13), reason="sqlite3 warns about unclosed connections from 3.13"
    )
    def test_no_connection_is_left_for_the_gc_to_close(self, private_db, tmp_path):
        """Python 3.13 issues ResourceWarning when it finalizes an unclosed connection.

        The collection here is the detector, not a fix: it finalizes whatever the paths
        below left behind, so an unclosed connection becomes a recorded warning.
        """
        scan_dir = str(tmp_path / "workflows")
        os.mkdir(scan_dir)
        # Finalize what earlier tests left behind first, silently: only connections the
        # passes below open may count, including the first pass and its migrations.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            gc.collect()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            for i in range(6):
                _exercise_db_paths(i, scan_dir)
            gc.collect()
        unclosed = [w for w in caught if issubclass(w.category, ResourceWarning)]
        assert unclosed == [], [str(w.message) for w in unclosed]
