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
    calls = {"run": 0, "step": 0}

    def _count_run() -> None:
        calls["run"] += 1

    def _count_step() -> None:
        calls["step"] += 1

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

    WHAT THIS DOES NOT PROVE — stated because BR-6's rationale reaches further than
    any test of this unit honestly can: the REAL migrators each wrap their body in
    ``except Exception`` and log at debug (clients/database.py), so a real-world
    migration failure never raises — it returns normally and IS therefore cached as
    success. This test cannot cover that case because ``_connect`` cannot observe it:
    a silent failure is indistinguishable from success at this seam. Fixing it would
    mean changing the migrators' error posture, which ``business-logic-model.md``
    ("Error handling", row 1) explicitly places outside this unit. The rule as
    implementable — and as tested here — is "do not cache a raise".

    The migrators are patched on ``clients.database``, which IS the resolution point
    for ``_connect``'s function-local import: it re-reads the module attribute on
    every call, so the patched name is the one ``_connect`` invokes. There is no
    module-level alias in ``workflow_journal`` to patch instead.
    """
    real_migrate_run = database_client._migrate_workflow_run  # captured before patching
    calls = {"run": 0, "step": 0}

    def _raise_once_then_delegate() -> None:
        calls["run"] += 1
        if calls["run"] == 1:
            raise RuntimeError("simulated migration failure")
        real_migrate_run()

    def _count_step() -> None:
        calls["step"] += 1

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
