"""Shared fixtures for API tests."""

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.plugins import PluginRegistry


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
