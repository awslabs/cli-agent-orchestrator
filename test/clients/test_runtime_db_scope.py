"""What an execution-only runtime is allowed to create in its local database (#745).

``cao-bridge`` executes panes and owns no orchestration state; the central
``cao-server`` owns all of it. It was nonetheless calling ``init_db``, the
control-plane initializer, so every execution pod created and migrated the whole
schema — workflow journals, memory, the vault, handoff results — on its own fresh
SQLite file. That is worker-local state that looks authoritative and is not, which
is the specific thing the topology contract rules out, plus a migration registry
running where it has no reader (Copilot review on #802, finding 5).

``init_runtime_db`` is the scoped initializer. These tests pin both halves of it:
the control-plane tables must be absent, and the pane bookkeeping the execution
path genuinely writes must still work.
"""

import sqlite3
import stat
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as db_mod

# Tables that exist only to serve orchestration, i.e. the ones a pane executor
# has no business holding. Named explicitly rather than derived, so adding a
# control-plane table to the runtime set has to be a deliberate edit here too.
CONTROL_PLANE_TABLES = (
    "workflow_run",
    "workflow_run_step",
    "workflow_run_event",
    "workflow_outcomes",
    "memory_metadata",
    "memory_relationships",
    "vault_note",
    "vault_exclusion",
    "handoff_results",
    "flows",
    "project_aliases",
)


@pytest.fixture
def runtime_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fresh, empty database file with the module pointed at it.

    Fresh is the case that matters: an execution pod starts with no file at all,
    so whatever the initializer creates is the whole of what a runtime ever holds.
    """
    db_dir = tmp_path / "db"
    db_file = db_dir / "cli-agent-orchestrator.db"
    monkeypatch.setattr(db_mod, "DB_DIR", db_dir)
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_file, raising=True)
    db_mod._ensure_db_dir()
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(
        db_mod, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=engine)
    )
    yield db_file
    engine.dispose()


def _tables(db_file: Path) -> set:
    with sqlite3.connect(str(db_file)) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows if not row[0].startswith("sqlite_")}


class TestTheRuntimeSchemaIsScoped:
    def test_only_the_pane_local_tables_are_created(self, runtime_db):
        db_mod.init_runtime_db()

        assert _tables(runtime_db) == set(db_mod.RUNTIME_TABLE_NAMES)

    @pytest.mark.parametrize("table", CONTROL_PLANE_TABLES)
    def test_no_orchestration_table_exists_on_a_runtime(self, runtime_db, table):
        """Each named individually so a failure says which boundary was crossed."""
        db_mod.init_runtime_db()

        assert table not in _tables(runtime_db)

    def test_the_control_plane_initializer_does_create_them(self, runtime_db):
        """The contrast, so the test above is about scope and not about a typo.

        If ``init_db`` did not create these either, the assertions above would pass
        for the wrong reason.
        """
        db_mod.init_db()

        created = _tables(runtime_db)
        assert set(CONTROL_PLANE_TABLES) <= created
        assert set(db_mod.RUNTIME_TABLE_NAMES) <= created

    def test_the_runtime_file_is_owner_only(self, runtime_db):
        """A pane row carries the prompt-adjacent fields; same posture as the server."""
        db_mod.init_runtime_db()

        assert stat.S_IMODE(runtime_db.stat().st_mode) == 0o600


class TestTheExecutionPathStillWorks:
    """Scoping the schema must not break what the runtime actually does.

    ``terminal_service`` — everything the bridge runs — writes exactly these three
    tables. If the scoped initializer missed one, the bridge would fail on its
    first LAUNCH with ``no such table``, which is why these go through the real DB
    functions rather than asserting the table list a second time.
    """

    @staticmethod
    def _create_pane(**extra):
        return db_mod.create_terminal(
            terminal_id="abcd1234",
            tmux_session="cao-1234",
            tmux_window="agent-0",
            provider="kiro_cli",
            agent_profile="developer",
            **extra,
        )

    def test_a_pane_row_can_be_created_and_read_back(self, runtime_db):
        db_mod.init_runtime_db()

        self._create_pane()

        rows = db_mod.list_all_terminals()
        assert [row["id"] for row in rows] == ["abcd1234"]

    def test_an_inbox_row_can_be_written(self, runtime_db):
        """A worker's callback lands here before the bridge forwards it."""
        db_mod.init_runtime_db()
        self._create_pane()

        db_mod.create_inbox_message(
            sender_id="supervisor", receiver_id="abcd1234", message="do the task"
        )

        assert [m.message for m in db_mod.get_inbox_messages("abcd1234")] == ["do the task"]

    def test_an_idempotency_key_can_be_claimed(self, runtime_db):
        """The retry guard on create: absent this table a retried LAUNCH duplicates."""
        db_mod.init_runtime_db()

        self._create_pane(idempotency_key="retry-1", request_fingerprint="deadbeef")

        record = db_mod.get_idempotency_record("retry-1")
        assert record is not None
        assert record.terminal_id == "abcd1234"

    def test_running_it_twice_is_a_no_op(self, runtime_db):
        """A restarted pod re-runs it against the file its predecessor left."""
        db_mod.init_runtime_db()
        self._create_pane()

        db_mod.init_runtime_db()

        assert [row["id"] for row in db_mod.list_all_terminals()] == ["abcd1234"]
        assert _tables(runtime_db) == set(db_mod.RUNTIME_TABLE_NAMES)


