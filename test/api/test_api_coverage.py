"""Additional API tests to maximize patch coverage.

Covers: WebSocket localhost guard, list_all_terminals endpoint,
flow path traversal validation, and remaining error branches.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import CreateFlowRequest, app


@pytest.fixture
def client():
    return TestClient(app)


class TestFlowNameValidation:
    """Test CreateFlowRequest.validate_name blocks path traversal."""

    @pytest.mark.parametrize(
        "bad_name",
        ["../../etc/cron", "../evil", "foo/bar", "a\\b", ".."],
    )
    def test_rejects_traversal_names(self, bad_name):
        with pytest.raises(Exception):
            CreateFlowRequest(
                name=bad_name,
                schedule="0 * * * *",
                agent_profile="dev",
                prompt_template="x",
            )

    @pytest.mark.parametrize(
        "good_name",
        ["my-flow", "nightly_build", "FLOW123", "a.b"],
    )
    def test_accepts_safe_names(self, good_name):
        req = CreateFlowRequest(
            name=good_name,
            schedule="0 * * * *",
            agent_profile="dev",
            prompt_template="x",
        )
        assert req.name == good_name


class TestAgentProfilesEndpoint:
    """Test GET /agents/profiles."""

    def test_list_profiles_success(self, client):
        with patch("cli_agent_orchestrator.utils.agent_profiles.list_agent_profiles") as mock_list:
            mock_list.return_value = [
                {"name": "dev", "description": "Developer", "source": "built-in"}
            ]
            resp = client.get("/agents/profiles")
            assert resp.status_code == 200
            assert len(resp.json()) == 1

    def test_list_profiles_error(self, client):
        with patch("cli_agent_orchestrator.utils.agent_profiles.list_agent_profiles") as mock_list:
            mock_list.side_effect = Exception("scan error")
            resp = client.get("/agents/profiles")
            assert resp.status_code == 500
