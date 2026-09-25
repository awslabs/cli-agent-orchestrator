"""Unit tests for Devin CLI provider."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.devin_cli import DevinCliProvider

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(filename: str) -> str:
    with open(FIXTURES_DIR / filename, "r", encoding="utf-8") as f:
        return f.read()


class TestDevinCliProviderInitialization:
    """Test Devin CLI provider initialization."""

    @patch("cli_agent_orchestrator.providers.devin_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.devin_cli.wait_until_status")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @pytest.mark.asyncio
    async def test_initialize_success(self, mock_backend, mock_wait_status, mock_wait_shell):
        """Test successful initialization."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_backend.return_value.send_keys.return_value = None

        provider = DevinCliProvider("test1234", "test-session", "window-0")
        result = await provider.initialize()

        assert result is True
        mock_wait_shell.assert_called_once()
        mock_backend.return_value.send_keys.assert_called_once()
        mock_wait_status.assert_called_once()

    def test_paste_enter_count_is_1(self):
        """Devin TUI accepts input with a single Enter after paste."""
        provider = DevinCliProvider("test1234", "test-session", "window-0")
        assert provider.paste_enter_count == 1

    def test_exit_cli_returns_slash_exit(self):
        """Verify exit_cli() returns the correct exit command for Devin CLI."""
        provider = DevinCliProvider("test1234", "test-session", "window-0")
        assert provider.exit_cli() == "/exit"


class TestDevinCliProviderStatusDetection:
    """Test status detection from terminal output."""

    def test_get_status_idle(self):
        """IDLE: status bar + input prompt visible, no user-input line."""
        buffer = load_fixture("devin_cli_idle_output.txt")

        provider = DevinCliProvider("test1234", "test-session", "window-0")
        status = provider.get_status(buffer)

        assert status == TerminalStatus.IDLE

    def test_get_status_processing(self):
        """PROCESSING: spinner text visible ('Running tools')."""
        buffer = load_fixture("devin_cli_processing_output.txt")

        provider = DevinCliProvider("test1234", "test-session", "window-0")
        status = provider.get_status(buffer)

        assert status == TerminalStatus.PROCESSING

    def test_get_status_completed(self):
        """COMPLETED: user input + response + idle prompt visible."""
        buffer = load_fixture("devin_cli_completed_output.txt")

        provider = DevinCliProvider("test1234", "test-session", "window-0")
        status = provider.get_status(buffer)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_empty_output(self):
        """UNKNOWN: empty/blank output → keep polling, don't latch a false error."""
        buffer = ""

        provider = DevinCliProvider("test1234", "test-session", "window-0")
        status = provider.get_status(buffer)

        assert status == TerminalStatus.UNKNOWN

    def test_get_status_user_input_no_response(self):
        """COMPLETED: user input sent, prompt returned (ready for next input)."""
        buffer = (
            "> what is 2+2\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "#\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "Mode: chat  Model: devin-v1\n"
        )

        provider = DevinCliProvider("test1234", "test-session", "window-0")
        status = provider.get_status(buffer)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_esc_to_interrupt(self):
        """PROCESSING: 'esc to interrupt' spinner is present."""
        buffer = "> write some code\nesc to interrupt\n#\nMode: chat  Model: devin-v1\n"

        provider = DevinCliProvider("test1234", "test-session", "window-0")
        status = provider.get_status(buffer)

        assert status == TerminalStatus.PROCESSING

    def test_get_status_completed_with_markdown_heading_response(self):
        """COMPLETED even when the response begins with a Markdown heading (Bug #1 regression)."""
        buffer = load_fixture("devin_cli_heading_response.txt")

        provider = DevinCliProvider("test1234", "test-session", "window-0")
        status = provider.get_status(buffer)

        assert status == TerminalStatus.COMPLETED


