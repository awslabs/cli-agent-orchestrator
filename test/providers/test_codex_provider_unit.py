"""Unit tests for Codex provider."""

import os
import shlex
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.codex import (
    CodexProvider,
    ProviderError,
    _toml_override,
    _toml_scalar,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(filename: str) -> str:
    with open(FIXTURES_DIR / filename, "r") as f:
        return f.read()


class TestCodexProviderInitialization:
    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.wait_until_status")
    @patch("cli_agent_orchestrator.providers.codex.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    async def test_initialize_success(self, mock_tmux, mock_wait_shell, mock_wait_status):
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_tmux.return_value.get_history.return_value = "OpenAI Codex (v0.98.0)"

        provider = CodexProvider("test1234", "test-session", "window-0", None)
        result = await provider.initialize()

        assert result is True
        mock_wait_shell.assert_called_once()
        # Two send_keys calls: warm-up echo + codex with tmux-compatible flags
        assert mock_tmux.return_value.send_keys.call_count == 2
        mock_tmux.return_value.send_keys.assert_any_call("test-session", "window-0", "echo ready")
        mock_tmux.return_value.send_keys.assert_any_call(
            "test-session",
            "window-0",
            "codex --yolo --no-alt-screen --disable shell_snapshot"
            " -c check_for_update_on_startup=false",
        )
        mock_wait_status.assert_called_once()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    async def test_initialize_shell_timeout(self, mock_tmux, mock_wait_shell):
        mock_wait_shell.return_value = False

        provider = CodexProvider("test1234", "test-session", "window-0", None)

        with pytest.raises(TimeoutError, match="Shell initialization timed out"):
            await provider.initialize()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.wait_until_status")
    @patch("cli_agent_orchestrator.providers.codex.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    async def test_initialize_codex_timeout(self, mock_tmux, mock_wait_shell, mock_wait_status):
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = False
        mock_tmux.return_value.get_history.return_value = "OpenAI Codex (v0.98.0)"

        provider = CodexProvider("test1234", "test-session", "window-0", None)

        with pytest.raises(TimeoutError, match="Codex initialization timed out"):
            await provider.initialize()


class TestCodexBuildCommand:
    def test_build_command_no_profile(self):
        provider = CodexProvider("test1234", "test-session", "window-0", None)
        command = provider._build_codex_command()
        assert command == (
            "codex --yolo --no-alt-screen --disable shell_snapshot"
            " -c check_for_update_on_startup=false"
        )

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_with_skill_prompt(self, mock_load_profile):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = "You are a supervisor."
        mock_profile.mcpServers = None
        mock_profile.codexProfile = None
        mock_load_profile.return_value = mock_profile

        provider = CodexProvider(
            "test1234",
            "test-session",
            "window-0",
            "code_supervisor",
            skill_prompt="## Available Skills\n- **python-testing**: Pytest",
        )
        command = provider._build_codex_command()

        mock_load_profile.assert_called_once_with("code_supervisor")
        assert "developer_instructions=" in command
        assert "## Available Skills" in command
        assert "python-testing" in command

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_with_agent_profile(self, mock_load_profile):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = "You are a code supervisor agent."
        mock_profile.mcpServers = None
        mock_profile.codexProfile = None
        mock_load_profile.return_value = mock_profile

        provider = CodexProvider("test1234", "test-session", "window-0", "code_supervisor")
        command = provider._build_codex_command()

        mock_load_profile.assert_called_once_with("code_supervisor")
        assert "codex --yolo --no-alt-screen --disable shell_snapshot" in command
        assert "-c" in command
        assert "developer_instructions=" in command
        assert "You are a code supervisor agent." in command

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_escapes_quotes(self, mock_load_profile):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = 'Use "double quotes" carefully.'
        mock_profile.mcpServers = None
        mock_profile.codexProfile = None
        mock_load_profile.return_value = mock_profile

        provider = CodexProvider("test1234", "test-session", "window-0", "test_agent")
        command = provider._build_codex_command()

        assert '\\"double quotes\\"' in command

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_escapes_newlines(self, mock_load_profile):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = "Line one.\nLine two.\n\n## Section\n- Item"
        mock_profile.mcpServers = None
        mock_profile.codexProfile = None
        mock_load_profile.return_value = mock_profile

        provider = CodexProvider("test1234", "test-session", "window-0", "test_agent")
        command = provider._build_codex_command()

        # Literal newlines must be escaped to \n for TOML and tmux compatibility
        assert "\n" not in command
        assert "\\n" in command
        assert "Line one.\\nLine two.\\n\\n## Section\\n- Item" in command

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_with_mcp_servers(self, mock_load_profile):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = "You are a supervisor."
        mock_profile.mcpServers = {
            "cao-mcp-server": {
                "type": "stdio",
                "command": "uvx",
                "args": ["--from", "git+https://example.com/repo.git@main", "cao-mcp-server"],
            }
        }
        mock_profile.codexProfile = None
        mock_load_profile.return_value = mock_profile

        provider = CodexProvider("test1234", "test-session", "window-0", "code_supervisor")
        command = provider._build_codex_command()

        assert "mcp_servers.cao-mcp-server.command=" in command
        assert "uvx" in command
        assert "mcp_servers.cao-mcp-server.args=" in command
        assert "cao-mcp-server" in command
        # CAO_TERMINAL_ID must be forwarded for handoff to work
        assert "mcp_servers.cao-mcp-server.env_vars=" in command
        assert "CAO_TERMINAL_ID" in command
        # Tool timeout must be a TOML float (600.0) for Codex's f64 deserializer
        assert "mcp_servers.cao-mcp-server.tool_timeout_sec=600.0" in command

    @patch("cli_agent_orchestrator.providers.codex.resolve_mcp_server_config")
    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_bundled_mcp_command_is_resolved(self, mock_load_profile, mock_resolve):
        """The bundled bare cao-mcp-server command is run through the resolver
        before being emitted as a -c override."""
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = "You are a supervisor."
        mock_profile.mcpServers = {
            "cao-mcp-server": {"type": "stdio", "command": "cao-mcp-server", "args": []}
        }
        mock_profile.codexProfile = None
        mock_load_profile.return_value = mock_profile
        # Simulate the resolver returning a PATH-independent absolute path.
        mock_resolve.side_effect = lambda cfg: {
            **cfg,
            "command": "/home/u/.local/bin/cao-mcp-server",
            "args": [],
        }

        provider = CodexProvider("test1234", "test-session", "window-0", "code_supervisor")
        command = provider._build_codex_command()

        # The resolver was invoked and its resolved command appears in the override.
        assert mock_resolve.called
        assert 'mcp_servers.cao-mcp-server.command="/home/u/.local/bin/cao-mcp-server"' in command

    @patch("cli_agent_orchestrator.providers.codex.resolve_mcp_server_config")
    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_mcp_server_command_field_is_toml_escaped(self, mock_load_profile, mock_resolve):
        """A resolved command containing TOML-special chars is escaped so the
        -c override stays a valid TOML basic string."""
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = "You are a supervisor."
        mock_profile.mcpServers = {
            "cao-mcp-server": {"type": "stdio", "command": "cao-mcp-server", "args": []}
        }
        mock_profile.codexProfile = None
        mock_load_profile.return_value = mock_profile
        # Simulate a resolved path containing a backslash and a quote.
        mock_resolve.side_effect = lambda cfg: {
            **cfg,
            "command": r'/tmp/we"ird\path/cao-mcp-server',
            "args": [],
        }

        provider = CodexProvider("test1234", "test-session", "window-0", "code_supervisor")
        command = provider._build_codex_command()

        # The backslash and quote are TOML-escaped in the emitted override.
        assert r"/tmp/we\"ird\\path/cao-mcp-server" in command
        # The raw (unescaped) form must NOT appear -- that would break TOML.
        assert '"/tmp/we"ird' not in command

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_mcp_server_args_and_env_are_toml_escaped(self, mock_load_profile):
        """Args and env values containing TOML-special chars are escaped."""
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = ""
        mock_profile.mcpServers = {
            "test-server": {
                "command": "runner",
                "args": [r'--flag="C:\data"'],
                "env": {"TOKEN": 'se"cret\nvalue'},
            }
        }
        mock_profile.codexProfile = None
        mock_load_profile.return_value = mock_profile

        provider = CodexProvider("test1234", "test-session", "window-0", "test_agent")
        command = provider._build_codex_command()

        # Arg: quote and backslash escaped inside the TOML array element.
        assert r"--flag=\"C:\\data\"" in command
        # Env value: quote escaped, literal newline escaped to \n.
        assert r"se\"cret\nvalue" in command
        assert "\n" not in command
        # The raw (unescaped) forms must NOT appear -- that would break TOML.
        assert r'--flag="C:\data"' not in command
        assert 'se"cret' not in command

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_mcp_env_vars_non_string_entry_fails_fast(self, mock_load_profile):
        """A non-string env_vars entry raises TypeError (intentional fail-fast).

        _toml_scalar rejects non-scalar values so a malformed profile fails at
        launch-command build time with a clear error instead of emitting a
        silently-broken override. Previously the entry was rendered via a raw
        f-string and never raised; the fail-fast is a deliberate change.
        """
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = ""
        mock_profile.mcpServers = {
            "test-server": {
                "command": "runner",
                "args": [],
                "env_vars": [{"not": "a-string"}],
            }
        }
        mock_profile.codexProfile = None
        mock_load_profile.return_value = mock_profile

        provider = CodexProvider("test1234", "test-session", "window-0", "test_agent")
        with pytest.raises(TypeError, match="scalars"):
            provider._build_codex_command()

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_with_mcp_servers_env(self, mock_load_profile):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = ""
        mock_profile.mcpServers = {
            "test-server": {
                "command": "npx",
                "args": ["-y", "test-server"],
                "env": {"API_KEY": "secret123"},
            }
        }
        mock_profile.codexProfile = None
        mock_load_profile.return_value = mock_profile

        provider = CodexProvider("test1234", "test-session", "window-0", "test_agent")
        command = provider._build_codex_command()

        assert "mcp_servers.test-server.command=" in command
        assert "mcp_servers.test-server.env.API_KEY=" in command
        assert "secret123" in command
        # CAO_TERMINAL_ID always forwarded even without explicit env_vars
        assert "mcp_servers.test-server.env_vars=" in command
        assert "CAO_TERMINAL_ID" in command

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_mcp_preserves_existing_env_vars(self, mock_load_profile):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = ""
        mock_profile.mcpServers = {
            "my-server": {
                "command": "node",
                "args": ["server.js"],
                "env_vars": ["HOME", "PATH"],
            }
        }
        mock_profile.codexProfile = None
        mock_load_profile.return_value = mock_profile

        provider = CodexProvider("test1234", "test-session", "window-0", "test_agent")
        command = provider._build_codex_command()

        # Existing env_vars preserved and CAO_TERMINAL_ID appended
        assert "HOME" in command
        assert "PATH" in command
        assert "CAO_TERMINAL_ID" in command

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_empty_system_prompt(self, mock_load_profile):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = ""
        mock_profile.mcpServers = None
        mock_profile.codexProfile = None
        mock_load_profile.return_value = mock_profile

        provider = CodexProvider("test1234", "test-session", "window-0", "empty_agent")
        command = provider._build_codex_command()

        assert command == (
            "codex --yolo --no-alt-screen --disable shell_snapshot"
            " -c check_for_update_on_startup=false"
        )
        assert "developer_instructions" not in command

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_none_system_prompt(self, mock_load_profile):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_profile.codexProfile = None
        mock_load_profile.return_value = mock_profile

        provider = CodexProvider("test1234", "test-session", "window-0", "none_agent")
        command = provider._build_codex_command()

        assert command == (
            "codex --yolo --no-alt-screen --disable shell_snapshot"
            " -c check_for_update_on_startup=false"
        )

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_profile_load_failure(self, mock_load_profile):
        mock_load_profile.side_effect = RuntimeError("Profile not found")

        provider = CodexProvider("test1234", "test-session", "window-0", "bad_agent")

        with pytest.raises(ProviderError, match="Failed to load agent profile"):
            provider._build_codex_command()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.wait_until_status")
    @patch("cli_agent_orchestrator.providers.codex.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    async def test_initialize_with_agent_profile(
        self, mock_tmux, mock_load_profile, mock_wait_shell, mock_wait_status
    ):
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_tmux.return_value.get_history.return_value = "OpenAI Codex (v0.98.0)"
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = "You are a supervisor."
        mock_profile.mcpServers = None
        mock_profile.codexProfile = None
        mock_load_profile.return_value = mock_profile

        provider = CodexProvider("test1234", "test-session", "window-0", "code_supervisor")
        result = await provider.initialize()

        assert result is True
        # The second send_keys call should contain developer_instructions
        codex_call = mock_tmux.return_value.send_keys.call_args_list[1]
        assert "developer_instructions=" in codex_call.args[2]
        assert "You are a supervisor." in codex_call.args[2]


class TestCodexProviderModelFlag:
    """Tests that profile.model is forwarded to Codex via --model."""

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_appends_model_when_set(self, mock_load):
        mock_profile = MagicMock()
        mock_profile.model = "gpt-5"
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_profile.codexProfile = None
        mock_load.return_value = mock_profile

        provider = CodexProvider("tid", "sess", "win", "agent")
        command = provider._build_codex_command()

        assert "--model gpt-5" in command

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_omits_model_when_unset(self, mock_load):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_profile.codexProfile = None
        mock_load.return_value = mock_profile

        provider = CodexProvider("tid", "sess", "win", "agent")
        command = provider._build_codex_command()

        assert "--model" not in command


class TestCodexBuildCommandExtra:
    """Coverage for branches inside ``_build_codex_command`` that the
    pre-existing fixtures didn't exercise."""

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_security_prompt_prepended_when_tools_restricted(self, mock_load):
        # When ``allowed_tools`` is a restricted set (no "*"), the provider
        # prepends SECURITY_PROMPT plus a "You only have access to these
        # tools:" hint to the developer_instructions payload.
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = "Original system prompt."
        mock_profile.mcpServers = None
        mock_profile.codexProfile = None
        mock_load.return_value = mock_profile

        provider = CodexProvider(
            "tid", "sess", "win", "agent", allowed_tools=["fs_read", "fs_list"]
        )
        command = provider._build_codex_command()

        assert "You only have access to these tools: fs_read, fs_list" in command
        assert "Original system prompt." in command
        # SECURITY_PROMPT lives in constants; assert on a stable substring
        # rather than importing the constant into the test fixture.
        assert "NEVER" in command  # "NEVER read/output: ~/.aws/credentials..."

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_mcp_server_accepts_model_instance(self, mock_load):
        # mcpServers values may arrive as McpServer model instances (not
        # dicts) when loaded via Pydantic; the provider falls back to
        # ``model_dump(exclude_none=True)`` for that path.
        from cli_agent_orchestrator.models.agent_profile import McpServer

        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = ""
        mock_profile.mcpServers = {
            "model-server": McpServer(command="node", args=["server.js"]),
        }
        mock_profile.codexProfile = None
        mock_load.return_value = mock_profile

        provider = CodexProvider("tid", "sess", "win", "agent")
        command = provider._build_codex_command()

        assert "mcp_servers.model-server.command=" in command
        assert "node" in command
        assert "mcp_servers.model-server.args=" in command
        assert "server.js" in command


class TestCodexProviderCodexProfile:
    """Tests that profile.codexProfile swaps --yolo for codex's --profile <name>."""

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_codex_profile_replaces_yolo(self, mock_load):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_profile.codexProfile = "cao_reviewer"
        mock_load.return_value = mock_profile

        provider = CodexProvider("tid", "sess", "win", "agent")
        command = provider._build_codex_command()

        assert "--profile cao_reviewer" in command
        assert "--yolo" not in command
        # Tmux-compat flags still required regardless of permission tier
        assert "--no-alt-screen" in command
        assert "--disable shell_snapshot" in command

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_codex_profile_composes_with_mcp_overrides(self, mock_load):
        # Regression guard: --profile <name> must still be followed by the
        # -c mcp_servers... overrides CAO injects, so handoff/assign keep
        # working when an agent profile opts into a named codex profile.
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {
            "cao-mcp-server": {
                "command": "uvx",
                "args": ["--from", "git+https://example.com/repo.git@main", "cao-mcp-server"],
            }
        }
        mock_profile.codexProfile = "cao_reviewer"
        mock_load.return_value = mock_profile

        provider = CodexProvider("tid", "sess", "win", "agent")
        command = provider._build_codex_command()

        assert "--profile cao_reviewer" in command
        assert "--yolo" not in command
        # Existing MCP wiring still applies
        assert "mcp_servers.cao-mcp-server.command=" in command
        assert "mcp_servers.cao-mcp-server.tool_timeout_sec=600.0" in command
        assert "CAO_TERMINAL_ID" in command

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_yolo_overrides_codex_profile(self, mock_load):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_profile.codexProfile = "cao_reviewer"
        mock_load.return_value = mock_profile

        provider = CodexProvider("tid", "sess", "win", "agent", allowed_tools=["*"])
        command = provider._build_codex_command()

        assert "--yolo" in command
        assert "--profile" not in command


class TestTomlScalar:
    """Tests for ``_toml_scalar`` TOML-literal serialization."""

    def test_string_is_quoted(self):
        assert _toml_scalar("xhigh") == '"xhigh"'

    def test_bool_true_is_bare(self):
        assert _toml_scalar(True) == "true"

    def test_bool_false_is_bare(self):
        assert _toml_scalar(False) == "false"

    def test_bool_checked_before_int(self):
        # bool is a subclass of int; True must render as "true", not "1".
        assert _toml_scalar(True) == "true"
        assert _toml_scalar(1) == "1"

    def test_int_is_bare(self):
        assert _toml_scalar(600) == "600"

    def test_float_is_bare(self):
        assert _toml_scalar(600.0) == "600.0"

    def test_string_escapes_quotes_and_backslashes(self):
        assert _toml_scalar('a"b\\c') == '"a\\"b\\\\c"'

    def test_string_escapes_newlines(self):
        # Literal newlines would split the tmux command across lines.
        assert "\n" not in _toml_scalar("line1\nline2")
        assert _toml_scalar("line1\nline2") == '"line1\\nline2"'

    def test_string_escapes_tabs_and_carriage_returns(self):
        assert _toml_scalar("a\tb\rc") == '"a\\tb\\rc"'

    @pytest.mark.parametrize("value", [{"a": 1}, ["x"], None])
    def test_rejects_non_scalar(self, value):
        with pytest.raises(TypeError):
            _toml_scalar(value)


class TestTomlOverride:
    """Tests for ``_toml_override`` key validation."""

    def test_builds_override_for_valid_dotted_key(self):
        assert _toml_override("features.fast_mode", True) == "features.fast_mode=true"
        assert _toml_override("model_reasoning_effort", "xhigh") == 'model_reasoning_effort="xhigh"'

    @pytest.mark.parametrize("key", ["bad key", "a=b", 'k"x', "key\ninjected", "key\n", "", "a/b"])
    def test_rejects_unsafe_key(self, key):
        # Unsafe keys would produce a malformed -c override or split the tmux
        # command across lines; fail fast instead.
        with pytest.raises(ValueError, match="Invalid codexConfig key"):
            _toml_override(key, "v")

    def test_non_scalar_value_error_names_offending_key(self):
        with pytest.raises(TypeError, match="codexConfig key 'features.x'"):
            _toml_override("features.x", {"nested": 1})


class TestMcpKeyValidation:
    """MCP server names and env keys are validated before interpolation into
    the ``-c mcp_servers.<name>.<field>`` override path — a quote or newline
    in the KEY half would corrupt the TOML the same way an unescaped value
    would."""

    @staticmethod
    def _profile_with(servers):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = servers
        mock_profile.codexProfile = None
        mock_profile.codexConfig = None
        return mock_profile

    @pytest.mark.parametrize(
        "name", ['srv"x', "srv\ninjected", "srv\n", "bad name", "a=b", "", "srv.dotted"]
    )
    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_rejects_unsafe_server_name(self, mock_load, name):
        mock_load.return_value = self._profile_with({name: {"command": "cmd", "args": []}})
        provider = CodexProvider("tid", "sess", "win", "agent")
        with pytest.raises(ValueError, match="Invalid mcpServers name key"):
            provider._build_codex_command()

    @pytest.mark.parametrize("env_key", ['K"X', "K\nY", "K\n", "BAD KEY", "a=b", "", "K.DOTTED"])
    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_rejects_unsafe_env_key(self, mock_load, env_key):
        mock_load.return_value = self._profile_with(
            {"srv": {"command": "cmd", "args": [], "env": {env_key: "value"}}}
        )
        provider = CodexProvider("tid", "sess", "win", "agent")
        with pytest.raises(ValueError, match="Invalid mcpServers env key"):
            provider._build_codex_command()

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_accepts_normal_names_and_env_keys(self, mock_load):
        mock_load.return_value = self._profile_with(
            {"cao-mcp-server": {"command": "cmd", "args": [], "env": {"API_KEY": "v"}}}
        )
        provider = CodexProvider("tid", "sess", "win", "agent")
        command = provider._build_codex_command()
        assert "mcp_servers.cao-mcp-server.command=" in command
        assert "mcp_servers.cao-mcp-server.env.API_KEY=" in command


class TestCodexProviderCodexConfig:
    """Tests that profile.codexConfig emits inline ``-c key=value`` overrides."""

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_codex_config_emits_c_overrides_in_yolo_path(self, mock_load):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_profile.codexProfile = None
        mock_profile.codexConfig = {
            "model_reasoning_effort": "xhigh",
            "service_tier": "fast",
            "features.fast_mode": True,
        }
        mock_load.return_value = mock_profile

        provider = CodexProvider("tid", "sess", "win", "agent")
        command = provider._build_codex_command()

        # Default --yolo path is kept; overrides are appended as -c key=value.
        # String values are shlex-quoted (the inner key="value" is preserved);
        # the bool value is emitted bare.
        assert "--yolo" in command
        assert 'model_reasoning_effort="xhigh"' in command
        assert 'service_tier="fast"' in command
        assert "features.fast_mode=true" in command

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_codex_config_composes_with_codex_profile(self, mock_load):
        # codexConfig must apply in the --profile path too, so effort/fast-mode
        # knobs work whether or not a named profile governs sandbox/approvals.
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_profile.codexProfile = "cao_reviewer"
        mock_profile.codexConfig = {"model_reasoning_effort": "high"}
        mock_load.return_value = mock_profile

        provider = CodexProvider("tid", "sess", "win", "agent")
        command = provider._build_codex_command()

        assert "--profile cao_reviewer" in command
        assert "--yolo" not in command
        assert 'model_reasoning_effort="high"' in command

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_codex_config_none_emits_no_overrides(self, mock_load):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_profile.codexProfile = None
        mock_profile.codexConfig = None
        mock_load.return_value = mock_profile

        provider = CodexProvider("tid", "sess", "win", "agent")
        command = provider._build_codex_command()

        assert command == (
            "codex --yolo --no-alt-screen --disable shell_snapshot"
            " -c check_for_update_on_startup=false"
        )

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_codex_config_empty_dict_emits_no_overrides(self, mock_load):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_profile.codexProfile = None
        mock_profile.codexConfig = {}
        mock_load.return_value = mock_profile

        provider = CodexProvider("tid", "sess", "win", "agent")
        command = provider._build_codex_command()

        assert command == (
            "codex --yolo --no-alt-screen --disable shell_snapshot"
            " -c check_for_update_on_startup=false"
        )

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_codex_config_composes_with_mcp_and_model(self, mock_load):
        # Regression guard: codexConfig overrides sit alongside the model flag
        # and the -c mcp_servers... wiring without clobbering either.
        mock_profile = MagicMock()
        mock_profile.model = "gpt-5.5"
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {"cao-mcp-server": {"command": "uvx", "args": ["cao-mcp-server"]}}
        mock_profile.codexProfile = None
        mock_profile.codexConfig = {"model_reasoning_effort": "xhigh"}
        mock_load.return_value = mock_profile

        provider = CodexProvider("tid", "sess", "win", "agent")
        command = provider._build_codex_command()

        assert "--model gpt-5.5" in command
        assert "mcp_servers.cao-mcp-server.command=" in command
        assert 'model_reasoning_effort="xhigh"' in command


class TestCodexProviderStatusDetection:
    def test_get_status_idle(self):
        output = load_fixture("codex_idle_output.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.IDLE

    def test_get_status_completed(self):
        output = load_fixture("codex_completed_output.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_processing(self):
        output = load_fixture("codex_processing_output.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.PROCESSING

    def test_get_status_waiting_user_answer(self):
        output = load_fixture("codex_permission_output.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_get_status_error(self):
        output = load_fixture("codex_error_output.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.ERROR

    def test_get_status_empty_output(self):
        # native=None always falls through (no dispatch-timing guess); on tmux
        # the live-read fallback is a pass-through, so an empty buffer hits
        # Codex's own no-output default directly.
        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status("")

        assert status == TerminalStatus.UNKNOWN

    def test_get_status_processing_when_old_prompt_present(self):
        # If the captured history contains an earlier prompt but the *latest* output is processing,
        # we should report PROCESSING. The old prompt should be far enough from the bottom
        # (more than IDLE_PROMPT_TAIL_LINES) to avoid false idle detection.
        output = (
            "Welcome to Codex\n"
            "❯ \n"
            "You Fix the failing tests\n"
            "assistant: Working on it...\n"
            "Reading file src/main.py...\n"
            "Analyzing code structure...\n"
            "Checking dependencies...\n"
            "Codex is thinking…\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.PROCESSING

    def test_get_status_not_error_on_failed_in_message(self):
        # "failed" is commonly used in normal assistant output; it should not automatically
        # force ERROR.
        output = (
            "You Explain why the test failed\n"
            "assistant: The test failed because the assertion is incorrect.\n"
            "\n"
            "❯ \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_idle_if_no_assistant_after_last_user(self):
        # If there is a user message but no assistant response after it, we should not
        # treat the session as COMPLETED.
        output = "assistant: Welcome\n" "You Do the thing\n" "\n" "❯ \n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.IDLE

    def test_get_status_processing_when_no_prompt_and_no_keywords(self):
        # Codex output may not always include explicit "thinking/processing" keywords.
        # Without an idle prompt at the end, we should assume it's still processing.
        output = "You Run the command\nWorking...\n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.PROCESSING

    def test_get_status_not_error_when_assistant_mentions_error_text(self):
        output = (
            "You Explain the failure\n"
            "assistant: Here's an example error:\n"
            "Error: example only\n"
            "\n"
            "❯ \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_not_waiting_when_assistant_mentions_approval_text(self):
        output = (
            "You Explain approvals\n"
            "assistant: You might see this prompt:\n"
            "Approve this command? [y/n]\n"
            "\n"
            "❯ \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_error_when_error_after_user_and_prompt(self):
        output = "You Run thing\nError: failed\n\n❯ \n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.ERROR

    def test_get_status_waiting_user_answer_when_no_user_prefix(self):
        output = "Approve this command? [y/n]\n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_get_status_error_when_no_user_prefix(self):
        output = "Error: something failed\n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.ERROR

    def test_get_status_idle_tui_with_status_bar(self):
        """Test IDLE detection with realistic TUI output (status bar after prompt)."""
        output = (
            "╭───────────────────────────────────────────╮\n"
            "│ >_ OpenAI Codex (v0.98.0)                 │\n"
            "│ model: gpt-5.3-codex high                 │\n"
            "│ directory: ~/project                      │\n"
            "╰───────────────────────────────────────────╯\n"
            "  Tip: Try the Codex App\n"
            "› Use /skills to list available skills\n"
            "  ? for shortcuts                     100% context left\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.IDLE

    def test_get_status_completed_tui_with_status_bar(self):
        """Test COMPLETED detection with TUI output (status bar after prompt)."""
        output = (
            "You Fix the bug\n"
            "assistant: I've fixed the issue in main.py.\n"
            "\n"
            "› \n"
            "  ? for shortcuts                     100% context left\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED


class TestCodexBulletFormatStatusDetection:
    """Tests for Codex's real interactive output format using › prompt and • bullets."""

    def test_get_status_completed_bullet_format(self):
        """COMPLETED when › user message followed by • response and idle prompt."""
        output = (
            "› what is your role?\n"
            "• I am the Coding Supervisor Agent.\n"
            "• I coordinate tasks between developer and reviewer agents.\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_processing_bullet_format(self):
        """PROCESSING when • response started but no idle prompt at bottom."""
        output = (
            "› fix the failing tests\n"
            "• Let me look at the test files.\n"
            "Reading src/test_main.py...\n"
            "Analyzing code structure...\n"
            "Checking dependencies...\n"
            "Running unit tests...\n"
            "Codex is thinking…\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.PROCESSING

    def test_get_status_idle_bullet_format_no_response(self):
        """IDLE when › user message but no • response yet and idle prompt at bottom."""
        output = "› hello\n\n› \n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.IDLE

    def test_get_status_idle_when_only_tool_call_after_user(self):
        """IDLE when the only "•" bullet after the user prompt is an MCP
        tool-call marker — the model hasn't actually replied yet.

        Regression for the Copilot review on PR #274 that flagged COMPLETED
        being satisfied by a tool-call marker. A "• Called <server>.<tool>(...)"
        bullet must not trip COMPLETED on its own.
        """
        output = (
            "› [CAO Handoff] do task\n"
            '• Called cao-mcp-server.load_skill({"name":"cao-worker-protocols"})\n'
            "  └ skill body text\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.IDLE

    def test_get_status_completed_when_real_reply_after_tool_call(self):
        """COMPLETED when a real "•" reply follows the MCP tool-call marker."""
        output = (
            "› [CAO Handoff] do task\n"
            '• Called cao-mcp-server.load_skill({"name":"cao-worker-protocols"})\n'
            "  └ skill body text\n"
            "• Done — created the function.\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_completed_bullet_with_code_block(self):
        """COMPLETED with • response containing code blocks."""
        output = (
            "› show me a function\n"
            "• Here's the function:\n"
            "\n"
            "  ```python\n"
            "  def hello():\n"
            "      print('hello')\n"
            "  ```\n"
            "\n"
            "• Let me know if you need changes.\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_error_not_masked_by_bullet_pattern(self):
        """ERROR still detected when no • response and error after › user message."""
        output = "› do something\nError: connection refused\n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.ERROR

    def test_get_status_completed_multi_turn_bullet(self):
        """COMPLETED uses last user message in multi-turn bullet format."""
        output = (
            "› first question\n"
            "• First answer.\n"
            "\n"
            "› second question\n"
            "• Second answer with details.\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_completed_bullet_with_tui_status_bar(self):
        """COMPLETED with bullet format and TUI status bar after prompt."""
        output = (
            "› fix the bug\n"
            "• I've fixed the issue in main.py by correcting the import.\n"
            "\n"
            "› \n"
            "  ? for shortcuts                     98% context left\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_processing_tui_spinner(self):
        """PROCESSING when TUI shows • Working spinner, not false COMPLETED."""
        output = (
            "› [CAO Handoff] Supervisor terminal ID: sup-123. Do the task.\n"
            "\n"
            "• Working (0s • esc to interrupt)\n"
            "\n"
            "› Use /skills to list available skills\n"
            "\n"
            "  ? for shortcuts                     100% context left\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.PROCESSING

    def test_get_status_processing_tui_thinking_spinner(self):
        """PROCESSING when TUI shows • Thinking spinner."""
        output = (
            "› Implement feature X\n"
            "\n"
            "• Thinking (3s • esc to interrupt)\n"
            "\n"
            "› Run /review on my current changes\n"
            "\n"
            "  ? for shortcuts                     95% context left\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.PROCESSING

    def test_get_status_processing_dynamic_spinner_text(self):
        """PROCESSING when TUI shows spinner with dynamic prefix text."""
        output = (
            "› [CAO Handoff] Do the task.\n"
            "\n"
            "• Creating /tmp/file.py\n"
            "\n"
            "• Starting script creation (10s • esc to interrupt)\n"
            "\n"
            "› Use /skills to list available skills\n"
            "\n"
            "  ? for shortcuts                     100% context left\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.PROCESSING


class TestCodexV0111FooterFormat:
    """Tests for Codex v0.111.0+ TUI footer format.

    v0.111.0 (PR #13202 'tui: restore draft footer hints') changed the footer:
    - Old: "› Use /skills to list available skills\\n  ? for shortcuts  100% context left"
    - New: "› Find and fix a bug in @filename\\n  gpt-5.3-codex high · 100% left · ~/path"
    The new format uses "N% left" instead of "N% context left" and removes "? for shortcuts".
    """

    def test_get_status_idle_v0111_footer(self):
        """IDLE with v0.111.0 footer format (no '? for shortcuts')."""
        output = (
            "╭───────────────────────────────────────────╮\n"
            "│ >_ OpenAI Codex (v0.111.0)                │\n"
            "│ model: gpt-5.3-codex high                 │\n"
            "│ directory: ~/project                      │\n"
            "╰───────────────────────────────────────────╯\n"
            "  Tip: You can run any shell command from Codex using ! (e.g. !ls)\n"
            "\n"
            "› Find and fix a bug in @filename\n"
            "\n"
            "  gpt-5.3-codex high · 100% left · ~/project\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE

    def test_get_status_completed_v0111_footer(self):
        """COMPLETED with v0.111.0 footer (suggestion hint must not be treated as user input)."""
        output = (
            "› fix the bug\n"
            "• I've fixed the issue in main.py by correcting the import.\n"
            "\n"
            "› Find and fix a bug in @filename\n"
            "\n"
            "  gpt-5.3-codex high · 98% left · ~/project\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_get_status_completed_v0111_multi_turn(self):
        """COMPLETED in multi-turn with v0.111.0 footer."""
        output = (
            "› first question\n"
            "• First answer.\n"
            "\n"
            "› second question\n"
            "• Second answer with details.\n"
            "\n"
            "› Write tests for @main.py\n"
            "\n"
            "  gpt-5.3-codex high · 95% left · ~/project\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_get_status_processing_v0111_spinner(self):
        """PROCESSING when TUI shows spinner with v0.111.0 footer."""
        output = (
            "› [CAO Handoff] Do the task.\n"
            "\n"
            "• Working (0s • esc to interrupt)\n"
            "\n"
            "› Find and fix a bug in @filename\n"
            "\n"
            "  gpt-5.3-codex high · 100% left · ~/project\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING


class TestCodexV0136FooterFormat:
    """Tests for Codex v0.136.0+ TUI footer format.

    v0.136 dropped the "N% left" segment from the status bar; the footer is now
    just "model · path". Without an updated TUI_FOOTER_PATTERN the suggestion
    hint line ("› Run /review on my current changes") is mistaken for a real
    user message, which hides any preceding • assistant response and keeps the
    terminal status pinned at IDLE forever.
    """

    def test_get_status_completed_v0136_footer(self):
        """COMPLETED with v0.136 footer (suggestion hint must not mask the • response)."""
        output = (
            "› Create a Python function called 'greet'.\n"
            "• def greet(name):\n"
            '      return f"Hello, {name}!"\n'
            "\n"
            "› Run /review on my current changes\n"
            "\n"
            "  openai.gpt-5.5 medium · ~/project\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_idle_v0136_footer(self):
        """IDLE with v0.136 footer format (no user message, no response yet)."""
        output = (
            "╭───────────────────────────────────────────╮\n"
            "│ >_ OpenAI Codex (v0.136.0)                │\n"
            "│ model: openai.gpt-5.5 medium              │\n"
            "│ directory: ~/project                      │\n"
            "╰───────────────────────────────────────────╯\n"
            "\n"
            "› Find and fix a bug in @filename\n"
            "\n"
            "  openai.gpt-5.5 medium · ~/project\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.IDLE

    def test_get_status_processing_v0136_spinner(self):
        """PROCESSING when TUI shows spinner with v0.136 footer."""
        output = (
            "› [CAO Handoff] Do the task.\n"
            "\n"
            "• Working (0s • esc to interrupt)\n"
            "\n"
            "› Find and fix a bug in @filename\n"
            "\n"
            "  openai.gpt-5.5 medium · ~/project\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.PROCESSING

    def test_extract_last_message_v0136_footer(self):
        """extract_last_message_from_script ignores v0.136 suggestion-hint footer."""
        script_output = (
            "› Create a Python function called 'greet'.\n"
            "• def greet(name):\n"
            '      return f"Hello, {name}!"\n'
            "\n"
            "› Run /review on my current changes\n"
            "\n"
            "  openai.gpt-5.5 medium · ~/project\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(script_output)

        assert "def greet(name):" in message
        assert "Hello, {name}!" in message
        assert "Run /review" not in message


class TestCodexProviderMessageExtraction:
    def test_extract_last_message_success(self):
        output = load_fixture("codex_completed_output.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "Here's the fix" in message
        assert "All tests now pass." in message

    def test_extract_complex_message(self):
        output = load_fixture("codex_complex_response.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "def add(a, b):" in message
        assert "Let me know" in message

    def test_extract_message_no_marker(self):
        output = "No assistant prefix here"

        provider = CodexProvider("test1234", "test-session", "window-0")

        with pytest.raises(ValueError, match="No Codex response found"):
            provider.extract_last_message_from_script(output)

    def test_extract_strips_cursor_and_erase_escapes(self):
        """PR #390: extraction must strip ALL terminal escapes, not just SGR
        colour codes. codex's TUI emits cursor-move (H) and erase (K) CSI
        sequences heavily; the old SGR-only strip (\\x1b[...m) left them in the
        result as garbage. This fixture interleaves those sequences with the
        response; the extracted text must be clean and contain the answer.
        """
        # Cursor-position (\x1b[<r>;<c>H), erase-line (\x1b[K), and truecolor SGR
        # (\x1b[38;2;...m) all interleaved — the exact shape seen in the failing
        # e2e run. Only the SGR codes end in 'm'; H and K would survive a
        # SGR-only strip.
        output = (
            "\x1b[2K\x1b[38;2;200;200;200m› analyze dataset A\x1b[0m\n"
            "\x1b[32;76H\x1b[K• The mean is 3.0 and the median is 3.0.\x1b[K\n"
            "\x1b[33;2H\x1b[38;2;120;120;120mDataset is symmetric.\x1b[0m\n"
            "\x1b[K❯ \n"
        )
        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "\x1b" not in message, f"escapes leaked into extracted message: {message!r}"
        assert "mean is 3.0" in message
        assert "median is 3.0" in message

    def test_extract_message_empty_response(self):
        output = "assistant:   \n\n❯ "

        provider = CodexProvider("test1234", "test-session", "window-0")

        with pytest.raises(ValueError, match="Empty Codex response"):
            provider.extract_last_message_from_script(output)


class TestCodexBulletFormatExtraction:
    """Tests for message extraction from Codex's real • bullet format."""

    def test_extract_bullet_format_single_line(self):
        """Extract single-line • response."""
        output = "› what is your role?\n• I am the Coding Supervisor Agent.\n\n› \n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "I am the Coding Supervisor Agent." in message

    def test_extract_bullet_format_multi_line(self):
        """Extract multi-line • response with all bullets preserved."""
        output = (
            "› describe your capabilities\n"
            "• I can coordinate development tasks.\n"
            "• I assign work to developer agents.\n"
            "• I review results from workers.\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "coordinate development tasks" in message
        assert "assign work" in message
        assert "review results" in message

    def test_extract_bullet_format_with_code_block(self):
        """Extract • response containing code blocks."""
        output = (
            "› show me the fix\n"
            "• Here's the corrected code:\n"
            "\n"
            "  ```python\n"
            "  def add(a, b):\n"
            "      return a + b\n"
            "  ```\n"
            "\n"
            "• All tests pass now.\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "def add(a, b):" in message
        assert "All tests pass now." in message

    def test_extract_bullet_format_multi_turn(self):
        """Extract only the last response from multi-turn • format."""
        output = (
            "› first question\n"
            "• First answer.\n"
            "\n"
            "› second question\n"
            "• Second answer with more detail.\n"
            "• Additional context here.\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        # Should only contain the second response
        assert "First answer" not in message
        assert "Second answer with more detail." in message
        assert "Additional context here." in message

    def test_extract_bullet_format_without_trailing_prompt(self):
        """Extract • response when no trailing idle prompt (output still streaming)."""
        output = "› fix the bug\n• I've fixed the import issue in main.py.\n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "I've fixed the import issue" in message

    def test_extract_skips_mcp_tool_call_marker(self):
        """`• Called <tool>(...)` markers must not be treated as the response start.

        Codex emits "• Called cao-mcp-server.load_skill({...})" when invoking an
        MCP tool, followed by "└ <tool output>". The next "•" line is the actual
        model reply. Anchoring on the tool-call marker would pull tool output
        (e.g. skill body containing "[CAO Handoff]") into the extracted output.
        """
        output = (
            "› [CAO Handoff] Create a Python function called 'add_numbers'.\n"
            '• Called cao-mcp-server.load_skill({"name":"cao-worker-protocols"})\n'
            "  └ # CAO Worker Protocols\n"
            "\n"
            "    Use this skill when acting as a worker agent.\n"
            "    For example, Codex workers receive a `[CAO Handoff]` prefix.\n"
            "\n"
            "• def add_numbers(a, b):\n"
            "      return a + b\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "def add_numbers(a, b):" in message
        assert "return a + b" in message
        assert "[CAO Handoff]" not in message
        assert "CAO Worker Protocols" not in message
        assert "Called cao-mcp-server" not in message

    def test_extract_skips_tool_call_with_blank_separators(self):
        """Tool-call filtering must work when blank lines separate the tool call
        from later content. The ASSISTANT_PREFIX_PATTERN must anchor on the
        bullet line itself — not on a preceding blank line — otherwise the
        per-line tool-call check sees an empty line and is bypassed.
        """
        output = (
            "› [CAO Handoff] do task\n"
            "\n"
            '• Called cao-mcp-server.load_skill({"name":"cao-worker-protocols"})\n'
            "  └ skill body with [CAO Handoff] reference\n"
            "\n"
            "• def add_numbers(a, b):\n"
            "      return a + b\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "def add_numbers(a, b):" in message
        assert "[CAO Handoff]" not in message
        assert "skill body" not in message

    def test_extract_skips_multiple_tool_calls(self):
        """Multiple consecutive tool calls before the final response."""
        output = (
            "› do the task\n"
            '• Called cao-mcp-server.load_skill({"name":"foo"})\n'
            "  └ skill body text\n"
            "• Called cao-mcp-server.list_terminals({})\n"
            '  └ [{"id":"abc"}]\n'
            "• Done — created the function.\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "Done" in message
        assert "created the function" in message
        assert "skill body text" not in message
        assert "list_terminals" not in message

    def test_extract_does_not_filter_called_as_english_word(self):
        """A model bullet starting "• Called <english word>" must NOT be filtered.

        The MCP tool-call pattern requires a "<server>.<tool>(" shape.
        Bullets like "• Called attention to the bug" are real model replies
        and must survive extraction. Regression for the Copilot review on
        PR #274 that flagged the previous loose pattern.
        """
        output = (
            "› what did you do?\n"
            "• Called attention to the import bug in main.py and fixed it.\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "Called attention to the import bug" in message


class TestCodexV0111Extraction:
    """Extraction tests for Codex v0.111.0+ footer format."""

    def test_extract_bullet_with_v0111_footer(self):
        """Extract response when v0.111.0 footer (suggestion hint) is present."""
        output = (
            "› fix the bug\n"
            "• I've fixed the issue in main.py by correcting the import.\n"
            "\n"
            "› Find and fix a bug in @filename\n"
            "\n"
            "  gpt-5.3-codex high · 98% left · ~/project\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "I've fixed the issue" in message
        # Suggestion hint should not leak into extracted output
        assert "Find and fix a bug" not in message
        assert "gpt-5.3-codex" not in message

    def test_extract_multi_turn_with_v0111_footer(self):
        """Extract last response from multi-turn with v0.111.0 footer."""
        output = (
            "› first question\n"
            "• First answer.\n"
            "\n"
            "› second question\n"
            "• Second answer with details.\n"
            "\n"
            "› Write tests for @main.py\n"
            "\n"
            "  gpt-5.3-codex high · 95% left · ~/project\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "First answer" not in message
        assert "Second answer with details." in message
        assert "Write tests" not in message

    def test_extract_double_blank_between_hint_and_status(self):
        """Suggestion hint must not leak when 2 blank lines separate it from status bar."""
        output = (
            "› fix the bug\n"
            "• I've fixed the issue in main.py by correcting the import.\n"
            "\n"
            "› Find and fix a bug in @filename\n"
            "\n"
            "\n"
            "  gpt-5.3-codex high · 98% left · ~/project\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "I've fixed the issue" in message
        assert "Find and fix a bug" not in message


class TestCodexProviderMisc:
    def test_exit_cli(self):
        provider = CodexProvider("test1234", "test-session", "window-0")
        assert provider.exit_cli() == "/exit"

    def test_cleanup(self):
        provider = CodexProvider("test1234", "test-session", "window-0")
        provider._initialized = True
        provider.cleanup()
        assert provider._initialized is False

    def test_extract_last_message_without_trailing_prompt(self):
        output = "You do thing\nassistant: Hello\nSecond line\n"
        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)
        assert message == "Hello\nSecond line"


class TestCodexProviderTrustPrompt:
    """Tests for Codex workspace trust prompt handling."""

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    async def test_handle_trust_prompt_detected_and_accepted(self, mock_tmux):
        """Test that trust prompt is detected and auto-accepted."""
        mock_tmux.return_value.get_history.return_value = (
            "> You are running Codex in /Users/test/project\n"
            "\n"
            "  Since this folder is version controlled, you may wish to "
            "allow Codex to work in this folder without asking for approval.\n"
            "\n"
            "› 1. Yes, allow Codex to work in this folder without asking for approval\n"
            "  2. No, ask me to approve edits and commands\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        await provider._handle_trust_prompt(timeout=2.0)

        mock_tmux.return_value.send_special_key.assert_called_once_with(
            "test-session", "window-0", "Enter"
        )

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    async def test_handle_trust_prompt_not_needed(self, mock_tmux):
        """Test early return when Codex starts without trust prompt."""
        mock_tmux.return_value.get_history.return_value = "OpenAI Codex (v0.98.0)\n› "

        provider = CodexProvider("test1234", "test-session", "window-0")
        await provider._handle_trust_prompt(timeout=2.0)

        mock_tmux.return_value.send_special_key.assert_not_called()

    def test_get_status_trust_prompt_is_waiting_user_answer(self):
        """Test that trust prompt reports WAITING_USER_ANSWER, not PROCESSING."""
        output = (
            "> You are running Codex in /Users/test/project\n"
            "allow Codex to work in this folder without asking for approval.\n"
            "› 1. Yes\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        # Should be WAITING_USER_ANSWER (not PROCESSING despite "running" in text)
        assert status == TerminalStatus.WAITING_USER_ANSWER

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.wait_until_status")
    @patch("cli_agent_orchestrator.providers.codex.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    async def test_initialize_with_trust_prompt(self, mock_tmux, mock_wait_shell, mock_wait_status):
        """Test that initialize handles trust prompt during startup."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_tmux.return_value.get_history.return_value = (
            "allow Codex to work in this folder without asking for approval.\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        result = await provider.initialize()

        assert result is True
        mock_tmux.return_value.send_special_key.assert_called_with(
            "test-session", "window-0", "Enter"
        )

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.wait_until_status")
    @patch("cli_agent_orchestrator.providers.codex.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    async def test_initialize_with_trust_prompt_v2(
        self, mock_tmux, mock_wait_shell, mock_wait_status
    ):
        """Test that initialize handles v2 trust prompt (git worktree variant)."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_tmux.return_value.get_history.return_value = (
            "Note: You're in a subdirectory of a Git project. Trusting will apply\n"
            "to the repository root: /Users/test/project\n"
            "\n"
            "Do you trust the contents of this directory?\n"
            "\n"
            "› 1. Yes, continue\n"
            "  2. No, quit\n"
            "\n"
            "Press enter to continue\n"
        )
        mock_tmux.return_value.get_pane_current_command.return_value = "zsh"

        provider = CodexProvider("test1234", "test-session", "window-0")
        result = await provider.initialize()

        assert result is True
        mock_tmux.return_value.send_special_key.assert_called_with(
            "test-session", "window-0", "Enter"
        )

    def test_get_status_trust_prompt_v2_is_waiting(self):
        """V2 trust dialog in bottom region classifies WAITING_USER_ANSWER."""
        output = (
            "Note: You're in a subdirectory of a Git project.\n"
            "Trusting will apply to the repository root: /Users/test/project\n"
            "\n"
            "Do you trust the contents of this directory?\n"
            "\n"
            "› 1. Yes, continue\n"
            "  2. No, quit\n"
            "\n"
            "Press enter to continue\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        provider._initialized = True
        provider.shell_baseline = "zsh"
        status = provider.get_status(output)

        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_get_status_trust_v2_in_scrollback_does_not_false_positive(self):
        """V2 trust text in scrollback (not bottom) must NOT trigger WAITING."""
        output = (
            "› explain trust prompts\n"
            '• The dialog says "Do you trust the contents of this directory?"\n'
            "• and shows options like Yes/No.\n"
            "\n"
            "• Here's more explanation about how it works.\n"
            "• The trust system is directory-based.\n"
            "• It remembers your choice for future sessions.\n"
            "• You can reset trust settings in ~/.codex/config.toml.\n"
            "• Each project root has its own trust state.\n"
            "• Subdirectories inherit the parent trust level.\n"
            "• Git worktrees prompt separately from the main repo.\n"
            "• You can pre-trust with --full-auto flag.\n"
            "• The dialog only shows on first visit to a new directory.\n"
            "• Once trusted, Codex won't ask again.\n"
            "• The trust prompt has two variants depending on version.\n"
            "• Newer versions use the git-aware prompt.\n"
            "\n"
            "› \n"
            "  ? for shortcuts                     95% context left\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        provider._initialized = True
        provider.shell_baseline = "zsh"
        status = provider.get_status(output)

        # Should be COMPLETED (model replied to user question), NOT WAITING
        assert status == TerminalStatus.COMPLETED


class TestCodexProviderUpdateDialog:
    """Tests for Codex update-available dialog handling."""

    def test_get_status_update_dialog_waiting(self):
        """Active update dialog classifies as WAITING_USER_ANSWER."""
        output = load_fixture("codex_update_dialog.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_get_status_update_dialog_in_scrollback_is_idle(self):
        """After update dialog is dismissed, status returns to IDLE (not stuck WAITING)."""
        output = load_fixture("codex_update_dialog_scrollback.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.IDLE

    def test_get_status_update_dialog_scrollback_with_padding(self):
        """Update text in scrollback far above the bottom region must not false-positive."""
        output = (
            "Update available! 0.142.5 -> 0.144.5\n"
            "1. Update now (runs npm install -g @openai/codex)\n"
            "2. Skip\n"
            "3. Skip until next version\n"
            "Press enter to continue\n"
            "› summarize the startup dialog\n"
            "• The update prompt had three choices and was skipped.\n"
            "• It is no longer active once the TUI returns to the prompt.\n"
            "• Padding line 1.\n"
            "• Padding line 2.\n"
            "• Padding line 3.\n"
            "• Padding line 4.\n"
            "• Padding line 5.\n"
            "• Padding line 6.\n"
            "• Padding line 7.\n"
            "• Padding line 8.\n"
            "• Padding line 9.\n"
            "• Padding line 10.\n"
            "• Done.\n"
            "\n"
            "› \n"
            "  ? for shortcuts                     95% context left\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    async def test_handle_trust_prompt_dismisses_update_dialog(self, mock_tmux):
        """_handle_trust_prompt detects update dialog and selects '3'+Enter."""
        mock_tmux.return_value.get_history.side_effect = [
            (
                "✨ Update available! 0.142.5 -> 0.144.5\n"
                "1. Update now (runs npm install -g @openai/codex)\n"
                "2. Skip\n"
                "3. Skip until next version\n"
                "Press enter to continue\n"
            ),
            "OpenAI Codex (v0.142.5)\n› ",
        ]

        provider = CodexProvider("test1234", "test-session", "window-0")
        await provider._handle_trust_prompt(timeout=5.0)

        mock_tmux.return_value.send_keys.assert_any_call(
            "test-session", "window-0", "3", enter_count=0
        )
        mock_tmux.return_value.send_special_key.assert_any_call("test-session", "window-0", "Enter")

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    async def test_handle_trust_prompt_no_update_dialog(self, mock_tmux):
        """Normal startup (no dialog) must not trigger any dismissal keystrokes."""
        mock_tmux.return_value.get_history.return_value = "OpenAI Codex (v0.142.5)\n› "

        provider = CodexProvider("test1234", "test-session", "window-0")
        await provider._handle_trust_prompt(timeout=2.0)

        mock_tmux.return_value.send_keys.assert_not_called()
        mock_tmux.return_value.send_special_key.assert_not_called()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.wait_until_status")
    @patch("cli_agent_orchestrator.providers.codex.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    async def test_initialize_dismisses_update_dialog(
        self, mock_tmux, mock_wait_shell, mock_wait_status
    ):
        """initialize() sees the update dialog, sends '3'+Enter, then reaches ready."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_tmux.return_value.get_history.side_effect = [
            (
                "OpenAI Codex (v0.142.5)\n"
                "✨ Update available! 0.142.5 -> 0.144.5\n"
                "1. Update now (runs npm install -g @openai/codex)\n"
                "2. Skip\n"
                "3. Skip until next version\n"
                "Press enter to continue\n"
            ),
            "OpenAI Codex (v0.142.5)\n› ",
        ]

        provider = CodexProvider("test1234", "test-session", "window-0")
        result = await provider.initialize()

        assert result is True
        mock_tmux.return_value.send_keys.assert_any_call(
            "test-session", "window-0", "3", enter_count=0
        )
        mock_tmux.return_value.send_special_key.assert_any_call("test-session", "window-0", "Enter")
        mock_wait_status.assert_called_once()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    async def test_handle_trust_then_late_update_dialog(self, mock_tmux):
        """Multi-frame: trust dismissed → transitional banner (no dialog yet) → update dialog → dismissed."""
        # Frame 1: trust prompt visible
        frame_trust = (
            "Note: You're in a subdirectory of a Git project. Trusting will apply\n"
            "to the repository root: /Users/test/project\n"
            "\n"
            "Do you trust the contents of this directory?\n"
            "\n"
            "› 1. Yes, continue\n"
            "  2. No, quit\n"
            "\n"
            "Press enter to continue\n"
        )
        # Frame 2: trust dismissed, welcome banner visible but NO idle prompt yet
        # (transitional state before update dialog renders)
        frame_transitional = "OpenAI Codex (v0.142.5)\n"
        # Frame 3: update dialog renders
        frame_update = (
            "OpenAI Codex (v0.142.5)\n"
            "✨ Update available! 0.142.5 -> 0.144.5\n"
            "1. Update now (runs npm install -g @openai/codex)\n"
            "2. Skip\n"
            "3. Skip until next version\n"
            "Press enter to continue\n"
        )
        # Frame 4: update dismissed, idle prompt visible
        frame_idle = "OpenAI Codex (v0.142.5)\n› "

        mock_tmux.return_value.get_history.side_effect = [
            frame_trust,
            frame_transitional,
            frame_update,
            frame_idle,
        ]

        provider = CodexProvider("test1234", "test-session", "window-0")
        await provider._handle_trust_prompt(timeout=10.0)

        # Trust was dismissed with Enter
        mock_tmux.return_value.send_special_key.assert_any_call("test-session", "window-0", "Enter")
        # Update dialog was dismissed with '3' then Enter
        mock_tmux.return_value.send_keys.assert_any_call(
            "test-session", "window-0", "3", enter_count=0
        )

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_update_check_suppression_is_last_override(self, mock_load):
        """CAO's update suppression must win even if a profile sets the key."""
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_profile.codexProfile = None
        mock_profile.codexConfig = {"check_for_update_on_startup": True}
        mock_load.return_value = mock_profile

        provider = CodexProvider("tid", "sess", "win", "agent")
        command = provider._build_codex_command()

        assert "check_for_update_on_startup=true" in command
        assert command.endswith("-c check_for_update_on_startup=false")


class TestCodexProviderUpdateDialogLive:
    """Live binary tests for update dialog config key. Requires CAO_RUN_LIVE_PROVIDER_TESTS=1."""

    @pytest.mark.skipif(
        os.environ.get("CAO_RUN_LIVE_PROVIDER_TESTS", "") != "1",
        reason="Live provider tests disabled. Set CAO_RUN_LIVE_PROVIDER_TESTS=1 to enable.",
    )
    def test_check_for_update_on_startup_accepted_by_binary(self):
        """check_for_update_on_startup is a recognized config key under --strict-config."""
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_home:
            env = {**os.environ, "CODEX_HOME": tmp_home}
            result = subprocess.run(
                [
                    "codex",
                    "--strict-config",
                    "-c",
                    "check_for_update_on_startup=false",
                    "exec",
                    "--skip-git-repo-check",
                    "echo hi",
                ],
                capture_output=True,
                text=True,
                timeout=20,
                env=env,
            )
            combined = result.stdout + result.stderr
            assert (
                "unknown" not in combined.lower() or "configuration field" not in combined.lower()
            )

    @pytest.mark.skipif(
        os.environ.get("CAO_RUN_LIVE_PROVIDER_TESTS", "") != "1",
        reason="Live provider tests disabled. Set CAO_RUN_LIVE_PROVIDER_TESTS=1 to enable.",
    )
    def test_bogus_config_key_rejected_negative_control(self):
        """Negative control: a bogus key IS rejected under --strict-config."""
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_home:
            env = {**os.environ, "CODEX_HOME": tmp_home}
            result = subprocess.run(
                [
                    "codex",
                    "--strict-config",
                    "-c",
                    "this_key_does_not_exist_xyz_cao_probe=true",
                    "exec",
                    "--skip-git-repo-check",
                    "echo hi",
                ],
                capture_output=True,
                text=True,
                timeout=20,
                env=env,
            )
            combined = result.stdout + result.stderr
            assert "unknown configuration field" in combined.lower()


class TestCodexProviderExitDetection:
    """Tests for detecting when the codex process has exited."""

    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    def test_get_status_error_when_provider_exited(self, mock_tmux):
        """ERROR when pane command reverts to shell (codex process exited)."""
        mock_tmux.return_value.get_native_status.return_value = None
        mock_tmux.return_value.get_pane_current_command.return_value = "zsh"

        provider = CodexProvider("test1234", "test-session", "window-0")
        provider._initialized = True
        provider.shell_baseline = "zsh"
        output = "OpenAI Codex (v0.98.0)\n› \n% \n"
        status = provider.get_status(output)

        assert status == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    def test_get_status_normal_when_codex_running(self, mock_tmux):
        """Normal status detection when codex is still running (pane command != shell)."""
        mock_tmux.return_value.get_native_status.return_value = None
        mock_tmux.return_value.get_pane_current_command.return_value = "codex"

        provider = CodexProvider("test1234", "test-session", "window-0")
        provider._initialized = True
        provider.shell_baseline = "zsh"
        output = (
            "OpenAI Codex (v0.98.0)\n"
            "› \n"
            "  ? for shortcuts                     100% context left\n"
        )
        status = provider.get_status(output)

        assert status == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    def test_get_status_skips_exit_check_before_init(self, mock_tmux):
        """Exit check skipped before initialization (avoids false ERROR on launch)."""
        mock_tmux.return_value.get_native_status.return_value = None
        mock_tmux.return_value.get_pane_current_command.return_value = "zsh"

        provider = CodexProvider("test1234", "test-session", "window-0")
        provider._initialized = False
        provider.shell_baseline = "zsh"
        output = "OpenAI Codex (v0.98.0)\n› \n"
        status = provider.get_status(output)

        # Should NOT be ERROR — exit check gated on _initialized
        assert status == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    def test_get_status_skips_exit_check_without_baseline(self, mock_tmux):
        """Exit check skipped when no shell_baseline was captured."""
        mock_tmux.return_value.get_native_status.return_value = None
        mock_tmux.return_value.get_pane_current_command.return_value = "zsh"

        provider = CodexProvider("test1234", "test-session", "window-0")
        provider._initialized = True
        provider.shell_baseline = None
        output = "OpenAI Codex (v0.98.0)\n› \n"
        status = provider.get_status(output)

        # Should NOT be ERROR — no baseline to compare against
        assert status == TerminalStatus.IDLE

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.wait_until_status")
    @patch("cli_agent_orchestrator.providers.codex.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    async def test_initialize_captures_shell_baseline(
        self, mock_tmux, mock_wait_shell, mock_wait_status
    ):
        """Initialize captures shell_baseline for exit detection."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_tmux.return_value.get_history.return_value = "OpenAI Codex (v0.98.0)"
        mock_tmux.return_value.get_pane_current_command.return_value = "zsh"

        provider = CodexProvider("test1234", "test-session", "window-0")
        await provider.initialize()

        assert provider.shell_baseline == "zsh"
        mock_tmux.return_value.get_pane_current_command.assert_called_with(
            "test-session", "window-0"
        )


class TestCodexLaunchFlagsValidity:
    """Validate that all flags in the codex launch command are accepted by the binary.

    Gated behind CAO_RUN_LIVE_PROVIDER_TESTS=1 since it invokes the real codex binary.
    """

    @pytest.mark.skipif(
        os.environ.get("CAO_RUN_LIVE_PROVIDER_TESTS", "") != "1",
        reason="Live provider tests disabled. Set CAO_RUN_LIVE_PROVIDER_TESTS=1 to enable.",
    )
    def test_codex_launch_flags_are_valid(self):
        """Every flag in the default launch command must be accepted by codex."""
        import subprocess

        provider = CodexProvider("test1234", "test-session", "window-0", None)
        command = provider._build_codex_command()
        parts = shlex.split(command)

        # Extract flags (anything starting with -)
        flags = [p for p in parts if p.startswith("-")]
        assert flags, "Expected flags in codex launch command"

        # Get codex help to validate flags exist
        help_result = subprocess.run(
            ["codex", "--help"], capture_output=True, text=True, timeout=10
        )
        help_text = help_result.stdout + help_result.stderr

        for flag in flags:
            flag_name = flag.lstrip("-").split("=")[0]
            if flag_name in help_text or f"--{flag_name}" in help_text:
                continue
            # Hidden aliases (e.g. --yolo) don't appear in --help; probe the
            # binary directly — an unknown flag makes clap exit non-zero with
            # "unexpected argument".
            probe = subprocess.run(
                ["codex", flag, "--help"], capture_output=True, text=True, timeout=10
            )
            assert (
                probe.returncode == 0 and "unexpected argument" not in probe.stderr
            ), f"Flag '{flag}' in launch command rejected by codex binary"
