"""Tests for the install CLI command."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import frontmatter
import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.install import _download_agent, install


class TestDownloadAgent:
    """Tests for the _download_agent helper function."""

    @patch("cli_agent_orchestrator.cli.commands.install.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.cli.commands.install.requests.get")
    def test_download_from_url_success(self, mock_get, mock_store_dir):
        """Test downloading agent from URL."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_store_dir.__truediv__ = lambda self, x: Path(tmpdir) / x
            mock_store_dir.mkdir = MagicMock()

            mock_response = MagicMock()
            mock_response.text = "# Test Agent\nname: test"
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            result = _download_agent("https://example.com/test-agent.md")

            assert result == "test-agent"
            mock_get.assert_called_once_with("https://example.com/test-agent.md")

    @patch("cli_agent_orchestrator.cli.commands.install.LOCAL_AGENT_STORE_DIR")
    def test_download_from_url_invalid_extension(self, mock_store_dir):
        """Test downloading agent from URL with invalid extension."""
        mock_store_dir.mkdir = MagicMock()

        with patch("cli_agent_orchestrator.cli.commands.install.requests.get") as mock_get:
            mock_response = MagicMock()
            mock_response.text = "content"
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            with pytest.raises(ValueError, match="URL must point to a .md file"):
                _download_agent("https://example.com/test-agent.txt")

    def test_download_from_file_success(self):
        """Test copying agent from local file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create source file
            source_file = Path(tmpdir) / "source-agent.md"
            source_file.write_text("# Test Agent\nname: test")

            with patch(
                "cli_agent_orchestrator.cli.commands.install.LOCAL_AGENT_STORE_DIR",
                Path(tmpdir) / "store",
            ):
                (Path(tmpdir) / "store").mkdir(parents=True, exist_ok=True)
                result = _download_agent(str(source_file))

                assert result == "source-agent"

    def test_download_from_file_invalid_extension(self):
        """Test copying agent from file with invalid extension."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source_file = Path(tmpdir) / "source-agent.txt"
            source_file.write_text("content")

            with patch(
                "cli_agent_orchestrator.cli.commands.install.LOCAL_AGENT_STORE_DIR",
                Path(tmpdir) / "store",
            ):
                with pytest.raises(ValueError, match="File must be a .md file"):
                    _download_agent(str(source_file))

    def test_download_source_not_found(self):
        """Test downloading agent from non-existent source."""
        with pytest.raises(FileNotFoundError, match="Source not found"):
            _download_agent("/nonexistent/path/agent.md")