class TestDevinCliResponseExtraction:
    """Test response extraction from script output."""

    def test_extract_simple_response(self):
        """Basic extraction between user input and horizontal rule."""
        provider = DevinCliProvider("test1234", "test-session", "window-0")
        output = load_fixture("devin_cli_completed_output.txt")
        message = provider.extract_last_message_from_script(output)

        assert message is not None
        assert "README.md" in message
        assert "src/" in message

    def test_extract_complex_response(self):
        """Extraction of a multi-line response."""
        provider = DevinCliProvider("test1234", "test-session", "window-0")
        output = load_fixture("devin_cli_complex_response.txt")
        message = provider.extract_last_message_from_script(output)

        assert message is not None
        assert "orchestrator" in message.lower()
        assert "providers" in message.lower()

    def test_extract_no_user_input_raises(self):
        """Raises ValueError when no user-input line is present."""
        provider = DevinCliProvider("test1234", "test-session", "window-0")
        output = load_fixture("devin_cli_idle_output.txt")

        with pytest.raises(ValueError, match="No user input found"):
            provider.extract_last_message_from_script(output)

    def test_extract_uses_last_user_input(self):
        """Extraction is anchored to the LAST user-input line."""
        provider = DevinCliProvider("test1234", "test-session", "window-0")
        output = (
            "> first question\n"
            "First answer.\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "#\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "Mode: chat  Model: devin-v1\n"
            "> second question\n"
            "Second answer.\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "#\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "Mode: chat  Model: devin-v1\n"
        )
        message = provider.extract_last_message_from_script(output)
        assert message == "Second answer."

    def test_extract_strips_whitespace(self):
        """Leading/trailing blank lines are stripped from the response."""
        provider = DevinCliProvider("test1234", "test-session", "window-0")
        output = (
            "> hello\n"
            "\n"
            "   \n"
            "Hello there!\n"
            "\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "#\n"
            "Mode: chat  Model: devin-v1\n"
        )
        message = provider.extract_last_message_from_script(output)
        assert message == "Hello there!"

    def test_extract_empty_response_raises(self):
        """Raises ValueError when response section is empty."""
        provider = DevinCliProvider("test1234", "test-session", "window-0")
        output = (
            "> hello\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "#\n"
            "Mode: chat  Model: devin-v1\n"
        )
        with pytest.raises(ValueError, match="No response found"):
            provider.extract_last_message_from_script(output)

    def test_extract_response_with_markdown_heading(self):
        """Response starting with a Markdown heading is extracted in full (Bug #1 regression)."""
        provider = DevinCliProvider("test1234", "test-session", "window-0")
        output = load_fixture("devin_cli_heading_response.txt")
        message = provider.extract_last_message_from_script(output)

        # The full response including the "# Overview" heading must be returned.
        assert message is not None
        assert "# Overview" in message
        assert "Supported providers" in message

    def test_extract_response_with_markdown_heading_inline(self):
        """Markdown headings inside the response are not treated as terminators."""
        provider = DevinCliProvider("test1234", "test-session", "window-0")
        output = (
            "> summarise\n"
            "# Summary\n"
            "Here is the summary.\n"
            "## Details\n"
            "Some details here.\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "#\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "Mode: chat  Model: devin-v1\n"
        )
        message = provider.extract_last_message_from_script(output)
        assert message is not None
        assert "# Summary" in message
        assert "## Details" in message
        assert "Some details here." in message


class TestDevinCliToolRestrictions:
    """Test that allowed_tools restrictions are enforced via the prompt file."""

    def test_allowed_tools_constraint_prepended_to_prompt(self):
        """Security constraint is prepended when allowed_tools is restricted."""
        provider = DevinCliProvider(
            "test1234", "test-session", "window-0", allowed_tools=["fs_read", "execute_bash"]
        )
        command = provider._build_command()

        # A --prompt-file flag must be present.
        assert "--prompt-file" in command

        # Verify the temp file contains the security constraint and tool list.
        assert provider._temp_prompt_file is not None
        with open(provider._temp_prompt_file, encoding="utf-8") as f:
            content = f.read()
        assert "fs_read" in content
        assert "execute_bash" in content
        assert "SECURITY CONSTRAINTS" in content

        # Cleanup
        provider.cleanup()

    def test_no_prompt_file_when_unrestricted(self):
        """No prompt file is written when allowed_tools is unrestricted ('*')."""
        provider = DevinCliProvider("test1234", "test-session", "window-0", allowed_tools=["*"])
        provider._build_command()

        assert provider._temp_prompt_file is None
        provider.cleanup()

    def test_no_prompt_file_when_no_profile_and_no_restrictions(self):
        """No prompt file written when there is no profile and no restrictions."""
        provider = DevinCliProvider("test1234", "test-session", "window-0")
        command = provider._build_command()

        assert "--prompt-file" not in command
        assert provider._temp_prompt_file is None
        provider.cleanup()

    def test_tool_restriction_with_agent_profile(self):
        """Security constraint is prepended before the profile system prompt."""
        mock_profile = MagicMock()
        mock_profile.system_prompt = "You are a helpful assistant."
        mock_profile.mcpServers = None
        mock_profile.container = None

        with patch(
            "cli_agent_orchestrator.providers.devin_cli.load_agent_profile",
            return_value=mock_profile,
        ):
            provider = DevinCliProvider(
                "test1234",
                "test-session",
                "window-0",
                agent_profile="my-agent",
                allowed_tools=["fs_read"],
            )
            provider._build_command()

        assert provider._temp_prompt_file is not None
        with open(provider._temp_prompt_file, encoding="utf-8") as f:
            content = f.read()
        # Security constraint must come BEFORE the profile system prompt.
        security_pos = content.find("SECURITY CONSTRAINTS")
        profile_pos = content.find("You are a helpful assistant.")
        assert security_pos < profile_pos
        provider.cleanup()


