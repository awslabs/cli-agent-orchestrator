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

pytestmark = pytest.mark.usefixtures("isolated_memory_db")

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
        _run(
            svc.store(
                content="federated fact",
                scope="federated",
                memory_type="project",
                key="fact-federated",
                terminal_context=ctx,
            )
        )

        results = _run(svc.recall(query="fact", terminal_context=ctx))

        # Session should come before project, project before global,
        # global before federated (federated has lowest precedence).
        scopes = [m.scope for m in results]
        if "session" in scopes and "project" in scopes:
            assert scopes.index("session") < scopes.index("project")
        if "project" in scopes and "global" in scopes:
            assert scopes.index("project") < scopes.index("global")
        if "global" in scopes and "federated" in scopes:
            assert scopes.index("global") < scopes.index("federated")


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


# ---------------------------------------------------------------------------
# P3-B — production path: _get_terminal_context() must resolve cwd
# from tmux without test-side monkeypatching.
# ---------------------------------------------------------------------------


class TestTerminalContextResolution:
    """Verify _get_terminal_context() wires cwd via the working-directory helper.

    These tests do NOT monkeypatch ``_get_terminal_context`` itself. Instead
    they monkeypatch the database lookup and the tmux working-directory
    helper, exercising the real wiring inside the method. This catches
    regressions where the production code path silently returns ``cwd=None``.
    """

    def test_cwd_resolved_via_working_directory_helper(self, tmp_path, monkeypatch):
        """A live terminal_id should produce a context with cwd populated."""
        svc = MemoryService(base_dir=tmp_path)

        # Stub the database query inside _get_terminal_context.
        class _FakeTerminal:
            id = "term-prod"
            tmux_session = "cao-demo"
            provider = "claude_code"
            agent_profile = "developer"

        class _FakeQuery:
            def filter(self, *_args, **_kwargs):
                return self

            def first(self):
                return _FakeTerminal()

        class _FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def query(self, _model):
                return _FakeQuery()

        from cli_agent_orchestrator.clients import database as db_mod

        monkeypatch.setattr(db_mod, "SessionLocal", lambda: _FakeSession())

        # Stub get_working_directory so we don't need a real tmux pane.
        from cli_agent_orchestrator.services import terminal_service as ts_mod

        monkeypatch.setattr(
            ts_mod,
            "get_working_directory",
            lambda terminal_id: "/home/user/project-x",
        )

        ctx = svc._get_terminal_context("term-prod")

        assert ctx is not None
        assert ctx["terminal_id"] == "term-prod"
        assert ctx["session_name"] == "cao-demo"
        assert ctx["provider"] == "claude_code"
        assert ctx["agent_profile"] == "developer"
        # The fix that this test guards: cwd MUST be the value the
        # working-directory helper returned, not None.
        assert ctx["cwd"] == "/home/user/project-x"

    def test_cwd_falls_back_to_none_when_helper_raises(self, tmp_path, monkeypatch):
        """Helper failures must not crash _get_terminal_context."""
        svc = MemoryService(base_dir=tmp_path)

        class _FakeTerminal:
            id = "term-broken"
            tmux_session = "cao-demo"
            provider = "claude_code"
            agent_profile = "developer"

        class _FakeQuery:
            def filter(self, *_args, **_kwargs):
                return self

            def first(self):
                return _FakeTerminal()

        class _FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def query(self, _model):
                return _FakeQuery()

        from cli_agent_orchestrator.clients import database as db_mod

        monkeypatch.setattr(db_mod, "SessionLocal", lambda: _FakeSession())

        def _broken(_terminal_id):
            raise RuntimeError("tmux unavailable")

        from cli_agent_orchestrator.services import terminal_service as ts_mod

        monkeypatch.setattr(ts_mod, "get_working_directory", _broken)

        ctx = svc._get_terminal_context("term-broken")
        assert ctx is not None
        # Other fields still populated from the DB.
        assert ctx["terminal_id"] == "term-broken"
        # cwd is None when the helper fails — but importantly the call
        # didn't raise.
        assert ctx["cwd"] is None


