"""Connection posture of the workflow spec-index factory (issue #583, Bolt 3, unit 1).

Guards ONE property: every connection ``workflow_spec_service._connect()`` returns
carries an explicit ``busy_timeout``, and it survives a caller or a library that would
otherwise remove it.

**Why this module monkeypatches ``sqlite3.connect``, which is unusual enough to explain.**
At the configured value the pragma is a runtime no-op: CPython's ``sqlite3.connect()``
already applies a 5000 ms busy timeout through its ``timeout=5.0`` default, and
``WORKFLOW_SPEC_INDEX_BUSY_TIMEOUT_MS`` is also 5000. So the obvious assertion —
"``PRAGMA busy_timeout`` reports 5000" — **passes with the production code reverted**,
which would make it a test that always passes regardless of implementation (BR-3A1-3).

The only way to exercise the real property is to make the library stop supplying the
default. Patching ``sqlite3.connect`` so its effective default is ``timeout=0`` does
exactly that: without the explicit pragma the timeout reads 0, with it the configured
value. Red before, green after.

The patch is applied through ``monkeypatch`` inside each test and never at module import
level — a module-level stub is how a distant signature change gets broken by a test that
looks unrelated (the trap recorded at ``functional-design:c21``).
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from cli_agent_orchestrator.constants import WORKFLOW_SPEC_INDEX_BUSY_TIMEOUT_MS
from cli_agent_orchestrator.services import workflow_spec_service


@pytest.fixture
def zero_default_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``sqlite3.connect``'s effective default ``timeout=0``.

    Removes the stdlib guarantee so the explicit pragma is the ONLY thing that can
    produce a non-zero busy timeout. A caller that passes ``timeout=`` explicitly is
    still honoured, so this narrows the default rather than overriding intent.
    """
    real_connect = sqlite3.connect

    def _connect_without_default_timeout(database, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("timeout", 0)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", _connect_without_default_timeout)


def test_busy_timeout_is_set_explicitly_not_inherited_from_the_stdlib_default(
    zero_default_connect: None,
) -> None:
    """BR-3A1-1: the pragma lands even when the library supplies no default.

    This is the assertion with teeth. With the ``PRAGMA`` statement removed from
    ``_connect`` this reads 0 and fails; with it present it reads the constant.
    """
    conn = workflow_spec_service._connect()
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == (
            WORKFLOW_SPEC_INDEX_BUSY_TIMEOUT_MS
        )
    finally:
        conn.close()


def test_the_stdlib_default_alone_would_not_satisfy_the_requirement(
    zero_default_connect: None,
) -> None:
    """Proves the fixture actually removes the guarantee.

    Without this, a broken fixture would make the test above pass for the wrong
    reason — the stdlib default quietly supplying 5000 again — and the teeth would be
    gone without anything failing.
    """
    conn = sqlite3.connect(":memory:")
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_failing_pragma_degrades_and_still_returns_a_usable_connection(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SR-3A1-3: degrade, never deny — and the degradation is specified, not accidental.

    Makes the pragma statement raise while leaving every other statement working. The
    factory must log at ``warning`` and hand back a connection the caller can still use;
    it must NOT propagate, because that would turn an unobserved pragma failure into a
    failure of every list/get/delete in the module.
    """
    real_connect = sqlite3.connect

    class _PragmaHostileConnection:
        """Delegates everything to a real connection, but rejects the pragma."""

        def __init__(self, inner: sqlite3.Connection) -> None:
            self._inner = inner

        def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
            if "busy_timeout" in sql:
                raise sqlite3.OperationalError("pragma refused")
            return self._inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    def _connect_pragma_hostile(database, *args, **kwargs):  # type: ignore[no-untyped-def]
        return _PragmaHostileConnection(real_connect(database, *args, **kwargs))

    monkeypatch.setattr(sqlite3, "connect", _connect_pragma_hostile)

    with caplog.at_level("WARNING"):
        conn = workflow_spec_service._connect()

    assert conn is not None, "a failing pragma must not deny service"
    # Still usable: the caller's own statements work.
    assert conn.execute("SELECT 1").fetchone()[0] == 1
    # ``getMessage()`` applies the lazy %-args exactly once. Reading ``record.message``
    # and re-applying ``record.args`` double-formats and raises TypeError.
    assert any(
        "busy_timeout" in record.getMessage() for record in caplog.records
    ), "the degradation must be logged, or the lost guarantee is invisible"


def test_a_failing_connect_still_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """SR-3A1-3 bounds the swallow: only the pragma is guarded, never ``connect``.

    A database that cannot be opened is a real failure. If the guard ever widened to
    cover ``connect`` itself, callers would receive ``None`` instead of an error.
    """

    def _connect_broken(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(sqlite3, "connect", _connect_broken)

    with pytest.raises(sqlite3.OperationalError):
        workflow_spec_service._connect()
