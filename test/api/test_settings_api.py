"""Tests for settings API endpoints."""

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.api.main import app


class TestGetAgentDirsEndpoint:
    """Tests for GET /settings/agent-dirs endpoint."""

    def test_returns_agent_dirs_and_extra_dirs(self, client):
        """GET /settings/agent-dirs returns both agent_dirs and extra_dirs."""
        mock_agent_dirs = {
            "kiro_cli": "/home/user/.kiro/agents",
            "cao_installed": "/home/user/.aws/cli-agent-orchestrator/installed-agents",
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
            "cao_installed": "/path2",
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
    """POST /settings/agent-dirs persists changes and returns the full EFFECTIVE
    state (agent_dirs + extra_dirs + disabled_dirs) so the UI never has to guess
    what actually stuck (GH #281). Uses an isolated settings file per test."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        from cli_agent_orchestrator.services import settings_service as svc

        monkeypatch.setattr(svc, "SETTINGS_FILE", tmp_path / "settings.json")

    def test_updates_agent_dirs_and_returns_effective_state(self, client):
        response = client.post(
            "/settings/agent-dirs",
            json={"agent_dirs": {"kiro_cli": "/new/kiro"}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["agent_dirs"]["kiro_cli"] == "/new/kiro"
        assert "extra_dirs" in data
        assert "disabled_dirs" in data

    def test_updates_extra_dirs(self, client):
        response = client.post("/settings/agent-dirs", json={"extra_dirs": ["/new/extra"]})
        assert response.status_code == 200
        assert response.json()["extra_dirs"] == ["/new/extra"]

    def test_updates_both_agent_dirs_and_extra_dirs(self, client):
        response = client.post(
            "/settings/agent-dirs",
            json={"agent_dirs": {"kiro_cli": "/updated"}, "extra_dirs": ["/extra1"]},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["agent_dirs"]["kiro_cli"] == "/updated"
        assert data["extra_dirs"] == ["/extra1"]

    def test_empty_body_returns_effective_state(self, client):
        response = client.post("/settings/agent-dirs", json={})
        assert response.status_code == 200
        data = response.json()
        # Full default provider map is returned (not empty); nothing disabled.
        assert {"kiro_cli", "claude_code", "codex", "cao_installed"} <= set(data["agent_dirs"])
        assert data["extra_dirs"] == []
        assert data["disabled_dirs"] == []


class TestGetSkillDirsEndpoint:
    """Tests for GET /settings/skill-dirs endpoint."""

    def test_returns_skills_dir_and_extra_dirs(self, client):
        """GET /settings/skill-dirs returns the global store path and extra_dirs."""
        with patch(
            "cli_agent_orchestrator.services.settings_service.get_extra_skill_dirs",
            return_value=["/extra/skills1", "/extra/skills2"],
        ):
            response = client.get("/settings/skill-dirs")

        assert response.status_code == 200
        data = response.json()
        assert "skills_dir" in data
        assert data["extra_dirs"] == ["/extra/skills1", "/extra/skills2"]

    def test_returns_empty_extra_dirs_when_none(self, client):
        """GET /settings/skill-dirs returns empty extra_dirs when none configured."""
        with patch(
            "cli_agent_orchestrator.services.settings_service.get_extra_skill_dirs",
            return_value=[],
        ):
            response = client.get("/settings/skill-dirs")

        assert response.status_code == 200
        assert response.json()["extra_dirs"] == []


class TestSetSkillDirsEndpoint:
    """Tests for POST /settings/skill-dirs endpoint."""

    def test_updates_extra_dirs(self, client):
        """POST /settings/skill-dirs updates extra_dirs and returns them."""
        with (
            patch(
                "cli_agent_orchestrator.services.settings_service.set_extra_skill_dirs",
                return_value=["/new/skills"],
            ),
            patch(
                "cli_agent_orchestrator.services.settings_service.get_extra_skill_dirs",
                return_value=["/new/skills"],
            ),
        ):
            response = client.post(
                "/settings/skill-dirs",
                json={"extra_dirs": ["/new/skills"]},
            )

        assert response.status_code == 200
        data = response.json()
        assert "skills_dir" in data
        assert data["extra_dirs"] == ["/new/skills"]

    def test_empty_body_returns_existing(self, client):
        """POST /settings/skill-dirs with empty body returns existing extra dirs."""
        with patch(
            "cli_agent_orchestrator.services.settings_service.get_extra_skill_dirs",
            return_value=[],
        ):
            response = client.post("/settings/skill-dirs", json={})

        assert response.status_code == 200
        assert response.json()["extra_dirs"] == []


class TestDisabledDirsEndpoint:
    """GH #280/#281: /settings/agent-dirs carries the enable/disable set."""

    def test_get_includes_disabled_key(self, client, tmp_path, monkeypatch):
        from cli_agent_orchestrator.services import settings_service as svc

        monkeypatch.setattr(svc, "SETTINGS_FILE", tmp_path / "settings.json")
        got = client.get("/settings/agent-dirs").json()
        assert got["disabled_dirs"] == []

    def test_disabled_dirs_roundtrip(self, client, tmp_path, monkeypatch):
        from cli_agent_orchestrator.services import settings_service as svc

        monkeypatch.setattr(svc, "SETTINGS_FILE", tmp_path / "settings.json")
        extra = tmp_path / "x"
        extra.mkdir()

        # Add an extra dir and disable it in the SAME request — the endpoint
        # persists extras before validating the disabled set, so this sticks.
        resp = client.post(
            "/settings/agent-dirs",
            json={"extra_dirs": [str(extra)], "disabled_dirs": [str(extra)]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert str(extra) in body["extra_dirs"]
        assert body["disabled_dirs"] == [str(extra)]

        # GET reflects the persisted disabled set.
        assert client.get("/settings/agent-dirs").json()["disabled_dirs"] == [str(extra)]

    def test_unknown_disabled_path_is_dropped(self, client, tmp_path, monkeypatch):
        from cli_agent_orchestrator.services import settings_service as svc

        monkeypatch.setattr(svc, "SETTINGS_FILE", tmp_path / "settings.json")
        resp = client.post("/settings/agent-dirs", json={"disabled_dirs": ["/not/configured"]})
        assert resp.status_code == 200
        assert resp.json()["disabled_dirs"] == []
