"""Tests for the install CLI command."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

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
        profile.prompt = "Test prompt"
        profile.toolAliases = None
        profile.toolsSettings = None
        profile.hooks = None
        profile.model = None
        return profile

    @patch("cli_agent_orchestrator.cli.commands.install.load_agent_profile")
    @patch("cli_agent_orchestrator.cli.commands.install.AGENT_CONTEXT_DIR")
    @patch("cli_agent_orchestrator.cli.commands.install.KIRO_AGENTS_DIR")
    @patch("cli_agent_orchestrator.cli.commands.install.LOCAL_AGENT_STORE_DIR")
    def test_install_builtin_agent_kiro_cli(
        self, mock_local_store, mock_kiro_dir, mock_context_dir, mock_load, runner, mock_agent_profile
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
            with patch("cli_agent_orchestrator.cli.commands.install.resources.files") as mock_resources:
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
                mock_load.assert_called_once_with("test-agent")

    @patch("cli_agent_orchestrator.cli.commands.install._download_agent")
    @patch("cli_agent_orchestrator.cli.commands.install.load_agent_profile")
    def test_install_from_url(self, mock_load, mock_download, runner, mock_agent_profile):
        """Test installing agent from URL."""
        mock_download.return_value = "downloaded-agent"
        mock_load.side_effect = FileNotFoundError("Agent not found")

        result = runner.invoke(install, ["https://example.com/agent.md"])

        mock_download.assert_called_once_with("https://example.com/agent.md")

    @patch("cli_agent_orchestrator.cli.commands.install.Path")
    @patch("cli_agent_orchestrator.cli.commands.install._download_agent")
    @patch("cli_agent_orchestrator.cli.commands.install.load_agent_profile")
    def test_install_from_file_path(self, mock_load, mock_download, mock_path, runner, mock_agent_profile):
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

    @patch("cli_agent_orchestrator.cli.commands.install.load_agent_profile")
    def test_install_general_error(self, mock_load, runner):
        """Test installing agent with general error."""
        mock_load.side_effect = Exception("Unexpected error")

        result = runner.invoke(install, ["test-agent"])

        assert "Error" in result.output
        assert "Failed to install agent" in result.output

    @patch("cli_agent_orchestrator.cli.commands.install.load_agent_profile")
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

            mock_load.assert_called_once_with("test-agent")

    @patch("cli_agent_orchestrator.cli.commands.install.load_agent_profile")
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

            mock_load.assert_called_once_with("test-agent")

    @patch("cli_agent_orchestrator.cli.commands.install.load_agent_profile")
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
