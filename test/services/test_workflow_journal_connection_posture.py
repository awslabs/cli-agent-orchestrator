"""Tests for ``workflow_journal._connect``'s connection posture (issue #583, NFR-4).

Unit ``journal-connection-posture``. One test per business rule, named so a failure
identifies the rule it broke:

- BR-1 ``test_connection_carries_busy_timeout`` / ``test_timeout_comes_from_the_constant``
- BR-2 ``test_every_connection_carries_the_pragma``
- ``test_stdlib_already_sets_the_same_timeout`` — a ninth test beyond the plan's table.
  CPython's ``sqlite3.connect`` already defaults ``busy_timeout`` to 5000 ms, so at
  this unit's chosen value the pragma writes a number the connection already had. The
  two BR-1/BR-2 tests above therefore run behind the
  ``stdlib_default_timeout_disabled`` fixture, without which they assert the standard
  library's default instead of this unit's behaviour. Read that fixture's docstring
  before changing either test.
- BR-3 ``test_migrators_run_once_per_path`` (also SR-2: the set holds only paths)
- BR-4/BR-5 ``test_new_path_migrates_mid_process`` — the reason the design keyed the
  guard on the PATH rather than on a boolean. It is the test that fails if someone
  later "simplifies" ``_MIGRATED_PATHS`` into a flag.
- BR-6 ``test_failed_migration_is_not_cached`` — see that test's own comment for
  exactly what it does and does not prove.
- BR-7 ``test_connect_signature_unchanged``
- BR-8/SR-4 ``test_journal_mode_unchanged_and_no_wal_sidecar``

Every test repoints ``DATABASE_FILE`` at a per-test temporary database with the
established repo idiom (``monkeypatch.setattr("cli_agent_orchestrator.constants.
DATABASE_FILE", ...)``, as in ``test/clients/test_workflow_run_migration.py``), so no
test touches the developer's real database. Because ``tmp_path`` is unique per test,
each test's path starts absent from the module-level ``_MIGRATED_PATHS`` set without
any test having to mutate that shared state.
"""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from cli_agent_orchestrator import constants
from cli_agent_orchestrator.clients import database as database_client
from cli_agent_orchestrator.services import workflow_journal
from cli_agent_orchestrator.services.workflow_journal import _MIGRATED_PATHS, _connect


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the journal at a fresh temp DB. The migrators are NOT run here.

    Deliberately left un-migrated: what these tests exercise is ``_connect``'s own
    migrate-once-per-path behaviour, so pre-migrating would hide it.
    """
    path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path, raising=True)
    assert str(path) not in _MIGRATED_PATHS, "fresh tmp_path must not be pre-cached"
    return path


@pytest.fixture
def stdlib_default_timeout_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the stdlib's OWN busy timeout to 0 so this unit's pragma is observable.

    THIS FIXTURE IS LOAD-BEARING. Do not delete it to "simplify" the two tests that
    use it.

    CPython's ``sqlite3.connect`` already applies a busy timeout of its own: its
    ``timeout`` parameter defaults to 5.0 seconds and is implemented with
    ``sqlite3_busy_timeout``, so a bare ``sqlite3.connect(path)`` reports
    ``PRAGMA busy_timeout == 5000`` before this unit executes any pragma at all —
    the SAME number the unit writes. A test that merely reads ``busy_timeout`` back
    off a connection therefore passes whether or not the pragma line exists: it is
    asserting the standard library's default, not this unit's behaviour. (Verified on
    this interpreter, and pinned by ``test_stdlib_already_sets_the_same_timeout``.)

    Patching ``sqlite3.connect`` to force ``timeout=0`` removes that default, so the
    only thing that can make the connection report 5000 is the unit's own
    ``PRAGMA busy_timeout`` statement. ``workflow_journal`` looks ``connect`` up on
    the shared ``sqlite3`` module at call time, so the patch reaches it (the migrators
    resolve the same module object; a 0 timeout is harmless to them — nothing in these
    tests contends for a lock).
    """
    real_connect = sqlite3.connect

    def _connect_without_stdlib_timeout(*args, **kwargs):
        kwargs["timeout"] = 0
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", _connect_without_stdlib_timeout, raising=True)


