"""Tests for GET /memory/export (#345 Unit 3, D6 HTTP surface).

Mirrors test/api/test_memory_api.py: the _get_memory_service factory and
the is_memory_enabled gate are patched at the seams the endpoint reads.
Tar-producing tests use a real MemoryService in tmp dirs.
"""

import asyncio
import io
import tarfile
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine

from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services.memory_service import MemoryService

FACTORY_TARGET = "cli_agent_orchestrator.api.main._get_memory_service"
ENABLED_TARGET = "cli_agent_orchestrator.services.settings_service.is_memory_enabled"


@pytest.fixture
def real_service(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'api.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    svc = MemoryService(base_dir=tmp_path / "memory", db_engine=engine)
    with patch(FACTORY_TARGET, return_value=svc):
        yield svc


class TestExportEndpointGates:
    @pytest.mark.parametrize("scope", ["session", "agent"])
    def test_private_scope_is_400(self, client, real_service, scope):
        with patch(ENABLED_TARGET, return_value=True):
            response = client.get(f"/memory/export?scope={scope}")
        assert response.status_code == 400
        assert "private" in response.json()["detail"]

    def test_unknown_format_is_400(self, client, real_service):
        with patch(ENABLED_TARGET, return_value=True):
            response = client.get("/memory/export?scope=global&format=nope")
        assert response.status_code == 400
        assert "Unknown memory archive format" in response.json()["detail"]

    def test_project_scope_requires_scope_id(self, client, real_service):
        with patch(ENABLED_TARGET, return_value=True):
            response = client.get("/memory/export?scope=project")
        assert response.status_code == 400
        assert "scope_id" in response.json()["detail"]

    def test_disabled_memory_is_404(self, client, real_service):
        with patch(ENABLED_TARGET, return_value=False):
            response = client.get("/memory/export?scope=global")
        assert response.status_code == 404

    def test_invalid_scope_is_422(self, client, real_service):
        with patch(ENABLED_TARGET, return_value=True):
            response = client.get("/memory/export?scope=nope")
        assert response.status_code == 422


class TestExportEndpointStream:
    def test_returns_valid_tar_with_expected_members(self, client, real_service):
        asyncio.run(real_service.store(content="api fact", scope="global", key="api-topic"))
        with patch(ENABLED_TARGET, return_value=True):
            response = client.get("/memory/export?scope=global")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/gzip"
        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tar:
            names = sorted(tar.getnames())
            body = tar.extractfile("api-topic.md").read().decode("utf-8")
        assert names == ["api-topic.md", "index.md", "manifest.md"]
        assert "api fact" in body

    def test_empty_scope_returns_valid_tar(self, client, real_service):
        with patch(ENABLED_TARGET, return_value=True):
            response = client.get("/memory/export?scope=global")
        assert response.status_code == 200
        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tar:
            assert sorted(tar.getnames()) == ["index.md", "manifest.md"]
