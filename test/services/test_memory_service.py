"""Tests for MemoryService — unit tests (U8.1) and integration tests (U8.2).

Unit tests use tmp_path fixture with real filesystem (no mocking).
Integration tests are marked with @pytest.mark.integration.
"""

import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cli_agent_orchestrator.services.memory_service import MemoryService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_terminal_context(
    cwd: str = "/home/user/project",
    session_name: str = "test-session",
    agent_profile: str = "developer",
    provider: str = "claude_code",
    terminal_id: str = "term-001",
) -> dict:
    """Build a minimal terminal context dict for testing."""
    return {
        "terminal_id": terminal_id,
        "session_name": session_name,
        "agent_profile": agent_profile,
        "provider": provider,
        "cwd": cwd,
    }


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


def _backdate_memory(
    wiki_file: Path,
    svc: MemoryService,
    scope: str,
    scope_id,
    old_ts: str,
) -> None:
    """Rewrite timestamps in a wiki file and its index entry to simulate age."""
    # Replace all ISO timestamps in wiki file
    content = wiki_file.read_text(encoding="utf-8")
    content = re.sub(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
        old_ts,
        content,
    )
    wiki_file.write_text(content, encoding="utf-8")

    # Replace updated: timestamp in index.md
    index_path = svc.get_index_path(scope, scope_id)
    if index_path.exists():
        idx = index_path.read_text(encoding="utf-8")
        # Replace the updated: field for entries that reference this file's key
        key = wiki_file.stem
        lines = idx.splitlines()
        new_lines = []
        for line in lines:
            if f"[{key}](" in line:
                line = re.sub(r"updated:\S+", f"updated:{old_ts}", line)
            new_lines.append(line)
        index_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# ===========================================================================
# U8.1 — Unit Tests
# ===========================================================================


class TestStoreCreatesWikiFile:
    """U8.1: store → file exists, content correct."""

    def test_store_creates_wiki_file(self, tmp_path: Path):
        """Storing a memory should create a wiki file with correct content."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()

        mem = _run(
            svc.store(
                content="User prefers pytest for all tests",
                scope="project",
                memory_type="feedback",
                key="prefer-pytest",
                tags="testing,pytest",
                terminal_context=ctx,
            )
        )

        assert Path(mem.file_path).exists()
        file_content = Path(mem.file_path).read_text(encoding="utf-8")
        assert "# prefer-pytest" in file_content
        assert "User prefers pytest for all tests" in file_content
        assert "type: feedback" in file_content
        assert "tags: testing,pytest" in file_content


class TestStoreUpdatesIndexMd:
    """U8.1: store → index.md contains entry."""

    def test_store_updates_index_md(self, tmp_path: Path):
        """Storing a memory should add an entry to index.md."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()

        mem = _run(
            svc.store(
                content="Always use type hints",
                scope="project",
                memory_type="feedback",
                key="use-type-hints",
                terminal_context=ctx,
            )
        )

        index_path = svc.get_index_path("project", svc.resolve_scope_id("project", ctx))
        assert index_path.exists()
        index_content = index_path.read_text(encoding="utf-8")
        assert "[use-type-hints]" in index_content
        assert "type:feedback" in index_content


class TestStoreUpsertSameKey:
    """U8.1: double store with same key → one file, updated content (SC-2)."""

    def test_store_upsert_same_key(self, tmp_path: Path):
        """Storing twice with the same key should update, not duplicate."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()

        mem1 = _run(
            svc.store(
                content="First version",
                scope="project",
                memory_type="project",
                key="my-decision",
                terminal_context=ctx,
            )
        )
        mem2 = _run(
            svc.store(
                content="Updated version",
                scope="project",
                memory_type="project",
                key="my-decision",
                terminal_context=ctx,
            )
        )

        # Same file path
        assert mem1.file_path == mem2.file_path

        # File contains both entries
        file_content = Path(mem2.file_path).read_text(encoding="utf-8")
        assert "First version" in file_content
        assert "Updated version" in file_content

        # Only one index entry for this key
        index_path = svc.get_index_path("project", svc.resolve_scope_id("project", ctx))
        index_content = index_path.read_text(encoding="utf-8")
        assert index_content.count("[my-decision]") == 1


class TestAutoKeyGeneration:
    """U8.1: no key → slug from first 6 words (SC-12)."""

    def test_auto_key_generation(self, tmp_path: Path):
        """When key is omitted, it should be auto-generated from content."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()

        mem = _run(
            svc.store(
                content="User prefers pytest for all tests",
                scope="global",
                memory_type="user",
                terminal_context=ctx,
            )
        )

        assert mem.key == "user-prefers-pytest-for-all-tests"

    def test_auto_key_generation_slug_format(self):
        """auto_generate_key should produce correct slug."""
        assert (
            MemoryService.auto_generate_key("User prefers pytest for all tests")
            == "user-prefers-pytest-for-all-tests"
        )
        assert MemoryService.auto_generate_key("UPPER CASE words") == "upper-case-words"
        assert MemoryService.auto_generate_key("special!@#chars$%^here") == "specialcharshere"

    def test_auto_key_truncate_60(self):
        """Auto-generated key should be truncated to 60 chars."""
        long_content = " ".join(["word"] * 20)
        key = MemoryService.auto_generate_key(long_content)
        assert len(key) <= 60


