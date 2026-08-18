"""Unit tests for the OpenCode CLI provider."""

import json
import re
import shlex
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import cli_agent_orchestrator.providers.opencode_cli as opencode_provider_module
import cli_agent_orchestrator.utils.opencode_config as opencode_config_module
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.opencode_cli import (
    ANSI_CODE_PATTERN,
    COMPLETION_MARKER_PATTERN,
    IDLE_FOOTER_PATTERN,
    PERMISSION_PROMPT_PATTERN,
    PROCESSING_FOOTER_PATTERN,
    TOOL_CALL_IN_FLIGHT_PATTERN,
    USER_MESSAGE_PATTERN,
    OpenCodeCliProvider,
)
from cli_agent_orchestrator.utils.mcp_resolution import CAO_MCP_SERVER_MODULE

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    """Load a plain-text fixture file."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def load_ansi_fixture(name: str) -> str:
    """Load an ANSI-encoded fixture file."""
    return (FIXTURES_DIR / name).read_bytes().decode("utf-8", errors="replace")


def make_provider(
    agent_profile: str = "developer",
    model: str | None = None,
    terminal_id: str = "test-tid",
) -> OpenCodeCliProvider:
    return OpenCodeCliProvider(
        terminal_id=terminal_id,
        session_name="test-session",
        window_name="window-0",
        agent_profile=agent_profile,
        model=model,
    )


# ---------------------------------------------------------------------------
# (g) Regex patterns individually match expected substrings
# ---------------------------------------------------------------------------


class TestRegexPatterns:
    def test_user_message_pattern_matches_bar_indent(self):
        assert re.search(USER_MESSAGE_PATTERN, "┃  some user message", re.MULTILINE)

    def test_user_message_pattern_rejects_plain_text(self):
        assert not re.search(USER_MESSAGE_PATTERN, "  some agent message", re.MULTILINE)

    def test_completion_marker_pattern_matches_full_marker(self):
        assert re.search(COMPLETION_MARKER_PATTERN, "▣  Build · Big Pickle · 7.2s")

    def test_completion_marker_pattern_matches_minute_second_duration(self):
        # Responses > 60 s are formatted as "Nm Ns" by OpenCode.
        assert re.search(COMPLETION_MARKER_PATTERN, "▣  Data_analyst · Big Pickle · 1m 8s")
        assert re.search(COMPLETION_MARKER_PATTERN, "▣  Data_analyst · Big Pickle · 2m 30.5s")

    def test_completion_marker_pattern_rejects_incomplete(self):
        # No duration suffix → not a full completion marker
        assert not re.search(COMPLETION_MARKER_PATTERN, "▣  Build · Big Pickle")

    def test_processing_footer_pattern_matches_esc_interrupt(self):
        assert re.search(PROCESSING_FOOTER_PATTERN, "⬝⬝⬝⬝  esc interrupt   ctrl+p commands")

    def test_idle_footer_pattern_matches_ctrl_p(self):
        assert re.search(IDLE_FOOTER_PATTERN, "tab agents  ctrl+p commands  • OpenCode")

    def test_permission_prompt_matches_permission_required(self):
        assert re.search(PERMISSION_PROMPT_PATTERN, "△  Permission required")

    def test_permission_prompt_matches_always_allow(self):
        assert re.search(PERMISSION_PROMPT_PATTERN, "△  Always allow")

    def test_tool_call_in_flight_matches_braille_spinner(self):
        assert re.search(TOOL_CALL_IN_FLIGHT_PATTERN, "  ⠋ Read /etc/hostname", re.MULTILINE)

    def test_ansi_code_pattern_strips_truecolor(self):
        # 24-bit truecolor sequence — must be stripped
        seq = "\x1b[38;2;255;100;50mHello\x1b[0m"
        clean = re.sub(ANSI_CODE_PATTERN, "", seq)
        assert clean == "Hello"


# ---------------------------------------------------------------------------
# (a) + (b)  get_status() per fixture
# ---------------------------------------------------------------------------


class TestGetStatusFromFixtures:
    """Verify get_status() returns the correct enum for each Phase 1 fixture.

    Merged event-driven contract: get_status(output) receives the StatusMonitor
    buffer STRING directly (providers no longer read tmux internally), so each
    test passes the fixture contents to get_status(...).
    """

    def test_idle_splash_returns_idle(self):
        output = load_fixture("opencode_cli_idle_splash.txt")
        provider = make_provider()
        assert provider.get_status(output) == TerminalStatus.IDLE

    def test_idle_splash_ansi_returns_idle(self):
        output = load_ansi_fixture("opencode_cli_idle_splash.ansi.txt")
        provider = make_provider()
        assert provider.get_status(output) == TerminalStatus.IDLE

    def test_processing_returns_processing(self):
        output = load_fixture("opencode_cli_processing.txt")
        provider = make_provider()
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_processing_ansi_returns_processing(self):
        output = load_ansi_fixture("opencode_cli_processing.ansi.txt")
        provider = make_provider()
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_completed_returns_completed(self):
        output = load_fixture("opencode_cli_completed.txt")
        provider = make_provider()
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_completed_ansi_returns_completed(self):
        output = load_ansi_fixture("opencode_cli_completed.ansi.txt")
        provider = make_provider()
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_permission_ansi_returns_waiting_user_answer(self):
        # ANSI fixture required: plain text loses the △ Permission required overlay.
        output = load_ansi_fixture("opencode_cli_permission.ansi.txt")
        provider = make_provider()
        assert provider.get_status(output) == TerminalStatus.WAITING_USER_ANSWER

    def test_idle_post_completion_returns_idle(self):
        # Use plain fixture — ANSI variant reuses the completed frame (see OPENCODE_FIXTURES.md).
        output = load_fixture("opencode_cli_idle_post_completion.txt")
        provider = make_provider()
        assert provider.get_status(output) == TerminalStatus.IDLE

    def test_empty_output_returns_unknown(self):
        # native=None always falls through (no dispatch-timing guess); on tmux
        # the live-read fallback is a pass-through, so an empty buffer hits
        # OpenCode's own no-output default directly.
        provider = make_provider()
        assert provider.get_status("") == TerminalStatus.UNKNOWN

    def test_none_output_returns_unknown(self):
        provider = make_provider()
        assert provider.get_status(None) == TerminalStatus.UNKNOWN

    def test_unknown_output_returns_unknown_fallback(self):
        """Non-empty output with no recognizable pattern → UNKNOWN fallback.

        Exercises the final ``return TerminalStatus.UNKNOWN`` at the end of
        get_status(), distinct from the early return on empty/None output.
        """
        provider = make_provider()
        assert provider.get_status("random text without any tui markers") == TerminalStatus.UNKNOWN


class TestGetStatusFromScreen:
    """Pin OpenCode's pyte-rendered screen detector.

    The screen path receives escape-free viewport rows, so it should classify the
    same user-visible TUI markers without relying on the rolling raw buffer.
    """

    def test_idle_footer_returns_idle(self):
        provider = make_provider()

        assert provider.get_status_from_screen(["", "tab agents  ctrl+p commands  • OpenCode"]) == (
            TerminalStatus.IDLE
        )

    def test_processing_footer_returns_processing(self):
        provider = make_provider()

        assert provider.get_status_from_screen(["⬝⬝⬝⬝  esc interrupt   ctrl+p commands"]) == (
            TerminalStatus.PROCESSING
        )

    def test_stale_processing_footer_followed_by_idle_returns_idle(self):
        provider = make_provider()

        assert (
            provider.get_status_from_screen(
                [
                    "⬝⬝⬝⬝  esc interrupt",
                    "",
                    "tab agents  ctrl+p commands  • OpenCode",
                ]
            )
            == TerminalStatus.IDLE
        )

    def test_stale_processing_footer_followed_by_completion_returns_completed(self):
        provider = make_provider()

        assert (
            provider.get_status_from_screen(
                [
                    "⬝⬝⬝⬝  esc interrupt",
                    "Hello from OpenCode",
                    "▣  Build · Big Pickle · 7.2s",
                    "tab agents  ctrl+p commands  • OpenCode",
                ]
            )
            == TerminalStatus.COMPLETED
        )

    def test_completion_marker_followed_by_idle_returns_completed(self):
        provider = make_provider()

        assert (
            provider.get_status_from_screen(
                [
                    "Hello from OpenCode",
                    "▣  Build · Big Pickle · 7.2s",
                    "tab agents  ctrl+p commands  • OpenCode",
                ]
            )
            == TerminalStatus.COMPLETED
        )

    def test_permission_prompt_without_idle_returns_waiting_user_answer(self):
        provider = make_provider()

        assert provider.get_status_from_screen(["△  Permission required", "Allow command?"]) == (
            TerminalStatus.WAITING_USER_ANSWER
        )

    def test_blank_screen_returns_unknown(self):
        provider = make_provider()

        assert provider.get_status_from_screen(["", "   "]) == TerminalStatus.UNKNOWN

    def test_unrecognized_nonblank_screen_returns_unknown(self):
        provider = make_provider()

        assert provider.get_status_from_screen(["OpenCode is repainting"]) == TerminalStatus.UNKNOWN


# ---------------------------------------------------------------------------
# (c)  Stale ``esc interrupt`` + idle footer on later line → IDLE
# ---------------------------------------------------------------------------


class TestStaleEscInterruptGuard:
    """Verify position-aware guard: esc interrupt on earlier line + idle footer on later line → IDLE.

    Merged contract: get_status receives the buffer string directly.
    """

    def test_stale_esc_interrupt_returns_idle(self):
        # Synthetic: esc interrupt on line 0 (old frame), ctrl+p commands on line 2 (new frame).
        stale_output = (
            "   ⬝⬝⬝⬝  esc interrupt\n"
            "\n"
            "                  ctrl+p commands    • OpenCode 1.14.19"
        )
        provider = make_provider()
        assert provider.get_status(stale_output) == TerminalStatus.IDLE

    def test_same_line_esc_and_idle_returns_processing(self):
        # esc interrupt and ctrl+p commands on the same footer line → PROCESSING (normal case).
        same_line_output = (
            "   some content here\n"
            "   ⬝⬝⬝⬝  esc interrupt                     tab agents  ctrl+p commands    • OpenCode"
        )
        provider = make_provider()
        assert provider.get_status(same_line_output) == TerminalStatus.PROCESSING

    def test_stale_esc_followed_by_full_completion_returns_completed(self):
        # Stale esc interrupt line, then full completion marker + idle footer → COMPLETED.
        stale_to_completed = (
            "   ⬝⬝⬝⬝  esc interrupt\n"
            "\n"
            "   Hello there, friend!\n"
            "\n"
            "   ▣  Build · Big Pickle · 7.2s\n"
            "\n"
            "                  ctrl+p commands    • OpenCode 1.14.19"
        )
        provider = make_provider()
        assert provider.get_status(stale_to_completed) == TerminalStatus.COMPLETED


# ---------------------------------------------------------------------------
# (d)  extract_last_message_from_script()
# ---------------------------------------------------------------------------


class TestExtractLastMessage:
    """Verify message extraction from the completed fixture and synthetic output."""

    def test_extracts_response_from_completed_fixture(self):
        provider = make_provider()
        output = load_fixture("opencode_cli_completed.txt")
        result = provider.extract_last_message_from_script(output)
        assert "Hello" in result

    def test_strips_thinking_preamble(self):
        provider = make_provider()
        # Synthetic: Thinking: block before the actual response
        output = (
            "┃  say hello\n"
            "┃\n"
            "\n"
            "┃  Thinking: The user wants a greeting. Simple.\n"
            "\n"
            "     Hi there!\n"
            "\n"
            "     ▣  Build · Big Pickle · 3.1s\n"
        )
        result = provider.extract_last_message_from_script(output)
        assert "Thinking:" not in result
        assert "Hi there!" in result

    def test_raises_if_no_completion_marker(self):
        provider = make_provider()
        # No completion marker at all → first thing checked after ANSI strip.
        with pytest.raises(ValueError, match="No completion marker"):
            provider.extract_last_message_from_script("some random output without any markers")

    def test_raises_if_no_user_message_before_completion(self):
        provider = make_provider()
        # Completion marker present but no ┃  before it.
        output = "     ▣  Build · Big Pickle · 3.1s\n"
        with pytest.raises(ValueError, match="No user message"):
            provider.extract_last_message_from_script(output)

    def test_fallback_extracts_when_user_message_scrolled_off(self):
        provider = make_provider()
        # Simulate the 41-line TUI viewport where the user message has scrolled off the top.
        # Only the tail of the agent's response is visible, plus the completion marker and
        # the input box (┃ lines) below it. No ┃  appears before the completion marker.
        output = (
            "\n"
            "➜  cli-agent-orchestrator\n"  # shell prompt from 2-line tmux scrollback
            "     Response line 1.\n"
            "     Response line 2.\n"
            "\n"
            "     ▣  Developer · Big Pickle · 8s\n"
            "\n"
            "  ┃\n"
            "  ┃  Developer · Big Pickle OpenCode Zen\n"
        )
        result = provider.extract_last_message_from_script(output)
        assert "Response line 1." in result
        assert "Response line 2." in result
        # Shell prompt must not leak into extracted content
        assert "cli-agent-orchestrator" not in result

    def test_strips_ansi_before_extraction(self):
        provider = make_provider()
        ansi_output = load_ansi_fixture("opencode_cli_completed.ansi.txt")
        result = provider.extract_last_message_from_script(ansi_output)
        # Should not contain raw ANSI sequences
        assert "\x1b[" not in result
        assert result.strip()

    def test_extract_skips_residual_bar_lines(self):
        """Residual ┃ lines inside raw_response are stripped by the agent-block loop.

        Constructs a raw_response slice whose first ``┃\\s{2}`` match anchors at the
        top but contains a bare ┃ line (followed by non-whitespace so the regex
        doesn't consume it) and a trailing blank line before the agent response.
        Exercises both the ``^\\s*┃`` branch and the ``not past_user_block and not
        line.strip()`` branch of the extraction loop.
        """
        provider = make_provider()
        output = (
            "┃  user msg\n"
            "  ┃Xstray-residual\n"  # residual ┃ line; does not match ┃\\s{2}
            "\n"  # blank after ┃-strip → past_user_block=False branch
            "     agent reply here\n"
            "\n"
            "     ▣  Build · Big Pickle · 3.1s\n"
        )
        result = provider.extract_last_message_from_script(output)
        assert "agent reply here" in result
        assert "stray-residual" not in result

    def test_extract_raises_when_response_is_empty(self):
        """Empty content between user bar and ▣ → ValueError.

        Exercises the ``if not result:`` guard after dedent/strip.
        """
        provider = make_provider()
        # User bar with nothing after it before the ▣ marker.
        output = "┃  \n     ▣  Build · Big Pickle · 3.1s\n"
        with pytest.raises(ValueError, match="Empty OpenCode response"):
            provider.extract_last_message_from_script(output)


# ---------------------------------------------------------------------------
# (e)  initialize() calls wait_until_status with timeout=120.0
# ---------------------------------------------------------------------------


class TestInitialize:
    @pytest.fixture(autouse=True)
    def stub_private_config(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """Keep mocked provider lifecycle tests away from shared OpenCode state."""
        monkeypatch.setattr(
            opencode_provider_module,
            "prepare_opencode_runtime_config",
            lambda _terminal_id: tmp_path / "opencode-runtime",
            raising=False,
        )

    @pytest.mark.asyncio
    async def test_initialize_uses_private_config_and_refreshes_only_owned_helpers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Launch snapshots shared config without rewriting user-owned state."""
        shared_config_dir = tmp_path / "shared-opencode"
        config_file = shared_config_dir / "opencode.json"
        old_helper = tmp_path / "old-install" / "cao-mcp-server"
        old_helper.parent.mkdir(parents=True)
        old_helper.write_text(
            "from cli_agent_orchestrator.mcp_server.server import main\n",
            encoding="utf-8",
        )
        custom_helper = tmp_path / "custom" / "cao-mcp-server"
        custom_helper.parent.mkdir(parents=True)
        custom_helper.write_text("#!/bin/sh\necho custom\n", encoding="utf-8")
        custom_server = {
            "type": "local",
            "command": [str(custom_helper), "--custom-flag"],
            "enabled": True,
        }
        (shared_config_dir / "agents").mkdir(parents=True)
        (shared_config_dir / "plugin-state.json").write_text("{}", encoding="utf-8")
        (shared_config_dir / "opencode.jsonc").write_text("{}", encoding="utf-8")
        config_file.write_text(
            json.dumps(
                {
                    "mcp": {
                        "orchestration-alias": {
                            "type": "local",
                            "command": [str(old_helper), "--verbose"],
                            "enabled": True,
                        },
                        "bare-alias": {"type": "local", "command": ["cao-mcp-server", "--bare"]},
                        "module-alias": {
                            "type": "local",
                            "command": [
                                "/old-install/python",
                                "-m",
                                CAO_MCP_SERVER_MODULE,
                                "--module",
                            ],
                        },
                        "user-server": custom_server,
                    }
                }
            ),
            encoding="utf-8",
        )
        source_before = config_file.read_text(encoding="utf-8")
        cao_home = tmp_path / "cao home;$runtime"

        monkeypatch.setattr(opencode_config_module, "OPENCODE_CONFIG_DIR", shared_config_dir)
        monkeypatch.setattr(opencode_config_module, "OPENCODE_CONFIG_FILE", config_file)
        monkeypatch.setattr(opencode_config_module, "CAO_HOME_DIR", cao_home, raising=False)
        monkeypatch.setattr(opencode_provider_module, "CAO_HOME_DIR", cao_home, raising=False)
        monkeypatch.setattr(opencode_provider_module, "OPENCODE_CONFIG_DIR", shared_config_dir)
        monkeypatch.setattr(opencode_provider_module, "OPENCODE_CONFIG_FILE", config_file)
        monkeypatch.setattr(
            opencode_provider_module,
            "prepare_opencode_runtime_config",
            getattr(opencode_config_module, "prepare_opencode_runtime_config", None),
            raising=False,
        )
        current_helper = ["/new-install/one/cao-mcp-server"]
        monkeypatch.setattr(
            opencode_config_module,
            "resolve_cao_mcp_command",
            lambda command, args: (current_helper[0], list(args)),
        )

        async def shell_ready(*_args, **_kwargs):
            return True

        async def opencode_ready(*_args, **_kwargs):
            return True

        backend = MagicMock()
        monkeypatch.setattr(opencode_provider_module, "wait_for_shell", shell_ready)
        monkeypatch.setattr(opencode_provider_module, "wait_until_status", opencode_ready)
        monkeypatch.setattr(opencode_provider_module, "get_backend", lambda: backend)
        monkeypatch.setattr(
            opencode_provider_module,
            "get_server_settings",
            lambda: {"provider_init_timeout": 1},
        )

        provider = make_provider()
        assert await provider.initialize() is True

        private_root = cao_home / "tmp" / "opencode-test-tid"
        private_config = private_root / "opencode.json"
        data = json.loads(private_config.read_text(encoding="utf-8"))
        assert data["mcp"]["orchestration-alias"]["command"] == [
            "/new-install/one/cao-mcp-server",
            "--verbose",
        ]
        assert data["mcp"]["bare-alias"]["command"] == [
            "/new-install/one/cao-mcp-server",
            "--bare",
        ]
        assert data["mcp"]["module-alias"]["command"] == [
            "/new-install/one/cao-mcp-server",
            "--module",
        ]
        assert data["mcp"]["user-server"] == custom_server
        assert config_file.read_text(encoding="utf-8") == source_before
        assert stat.S_IMODE(private_root.stat().st_mode) == 0o700
        assert stat.S_IMODE(private_config.stat().st_mode) == 0o600
        assert (private_root / "agents").is_symlink()
        assert (private_root / "agents").resolve() == (shared_config_dir / "agents").resolve()
        assert (private_root / "plugin-state.json").is_symlink()
        assert not (private_root / "opencode.jsonc").exists()
        launch_command = backend.send_keys.call_args.args[2]
        assert f"OPENCODE_CONFIG={shlex.quote(str(private_config))}" in launch_command
        assert f"OPENCODE_CONFIG_DIR={shlex.quote(str(private_root))}" in launch_command
        assert shlex.split(launch_command)[:2] == [
            f"OPENCODE_CONFIG={private_config}",
            f"OPENCODE_CONFIG_DIR={private_root}",
        ]

        current_helper[0] = "/new-install/two/cao-mcp-server"
        second_provider = make_provider(terminal_id="test-tid-two")
        assert await second_provider.initialize() is True
        second_root = cao_home / "tmp" / "opencode-test-tid-two"
        second_data = json.loads((second_root / "opencode.json").read_text(encoding="utf-8"))
        assert second_data["mcp"]["orchestration-alias"]["command"] == [
            "/new-install/two/cao-mcp-server",
            "--verbose",
        ]
        assert data["mcp"]["orchestration-alias"]["command"][0] == "/new-install/one/cao-mcp-server"

        provider.cleanup()
        assert not private_root.exists()
        assert second_root.exists()
        assert config_file.read_text(encoding="utf-8") == source_before
        second_provider.cleanup()
        assert not second_root.exists()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.opencode_cli.get_backend")
    async def test_initialize_success_returns_true(self, mock_tmux, mock_shell, mock_wait):
        mock_shell.return_value = True
        mock_wait.return_value = True
        provider = make_provider()
        assert await provider.initialize() is True

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.opencode_cli.get_backend")
    async def test_initialize_uses_120s_timeout(self, mock_tmux, mock_shell, mock_wait):
        mock_shell.return_value = True
        mock_wait.return_value = True
        provider = make_provider()
        await provider.initialize()
        _args, kwargs = mock_wait.call_args
        assert kwargs.get("timeout") == 120.0 or _args[2] == 120.0

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.opencode_cli.get_backend")
    async def test_initialize_sends_agent_flag(self, mock_tmux, mock_shell, mock_wait):
        mock_shell.return_value = True
        mock_wait.return_value = True
        provider = make_provider(agent_profile="developer")
        await provider.initialize()
        sent_cmd = mock_tmux.return_value.send_keys.call_args[0][2]
        assert "--agent developer" in sent_cmd

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.opencode_cli.get_backend")
    async def test_initialize_includes_model_when_set(self, mock_tmux, mock_shell, mock_wait):
        mock_shell.return_value = True
        mock_wait.return_value = True
        provider = make_provider(model="anthropic/claude-sonnet-4-6")
        await provider.initialize()
        sent_cmd = mock_tmux.return_value.send_keys.call_args[0][2]
        assert "--model anthropic/claude-sonnet-4-6" in sent_cmd

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.opencode_cli.get_backend")
    async def test_initialize_no_model_flag_when_unset(self, mock_tmux, mock_shell, mock_wait):
        mock_shell.return_value = True
        mock_wait.return_value = True
        provider = make_provider(model=None)
        await provider.initialize()
        sent_cmd = mock_tmux.return_value.send_keys.call_args[0][2]
        assert "--model" not in sent_cmd

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.opencode_cli.get_backend")
    async def test_initialize_raises_on_shell_timeout(self, mock_tmux, mock_shell):
        mock_shell.return_value = False
        provider = make_provider()
        with pytest.raises(TimeoutError, match="Shell initialization timed out"):
            await provider.initialize()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.opencode_cli.get_backend")
    async def test_initialize_raises_on_opencode_timeout(self, mock_tmux, mock_shell, mock_wait):
        mock_shell.return_value = True
        mock_wait.return_value = False
        provider = make_provider()
        with pytest.raises(TimeoutError, match="OpenCode CLI initialization timed out"):
            await provider.initialize()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.opencode_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.opencode_cli.get_backend")
    async def test_initialize_sets_env_vars_in_command(self, mock_tmux, mock_shell, mock_wait):
        mock_shell.return_value = True
        mock_wait.return_value = True
        provider = make_provider()
        await provider.initialize()
        sent_cmd = mock_tmux.return_value.send_keys.call_args[0][2]
        assert "OPENCODE_DISABLE_AUTOUPDATE=1" in sent_cmd
        assert "OPENCODE_DISABLE_MOUSE=1" in sent_cmd
        assert "OPENCODE_CLIENT=cao" in sent_cmd
        assert "TERM=xterm-256color" in sent_cmd
        assert "OPENCODE_CONFIG=" in sent_cmd
        assert "OPENCODE_CONFIG_DIR=" in sent_cmd


# ---------------------------------------------------------------------------
# (f)  exit_cli() returns "/exit"
# ---------------------------------------------------------------------------


class TestExitCli:
    def test_exit_cli_returns_slash_exit(self):
        assert make_provider().exit_cli() == "/exit"


# ---------------------------------------------------------------------------
# Miscellaneous interface methods
# ---------------------------------------------------------------------------


class TestMiscInterface:
    def test_cleanup_sets_initialized_false(self):
        provider = make_provider()
        provider._initialized = True
        provider.cleanup()
        assert provider._initialized is False

    def test_get_idle_pattern_for_log_returns_ctrl_p_pattern(self):
        pattern = make_provider().get_idle_pattern_for_log()
        assert re.search(pattern, "tab agents  ctrl+p commands  • OpenCode")

    def test_paste_enter_count_is_one(self):
        assert make_provider().paste_enter_count == 1

    def test_extraction_tail_lines_is_2000(self):
        """extraction_tail_lines must be large enough for long-response agents."""
        assert make_provider().extraction_tail_lines == 2000

    def test_paste_submit_delay_is_1_0(self):
        """paste_submit_delay must exceed BaseProvider's 0.3s default to reduce
        Enter-swallowing after bracketed paste (see #479 and #496)."""
        assert make_provider().paste_submit_delay == 1.0

    def test_supports_direct_status_probe_is_true(self):
        """The deferred-init retry loop uses this opt-in flag to gate the
        capture-pane direct status probe; only OpenCode currently sets it."""
        assert make_provider().supports_direct_status_probe is True


# ---------------------------------------------------------------------------
# Provider manager registration
# ---------------------------------------------------------------------------


class TestProviderManagerRegistration:
    """ProviderManager.create_provider('opencode_cli', …) returns an OpenCodeCliProvider."""

    def test_create_provider_returns_opencode_instance(self):
        from cli_agent_orchestrator.providers.manager import ProviderManager

        pm = ProviderManager()
        provider = pm.create_provider(
            provider_type="opencode_cli",
            terminal_id="oc-test-tid",
            tmux_session="test-session",
            tmux_window="window-0",
            agent_profile="developer",
        )
        assert isinstance(provider, OpenCodeCliProvider)

    def test_unknown_provider_type_raises(self):
        from cli_agent_orchestrator.providers.manager import ProviderManager

        pm = ProviderManager()
        with pytest.raises(ValueError, match="Unknown provider type"):
            pm.create_provider(
                provider_type="nonexistent_provider",
                terminal_id="tid",
                tmux_session="s",
                tmux_window="w",
            )


# ---------------------------------------------------------------------------
# launch.py PROVIDERS_REQUIRING_WORKSPACE_ACCESS
# ---------------------------------------------------------------------------


class TestWorkspaceAccess:
    def test_opencode_cli_in_workspace_access_set(self):
        from cli_agent_orchestrator.cli.commands.launch import (
            PROVIDERS_REQUIRING_WORKSPACE_ACCESS,
        )

        assert "opencode_cli" in PROVIDERS_REQUIRING_WORKSPACE_ACCESS
