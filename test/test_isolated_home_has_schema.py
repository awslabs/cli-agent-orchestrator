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

Three angles, because the declaration and the effect can rot separately:

* the subprocess run is the end-to-end proof -- one real test file, alone, in a
  home that has never seen ``init_db()``;
* the in-process assertion proves the fixture actually reached *this* test
  without it asking for any database fixture;
* the declaration pin catches someone narrowing the scope or dropping
  ``autouse`` while the suite still happens to pass from ordering luck.
"""

import os
import subprocess
import sys
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
