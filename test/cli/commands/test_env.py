"""Tests for the env CLI command group."""

from click.testing import CliRunner

from cli_agent_orchestrator.cli.main import cli


class TestEnvCommand:
    """Tests for the `cao env` command group."""

    def test_env_without_subcommand_shows_help(self):
        """The env group should show usage text when invoked without a subcommand."""
        runner = CliRunner()

        result = runner.invoke(cli, ["env"])

        assert result.exit_code == 0
        assert "Manage CAO environment variables" in result.output
        assert "set" in result.output
        assert "get" in result.output
        assert "list" in result.output
        assert "unset" in result.output

    def test_env_set_creates_env_file(self, tmp_path, monkeypatch):
        """`cao env set` should create the managed env file and write the key."""
        env_file = tmp_path / ".env"
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.env.CAO_ENV_FILE", env_file)
        monkeypatch.setattr("cli_agent_orchestrator.utils.env.CAO_ENV_FILE", env_file)
        runner = CliRunner()

        result = runner.invoke(cli, ["env", "set", "API_KEY", "secret"])

        assert result.exit_code == 0
        assert env_file.exists()
        assert "API_KEY='secret'" in env_file.read_text()
        assert f"✓ Set API_KEY in {env_file}" in result.output

    def test_env_set_overwrites_existing_key(self, tmp_path, monkeypatch):
        """`cao env set` should update an existing value."""
        env_file = tmp_path / ".env"
        env_file.write_text("API_KEY=old\n")
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.env.CAO_ENV_FILE", env_file)
        monkeypatch.setattr("cli_agent_orchestrator.utils.env.CAO_ENV_FILE", env_file)
        runner = CliRunner()

        result = runner.invoke(cli, ["env", "set", "API_KEY", "new"])

        assert result.exit_code == 0
        assert "API_KEY='new'" in env_file.read_text()

    def test_env_get_returns_value(self, tmp_path, monkeypatch):
        """`cao env get` should print the requested value."""
        env_file = tmp_path / ".env"
        env_file.write_text("API_KEY=secret\n")
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.env.CAO_ENV_FILE", env_file)
        monkeypatch.setattr("cli_agent_orchestrator.utils.env.CAO_ENV_FILE", env_file)
        runner = CliRunner()

        result = runner.invoke(cli, ["env", "get", "API_KEY"])

        assert result.exit_code == 0
        assert result.output.strip() == "secret"

    def test_env_get_nonexistent_key_exits_with_error(self, tmp_path, monkeypatch):
        """`cao env get` should exit with code 1 for missing keys."""
        env_file = tmp_path / ".env"
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.env.CAO_ENV_FILE", env_file)
        monkeypatch.setattr("cli_agent_orchestrator.utils.env.CAO_ENV_FILE", env_file)
        runner = CliRunner()

        result = runner.invoke(cli, ["env", "get", "MISSING_KEY"])

        assert result.exit_code == 1
        assert "Environment variable 'MISSING_KEY' not found." in result.output

    def test_env_list_shows_all_vars(self, tmp_path, monkeypatch):
        """`cao env list` should print each key-value pair on its own line."""
        env_file = tmp_path / ".env"
        env_file.write_text("API_KEY=secret\nBASE_URL=http://localhost:27124\n")
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.env.CAO_ENV_FILE", env_file)
        monkeypatch.setattr("cli_agent_orchestrator.utils.env.CAO_ENV_FILE", env_file)
        runner = CliRunner()

        result = runner.invoke(cli, ["env", "list"])

        assert result.exit_code == 0
        assert "API_KEY=secret" in result.output
        assert "BASE_URL=http://localhost:27124" in result.output

    def test_env_list_with_missing_file_shows_empty_message(self, tmp_path, monkeypatch):
        """`cao env list` should report empty state when the env file is missing."""
        env_file = tmp_path / ".env"
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.env.CAO_ENV_FILE", env_file)
        monkeypatch.setattr("cli_agent_orchestrator.utils.env.CAO_ENV_FILE", env_file)
        runner = CliRunner()

        result = runner.invoke(cli, ["env", "list"])

        assert result.exit_code == 0
        assert result.output.strip() == f"No env vars set in {env_file}"

    def test_env_list_with_empty_file_shows_empty_message(self, tmp_path, monkeypatch):
        """`cao env list` should report empty state when the env file has no values."""
        env_file = tmp_path / ".env"
        env_file.write_text("")
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.env.CAO_ENV_FILE", env_file)
        monkeypatch.setattr("cli_agent_orchestrator.utils.env.CAO_ENV_FILE", env_file)
        runner = CliRunner()

        result = runner.invoke(cli, ["env", "list"])

        assert result.exit_code == 0
        assert result.output.strip() == f"No env vars set in {env_file}"

    def test_env_unset_removes_key(self, tmp_path, monkeypatch):
        """`cao env unset` should remove the requested key."""
        env_file = tmp_path / ".env"
        env_file.write_text("API_KEY=secret\nBASE_URL=http://localhost:27124\n")
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.env.CAO_ENV_FILE", env_file)
        monkeypatch.setattr("cli_agent_orchestrator.utils.env.CAO_ENV_FILE", env_file)
        runner = CliRunner()

        result = runner.invoke(cli, ["env", "unset", "API_KEY"])

        assert result.exit_code == 0
        assert "API_KEY" not in env_file.read_text()
        assert "BASE_URL=http://localhost:27124" in env_file.read_text()
        assert f"✓ Unset API_KEY in {env_file}" in result.output

    def test_env_unset_nonexistent_key_does_not_error(self, tmp_path, monkeypatch):
        """`cao env unset` should be a no-op for missing keys."""
        env_file = tmp_path / ".env"
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.env.CAO_ENV_FILE", env_file)
        monkeypatch.setattr("cli_agent_orchestrator.utils.env.CAO_ENV_FILE", env_file)
        runner = CliRunner()

        result = runner.invoke(cli, ["env", "unset", "MISSING_KEY"])

        assert result.exit_code == 0
        assert not env_file.exists()
        assert f"✓ Unset MISSING_KEY in {env_file}" in result.output