def _busy_timeout(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA busy_timeout").fetchone()[0]


def _table_names(path: Path) -> set[str]:
    with sqlite3.connect(str(path)) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# BR-1 — the busy timeout is 5000 ms and it comes from a named constant.
# ---------------------------------------------------------------------------
def test_connection_carries_busy_timeout(db_path: Path, stdlib_default_timeout_disabled: None):
    """A connection from ``_connect`` reports ``busy_timeout == 5000`` (BR-1).

    Runs with the stdlib's own 5000 ms default suppressed, so this asserts the unit's
    pragma rather than CPython's default — see the fixture's docstring.
    """
    conn = _connect()
    try:
        assert _busy_timeout(conn) == 5000
    finally:
        conn.close()


def test_stdlib_already_sets_the_same_timeout(db_path: Path):
    """Pins the fact that makes the fixture above necessary — and a gap in NFR-4.

    ``sqlite3.connect(path)`` with no ``timeout`` argument — exactly the call
    ``_connect`` makes — ALREADY reports ``busy_timeout == 5000`` before this unit's
    pragma runs, because CPython's ``timeout`` parameter defaults to 5.0 seconds.

    Consequence, recorded here rather than left for someone to rediscover: at the
    current value the unit's ``PRAGMA busy_timeout = 5000`` writes the number the
    connection already had, so it changes no runtime behaviour. It makes the timeout
    EXPLICIT and revisable from a named constant (which is what BR-1 asks for and what
    ``test_timeout_comes_from_the_constant`` proves), but it does not by itself widen
    the contention window NFR-4 is about. Widening that window needs a value LARGER
    than 5000 — a number this unit was not given the authority to choose.

    If a future change makes this test fail (a new CPython default), the unit's pragma
    starts doing real work and this test should be updated to say so, not deleted.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        assert _busy_timeout(conn) == 5000
    finally:
        conn.close()


def test_timeout_comes_from_the_constant(db_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The value is READ from the constant, not duplicated as a literal (BR-1, SR-1).

    Asserting ``_busy_timeout(conn) == WORKFLOW_JOURNAL_BUSY_TIMEOUT_MS`` alone would
    also pass against a hardcoded ``5000``, so this test moves the constant and
    requires the connection to follow it. That is the property BR-1 actually wants:
    ``nfr-requirements`` can revise the number without editing journal code.
    """
    assert constants.WORKFLOW_JOURNAL_BUSY_TIMEOUT_MS == 5000

    monkeypatch.setattr(
        "cli_agent_orchestrator.constants.WORKFLOW_JOURNAL_BUSY_TIMEOUT_MS", 1234, raising=True
    )
    conn = _connect()
    try:
        assert _busy_timeout(conn) == 1234
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# BR-2 — the pragma is set on EVERY connection, never memoised.
# ---------------------------------------------------------------------------
def test_every_connection_carries_the_pragma(db_path: Path, stdlib_default_timeout_disabled: None):
    """Two sequential connections both carry the timeout.

    The second call takes the migration-skipped branch (the path is cached by then),
    which is exactly where an implementation that set the pragma "once" alongside the
    migration state would drop it. A single-connection test would not catch that.

    Also runs with the stdlib default suppressed — without that, BOTH connections
    report 5000 from CPython's own default and this test passes against an
    implementation that sets the pragma only on the first, migrating connection
    (verified: that mutation passed the earlier version of this test).
    """
    first = _connect()
    try:
        assert _busy_timeout(first) == 5000
    finally:
        first.close()

    assert str(db_path) in _MIGRATED_PATHS  # the second call skips the migrators

    second = _connect()
    try:
        assert _busy_timeout(second) == 5000
    finally:
        second.close()


# ---------------------------------------------------------------------------
# BR-3 — migrators run at most once per database path per process (+ SR-2).
# ---------------------------------------------------------------------------
def test_migrators_run_once_per_path(db_path: Path, monkeypatch: pytest.MonkeyPatch):
    """THE SPIES DELEGATE TO THE REAL MIGRATORS, and since PR #628's review they must.

    They were pure counters that created nothing. ``_connect`` now verifies the schema before
    caching the path (BR-6, corrected), so a stub that counts without creating a table is
    indistinguishable from a silently failed migration — correctly NOT cached, and correctly
    re-run on the next call. Counting the real call is what this test was always about; the
    stub's silence about the schema was incidental to it and is now load-bearing elsewhere
    (see :class:`TestASilentMigrationFailureIsNotCached`).
    """
    real_run = database_client._migrate_workflow_run
    real_step = database_client._migrate_workflow_run_step
    calls = {"run": 0, "step": 0}

    def _count_run() -> None:
        calls["run"] += 1
        real_run()

    def _count_step() -> None:
        calls["step"] += 1
        real_step()

    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database._migrate_workflow_run", _count_run, raising=True
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database._migrate_workflow_run_step",
        _count_step,
        raising=True,
    )

    for _ in range(2):
        _connect().close()

    assert calls == {"run": 1, "step": 1}
    # SR-2: the guard set accumulates paths — strings — and nothing else.
    assert all(isinstance(entry, str) for entry in _MIGRATED_PATHS)


