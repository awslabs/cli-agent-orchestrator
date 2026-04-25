"""Tests for U3 — memory_forget + memory_consolidate MCP Tools.

Covers:
- U3.1: memory_forget MCP tool (already exposed in Phase 1, verify it works)
- U3.2: memory_consolidate MCP tool — merges entries, deletes originals
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.mcp_server.server import memory_consolidate, memory_forget

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The MCP tool functions use lazy imports:
#   from cli_agent_orchestrator.services.memory_service import MemoryService
# We must patch at the source module so the lazy import picks up the mock.
MEMORY_SERVICE_PATH = "cli_agent_orchestrator.services.memory_service.MemoryService"
TERMINAL_CTX_PATH = "cli_agent_orchestrator.mcp_server.server._get_terminal_context_from_env"


def run_async(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# U3.1 — memory_forget MCP tool
# ---------------------------------------------------------------------------


class TestMemoryForgetMcp:
    """Verify memory_forget is exposed and delegates to MemoryService.forget()."""

    @patch(TERMINAL_CTX_PATH)
    @patch(MEMORY_SERVICE_PATH)
    def test_forget_success(self, MockService, mock_ctx):
        mock_ctx.return_value = {"terminal_id": "t1", "session_name": "s1"}
        instance = MockService.return_value
        instance.forget = AsyncMock(return_value=True)

        result = run_async(memory_forget(key="old-fact", scope="project"))

        assert result["success"] is True
        assert result["deleted"] is True
        assert result["key"] == "old-fact"
        assert result["scope"] == "project"

    @patch(TERMINAL_CTX_PATH)
    @patch(MEMORY_SERVICE_PATH)
    def test_forget_not_found(self, MockService, mock_ctx):
        mock_ctx.return_value = None
        instance = MockService.return_value
        instance.forget = AsyncMock(return_value=False)

        result = run_async(memory_forget(key="nonexistent", scope="global"))

        assert result["success"] is True
        assert result["deleted"] is False

    @patch(TERMINAL_CTX_PATH)
    @patch(MEMORY_SERVICE_PATH)
    def test_forget_error_returns_failure(self, MockService, mock_ctx):
        mock_ctx.return_value = None
        instance = MockService.return_value
        instance.forget = AsyncMock(side_effect=RuntimeError("db error"))

        result = run_async(memory_forget(key="bad", scope="project"))

        assert result["success"] is False
        assert "db error" in result["error"]


# ---------------------------------------------------------------------------
# U3.2 — memory_consolidate MCP tool
# ---------------------------------------------------------------------------


class TestMemoryConsolidateMcp:
    """Verify memory_consolidate merges entries correctly."""

    @patch(TERMINAL_CTX_PATH)
    @patch(MEMORY_SERVICE_PATH)
    def test_consolidate_two_keys(self, MockService, mock_ctx):
        """Merge 2 keys into first key (new_key=None)."""
        mock_ctx.return_value = {"terminal_id": "t1", "session_name": "s1"}
        instance = MockService.return_value
        instance.store = AsyncMock()
        instance.forget = AsyncMock(return_value=True)

        result = run_async(
            memory_consolidate(
                keys=["fact-a", "fact-b"],
                new_content="Combined facts A and B",
                scope="project",
            )
        )

        assert result["success"] is True
        assert result["merged_from"] == ["fact-a", "fact-b"]
        assert result["new_key"] == "fact-a"  # defaults to first key
        assert result["scope"] == "project"

        # store called with first key
        instance.store.assert_called_once()
        store_kwargs = instance.store.call_args.kwargs
        assert store_kwargs["key"] == "fact-a"
        assert store_kwargs["content"] == "Combined facts A and B"

        # forget called only for fact-b (fact-a is the new key, kept)
        forget_calls = instance.forget.call_args_list
        assert len(forget_calls) == 1
        assert forget_calls[0].kwargs["key"] == "fact-b"

    @patch(TERMINAL_CTX_PATH)
    @patch(MEMORY_SERVICE_PATH)
    def test_consolidate_with_new_key(self, MockService, mock_ctx):
        """Merge with explicit new_key — all originals deleted."""
        mock_ctx.return_value = None
        instance = MockService.return_value
        instance.store = AsyncMock()
        instance.forget = AsyncMock(return_value=True)

        result = run_async(
            memory_consolidate(
                keys=["old-1", "old-2", "old-3"],
                new_content="All three merged",
                scope="global",
                new_key="merged-fact",
            )
        )

        assert result["success"] is True
        assert result["new_key"] == "merged-fact"
        assert result["merged_from"] == ["old-1", "old-2", "old-3"]

        # All 3 originals should be deleted (none matches "merged-fact")
        forget_calls = instance.forget.call_args_list
        deleted_keys = [c.kwargs["key"] for c in forget_calls]
        assert set(deleted_keys) == {"old-1", "old-2", "old-3"}

    @patch(TERMINAL_CTX_PATH)
    def test_consolidate_fewer_than_two_keys(self, mock_ctx):
        """Should reject if fewer than 2 keys."""
        mock_ctx.return_value = None

        result = run_async(
            memory_consolidate(
                keys=["only-one"],
                new_content="not enough",
            )
        )

        assert result["success"] is False
        assert "At least 2 keys" in result["error"]

    @patch(TERMINAL_CTX_PATH)
    @patch(MEMORY_SERVICE_PATH)
    def test_consolidate_store_failure(self, MockService, mock_ctx):
        """If store() fails, return error."""
        mock_ctx.return_value = None
        instance = MockService.return_value
        instance.store = AsyncMock(side_effect=RuntimeError("write failed"))

        result = run_async(
            memory_consolidate(
                keys=["a", "b"],
                new_content="merged",
            )
        )

        assert result["success"] is False
        assert "write failed" in result["error"]

    @patch(TERMINAL_CTX_PATH)
    @patch(MEMORY_SERVICE_PATH)
    def test_consolidate_partial_forget_failure(self, MockService, mock_ctx):
        """If one forget fails, success=False (all-or-nothing semantics)."""
        mock_ctx.return_value = None
        instance = MockService.return_value
        instance.store = AsyncMock()

        # 3 keys with new_key="merged-key" -> all 3 get forget called
        # First succeeds, second raises, third succeeds
        instance.forget = AsyncMock(side_effect=[True, RuntimeError("delete failed"), True])

        result = run_async(
            memory_consolidate(
                keys=["a", "b", "c"],
                new_content="merged",
                new_key="merged-key",
            )
        )

        assert result["success"] is False
        assert result["new_key"] == "merged-key"
        # "a" and "c" deleted, "b" failed
        assert "a" in result["deleted_originals"]
        assert "c" in result["deleted_originals"]
        assert "b" not in result["deleted_originals"]
        assert result["errors"] is not None
        assert any("b" in e for e in result["errors"])

    @patch(TERMINAL_CTX_PATH)
    @patch(MEMORY_SERVICE_PATH)
    def test_consolidate_passes_tags_and_type(self, MockService, mock_ctx):
        """Verify tags and memory_type are forwarded to store()."""
        mock_ctx.return_value = None
        instance = MockService.return_value
        instance.store = AsyncMock()
        instance.forget = AsyncMock(return_value=True)

        result = run_async(
            memory_consolidate(
                keys=["x", "y"],
                new_content="merged",
                tags="important,merged",
                memory_type="fact",
            )
        )

        assert result["success"] is True
        store_kwargs = instance.store.call_args.kwargs
        assert store_kwargs["tags"] == "important,merged"
        assert store_kwargs["memory_type"] == "fact"