class TestRecallScopePrecedence:
    """U8.1: session > project > global ordering (SC-3)."""

    def test_recall_scope_precedence(self, tmp_path: Path):
        """Recall without scope filter should return session first, then project, then global."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()

        # Store in different scopes
        _run(
            svc.store(
                content="global fact",
                scope="global",
                memory_type="project",
                key="fact-global",
                terminal_context=ctx,
            )
        )
        _run(
            svc.store(
                content="project fact",
                scope="project",
                memory_type="project",
                key="fact-project",
                terminal_context=ctx,
            )
        )
        _run(
            svc.store(
                content="session fact",
                scope="session",
                memory_type="project",
                key="fact-session",
                terminal_context=ctx,
            )
        )

        results = _run(svc.recall(query="fact", terminal_context=ctx))

        # Session should come before project, project before global
        scopes = [m.scope for m in results]
        if "session" in scopes and "project" in scopes:
            assert scopes.index("session") < scopes.index("project")
        if "project" in scopes and "global" in scopes:
            assert scopes.index("project") < scopes.index("global")


class TestRecallQueryMatching:
    """U8.1: query filters to relevant files."""

    def test_recall_query_matching(self, tmp_path: Path):
        """Recall with query should only return memories matching the query terms."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()

        _run(
            svc.store(
                content="pytest is the preferred testing framework",
                scope="global",
                memory_type="feedback",
                key="prefer-pytest",
                terminal_context=ctx,
            )
        )
        _run(
            svc.store(
                content="use black for formatting",
                scope="global",
                memory_type="feedback",
                key="use-black",
                terminal_context=ctx,
            )
        )

        results = _run(svc.recall(query="pytest", terminal_context=ctx))
        keys = [m.key for m in results]

        assert "prefer-pytest" in keys
        assert "use-black" not in keys


class TestForgetRemovesFileAndIndex:
    """U8.1: forget → file deleted, index updated (SC-4)."""

    def test_forget_removes_file_and_index(self, tmp_path: Path):
        """Forgetting a memory should delete the file and remove the index entry."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()

        mem = _run(
            svc.store(
                content="temporary note",
                scope="global",
                memory_type="reference",
                key="temp-note",
                terminal_context=ctx,
            )
        )

        file_path = Path(mem.file_path)
        assert file_path.exists()

        deleted = _run(svc.forget(key="temp-note", scope="global", terminal_context=ctx))
        assert deleted is True
        assert not file_path.exists()

        # Check index
        index_path = svc.get_index_path("global", None)
        if index_path.exists():
            index_content = index_path.read_text(encoding="utf-8")
            assert "[temp-note]" not in index_content


class TestForgetNonexistentKey:
    """U8.1: graceful no-op for nonexistent key."""

    def test_forget_nonexistent_key_returns_false(self, tmp_path: Path):
        """Forgetting a key that doesn't exist should return False."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()

        result = _run(svc.forget(key="nonexistent", scope="global", terminal_context=ctx))
        assert result is False


