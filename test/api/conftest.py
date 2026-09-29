"""Shared fixtures for API tests."""

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.plugins import PluginRegistry


@pytest.fixture(autouse=True)
def isolated_startup_skill_store(tmp_path, monkeypatch):
    """Keep server-startup skill seeding out of the user's configured store."""
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.init.SKILLS_DIR",
        tmp_path / "skills",
    )


@pytest.fixture(autouse=True)
def no_startup_terminal_readoption(monkeypatch):
    """Keep server startup from re-adopting terminals it finds in the registry.

    Re-adoption stops and restarts ``pipe-pane`` on every live tmux window a
    registry row points at. Tests that drive the real lifespan run against
    whatever registry and tmux server the machine has, so without this they
    could re-pipe a developer's own running agents. Tests of the re-adoption
    wiring patch ``list_all_terminals`` themselves, which takes precedence.
    """
    monkeypatch.setattr("cli_agent_orchestrator.api.main.list_all_terminals", lambda: [])


class TestClientWithHost(TestClient):
    """TestClient that always sends correct Host header for TrustedHostMiddleware."""

    def request(self, method, url, **kwargs):
        # Ensure Host header is always set to localhost
        if "headers" not in kwargs or kwargs["headers"] is None:
            kwargs["headers"] = {}

        # Check if Host header is already present (case-insensitive)
        headers_dict = kwargs["headers"]
        has_host = any(k.lower() == "host" for k in headers_dict.keys())

        if not has_host:
            headers_dict["Host"] = "localhost"

        return super().request(method, url, **kwargs)


@pytest.fixture
def client():
    """Test client with proper Host header for security middleware."""
    app.state.plugin_registry = PluginRegistry()
    return TestClientWithHost(app)
