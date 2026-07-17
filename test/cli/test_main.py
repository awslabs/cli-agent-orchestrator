"""Tests for CLI main entry point."""

import importlib
from importlib.metadata import PackageNotFoundError, version

from click.testing import CliRunner

from cli_agent_orchestrator.cli.main import cli


class TestCliMain:
    """Tests for main CLI group."""

    def test_cli_help(self):
        """Test CLI help command."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert "CLI Agent Orchestrator" in result.output

    def test_cli_has_launch_command(self):
        """Test CLI has launch command."""
        runner = CliRunner()
        result = runner.invoke(cli, ["launch", "--help"])

        assert result.exit_code == 0
        assert "Launch" in result.output or "launch" in result.output.lower()

    def test_cli_has_init_command(self):
        """Test CLI has init command."""
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--help"])

        assert result.exit_code == 0

    def test_cli_has_install_command(self):
        """Test CLI has install command."""
        runner = CliRunner()
        result = runner.invoke(cli, ["install", "--help"])

        assert result.exit_code == 0

    def test_cli_has_shutdown_command(self):
        """Test CLI has shutdown command."""
        runner = CliRunner()
        result = runner.invoke(cli, ["shutdown", "--help"])

        assert result.exit_code == 0

    def test_cli_has_schedule_command(self):
        """Test CLI has schedule command group."""
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "--help"])

        assert result.exit_code == 0

    def test_cli_flow_alias_still_works(self):
        """Test deprecated 'flow' alias still resolves (issue #378)."""
        runner = CliRunner()
        result = runner.invoke(cli, ["flow", "--help"])

        assert result.exit_code == 0

    def test_cli_has_skills_command(self):
        """Test CLI has skills command group."""
        runner = CliRunner()
        result = runner.invoke(cli, ["skills", "--help"])

        assert result.exit_code == 0

    def test_cli_has_skills_add_help(self):
        """Test CLI has skills add subcommand help."""
        runner = CliRunner()
        result = runner.invoke(cli, ["skills", "add", "--help"])

        assert result.exit_code == 0

    def test_cli_has_skills_remove_help(self):
        """Test CLI has skills remove subcommand help."""
        runner = CliRunner()
        result = runner.invoke(cli, ["skills", "remove", "--help"])

        assert result.exit_code == 0

    def test_cli_has_skills_list_help(self):
        """Test CLI has skills list subcommand help."""
        runner = CliRunner()
        result = runner.invoke(cli, ["skills", "list", "--help"])

        assert result.exit_code == 0

    def test_cli_has_update_command(self):
        """Test CLI has update command (issue #26)."""
        runner = CliRunner()
        result = runner.invoke(cli, ["update", "--help"])

        assert result.exit_code == 0
        assert "Update CAO" in result.output

    def test_cli_unknown_command(self):
        """Test CLI with unknown command."""
        runner = CliRunner()
        result = runner.invoke(cli, ["unknown-command"])

        assert result.exit_code != 0

    def test_cli_version_long_flag(self):
        """Test --version prints the installed package version and exits 0."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--version"])

        assert result.exit_code == 0
        assert "cao" in result.output
        assert version("cli-agent-orchestrator") in result.output

    def test_cli_version_short_flag(self):
        """Test -V prints the installed package version and exits 0."""
        runner = CliRunner()
        result = runner.invoke(cli, ["-V"])

        assert result.exit_code == 0
        assert "cao" in result.output
        assert version("cli-agent-orchestrator") in result.output

    def test_cli_version_fallback_when_package_not_found(self, mocker):
        """Test that a missing package metadata falls back gracefully instead of crashing."""
        main_module = importlib.import_module("cli_agent_orchestrator.cli.main")
        with mocker.patch(
            "importlib.metadata.version",
            side_effect=PackageNotFoundError("cli-agent-orchestrator"),
        ):
            importlib.reload(main_module)
            assert main_module.__version__ == "unknown"

            runner = CliRunner()
            result = runner.invoke(main_module.cli, ["--help"])
            assert result.exit_code == 0

        importlib.reload(main_module)  # patch is undone here — real version restored
