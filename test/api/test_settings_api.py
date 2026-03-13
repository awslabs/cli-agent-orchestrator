"""Tests for settings API endpoints."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app


@pytest.fixture
def client():
    """Create a test client."""
    return TestClient(app)


class TestGetAgentDirsEndpoint:
    """Tests for GET /settings/agent-dirs endpoint."""

    def test_returns_agent_dirs_and_extra_dirs(self, client):
        """GET /settings/agent-dirs returns both agent_dirs and extra_dirs."""
        mock_agent_dirs = {
            "kiro_cli": "/home/user/.kiro/agents",
            "q_cli": "/home/user/.aws/amazonq/cli-agents",
            "claude_code": "/custom/claude",
            "codex": "/custom/codex",
        }
        mock_extra_dirs = ["/extra/dir1", "/extra/dir2"]

        with (
            patch(
                "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
                return_value=mock_agent_dirs,
            ),
            patch(
                "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
                return_value=mock_extra_dirs,
            ),
        ):
            response = client.get("/settings/agent-dirs")

        assert response.status_code == 200
        data = response.json()
        assert "agent_dirs" in data
        assert "extra_dirs" in data
        assert data["agent_dirs"] == mock_agent_dirs
        assert data["extra_dirs"] == mock_extra_dirs

    def test_returns_empty_extra_dirs_when_none(self, client):
        """GET /settings/agent-dirs returns empty extra_dirs when none configured."""
        mock_agent_dirs = {
            "kiro_cli": "/path",
            "q_cli": "/path2",
            "claude_code": "/p3",
            "codex": "/p4",
        }

        with (
            patch(
                "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
                return_value=mock_agent_dirs,
            ),
            patch(
                "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
                return_value=[],
            ),
        ):
            response = client.get("/settings/agent-dirs")

        assert response.status_code == 200
        data = response.json()
        assert data["extra_dirs"] == []


class TestSetAgentDirsEndpoint:
    """Tests for POST /settings/agent-dirs endpoint."""

    def test_updates_agent_dirs_and_returns_result(self, client):
        """POST /settings/agent-dirs updates agent_dirs and returns new settings."""
        updated_dirs = {
            "kiro_cli": "/new/kiro",
            "q_cli": "/default/q",
            "claude_code": "/default/claude",
            "codex": "/default/codex",
        }

        with (
            patch(
                "cli_agent_orchestrator.services.settings_service.set_agent_dirs",
                return_value=updated_dirs,
            ),
            patch(
                "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
                return_value=["/existing/extra"],
            ),
        ):
            response = client.post(
                "/settings/agent-dirs",
                json={"agent_dirs": {"kiro_cli": "/new/kiro"}},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["agent_dirs"] == updated_dirs
        assert data["extra_dirs"] == ["/existing/extra"]

    def test_updates_extra_dirs(self, client):
        """POST /settings/agent-dirs can update extra_dirs."""
        with (
            patch(
                "cli_agent_orchestrator.services.settings_service.set_extra_agent_dirs",
                return_value=["/new/extra"],
            ),
            patch(
                "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
                return_value=["/new/extra"],
            ),
        ):
            response = client.post(
                "/settings/agent-dirs",
                json={"extra_dirs": ["/new/extra"]},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["extra_dirs"] == ["/new/extra"]

    def test_updates_both_agent_dirs_and_extra_dirs(self, client):
        """POST /settings/agent-dirs can update both in one request."""
        updated_dirs = {
            "kiro_cli": "/updated",
            "q_cli": "/default/q",
            "claude_code": "/default/claude",
            "codex": "/default/codex",
        }

        with (
            patch(
                "cli_agent_orchestrator.services.settings_service.set_agent_dirs",
                return_value=updated_dirs,
            ),
            patch(
                "cli_agent_orchestrator.services.settings_service.set_extra_agent_dirs",
                return_value=["/extra1"],
            ),
            patch(
                "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
                return_value=["/extra1"],
            ),
        ):
            response = client.post(
                "/settings/agent-dirs",
                json={
                    "agent_dirs": {"kiro_cli": "/updated"},
                    "extra_dirs": ["/extra1"],
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["agent_dirs"] == updated_dirs
        assert data["extra_dirs"] == ["/extra1"]

    def test_empty_body_returns_defaults(self, client):
        """POST /settings/agent-dirs with empty body returns empty agent_dirs and existing extra."""
        with patch(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
            return_value=[],
        ):
            response = client.post("/settings/agent-dirs", json={})

        assert response.status_code == 200
        data = response.json()
        assert data["agent_dirs"] == {}
        assert data["extra_dirs"] == []
