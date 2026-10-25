"""Guard: every test gets a database, whatever ran before it.

``clients/database.py`` binds ``SessionLocal`` to a module-global engine at
import but only ``init_db()`` creates the tables, and ``test/conftest.py``'s
``_hermetic_cao_env`` deliberately leaves ``CAO_HOME_DIR`` alone so tests see
the documented default home layout. Before the ``_cao_home_schema`` fixture,
that combination meant a throwaway home had tables only if some earlier test in
the same process happened to call ``init_db()`` itself -- so
``CAO_HOME_DIR=$(mktemp -d) uv run pytest test/services/vault`` reported 50
``no such table: project_aliases`` failures dressed up as
``VaultConfigUnavailableError`` security refusals, and
``test_session_service.py`` reported 29. Both passed inside a warm home.

Four angles, because the declaration and the effect can rot separately:

* the subprocess run is the end-to-end proof -- one real test file, alone, in a
  home that has never seen ``init_db()``;
* the in-process assertion proves the fixture actually reached *this* test
  without it asking for any database fixture;
* the declaration pin catches someone narrowing the scope or dropping
  ``autouse`` while the suite still happens to pass from ordering luck;
* the concurrency guard proves the build is safe when several processes reach a
  schema-less home at once, which is what ``pytest -n auto`` does -- every xdist
  worker is a separate process running the same session-scoped fixture.
"""

import os
import subprocess
import sys
import time
from pathlib import Path
from test import conftest as root_conftest
from typing import Any

import pytest
from sqlalchemy import inspect as sa_inspect

from cli_agent_orchestrator.clients import database

REPO_ROOT = Path(__file__).resolve().parents[1]

# Ran 29 failed / 22 passed alone before the fixture, 51 passed after.
_CANARY_TEST_FILE = "test/services/test_session_service.py"

# Two tables the fail-closed paths read first, so their absence is what produced
# the false security refusals.
_LOAD_BEARING_TABLES = ("project_aliases", "memory_metadata")

# How many processes pile onto the one fresh home in the concurrency guard.
# haofeif measured 5 to 7 of 8 failing per round without the lock; 8 keeps that
# margin while the whole guard still runs in a couple of seconds.
_RACERS = 8

# Run by each racer. Two things make the overlap real rather than hoped for:
#
# * every import happens BEFORE the process reports ready, so the release leaves
#   each process one call away from init_db(). Import cost varies by hundreds of
#   milliseconds between processes; a shared wall-clock start time would just be
#   a lottery over who finished importing.
# * the release is one file appearing, spun on with no sleep, not a poll
#   interval. A 5ms poll was enough slack for the winner to finish init_db()
#   before some racers even looked: over 16 rounds with the lock removed, that
#   version left 2 rounds with no loser at all and 1-5 losers otherwise. Spinning
#   on the release file instead: 16 of 16 rounds red, 1-7 losers, which matches
#   the 5-7 of 8 haofeif measured by hand. Sensitivity is all this buys -- with
#   the lock in place the guard cannot fail either way -- but a guard that misses
#   the regression one round in eight is not a guard.
_RACER_PROGRAM = """
import os
import pathlib
import sys
import time

from sqlalchemy import inspect as sa_inspect
from test.conftest import SCHEMA_INIT_LOCK_NAME, ensure_cao_home_schema

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.constants import DB_DIR

barrier_dir = pathlib.Path(sys.argv[1])
go = barrier_dir / "go"

(barrier_dir / f"{os.getpid()}.ready").write_text("")
deadline = time.monotonic() + 120
spins = 0
while not go.exists():
    spins += 1
    if spins % 10000 == 0 and time.monotonic() > deadline:
        raise SystemExit("barrier timed out waiting for the parent's release")

ensure_cao_home_schema()

# Prove this process can actually use the schema, not merely that it survived.
present = set(sa_inspect(database.engine).get_table_names())
assert "session_incarnations" in present, sorted(present)
assert (DB_DIR / SCHEMA_INIT_LOCK_NAME).exists()
print("ok", os.getpid())
"""


def _fixture_marker(fixture: Any) -> Any:
    """Return the ``@pytest.fixture`` marker for *fixture*, whatever pytest calls it.

    pytest renamed this attribute when fixtures became
    ``FixtureFunctionDefinition`` objects (8.4). Both names are private, so
    check both and fail loudly rather than silently passing on a third name --
    a pin that cannot find what it pins is not a pin.
    """
    for attr in ("_fixture_function_marker", "_pytestfixturefunction"):
        marker = getattr(fixture, attr, None)
        if marker is not None:
            return marker
    raise AssertionError(
        f"cannot read the fixture marker off {fixture!r}; pytest "
        f"{pytest.__version__} renamed it again -- teach _fixture_marker the new name"
    )


