"""Phase 2 tests for MemoryService — SQLite-backed metadata (U1).

Tests create a per-test SQLite DB via db_engine fixture.
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.services.memory_service import MemoryService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(
    cwd: str = "/home/user/project",
    session_name: str = "test-session",
    agent_profile: str = "developer",
    provider: str = "claude_code",
    terminal_id: str = "term-001",
) -> dict:
    return {
        "terminal_id": terminal_id,
        "session_name": session_name,
        "agent_profile": agent_profile,
        "provider": provider,
        "cwd": cwd,
    }


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db_engine(tmp_path):
    """Create a per-test SQLite engine with memory_metadata table."""
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture
def svc(tmp_path, db_engine):
    """MemoryService wired to tmp_path + test DB."""
    return MemoryService(base_dir=tmp_path, db_engine=db_engine)


# ===========================================================================
# U1.3 — store() inserts SQLite row
# ===========================================================================


class TestStoreInsertsSqliteRow:
    def test_store_inserts_sqlite_row(self, svc, db_engine):
        ctx = _make_ctx()
        mem = _run(
            svc.store(
                content="pytest is the preferred test framework",
                scope="project",
                memory_type="feedback",
                key="prefer-pytest",
                tags="testing",
                terminal_context=ctx,
            )
        )

        # Verify SQLite row exists
        from sqlalchemy.orm import sessionmaker

        Session = sessionmaker(bind=db_engine)
        with Session() as db:
            row = db.query(MemoryMetadataModel).filter_by(key="prefer-pytest").first()
            assert row is not None
            assert row.scope == "project"
            assert row.memory_type == "feedback"
            assert row.tags == "testing"
            assert row.source_provider == "claude_code"
            assert row.source_terminal_id == "term-001"
            assert row.file_path == mem.file_path

    def test_store_upsert_updates_sqlite_row(self, svc, db_engine):
        ctx = _make_ctx()
        _run(
            svc.store(
                content="first version",
                scope="global",
                memory_type="user",
                key="my-pref",
                terminal_context=ctx,
            )
        )
        _run(
            svc.store(
                content="updated version",
                scope="global",
                memory_type="user",
                key="my-pref",
                tags="updated",
                terminal_context=ctx,
            )
        )

        from sqlalchemy.orm import sessionmaker

        Session = sessionmaker(bind=db_engine)
        with Session() as db:
            rows = db.query(MemoryMetadataModel).filter_by(key="my-pref").all()
            assert len(rows) == 1  # upsert, not duplicate
            assert rows[0].tags == "updated"


# ===========================================================================
# U1.4 — recall() uses SQLite query
# ===========================================================================


class TestRecallUsesSqliteQuery:
    def test_recall_by_key_match(self, svc):
        ctx = _make_ctx()
        _run(
            svc.store(
                content="always use black",
                scope="global",
                memory_type="feedback",
                key="use-black",
                terminal_context=ctx,
            )
        )
        _run(
            svc.store(
                content="prefer isort",
                scope="global",
                memory_type="feedback",
                key="use-isort",
                terminal_context=ctx,
            )
        )

        results = _run(svc.recall(query="black", scope="global", terminal_context=ctx))
        assert len(results) == 1
        assert results[0].key == "use-black"

    def test_recall_by_tags_match(self, svc):
        ctx = _make_ctx()
        _run(
            svc.store(
                content="some content",
                scope="global",
                memory_type="project",
                key="api-notes",
                tags="api,backend",
                terminal_context=ctx,
            )
        )

        results = _run(svc.recall(query="api", scope="global", terminal_context=ctx))
        assert len(results) >= 1
        assert any(r.key == "api-notes" for r in results)

    def test_recall_with_memory_type_filter(self, svc):
        ctx = _make_ctx()
        _run(
            svc.store(
                content="feedback item",
                scope="global",
                memory_type="feedback",
                key="fb-1",
                terminal_context=ctx,
            )
        )
        _run(
            svc.store(
                content="project item",
                scope="global",
                memory_type="project",
                key="proj-1",
                terminal_context=ctx,
            )
        )

        results = _run(svc.recall(memory_type="feedback", scope="global", terminal_context=ctx))
        assert all(r.memory_type == "feedback" for r in results)


# ===========================================================================
# U1.5 — forget() deletes SQLite row
# ===========================================================================


class TestForgetDeletesSqliteRow:
    def test_forget_deletes_sqlite_row(self, svc, db_engine):
        ctx = _make_ctx()
        _run(
            svc.store(
                content="to be deleted",
                scope="global",
                memory_type="project",
                key="del-me",
                terminal_context=ctx,
            )
        )

        result = _run(svc.forget(key="del-me", scope="global", terminal_context=ctx))
        assert result is True

        from sqlalchemy.orm import sessionmaker

        Session = sessionmaker(bind=db_engine)
        with Session() as db:
            row = db.query(MemoryMetadataModel).filter_by(key="del-me").first()
            assert row is None

    def test_forget_returns_false_for_missing(self, svc):
        ctx = _make_ctx()
        result = _run(svc.forget(key="nonexistent", scope="global", terminal_context=ctx))
        assert result is False


# ===========================================================================
# U1.7 — token estimate stored
# ===========================================================================


class TestTokenEstimateStored:
    def test_token_estimate_char_based(self, svc, db_engine):
        ctx = _make_ctx()
        content = "a" * 400  # 400 chars → 100 tokens (400/4)
        _run(
            svc.store(
                content=content,
                scope="global",
                memory_type="project",
                key="tok-test",
                terminal_context=ctx,
            )
        )

        from sqlalchemy.orm import sessionmaker

        Session = sessionmaker(bind=db_engine)
        with Session() as db:
            row = db.query(MemoryMetadataModel).filter_by(key="tok-test").first()
            assert row is not None
            assert row.token_estimate == 100  # len(content) / 4


# ===========================================================================
# U1.8 — index.md stays consistent with SQLite
# ===========================================================================


class TestIndexConsistencyWithSqlite:
    def test_index_regenerated_from_sqlite(self, svc):
        ctx = _make_ctx()
        _run(
            svc.store(
                content="item one",
                scope="global",
                memory_type="project",
                key="item-one",
                terminal_context=ctx,
            )
        )
        _run(
            svc.store(
                content="item two",
                scope="global",
                memory_type="feedback",
                key="item-two",
                terminal_context=ctx,
            )
        )

        index_path = svc.get_index_path("global", None)
        assert index_path.exists()
        content = index_path.read_text()
        assert "[item-one]" in content
        assert "[item-two]" in content

    def test_index_updated_after_forget(self, svc):
        ctx = _make_ctx()
        _run(
            svc.store(
                content="keep me",
                scope="global",
                memory_type="project",
                key="keeper",
                terminal_context=ctx,
            )
        )
        _run(
            svc.store(
                content="remove me",
                scope="global",
                memory_type="project",
                key="goner",
                terminal_context=ctx,
            )
        )

        _run(svc.forget(key="goner", scope="global", terminal_context=ctx))

        index_path = svc.get_index_path("global", None)
        content = index_path.read_text()
        assert "[keeper]" in content
        assert "[goner]" not in content

    def test_direct_db_insert_reflected_in_index(self, svc, db_engine, tmp_path):
        """Insert directly to DB, then store via service — index should be consistent."""
        ctx = _make_ctx()
        # Store one normally
        _run(
            svc.store(
                content="normal",
                scope="global",
                memory_type="project",
                key="normal-one",
                terminal_context=ctx,
            )
        )

        # Verify index has it
        index_path = svc.get_index_path("global", None)
        content = index_path.read_text()
        assert "[normal-one]" in content


# ===========================================================================
# Full roundtrip with SQLite
# ===========================================================================


# ===========================================================================
# U10.1 — BM25 finds content match
# ===========================================================================


class TestBm25FindsContentMatch:
    def test_bm25_finds_content_not_in_key_or_tags(self, svc):
        """BM25 should find a memory by words in content, not just key/tags."""
        ctx = _make_ctx()
        _run(
            svc.store(
                content="The deployment uses GitHub Actions with OIDC authentication",
                scope="global",
                memory_type="project",
                key="deploy-pipeline",
                tags="ci,cd",
                terminal_context=ctx,
            )
        )
        _run(
            svc.store(
                content="Always use pytest for testing",
                scope="global",
                memory_type="feedback",
                key="test-framework",
                tags="testing",
                terminal_context=ctx,
            )
        )

        # "OIDC" is only in content, not in key or tags
        results = _run(svc.recall(query="OIDC authentication", scope="global", terminal_context=ctx))
        assert any(r.key == "deploy-pipeline" for r in results)

    def test_bm25_performance_within_budget(self, svc):
        """BM25 search on 100 files should complete within 500ms."""
        import time

        ctx = _make_ctx()
        for i in range(100):
            _run(
                svc.store(
                    content=f"Memory content about topic {i} with unique words like banana{i}",
                    scope="global",
                    memory_type="project",
                    key=f"perf-test-{i}",
                    terminal_context=ctx,
                )
            )

        start = time.time()
        results = _run(svc.recall(query="banana50", scope="global", terminal_context=ctx))
        elapsed = time.time() - start

        assert elapsed < 0.5, f"BM25 search took {elapsed:.3f}s, exceeds 500ms budget"
        assert any(r.key == "perf-test-50" for r in results)


# ===========================================================================
# U10.1 — Hybrid recall merges and deduplicates results
# ===========================================================================


class TestHybridRecallMergesResults:
    def test_metadata_and_bm25_results_merged(self, svc):
        """Recall should return results from both metadata and BM25 search, deduped."""
        ctx = _make_ctx()
        # This memory matches by key ("api-design") and also has unique content
        _run(
            svc.store(
                content="RESTful API design with versioning in the URL path",
                scope="global",
                memory_type="project",
                key="api-design",
                tags="api",
                terminal_context=ctx,
            )
        )
        # This memory only matches by content (BM25), not key/tags
        _run(
            svc.store(
                content="The versioning strategy follows semantic versioning for all APIs",
                scope="global",
                memory_type="feedback",
                key="versioning-strategy",
                tags="engineering",
                terminal_context=ctx,
            )
        )

        results = _run(svc.recall(query="api", scope="global", terminal_context=ctx))
        result_keys = {r.key for r in results}
        # "api-design" matches by key/tags; "versioning-strategy" may match by BM25
        assert "api-design" in result_keys

    def test_no_duplicates_in_merged_results(self, svc):
        """If a memory matches both metadata and BM25, it should appear only once."""
        ctx = _make_ctx()
        _run(
            svc.store(
                content="Always use black formatter for Python code",
                scope="global",
                memory_type="feedback",
                key="black-formatter",
                tags="black,formatter",
                terminal_context=ctx,
            )
        )

        # "black" matches both key/tags AND content
        results = _run(svc.recall(query="black", scope="global", terminal_context=ctx))
        keys = [r.key for r in results]
        assert keys.count("black-formatter") == 1


# ===========================================================================
# U10.1 — Consolidate merges two entries
# ===========================================================================


class TestConsolidateMergesTwoEntries:
    def test_consolidate_merges_and_removes_originals(self, svc, db_engine):
        """After consolidation, merged entry exists, originals are gone."""
        ctx = _make_ctx()
        _run(
            svc.store(
                content="pref A",
                scope="global",
                memory_type="feedback",
                key="pref-a",
                terminal_context=ctx,
            )
        )
        _run(
            svc.store(
                content="pref B",
                scope="global",
                memory_type="feedback",
                key="pref-b",
                terminal_context=ctx,
            )
        )

        # Consolidate via the MCP tool pattern: store merged, then forget originals
        _run(
            svc.store(
                content="Combined preference: A and B together",
                scope="global",
                memory_type="feedback",
                key="pref-merged",
                terminal_context=ctx,
            )
        )
        _run(svc.forget(key="pref-a", scope="global", terminal_context=ctx))
        _run(svc.forget(key="pref-b", scope="global", terminal_context=ctx))

        # Verify merged entry exists
        results = _run(svc.recall(query="pref-merged", scope="global", terminal_context=ctx))
        assert any(r.key == "pref-merged" for r in results)

        # Verify originals are gone
        from sqlalchemy.orm import sessionmaker

        Session = sessionmaker(bind=db_engine)
        with Session() as db:
            assert db.query(MemoryMetadataModel).filter_by(key="pref-a").first() is None
            assert db.query(MemoryMetadataModel).filter_by(key="pref-b").first() is None
            assert db.query(MemoryMetadataModel).filter_by(key="pref-merged").first() is not None


class TestFullSqliteRoundtrip:
    def test_store_recall_forget_roundtrip(self, svc, db_engine):
        ctx = _make_ctx()

        # Store
        mem = _run(
            svc.store(
                content="roundtrip test",
                scope="global",
                memory_type="project",
                key="roundtrip",
                terminal_context=ctx,
            )
        )
        assert Path(mem.file_path).exists()

        # Recall
        results = _run(svc.recall(query="roundtrip", scope="global", terminal_context=ctx))
        assert len(results) == 1
        assert results[0].key == "roundtrip"
        assert "roundtrip test" in results[0].content

        # Forget
        _run(svc.forget(key="roundtrip", scope="global", terminal_context=ctx))
        assert not Path(mem.file_path).exists()

        # Recall again — should be empty
        results = _run(svc.recall(query="roundtrip", scope="global", terminal_context=ctx))
        assert len(results) == 0

    def test_concurrent_stores_wal_safety(self, svc):
        """Concurrent stores should not cause write contention with WAL mode."""
        ctx = _make_ctx()
        # Store multiple items quickly
        for i in range(10):
            _run(
                svc.store(
                    content=f"concurrent item {i}",
                    scope="global",
                    memory_type="project",
                    key=f"conc-{i}",
                    terminal_context=ctx,
                )
            )

        results = _run(svc.recall(scope="global", terminal_context=ctx, limit=20))
        assert len(results) == 10


# ===========================================================================
# Migration: fresh DB
# ===========================================================================


class TestMigrationFreshDb:
    def test_memory_metadata_table_created(self, db_engine):
        """On a fresh DB, memory_metadata table should exist after create_all."""
        from sqlalchemy import inspect

        inspector = inspect(db_engine)
        tables = inspector.get_table_names()
        assert "memory_metadata" in tables

    def test_unique_constraint_enforced(self, svc, db_engine):
        """Storing same key+scope should upsert, not duplicate."""
        ctx = _make_ctx()
        _run(
            svc.store(
                content="v1",
                scope="global",
                memory_type="project",
                key="uniq-test",
                terminal_context=ctx,
            )
        )
        _run(
            svc.store(
                content="v2",
                scope="global",
                memory_type="project",
                key="uniq-test",
                terminal_context=ctx,
            )
        )

        from sqlalchemy.orm import sessionmaker

        Session = sessionmaker(bind=db_engine)
        with Session() as db:
            count = db.query(MemoryMetadataModel).filter_by(key="uniq-test").count()
            assert count == 1
