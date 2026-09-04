"""The learning gate must not report a filesystem fault as a configuration choice.

``is_learning_enabled()`` fails closed, which is correct. But an unreadable
settings.json also resolves to False, so ``/outcomes`` answered 404 — "learning is
disabled" — for what was a permissions problem, and the MCP tools relayed that to
agents as a ``disabled`` payload they are told to skip silently. 503 says "I
cannot tell", which is what an operator needs to hear.

``_require_memory_enabled`` deliberately has no 503 branch: memory fails OPEN, so
an unreadable file there resolves to enabled and cannot mislead.
"""

import json
import os
from unittest.mock import patch

import pytest

BODY = {"session_name": "s", "task_label": "t", "success": True}

requires_unprivileged = pytest.mark.skipif(
    os.geteuid() == 0, reason="chmod-based read denial does not apply to root"
)


@pytest.fixture
def settings_file(tmp_path):
    """Point settings_service at an isolated settings.json."""
    fake = tmp_path / "settings.json"
    with (
        patch("cli_agent_orchestrator.services.settings_service.SETTINGS_FILE", fake),
        patch("cli_agent_orchestrator.services.settings_service.CAO_HOME_DIR", tmp_path),
    ):
        yield fake


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    """The env var beats settings.json, so it would mask what these tests probe."""
    monkeypatch.delenv("CAO_MEMORY_LEARNING_ENABLED", raising=False)
    monkeypatch.delenv("CAO_MEMORY_ENABLED", raising=False)


class TestDisabledVersusUnreadable:
    def test_disabled_is_404_with_an_actionable_detail(self, client, settings_file):
        settings_file.write_text(json.dumps({"memory": {"learning_enabled": False}}))
        response = client.post("/outcomes", json=BODY)
        assert response.status_code == 404
        detail = response.json()["detail"]
        assert "memory.learning_enabled" in detail
        assert "settings.json" in detail

    @requires_unprivileged
    def test_unreadable_is_503_naming_the_cause(self, client, tmp_path):
        home = tmp_path / "cao-home"
        home.mkdir()
        fake = home / "settings.json"
        fake.write_text(json.dumps({"memory": {"learning_enabled": True}}))
        home.chmod(0o000)
        try:
            with (
                patch("cli_agent_orchestrator.services.settings_service.SETTINGS_FILE", fake),
                patch("cli_agent_orchestrator.services.settings_service.CAO_HOME_DIR", home),
            ):
                response = client.post("/outcomes", json=BODY)
                get_response = client.get("/outcomes")
        finally:
            home.chmod(0o700)

        assert response.status_code == 503, response.text
        assert get_response.status_code == 503
        detail = response.json()["detail"]
        assert "PermissionError" in detail
        # Server filesystem paths must not travel in an HTTP payload.
        assert str(fake) not in detail
        assert str(home) not in detail

    def test_enabled_still_works(self, client, settings_file):
        settings_file.write_text(json.dumps({"memory": {"learning_enabled": True}}))
        with patch(
            "cli_agent_orchestrator.services.outcome_service.OutcomeService.record_outcome",
            return_value={"id": "abc"},
        ):
            response = client.post("/outcomes", json=BODY)
        assert response.status_code == 200, response.text


class TestSettingsReadableSurface:
    def test_reports_readable(self, client, settings_file):
        settings_file.write_text(json.dumps({"memory": {"learning_enabled": True}}))
        body = client.get("/settings/memory").json()
        assert body["settings_readable"] is True
        assert body["learning_enabled"] is True

    @requires_unprivileged
    def test_reports_unreadable_while_still_failing_closed(self, client, tmp_path):
        home = tmp_path / "cao-home"
        home.mkdir()
        fake = home / "settings.json"
        fake.write_text(json.dumps({"memory": {"learning_enabled": True}}))
        home.chmod(0o000)
        try:
            with (
                patch("cli_agent_orchestrator.services.settings_service.SETTINGS_FILE", fake),
                patch("cli_agent_orchestrator.services.settings_service.CAO_HOME_DIR", home),
            ):
                body = client.get("/settings/memory").json()
        finally:
            home.chmod(0o700)

        assert body["settings_readable"] is False
        assert body["learning_enabled"] is False, "must still fail closed"
        # memory.enabled fails OPEN by design — do not "fix" this to False.
        assert body["enabled"] is True
