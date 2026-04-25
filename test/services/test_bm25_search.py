"""Tests for U6 — BM25 Fallback Search.

Covers:
- U6.1: rank-bm25 dependency available
- U6.2: BM25 index built on recall, finds content not in key/tags
- U6.3: search_mode parameter (metadata, bm25, hybrid)
- U6.4: MCP tool accepts search_mode
- Graceful fallback when rank-bm25 not installed
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.services.memory_service import MemoryService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WIKI_TEMPLATE = """\
<!-- id: {id} | tags: {tags} | scope: {scope} | type: {memory_type} -->
# {key}

## 2026-04-17T10:00:00Z

{content}
"""


def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def make_wiki_file(
    base_dir: Path,
    key: str,
    content: str,
    scope: str = "project",
    memory_type: str = "project",
    tags: str = "",
    scope_id: str = "test-project",
):
    """Create a wiki file in the expected directory structure."""
    wiki_dir = base_dir / scope_id / "wiki" / scope
    wiki_dir.mkdir(parents=True, exist_ok=True)
    wiki_file = wiki_dir / f"{key}.md"
    wiki_file.write_text(
        WIKI_TEMPLATE.format(
            id="00000000-0000-0000-0000-000000000000",
            key=key,
            content=content,
            scope=scope,
            memory_type=memory_type,
            tags=tags,
        ),
        encoding="utf-8",
    )
    return wiki_file


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def svc(tmp_path):
    """MemoryService with tmp_path as base_dir and test DB engine."""
    from sqlalchemy import create_engine

    from cli_agent_orchestrator.clients.database import Base

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    service = MemoryService(base_dir=tmp_path, db_engine=eng)
    return service


# ---------------------------------------------------------------------------
# U6.1 — rank-bm25 dependency
# ---------------------------------------------------------------------------


class TestBm25DependencyAvailable:
    def test_rank_bm25_importable(self):
        from rank_bm25 import BM25Okapi

        assert BM25Okapi is not None


# ---------------------------------------------------------------------------
# U6.2 — BM25 finds content not in key/tags
# ---------------------------------------------------------------------------


class TestBm25ContentSearch:
    def test_bm25_finds_content_match(self, svc, tmp_path):
        """BM25 should find a memory where query matches content but not key/tags."""
        # Create wiki file with key "db-config" but content mentions "pytest"
        make_wiki_file(
            tmp_path,
            key="db-config",
            content="Always use pytest for running test suites in this project.",
            scope="project",
            tags="database",
        )

        # Create global dir too
        (tmp_path / "global" / "wiki").mkdir(parents=True, exist_ok=True)

        results = run_async(
            svc.recall(
                query="pytest",
                search_mode="bm25",
                terminal_context=None,
                scan_all=True,
            )
        )

        assert len(results) >= 1
        assert results[0].key == "db-config"

    def test_bm25_excludes_zero_score(self, svc, tmp_path):
        """Files with no query match should not appear."""
        make_wiki_file(
            tmp_path,
            key="unrelated",
            content="This file has nothing to do with the search query.",
        )
        (tmp_path / "global" / "wiki").mkdir(parents=True, exist_ok=True)

        results = run_async(
            svc.recall(
                query="kubernetes",
                search_mode="bm25",
                terminal_context=None,
                scan_all=True,
            )
        )

        assert len(results) == 0

    def test_bm25_respects_scope_filter(self, svc, tmp_path):
        """BM25 should only search files in the requested scope."""
        make_wiki_file(tmp_path, key="in-scope", content="pytest rocks", scope="project")
        # Create a global-scope file
        global_dir = tmp_path / "global" / "wiki" / "global"
        global_dir.mkdir(parents=True, exist_ok=True)
        (global_dir / "out-scope.md").write_text(
            WIKI_TEMPLATE.format(
                id="11111111-1111-1111-1111-111111111111",
                key="out-scope",
                content="pytest is everywhere",
                scope="global",
                memory_type="project",
                tags="",
            ),
            encoding="utf-8",
        )

        results = run_async(
            svc.recall(
                query="pytest",
                scope="project",
                search_mode="bm25",
                terminal_context=None,
                scan_all=True,
            )
        )

        keys = [m.key for m in results]
        assert "in-scope" in keys
        assert "out-scope" not in keys


# ---------------------------------------------------------------------------
# U6.3 — search_mode parameter
# ---------------------------------------------------------------------------


class TestSearchMode:
    def test_invalid_search_mode_raises(self, svc):
        with pytest.raises(ValueError, match="Invalid search_mode"):
            run_async(svc.recall(query="test", search_mode="invalid"))

    def test_metadata_mode_skips_bm25(self, svc, tmp_path):
        """metadata mode should not invoke BM25 even if content matches."""
        make_wiki_file(
            tmp_path,
            key="hidden-gem",
            content="pytest is the best testing framework",
            tags="",
        )
        (tmp_path / "global" / "wiki").mkdir(parents=True, exist_ok=True)

        # metadata mode: query "pytest" won't match key "hidden-gem" or empty tags
        results = run_async(
            svc.recall(
                query="pytest",
                search_mode="metadata",
                terminal_context=None,
                scan_all=True,
            )
        )

        # SQLite has no rows, so metadata returns nothing
        assert len(results) == 0

    def test_hybrid_mode_merges_results(self, svc, tmp_path):
        """Hybrid should return SQLite matches + BM25 fill."""
        # Store one via service (goes to SQLite + wiki)
        run_async(
            svc.store(
                content="Use pytest-xdist for parallel tests",
                key="parallel-testing",
                scope="project",
                memory_type="project",
                tags="pytest,testing",
                terminal_context=None,
            )
        )
        # Create another wiki file NOT in SQLite (simulates legacy)
        make_wiki_file(
            tmp_path,
            key="legacy-tip",
            content="pytest fixtures are powerful for test setup",
            scope="project",
            tags="",
            scope_id="global",
        )

        results = run_async(
            svc.recall(
                query="pytest",
                search_mode="hybrid",
                terminal_context=None,
                scan_all=True,
            )
        )

        keys = [m.key for m in results]
        # Should find both: SQLite match + BM25 content match
        assert "parallel-testing" in keys
        assert "legacy-tip" in keys


# ---------------------------------------------------------------------------
# Graceful fallback when rank-bm25 not installed
# ---------------------------------------------------------------------------


class TestBm25GracefulFallback:
    def test_returns_empty_when_not_installed(self, svc, tmp_path):
        """If rank-bm25 is not importable, _bm25_search returns []."""
        make_wiki_file(tmp_path, key="test", content="pytest content")
        (tmp_path / "global" / "wiki").mkdir(parents=True, exist_ok=True)

        with patch.dict("sys.modules", {"rank_bm25": None}):
            results = svc._bm25_search(
                query="pytest",
                scope=None,
                scope_id=None,
                memory_type=None,
                limit=10,
                exclude_keys=set(),
                terminal_context=None,
                scan_all=True,
            )

        assert results == []


# ---------------------------------------------------------------------------
# U6.4 — MCP tool accepts search_mode
# ---------------------------------------------------------------------------


class TestMcpRecallSearchMode:
    @patch("cli_agent_orchestrator.mcp_server.server._get_terminal_context_from_env")
    @patch("cli_agent_orchestrator.services.memory_service.MemoryService")
    def test_mcp_recall_passes_search_mode(self, MockService, mock_ctx):
        from cli_agent_orchestrator.mcp_server.server import memory_recall

        mock_ctx.return_value = None
        instance = MockService.return_value
        instance.recall = AsyncMock(return_value=[])

        result = run_async(memory_recall(query="test", search_mode="bm25"))

        call_kwargs = instance.recall.call_args.kwargs
        assert call_kwargs["search_mode"] == "bm25"

    @patch("cli_agent_orchestrator.mcp_server.server._get_terminal_context_from_env")
    @patch("cli_agent_orchestrator.services.memory_service.MemoryService")
    def test_mcp_recall_defaults_to_hybrid(self, MockService, mock_ctx):
        from cli_agent_orchestrator.mcp_server.server import memory_recall

        mock_ctx.return_value = None
        instance = MockService.return_value
        instance.recall = AsyncMock(return_value=[])

        result = run_async(memory_recall(query="test"))

        call_kwargs = instance.recall.call_args.kwargs
        assert call_kwargs["search_mode"] == "hybrid"
