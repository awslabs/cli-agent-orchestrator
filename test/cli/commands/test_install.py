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
    @patch("cli_agent_orchestrator.cli.commands.install.CLAUDE_AGENTS_DIR")
    @patch("cli_agent_orchestrator.cli.commands.install.AGENT_CONTEXT_DIR")
    @patch("cli_agent_orchestrator.cli.commands.install.LOCAL_AGENT_STORE_DIR")
    def test_install_claude_code_provider(
        self,
        mock_local_store,
        mock_context_dir,
        mock_claude_dir,
        mock_load,
        runner,
        mock_agent_profile,
    ):
        """Test installing agent for claude_code provider writes correct .md file.

        The entire source file frontmatter is passed through wholesale; only
        permissionMode is forced to bypassPermissions.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            local_path = tmppath / "local"
            local_path.mkdir(parents=True, exist_ok=True)
            # Source has real YAML frontmatter + body — both should appear in output.
            local_profile = local_path / "test-agent.md"
            local_profile.write_text(
                "---\nname: test-agent\ndescription: Test agent description\n---\n\nDo the thing."
            )

            claude_dir = tmppath / "claude_agents"
            claude_dir.mkdir(parents=True, exist_ok=True)

            mock_local_store.__truediv__ = lambda self, x: local_path / x
            mock_context_dir.__truediv__ = lambda self, x: tmppath / "context" / x
            mock_context_dir.mkdir = MagicMock()
            mock_claude_dir.__truediv__ = lambda self, x: claude_dir / x
            mock_claude_dir.mkdir = MagicMock()

            mock_load.return_value = mock_agent_profile

            (tmppath / "context").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(install, ["test-agent", "--provider", "claude_code"])

            assert "installed successfully" in result.output

            # Verify the .md file was written with correct content
            agent_file = claude_dir / "test-agent.md"
            assert agent_file.exists(), f"Expected {agent_file} to be created"
            content = agent_file.read_text()

            assert "---" in content
            assert "name: test-agent" in content
            assert "permissionMode: bypassPermissions" in content
            # Body is taken wholesale from the source file, not from profile.system_prompt
            assert "Do the thing." in content

    @patch("cli_agent_orchestrator.cli.commands.install.load_agent_profile")
    @patch("cli_agent_orchestrator.cli.commands.install.CLAUDE_AGENTS_DIR")
    @patch("cli_agent_orchestrator.cli.commands.install.AGENT_CONTEXT_DIR")
    @patch("cli_agent_orchestrator.cli.commands.install.LOCAL_AGENT_STORE_DIR")
    def test_install_claude_code_no_system_prompt(
        self,
        mock_local_store,
        mock_context_dir,
        mock_claude_dir,
        mock_load,
        runner,
        mock_agent_profile,
    ):
        """Test Claude Code install when source file has no body produces empty body."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            local_path = tmppath / "local"
            local_path.mkdir(parents=True, exist_ok=True)
            # Source file has frontmatter but no body content
            (local_path / "test-agent.md").write_text(
                "---\nname: test-agent\ndescription: Test agent description\n---\n"
            )

            claude_dir = tmppath / "claude_agents"
            claude_dir.mkdir(parents=True, exist_ok=True)

            mock_local_store.__truediv__ = lambda self, x: local_path / x
            mock_context_dir.__truediv__ = lambda self, x: tmppath / "context" / x
            mock_context_dir.mkdir = MagicMock()
            mock_claude_dir.__truediv__ = lambda self, x: claude_dir / x
            mock_claude_dir.mkdir = MagicMock()

            mock_load.return_value = mock_agent_profile

            (tmppath / "context").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(install, ["test-agent", "--provider", "claude_code"])

            assert "installed successfully" in result.output
            agent_file = claude_dir / "test-agent.md"
            assert agent_file.exists()
            content = agent_file.read_text()
            # Body is empty — frontmatter.dumps() produces `---\n...\n---` with no
            # trailing newline when the body is empty.
            assert content.endswith("---")
            assert "permissionMode: bypassPermissions" in content

    @patch("cli_agent_orchestrator.cli.commands.install.load_agent_profile")
    @patch("cli_agent_orchestrator.cli.commands.install.CLAUDE_AGENTS_DIR")
    @patch("cli_agent_orchestrator.cli.commands.install.AGENT_CONTEXT_DIR")
    @patch("cli_agent_orchestrator.cli.commands.install.LOCAL_AGENT_STORE_DIR")
    def test_install_claude_code_slash_in_name_sanitized(
        self, mock_local_store, mock_context_dir, mock_claude_dir, mock_load, runner
    ):
        """Test that slashes in agent name are replaced with __ in the output filename.

        agent_name='org/sub-agent' → LOCAL_AGENT_STORE_DIR/'org'/'sub-agent.md'
        and AGENT_CONTEXT_DIR/'org'/'sub-agent.md', so those subdirs must exist.
        The Claude Code output file must be flattened: 'org__sub-agent.md'.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            # LOCAL_AGENT_STORE_DIR / "org/sub-agent.md" resolves to local/org/sub-agent.md
            local_path = tmppath / "local"
            (local_path / "org").mkdir(parents=True, exist_ok=True)
            # Source file needs proper YAML frontmatter so frontmatter.loads() works correctly
            (local_path / "org" / "sub-agent.md").write_text(
                "---\nname: org/sub-agent\ndescription: Test\n---\n"
            )

            claude_dir = tmppath / "claude_agents"
            claude_dir.mkdir(parents=True, exist_ok=True)

            # AGENT_CONTEXT_DIR / "org/sub-agent.md" → context/org/sub-agent.md
            # parent dir must exist for dest_file.write_text() to succeed
            (tmppath / "context" / "org").mkdir(parents=True, exist_ok=True)

            mock_local_store.__truediv__ = lambda self, x: local_path / x
            mock_context_dir.__truediv__ = lambda self, x: tmppath / "context" / x
            mock_context_dir.mkdir = MagicMock()
            mock_claude_dir.__truediv__ = lambda self, x: claude_dir / x
            mock_claude_dir.mkdir = MagicMock()

            slash_profile = MagicMock()
            slash_profile.name = "org/sub-agent"
            slash_profile.description = "Test"
            slash_profile.system_prompt = None
            slash_profile.tools = None
            slash_profile.allowedTools = None
            slash_profile.mcpServers = None
            slash_profile.model = None
            slash_profile.hooks = None
            mock_load.return_value = slash_profile

            result = runner.invoke(install, ["org/sub-agent", "--provider", "claude_code"])

            assert "installed successfully" in result.output, f"Unexpected output: {result.output}"
            # Filename should use __ instead of / — must not create a subdirectory
            sanitized_file = claude_dir / "org__sub-agent.md"
            assert sanitized_file.exists(), (
                f"Expected sanitized filename 'org__sub-agent.md'; "
                f"claude_dir contents: {list(claude_dir.iterdir())}"
            )

    @patch("cli_agent_orchestrator.cli.commands.install.load_agent_profile")
    @patch("cli_agent_orchestrator.cli.commands.install.CLAUDE_AGENTS_DIR")
    @patch("cli_agent_orchestrator.cli.commands.install.AGENT_CONTEXT_DIR")
    @patch("cli_agent_orchestrator.cli.commands.install.LOCAL_AGENT_STORE_DIR")
    def test_install_claude_code_extra_frontmatter_passthrough(
        self,
        mock_local_store,
        mock_context_dir,
        mock_claude_dir,
        mock_load,
        runner,
        mock_agent_profile,
    ):
        """Test that extra frontmatter fields in the source file are preserved in output.

        The new implementation copies the entire source frontmatter wholesale rather
        than cherry-picking known fields, so any extra keys should appear verbatim.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            local_path = tmppath / "local"
            local_path.mkdir(parents=True, exist_ok=True)
            # Include an extra field (customTag) that AgentProfile doesn't model
            (local_path / "test-agent.md").write_text(
                "---\n"
                "name: test-agent\n"
                "description: Test agent description\n"
                "model: claude-opus-4-5\n"
                "customTag: some-value\n"
                "---\n\n"
                "System prompt body."
            )

            claude_dir = tmppath / "claude_agents"
            claude_dir.mkdir(parents=True, exist_ok=True)

            mock_local_store.__truediv__ = lambda self, x: local_path / x
            mock_context_dir.__truediv__ = lambda self, x: tmppath / "context" / x
            mock_context_dir.mkdir = MagicMock()
            mock_claude_dir.__truediv__ = lambda self, x: claude_dir / x
            mock_claude_dir.mkdir = MagicMock()

            mock_load.return_value = mock_agent_profile
            (tmppath / "context").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(install, ["test-agent", "--provider", "claude_code"])

            assert "installed successfully" in result.output
            agent_file = claude_dir / "test-agent.md"
            assert agent_file.exists()
            content = agent_file.read_text()

            # All source frontmatter fields should be present in output
            assert "name: test-agent" in content
            assert "model: claude-opus-4-5" in content
            assert "customTag: some-value" in content
            # permissionMode is forced in
            assert "permissionMode: bypassPermissions" in content
            # Body is preserved
            assert "System prompt body." in content