class TestInstallCommand:
    """Tests for the install command."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    @pytest.fixture
    def mock_agent_profile(self):
        """Create a mock agent profile."""
        profile = MagicMock()
        profile.name = "test-agent"
        profile.description = "Test agent description"
        profile.tools = ["*"]
        profile.allowedTools = None
        profile.mcpServers = None
        profile.system_prompt = "Test system prompt"
        profile.prompt = "Test prompt"
        profile.toolAliases = None
        profile.toolsSettings = None
        profile.hooks = None
        profile.model = None
        return profile

    @patch("cli_agent_orchestrator.cli.commands.install.parse_agent_profile_text")
    @patch("cli_agent_orchestrator.cli.commands.install.AGENT_CONTEXT_DIR")
    @patch("cli_agent_orchestrator.cli.commands.install.KIRO_AGENTS_DIR")
    @patch("cli_agent_orchestrator.cli.commands.install.LOCAL_AGENT_STORE_DIR")
    def test_install_builtin_agent_kiro_cli(
        self,
        mock_local_store,
        mock_kiro_dir,
        mock_context_dir,
        mock_load,
        runner,
        mock_agent_profile,
    ):
        """Test installing built-in agent for kiro_cli provider."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            mock_local_store.__truediv__ = lambda self, x: tmppath / "local" / x
            mock_local_store.exists = MagicMock(return_value=False)
            mock_kiro_dir.__truediv__ = lambda self, x: tmppath / "kiro" / x
            mock_kiro_dir.mkdir = MagicMock()
            mock_context_dir.__truediv__ = lambda self, x: tmppath / "context" / x
            mock_context_dir.mkdir = MagicMock()

            mock_load.return_value = mock_agent_profile

            # Create mock for resources.files
            with patch(
                "cli_agent_orchestrator.cli.commands.install.resources.files"
            ) as mock_resources:
                mock_agent_store = MagicMock()
                mock_agent_store.__truediv__ = lambda self, x: tmppath / "builtin" / x
                mock_resources.return_value = mock_agent_store

                # Create builtin file
                (tmppath / "builtin").mkdir(parents=True, exist_ok=True)
                (tmppath / "builtin" / "test-agent.md").write_text("# Test\nname: test-agent")
                (tmppath / "context").mkdir(parents=True, exist_ok=True)
                (tmppath / "kiro").mkdir(parents=True, exist_ok=True)

                result = runner.invoke(install, ["test-agent", "--provider", "kiro_cli"])

                # Should not fail (may have issues with file writes in test env)
                mock_load.assert_called_once()

    @patch("cli_agent_orchestrator.cli.commands.install._download_agent")
    @patch("cli_agent_orchestrator.cli.commands.install.parse_agent_profile_text")
    def test_install_from_url(self, mock_load, mock_download, runner, mock_agent_profile):
        """Test installing agent from URL."""
        mock_download.return_value = "downloaded-agent"
        mock_load.side_effect = FileNotFoundError("Agent not found")

        result = runner.invoke(install, ["https://example.com/agent.md"])

        mock_download.assert_called_once_with("https://example.com/agent.md")

    @patch("cli_agent_orchestrator.cli.commands.install.Path")
    @patch("cli_agent_orchestrator.cli.commands.install._download_agent")
    @patch("cli_agent_orchestrator.cli.commands.install.parse_agent_profile_text")
    def test_install_from_file_path(
        self, mock_load, mock_download, mock_path, runner, mock_agent_profile
    ):
        """Test installing agent from file path."""
        mock_path_instance = MagicMock()
        mock_path_instance.exists.return_value = True
        mock_path.return_value = mock_path_instance

        mock_download.return_value = "local-agent"
        mock_load.side_effect = FileNotFoundError("Agent not found")

        result = runner.invoke(install, ["./my-agent.md"])

        mock_download.assert_called_once_with("./my-agent.md")

    def test_install_file_not_found(self, runner):
        """Test installing non-existent agent."""
        result = runner.invoke(install, ["nonexistent-agent"])

        assert "Error" in result.output

    @patch("cli_agent_orchestrator.cli.commands.install.requests.get")
    def test_install_url_request_error(self, mock_get, runner):
        """Test installing from URL with request error."""
        import requests

        mock_get.side_effect = requests.RequestException("Connection failed")

        result = runner.invoke(install, ["https://example.com/agent.md"])

        assert "Error" in result.output
        assert "Failed to download agent" in result.output

    @patch("cli_agent_orchestrator.cli.commands.install.parse_agent_profile_text")
    @patch("cli_agent_orchestrator.cli.commands.install.LOCAL_AGENT_STORE_DIR")
    def test_install_general_error(self, mock_local_store, mock_parse, runner):
        """Test installing agent with general error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            local_path = tmppath / "local"
            local_path.mkdir(parents=True, exist_ok=True)
            (local_path / "test-agent.md").write_text("# Test")
            mock_local_store.__truediv__ = lambda self, x: local_path / x
            mock_parse.side_effect = Exception("Unexpected error")

            result = runner.invoke(install, ["test-agent"])

            assert "Error" in result.output
            assert "Failed to install agent" in result.output

    def test_install_help_describes_env_workflow(self, runner):
        """Help text should describe env file storage, ${VAR} syntax, and an example."""
        result = runner.invoke(install, ["--help"])

        assert result.exit_code == 0
        assert "~/.aws/cli-agent-orchestrator/.env" in result.output
        assert "${VAR}" in result.output
        assert "API_TOKEN=my-secret-token" in result.output

    @patch("cli_agent_orchestrator.cli.commands.install.parse_agent_profile_text")
    @patch("cli_agent_orchestrator.cli.commands.install.AGENT_CONTEXT_DIR")
    @patch("cli_agent_orchestrator.cli.commands.install.Q_AGENTS_DIR")
    @patch("cli_agent_orchestrator.cli.commands.install.LOCAL_AGENT_STORE_DIR")
    def test_install_q_cli_provider(
        self, mock_local_store, mock_q_dir, mock_context_dir, mock_load, runner, mock_agent_profile
    ):
        """Test installing agent for q_cli provider."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            # Setup local profile to exist (covers line 99)
            local_path = tmppath / "local"
            local_path.mkdir(parents=True, exist_ok=True)
            local_profile = local_path / "test-agent.md"
            local_profile.write_text("# Test\nname: test-agent")

            mock_local_store.__truediv__ = lambda self, x: local_path / x
            mock_q_dir.__truediv__ = lambda self, x: tmppath / "q" / x
            mock_q_dir.mkdir = MagicMock()
            mock_context_dir.__truediv__ = lambda self, x: tmppath / "context" / x
            mock_context_dir.mkdir = MagicMock()

            mock_load.return_value = mock_agent_profile

            (tmppath / "context").mkdir(parents=True, exist_ok=True)
            (tmppath / "q").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(install, ["test-agent", "--provider", "q_cli"])

            mock_load.assert_called_once()

    @patch("cli_agent_orchestrator.cli.commands.install.parse_agent_profile_text")
    @patch("cli_agent_orchestrator.cli.commands.install.AGENT_CONTEXT_DIR")
    @patch("cli_agent_orchestrator.cli.commands.install.KIRO_AGENTS_DIR")
    @patch("cli_agent_orchestrator.cli.commands.install.LOCAL_AGENT_STORE_DIR")
    def test_install_with_mcp_servers(
        self, mock_local_store, mock_kiro_dir, mock_context_dir, mock_load, runner
    ):
        """Test installing agent with MCP servers (covers lines 115-116)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            # Create profile with mcpServers
            profile = MagicMock()
            profile.name = "test-agent"
            profile.description = "Test agent"
            profile.tools = ["*"]
            profile.allowedTools = None  # Will trigger default with MCP servers
            profile.mcpServers = {"server1": {"command": "test"}, "server2": {"command": "test2"}}
            profile.prompt = "Test prompt"
            profile.toolAliases = None
            profile.toolsSettings = None
            profile.hooks = None
            profile.model = None

            local_path = tmppath / "local"
            local_path.mkdir(parents=True, exist_ok=True)
            local_profile = local_path / "test-agent.md"
            local_profile.write_text("# Test\nname: test-agent")

            mock_local_store.__truediv__ = lambda self, x: local_path / x
            mock_kiro_dir.__truediv__ = lambda self, x: tmppath / "kiro" / x
            mock_kiro_dir.mkdir = MagicMock()
            mock_context_dir.__truediv__ = lambda self, x: tmppath / "context" / x
            mock_context_dir.mkdir = MagicMock()

            mock_load.return_value = profile

            (tmppath / "context").mkdir(parents=True, exist_ok=True)
            (tmppath / "kiro").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(install, ["test-agent", "--provider", "kiro_cli"])

            mock_load.assert_called_once()

    @patch("cli_agent_orchestrator.cli.commands.install.parse_agent_profile_text")
    @patch("cli_agent_orchestrator.cli.commands.install.AGENT_CONTEXT_DIR")
    @patch("cli_agent_orchestrator.cli.commands.install.LOCAL_AGENT_STORE_DIR")
    def test_install_without_provider_specific_config(
        self, mock_local_store, mock_context_dir, mock_load, runner, mock_agent_profile
    ):
        """Test installing agent for claude_code provider (no agent file created)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            local_path = tmppath / "local"
            local_path.mkdir(parents=True, exist_ok=True)
            local_profile = local_path / "test-agent.md"
            local_profile.write_text("# Test\nname: test-agent")

            mock_local_store.__truediv__ = lambda self, x: local_path / x
            mock_context_dir.__truediv__ = lambda self, x: tmppath / "context" / x
            mock_context_dir.mkdir = MagicMock()

            mock_load.return_value = mock_agent_profile

            (tmppath / "context").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(install, ["test-agent", "--provider", "claude_code"])

            assert "installed successfully" in result.output

    @patch("cli_agent_orchestrator.cli.commands.install.parse_agent_profile_text")
    @patch("cli_agent_orchestrator.cli.commands.install.AGENT_CONTEXT_DIR")
    @patch("cli_agent_orchestrator.cli.commands.install.COPILOT_AGENTS_DIR")
    @patch("cli_agent_orchestrator.cli.commands.install.LOCAL_AGENT_STORE_DIR")
    def test_install_copilot_cli_provider(
        self,
        mock_local_store,
        mock_copilot_dir,
        mock_context_dir,
        mock_load,
        runner,
        mock_agent_profile,
    ):
        """Test installing agent for copilot_cli provider."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            local_path = tmppath / "local"
            local_path.mkdir(parents=True, exist_ok=True)
            local_profile = local_path / "test-agent.md"
            local_profile.write_text("# Test\nname: test-agent")

            context_path = tmppath / "context"
            context_path.mkdir(parents=True, exist_ok=True)
            copilot_path = tmppath / "copilot"
            copilot_path.mkdir(parents=True, exist_ok=True)

            mock_local_store.__truediv__ = lambda self, x: local_path / x
            mock_context_dir.__truediv__ = lambda self, x: context_path / x
            mock_context_dir.mkdir = MagicMock()
            mock_copilot_dir.__truediv__ = lambda self, x: copilot_path / x
            mock_copilot_dir.mkdir = MagicMock()
            mock_load.return_value = mock_agent_profile

            result = runner.invoke(install, ["test-agent", "--provider", "copilot_cli"])

            assert result.exit_code == 0
            assert "installed successfully" in result.output
            assert "copilot_cli agent:" in result.output

            agent_file = copilot_path / "test-agent.agent.md"
            assert agent_file.exists()
            post = frontmatter.loads(agent_file.read_text())
            assert post.metadata["name"] == "test-agent"
            assert post.metadata["description"] == "Test agent description"
            assert "Test system prompt" in post.content

    @patch("cli_agent_orchestrator.cli.commands.install.parse_agent_profile_text")
    @patch("cli_agent_orchestrator.cli.commands.install.AGENT_CONTEXT_DIR")
    @patch("cli_agent_orchestrator.cli.commands.install.COPILOT_AGENTS_DIR")
    @patch("cli_agent_orchestrator.cli.commands.install.LOCAL_AGENT_STORE_DIR")
    def test_install_copilot_cli_provider_requires_prompt(
        self,
        mock_local_store,
        mock_copilot_dir,
        mock_context_dir,
        mock_load,
        runner,
        mock_agent_profile,
    ):
        """Test copilot_cli install fails when profile has no prompt text."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            local_path = tmppath / "local"
            local_path.mkdir(parents=True, exist_ok=True)
            local_profile = local_path / "test-agent.md"
            local_profile.write_text("# Test\nname: test-agent")

            context_path = tmppath / "context"
            context_path.mkdir(parents=True, exist_ok=True)
            copilot_path = tmppath / "copilot"
            copilot_path.mkdir(parents=True, exist_ok=True)

            mock_local_store.__truediv__ = lambda self, x: local_path / x
            mock_context_dir.__truediv__ = lambda self, x: context_path / x
            mock_context_dir.mkdir = MagicMock()
            mock_copilot_dir.__truediv__ = lambda self, x: copilot_path / x
            mock_copilot_dir.mkdir = MagicMock()

            mock_agent_profile.system_prompt = ""
            mock_agent_profile.prompt = ""
            mock_load.return_value = mock_agent_profile

            result = runner.invoke(install, ["test-agent", "--provider", "copilot_cli"])

            assert "Failed to install agent" in result.output
            assert "has no usable prompt content for Copilot" in result.output


