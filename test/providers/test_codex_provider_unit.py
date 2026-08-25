"""Unit tests for Codex provider."""

import os
import re
import shlex
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.codex import (
    APPROVAL_PROMPT_FOOTER,
    CodexProvider,
    ProviderError,
    _find_response_marker,
    _has_approval_modal_in_bottom,
    _has_approval_prompt_in_bottom,
    _has_startup_idle_composer,
    _toml_override,
    _toml_scalar,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(filename: str) -> str:
    with open(FIXTURES_DIR / filename, "r") as f:
        return f.read()


def read_developer_instructions_file(command: str) -> str:
    """Extracts the path from the command's ``$(cat <path>)`` developer_instructions
    fragment and returns that file's actual on-disk content -- the fragment keeps the
    launch line itself short (see codex.py's own long comment at the assignment site),
    so tests that need to check the actual (TOML-escaped) prompt text now read it from
    here instead of asserting on ``command`` directly."""
    match = re.search(r"\$\(cat (\S+)\)", command)
    assert match is not None, f"no $(cat <file>) developer_instructions fragment in: {command!r}"
    return Path(match.group(1)).read_text(encoding="utf-8")


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
    def test_build_command_with_skill_prompt(self, mock_load_profile, tmp_path):
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
        with patch("cli_agent_orchestrator.providers.codex.CAO_HOME_DIR", tmp_path):
            command = provider._build_codex_command()

        mock_load_profile.assert_called_once_with("code_supervisor")
        assert "developer_instructions=$(cat " in command
        instructions = read_developer_instructions_file(command)
        assert "## Available Skills" in instructions
        assert "python-testing" in instructions

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_with_agent_profile(self, mock_load_profile, tmp_path):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = "You are a code supervisor agent."
        mock_profile.mcpServers = None
        mock_profile.codexProfile = None
        mock_load_profile.return_value = mock_profile

        provider = CodexProvider("test1234", "test-session", "window-0", "code_supervisor")
        with patch("cli_agent_orchestrator.providers.codex.CAO_HOME_DIR", tmp_path):
            command = provider._build_codex_command()

        mock_load_profile.assert_called_once_with("code_supervisor")
        assert "codex --yolo --no-alt-screen --disable shell_snapshot" in command
        assert "-c" in command
        assert "developer_instructions=$(cat " in command
        assert "You are a code supervisor agent." in read_developer_instructions_file(command)

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_escapes_quotes(self, mock_load_profile, tmp_path):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = 'Use "double quotes" carefully.'
        mock_profile.mcpServers = None
        mock_profile.codexProfile = None
        mock_load_profile.return_value = mock_profile

        provider = CodexProvider("test1234", "test-session", "window-0", "test_agent")
        with patch("cli_agent_orchestrator.providers.codex.CAO_HOME_DIR", tmp_path):
            command = provider._build_codex_command()

        assert '\\"double quotes\\"' in read_developer_instructions_file(command)

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_escapes_newlines(self, mock_load_profile, tmp_path):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = "Line one.\nLine two.\n\n## Section\n- Item"
        mock_profile.mcpServers = None
        mock_profile.codexProfile = None
        mock_load_profile.return_value = mock_profile

        provider = CodexProvider("test1234", "test-session", "window-0", "test_agent")
        with patch("cli_agent_orchestrator.providers.codex.CAO_HOME_DIR", tmp_path):
            command = provider._build_codex_command()

        # The launch line itself must never contain a literal newline (that's the whole point
        # of this fix -- see the long comment at the fragment's assignment site in codex.py) OR
        # any of the actual prompt text; both now live only in the temp file.
        assert "\n" not in command
        assert "Line one." not in command

        # Literal newlines in the prompt must be escaped to \n for TOML and tmux compatibility,
        # in the temp file's own content.
        instructions = read_developer_instructions_file(command)
        assert "\n" not in instructions
        assert "\\n" in instructions
        assert "Line one.\\nLine two.\\n\\n## Section\\n- Item" in instructions

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_with_mcp_servers(self, mock_load_profile):
        mock_profile = MagicMock()
        mock_profile.model = None
        # Empty -- these assertions only care about the MCP -c overrides, not the
        # developer_instructions temp file, so there's nothing to gain from writing
        # one to disk (and every write outside a patched CAO_HOME_DIR touches the
        # real ~/.aws/cli-agent-orchestrator/tmp/, same convention as
        # test_build_command_with_mcp_servers_env below).
        mock_profile.system_prompt = ""
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
        # Empty -- see test_build_command_with_mcp_servers's comment above.
        mock_profile.system_prompt = ""
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
        # Empty -- see test_build_command_with_mcp_servers's comment above.
        mock_profile.system_prompt = ""
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
        self, mock_tmux, mock_load_profile, mock_wait_shell, mock_wait_status, tmp_path
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
        with patch("cli_agent_orchestrator.providers.codex.CAO_HOME_DIR", tmp_path):
            result = await provider.initialize()

        assert result is True
        # The second send_keys call should contain developer_instructions
        codex_call = mock_tmux.return_value.send_keys.call_args_list[1]
        assert "developer_instructions=$(cat " in codex_call.args[2]
        assert "You are a supervisor." in read_developer_instructions_file(codex_call.args[2])


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

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_explicit_model_override_wins_over_profile_model(self, mock_load):
        mock_profile = MagicMock()
        mock_profile.model = "gpt-5"
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_profile.codexProfile = None
        mock_load.return_value = mock_profile

        provider = CodexProvider("tid", "sess", "win", "agent", model="fable-5")
        command = provider._build_codex_command()

        assert "--model fable-5" in command
        assert "--model gpt-5" not in command

    def test_explicit_model_override_applies_with_no_agent_profile(self):
        provider = CodexProvider("tid", "sess", "win", None, model="fable-5")
        command = provider._build_codex_command()

        assert "--model fable-5" in command


class TestCodexBuildCommandExtra:
    """Coverage for branches inside ``_build_codex_command`` that the
    pre-existing fixtures didn't exercise."""

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_security_prompt_prepended_when_tools_restricted(self, mock_load, tmp_path):
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
        with patch("cli_agent_orchestrator.providers.codex.CAO_HOME_DIR", tmp_path):
            command = provider._build_codex_command()

        instructions = read_developer_instructions_file(command)
        assert "You only have access to these tools: fs_read, fs_list" in instructions
        assert "Original system prompt." in instructions
        # SECURITY_PROMPT lives in constants; assert on a stable substring
        # rather than importing the constant into the test fixture.
        assert "NEVER" in instructions  # "NEVER read/output: ~/.aws/credentials..."

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_long_system_prompt_keeps_launch_line_short(self, mock_load, tmp_path):
        """Regression test for the real, live-reproduced failure: a large system_prompt
        (harness-control's own injected operating instructions + skill list commonly produce
        several KB once escaped) used to be inlined directly into the launch command via
        ``-c developer_instructions="<escaped text>"``. When that pane is still a bare shell
        (codex has not started yet -- correctly NOT given bracketed-paste framing, since a bare
        shell does not understand those escape sequences), a single typed/pasted line beyond the
        tty's canonical-mode line-length limit (MAX_CANON, 4096 bytes on Linux) is silently
        truncated by the kernel's tty line discipline before the shell ever sees a complete,
        valid command -- the shell hangs at an unclosed-quote continuation prompt forever, no
        codex process is ever spawned, and CAO's own init-timeout eventually fires with a
        generic "Codex initialization timed out" that gives no hint of the real cause.

        Confirmed live (isolated scratch tmux pane, zero risk to any other session): an 8.3KB
        escaped instructions payload, sent via CAO's own real send_keys code path to a real bare
        shell pane, never executed even after an explicit trailing Enter (verified with a
        marker-file test) -- while `dash -n`/`bash -n` on the exact same text as a plain script
        confirmed the content itself was syntactically valid, ruling out a quoting bug and
        pointing squarely at line length as the real, sole cause."""
        long_prompt = "A" * 10_000  # escapes to something well over the 4096-byte MAX_CANON limit
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = long_prompt
        mock_profile.mcpServers = None
        mock_profile.codexProfile = None
        mock_load.return_value = mock_profile

        provider = CodexProvider("tid", "sess", "win", "agent")
        with patch("cli_agent_orchestrator.providers.codex.CAO_HOME_DIR", tmp_path):
            command = provider._build_codex_command()

        # The actual typed/pasted launch line must stay well under the tty's canonical-mode
        # line-length limit regardless of how long the instructions text is -- this is the
        # entire point of the fix. 1000 is a generous margin under the real 4096-byte limit.
        assert len(command) < 1000, (
            f"launch line is {len(command)} bytes -- long enough to risk the tty canonical-mode "
            "line-length limit this fix exists to avoid"
        )
        assert long_prompt not in command
        assert "developer_instructions=$(cat " in command
        assert long_prompt in read_developer_instructions_file(command)

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_developer_instructions_file_written_with_owner_only_permissions(
        self, mock_load, tmp_path
    ):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = "Sensitive: contains real secrets context."
        mock_profile.mcpServers = None
        mock_profile.codexProfile = None
        mock_load.return_value = mock_profile

        provider = CodexProvider("tid", "sess", "win", "agent")
        with patch("cli_agent_orchestrator.providers.codex.CAO_HOME_DIR", tmp_path):
            command = provider._build_codex_command()

        match = re.search(r"\$\(cat (\S+)\)", command)
        assert match is not None
        file_path = Path(match.group(1))
        assert oct(file_path.stat().st_mode)[-3:] == "600"

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_cleanup_removes_developer_instructions_file(self, mock_load, tmp_path):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = "Some instructions."
        mock_profile.mcpServers = None
        mock_profile.codexProfile = None
        mock_load.return_value = mock_profile

        provider = CodexProvider("tid", "sess", "win", "agent")
        with patch("cli_agent_orchestrator.providers.codex.CAO_HOME_DIR", tmp_path):
            command = provider._build_codex_command()
            match = re.search(r"\$\(cat (\S+)\)", command)
            assert match is not None
            file_path = Path(match.group(1))
            assert file_path.exists()

            provider.cleanup()
            assert not file_path.exists()

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


class TestCodexRenderedScreenStatusDetection:
    """Regression coverage for in-place Codex TUI redraws.

    ``tmux pipe-pane`` is append-only: text erased from the visible terminal
    remains in CAO's raw rolling buffer.  MCP startup uses the same spinner
    shape as a live agent turn, so raw parsing can remain PROCESSING forever
    after the visible screen has returned to the idle composer.
    """

    def test_provider_opts_into_rendered_screen_detection(self):
        provider = CodexProvider("test1234", "test-session", "window-0")

        assert provider.supports_screen_detection is True

    def test_blank_rendered_screen_is_unknown(self):
        provider = CodexProvider("test1234", "test-session", "window-0")

        assert provider.get_status_from_screen(["", "   "]) == TerminalStatus.UNKNOWN

    def test_overwritten_mcp_startup_spinner_does_not_pin_processing(self):
        import pyte

        raw = (
            "\x1b[1;1H• Starting MCP servers (0/3): cao-mcp-server"
            " (0s • esc to interrupt)"
            "\x1b[3;1H› Improve documentation in @filename"
            "\x1b[5;1H  gpt-5.6-terra high · /tmp/project"
            # Codex clears the transient activity row once MCP startup settles.
            "\x1b[1;1H\x1b[2K"
        )
        screen = pyte.Screen(200, 20)
        pyte.Stream(screen).feed(raw)
        provider = CodexProvider("test1234", "test-session", "window-0")

        # Demonstrate the old failure mode: stripping cursor controls from the
        # append-only stream leaves the erased spinner behind.
        assert provider.get_status(raw) == TerminalStatus.PROCESSING
        # The composited viewport contains only the live idle composer.
        assert provider.get_status_from_screen(list(screen.display)) == TerminalStatus.IDLE

    def test_live_mcp_startup_spinner_is_processing(self):
        screen_lines = [
            "• Starting MCP servers (1/3): cao-mcp-server (0s • esc to interrupt)",
            "",
            "› Improve documentation in @filename",
            "",
            "  gpt-5.6-terra high · /tmp/project",
        ]
        provider = CodexProvider("test1234", "test-session", "window-0")

        assert provider.get_status_from_screen(screen_lines) == TerminalStatus.PROCESSING

    @pytest.mark.parametrize("elapsed", ["1m 00s", "1h 00m 00s"])
    def test_minute_plus_live_progress_is_processing(self, elapsed):
        screen_lines = [
            "› Implement the requested feature",
            f"• Working ({elapsed} • esc to interrupt)",
            "",
            "› Improve documentation in @filename",
            "",
            "  gpt-5.6-terra high · /tmp/project",
        ]
        provider = CodexProvider("test1234", "test-session", "window-0")

        assert provider.get_status_from_screen(screen_lines) == TerminalStatus.PROCESSING

    def test_completed_turn_on_rendered_screen_is_completed(self):
        screen_lines = [
            "› Reply with the readiness token",
            "• CAO_CODEX_READY",
            "",
            "› Improve documentation in @filename",
            "",
            "  gpt-5.6-terra high · /tmp/project",
        ]
        provider = CodexProvider("test1234", "test-session", "window-0")

        assert provider.get_status_from_screen(screen_lines) == TerminalStatus.COMPLETED


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

    def test_extract_preserves_ambiguous_compact_bullet_group(self):
        """Compact bullet groups are indistinguishable from a legitimate answer."""
        output = (
            "› fix the failing test\n"
            "\n"
            "• Explored src/providers\n"
            "• Ran pytest -q\n"
            "\n"
            "• The bug is in the poll loop.\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "Explored src/providers" in message
        assert "Ran pytest -q" in message
        assert "The bug is in the poll loop" in message

    def test_response_marker_returns_none_without_assistant_output(self):
        """A user prompt without a response has no response marker."""
        assert _find_response_marker("› still waiting") is None

    def test_response_marker_handles_final_line_without_newline(self):
        """A marker on the final line is detected without a trailing newline."""
        marker = _find_response_marker("• Complete")

        assert marker is not None
        assert marker.group() == "•"

    def test_extract_preserves_single_tree_formatted_bullet(self):
        """One tree-formatted bullet can be a legitimate answer, so retain it."""
        output = (
            "› inspect the provider\n"
            "• Explored src/providers\n"
            "  └ Read codex.py\n"
            "\n"
            "• The extraction starts at the wrong marker.\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "Explored src/providers" in message
        assert "Read codex.py" in message
        assert "The extraction starts at the wrong marker" in message

    def test_extract_skips_multiple_blank_separated_activity_cells(self):
        """The response starts after the last complete native activity cell."""
        output = (
            "› inspect the provider\n"
            "• Explored\n"
            "  └ Read codex.py\n"
            "\n"
            "• Ran pytest -q\n"
            "  └ 170 passed\n"
            "\n"
            "• The bug is fixed.\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert message == "• The bug is fixed."

    def test_extract_skips_activity_cells_before_prose_reply(self):
        """A prose reply starts after the last complete native activity cell."""
        output = (
            "› inspect the provider\n"
            "• Explored\n"
            "  └ Read codex.py\n"
            "\n"
            "• Ran pytest -q\n"
            "  └ 170 passed\n"
            "\n"
            "The bug is fixed.\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert message == "The bug is fixed."

    def test_extract_skips_multiple_tree_rows_before_prose_reply(self):
        """All tree rows in the final activity cell stay before the reply."""
        output = (
            "› inspect the provider\n"
            "• Explored\n"
            "  └ Read codex.py\n"
            "\n"
            "• Ran pytest -q\n"
            "  └ pytest -q\n"
            "  └ 170 passed\n"
            "\n"
            "All green.\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert message == "All green."

    def test_extract_skips_indented_tree_output_before_prose_reply(self):
        """Indented output belonging to the final tree row is not returned."""
        output = (
            "› inspect the provider\n"
            "• Explored\n"
            "  └ Read codex.py\n"
            "\n"
            "• Ran pytest -q\n"
            "  └ 170 passed\n"
            "    3 skipped\n"
            "\n"
            "All green.\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert message == "All green."

    def test_extract_skips_three_activity_cells_before_prose_reply(self):
        """All complete activity cells are removed before a prose reply."""
        output = (
            "› inspect the provider\n"
            "• Explored\n"
            "  └ Read codex.py\n"
            "\n"
            "• Edited\n"
            "  └ Updated codex.py\n"
            "\n"
            "• Ran pytest -q\n"
            "  └ 170 passed\n"
            "\n"
            "The bug is fixed.\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert message == "The bug is fixed."

    def test_extract_skips_interleaved_commentary_and_activity(self):
        """Commentary between complete activity cells stays before the reply boundary."""
        output = (
            "› inspect the provider\n"
            "• Explored\n"
            "  └ Read codex.py\n"
            "I will verify the focused behavior next.\n"
            "\n"
            "• Ran pytest -q\n"
            "  └ 170 passed\n"
            "\n"
            "• The bug is fixed.\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert message == "• The bug is fixed."

    def test_extract_preserves_two_consecutive_legitimate_answer_bullets(self):
        """An ambiguous compact answer is preserved rather than truncated."""
        output = (
            "› summarize the fix\n"
            "• Fixed parser\n"
            "• Added regression tests\n"
            "\n"
            "• Verification: all tests pass\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "Fixed parser" in message
        assert "Added regression tests" in message
        assert "Verification: all tests pass" in message

    def test_extract_preserves_tree_formatted_legitimate_answer(self):
        """One tree-formatted answer followed by another bullet is not activity."""
        output = (
            "› summarize the fix\n"
            "• Files changed\n"
            "  └ src/provider.py\n"
            "\n"
            "• Tests pass\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "Files changed" in message
        assert "src/provider.py" in message
        assert "Tests pass" in message

    def test_extract_does_not_count_mcp_tree_output_as_activity_cells(self):
        """MCP output must not complete neighboring model-reply bullets."""
        output = (
            "› summarize the work\n"
            "• First finding\n"
            '• Called tools.inspect({"path":"src"})\n'
            "  └ inspection result\n"
            "\n"
            "• Second finding\n"
            '• Called tools.verify({"path":"test"})\n'
            "  └ verification result\n"
            "\n"
            "• Conclusion\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "First finding" in message
        assert "Second finding" in message
        assert "Conclusion" in message

    def test_extract_preserves_blank_separated_reply_bullets(self):
        """A single reply bullet before a blank line is not an activity prelude."""
        output = (
            "› summarize the fix\n"
            "• The parser now uses structural layout.\n"
            "\n"
            "• English verbs remain valid answer text.\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "parser now uses structural layout" in message
        assert "English verbs remain valid" in message


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

    @pytest.mark.parametrize(
        "placeholder",
        [
            "Explain this codebase",
            "Summarize recent commits",
            "Implement {feature}",
            "Find and fix a bug in @filename",
            "Write tests for @filename",
            "Improve documentation in @filename",
            "Run /review on my current changes",
            "Use /skills to list available skills",
        ],
    )
    def test_v0145_idle_composer_placeholders(self, placeholder):
        output = (
            f"OpenAI Codex (v0.145.0)\n› {placeholder}\n"
            "  gpt-5.6-sol medium · Context 100% left\n"
        )

        assert _has_startup_idle_composer(output) is True

    def test_v0149_idle_composer_placeholder(self):
        """Codex 0.149's new glyph/copy is a ready composer, not a timeout."""
        output = (
            "OpenAI Codex (v0.149.0)\n"
            "» Ask Codex to do anything\n\n"
            "  gpt-5.6-sol ultra · /private/tmp/cao-smoke\n"
        )

        assert _has_startup_idle_composer(output) is True

    @pytest.mark.asyncio
    @patch(
        "cli_agent_orchestrator.providers.codex.time.time",
        side_effect=[0.0, 0.0, 20.0],
    )
    @patch("cli_agent_orchestrator.providers.codex.asyncio.sleep", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.providers.codex.logger.error")
    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    async def test_handle_trust_prompt_returns_on_v0149_idle_composer(
        self, mock_backend, mock_error, mock_sleep, _mock_time
    ):
        mock_backend.return_value.get_history.return_value = (
            "OpenAI Codex (v0.149.0)\n"
            "» Ask Codex to do anything\n\n"
            "  gpt-5.6-sol ultra · /private/tmp/cao-smoke\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        await provider._handle_trust_prompt(timeout=20.0)

        mock_backend.return_value.get_history.assert_called_once()
        mock_sleep.assert_not_awaited()
        mock_error.assert_not_called()
        mock_backend.return_value.send_keys.assert_not_called()
        mock_backend.return_value.send_special_key.assert_not_called()

    @pytest.mark.asyncio
    @patch(
        "cli_agent_orchestrator.providers.codex.time.time",
        side_effect=[0.0, 0.0, 20.0],
    )
    @patch("cli_agent_orchestrator.providers.codex.asyncio.sleep", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.providers.codex.logger.error")
    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    async def test_handle_trust_prompt_returns_on_v0145_idle_composer(
        self, mock_backend, mock_error, mock_sleep, _mock_time
    ):
        """Codex 0.145's placeholder composer is a ready state, not a timeout."""
        mock_backend.return_value.get_history.return_value = load_fixture(
            "codex_v0145_idle_output.txt"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        await provider._handle_trust_prompt(timeout=20.0)

        mock_backend.return_value.get_history.assert_called_once()
        mock_sleep.assert_not_awaited()
        mock_error.assert_not_called()
        mock_backend.return_value.send_keys.assert_not_called()
        mock_backend.return_value.send_special_key.assert_not_called()

    @pytest.mark.asyncio
    @patch(
        "cli_agent_orchestrator.providers.codex.time.time",
        side_effect=[0.0, 0.0, 1.0, 2.0, 20.0],
    )
    @patch("cli_agent_orchestrator.providers.codex.asyncio.sleep", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.providers.codex.logger.error")
    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    async def test_handle_trust_prompt_waits_for_complete_v0145_composer_frame(
        self, mock_backend, mock_error, mock_sleep, _mock_time
    ):
        """Chunked redraws are not ready until composer and footer are both visible."""
        fixture = load_fixture("codex_v0145_idle_output.txt")
        mock_backend.return_value.get_history.side_effect = [
            "OpenAI Codex (v0.145.0)\n",
            "OpenAI Codex (v0.145.0)\n› Write tests for @filename\n",
            fixture,
            "timeout tail",
        ]

        provider = CodexProvider("test1234", "test-session", "window-0")
        await provider._handle_trust_prompt(timeout=20.0)

        assert mock_backend.return_value.get_history.call_count == 3
        assert mock_sleep.await_count == 2
        mock_error.assert_not_called()

    @pytest.mark.parametrize(
        "output",
        [
            (
                "› Fix the failing tests\n"
                "• Working (2s • esc to interrupt)\n"
                "› Write tests for @filename\n"
                "  gpt-5.6-sol medium · Context 100% left\n"
            ),
            (
                "› Fix the failing tests\n"
                "• Working\n"
                "› Write tests for @filename\n"
                "  gpt-5.6-sol medium · Context 100% left\n"
            ),
            (
                "Approve this command? [y/n]\n"
                "› Write tests for @filename\n"
                "  gpt-5.6-sol medium · Context 100% left\n"
            ),
            (
                "› Write tests for @filename\n"
                "  gpt-5.6-sol medium · Context 100% left\n"
                "╭─ Command Approval Required ─╮\n"
                "│ [a] Accept  [d] Decline     │\n"
                "╰─────────────────────────────╯\n"
            ),
            ("› fix the failing tests\n" "  gpt-5.6-sol medium · Context 100% left\n"),
            "OpenAI Codex (v0.145.0)\nLoading project instructions\n",
            (
                "The docs show › Write tests for @filename as an example.\n"
                "This is not a Codex footer: Context 100% left\n"
            ),
            "› \nold output\n\nstill running\n\nlatest output\n",
        ],
        ids=[
            "working",
            "partial-working-frame",
            "approval",
            "boxed-approval",
            "typed-draft",
            "ordinary-output",
            "similar-strings",
            "stale-legacy-prompt",
        ],
    )
    @pytest.mark.asyncio
    @patch(
        "cli_agent_orchestrator.providers.codex.time.time",
        side_effect=[0.0, 0.0, 20.0],
    )
    @patch("cli_agent_orchestrator.providers.codex.asyncio.sleep", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.providers.codex.logger.error")
    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    async def test_handle_trust_prompt_does_not_treat_non_ready_output_as_idle(
        self, mock_backend, mock_error, mock_sleep, _mock_time, output
    ):
        mock_backend.return_value.get_history.return_value = output

        provider = CodexProvider("test1234", "test-session", "window-0")
        await provider._handle_trust_prompt(timeout=20.0)

        mock_sleep.assert_awaited_once_with(1.0)
        mock_error.assert_called_once()
        mock_backend.return_value.send_keys.assert_not_called()
        mock_backend.return_value.send_special_key.assert_not_called()

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

    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    def test_get_status_trust_prompt_v2_is_waiting(self, mock_backend):
        """V2 trust dialog in bottom region classifies WAITING_USER_ANSWER."""
        mock_backend.return_value.get_pane_current_command.return_value = "codex"
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

    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    def test_get_status_trust_v2_in_scrollback_does_not_false_positive(self, mock_backend):
        """V2 trust text in scrollback (not bottom) must NOT trigger WAITING."""
        mock_backend.return_value.get_pane_current_command.return_value = "codex"
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

    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    def test_get_status_login_menu_is_waiting(self, mock_backend):
        """First-run auth menu (no credentials configured) in the bottom region
        classifies WAITING_USER_ANSWER -- live-reproduced real Codex output."""
        mock_backend.return_value.get_pane_current_command.return_value = "codex"
        output = (
            "  Welcome to Codex, OpenAI's command-line coding agent\n"
            "\n"
            "  Sign in with ChatGPT to use Codex as part of your paid plan\n"
            "  or connect an API key for usage-based billing\n"
            "\n"
            "> 1. Sign in with ChatGPT\n"
            "     Usage included with Plus, Pro, Business, and Enterprise plans\n"
            "\n"
            "  2. Sign in with Device Code\n"
            "     Sign in from another device with a one-time code\n"
            "\n"
            "  3. Provide your own API key\n"
            "     Pay for what you use\n"
            "\n"
            "  Press enter to continue\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        provider._initialized = True
        provider.shell_baseline = "zsh"
        status = provider.get_status(output)

        assert status == TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    def test_get_status_login_menu_in_scrollback_does_not_false_positive(self, mock_backend):
        """Login-menu text in scrollback (not the bottom region) must NOT trigger WAITING --
        same bottom-anchoring discipline as the V2 trust dialog check immediately above."""
        mock_backend.return_value.get_pane_current_command.return_value = "codex"
        output = (
            "› explain the codex login menu\n"
            '• Earlier it showed "Sign in with ChatGPT to use Codex as part of your paid plan".\n'
            "• That happens on first run with no credentials configured.\n"
            "• There were three options: ChatGPT, Device Code, or an API key.\n"
            "• Once authenticated, this menu never shows again.\n"
            "• You can re-trigger it with codex logout.\n"
            "• The credentials get stored in ~/.codex/auth.json.\n"
            "• API keys are validated on first use, not at login time.\n"
            "• Device code login works well for headless environments.\n"
            "• ChatGPT login opens a browser tab for OAuth.\n"
            "• Both paths end up writing the same auth.json format.\n"
            "• You can check current auth status with codex login status.\n"
            "• Logging out clears the stored credentials entirely.\n"
            "• None of this appears again once you're signed in.\n"
            "• This whole explanation is well past fifteen lines by now.\n"
            "• Padding further to push the earlier mention out of the tail window.\n"
            "\n"
            "› \n"
            "  ? for shortcuts                     95% context left\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        provider._initialized = True
        provider.shell_baseline = "zsh"
        status = provider.get_status(output)

        assert status != TerminalStatus.WAITING_USER_ANSWER

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.wait_until_status")
    @patch("cli_agent_orchestrator.providers.codex.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex.get_backend")
    async def test_initialize_includes_waiting_user_answer_in_target_status(
        self, mock_tmux, mock_wait_shell, mock_wait_status
    ):
        """Regression test for the real, live-reproduced failure: an account with no
        credentials configured yet reaches a correctly-rendered, fully-alive login screen
        that never becomes IDLE/COMPLETED on its own -- initialize()'s own
        wait_until_status(..., {IDLE, COMPLETED}, ...) target set had no way to ever
        succeed for it, so CAO tore the terminal down on every single attempt (a live,
        reproduced "Codex initialization timed out after 60 seconds", the operator's own
        "the session doesn't even start" symptom) before anyone had a real chance to open
        the session and complete login. WAITING_USER_ANSWER must be in the target set."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_tmux.return_value.get_history.return_value = "OpenAI Codex (v0.98.0)"

        provider = CodexProvider("test1234", "test-session", "window-0", None)
        result = await provider.initialize()

        assert result is True
        target_status_arg = mock_wait_status.call_args.args[1]
        assert TerminalStatus.WAITING_USER_ANSWER in target_status_arg
        assert TerminalStatus.IDLE in target_status_arg
        assert TerminalStatus.COMPLETED in target_status_arg

    def test_backend_registry_is_clean_at_test_start(self):
        """Regression for #522: autouse fixture resets the backend singleton."""
        from cli_agent_orchestrator.backends import registry

        assert registry._backend is None, (
            "Backend singleton leaked from a prior test — "
            "_reset_backend_registry fixture is not working"
        )


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


class TestCodexProviderApprovalModal:
    """Tests for Codex's boxed command-approval modal appearing at RUNTIME.

    The modal's copy was previously only consulted on the startup path
    (STARTUP_BLOCKING_INPUT_PATTERN in _has_startup_idle_composer), so a pane
    blocked on it mid-session was classified COMPLETED/PROCESSING and the
    conductor would keep sending work into a pane hard-blocked on a keystroke.
    """

    def test_get_status_approval_modal_waiting(self):
        """Active approval modal classifies as WAITING_USER_ANSWER."""
        output = load_fixture("codex_approval_modal.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_get_status_approval_modal_below_tui_footer_is_not_completed(self):
        """Composer chrome above the modal must not win over the modal.

        This is the dangerous shape: the TUI keeps rendering the idle composer
        and status bar while the modal is up, so the idle-prompt check reported
        COMPLETED — telling the conductor the agent was free.
        """
        output = (
            "› run the deploy script\n"
            "• I'll run the deploy script now.\n"
            "› \n"
            "  ? for shortcuts                     92% context left\n"
            "╭─ Command Approval Required ─╮\n"
            "│ [a] Accept  [d] Decline     │\n"
            "╰─────────────────────────────╯\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_get_status_approval_modal_unframed(self):
        """Modal without box-drawing chrome still classifies as WAITING.

        The frame glyphs have changed across Codex releases while the copy has
        not, so detection must not depend on them.
        """
        output = "› run the deploy script\nCommand Approval Required\n[a] Accept  [d] Decline\n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_get_status_approval_modal_in_scrollback_is_completed(self):
        """An already-answered modal scrolled out of the bottom region must not latch."""
        output = load_fixture("codex_approval_modal_scrollback.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_approval_modal_quoted_in_assistant_reply_is_completed(self):
        """The model describing the modal in prose must not be read as the modal.

        Cannot be excluded by the assistant_after_last_user gate — a real modal
        also appears after assistant bullets — so it is excluded structurally:
        prose embeds the copy mid-sentence instead of owning its own line.
        """
        output = (
            "› why did the last run stall?\n"
            "• The pane was blocked on Codex's Command Approval Required modal, "
            "which offers [a] Accept and [d] Decline and cannot be answered by CAO.\n"
            '• Switch the profile to approval_policy = "never" to avoid it.\n'
            "› \n"
            "  ? for shortcuts                     91% context left\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_choice_keys_without_header_is_completed(self):
        """Choice keys alone are not a modal — both halves must corroborate."""
        output = (
            "› list the approval keys\n"
            "• [a] Accept and [d] Decline are the approval keys.\n"
            "› \n"
            "  ? for shortcuts                     91% context left\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_header_without_choice_keys_is_not_waiting(self):
        """Header alone is not a modal — the choice line must be present too."""
        output = "› run the deploy script\n╭─ Command Approval Required ─╮\n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status != TerminalStatus.WAITING_USER_ANSWER

    def test_has_approval_modal_requires_header_above_choices(self):
        """Choice keys ABOVE the header are a partial/scrolled frame, not a live modal."""
        assert not _has_approval_modal_in_bottom(
            "│ [a] Accept  [d] Decline     │\n╭─ Command Approval Required ─╮\n"
        )
        assert _has_approval_modal_in_bottom(
            "╭─ Command Approval Required ─╮\n│ [a] Accept  [d] Decline     │\n"
        )

    def test_get_status_modal_quoted_as_indented_transcript_is_completed(self):
        """The model quoting a modal TRANSCRIPT back must not be read as the modal.

        Harder than prose: the quoted block reproduces the modal's per-line
        structure exactly (header alone on its line, choice keys starting their
        line), so it satisfies the corroboration and line-structure guards. Only
        the left-margin guard separates it — the quote is indented under its
        bullet, the real box is drawn at the margin.
        """
        output = load_fixture("codex_approval_modal_quoted_in_reply.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_modal_quoted_as_markdown_table_is_completed(self):
        """A modal transcribed into a markdown table must not be read as the modal.

        Motivates excluding ASCII ``+-|`` from MODAL_FRAME_CHARS: were they
        stripped as frame chrome, these rows would reduce to the modal shape
        while sitting at the left margin, defeating every guard.
        """
        output = (
            "› document the approval modal\n"
            "• I documented it as:\n"
            "| Command Approval Required |\n"
            "| [a] Accept | [d] Decline |\n"
            "› \n"
            "  ? for shortcuts                     90% context left\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_approval_modal_heavy_box(self):
        """A modal framed in heavy box-drawing glyphs still classifies as WAITING.

        Defensive: only light glyphs have been observed in the wild, but the
        frame style is not contractual and missing a real modal is the costly
        direction.
        """
        output = load_fixture("codex_approval_modal_heavy_box.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_get_status_approval_modal_double_box(self):
        """Double-line frame glyphs are stripped as chrome too."""
        output = (
            "› run the deploy script\n"
            "╔═ Command Approval Required ═╗\n"
            "║ [a] Accept  [d] Decline    ║\n"
            "╚════════════════════════════╝\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_has_approval_modal_accepts_framed_box_with_whitespace_gutter(self):
        """A framed box indented as a whole is still a modal.

        The left-margin guard rejects a leading run of whitespace ONLY; a run
        containing frame glyphs is chrome regardless of surrounding padding, so
        indenting the box does not break detection.
        """
        assert _has_approval_modal_in_bottom(
            "    ╭─ Command Approval Required ─╮\n    │ [a] Accept  [d] Decline    │\n"
        )

    def test_has_approval_modal_accepts_unframed_modal_at_left_margin(self):
        """An unframed modal at column 0 is accepted — no indent, so no prose signal."""
        assert _has_approval_modal_in_bottom("Command Approval Required\n[a] Accept  [d] Decline\n")

    def test_has_approval_modal_requires_both_halves_at_left_margin(self):
        """One half framed and the other indented is a quote, not a box."""
        assert not _has_approval_modal_in_bottom(
            "╭─ Command Approval Required ─╮\n    [a] Accept  [d] Decline\n"
        )
        assert not _has_approval_modal_in_bottom(
            "    Command Approval Required\n│ [a] Accept  [d] Decline │\n"
        )

    def test_get_status_answered_modal_with_work_resumed_is_not_waiting(self):
        """An answered modal still in-window, with work running below it, is not WAITING.

        The box has not scrolled out yet, so guards 1-4 all pass; only the
        spinner below the choice line reveals that the modal was answered and
        execution resumed. Reporting WAITING here withholds work from a pane
        that is actively running.
        """
        output = (
            "╭─ Command Approval Required ─╮\n"
            "│ [a] Accept   [d] Decline    │\n"
            "╰─────────────────────────────╯\n"
            "• Accepted. Running deploy...\n"
            "• Working (3s • esc to interrupt)\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status != TerminalStatus.WAITING_USER_ANSWER
        assert status == TerminalStatus.PROCESSING

    def test_get_status_live_modal_without_spinner_is_still_waiting(self):
        """Control for the spinner guard: no spinner below the box means WAITING."""
        output = (
            "› run the deploy script\n"
            "• I'll run the deploy script now.\n"
            "╭─ Command Approval Required ─╮\n"
            "│ [a] Accept   [d] Decline    │\n"
            "╰─────────────────────────────╯\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_get_status_live_modal_with_stale_spinner_above_is_waiting(self):
        """A spinner in scrollback ABOVE the box must not suppress a live modal.

        Why the spinner guard is scoped to lines strictly below the choice line
        rather than the whole bottom region: with --no-alt-screen a spinner from
        earlier in the same turn can survive above the box, and a region-wide
        test would then miss a genuinely blocked pane.
        """
        output = (
            "› run the deploy script\n"
            "• Working (5s • esc to interrupt)\n"
            "• I need approval to run this.\n"
            "╭─ Command Approval Required ─╮\n"
            "│ [a] Accept   [d] Decline    │\n"
            "╰─────────────────────────────╯\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_startup_blocking_input_pattern_still_vetoes_readiness(self):
        """Splitting the startup pattern must not weaken the startup-path veto."""
        assert not _has_startup_idle_composer(
            "› Write tests for @filename\n"
            "  gpt-5.6-sol medium · Context 100% left\n"
            "╭─ Command Approval Required ─╮\n"
            "│ [a] Accept  [d] Decline     │\n"
        )

    def test_modal_taller_than_bottom_region_is_still_waiting(self):
        """A modal taller than STARTUP_PROMPT_BOTTOM_LINES must not fail open.

        The first implementation searched only the bottom 15 lines for BOTH
        halves, so a box with a long command preview pushed its header out of
        the window, dropped the corroboration guard, and returned COMPLETED —
        the exact "pane is free" misreport this class exists to prevent.
        Anchoring bottom-up on the choice line and walking up to the enclosing
        transcript cell removes the height ceiling.
        """
        preview = "".join(f"│ arg-{n:02d}={'x' * 30}   │\n" for n in range(20))
        output = (
            "› run the deploy script\n"
            "• I'll run the deploy script now.\n"
            "╭─ Command Approval Required ─╮\n" + preview + "│ [a] Accept  [d] Decline     │\n"
            "╰─────────────────────────────╯\n"
        )

        assert _has_approval_modal_in_bottom(output)

        provider = CodexProvider("test1234", "test-session", "window-0")

        assert provider.get_status(output) == TerminalStatus.WAITING_USER_ANSWER

    def test_answered_modal_above_live_modal_does_not_veto_the_live_one(self):
        """An answered modal above a live one must not suppress the live one.

        Top-down anchoring found the FIRST header and paired it with the FIRST
        choice line below it, then judged liveness from THAT box. The spinner
        left in scrollback by the first command's execution sits below the first
        choice line, so the answered box vetoed the whole detector and the live
        box below was never considered — get_status fell through to PROCESSING.
        Anchoring on the LAST choice line makes the live modal the subject.

        The surviving spinner is the same --no-alt-screen artefact that
        test_get_status_live_modal_with_stale_spinner_above_is_waiting relies on.
        """
        output = (
            "› run both deploy scripts\n"
            "╭─ Command Approval Required ─╮\n"
            "│ [a] Accept  [d] Decline     │\n"
            "╰─────────────────────────────╯\n"
            "• Accepted — running ./scripts/deploy-a.sh.\n"
            "• Working (12s • esc to interrupt)\n"
            "• deploy-a.sh finished. deploy-b.sh needs approval.\n"
            "╭─ Command Approval Required ─╮\n"
            "│ [a] Accept  [d] Decline     │\n"
            "╰─────────────────────────────╯\n"
        )

        assert _has_approval_modal_in_bottom(output)

        provider = CodexProvider("test1234", "test-session", "window-0")

        assert provider.get_status(output) == TerminalStatus.WAITING_USER_ANSWER

    def test_framed_quote_mid_reply_is_not_waiting(self):
        """A framed modal quote the reply CONTINUES past must not latch WAITING.

        Harder than the indented plain-text quote: the model reproduces the box
        glyphs too, so the left-margin guard passes (the leading run contains
        frame chrome, not just spaces) and the old detector latched
        WAITING_USER_ANSWER for as long as the reply stayed on screen — work
        withheld from an idle pane indefinitely.

        The discriminator is positional: a live modal IS the bottom of the pane,
        so only frame rows and footer chrome may follow it. Here the reply's own
        closing sentence follows, which no live modal can have below it.
        """
        output = (
            "› why did the earlier run stall?\n"
            "• The terminal showed:\n"
            "    ╭─ Command Approval Required ─╮\n"
            "    │ [a] Accept  [d] Decline     │\n"
            "    ╰─────────────────────────────╯\n"
            "  so the pane was blocked on approval.\n"
            "› \n"
            "  ? for shortcuts                     88% context left\n"
        )

        assert not _has_approval_modal_in_bottom(output)

        provider = CodexProvider("test1234", "test-session", "window-0")

        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_typed_draft_below_modal_is_not_waiting(self):
        """Text typed into the composer below the box means the box is not the bottom.

        Control for _is_chrome_only's composer case: the EMPTY composer is
        chrome, a composer holding a draft is content.
        """
        assert not _has_approval_modal_in_bottom(
            "╭─ Command Approval Required ─╮\n"
            "│ [a] Accept  [d] Decline     │\n"
            "╰─────────────────────────────╯\n"
            "› and now do the other thing\n"
        )

    def test_framed_quote_ending_a_reply_is_a_known_false_positive(self):
        """Documents the one accepted misread: a framed quote that ENDS the reply.

        With only the empty composer and status bar after it, the quote is
        positionally indistinguishable from a live modal — separating them needs
        semantics this detector does not have. Asserted rather than left
        undocumented so the behaviour is a recorded trade, not a surprise.

        Costs a spurious WAITING_USER_ANSWER (work withheld from an idle pane)
        rather than a COMPLETED (work pasted into a hard-blocked pane), which is
        the safe direction for this detector to be wrong in.
        """
        output = (
            "› why did the earlier run stall?\n"
            "• The terminal showed:\n"
            "    ╭─ Command Approval Required ─╮\n"
            "    │ [a] Accept  [d] Decline     │\n"
            "    ╰─────────────────────────────╯\n"
            "› \n"
            "  ? for shortcuts                     88% context left\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")

        assert provider.get_status(output) == TerminalStatus.WAITING_USER_ANSWER


class TestCodexProviderApprovalPromptLive:
    """Tests for the approval prompt codex-cli 0.147.0 ACTUALLY renders.

    The boxed "Command Approval Required" / "[a] Accept" modal that
    TestCodexProviderApprovalModal covers is not emitted by 0.147.0 at all --
    ``strings`` over the vendored native binary finds zero occurrences of that
    copy. The live prompt is a numbered menu (see the fixtures below), so without
    an approval check a pane hard-blocked on a real approval classified as IDLE:
    the prompt's own "› 1. Yes, proceed (y)" cursor line is simultaneously the
    last USER_PREFIX_PATTERN match and an idle-prompt match, so get_status saw a
    user message with no reply after it.

    Detection is STRUCTURAL -- the numbered menu plus its confirm footer, anchored
    bottom-up -- not a list of question titles inside a fixed-height window. The
    tests below pin the three ways the title-in-a-window approach was wrong:
    a long command preview pushed the title out of the window and failed OPEN to
    IDLE (test_live_capture_long_command_preview_is_waiting, against a real
    capture); the title list was incomplete
    (test_network_approval_prompt_is_waiting); and a reply that merely QUOTED the
    copy latched a sticky WAITING_USER_ANSWER onto a ready worker
    (test_quoted_prompt_in_completed_reply_is_not_waiting).
    """

    def test_get_status_live_capture_is_waiting(self):
        """Regression against a real captured approval prompt.

        Fixture is an unedited ``tmux capture-pane -p`` of codex-cli 0.147.0
        parked on an exec approval, produced by launching
        ``codex -a untrusted -s read-only --no-alt-screen`` and asking it to run
        ``mkdir -p``. Before APPROVAL_PROMPT_PATTERN this returned IDLE.
        """
        output = load_fixture("codex_approval_modal_raw.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")

        assert provider.get_status(output) == TerminalStatus.WAITING_USER_ANSWER

    def test_get_status_from_screen_live_capture_is_waiting(self):
        """The pyte-composited screen path must agree with the buffer path.

        supports_screen_detection is True for this provider, so the screen path
        is what StatusMonitor actually calls in production.
        """
        output = load_fixture("codex_approval_modal_raw.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")

        assert (
            provider.get_status_from_screen(output.splitlines())
            == TerminalStatus.WAITING_USER_ANSWER
        )

    def test_get_status_live_capture_edits_approval_is_waiting(self):
        """Regression against a real captured apply_patch approval.

        Second unedited capture from the same live session, parked on
        "Would you like to make the following edits?" after being asked to edit a
        file under ``-s read-only``. Corroborates that the variants share one
        prompt shape and footer rather than being three unrelated screens.
        Returns IDLE without APPROVAL_PROMPT_PATTERN, same as the exec capture.
        """
        output = load_fixture("codex_approval_edits_raw.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")

        assert provider.get_status(output) == TerminalStatus.WAITING_USER_ANSWER
        assert (
            provider.get_status_from_screen(output.splitlines())
            == TerminalStatus.WAITING_USER_ANSWER
        )

    def test_capture_pane_trailing_padding_does_not_hide_the_prompt(self):
        """Blank padding rows below the prompt must not defeat detection.

        ``tmux capture-pane`` pads to the full pane height, so a live prompt is
        followed by an arbitrary number of empty rows. Those rows are why the
        "nothing but chrome below the footer" guard has to treat blank lines as
        chrome (:func:`_is_chrome_only` does, via ``_is_frame_padding``).
        """
        prompt = (
            "  Would you like to run the following command?\n"
            "\n"
            "  Environment: local\n"
            "\n"
            "  $ mkdir -p /tmp/subdir\n"
            "\n"
            "› 1. Yes, proceed (y)\n"
            "  2. No, and tell Codex what to do differently (esc)\n"
            "\n"
            "  Press enter to confirm or esc to cancel\n"
        )

        assert _has_approval_prompt_in_bottom(prompt + "\n" * 20)

    @pytest.mark.parametrize(
        "accept_key",
        [
            "enter",
            "space",
            "tab",
            "y",
            # Modified single keys: modifiers join with " + "
            # (key_hint.rs CTRL_PREFIX et al.), so one binding is already
            # multi-token on screen.
            "ctrl + s",
            "shift + tab",
            # Two-stroke chords: RuntimeChordKeymap accepts e.g.
            # tui.keymap.list.accept="ctrl-x ctrl-s" and
            # ShortcutHint::display_label() joins the strokes with a single
            # space (haofeif round 4). A single-token match classified this
            # pane IDLE — the unsafe direction.
            "ctrl + x ctrl + s",
            # Worst legal shape: every modifier on both strokes, 14 tokens.
            "ctrl + shift + alt + x ctrl + shift + alt + s",
        ],
    )
    def test_configurable_confirm_key_still_detected(self, accept_key):
        """The accept key is configurable (``tui.keymap.list.accept``), so the
        footer can read ``Press space to confirm ...`` etc. Detection must key on
        the structural wording, not the literal "enter" — otherwise a custom
        keymap re-creates the original unsafe IDLE classification (haofeif P2).
        The label is not even one token: chords render as e.g.
        ``ctrl + x ctrl + s`` (haofeif round 4)."""
        prompt = (
            "  Would you like to run the following command?\n"
            "\n"
            "  $ rm -rf build\n"
            "\n"
            "› 1. Yes, proceed (y)\n"
            "  2. No, and tell Codex what to do differently (esc)\n"
            "\n"
            f"  Press {accept_key} to confirm or esc to cancel\n"
        )
        assert _has_approval_prompt_in_bottom(prompt + "\n" * 10)

    def test_chord_confirm_key_is_waiting_via_both_status_paths(self):
        """A two-stroke accept chord must classify WAITING through both public
        entry points, not just the private helper.

        At ``a926f89`` the footer ``Press ctrl + x ctrl + s to confirm ...``
        (the exact rendering of ``tui.keymap.list.accept="ctrl-x ctrl-s"`` at
        0.147.0) made both :meth:`get_status` and :meth:`get_status_from_screen`
        return IDLE for a hard-blocked pane, so queued input was pasted into
        the approval menu (haofeif round 4).
        """
        output = (
            "› do the thing\n"
            "• Working on it.\n"
            "  Would you like to run the following command?\n"
            "\n"
            "  $ mkdir -p /tmp/subdir\n"
            "\n"
            "› 1. Yes, proceed (y)\n"
            "  2. No, and tell Codex what to do differently (esc)\n"
            "\n"
            "  Press ctrl + x ctrl + s to confirm or esc to cancel\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")

        assert provider.get_status(output) == TerminalStatus.WAITING_USER_ANSWER
        assert (
            provider.get_status_from_screen(output.splitlines())
            == TerminalStatus.WAITING_USER_ANSWER
        )

    def test_footer_match_is_bounded_against_prose(self):
        """The multi-token key label is BOUNDED (15 tokens, the worst legal
        chord). A prose sentence that happens to contain "Press" and, much
        later on the same line, "to confirm" must not bridge the two — the
        bound is the regex-level backstop under the structural gates."""
        prose = (
            "  Press the escape key if you would instead like the assistant "
            "to stop what it is doing right now and wait for you to confirm\n"
        )
        assert not re.search(APPROVAL_PROMPT_FOOTER, prose)

    @pytest.mark.parametrize(
        ("keymap_case", "footer_replacement"),
        [
            # tui.keymap.list.accept = [] — an empty list explicitly unbinds
            # (config/src/tui_keymap.rs at rust-v0.147.0), and
            # accept_cancel_hint_line's (None, Some(cancel)) arm renders the
            # cancel hint alone (popup_consts.rs).
            ("accept_unbound_cancel_only", "  Press esc to cancel"),
            # Both list actions unbound: the (None, None) arm renders an empty
            # line — the blocking menu has NO footer at all.
            ("both_unbound_no_footer", ""),
            # Both unbound on a request with a thread label: the overlay
            # appends its own hint, so the whole footer is the thread hint
            # (approval_overlay.rs approval_footer_hint).
            ("both_unbound_thread_hint", "  Press t to open thread"),
        ],
    )
    def test_unbound_accept_keymap_is_waiting_via_both_status_paths(
        self, keymap_case, footer_replacement
    ):
        """An approval menu with the accept action UNBOUND must still classify
        WAITING through both public entry points.

        Codex 0.147.0 renders the same blocking numbered menu either with a
        cancel-only footer or with no footer line at all, so the footer cannot
        be the anchor — the menu is (haofeif round 5). At ``e6f0a57`` these
        shapes returned IDLE from both paths while the overlay stayed active,
        re-enabling delivery into a blocked pane.

        Built from the real capture with only the footer line swapped, so the
        menu/preview/question rows stay pinned to the live rendering.
        """
        raw = load_fixture("codex_approval_modal_raw.txt")
        footer_line = next(line for line in raw.splitlines() if "to confirm" in line)
        output = raw.replace(footer_line, footer_replacement)
        assert output != raw

        provider = CodexProvider("test1234", "test-session", "window-0")

        assert _has_approval_prompt_in_bottom(output)
        assert provider.get_status(output) == TerminalStatus.WAITING_USER_ANSWER
        assert (
            provider.get_status_from_screen(output.splitlines())
            == TerminalStatus.WAITING_USER_ANSWER
        )

    def test_quoted_menu_without_footer_is_still_rejected(self):
        """Making the footer optional must not admit a menu the model QUOTED.

        The discriminator is the column-0 cursor: quoted or continuation prose
        is indented under its bullet, so a pasted menu carries its cursor at
        column >= 2 and finds no anchor even now that no footer is required.
        """
        output = (
            "› what did the approval look like?\n"
            "• It showed this menu:\n"
            "  › 1. Yes, proceed (y)\n"
            "    2. No, and tell Codex what to do differently (esc)\n"
            "› \n"
            "  ? for shortcuts                     88% context left\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")

        assert not _has_approval_prompt_in_bottom(output)
        assert provider.get_status(output) != TerminalStatus.WAITING_USER_ANSWER

    def test_menu_with_reply_below_is_rejected_even_without_footer(self):
        """A scrolled-out menu followed by reply content must stay rejected:
        the menu-block scan below the anchor refuses anything that is not an
        option row, a footer hint, or chrome."""
        output = (
            "› 1. Yes, proceed (y)\n"
            "  2. No, and tell Codex what to do differently (esc)\n"
            "• proceeding with the command.\n"
            "› \n"
            "  ? for shortcuts                     88% context left\n"
        )

        assert not _has_approval_prompt_in_bottom(output)

    def test_single_numbered_line_at_bottom_is_not_a_menu(self):
        """A lone column-0 numbered line over the idle composer is NOT a menu.

        APPROVAL_MENU_MIN_OPTIONS is the floor: a genuine approval always
        offers at least accept and decline, so a single-item shape — most
        commonly a user message that happens to start with "1." — must not
        anchor. This is also what keeps the disclosed numbered-list residual
        (below) confined to MULTI-line user lists.
        """
        output = (
            "› 1. run the tests\n" "› \n" "  ? for shortcuts                     88% context left\n"
        )

        assert not _has_approval_prompt_in_bottom(output)

    # The real capture's 108-column option and the tagged renderer's width-100
    # wrapping of it: ListSelectionView wraps rows by default
    # (SelectionRowDisplay::Wrapped) and word_wrap_line indents each
    # continuation to the option text column — 5 columns for a single-digit
    # menu (haofeif round 6).
    _UNWRAPPED_OPTION = (
        "  2. Yes, and don't ask again for commands that start with "
        "`mkdir -p /private/tmp/codex-work-567/subdir` (p)"
    )
    _WRAPPED_OPTION = (
        "  2. Yes, and don't ask again for commands that start with `mkdir -p\n"
        "     /private/tmp/codex-work-567/subdir` (p)"
    )

    def test_wrapped_option_rows_are_waiting_via_both_status_paths(self):
        """A menu whose long option WRAPPED at a narrow pane width must still
        classify WAITING through both public entry points.

        CAO's panes shrink when a narrower terminal attaches and the screen
        path is the production path, so at ``9418e65`` the width-100 rendering
        of this PR's own capture returned IDLE from ``get_status_from_screen``
        while the menu was live (haofeif round 6).
        """
        raw = load_fixture("codex_approval_modal_raw.txt")
        output = raw.replace(self._UNWRAPPED_OPTION, self._WRAPPED_OPTION)
        assert output != raw, "fixture no longer contains the 108-column option"

        provider = CodexProvider("test1234", "test-session", "window-0")

        assert _has_approval_prompt_in_bottom(output)
        assert provider.get_status(output) == TerminalStatus.WAITING_USER_ANSWER
        assert (
            provider.get_status_from_screen(output.splitlines())
            == TerminalStatus.WAITING_USER_ANSWER
        )

    def test_wrapped_anchor_row_is_still_the_anchor(self):
        """The CURSOR row itself can be the one that wraps — the continuation
        sits between the anchor and the next option and must not break the
        below-anchor scan."""
        raw = load_fixture("codex_approval_modal_raw.txt")
        output = raw.replace(self._UNWRAPPED_OPTION, self._WRAPPED_OPTION)
        # move the cursor from option 1 onto the wrapped option 2
        output = output.replace("› 1. Yes, proceed (y)", "  1. Yes, proceed (y)")
        output = output.replace("  2. Yes, and don't ask", "› 2. Yes, and don't ask")
        assert "› 2." in output and "› 1." not in output

        assert _has_approval_prompt_in_bottom(output)

    @pytest.mark.parametrize(
        "tail",
        [
            # after the footer: no option is open, so option-column indent is
            # foreign content again
            "  Press enter to confirm or esc to cancel\n     stray indented prose",
            # after blank filler: same — wrapping never crosses a blank row
            "  3. No, and tell Codex what to do differently (esc)\n\n     stray indented prose",
        ],
    )
    def test_indented_prose_outside_an_option_still_disqualifies(self, tail):
        """Continuation rows are honoured only while INSIDE an option: indented
        prose after the footer or after a blank row still proves the menu is
        not the bottom of the pane."""
        raw = load_fixture("codex_approval_modal_raw.txt")
        needle = tail.splitlines()[0]
        output = raw.replace(needle, tail)
        assert output != raw

        assert not _has_approval_prompt_in_bottom(output)

    def test_two_column_indent_below_an_option_is_not_wrapping(self):
        """The continuation floor is the option TEXT column (5), not the
        2-column indent of the question/footer/ordinary prose.

        The renderer wraps to the width of the "{prefix} {n}. " gutter, so a
        2-column-indented line directly under an option is foreign content and
        must disqualify the block. This is the boundary that keeps the
        numbered-list residual (below) from swallowing a user list that
        continues with ordinary indented prose.
        """
        output = (
            "› 1. run the tests\n"
            "  2. fix whatever fails\n"
            "  then rerun them until green\n"
            "› \n"
            "  ? for shortcuts                     88% context left\n"
        )

        assert not _has_approval_prompt_in_bottom(output)

    def test_user_numbered_list_at_bottom_is_the_disclosed_residual(self):
        """DOCUMENTED RESIDUAL, asserted so a change is a conscious decision.

        A user message that is itself a numbered list renders with the same
        column-0 gutter marker as a live menu cursor, so parked at the bottom
        of an otherwise idle pane (codex interrupted before replying) it now
        reads WAITING rather than IDLE. That errs toward withholding work —
        never toward pasting into a blocked pane — and clears as soon as codex
        renders any activity below the cell (the case above). The
        footer-anchored detector rejected this shape only by failing open to
        IDLE on every unbound-keymap approval, the dangerous direction.
        """
        output = (
            "› 1. run the tests\n"
            "  2. fix whatever fails\n"
            "› \n"
            "  ? for shortcuts                     88% context left\n"
        )

        assert _has_approval_prompt_in_bottom(output)

    @pytest.mark.parametrize(
        "question",
        [
            "Would you like to run the following command?",
            "Would you like to make the following edits?",
            "Would you like to grant these permissions?",
        ],
    )
    def test_all_three_approval_variants_are_waiting(self, question):
        """exec, apply_patch, and permission-escalation approvals all block the TUI.

        All three strings are present in the 0.147.0 binary and all three park
        the pane on the same numbered menu.
        """
        output = (
            "› do the thing\n"
            "• Working on it.\n"
            f"  {question}\n"
            "\n"
            "› 1. Yes, proceed (y)\n"
            "  2. No, and tell Codex what to do differently (esc)\n"
            "\n"
            "  Press enter to confirm or esc to cancel\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")

        assert provider.get_status(output) == TerminalStatus.WAITING_USER_ANSWER

    def test_question_without_footer_is_not_waiting(self):
        """Corroboration guard: the question alone does not classify as WAITING."""
        assert not _has_approval_prompt_in_bottom(
            "› why did it stall?\n"
            "• Codex asked 'Would you like to run the following command?' and waited.\n"
            "› \n"
            "  ? for shortcuts                     88% context left\n"
        )

    def test_footer_without_question_is_not_waiting(self):
        """Corroboration guard: the footer alone does not classify as WAITING.

        "Press enter to confirm" also appears under non-approval prompts, so on
        its own it is not evidence of an approval.
        """
        assert not _has_approval_prompt_in_bottom(
            "  Name this session\n  Press enter to confirm or esc to cancel\n"
        )

    def test_answered_prompt_scrolled_out_is_not_waiting(self):
        """Once the prompt scrolls out of the region it must stop latching."""
        output = (
            "  Would you like to run the following command?\n"
            "  $ mkdir -p /tmp/subdir\n"
            "› 1. Yes, proceed (y)\n"
            "  Press enter to confirm or esc to cancel\n"
            + "".join(f"• step {n} done.\n" for n in range(16))
            + "› \n"
            "  ? for shortcuts                     88% context left\n"
        )

        assert not _has_approval_prompt_in_bottom(output)

        provider = CodexProvider("test1234", "test-session", "window-0")

        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_live_capture_long_command_preview_is_waiting(self):
        """A long command preview must not push the prompt out of detection.

        Fixture is an unedited ``tmux capture-pane -p`` of codex-cli 0.147.0
        parked on an exec approval whose command is a 12-line heredoc, produced
        the same way as the two captures above (``codex -a untrusted -s read-only
        --no-alt-screen``) by asking it to write a script with a single
        ``bash -lc`` heredoc. The renderer does NOT truncate the preview.

        This is the P1 fail-open case, and it is real rather than constructed:
        in this capture the question lands on non-blank row 16 counted from the
        bottom, one row outside the 15-row window the title-based detector used,
        so that detector found no title and returned IDLE for a pane hard-blocked
        on a keystroke. IDLE is the dangerous direction -- it invites the
        conductor to send more work into a dead pane.
        """
        output = load_fixture("codex_approval_long_preview_raw.txt")

        # The premise of the regression: the question really is outside a 15-row
        # window of non-blank rows, so this fixture cannot pass by accident.
        rows = [line for line in output.splitlines() if line.strip()]
        assert not any("Would you like to" in line for line in rows[-15:])

        provider = CodexProvider("test1234", "test-session", "window-0")

        assert provider.get_status(output) == TerminalStatus.WAITING_USER_ANSWER
        assert (
            provider.get_status_from_screen(output.splitlines())
            == TerminalStatus.WAITING_USER_ANSWER
        )

    def test_network_approval_prompt_is_waiting(self):
        """The network-access approval is a fourth title on the same menu.

        SYNTHETIC, not a capture: this prompt only fires with
        ``features.network_proxy=true`` AND reachable DNS, and the sandbox the
        other fixtures were captured in has no network. The copy is not invented
        though -- the title and every option string below appear verbatim in the
        0.147.0 native binary's string table alongside the three titles the old
        detector enumerated, which is the point: the title list was a list of the
        variants someone had happened to see, and could not be completed.

        Note this menu carries no ``(y)``/``(esc)`` key hints, so it also
        demonstrates that detection does not depend on those.
        """
        output = (
            "› fetch the changelog from example.com\n"
            "\n"
            "• Fetching https://example.com/CHANGELOG.md\n"
            "\n"
            '  Do you want to approve network access to "example.com"?\n'
            "\n"
            "› 1. Yes, just this once\n"
            "  2. Yes, and allow this host for this conversation\n"
            "  3. Yes, and allow this host in the future\n"
            "  4. No, and block this host in the future\n"
            "\n"
            "  Press enter to confirm or esc to cancel\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")

        assert provider.get_status(output) == TerminalStatus.WAITING_USER_ANSWER
        assert (
            provider.get_status_from_screen(output.splitlines())
            == TerminalStatus.WAITING_USER_ANSWER
        )

    def test_detector_does_not_require_a_known_title(self):
        """An unrecognized title over the same menu must still classify as WAITING.

        The whole point of anchoring on structure: a title nobody has catalogued
        yet (0.147.0 also ships "Approve app tool call?", and future releases will
        ship more) still blocks the pane, so it must still be detected.
        """
        output = (
            "› do the thing\n"
            "• Working on it.\n"
            "  Some approval question nobody has catalogued yet?\n"
            "\n"
            "› 1. Yes, proceed (y)\n"
            "  2. No, and tell Codex what to do differently (esc)\n"
            "\n"
            "  Press enter to confirm or esc to cancel\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")

        assert provider.get_status(output) == TerminalStatus.WAITING_USER_ANSWER

    def test_quoted_prompt_in_completed_reply_is_not_waiting(self):
        """A reply that QUOTES the prompt must not latch WAITING_USER_ANSWER.

        This is the P2 quoted-latch case, and the failure it caused is worse than
        a one-off misread: WAITING_USER_ANSWER is sticky, and CodexProvider sets
        ``blocks_orchestrated_input_while_waiting_user_answer``, so a finished
        worker whose summary happened to quote both the question and the footer
        would be held out of rotation with orchestrated delivery blocked.

        Rejected by the "nothing but chrome below the footer" guard: the reply
        continues below the quote and ends at a live composer.
        """
        output = (
            "› summarize what PR #567 changes\n"
            "\n"
            "• PR #567 teaches the Codex provider to recognize the runtime approval\n"
            "  prompt. Codex 0.147.0 renders it as a numbered menu:\n"
            "\n"
            "    Would you like to run the following command?\n"
            "    Environment: local\n"
            "    $ mkdir -p /tmp/subdir\n"
            "  › 1. Yes, proceed (y)\n"
            "    2. Yes, and don't ask again (p)\n"
            "    3. No, and tell Codex what to do differently (esc)\n"
            "    Press enter to confirm or esc to cancel\n"
            "\n"
            "  Before the fix get_status returned IDLE for that screen.\n"
            "\n"
            "›\n"
            "  openai.gpt-5.6-sol high · ~/wt\n"
        )

        assert not _has_approval_prompt_in_bottom(output)

        provider = CodexProvider("test1234", "test-session", "window-0")

        assert provider.get_status(output) == TerminalStatus.COMPLETED
        assert provider.get_status_from_screen(output.splitlines()) == TerminalStatus.COMPLETED

    def test_quoted_prompt_ending_a_reply_is_not_waiting(self):
        """The harder quoted variant: the quote is the LAST thing in the reply.

        With only the empty composer and status bar below it, the chrome guard
        cannot help -- this is exactly the residual the boxed-modal detector
        documents as a known false positive
        (test_framed_quote_ending_a_reply_is_a_known_false_positive). The numbered
        menu closes it on position instead: Codex draws the live prompt's
        selection cursor flush at column 0, while a quote indented under its
        bullet carries the cursor at column >= 2.
        """
        output = (
            "› what did the approval prompt look like?\n"
            "\n"
            "• It renders like this:\n"
            "\n"
            "    Would you like to run the following command?\n"
            "    $ mkdir -p /tmp/subdir\n"
            "  › 1. Yes, proceed (y)\n"
            "    2. No, and tell Codex what to do differently (esc)\n"
            "    Press enter to confirm or esc to cancel\n"
            "\n"
            "›\n"
            "  openai.gpt-5.6-sol high · ~/wt\n"
        )

        assert not _has_approval_prompt_in_bottom(output)

        provider = CodexProvider("test1234", "test-session", "window-0")

        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_answered_prompt_with_work_resumed_is_not_waiting(self):
        """Once answered, the prompt must stop latching even though it stays in the buffer.

        The rendered screen drops the prompt entirely on answer -- there is no
        footer left to anchor on -- but the pipe-pane buffer get_status() also
        reads is append-only and keeps the frame. The resumption copy below is
        verbatim from a live 0.147.0 pane observed immediately after pressing
        enter on the curl approval.
        """
        output = (
            "› Fetch https://example.com/ with curl right now.\n"
            "\n"
            "• Running curl https://example.com/\n"
            "\n"
            "  Would you like to run the following command?\n"
            "\n"
            "  $ curl https://example.com/\n"
            "\n"
            "› 1. Yes, proceed (y)\n"
            "  2. No, and tell Codex what to do differently (esc)\n"
            "\n"
            "  Press enter to confirm or esc to cancel\n"
            "\n"
            "✔ You approved codex to run curl https://example.com/ this time\n"
            "\n"
            "• Ran curl https://example.com/\n"
            "  └ (output elided)\n"
            "\n"
            "• Fetched the page.\n"
            "\n"
            "›\n"
            "  openai.gpt-5.6-sol high · ~/wt\n"
        )

        assert not _has_approval_prompt_in_bottom(output)

        provider = CodexProvider("test1234", "test-session", "window-0")

        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_menu_without_the_selection_cursor_is_not_waiting(self):
        """Options and a footer with no cursor anywhere are not a live menu.

        Codex always draws the cursor on one option, so its absence means this is
        a rendering of a menu rather than a menu.
        """
        assert not _has_approval_prompt_in_bottom(
            "› do the thing\n"
            "• Working on it.\n"
            "  Would you like to run the following command?\n"
            "  1. Yes, proceed (y)\n"
            "  2. No, and tell Codex what to do differently (esc)\n"
            "  Press enter to confirm or esc to cancel\n"
        )

    def test_single_option_is_not_a_menu(self):
        """One option is not an approval -- an approval can always be declined."""
        assert not _has_approval_prompt_in_bottom(
            "› do the thing\n"
            "• Working on it.\n"
            "› 1. Acknowledged\n"
            "  Press enter to confirm or esc to cancel\n"
        )

    def test_trust_dialog_is_not_matched_by_the_approval_footer(self):
        """The startup trust dialog is a numbered menu too, but a different footer.

        Copy taken from a live 0.147.0 startup screen. It ends "Press enter to
        continue", not "...to confirm", so the approval detector must not claim
        it -- TRUST_PROMPT_PATTERN_V2 owns it, and get_status still reports
        WAITING_USER_ANSWER through that path.
        """
        output = (
            "  Welcome to Codex, OpenAI's command-line coding agent\n"
            "\n"
            "> You are in /private/tmp/codex-cap-567\n"
            "\n"
            "  Do you trust the contents of this directory? Working with untrusted\n"
            "  contents comes with higher risk of prompt injection.\n"
            "\n"
            "› 1. Yes, continue\n"
            "  2. No, quit\n"
            "\n"
            "  Press enter to continue\n"
        )

        assert not _has_approval_prompt_in_bottom(output)

        provider = CodexProvider("test1234", "test-session", "window-0")

        assert provider.get_status(output) == TerminalStatus.WAITING_USER_ANSWER

    def test_startup_path_vetoes_readiness_on_the_live_prompt(self):
        """The startup readiness veto must know the copy Codex actually emits.

        STARTUP_BLOCKING_INPUT_PATTERN only carried the legacy modal's copy, so
        a pane parked on a real approval during initialize() could be read as an
        idle composer and declared ready.
        """
        assert not _has_startup_idle_composer(
            "› Write tests for @filename\n"
            "  gpt-5.6-sol medium · Context 100% left\n"
            "  Would you like to run the following command?\n"
            "› 1. Yes, proceed (y)\n"
            "  Press enter to confirm or esc to cancel\n"
        )

    def test_startup_path_vetoes_readiness_on_the_network_prompt(self):
        """The startup veto must cover the network title too.

        The footer alone already vetoes here, but the title is folded into
        APPROVAL_PROMPT_PATTERN as well: that pattern's only remaining job is this
        permissive negative gate, where an extra match merely keeps the startup
        poll running and so costs nothing.
        """
        assert not _has_startup_idle_composer(
            "› Write tests for @filename\n"
            "  gpt-5.6-sol medium · Context 100% left\n"
            '  Do you want to approve network access to "example.com"?\n'
            "› 1. Yes, just this once\n"
        )


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


class TestCodexProviderBlocksOrchestratedInputWhileWaitingUserAnswer:
    """PR #540 follow-up (raised during the round-2 review pass): CodexProvider must
    opt into `blocks_orchestrated_input_while_waiting_user_answer` -- otherwise, now
    that `initialize()` accepts WAITING_USER_ANSWER (the first-run login menu) as a
    success outcome, an assign/handoff's deferred-init `send_input` would paste the
    orchestrated task straight into the live login menu instead of being held for
    `answer_user_prompt`. Same hazard PR #539's review flagged for ClaudeCodeProvider,
    fixed here the same way (a property override matching hermes.py/antigravity_cli.py).
    """

    def test_property_is_true(self):
        provider = CodexProvider("test1234", "test-session", "window-0", None)
        assert provider.blocks_orchestrated_input_while_waiting_user_answer is True

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service._notify_caller_of_deferred_failure")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    async def test_waiting_on_login_menu_leaves_worker_alive_task_undelivered(
        self, mock_tmux, mock_pm, mock_status_monitor, mock_meta, mock_notify
    ):
        """RED (pre-fix, property False): send_input's guard no-ops, the task text
        is pasted into the live login menu via `send_keys`, and the deferred-init
        path treats delivery as having succeeded -- `_notify_caller_of_deferred_failure`
        is never called at all, so this test's own assertion of a undelivered/alive
        worker fails outright (no call to assert on).
        GREEN (post-fix, property True): `send_input` raises `TerminalInputBlockedError`
        before any `send_keys` call, `_schedule_deferred_init` catches it and leaves the
        worker alive (`delete_worker=False`) with nothing pasted.
        """
        from cli_agent_orchestrator.services.terminal_service import (
            _deferred_init_tasks,
            _schedule_deferred_init,
        )

        mock_meta.return_value = {
            "caller_id": "super123",
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        mock_status_monitor.get_status.return_value = TerminalStatus.WAITING_USER_ANSWER
        # Real provider instance (not a generic mock) so its actual
        # blocks_orchestrated_input_while_waiting_user_answer property value is
        # what send_input's guard consults -- this is the thing under test.
        real_provider = CodexProvider("worker99", "cao-session", "developer-abcd")
        mock_pm.get_provider.return_value = real_provider

        provider_instance = AsyncMock()
        provider_instance.initialize.return_value = True  # succeeded: WAITING_USER_ANSWER reached
        provider_instance.shell_baseline = None

        before_tasks = set(_deferred_init_tasks)
        _schedule_deferred_init(
            provider_instance, "worker99", "do the task", OrchestrationType.ASSIGN, None
        )
        (task,) = set(_deferred_init_tasks) - before_tasks
        await task

        mock_tmux.send_keys.assert_not_called()
        mock_notify.assert_called_once()
        assert mock_notify.call_args.kwargs["delete_worker"] is False
