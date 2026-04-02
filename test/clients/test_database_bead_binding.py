"""Integration tests for bead_id binding on terminals."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import cli_agent_orchestrator.clients.database as db_mod


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    """Swap the global engine/SessionLocal to a temp SQLite file for isolation."""
    db_file = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Save originals
    orig_engine = db_mod.engine
    orig_session = db_mod.SessionLocal

    # Swap in test engine
    db_mod.engine = engine
    db_mod.SessionLocal = session_factory

    # Create all tables (including bead_id column since it's on the model)
    db_mod.Base.metadata.create_all(bind=engine)

    yield

    # Restore originals
    db_mod.engine = orig_engine
    db_mod.SessionLocal = orig_session


class TestCreateTerminalWithBeadId:
    def test_stores_bead_id(self):
        result = db_mod.create_terminal("t1", "cao-sess", "win", "q_cli", bead_id="bead-123")
        meta = db_mod.get_terminal_metadata("t1")
        assert meta is not None
        assert meta["bead_id"] == "bead-123"

    def test_bead_id_defaults_to_none(self):
        db_mod.create_terminal("t2", "cao-sess", "win", "q_cli")
        meta = db_mod.get_terminal_metadata("t2")
        assert meta is not None
        assert meta["bead_id"] is None

    def test_bead_id_in_create_return_value(self):
        result = db_mod.create_terminal("t3", "cao-sess", "win", "q_cli", bead_id="bead-ret")
        assert result["bead_id"] == "bead-ret"


class TestSetTerminalBead:
    def test_sets_bead_id(self):
        db_mod.create_terminal("t4", "cao-sess", "win", "q_cli")
        assert db_mod.set_terminal_bead("t4", "bead-456")
        meta = db_mod.get_terminal_metadata("t4")
        assert meta["bead_id"] == "bead-456"

    def test_clears_bead_id_with_none(self):
        db_mod.create_terminal("t5", "cao-sess", "win", "q_cli", bead_id="bead-789")
        db_mod.set_terminal_bead("t5", None)
        meta = db_mod.get_terminal_metadata("t5")
        assert meta["bead_id"] is None

    def test_returns_false_for_missing_terminal(self):
        assert not db_mod.set_terminal_bead("nonexistent", "bead-1")


class TestGetTerminalByBead:
    def test_finds_terminal(self):
        db_mod.create_terminal("t6", "cao-sess", "win", "q_cli", bead_id="bead-lookup")
        result = db_mod.get_terminal_by_bead("bead-lookup")
        assert result is not None
        assert result["id"] == "t6"
        assert result["bead_id"] == "bead-lookup"

    def test_returns_none_for_missing(self):
        assert db_mod.get_terminal_by_bead("nonexistent") is None


class TestListTerminalsIncludesBeadId:
    def test_list_by_session(self):
        db_mod.create_terminal("t7", "cao-sess-list", "win", "q_cli", bead_id="bead-list")
        terminals = db_mod.list_terminals_by_session("cao-sess-list")
        assert len(terminals) == 1
        assert terminals[0]["bead_id"] == "bead-list"

    def test_list_all(self):
        db_mod.create_terminal("t8", "cao-sess-all", "win", "q_cli", bead_id="bead-all")
        terminals = db_mod.list_all_terminals()
        t8 = next(t for t in terminals if t["id"] == "t8")
        assert t8["bead_id"] == "bead-all"


class TestInitDbMigration:
    def test_idempotent(self):
        """Calling init_db twice should not crash."""
        db_mod.init_db()
        db_mod.init_db()
