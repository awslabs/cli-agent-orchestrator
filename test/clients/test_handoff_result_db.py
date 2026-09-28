"""Unit tests for the HandoffResultModel CRUD helpers (issue #447).

Uses an in-memory SQLite database so no file system state is required.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as db_mod
from cli_agent_orchestrator.clients.database import (
    Base,
    HandoffResultModel,
    delete_old_handoff_results,
    get_handoff_result,
    upsert_handoff_result,
)


@pytest.fixture(autouse=True)
def _use_test_db(monkeypatch):
    """Redirect all DB calls to a fresh in-memory SQLite DB."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr("cli_agent_orchestrator.clients.database.SessionLocal", TestSession)
    yield


def _utcnow():
    return datetime.now(timezone.utc)


class TestUpsertHandoffResult:
    def test_creates_new_record_on_first_call(self):
        upsert_handoff_result("job-1", "running")
        record = get_handoff_result("job-1")
        assert record is not None
        assert record["state"] == "running"
        assert record["last_message"] is None

    def test_updates_existing_record_on_second_call(self):
        upsert_handoff_result("job-2", "running")
        upsert_handoff_result("job-2", "completed", last_message="ok", terminal_id="abc12345")
        record = get_handoff_result("job-2")
        assert record["state"] == "completed"
        assert record["last_message"] == "ok"
        assert record["terminal_id"] == "abc12345"

    def test_error_state_stored_with_message(self):
        upsert_handoff_result("job-3", "running")
        upsert_handoff_result("job-3", "error", error_message="worker crashed")
        record = get_handoff_result("job-3")
        assert record["state"] == "error"
        assert record["error_message"] == "worker crashed"

    def test_partial_update_does_not_overwrite_nones(self):
        upsert_handoff_result("job-4", "completed", last_message="original", terminal_id="t1")
        # Calling with no last_message must not clear the existing value.
        upsert_handoff_result("job-4", "completed")
        record = get_handoff_result("job-4")
        assert record["last_message"] == "original"
        assert record["terminal_id"] == "t1"


class TestGetHandoffResult:
    def test_returns_none_for_unknown_job(self):
        assert get_handoff_result("no-such-job") is None

    def test_returns_dict_with_expected_keys(self):
        upsert_handoff_result("job-5", "running")
        record = get_handoff_result("job-5")
        assert set(record.keys()) == {
            "job_id",
            "state",
            "terminal_id",
            "last_message",
            "error_message",
            "created_at",
            "updated_at",
        }


class TestDeleteOldHandoffResults:
    def test_deletes_records_older_than_cutoff(self):
        upsert_handoff_result("old-1", "completed")
        upsert_handoff_result("old-2", "error")
        upsert_handoff_result("new-1", "running")
        # Backdate old-1 and old-2 by reaching into the DB directly.
        from cli_agent_orchestrator.clients.database import HandoffResultModel, SessionLocal

        past = _utcnow() - timedelta(days=20)
        with SessionLocal() as db:
            for jid in ("old-1", "old-2"):
                row = db.query(HandoffResultModel).filter(HandoffResultModel.job_id == jid).first()
                row.created_at = past
            db.commit()

        cutoff = _utcnow() - timedelta(days=10)
        deleted = delete_old_handoff_results(cutoff)
        assert deleted == 2
        assert get_handoff_result("old-1") is None
        assert get_handoff_result("old-2") is None
        assert get_handoff_result("new-1") is not None

    def test_returns_zero_when_nothing_to_delete(self):
        upsert_handoff_result("recent", "completed")
        cutoff = _utcnow() - timedelta(days=30)
        deleted = delete_old_handoff_results(cutoff)
        assert deleted == 0


class TestMigrateAddHandoffResults:
    """PR #453 review finding 7: the migrator shipped untested.

    The CRUD tests above sidestep it entirely -- they build their schema with
    ``Base.metadata.create_all``, which is the FRESH-database path. Nothing
    exercised the raw-SQL path that upgrades an operator's existing DB, so a typo
    in the DDL, a non-idempotent re-run, or a missing ``init_db`` registration
    would all have passed CI. Mirrors the coverage every other migrator ships
    (see test_memory_relationships_migration.py): table shape, idempotent re-run,
    registry placement.
    """

    @pytest.fixture
    def legacy_db(self, tmp_path, monkeypatch):
        """A sqlite file with SOME schema but no handoff_results -- an existing
        install upgrading into this change."""
        import cli_agent_orchestrator.constants as consts

        db_path = tmp_path / "legacy.db"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("CREATE TABLE terminals (id TEXT PRIMARY KEY)")
            conn.commit()
        monkeypatch.setattr(consts, "DATABASE_FILE", db_path, raising=False)
        return db_path

    def test_creates_table_with_expected_shape(self, legacy_db):
        with sqlite3.connect(str(legacy_db)) as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='handoff_results'"
                ).fetchone()[0]
                == 0
            )

        db_mod._migrate_add_handoff_results()

        with sqlite3.connect(str(legacy_db)) as conn:
            info = list(conn.execute("PRAGMA table_info(handoff_results)"))
        cols = {r[1]: r for r in info}
        assert set(cols) == {
            "job_id",
            "state",
            "terminal_id",
            "last_message",
            "error_message",
            "created_at",
            "updated_at",
        }
        # job_id is the primary key -- it is the upsert key AND the retrieval
        # capability, so a non-unique column here would let two jobs collide.
        assert cols["job_id"][5] == 1
        assert cols["state"][3] == 1, "state must be NOT NULL"
        for c in ("terminal_id", "last_message", "error_message"):
            assert cols[c][3] == 0, f"{c} must be nullable"

    def test_idempotent_rerun_keeps_one_table_and_its_rows(self, legacy_db):
        db_mod._migrate_add_handoff_results()
        with sqlite3.connect(str(legacy_db)) as conn:
            conn.execute(
                "INSERT INTO handoff_results (job_id, state, last_message) VALUES (?, ?, ?)",
                ("survivor", "completed", "worker output"),
            )
            conn.commit()

        db_mod._migrate_add_handoff_results()  # re-run must not raise or reset

        with sqlite3.connect(str(legacy_db)) as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='handoff_results'"
                ).fetchone()[0]
                == 1
            )
            # A re-run that dropped and recreated would silently destroy results.
            assert (
                conn.execute(
                    "SELECT last_message FROM handoff_results WHERE job_id='survivor'"
                ).fetchone()[0]
                == "worker output"
            )

    def test_registered_in_init_db_and_order_independent(self):
        """Registered, and safe wherever it sits: it names no table but its own, so
        it cannot depend on or perturb any migrator around it."""
        import ast
        import inspect
        import textwrap

        assert "_migrate_add_handoff_results()" in inspect.getsource(db_mod.init_db)

        tree = ast.parse(textwrap.dedent(inspect.getsource(db_mod._migrate_add_handoff_results)))
        # Drop the docstring, which legitimately discusses create_all and the ORM.
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.Module)) and node.body:
                if ast.get_docstring(node, clean=False) and isinstance(node.body[0], ast.Expr):
                    node.body = node.body[1:] or [ast.Pass()]
        code = ast.unparse(tree)
        assert "handoff_results" in code
        for other in ("memory_metadata", "memory_relationships", "workflow_run", "terminals"):
            assert other not in code, f"migrator must not issue SQL against {other}"