def test_root_conftest_schema_fixture_is_session_scoped_and_autouse():
    marker = _fixture_marker(root_conftest._cao_home_schema)
    assert marker.autouse is True, (
        "_cao_home_schema must be autouse: a fixture tests have to request by "
        "name is exactly the ordering dependence it exists to remove"
    )
    assert marker.scope == "session", (
        f"_cao_home_schema must be session-scoped, got {marker.scope!r}; it runs "
        "init_db() against the real configured home and must do so once, before "
        "the first test"
    )


def test_schema_exists_without_requesting_a_database_fixture():
    """This test asks for nothing. The tables must be there anyway."""
    present = set(sa_inspect(database.engine).get_table_names())
    missing = [t for t in _LOAD_BEARING_TABLES if t not in present]
    assert not missing, (
        f"{missing} absent from the configured home at session start; the "
        "fail-closed vault paths will refuse with VaultConfigUnavailableError "
        "and it will read as a security finding, not a harness bug"
    )


@pytest.mark.slow
def test_canary_file_passes_alone_in_a_never_initialized_home(tmp_path: Path):
    """Run one real test file, alone, in a home with no schema at all."""
    home = tmp_path / "cao-home"
    home.mkdir()

    env = {k: v for k, v in os.environ.items() if not k.startswith("COV_CORE")}
    env["CAO_HOME_DIR"] = str(home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # The outer session's assurance gates do not apply to this nested one, and an
    # unmet gate name would fail it for an unrelated reason.
    env.pop("CAO_MUST_RUN", None)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--no-cov",
            "-q",
            # Nothing to recurse into: this guard is not in the selected file.
            _CANARY_TEST_FILE,
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )

    assert result.returncode == 0, (
        f"{_CANARY_TEST_FILE} does not pass alone in a throwaway home, so some "
        "test's database still depends on which tests ran before it.\n"
        f"--- stdout ---\n{result.stdout[-4000:]}\n--- stderr ---\n{result.stderr[-2000:]}"
    )
    # The home really was fresh: the subprocess had to build the schema itself.
    assert (home / "db" / "cli-agent-orchestrator.db").exists()


def test_concurrent_processes_can_build_one_fresh_home(tmp_path: Path):
    """``pytest -n auto``: every xdist worker runs the fixture on the same home.

    The fixture is per-session, and an xdist worker is a separate process with
    its own session, so N workers do the check-and-build N times against one
    ``CAO_HOME_DIR``. Before the lock in ``ensure_cao_home_schema()``, all of
    them saw no tables, all of them called ``init_db()``, and the losers died in
    ``Base.metadata.create_all()`` with ``table session_incarnations already
    exists`` -- which, for a session-scoped autouse fixture, is a setup error on
    every test the worker owned. haofeif measured 336-448 errors from
    ``pytest -n 4 test/services/vault`` on a fresh home (#1049 review at
    10587987).

    So: 8 processes, one fresh home, released together by a barrier, each
    calling the real ``ensure_cao_home_schema()``. All 8 must exit clean and all
    8 must see the schema.
    """
    home = tmp_path / "cao-home"
    home.mkdir()
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir()

    env = {k: v for k, v in os.environ.items() if not k.startswith("COV_CORE")}
    env["CAO_HOME_DIR"] = str(home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _RACER_PROGRAM, str(barrier_dir)],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(_RACERS)
    ]
    results = []
    try:
        deadline = time.monotonic() + 120
        while len(list(barrier_dir.glob("*.ready"))) < _RACERS:
            assert time.monotonic() < deadline, (
                f"only {len(list(barrier_dir.glob('*.ready')))} of {_RACERS} racers "
                "finished importing; the race never started"
            )
            assert all(p.poll() is None for p in procs), "a racer died before the barrier: " + str(
                [p.communicate()[1][-1500:] for p in procs if p.poll() not in (None, 0)]
            )
            time.sleep(0.01)
        (barrier_dir / "go").write_text("")

        for proc in procs:
            stdout, stderr = proc.communicate(timeout=300)
            results.append((proc.returncode, stdout, stderr))
    finally:
        for proc in procs:
            if proc.poll() is None:  # pragma: no cover - only on a barrier hang
                proc.kill()

    failed = [(rc, out, err) for rc, out, err in results if rc != 0]
    assert not failed, (
        f"{len(failed)} of {_RACERS} processes failed to build the schema "
        "concurrently on one fresh home; the check and init_db() are not "
        "serialized across processes, so `pytest -n auto` errors every test on "
        "every losing worker.\n"
        + "\n".join(f"--- rc={rc} ---\n{out[-1500:]}\n{err[-3000:]}" for rc, out, err in failed[:3])
    )
    # One home, one database, one lock file -- the schema was built once and the
    # other seven observed it rather than rebuilding it.
    assert (home / "db" / "cli-agent-orchestrator.db").exists()
    assert (home / "db" / root_conftest.SCHEMA_INIT_LOCK_NAME).exists()
