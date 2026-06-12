"""Tests for memory REST endpoints (issue #286).

Endpoint logic is isolated by patching the _get_memory_service factory
(mirroring test/cli/commands/test_memory.py) and the is_memory_enabled
gate at the seam the endpoints read.
"""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.memory import Memory

MEMORY_BASE = Path("/home/user/.aws/cli-agent-orchestrator/memory")

FACTORY_TARGET = "cli_agent_orchestrator.api.main._get_memory_service"
ENABLED_TARGET = "cli_agent_orchestrator.services.settings_service.is_memory_enabled"


def _make_memory(
    key="test-key",
    scope="global",
    scope_id=None,
    memory_type="project",
    tags="",
    content="Test content.",
    project_dir="global",
):
    """Build a Memory with a realistic on-disk file_path.

    Layout (memory_service.get_wiki_path): base/<container>/wiki/<scope>[/<scope_id>]/<key>.md
    where container is the project id for project scope and "global" otherwise.
    """
    if scope in ("session", "agent") and scope_id:
        file_path = MEMORY_BASE / "global" / "wiki" / scope / scope_id / f"{key}.md"
    else:
        file_path = MEMORY_BASE / project_dir / "wiki" / scope / f"{key}.md"
    return Memory(
        id=f"{scope}:{key}",
        key=key,
        memory_type=memory_type,
        scope=scope,
        scope_id=scope_id,
        file_path=str(file_path),
        tags=tags,
        created_at=datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc),
        content=content,
    )


@pytest.fixture
def mock_service():
    """Patch the module-level factory with a service mock (async recall/forget)."""
    svc = MagicMock()
    svc.base_dir = MEMORY_BASE
    svc.recall = AsyncMock(return_value=[])
    svc.forget = AsyncMock(return_value=True)
    with patch(FACTORY_TARGET, return_value=svc):
        yield svc


class TestMemorySettingsEndpoint:
    """Tests for GET /settings/memory."""

    def test_enabled(self, client):
        with patch(ENABLED_TARGET, return_value=True):
            response = client.get("/settings/memory")
        assert response.status_code == 200
        assert response.json() == {"enabled": True}

    def test_disabled(self, client):
        with patch(ENABLED_TARGET, return_value=False):
            response = client.get("/settings/memory")
        assert response.status_code == 200
        assert response.json() == {"enabled": False}


class TestListMemories:
    """Tests for GET /memory."""

    def test_list_success(self, client, mock_service):
        mock_service.recall.return_value = [
            _make_memory(key="alpha", scope="global"),
            _make_memory(key="beta", scope="project", project_dir="proj-abc123"),
        ]
        response = client.get("/memory")

        assert response.status_code == 200
        data = response.json()
        assert [m["key"] for m in data] == ["alpha", "beta"]
        # project scope_id is derived from the storage path; global has none
        assert data[0]["scope_id"] is None
        assert data[1]["scope_id"] == "proj-abc123"
        # file_path must never leak into the response
        assert "file_path" not in data[0]

    def test_list_recall_args(self, client, mock_service):
        """recall uses internal limit 1000 + scan_all + metadata; response sliced to user limit."""
        mock_service.recall.return_value = [_make_memory(key=f"key-{i}") for i in range(5)]
        response = client.get("/memory?limit=3&scope=global&type=feedback")

        assert response.status_code == 200
        assert len(response.json()) == 3
        mock_service.recall.assert_awaited_once_with(
            scope="global",
            memory_type="feedback",
            limit=1000,
            scan_all=True,
            search_mode="metadata",
        )

    def test_list_filters_project_by_scope_id(self, client, mock_service):
        mock_service.recall.return_value = [
            _make_memory(key="mine", scope="project", project_dir="proj-mine"),
            _make_memory(key="other", scope="project", project_dir="proj-other"),
        ]
        response = client.get("/memory?scope=project&scope_id=proj-mine")

        assert response.status_code == 200
        data = response.json()
        assert [m["key"] for m in data] == ["mine"]

    def test_list_slices_after_scope_id_filter(self, client, mock_service):
        """The user limit applies to the FILTERED set, not the raw recall page."""
        mock_service.recall.return_value = [
            # interleave non-matching first so a slice-before-filter would
            # return too few matching rows
            mem
            for i in range(5)
            for mem in (
                _make_memory(key=f"other-{i}", scope="project", project_dir="proj-other"),
                _make_memory(key=f"mine-{i}", scope="project", project_dir="proj-mine"),
            )
        ]
        response = client.get("/memory?scope=project&scope_id=proj-mine&limit=3")

        assert response.status_code == 200
        assert [m["key"] for m in response.json()] == ["mine-0", "mine-1", "mine-2"]

    def test_list_dot_only_scope_id_returns_422(self, client, mock_service):
        assert client.get("/memory?scope_id=..").status_code == 422
        assert client.get("/memory?scope_id=.").status_code == 422
        mock_service.recall.assert_not_awaited()

    def test_list_scope_id_excludes_globals(self, client, mock_service):
        """scope_id strictly narrows: global memories (scope_id=None) never match."""
        mock_service.recall.return_value = [
            _make_memory(key="global-one", scope="global"),
            _make_memory(key="mine", scope="project", project_dir="proj-mine"),
        ]
        response = client.get("/memory?scope_id=proj-mine")

        assert response.status_code == 200
        assert [m["key"] for m in response.json()] == ["mine"]

    def test_list_filters_session_by_native_scope_id(self, client, mock_service):
        mock_service.recall.return_value = [
            _make_memory(key="s-one", scope="session", scope_id="sess-1"),
            _make_memory(key="s-two", scope="session", scope_id="sess-2"),
        ]
        response = client.get("/memory?scope=session&scope_id=sess-1")

        assert response.status_code == 200
        data = response.json()
        assert [m["key"] for m in data] == ["s-one"]
        assert data[0]["scope_id"] == "sess-1"

    def test_list_disabled_returns_404(self, client, mock_service):
        with patch(ENABLED_TARGET, return_value=False):
            response = client.get("/memory")
        assert response.status_code == 404
        assert "disabled" in response.json()["detail"]

    def test_list_invalid_scope_returns_422(self, client, mock_service):
        assert client.get("/memory?scope=bogus").status_code == 422

    def test_list_invalid_limit_returns_422(self, client, mock_service):
        assert client.get("/memory?limit=0").status_code == 422
        assert client.get("/memory?limit=101").status_code == 422

    def test_list_server_error_returns_500(self, client, mock_service):
        mock_service.recall.side_effect = Exception("storage exploded")
        response = client.get("/memory")
        assert response.status_code == 500
        assert "Failed to list memories" in response.json()["detail"]


