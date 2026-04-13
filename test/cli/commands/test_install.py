"""Tests for the install CLI command wrapper."""

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.install import install
from cli_agent_orchestrator.services.install_service import InstallResult


class TestInstallCommand:
    """Tests for the thin CLI wrapper around install_service.install_agent."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_install_help_describes_env_workflow(self, runner: CliRunner) -> None:
        """Help text should describe env file storage, ${VAR} syntax, and an example."""
        result = runner.invoke(install, ["--help"])

        assert result.exit_code == 0
        assert "~/.aws/cli-agent-orchestrator/.env" in result.output
        assert "${VAR}" in result.output
        assert "API_TOKEN=my-secret-token" in result.output

    def test_install_success_outputs_result_details(self, runner: CliRunner) -> None:
        """Successful installs should print the same user-facing summary lines."""
        service_result = InstallResult(
            success=True,
            message="Agent 'developer' installed successfully",
            agent_name="developer",
            context_file="/tmp/agent-context/developer.md",
            agent_file="/tmp/kiro/developer.json",
            unresolved_vars=["BASE_URL"],
            source_kind="name",
        )

        with patch(
            "cli_agent_orchestrator.cli.commands.install.install_agent",
            return_value=service_result,
        ) as mock_install:
            result = runner.invoke(
                install,
                [
                    "developer",
                    "--provider",
                    "kiro_cli",
                    "--env",
                    "API_TOKEN=secret-token",
                ],
            )

        assert result.exit_code == 0
        assert "Agent 'developer' installed successfully" in result.output
        assert "Set 1 env var(s)" in result.output
        assert "Unresolved env var(s) in profile: BASE_URL" in result.output
        assert "cao env set" in result.output
        assert "Context file: /tmp/agent-context/developer.md" in result.output
        assert "kiro_cli agent: /tmp/kiro/developer.json" in result.output
        mock_install.assert_called_once_with(
            "developer",
            "kiro_cli",
            {"API_TOKEN": "secret-token"},
        )

    def test_install_url_source_prints_download_confirmation(self, runner: CliRunner) -> None:
        """URL installs should print a download confirmation line."""
        service_result = InstallResult(
            success=True,
            message="Agent 'remote' installed successfully",
            agent_name="remote",
            source_kind="url",
        )

        with patch(
            "cli_agent_orchestrator.cli.commands.install.install_agent",
            return_value=service_result,
        ):
            result = runner.invoke(
                install,
                ["https://example.com/remote.md", "--provider", "kiro_cli"],
            )

        assert result.exit_code == 0
        assert "Downloaded agent from URL to local store" in result.output
        assert "Agent 'remote' installed successfully" in result.output

    def test_install_file_source_prints_copy_confirmation(self, runner: CliRunner) -> None:
        """File path installs should print a copy confirmation line."""
        service_result = InstallResult(
            success=True,
            message="Agent 'local' installed successfully",
            agent_name="local",
            source_kind="file",
        )

        with patch(
            "cli_agent_orchestrator.cli.commands.install.install_agent",
            return_value=service_result,
        ):
            result = runner.invoke(
                install,
                ["./local.md", "--provider", "kiro_cli"],
            )

        assert result.exit_code == 0
        assert "Copied agent from file to local store" in result.output
        assert "Agent 'local' installed successfully" in result.output

    def test_install_failure_prints_error(self, runner: CliRunner) -> None:
        """Service failures should be surfaced as CLI errors without raising."""
        with patch(
            "cli_agent_orchestrator.cli.commands.install.install_agent",
            return_value=InstallResult(success=False, message="Source not found: missing"),
        ):
            result = runner.invoke(install, ["missing"])

        assert result.exit_code == 0
        assert "Error: Source not found: missing" in result.output

    def test_install_invalid_env_format_returns_click_error(self, runner: CliRunner) -> None:
        """Assignments without '=' should fail validation with a user-friendly error."""
        result = runner.invoke(install, ["developer", "--env", "INVALID_FORMAT"])

        assert result.exit_code == 2
        assert "Invalid value for --env" in result.output
        assert "Expected format KEY=VALUE" in result.output

    def test_install_empty_env_key_returns_click_error(self, runner: CliRunner) -> None:
        """Assignments with an empty key should fail validation."""
        result = runner.invoke(install, ["developer", "--env", "=value"])

        assert result.exit_code == 2
        assert "Invalid value for --env" in result.output
        assert "Key must not be empty" in result.output