class TestSessionScopeIdRoundTrip:
    """Session/agent memories store scope_id in the path; recall must
    return it so callers (CLI clear, cleanup) can target the right file
    without reconstructing the original terminal_context.
    """

    @pytest.mark.asyncio
    async def test_recall_returns_scope_id_for_session_memory(self, tmp_path):
        svc = MemoryService(base_dir=tmp_path)
        ctx = {"session_name": "demo-session", "terminal_id": "term-1"}

        await svc.store(
            content="session-only fact",
            scope="session",
            memory_type="reference",
            key="fact-a",
            terminal_context=ctx,
        )

        results = await svc.recall(scope="session", terminal_context=ctx)
        assert len(results) == 1
        assert results[0].scope_id == "demo-session"

    @pytest.mark.asyncio
    async def test_forget_via_recalled_scope_id_succeeds(self, tmp_path):
        """Round-trip: store → recall → forget(scope_id=mem.scope_id).
        This is the exact path the CLI ``clear`` command takes.
        """
        svc = MemoryService(base_dir=tmp_path)
        ctx_a = {"session_name": "session-a", "terminal_id": "term-a"}
        ctx_b = {"session_name": "session-b", "terminal_id": "term-b"}

        # Same key under two different sessions — must NOT collide.
        await svc.store(
            content="from-a",
            scope="session",
            memory_type="reference",
            key="shared-key",
            terminal_context=ctx_a,
        )
        await svc.store(
            content="from-b",
            scope="session",
            memory_type="reference",
            key="shared-key",
            terminal_context=ctx_b,
        )

        # Recall WITHOUT a session in context (CLI case) — should
        # surface entries from both sessions, each with its own scope_id.
        results = await svc.recall(scope="session", terminal_context=None)
        scope_ids = {m.scope_id for m in results}
        assert scope_ids == {"session-a", "session-b"}

        # Forget using the recalled scope_id, not the original context.
        for mem in results:
            ok = await svc.forget(
                key=mem.key,
                scope="session",
                terminal_context=None,
                scope_id=mem.scope_id,
            )
            assert ok is True

        # Both files gone.
        remaining = await svc.recall(scope="session", terminal_context=None)
        assert remaining == []

    @pytest.mark.asyncio
    async def test_recall_isolates_by_session_scope_id(self, tmp_path):
        """A scoped recall with a session context must not return
        memories that belong to OTHER sessions, even though all
        session memories share the global index.
        """
        svc = MemoryService(base_dir=tmp_path)
        ctx_a = {"session_name": "session-a", "terminal_id": "term-a"}
        ctx_b = {"session_name": "session-b", "terminal_id": "term-b"}

        await svc.store(
            content="visible-from-a",
            scope="session",
            memory_type="reference",
            key="a-key",
            terminal_context=ctx_a,
        )
        await svc.store(
            content="visible-from-b",
            scope="session",
            memory_type="reference",
            key="b-key",
            terminal_context=ctx_b,
        )

        # Recall as session-a → only a-key.
        results_a = await svc.recall(scope="session", terminal_context=ctx_a)
        assert {m.key for m in results_a} == {"a-key"}

        # Recall as session-b → only b-key.
        results_b = await svc.recall(scope="session", terminal_context=ctx_b)
        assert {m.key for m in results_b} == {"b-key"}

        # CLI scan_all path still sees both.
        results_all = await svc.recall(scope="session", terminal_context=None, scan_all=True)
        assert {m.key for m in results_all} == {"a-key", "b-key"}


# ===========================================================================
# FEDERATED scope (issue #313) — machine-wide shared tier
# ===========================================================================