class TestGetMemory:
    """Tests for GET /memory/{key}."""

    def test_get_success(self, client, mock_service):
        mock_service.recall.return_value = [
            _make_memory(key="my-key", content="Full content here.")
        ]
        response = client.get("/memory/my-key")

        assert response.status_code == 200
        data = response.json()
        assert data["key"] == "my-key"
        assert data["content"] == "Full content here."
        assert "file_path" not in data
        mock_service.recall.assert_awaited_once_with(
            query="my-key",
            scope=None,
            limit=1000,
            scan_all=True,
            search_mode="metadata",
        )

    def test_get_exact_match_only(self, client, mock_service):
        """Substring recall hits that aren't exact key matches are skipped."""
        mock_service.recall.return_value = [_make_memory(key="my-key-extended")]
        response = client.get("/memory/my-key")
        assert response.status_code == 404
        assert "Memory 'my-key' not found" in response.json()["detail"]

    def test_get_narrows_by_scope_id(self, client, mock_service):
        mock_service.recall.return_value = [
            _make_memory(key="dup", scope="project", project_dir="proj-other"),
            _make_memory(key="dup", scope="project", project_dir="proj-mine"),
        ]
        response = client.get("/memory/dup?scope=project&scope_id=proj-mine")

        assert response.status_code == 200
        assert response.json()["scope_id"] == "proj-mine"

    def test_get_not_found(self, client, mock_service):
        response = client.get("/memory/missing-key")
        assert response.status_code == 404
        assert "Memory 'missing-key' not found" in response.json()["detail"]

    def test_get_invalid_keys_return_422(self, client, mock_service):
        # uppercase, underscore, overlong — all rejected pre-handler
        assert client.get("/memory/BadKey").status_code == 422
        assert client.get("/memory/bad_key").status_code == 422
        assert client.get(f"/memory/{'a' * 61}").status_code == 422
        # encoded traversal never routes to the handler (404 from Starlette,
        # not from our not-found path) — the property that matters is that
        # the service is never reached
        assert client.get("/memory/%2E%2E%2Fetc").status_code == 404
        mock_service.recall.assert_not_awaited()

    def test_get_disabled_returns_404(self, client, mock_service):
        with patch(ENABLED_TARGET, return_value=False):
            response = client.get("/memory/my-key")
        assert response.status_code == 404
        assert "disabled" in response.json()["detail"]

    def test_get_server_error_returns_500(self, client, mock_service):
        mock_service.recall.side_effect = Exception("boom")
        response = client.get("/memory/my-key")
        assert response.status_code == 500
        assert "Failed to get memory" in response.json()["detail"]