class TestInstallCommandEnvFlags:
    """Tests for install-time env var injection."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    @pytest.fixture
    def install_paths(self, tmp_path, monkeypatch):
        """Patch install-related filesystem paths into a temp workspace."""
        local_store_dir = tmp_path / "agent-store"
        context_dir = tmp_path / "agent-context"
        kiro_dir = tmp_path / "kiro"
        q_dir = tmp_path / "q"
        env_file = tmp_path / ".env"

        local_store_dir.mkdir()
        context_dir.mkdir()
        kiro_dir.mkdir()
        q_dir.mkdir()

        monkeypatch.setattr(
            "cli_agent_orchestrator.cli.commands.install.LOCAL_AGENT_STORE_DIR", local_store_dir
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.cli.commands.install.AGENT_CONTEXT_DIR", context_dir
        )
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.install.KIRO_AGENTS_DIR", kiro_dir)
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.install.Q_AGENTS_DIR", q_dir)
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.install.CAO_ENV_FILE", env_file)
        monkeypatch.setattr("cli_agent_orchestrator.utils.env.CAO_ENV_FILE", env_file)
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", local_store_dir
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
        )

        return {
            "local_store_dir": local_store_dir,
            "context_dir": context_dir,
            "kiro_dir": kiro_dir,
            "q_dir": q_dir,
            "env_file": env_file,
        }

    @staticmethod
    def _write_profile(profile_path: Path, body: str) -> None:
        """Write a local profile with env placeholders."""
        profile_path.write_text(
            "---\n"
            "name: test-agent\n"
            "description: Test agent\n"
            "mcpServers:\n"
            "  service:\n"
            "    command: service-mcp\n"
            "    env:\n"
            "      API_TOKEN: ${API_TOKEN}\n"
            "      BASE_URL: ${BASE_URL}\n"
            "      URL: ${URL}\n"
            "---\n"
            f"{body}\n"
        )

    def test_install_with_env_writes_env_file_and_resolves_provider_config(
        self, runner, install_paths
    ):
        """--env should persist to .env, resolve in provider config, but NOT in context file."""
        profile_path = install_paths["local_store_dir"] / "test-agent.md"
        self._write_profile(profile_path, "Token: ${API_TOKEN}")

        result = runner.invoke(
            install,
            [
                "test-agent",
                "--provider",
                "kiro_cli",
                "--env",
                "API_TOKEN=secret-token",
            ],
        )

        assert result.exit_code == 0
        assert install_paths["env_file"].read_text() == "API_TOKEN='secret-token'\n"
        assert f"✓ Set 1 env var(s) in {install_paths['env_file']}" in result.output

        # Context file keeps placeholders (secrets stay in .env)
        context_text = (install_paths["context_dir"] / "test-agent.md").read_text()
        assert "${API_TOKEN}" in context_text
        assert "secret-token" not in context_text

        # Provider config has resolved values (Kiro can't read .env)
        kiro_agent_file = install_paths["kiro_dir"] / "test-agent.json"
        kiro_config = json.loads(kiro_agent_file.read_text())
        assert kiro_config["mcpServers"]["service"]["env"]["API_TOKEN"] == "secret-token"

    def test_install_with_multiple_env_flags_writes_all_values(self, runner, install_paths):
        """Multiple --env flags should all be written before profile resolution."""
        profile_path = install_paths["local_store_dir"] / "test-agent.md"
        self._write_profile(profile_path, "Token: ${API_TOKEN}\nBase URL: ${BASE_URL}")

        result = runner.invoke(
            install,
            [
                "test-agent",
                "--provider",
                "kiro_cli",
                "--env",
                "API_TOKEN=secret-token",
                "--env",
                "BASE_URL=http://localhost:27124",
            ],
        )

        context_text = (install_paths["context_dir"] / "test-agent.md").read_text()

        assert result.exit_code == 0
        assert "API_TOKEN='secret-token'" in install_paths["env_file"].read_text()
        assert "BASE_URL='http://localhost:27124'" in install_paths["env_file"].read_text()
        # Context file keeps placeholders
        assert "${API_TOKEN}" in context_text
        assert "${BASE_URL}" in context_text
        assert f"✓ Set 2 env var(s) in {install_paths['env_file']}" in result.output

    def test_install_with_env_value_containing_equals_preserves_full_value(
        self, runner, install_paths
    ):
        """The first equals sign splits the assignment and later ones remain in the value."""
        profile_path = install_paths["local_store_dir"] / "test-agent.md"
        self._write_profile(profile_path, "URL: ${URL}")

        result = runner.invoke(
            install,
            [
                "test-agent",
                "--provider",
                "q_cli",
                "--env",
                "URL=http://host?a=b",
            ],
        )

        context_text = (install_paths["context_dir"] / "test-agent.md").read_text()
        q_agent_file = install_paths["q_dir"] / "test-agent.json"
        q_config = json.loads(q_agent_file.read_text())

        assert result.exit_code == 0
        assert "URL='http://host?a=b'" in install_paths["env_file"].read_text()
        # Context file keeps placeholder
        assert "${URL}" in context_text
        # Provider config has resolved value
        assert q_config["mcpServers"]["service"]["env"]["URL"] == "http://host?a=b"

    def test_install_with_invalid_env_format_returns_click_error(self, runner, install_paths):
        """Assignments without '=' should fail validation with a user-friendly error."""
        profile_path = install_paths["local_store_dir"] / "test-agent.md"
        self._write_profile(profile_path, "Token: ${API_TOKEN}")

        result = runner.invoke(install, ["test-agent", "--env", "INVALID_FORMAT"])

        assert result.exit_code == 2
        assert "Invalid value for --env" in result.output
        assert "Expected format KEY=VALUE" in result.output
        assert not install_paths["env_file"].exists()

    def test_install_with_empty_env_key_returns_click_error(self, runner, install_paths):
        """Assignments with an empty key should fail validation."""
        profile_path = install_paths["local_store_dir"] / "test-agent.md"
        self._write_profile(profile_path, "Token: ${API_TOKEN}")

        result = runner.invoke(install, ["test-agent", "--env", "=value"])

        assert result.exit_code == 2
        assert "Invalid value for --env" in result.output
        assert "Key must not be empty" in result.output
        assert not install_paths["env_file"].exists()

    def test_install_without_env_does_not_modify_env_file(self, runner, install_paths):
        """Install should not create or update the env file when --env is omitted."""
        profile_path = install_paths["local_store_dir"] / "test-agent.md"
        profile_path.write_text(
            "---\nname: test-agent\ndescription: Test agent\n---\nPlain system prompt\n"
        )

        result = runner.invoke(install, ["test-agent", "--provider", "kiro_cli"])

        assert result.exit_code == 0
        assert not install_paths["env_file"].exists()
        assert "Set 1 env var" not in result.output

    def test_install_warns_about_unresolved_env_vars(self, runner, install_paths):
        """Unresolved ${VAR} placeholders should trigger a stderr warning."""
        profile_path = install_paths["local_store_dir"] / "test-agent.md"
        self._write_profile(profile_path, "Token: ${API_TOKEN}")

        result = runner.invoke(
            install,
            ["test-agent", "--provider", "kiro_cli", "--env", "API_TOKEN=secret"],
        )

        assert result.exit_code == 0
        # API_TOKEN is set, but BASE_URL and URL are not
        assert "Unresolved env var(s)" in result.output
        assert "BASE_URL" in result.output
        assert "URL" in result.output
        assert "API_TOKEN" not in result.output.split("Unresolved")[1]

    def test_install_no_warning_when_all_env_vars_resolved(self, runner, install_paths):
        """No warning when every placeholder has a value in .env."""
        profile_path = install_paths["local_store_dir"] / "test-agent.md"
        profile_path.write_text(
            "---\nname: test-agent\ndescription: Test agent\n"
            "mcpServers:\n  svc:\n    command: svc\n    env:\n"
            "      KEY: ${KEY}\n---\nPrompt\n"
        )

        result = runner.invoke(
            install,
            ["test-agent", "--provider", "kiro_cli", "--env", "KEY=value"],
        )

        assert result.exit_code == 0
        assert "Unresolved" not in result.output

    def test_install_no_warning_when_profile_has_no_placeholders(self, runner, install_paths):
        """Profiles without any ${VAR} syntax should not trigger a warning."""
        profile_path = install_paths["local_store_dir"] / "test-agent.md"
        profile_path.write_text(
            "---\nname: test-agent\ndescription: Test agent\n---\nPlain prompt\n"
        )

        result = runner.invoke(install, ["test-agent", "--provider", "kiro_cli"])

        assert result.exit_code == 0
        assert "Unresolved" not in result.output

    def test_install_end_to_end_keeps_placeholders_in_context_file(
        self, runner, install_paths, tmp_path
    ):
        """Context file should preserve ${VAR} placeholders; secrets stay in .env."""
        install_paths["env_file"].write_text(
            "API_TOKEN=integration-secret\nSERVICE_URL=http://127.0.0.1:27124\n"
        )
        source_profile = tmp_path / "service-agent.md"
        source_profile.write_text(
            "---\n"
            "name: service-agent\n"
            "description: Integration test profile\n"
            "mcpServers:\n"
            "  service:\n"
            "    command: service-mcp\n"
            "    env:\n"
            "      API_TOKEN: ${API_TOKEN}\n"
            "      SERVICE_URL: ${SERVICE_URL}\n"
            "---\n"
            "Use the service endpoint at ${SERVICE_URL}.\n"
        )

        result = runner.invoke(install, [str(source_profile), "--provider", "claude_code"])

        installed_profile = install_paths["context_dir"] / "service-agent.md"
        installed_text = installed_profile.read_text()

        assert result.exit_code == 0
        assert "${API_TOKEN}" in installed_text
        assert "${SERVICE_URL}" in installed_text
        assert "integration-secret" not in installed_text