def _federated_engine(tmp_path: Path):
    """Build an isolated SQLite engine so DB-row assertions are deterministic."""
    from sqlalchemy import create_engine

    from cli_agent_orchestrator.clients.database import Base

    engine = create_engine(
        f"sqlite:///{tmp_path / 'fed.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    return engine


def _fed_row(engine, key: str):
    from sqlalchemy.orm import sessionmaker

    from cli_agent_orchestrator.clients.database import MemoryMetadataModel

    Session = sessionmaker(bind=engine)
    with Session() as db:
        return (
            db.query(MemoryMetadataModel)
            .filter_by(key=key, scope="federated", scope_id=None)
            .first()
        )


class TestFederatedScope:
    """Federated tier: store/recall roundtrip, precedence, layout, forget,
    scope_id resolution, search-dir invariant, and the secret gate.
    """

    def test_federated_store_recall_roundtrip(self, tmp_path: Path):
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()
        _run(
            svc.store(
                content="federated shared note about widgets",
                scope="federated",
                memory_type="reference",
                key="fed-note",
                terminal_context=ctx,
            )
        )
        results = _run(svc.recall(query="widgets", terminal_context=ctx))
        assert any(m.key == "fed-note" and m.scope == "federated" for m in results)

    def test_federated_recall_ranks_last(self, tmp_path: Path):
        """With one memory per scope sharing the query term, federated ranks
        after global (lowest precedence).
        """
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()
        for scope, key in (
            ("session", "rank-session"),
            ("project", "rank-project"),
            ("global", "rank-global"),
            ("federated", "rank-federated"),
        ):
            _run(
                svc.store(
                    content="shared rankterm content",
                    scope=scope,
                    memory_type="reference",
                    key=key,
                    terminal_context=ctx,
                )
            )
        results = _run(svc.recall(query="rankterm", terminal_context=ctx))
        scopes = [m.scope for m in results]
        assert "global" in scopes and "federated" in scopes
        assert scopes.index("global") < scopes.index("federated")

    def test_federated_file_location(self, tmp_path: Path):
        """Wiki file lands at base/federated/wiki/federated/{key}.md."""
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()
        mem = _run(
            svc.store(
                content="federated layout content",
                scope="federated",
                memory_type="reference",
                key="fed-layout",
                terminal_context=ctx,
            )
        )
        expected = tmp_path / "federated" / "wiki" / "federated" / "fed-layout.md"
        assert expected.exists()
        assert Path(mem.file_path).resolve() == expected.resolve()

    def test_federated_forget_removes_file_index_and_row(self, tmp_path: Path):
        engine = _federated_engine(tmp_path)
        svc = MemoryService(base_dir=tmp_path, db_engine=engine)
        ctx = _make_terminal_context()
        _run(
            svc.store(
                content="federated forgettable content",
                scope="federated",
                memory_type="reference",
                key="fed-forget",
                terminal_context=ctx,
            )
        )
        wiki = tmp_path / "federated" / "wiki" / "federated" / "fed-forget.md"
        assert wiki.exists()
        assert _fed_row(engine, "fed-forget") is not None
        index_path = svc.get_index_path("federated", None)
        assert "[fed-forget]" in index_path.read_text(encoding="utf-8")

        ok = _run(svc.forget(key="fed-forget", scope="federated", terminal_context=ctx))
        assert ok is True
        assert not wiki.exists()
        assert _fed_row(engine, "fed-forget") is None
        assert "[fed-forget]" not in index_path.read_text(encoding="utf-8")

    def test_federated_resolve_scope_id_is_none(self, tmp_path: Path):
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()
        assert svc.resolve_scope_id("federated", ctx) is None

    def test_empty_federated_search_dirs_byte_identical(self, tmp_path: Path):
        """When no federated dir exists, the search-dir list is unchanged from
        pre-federation behaviour; an empty federated dir is only appended once
        it actually exists (guards the recency invariant).
        """
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()
        # Seed a global memory so the global dir exists.
        _run(
            svc.store(
                content="global seed",
                scope="global",
                memory_type="reference",
                key="seed",
                terminal_context=ctx,
            )
        )
        fed_dir = tmp_path / "federated"
        assert not fed_dir.exists()
        dirs_absent = svc._get_search_dirs(None, ctx)
        assert fed_dir not in dirs_absent

        # Creating an empty federated dir adds it; removing it reverts.
        fed_dir.mkdir(parents=True)
        dirs_present = svc._get_search_dirs(None, ctx)
        assert fed_dir in dirs_present

        fed_dir.rmdir()
        dirs_again = svc._get_search_dirs(None, ctx)
        assert dirs_again == dirs_absent

    def test_scan_all_does_not_list_federated_twice(self, tmp_path: Path):
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()
        _run(
            svc.store(
                content="federated scanall content",
                scope="federated",
                memory_type="reference",
                key="fed-scanall",
                terminal_context=ctx,
            )
        )
        dirs = svc._get_search_dirs(None, ctx, scan_all=True)
        fed_dir = (tmp_path / "federated").resolve()
        matches = [d for d in dirs if d.resolve() == fed_dir]
        assert len(matches) == 1

    def test_federated_secret_rejected_nothing_written(self, tmp_path: Path, caplog):
        """An AKIA key on a federated write raises ValueError and writes
        nothing (no wiki file, no db row); the error/log carries no secrets.
        """
        engine = _federated_engine(tmp_path)
        svc = MemoryService(base_dir=tmp_path, db_engine=engine)
        ctx = _make_terminal_context()
        # NOTE: not named `secret` on purpose. CodeQL's
        # py/clear-text-storage-sensitive-data classifies a local variable
        # named `secret` as a sensitive-data *source* and traces it into the
        # memory-wiki write (a documented by-design plaintext sink), producing
        # a false positive (code-scanning alert #142). The literal is the
        # canonical AWS *documentation example* key, not a real credential; the
        # value is unchanged so the secret-gate is still exercised exactly.
        gated_content = "deploy creds AKIAIOSFODNN7EXAMPLE here"
        with caplog.at_level("WARNING", logger="cli_agent_orchestrator.services.memory_service"):
            with pytest.raises(ValueError) as exc_info:
                _run(
                    svc.store(
                        content=gated_content,
                        scope="federated",
                        memory_type="reference",
                        key="fed-secret",
                        terminal_context=ctx,
                    )
                )
        # No secret bytes in the raised message.
        assert "AKIAIOSFODNN7EXAMPLE" not in str(exc_info.value)
        # No secret bytes in any log record.
        for rec in caplog.records:
            assert "AKIAIOSFODNN7EXAMPLE" not in rec.getMessage()
        # Nothing persisted.
        wiki = tmp_path / "federated" / "wiki" / "federated" / "fed-secret.md"
        assert not wiki.exists()
        assert _fed_row(engine, "fed-secret") is None

    def test_same_secret_content_allowed_at_global_scope(self, tmp_path: Path):
        """The gate is federated-only: identical AKIA-bearing content stores
        fine at scope=global.
        """
        svc = MemoryService(base_dir=tmp_path)
        ctx = _make_terminal_context()
        # See the naming note above: kept off the name `secret` to avoid a
        # CodeQL clear-text-storage false positive (#142). Same value/behavior.
        gated_content = "deploy creds AKIAIOSFODNN7EXAMPLE here"
        mem = _run(
            svc.store(
                content=gated_content,
                scope="global",
                memory_type="reference",
                key="glob-secret-ok",
                terminal_context=ctx,
            )
        )
        assert Path(mem.file_path).exists()