class TestDevinCliProviderRegistration:
    """Test that Devin CLI is properly registered in the system."""

    def test_provider_type_exists(self):
        """ProviderType enum has DEVIN_CLI entry."""
        from cli_agent_orchestrator.models.provider import ProviderType

        assert hasattr(ProviderType, "DEVIN_CLI")
        assert ProviderType.DEVIN_CLI.value == "devin_cli"

    def test_provider_in_providers_list(self):
        """devin_cli appears in the PROVIDERS constant."""
        from cli_agent_orchestrator.constants import PROVIDERS

        assert "devin_cli" in PROVIDERS

    def test_manager_creates_devin_cli_provider(self):
        """ProviderManager can create a DevinCliProvider."""
        from cli_agent_orchestrator.models.provider import ProviderType
        from cli_agent_orchestrator.providers.manager import ProviderManager

        manager = ProviderManager()
        provider = manager.create_provider(
            ProviderType.DEVIN_CLI.value,
            terminal_id="t1",
            tmux_session="s1",
            tmux_window="w1",
            agent_profile=None,
        )

        assert isinstance(provider, DevinCliProvider)
        assert manager.get_provider("t1") is provider

    def test_devin_cli_in_workspace_access_set(self):
        """devin_cli is in PROVIDERS_REQUIRING_WORKSPACE_ACCESS."""
        from cli_agent_orchestrator.cli.commands.launch import PROVIDERS_REQUIRING_WORKSPACE_ACCESS

        assert "devin_cli" in PROVIDERS_REQUIRING_WORKSPACE_ACCESS

    def test_tool_mapping_has_devin_cli(self):
        """tool_mapping.py defines a mapping for devin_cli."""
        from cli_agent_orchestrator.utils.tool_mapping import TOOL_MAPPING

        assert "devin_cli" in TOOL_MAPPING
        mapping = TOOL_MAPPING["devin_cli"]
        assert "execute_bash" in mapping
        assert "fs_read" in mapping
        assert "fs_write" in mapping
        assert "fs_list" in mapping