class TestTheBridgeUsesIt:
    """The scoped initializer only helps if the bridge is the thing calling it.

    ``_amain`` calls it by module-global name, so what the bridge module imported
    is what an execution pod runs at startup.
    """

    def test_the_bridge_binds_the_runtime_initializer_and_not_the_control_plane_one(self):
        from cli_agent_orchestrator.runtime_channel import bridge as bridge_mod

        assert hasattr(bridge_mod, "init_runtime_db")
        assert not hasattr(
            bridge_mod, "init_db"
        ), "cao-bridge must not be able to create the control plane's schema"

    def test_the_startup_path_calls_it(self, monkeypatch):
        """Asserted through ``_amain`` itself, so a future edit that drops the call
        (or reorders it behind the connect) fails here rather than on a cluster."""
        import asyncio

        from cli_agent_orchestrator.runtime_channel import bridge as bridge_mod

        monkeypatch.setenv("CAO_BRIDGE_SERVER_URL", "ws://server/runtime/channel")
        monkeypatch.setenv("CAO_BRIDGE_RUNTIME_ID", "worker-1")
        monkeypatch.setenv("CAO_RUNTIME_TOKEN", "tok")

        calls = []
        monkeypatch.setattr(bridge_mod, "init_runtime_db", lambda: calls.append("init"))
        # Stop immediately after startup: the initializer is the subject, and the
        # reconnect loop would otherwise run forever.
        monkeypatch.setattr(bridge_mod, "Bridge", lambda *a, **k: _StopImmediately(calls))
        monkeypatch.setattr(bridge_mod.bus, "set_loop", lambda loop: None)
        # The background services are not the subject and would touch tmux.
        monkeypatch.setattr(bridge_mod, "status_monitor", _Idle())
        monkeypatch.setattr(bridge_mod, "log_writer", _Idle())

        asyncio.run(bridge_mod._amain())

        assert calls[0] == "init", f"startup order was {calls}"


class _Idle:
    """A background service that parks until cancelled."""

    async def run(self):
        import asyncio

        await asyncio.Event().wait()


class _StopImmediately:
    """A Bridge stand-in whose ``run`` returns at once."""

    def __init__(self, calls):
        self._calls = calls

    async def run(self):
        self._calls.append("run")

    def stop(self):  # pragma: no cover - only wired as a signal handler
        pass

    async def _forward_output(self):
        pass

    async def _forward_status(self):
        pass

    async def _heartbeat(self):
        pass


class TestAnUpgradedPodKeepsItsFile:
    def test_a_pane_table_from_an_older_image_gains_the_new_columns(self, runtime_db):
        """In-place upgrade: the file was written by an image without ``owner``.

        The terminals migrators are the only ones the runtime initializer runs, so
        this is what proves they actually run — a pod rolled forward onto a file it
        cannot query is the failure this guards.
        """
        with sqlite3.connect(str(runtime_db)) as conn:
            conn.execute(
                "CREATE TABLE terminals (id TEXT PRIMARY KEY, name TEXT, provider TEXT, "
                "session_name TEXT, agent_profile TEXT)"
            )

        db_mod.init_runtime_db()

        with sqlite3.connect(str(runtime_db)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(terminals)")}
        assert "owner" in columns
        assert "allowed_tools" in columns
        assert "shell_command" in columns