class TestDeleteMemory:
    """Tests for DELETE /memory/{key}."""

    def test_delete_global_success(self, client, mock_service):
        response = client.delete("/memory/my-key?scope=global")

        assert response.status_code == 200
        assert response.json() == {"success": True}
        mock_service.forget.assert_awaited_once_with(key="my-key", scope="global", scope_id=None)

    def test_delete_project_with_scope_id(self, client, mock_service):
        response = client.delete("/memory/my-key?scope=project&scope_id=proj-abc")

        assert response.status_code == 200
        mock_service.forget.assert_awaited_once_with(
            key="my-key", scope="project", scope_id="proj-abc"
        )

    def test_delete_project_without_scope_id_returns_400(self, client, mock_service):
        """Default scope is project (CLI parity) and non-global scopes need scope_id."""
        response = client.delete("/memory/my-key")
        assert response.status_code == 400
        assert "requires scope_id" in response.json()["detail"]
        mock_service.forget.assert_not_awaited()

    def test_delete_not_found(self, client, mock_service):
        mock_service.forget.return_value = False
        response = client.delete("/memory/my-key?scope=global")
        assert response.status_code == 404
        assert "not found in scope 'global'" in response.json()["detail"]

    def test_delete_invalid_key_returns_422(self, client, mock_service):
        assert client.delete("/memory/Bad_Key?scope=global").status_code == 422
        mock_service.forget.assert_not_awaited()

    def test_delete_dot_only_scope_id_returns_422(self, client, mock_service):
        """Traversal tokens are rejected at the boundary, not by the path guard."""
        assert client.delete("/memory/my-key?scope=project&scope_id=..").status_code == 422
        assert client.delete("/memory/my-key?scope=project&scope_id=.").status_code == 422
        mock_service.forget.assert_not_awaited()

    def test_delete_disabled_returns_404(self, client, mock_service):
        with patch(ENABLED_TARGET, return_value=False):
            response = client.delete("/memory/my-key?scope=global")
        assert response.status_code == 404
        assert "disabled" in response.json()["detail"]

    def test_delete_server_error_returns_500(self, client, mock_service):
        mock_service.forget.side_effect = Exception("boom")
        response = client.delete("/memory/my-key?scope=global")
        assert response.status_code == 500
        assert "Failed to delete memory" in response.json()["detail"]


class TestClearMemories:
    """Tests for DELETE /memory (clear by scope)."""

    def test_clear_global_success(self, client, mock_service):
        mock_service.recall.return_value = [
            _make_memory(key="one"),
            _make_memory(key="two"),
        ]
        response = client.delete("/memory?scope=global")

        assert response.status_code == 200
        assert response.json() == {"success": True, "deleted_count": 2}
        assert mock_service.forget.await_count == 2

    def test_clear_requires_scope(self, client, mock_service):
        assert client.delete("/memory").status_code == 422

    def test_clear_project_without_scope_id_returns_400(self, client, mock_service):
        response = client.delete("/memory?scope=project")
        assert response.status_code == 400
        assert "requires scope_id" in response.json()["detail"]

    def test_clear_project_filters_and_passes_scope_id(self, client, mock_service):
        mock_service.recall.return_value = [
            _make_memory(key="mine", scope="project", project_dir="proj-mine"),
            _make_memory(key="other", scope="project", project_dir="proj-other"),
        ]
        response = client.delete("/memory?scope=project&scope_id=proj-mine")

        assert response.status_code == 200
        assert response.json()["deleted_count"] == 1
        mock_service.forget.assert_awaited_once_with(
            key="mine", scope="project", scope_id="proj-mine"
        )

    def test_clear_session_passes_native_scope_id(self, client, mock_service):
        mock_service.recall.return_value = [
            _make_memory(key="s-one", scope="session", scope_id="sess-1"),
        ]
        response = client.delete("/memory?scope=session&scope_id=sess-1")

        assert response.status_code == 200
        mock_service.forget.assert_awaited_once_with(
            key="s-one", scope="session", scope_id="sess-1"
        )

    def test_clear_continues_past_per_item_failure(self, client, mock_service):
        mock_service.recall.return_value = [
            _make_memory(key="one"),
            _make_memory(key="two"),
        ]
        mock_service.forget.side_effect = [Exception("boom"), True]
        response = client.delete("/memory?scope=global")

        assert response.status_code == 200
        assert response.json()["deleted_count"] == 1
        assert mock_service.forget.await_count == 2

    def test_clear_disabled_returns_404(self, client, mock_service):
        with patch(ENABLED_TARGET, return_value=False):
            response = client.delete("/memory?scope=global")
        assert response.status_code == 404
        assert "disabled" in response.json()["detail"]

    def test_clear_recall_error_returns_500(self, client, mock_service):
        mock_service.recall.side_effect = Exception("boom")
        response = client.delete("/memory?scope=global")
        assert response.status_code == 500
        assert "Failed to clear memories" in response.json()["detail"]