# ---------------------------------------------------------------------------
# BR-4 / BR-5 — a new path always migrates, even mid-process. THE load-bearing test.
# ---------------------------------------------------------------------------
def test_new_path_migrates_mid_process(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Repointing ``DATABASE_FILE`` mid-process migrates the new path (BR-4, BR-5).

    This is the test that fails if the path-keyed ``_MIGRATED_PATHS`` set is ever
    "simplified" into a boolean flag: with a flag, path B below is connected to
    without its schema ever being created, which is precisely how a boolean would
    break the five ``test/clients/`` modules that repoint ``DATABASE_FILE``.

    It also pins BR-5: it only passes while ``DATABASE_FILE`` is read INSIDE
    ``_connect`` on every call. Capture it at import — or cache it beside the set —
    and path B never gets looked at.
    """
    _connect().close()
    assert {"workflow_run", "workflow_run_step"} <= _table_names(db_path)

    path_b = tmp_path / "other" / "wf-b.db"
    path_b.parent.mkdir()
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path_b, raising=True)

    _connect().close()

    assert {"workflow_run", "workflow_run_step"} <= _table_names(path_b)


# ---------------------------------------------------------------------------
# BR-6 — a failed migration is never cached as success.
# ---------------------------------------------------------------------------
def test_failed_migration_is_not_cached(db_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A migrator that RAISES leaves the path uncached, so the next call retries.

    WHAT THIS PROVES: ``_connect`` adds to ``_MIGRATED_PATHS`` only after both
    migrators return. A raise escaping a migrator propagates out of ``_connect``
    (it has no try/except), the path is not recorded, and the next call re-runs both
    migrators and gets a real schema. Asserted on call counts across the two calls
    plus set membership, per BR-6.

    WHAT IT NO LONGER LEAVES UNPROVEN (PR #628 review, Copilot F2). This docstring used
    to end here, saying: "the REAL migrators each wrap their body in ``except
    Exception`` and log at debug, so a real-world migration failure never raises —
    it returns normally and IS therefore cached as success. This test cannot cover
    that case because ``_connect`` cannot observe it." The first half was right and
    the conclusion was wrong: ``_connect`` cannot observe the FAILURE, but it can
    observe the SCHEMA, and that is the thing the cache is a claim about. It now
    verifies the columns before caching, so the silent-failure case IS covered —
    by :class:`TestASilentMigrationFailureIsNotCached` below. The migrators' error
    posture is unchanged, which is what ``business-logic-model.md`` ("Error
    handling", row 1) places outside this unit.

    The migrators are patched on ``clients.database``, which IS the resolution point
    for ``_connect``'s function-local import: it re-reads the module attribute on
    every call, so the patched name is the one ``_connect`` invokes. There is no
    module-level alias in ``workflow_journal`` to patch instead.
    """
    real_migrate_run = database_client._migrate_workflow_run  # captured before patching
    real_migrate_step = database_client._migrate_workflow_run_step
    calls = {"run": 0, "step": 0}

    def _raise_once_then_delegate() -> None:
        calls["run"] += 1
        if calls["run"] == 1:
            raise RuntimeError("simulated migration failure")
        real_migrate_run()

    def _count_step() -> None:
        # Delegates since PR #628's review: ``_connect`` verifies the schema before caching,
        # so a step migrator that counted without creating its table would leave the path
        # uncached for a REASON THIS TEST IS NOT ABOUT and hide the recovery it asserts.
        calls["step"] += 1
        real_migrate_step()

    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database._migrate_workflow_run",
        _raise_once_then_delegate,
        raising=True,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database._migrate_workflow_run_step",
        _count_step,
        raising=True,
    )

    with pytest.raises(RuntimeError, match="simulated migration failure"):
        _connect()

    # The failure is NOT cached: the path is absent, and the second migrator never
    # ran (the raise aborted the sequence before it).
    assert str(db_path) not in _MIGRATED_PATHS
    assert calls == {"run": 1, "step": 0}

    # The next call retries BOTH migrators rather than trusting a cached failure.
    _connect().close()
    assert calls == {"run": 2, "step": 1}
    assert str(db_path) in _MIGRATED_PATHS
    assert "workflow_run" in _table_names(db_path)


