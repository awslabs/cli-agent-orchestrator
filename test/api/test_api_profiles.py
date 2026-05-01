"""Tests for profile-related API endpoints."""

from unittest.mock import patch

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.services.install_service import InstallResult


class TestGetAgentProfileEndpoint:
    """Tests for GET /agents/profiles/{name}."""

    def test_returns_full_profile(self, client) -> None:
        """The endpoint should serialize the parsed AgentProfile with None fields excluded."""
        profile = AgentProfile(
            name="developer",
            description="Developer agent",
            role="developer",
            system_prompt="Implement the task.",
            mcpServers={"cao": {"command": "cao-mcp-server"}},
        )

        with patch(
            "cli_agent_orchestrator.api.main.load_agent_profile",
            return_value=profile,
        ) as mock_load:
            response = client.get("/agents/profiles/developer")

        assert response.status_code == 200
        assert response.json() == {
            "name": "developer",
            "description": "Developer agent",
            "role": "developer",
            "system_prompt": "Implement the task.",
            "mcpServers": {"cao": {"command": "cao-mcp-server"}},
        }
        mock_load.assert_called_once_with("developer")

    def test_returns_404_for_missing_profile(self, client) -> None:
        """Missing profiles should return 404 with the underlying error message."""
        with patch(
            "cli_agent_orchestrator.api.main.load_agent_profile",
            side_effect=FileNotFoundError("Agent profile not found: missing"),
        ):
            response = client.get("/agents/profiles/missing")

        assert response.status_code == 404
        assert response.json()["detail"] == "Agent profile not found: missing"

    def test_returns_500_for_parse_failure(self, client) -> None:
        """Malformed profiles should return 500."""
        with patch(
            "cli_agent_orchestrator.api.main.load_agent_profile",
            side_effect=RuntimeError("Failed to load agent profile 'bad': malformed frontmatter"),
        ):
            response = client.get("/agents/profiles/bad")

        assert response.status_code == 500
        assert "malformed frontmatter" in response.json()["detail"]

    def test_rejects_path_traversal_names(self, client) -> None:
        """Traversal attempts should be rejected by the real profile name validator."""
        response = client.get("/agents/profiles/..evil")

        assert response.status_code == 400
        assert "Invalid agent name" in response.json()["detail"]


class TestInstallAgentProfileEndpoint:
    """Tests for POST /agents/profiles/install."""

    def test_returns_install_result(self, client) -> None:
        """Successful installs should return the structured InstallResult payload."""
        service_result = InstallResult(
            success=True,
            message="Agent 'developer' installed successfully",
            agent_name="developer",
            context_file="/tmp/agent-context/developer.md",
            agent_file="/tmp/kiro/developer.json",
            unresolved_vars=["BASE_URL"],
        )

        with patch(
            "cli_agent_orchestrator.api.main.install_agent",
            return_value=service_result,
        ) as mock_install:
            response = client.post(
                "/agents/profiles/install",
                json={
                    "source": "developer",
                    "provider": "kiro_cli",
                    "env_vars": {
                        "API_TOKEN": "secret-token",
                        "BASE_URL": "http://localhost:27124",
                    },
                },
            )

        assert response.status_code == 200
        assert response.json() == service_result.model_dump()
        mock_install.assert_called_once_with(
            source="developer",
            provider="kiro_cli",
            env_vars={
                "API_TOKEN": "secret-token",
                "BASE_URL": "http://localhost:27124",
            },
        )

    def test_returns_400_for_invalid_source(self, client) -> None:
        """Structured service failures should be surfaced as 400s."""
        with patch(
            "cli_agent_orchestrator.api.main.install_agent",
            return_value=InstallResult(success=False, message="Agent profile not found: missing"),
        ):
            response = client.post(
                "/agents/profiles/install",
                json={"source": "missing", "provider": "kiro_cli"},
            )

        assert response.status_code == 400
        assert response.json()["detail"] == "Agent profile not found: missing"

    def test_returns_400_for_invalid_provider(self, client) -> None:
        """Invalid providers should be rejected by the install service."""
        response = client.post(
            "/agents/profiles/install",
            json={"source": "developer", "provider": "bad_provider"},
        )

        assert response.status_code == 400
        assert "Invalid provider 'bad_provider'" in response.json()["detail"]

    def test_returns_422_for_malformed_env_vars(self, client) -> None:
        """Env vars with the wrong type should be rejected by Pydantic validation."""
        response = client.post(
            "/agents/profiles/install",
            json={"source": "developer", "env_vars": "INVALID_FORMAT"},
        )

        assert response.status_code == 422