class TestDevinCliMcpDelivery:
    """MCP servers are delivered via ``.devin/mcp_config.local.json``.

    Devin CLI >= v3000.3 discovers servers only from the dedicated mcp_config
    files; ``--config`` overrides the main settings file and cannot carry them.
    """

    def _provider_in(self, tmp_path, monkeypatch):
        workdir = tmp_path / "repo"
        workdir.mkdir()
        monkeypatch.setattr(DevinCliProvider, "_terminal_workdir", lambda self: workdir)
        monkeypatch.setattr(DevinCliProvider, "_has_live_siblings", lambda self, wd: False)
        return DevinCliProvider("test1234", "test-session", "window-0"), workdir

    def test_stdio_server_written_to_project_local_config(self, tmp_path, monkeypatch):
        provider, workdir = self._provider_in(tmp_path, monkeypatch)

        provider._deliver_mcp_servers(
            {
                "tools": {
                    "type": "stdio",
                    "command": "demo-server",
                    "args": ["--serve"],
                    "env": {"API_KEY": "k"},
                }
            }
        )

        config_path = workdir / ".devin" / "mcp_config.local.json"
        entry = json.loads(config_path.read_text())["mcpServers"]["tools"]
        assert entry["command"] == "demo-server"
        assert entry["args"] == ["--serve"]
        assert entry["env"]["API_KEY"] == "k"
        assert "type" not in entry
        # Per-terminal identity via env expansion, not a literal value.
        assert entry["env"]["CAO_TERMINAL_ID"] == "${env:CAO_TERMINAL_ID}"
        provider.cleanup()

    def test_streamable_http_translates_to_devin_http(self, tmp_path, monkeypatch):
        provider, workdir = self._provider_in(tmp_path, monkeypatch)

        provider._deliver_mcp_servers(
            {"remote": {"type": "streamable-http", "url": "https://mcp.example/x"}}
        )

        config_path = workdir / ".devin" / "mcp_config.local.json"
        entry = json.loads(config_path.read_text())["mcpServers"]["remote"]
        assert entry["url"] == "https://mcp.example/x"
        assert entry["transport"] == "http"
        assert "type" not in entry
        provider.cleanup()

    def test_merges_with_existing_local_config(self, tmp_path, monkeypatch):
        provider, workdir = self._provider_in(tmp_path, monkeypatch)
        config_path = workdir / ".devin" / "mcp_config.local.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({"mcpServers": {"user-server": {"command": "mine"}}}))

        provider._deliver_mcp_servers({"tools": {"type": "stdio", "command": "demo"}})

        servers = json.loads(config_path.read_text())["mcpServers"]
        assert "user-server" in servers
        assert "tools" in servers

        # Cleanup removes only the entries this provider delivered.
        provider.cleanup()
        servers = json.loads(config_path.read_text())["mcpServers"]
        assert servers == {"user-server": {"command": "mine"}}

    def test_cleanup_removes_config_file_when_only_owned_entries(self, tmp_path, monkeypatch):
        provider, workdir = self._provider_in(tmp_path, monkeypatch)
        config_path = workdir / ".devin" / "mcp_config.local.json"

        provider._deliver_mcp_servers({"tools": {"type": "stdio", "command": "demo"}})
        assert config_path.exists()

        provider.cleanup()
        assert not config_path.exists()
        assert not config_path.parent.exists()

    def test_cleanup_restores_a_preexisting_same_named_entry(self, tmp_path, monkeypatch):
        """An operator server we overwrote at launch must come back."""
        provider, workdir = self._provider_in(tmp_path, monkeypatch)
        config_path = workdir / ".devin" / "mcp_config.local.json"
        config_path.parent.mkdir(parents=True)
        operator_entry = {"command": "operator-version", "env": {"TOKEN": "t"}}
        config_path.write_text(json.dumps({"mcpServers": {"tools": operator_entry}}))

        provider._deliver_mcp_servers({"tools": {"type": "stdio", "command": "demo"}})
        provider.cleanup()

        servers = json.loads(config_path.read_text())["mcpServers"]
        assert servers == {"tools": operator_entry}

    def test_cleanup_leaves_an_entry_rewritten_since_delivery(self, tmp_path, monkeypatch):
        """A name someone else rewrote after our delivery is not ours to remove."""
        provider, workdir = self._provider_in(tmp_path, monkeypatch)
        config_path = workdir / ".devin" / "mcp_config.local.json"

        provider._deliver_mcp_servers({"tools": {"type": "stdio", "command": "demo"}})
        doc = json.loads(config_path.read_text())
        doc["mcpServers"]["tools"] = {"command": "rewritten"}
        config_path.write_text(json.dumps(doc))

        provider.cleanup()
        servers = json.loads(config_path.read_text())["mcpServers"]
        assert servers == {"tools": {"command": "rewritten"}}

    def test_cleanup_leaves_the_file_while_a_sibling_terminal_lives(self, tmp_path, monkeypatch):
        """Two same-profile terminals in one dir share identical entries."""
        provider, workdir = self._provider_in(tmp_path, monkeypatch)
        config_path = workdir / ".devin" / "mcp_config.local.json"

        provider._deliver_mcp_servers({"tools": {"type": "stdio", "command": "demo"}})
        monkeypatch.setattr(DevinCliProvider, "_has_live_siblings", lambda self, wd: True)
        provider.cleanup()

        servers = json.loads(config_path.read_text())["mcpServers"]
        assert "tools" in servers

    def test_cleanup_does_not_restore_another_terminals_entry(self, tmp_path, monkeypatch):
        """A prior CAO-written entry collapses away rather than resurrecting."""
        provider, workdir = self._provider_in(tmp_path, monkeypatch)
        config_path = workdir / ".devin" / "mcp_config.local.json"
        config_path.parent.mkdir(parents=True)
        # As left by an earlier CAO terminal that exited without cleanup.
        cao_prior = {
            "command": "demo",
            "env": {"CAO_TERMINAL_ID": "${env:CAO_TERMINAL_ID}"},
        }
        config_path.write_text(json.dumps({"mcpServers": {"tools": cao_prior}}))

        provider._deliver_mcp_servers({"tools": {"type": "stdio", "command": "demo"}})
        provider.cleanup()

        servers = json.loads(config_path.read_text())["mcpServers"]
        assert servers == {}

    def test_build_command_delivers_profile_mcp_servers(self, tmp_path, monkeypatch):
        provider, workdir = self._provider_in(tmp_path, monkeypatch)
        provider._agent_profile = "my-agent"

        mock_profile = MagicMock()
        mock_profile.system_prompt = ""
        mock_profile.mcpServers = {"tools": {"type": "stdio", "command": "demo"}}
        mock_profile.container = None

        with patch(
            "cli_agent_orchestrator.providers.devin_cli.load_agent_profile",
            return_value=mock_profile,
        ):
            provider._build_command()

        config_path = workdir / ".devin" / "mcp_config.local.json"
        assert "tools" in json.loads(config_path.read_text())["mcpServers"]
        provider.cleanup()

    def test_no_mcp_config_without_profile_servers(self, tmp_path, monkeypatch):
        provider, workdir = self._provider_in(tmp_path, monkeypatch)
        provider._build_command()

        assert not (workdir / ".devin" / "mcp_config.local.json").exists()
        provider.cleanup()