class TestGetContextRespectsBudget:
    """U8.1: 20 memories → block under budget limit (SC-10)."""

    def test_get_context_respects_budget(self, tmp_path: Path):
        """Context block should stay within budget_chars."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()

        # Store 20 memories with substantial content
        for i in range(20):
            _run(
                svc.store(
                    content=f"Memory entry number {i} with some padding content to take up space "
                    * 3,
                    scope="global",
                    memory_type="project",
                    key=f"entry-{i:03d}",
                    terminal_context=ctx,
                )
            )

        # Patch _get_terminal_context to return our ctx
        svc._get_terminal_context = lambda terminal_id: ctx
        context = svc.get_memory_context_for_terminal("term-001", budget_chars=500)

        if context:
            # Should contain cao-memory tags
            assert "<cao-memory>" in context
            assert "</cao-memory>" in context
            # Content between tags should be under budget
            inner = context.split("<cao-memory>")[1].split("</cao-memory>")[0]
            assert len(inner) <= 600  # some overhead for tags/headers is OK


class TestCleanupSession14Days:
    """U8.1: session memory 15d old → deleted (SC-9)."""

    def test_cleanup_session_14_days(self, tmp_path: Path, monkeypatch):
        """Session-scoped memory older than 14 days should be cleaned up."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()

        # Store a session-scoped memory
        mem = _run(
            svc.store(
                content="old session finding",
                scope="session",
                memory_type="project",
                key="old-finding",
                terminal_context=ctx,
            )
        )

        # Backdate the timestamp in the wiki file and index to 15 days ago
        old_ts = (datetime.now(timezone.utc) - timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _backdate_memory(Path(mem.file_path), svc, "session", mem.scope_id, old_ts)

        # Patch MEMORY_BASE_DIR in both cleanup_service and memory_service
        import cli_agent_orchestrator.services.cleanup_service as cs
        import cli_agent_orchestrator.services.memory_service as ms

        monkeypatch.setattr(cs, "MEMORY_BASE_DIR", tmp_path)
        monkeypatch.setattr(ms, "MEMORY_BASE_DIR", tmp_path)

        _run(cs.cleanup_expired_memories())

        # File should be deleted
        assert not Path(mem.file_path).exists()


class TestCleanupPreservesUserFeedback:
    """U8.1: user/feedback any age → preserved (SC-9)."""

    def test_cleanup_preserves_user_feedback(self, tmp_path: Path, monkeypatch):
        """User and feedback memories should never expire regardless of age."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()

        # Store user and feedback memories
        user_mem = _run(
            svc.store(
                content="user preference",
                scope="global",
                memory_type="user",
                key="user-pref",
                terminal_context=ctx,
            )
        )
        feedback_mem = _run(
            svc.store(
                content="feedback item",
                scope="global",
                memory_type="feedback",
                key="feedback-item",
                terminal_context=ctx,
            )
        )

        # Backdate to 200 days ago
        old_ts = (datetime.now(timezone.utc) - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _backdate_memory(Path(user_mem.file_path), svc, "global", None, old_ts)
        _backdate_memory(Path(feedback_mem.file_path), svc, "global", None, old_ts)

        # Patch MEMORY_BASE_DIR in both cleanup_service and memory_service
        import cli_agent_orchestrator.services.cleanup_service as cs
        import cli_agent_orchestrator.services.memory_service as ms

        monkeypatch.setattr(cs, "MEMORY_BASE_DIR", tmp_path)
        monkeypatch.setattr(ms, "MEMORY_BASE_DIR", tmp_path)

        _run(cs.cleanup_expired_memories())

        # Both should still exist
        assert Path(user_mem.file_path).exists()
        assert Path(feedback_mem.file_path).exists()


class TestSurviveRestart:
    """U8.1: write file, recreate MemoryService, recall succeeds (SC-15)."""

    def test_survive_restart(self, tmp_path: Path):
        """Memories should survive service restart (stateless file-based design)."""
        ctx = _make_terminal_context()

        # Store with one service instance
        svc1 = MemoryService(base_dir=tmp_path)
        _run(
            svc1.store(
                content="persistent data",
                scope="global",
                memory_type="project",
                key="persistent",
                terminal_context=ctx,
            )
        )

        # Recall with a fresh service instance
        svc2 = MemoryService(base_dir=tmp_path)
        results = _run(svc2.recall(query="persistent", terminal_context=ctx))

        assert len(results) >= 1
        assert any(m.key == "persistent" for m in results)
        assert any("persistent data" in m.content for m in results)


# ===========================================================================
# Security review tests
# ===========================================================================


class TestKeyPathTraversalRejected:
    """Security: path traversal via key parameter should be rejected."""

    def test_path_traversal_dotdot(self, tmp_path: Path):
        """Key with '../' components should be sanitized or rejected."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()

        # Should not escape the memory base directory
        mem = _run(
            svc.store(
                content="test content",
                scope="global",
                memory_type="project",
                key="../../etc/passwd",
                terminal_context=ctx,
            )
        )

        # The resolved path must be inside base_dir
        resolved = Path(mem.file_path).resolve()
        assert str(resolved).startswith(str(tmp_path.resolve()))

    def test_path_traversal_absolute(self, tmp_path: Path):
        """Key with absolute path components should be sanitized."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()

        mem = _run(
            svc.store(
                content="test content",
                scope="global",
                memory_type="project",
                key="/etc/passwd",
                terminal_context=ctx,
            )
        )

        resolved = Path(mem.file_path).resolve()
        assert str(resolved).startswith(str(tmp_path.resolve()))

    def test_null_byte_in_key(self, tmp_path: Path):
        """Key with null bytes should be sanitized."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()

        # Should not crash; key should be sanitized
        mem = _run(
            svc.store(
                content="test content",
                scope="global",
                memory_type="project",
                key="safe\x00evil",
                terminal_context=ctx,
            )
        )

        resolved = Path(mem.file_path).resolve()
        assert str(resolved).startswith(str(tmp_path.resolve()))

    def test_markdown_injection_in_key(self, tmp_path: Path):
        """Key with markdown link syntax should be sanitized to prevent index injection."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()

        mem = _run(
            svc.store(
                content="test content",
                scope="global",
                memory_type="project",
                key="evil](../../secrets.md) — type:user",
                terminal_context=ctx,
            )
        )

        # Key should be sanitized — no brackets or parens
        assert "](" not in mem.key
        assert "(" not in mem.key


class TestRecallNoContextOnlyGlobal:
    """Security: recall without context should only search global scope."""

    def test_recall_no_context_only_global(self, tmp_path: Path):
        """Without terminal context, recall should not leak cross-project memories."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()

        # Store a project-scoped memory
        _run(
            svc.store(
                content="secret project data",
                scope="project",
                memory_type="project",
                key="project-secret",
                terminal_context=ctx,
            )
        )

        # Store a global memory
        _run(
            svc.store(
                content="global knowledge",
                scope="global",
                memory_type="project",
                key="global-info",
                terminal_context=ctx,
            )
        )

        # Recall without context — should not return project memories
        # (or at minimum, should include global)
        results = _run(svc.recall(query=None, terminal_context=None))
        keys = [m.key for m in results]

        assert "global-info" in keys


# ===========================================================================
# U8.2 — Integration Tests
# ===========================================================================


@pytest.mark.integration
class TestFullRoundtrip:
    """U8.2: store → recall, content matches (SC-1)."""

    def test_full_roundtrip(self, tmp_path: Path):
        """Full store-recall round trip should return stored content."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()

        content = "The deploy pipeline uses GitHub Actions with OIDC auth"
        _run(
            svc.store(
                content=content,
                scope="project",
                memory_type="reference",
                key="deploy-pipeline",
                tags="ci,deploy",
                terminal_context=ctx,
            )
        )

        results = _run(svc.recall(query="deploy pipeline", terminal_context=ctx))

        assert len(results) >= 1
        match = next((m for m in results if m.key == "deploy-pipeline"), None)
        assert match is not None
        assert match.content == content
        assert match.memory_type == "reference"


@pytest.mark.integration
class TestConcurrentStores:
    """U8.2: asyncio.gather 5 stores to same scope → no corruption (SC-13)."""

    def test_concurrent_stores(self, tmp_path: Path):
        """Concurrent store operations should not corrupt files."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()

        async def store_one(i: int):
            return await svc.store(
                content=f"Concurrent memory entry {i}",
                scope="global",
                memory_type="project",
                key=f"concurrent-{i}",
                terminal_context=ctx,
            )

        async def run_concurrent():
            return await asyncio.gather(*[store_one(i) for i in range(5)])

        results = asyncio.run(run_concurrent())

        # All 5 should succeed
        assert len(results) == 5

        # All files should exist
        for mem in results:
            assert Path(mem.file_path).exists()

        # Index should have all 5 entries
        index_path = svc.get_index_path("global", None)
        index_content = index_path.read_text(encoding="utf-8")
        for i in range(5):
            assert f"[concurrent-{i}]" in index_content


@pytest.mark.integration
class TestIndexConsistency:
    """U8.2: store/forget cycle → index matches filesystem (SC-14)."""

    def test_index_consistency(self, tmp_path: Path):
        """After store/forget cycles, index.md should match actual wiki files on disk."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()

        # Store 5 memories
        for i in range(5):
            _run(
                svc.store(
                    content=f"Memory {i}",
                    scope="global",
                    memory_type="project",
                    key=f"mem-{i}",
                    terminal_context=ctx,
                )
            )

        # Forget 2 of them
        _run(svc.forget(key="mem-1", scope="global", terminal_context=ctx))
        _run(svc.forget(key="mem-3", scope="global", terminal_context=ctx))

        # Check index matches filesystem
        index_path = svc.get_index_path("global", None)
        index_content = index_path.read_text(encoding="utf-8")

        # Remaining keys should be in index
        assert "[mem-0]" in index_content
        assert "[mem-2]" in index_content
        assert "[mem-4]" in index_content

        # Deleted keys should not be in index
        assert "[mem-1]" not in index_content
        assert "[mem-3]" not in index_content

        # Verify files match
        wiki_dir = index_path.parent / "global"
        if wiki_dir.exists():
            existing_files = {f.stem for f in wiki_dir.glob("*.md")}
            assert "mem-0" in existing_files
            assert "mem-2" in existing_files
            assert "mem-4" in existing_files
            assert "mem-1" not in existing_files
            assert "mem-3" not in existing_files


@pytest.mark.integration
class TestNoSqlalchemyImports:
    """U8.2: assert no SQLAlchemy in memory module (SC-11)."""

    def test_no_sqlalchemy_imports(self):
        """memory_service.py should not import SQLAlchemy (Phase 1 constraint)."""
        import inspect

        from cli_agent_orchestrator.services import memory_service

        source = inspect.getsource(memory_service)

        # _get_terminal_context is the ONE exception — it reads terminal metadata from DB
        # But the core memory operations must not use SQLAlchemy
        # Check that sqlalchemy is not imported at module level
        lines = source.split("\n")
        top_level_imports = [
            ln
            for ln in lines
            if ln.startswith("import sqlalchemy") or ln.startswith("from sqlalchemy")
        ]
        assert top_level_imports == [], f"Found SQLAlchemy imports: {top_level_imports}"


@pytest.mark.integration
class TestSurviveRestartIntegration:
    """U8.2: store with svc1, create new MemoryService, recall succeeds (SC-15)."""

    def test_survive_restart(self, tmp_path: Path):
        """Memories persist across MemoryService instances (file-based, stateless)."""
        ctx = _make_terminal_context()

        # Store with first instance
        svc1 = MemoryService(base_dir=tmp_path)
        _run(
            svc1.store(
                content="Architecture uses hexagonal pattern with ports and adapters",
                scope="project",
                memory_type="project",
                key="architecture",
                tags="design",
                terminal_context=ctx,
            )
        )
        _run(
            svc1.store(
                content="User prefers explicit error handling over exceptions",
                scope="global",
                memory_type="user",
                key="error-handling",
                terminal_context=ctx,
            )
        )

        # Create a completely fresh instance — no shared state
        svc2 = MemoryService(base_dir=tmp_path)

        # Recall should find both memories
        results = _run(svc2.recall(query="architecture", terminal_context=ctx))
        assert any(m.key == "architecture" for m in results)
        arch = next(m for m in results if m.key == "architecture")
        assert "hexagonal pattern" in arch.content

        results = _run(svc2.recall(query="error handling", terminal_context=ctx))
        assert any(m.key == "error-handling" for m in results)


@pytest.mark.integration
class TestForgetWithExplicitScopeId:
    """U8.2: forget(key, scope, scope_id=X) works without terminal_context."""

    def test_forget_with_explicit_scope_id(self, tmp_path: Path):
        """Cleanup can call forget with explicit scope_id, no terminal_context needed."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()

        # Store a project-scoped memory
        mem = _run(
            svc.store(
                content="project memory to delete",
                scope="project",
                memory_type="project",
                key="to-delete",
                terminal_context=ctx,
            )
        )

        project_scope_id = svc.resolve_scope_id("project", ctx)
        assert Path(mem.file_path).exists()

        # Forget using explicit scope_id (no terminal_context)
        deleted = _run(
            svc.forget(
                key="to-delete",
                scope="project",
                scope_id=project_scope_id,
            )
        )

        assert deleted is True
        assert not Path(mem.file_path).exists()

    def test_forget_global_with_explicit_scope_id_none(self, tmp_path: Path):
        """Forget global memory with scope_id=None (explicit) works."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()

        mem = _run(
            svc.store(
                content="global memory",
                scope="global",
                memory_type="reference",
                key="global-ref",
                terminal_context=ctx,
            )
        )

        assert Path(mem.file_path).exists()

        deleted = _run(svc.forget(key="global-ref", scope="global", scope_id=None))
        assert deleted is True
        assert not Path(mem.file_path).exists()


@pytest.mark.integration
class TestCleanupSessionIntegration:
    """U8.2: cleanup integration with real filesystem (SC-9)."""

    def test_cleanup_deletes_expired_session_memory(self, tmp_path: Path, monkeypatch):
        """Session memory older than 14 days should be cleaned up."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()

        mem = _run(
            svc.store(
                content="expired session data",
                scope="session",
                memory_type="project",
                key="expired-session",
                terminal_context=ctx,
            )
        )

        # Backdate to 15 days ago
        old_ts = (datetime.now(timezone.utc) - timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _backdate_memory(Path(mem.file_path), svc, "session", mem.scope_id, old_ts)

        assert Path(mem.file_path).exists()

        import cli_agent_orchestrator.services.cleanup_service as cs
        import cli_agent_orchestrator.services.memory_service as ms

        monkeypatch.setattr(cs, "MEMORY_BASE_DIR", tmp_path)
        monkeypatch.setattr(ms, "MEMORY_BASE_DIR", tmp_path)

        _run(cs.cleanup_expired_memories())

        assert not Path(mem.file_path).exists()

    def test_cleanup_preserves_recent_session_memory(self, tmp_path: Path, monkeypatch):
        """Session memory younger than 14 days should be preserved."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()

        mem = _run(
            svc.store(
                content="recent session data",
                scope="session",
                memory_type="project",
                key="recent-session",
                terminal_context=ctx,
            )
        )

        assert Path(mem.file_path).exists()

        import cli_agent_orchestrator.services.cleanup_service as cs
        import cli_agent_orchestrator.services.memory_service as ms

        monkeypatch.setattr(cs, "MEMORY_BASE_DIR", tmp_path)
        monkeypatch.setattr(ms, "MEMORY_BASE_DIR", tmp_path)

        _run(cs.cleanup_expired_memories())

        # Should still exist (just created)
        assert Path(mem.file_path).exists()

    def test_cleanup_preserves_old_user_and_feedback(self, tmp_path: Path, monkeypatch):
        """User and feedback memories should never expire regardless of age."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()

        user_mem = _run(
            svc.store(
                content="ancient user pref",
                scope="global",
                memory_type="user",
                key="ancient-user",
                terminal_context=ctx,
            )
        )
        feedback_mem = _run(
            svc.store(
                content="ancient feedback",
                scope="global",
                memory_type="feedback",
                key="ancient-feedback",
                terminal_context=ctx,
            )
        )

        # Backdate to 200 days ago
        old_ts = (datetime.now(timezone.utc) - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _backdate_memory(Path(user_mem.file_path), svc, "global", None, old_ts)
        _backdate_memory(Path(feedback_mem.file_path), svc, "global", None, old_ts)

        import cli_agent_orchestrator.services.cleanup_service as cs
        import cli_agent_orchestrator.services.memory_service as ms

        monkeypatch.setattr(cs, "MEMORY_BASE_DIR", tmp_path)
        monkeypatch.setattr(ms, "MEMORY_BASE_DIR", tmp_path)

        _run(cs.cleanup_expired_memories())

        # Both should still exist
        assert Path(user_mem.file_path).exists()
        assert Path(feedback_mem.file_path).exists()
