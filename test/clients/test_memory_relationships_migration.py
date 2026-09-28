"""U1 persistence tests (issue #511): schema shape, migrator registry placement,
dedup for global scope, idempotent re-run, and loss-aware legacy backfill."""

import sqlite3
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as db_mod
from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """A fresh sqlite file wired as both DATABASE_FILE (for the migrator's raw
    sqlite3 connect) and SessionLocal/engine (for ORM inserts)."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=False)
    # database.py imports DATABASE_FILE lazily inside migrators via
    # `from cli_agent_orchestrator.constants import DATABASE_FILE`, so patch the
    # constants module attribute.
    import cli_agent_orchestrator.constants as consts

    monkeypatch.setattr(consts, "DATABASE_FILE", db_path, raising=False)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(bind=engine))
    return db_path, engine


def _seed(engine, key, related_keys=None, scope="global", scope_id=None):
    s = sessionmaker(bind=engine)()
    try:
        s.add(
            MemoryMetadataModel(
                id=str(uuid.uuid4()),
                key=key,
                memory_type="project",
                scope=scope,
                scope_id=scope_id,
                file_path=f"/{key}.md",
                tags="t",
                related_keys=related_keys,
            )
        )
        s.commit()
    finally:
        s.close()


def test_table_shape_14_columns(fresh_db):
    db_path, _ = fresh_db
    db_mod._migrate_memory_relationships()
    conn = sqlite3.connect(str(db_path))
    cols = {r[1]: r[3] for r in conn.execute("PRAGMA table_info(memory_relationships)")}
    expected = {
        "id",
        "scope",
        "scope_id",
        "source_key",
        "target_key",
        "type",
        "origin",
        "status",
        "confidence",
        "rank",
        "attributes_json",
        "source_updated_at",
        "created_at",
        "updated_at",
    }
    assert set(cols) == expected
    # NOT NULL on the dedup-tuple columns incl scope_id (the sentinel column).
    for c in ("scope", "scope_id", "source_key", "target_key", "type", "origin", "status"):
        assert cols[c] == 1, f"{c} must be NOT NULL"
    for c in ("confidence", "rank", "attributes_json", "source_updated_at"):
        assert cols[c] == 0, f"{c} must be nullable"


def test_indexes_created(fresh_db):
    db_path, _ = fresh_db
    db_mod._migrate_memory_relationships()
    conn = sqlite3.connect(str(db_path))
    idx = {r[1] for r in conn.execute("PRAGMA index_list('memory_relationships')")}
    assert "uq_memory_rel" in idx
    assert "idx_memory_rel_lookup" in idx


def test_migrator_appended_last_in_registry():
    """The migrator is registered, and appended after _migrate_workflow_run_step
    (extend, do not reorder). Reads the init_db source to assert ordering without
    executing every migrator."""
    import inspect

    src = inspect.getsource(db_mod.init_db)
    assert "_migrate_memory_relationships()" in src
    assert src.index("_migrate_workflow_run_step()") < src.index("_migrate_memory_relationships()")
    # Must not issue SQL against the workflow tables. Parse the two functions
    # with ast and drop docstrings (which legitimately reference #504's
    # workflow_run* tables in prose), then assert no remaining code mentions
    # workflow_run while it does touch memory_relationships / memory_metadata.
    import ast
    import textwrap

    def _code_without_docstrings(fn) -> str:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                doc = ast.get_docstring(node, clean=False)
                if doc and node.body and isinstance(node.body[0], ast.Expr):
                    node.body = node.body[1:] or [ast.Pass()]
        return ast.unparse(tree)

    code = (
        _code_without_docstrings(db_mod._migrate_memory_relationships)
        + "\n"
        + _code_without_docstrings(db_mod._backfill_legacy_related_keys)
    )
    assert "workflow_run" not in code, "migrator must not issue SQL against workflow tables"
    assert "memory_relationships" in code
    assert "memory_metadata" in code  # backfill reads it


def test_idempotent_rerun(fresh_db):
    db_path, _ = fresh_db
    db_mod._migrate_memory_relationships()
    db_mod._migrate_memory_relationships()  # no error, no duplicate table
    conn = sqlite3.connect(str(db_path))
    n = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='memory_relationships'"
    ).fetchone()[0]
    assert n == 1


def test_backfill_happy_path_preserves_rank_null_confidence(fresh_db):
    db_path, engine = fresh_db
    for k in ("src", "a", "b", "c"):
        _seed(engine, k)
    _seed_src = sessionmaker(bind=engine)()
    # give src a related_keys list a,b,c
    row = _seed_src.query(MemoryMetadataModel).filter(MemoryMetadataModel.key == "src").first()
    row.related_keys = "a,b,c"
    _seed_src.commit()
    _seed_src.close()

    db_mod._migrate_memory_relationships()
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT target_key, rank, confidence, type, origin, status FROM memory_relationships "
        "WHERE source_key='src' ORDER BY rank"
    ).fetchall()
    assert [r[0] for r in rows] == ["a", "b", "c"]
    assert [r[1] for r in rows] == [0, 1, 2]  # rank preserved
    assert all(r[2] is None for r in rows)  # confidence NULL, never fabricated
    assert all(
        r[3] == "relates_to" and r[4] == "legacy_related_keys" and r[5] == "active" for r in rows
    )


def test_backfill_null_and_empty_yield_zero_rows(fresh_db):
    db_path, engine = fresh_db
    _seed(engine, "never", related_keys=None)  # never computed
    _seed(engine, "empty", related_keys="")  # computed-empty
    db_mod._migrate_memory_relationships()
    conn = sqlite3.connect(str(db_path))
    n = conn.execute("SELECT COUNT(*) FROM memory_relationships").fetchone()[0]
    assert n == 0


def test_backfill_dangling_reported_not_activated(fresh_db):
    db_path, engine = fresh_db
    _seed(engine, "src", related_keys="ghost")  # ghost does not exist
    db_mod._migrate_memory_relationships()
    conn = sqlite3.connect(str(db_path))
    n = conn.execute("SELECT COUNT(*) FROM memory_relationships WHERE source_key='src'").fetchone()[
        0
    ]
    assert n == 0  # dangling link NOT activated (reported instead)


def test_backfill_idempotent_per_source(fresh_db):
    db_path, engine = fresh_db
    _seed(engine, "src", related_keys="a")
    _seed(engine, "a")
    db_mod._migrate_memory_relationships()
    db_mod._migrate_memory_relationships()  # re-run
    conn = sqlite3.connect(str(db_path))
    n = conn.execute(
        "SELECT COUNT(*) FROM memory_relationships WHERE source_key='src' AND target_key='a'"
    ).fetchone()[0]
    assert n == 1  # not double-inserted


def test_related_keys_column_untouched(fresh_db):
    """The migrator must NOT modify memory_metadata.related_keys (retirement is a
    separate later change)."""
    db_path, engine = fresh_db
    _seed(engine, "src", related_keys="a")
    _seed(engine, "a")
    db_mod._migrate_memory_relationships()
    conn = sqlite3.connect(str(db_path))
    val = conn.execute("SELECT related_keys FROM memory_metadata WHERE key='src'").fetchone()[0]
    assert val == "a"  # unchanged