# ---------------------------------------------------------------------------
# BR-6, corrected by PR #628's review (Copilot F2) — a SILENTLY failed migration is
# not cached as success either. This is the case ``test_failed_migration_is_not_cached``
# above could not reach, and it is the case that actually happens in production:
# the real migrators return normally when they fail.
# ---------------------------------------------------------------------------
class TestASilentMigrationFailureIsNotCached:
    """``_MIGRATED_PATHS`` is a claim about the SCHEMA, so it is verified against the schema.

    The substituted migrators here RETURN NORMALLY AND CREATE NOTHING — which is exactly what
    the real ones do when their ``except Exception`` -> ``logger.debug`` fires. Under the old
    "cache after both migrators return" rule the path was cached, and every later connection in
    the process skipped the migrators and talked to a schema-less database.
    """

    @staticmethod
    def _silent_noop_migrators(monkeypatch: pytest.MonkeyPatch) -> dict:
        calls = {"run": 0, "step": 0}

        def _noop_run() -> None:
            calls["run"] += 1  # returns normally, creates nothing — a swallowed failure

        def _noop_step() -> None:
            calls["step"] += 1

        monkeypatch.setattr(
            "cli_agent_orchestrator.clients.database._migrate_workflow_run",
            _noop_run,
            raising=True,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.clients.database._migrate_workflow_run_step",
            _noop_step,
            raising=True,
        )
        return calls

    def test_a_migrator_that_returns_without_creating_the_schema_is_not_cached(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """THE REGRESSION TEST FOR F2. Fails against "cache after both migrators return"."""
        self._silent_noop_migrators(monkeypatch)

        _connect().close()

        assert str(db_path) not in _MIGRATED_PATHS

    def test_the_next_connection_retries_instead_of_trusting_the_cache(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The consequence that made F2 worth fixing: ONE transient failure must not disable
        migration for the rest of the process. Both migrators run again on the next call, and
        once they really create the schema the path is cached and they stop."""
        calls = self._silent_noop_migrators(monkeypatch)

        _connect().close()
        assert calls == {"run": 1, "step": 1}

        _connect().close()  # retried, not skipped
        assert calls == {"run": 2, "step": 2}

        # Now let the REAL migrators run: the schema appears, the path is cached, and a third
        # call skips them. Without the caching half, this test would also pass against an
        # implementation that never caches anything at all.
        monkeypatch.undo()
        monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
        _connect().close()
        assert str(db_path) in _MIGRATED_PATHS
        assert {"workflow_run", "workflow_run_step"} <= _table_names(db_path)

    def test_a_partial_migration_is_not_cached_either(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """One table created, the other silently skipped — the shape a real partial failure
        takes, since the two migrators are separate functions with separate ``try`` blocks.

        A whole-table check alone would catch this; the next test is the one that needs the
        column-level check.
        """
        real_migrate_run = database_client._migrate_workflow_run

        def _skip_step() -> None:
            return

        monkeypatch.setattr(
            "cli_agent_orchestrator.clients.database._migrate_workflow_run_step",
            _skip_step,
            raising=True,
        )

        _connect().close()

        assert real_migrate_run is database_client._migrate_workflow_run  # run/step unpatched
        assert "workflow_run" in _table_names(db_path)
        assert "workflow_run_step" not in _table_names(db_path)
        assert str(db_path) not in _MIGRATED_PATHS

    def test_a_table_missing_one_required_column_is_not_cached(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The case a table-existence check would MISS, and the one #583 actually shipped a
        column into: the ``ALTER TABLE ADD COLUMN`` half of ``_migrate_workflow_run_step``
        failing while the ``CREATE TABLE`` half succeeded. Five guarded ``ALTER``s now share
        one ``try`` block, so this is the most likely partial state in production.

        Simulated by creating the PRE-#583 table shape and then letting the migrators no-op,
        which is what a locked database does to an ``ALTER``.
        """
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "CREATE TABLE workflow_run_step ("
                "run_id TEXT NOT NULL, step_id TEXT NOT NULL, state TEXT NOT NULL, "
                "attempts INTEGER NOT NULL, output_json TEXT, error TEXT, "
                "updated_at TEXT NOT NULL, PRIMARY KEY (run_id, step_id))"
            )
        self._silent_noop_migrators(monkeypatch)

        _connect().close()

        assert "workflow_run_step" in _table_names(db_path)  # the table IS there...
        assert str(db_path) not in _MIGRATED_PATHS  # ...and ``result_json`` is not

    def test_the_required_column_sets_match_what_the_migrators_produce(self, db_path: Path):
        """THE DRIFT GUARD. Equality, not a subset check: a column added to a migrator without
        being added to the required set would silently drop out of the verification, and a
        column named in the set that no migrator creates would make every path fail
        verification and disable the cache entirely. Both are one edit away, so both fail here.
        """
        database_client._migrate_workflow_run()
        database_client._migrate_workflow_run_step()

        with sqlite3.connect(str(db_path)) as conn:
            run_cols = {r[1] for r in conn.execute("PRAGMA table_info(workflow_run)")}
            step_cols = {r[1] for r in conn.execute("PRAGMA table_info(workflow_run_step)")}

        assert set(workflow_journal._REQUIRED_RUN_COLUMNS) == run_cols
        assert set(workflow_journal._REQUIRED_STEP_COLUMNS) == step_cols

    def test_the_predicate_never_raises_on_an_unusable_connection(self, db_path: Path):
        """``_journal_schema_is_present`` is total, so verification cannot add a failure mode to
        ``_connect`` that ``_connect`` did not already have. A closed connection is the cheapest
        unusable one to produce."""
        conn = sqlite3.connect(str(db_path))
        conn.close()

        assert workflow_journal._journal_schema_is_present(conn) is False

    def test_a_verified_schema_is_cached_and_the_migrators_stop_running(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The other direction: verification must not turn BR-3 off. With the real migrators,
        the first call migrates and caches; the second runs neither migrator."""
        calls = {"n": 0}
        real_run = database_client._migrate_workflow_run
        real_step = database_client._migrate_workflow_run_step

        def _count_run() -> None:
            calls["n"] += 1
            real_run()

        def _count_step() -> None:
            calls["n"] += 1
            real_step()

        monkeypatch.setattr(
            "cli_agent_orchestrator.clients.database._migrate_workflow_run",
            _count_run,
            raising=True,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.clients.database._migrate_workflow_run_step",
            _count_step,
            raising=True,
        )

        _connect().close()
        _connect().close()

        assert calls == {"n": 2}  # two migrators, ONE migrating call
        assert str(db_path) in _MIGRATED_PATHS


# ---------------------------------------------------------------------------
# BR-7 — the observable contract is unchanged for every existing caller.
# ---------------------------------------------------------------------------
def test_connect_signature_unchanged(db_path: Path):
    signature = inspect.signature(_connect)
    assert signature.parameters == {}
    # ``from __future__ import annotations`` is active in the module, so the
    # annotation arrives as a string; accept either form.
    assert signature.return_annotation in ("sqlite3.Connection", sqlite3.Connection)
    assert _connect.__module__ == "cli_agent_orchestrator.services.workflow_journal"

    conn = _connect()
    try:
        assert isinstance(conn, sqlite3.Connection)
    finally:
        conn.close()

    # A real round-trip through two unmodified callers: nothing about how they use
    # the connection changed.
    workflow_journal.insert_run(
        run_id="run-posture-1",
        workflow_name="wf",
        spec_snapshot="{}",
        inputs_json="{}",
        state="running",
        started_at="2026-08-14T00:00:00Z",
    )
    row = workflow_journal.get_run("run-posture-1")
    assert row is not None
    assert row.run_id == "run-posture-1"
    assert row.state == "running"


# ---------------------------------------------------------------------------
# BR-8 / SR-4 — no WAL, no other database-level pragma, no sidecar files.
# ---------------------------------------------------------------------------
def test_journal_mode_unchanged_and_no_wal_sidecar(db_path: Path):
    """This unit sets no database-level property (BR-8, SR-4).

    ``journal_mode`` is a property of the FILE, shared with every other CAO
    subsystem that opens it (terminals, sessions, the spec index), which is why WAL
    is out of scope here and ``busy_timeout`` is not.
    """
    conn = _connect()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()

    assert mode.lower() != "wal"
    assert not db_path.with_name(db_path.name + "-wal").exists()
    assert not db_path.with_name(db_path.name + "-shm").exists()
