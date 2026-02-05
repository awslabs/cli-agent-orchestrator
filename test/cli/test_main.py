"""Tests for CLI main entry point."""

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

    def test_cli_has_flow_command(self):
        """Test CLI has flow command group."""
        runner = CliRunner()
        result = runner.invoke(cli, ["flow", "--help"])

        assert result.exit_code == 0

    def test_cli_unknown_command(self):
        """Test CLI with unknown command."""
        runner = CliRunner()
        result = runner.invoke(cli, ["unknown-command"])

        assert result.exit_code != 0
