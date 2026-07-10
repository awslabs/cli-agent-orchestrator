"""Unit tests for the Qwen Code (``qwen``) provider."""

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.qwen_cli import (
    IDLE_FOOTER_PATTERN,
    PROCESSING_FOOTER_PATTERN,
    ProviderError,
    QwenCliProvider,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"

WHICH_QWEN = "cli_agent_orchestrator.providers.qwen_cli.shutil.which"
LOAD_PROFILE = "cli_agent_orchestrator.providers.qwen_cli.load_agent_profile"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_provider(
    agent_profile=None, allowed_tools=None, model=None, skill_prompt=None
) -> QwenCliProvider:
    return QwenCliProvider(
        terminal_id="test-tid",
        session_name="test-session",
        window_name="window-0",
        agent_profile=agent_profile,
        allowed_tools=allowed_tools,
        model=model,
        skill_prompt=skill_prompt,
    )


# --------------------------------------------------------------------------- #
# Status detection (against captured live qwen TUI fixtures)
# --------------------------------------------------------------------------- #


def test_status_idle_fixture():
    assert make_provider().get_status(load_fixture("qwen_cli_idle.txt")) == TerminalStatus.IDLE


def test_status_processing_fixture():
    assert (
        make_provider().get_status(load_fixture("qwen_cli_processing.txt"))
        == TerminalStatus.PROCESSING
    )


def test_status_completed_after_turn():
    p = make_provider()
    p.mark_input_received()  # _turns -> 1
    assert p.get_status(load_fixture("qwen_cli_completed.txt")) == TerminalStatus.COMPLETED


def test_status_idle_vs_completed_split_on_turns():
    # Same ready-looking input box is IDLE before the first delivered turn.
    p = make_provider()
    assert p.get_status(load_fixture("qwen_cli_completed.txt")) == TerminalStatus.IDLE


def test_status_api_error_turn_is_completed_not_error():
    # A transient "✕ [API Error ...]" turn returns to the ready input box
    # (retryable) — reported as COMPLETED, not a status error.
    p = make_provider()
    p.mark_input_received()
    assert p.get_status(load_fixture("qwen_cli_error.txt")) == TerminalStatus.COMPLETED


def test_status_empty_is_unknown():
    assert make_provider().get_status("") == TerminalStatus.UNKNOWN
    assert make_provider().get_status(None) == TerminalStatus.UNKNOWN


def test_status_returns_native_when_backend_reports_it():
    # On the herdr backend, native agent state short-circuits buffer parsing.
    p = make_provider()
    with patch.object(p, "_resolve_native_status", return_value=TerminalStatus.COMPLETED):
        assert p.get_status("irrelevant buffer") == TerminalStatus.COMPLETED


def test_status_processing_takes_priority_over_idle_footer():
    # If both a live spinner and a ready footer appear, the "esc to cancel" tail
    # wins on the raw stream.
    buf = "shift + tab to cycle\n" * 5 + ("x" * 3000) + "\n..  Working... (2.5s · esc to cancel)\n"
    assert make_provider().get_status(buf) == TerminalStatus.PROCESSING


def test_status_waiting_user_answer_fixture():
    assert (
        make_provider().get_status(load_fixture("qwen_cli_waiting.txt"))
        == TerminalStatus.WAITING_USER_ANSWER
    )


def test_status_waiting_user_answer_yn_prompt():
    buf = "Do you want to proceed? [y/n]\n"
    assert make_provider().get_status(buf) == TerminalStatus.WAITING_USER_ANSWER


def test_status_hard_error():
    assert make_provider().get_status("Error: something exploded\n") == TerminalStatus.ERROR


def test_status_unknown_when_no_markers():
    # Non-empty buffer with no footer / spinner / error markers -> UNKNOWN.
    assert make_provider().get_status("just some banner text\n") == TerminalStatus.UNKNOWN


# --------------------------------------------------------------------------- #
# pyte rendered-screen status detection (get_status_from_screen)
# --------------------------------------------------------------------------- #


def test_provider_opts_into_screen_detection():
    assert make_provider().supports_screen_detection is True


def test_screen_status_processing_fixture():
    rows = load_fixture("qwen_cli_processing.txt").split("\n")
    assert make_provider().get_status_from_screen(rows) == TerminalStatus.PROCESSING


def test_screen_status_completed_after_turn():
    p = make_provider()
    p.mark_input_received()
    rows = load_fixture("qwen_cli_completed.txt").split("\n")
    assert p.get_status_from_screen(rows) == TerminalStatus.COMPLETED


def test_screen_status_idle_pre_first_turn():
    rows = load_fixture("qwen_cli_idle.txt").split("\n")
    assert make_provider().get_status_from_screen(rows) == TerminalStatus.IDLE


def test_screen_status_waiting_user_answer():
    screen = ["Do you want to proceed? [y/n]"]
    assert make_provider().get_status_from_screen(screen) == TerminalStatus.WAITING_USER_ANSWER


def test_screen_status_empty_is_unknown():
    assert make_provider().get_status_from_screen([]) == TerminalStatus.UNKNOWN
    assert make_provider().get_status_from_screen(["   ", ""]) == TerminalStatus.UNKNOWN


def test_screen_status_error():
    assert make_provider().get_status_from_screen(["Error: boom", "more"]) == TerminalStatus.ERROR


def test_screen_status_unknown_when_no_markers():
    assert (
        make_provider().get_status_from_screen(["just some text", "no markers here"])
        == TerminalStatus.UNKNOWN
    )


# --------------------------------------------------------------------------- #
# Startup-dialog handling (_handle_startup_dialog)
# --------------------------------------------------------------------------- #


@patch("cli_agent_orchestrator.providers.qwen_cli.time.sleep", return_value=None)
@patch("cli_agent_orchestrator.providers.qwen_cli.get_backend")
def test_handle_startup_dialog_dismisses_then_returns_on_ready(mock_backend, _sleep):
    backend = mock_backend.return_value
    backend.get_history.side_effect = [
        "Do you trust the files in this folder?  > Yes, proceed",  # dialog
        "  YOLO mode (shift + tab to cycle)",  # ready input box
    ]
    make_provider()._handle_startup_dialog(timeout=5.0)
    # Accepted the pre-selected option with Enter.
    backend.send_special_key.assert_called_once()
    assert backend.send_special_key.call_args.args[2] == "Enter"


@patch("cli_agent_orchestrator.providers.qwen_cli.time.sleep", return_value=None)
@patch("cli_agent_orchestrator.providers.qwen_cli.get_backend")
def test_handle_startup_dialog_noop_when_already_ready(mock_backend, _sleep):
    backend = mock_backend.return_value
    backend.get_history.return_value = "  YOLO mode (shift + tab to cycle)"
    make_provider()._handle_startup_dialog(timeout=5.0)
    backend.send_special_key.assert_not_called()


@patch("cli_agent_orchestrator.providers.qwen_cli.get_server_settings")
@patch("cli_agent_orchestrator.providers.qwen_cli.time.sleep", return_value=None)
@patch("cli_agent_orchestrator.providers.qwen_cli.get_backend")
def test_handle_startup_dialog_default_timeout_and_empty_output(
    mock_backend, _sleep, mock_settings
):
    # timeout=None resolves the server setting; an empty first poll falls through
    # to the tail sleep, then the ready footer ends the loop.
    mock_settings.return_value = {"startup_prompt_handler_timeout": 5.0}
    backend = mock_backend.return_value
    backend.get_history.side_effect = [None, "  YOLO mode (shift + tab to cycle)"]
    make_provider()._handle_startup_dialog()
    mock_settings.assert_called_once()
    backend.send_special_key.assert_not_called()


def test_screen_resolves_stale_processing_footer_regression():
    """Regression: the append-only stream keeps a stale "esc to cancel" after a
    turn ends (qwen redraws the spinner away in place), so the raw get_status()
    latches PROCESSING. A composited pyte viewport shows only the final ready
    input box ⇒ IDLE/COMPLETED, so the session reaches ready instead of timing
    out.
    """
    p = make_provider()
    # Raw append-only stream: stale spinner survives below the response, with the
    # live ready footer rendered last — raw path wrongly reports PROCESSING.
    raw = (
        "> analyze this\n  ● here is the analysis\n"
        + "..  Working... (3.0s · esc to cancel)\n"
        + ("\n" * 3)
        + "  YOLO mode (shift + tab to cycle)\n"
    )
    assert p.get_status(raw) == TerminalStatus.PROCESSING

    # Composited viewport: the in-place rewrite is resolved, leaving only the
    # ready input box ⇒ IDLE (no delivered turn yet).
    screen = [
        "> analyze this",
        "  ● here is the analysis",
        "─" * 80,
        "*   Type your message or @path/to/file",
        "─" * 80,
        "  YOLO mode (shift + tab to cycle)",
    ]
    assert p.get_status_from_screen(screen) == TerminalStatus.IDLE


# --------------------------------------------------------------------------- #
# Response extraction
# --------------------------------------------------------------------------- #


def test_extract_completed_response():
    assert (
        make_provider().extract_last_message_from_script(load_fixture("qwen_cli_completed.txt"))
        == "2 + 2 equals 4."
    )


def test_extract_multiline_response_strips_bullet():
    out = make_provider().extract_last_message_from_script(load_fixture("qwen_cli_response.txt"))
    assert out == "The answer is 4.\nTwo plus two equals four. Let me know if you want more detail."
    assert "●" not in out


def test_extract_raises_without_query():
    with pytest.raises(ValueError, match="No Qwen Code user query"):
        make_provider().extract_last_message_from_script("no query here\njust text\n")


_RULE = "─" * 30  # input-box separator (SEPARATOR_PATTERN needs >= 20)


def test_extract_filters_banner_tip_spinner_footer():
    script = (
        "> what is 2+2\n"
        "  ● Qwen Code (v0.19.8)\n"  # banner chrome
        "  Tips: press something\n"  # tip chrome
        "  ..  Working... (1.0s · esc to cancel)\n"  # spinner chrome
        "  YOLO mode (shift + tab to cycle)\n"  # footer chrome
        "  ● The answer is 4.\n"  # real content
        f"{_RULE}\n"
        "*   Type your message or @path/to/file\n"
    )
    out = make_provider().extract_last_message_from_script(script)
    assert out == "The answer is 4."


def test_extract_raises_on_empty_response():
    script = f"> hello\n  ..  Working... (1.0s · esc to cancel)\n{_RULE}\n"
    with pytest.raises(ValueError, match="Empty"):
        make_provider().extract_last_message_from_script(script)


def test_extract_uses_last_query():
    script = (
        "> first question\n  ● first answer\n"
        f"{_RULE}\n"
        "> second question\n  ● second answer\n"
        f"{_RULE}\n"
    )
    assert make_provider().extract_last_message_from_script(script) == "second answer"


def test_extract_strips_ansi_codes():
    script = "> hi\n  ● \x1b[32mHello\x1b[0m world\n" + _RULE + "\n"
    out = make_provider().extract_last_message_from_script(script)
    assert "\x1b" not in out
    assert "Hello world" in out


# --------------------------------------------------------------------------- #
# Command building
# --------------------------------------------------------------------------- #


def test_build_command_raises_when_binary_missing():
    with patch(WHICH_QWEN, return_value=None):
        with pytest.raises(ProviderError, match="not found"):
            make_provider()._build_qwen_command()


def test_build_command_includes_yolo_and_model():
    with patch(WHICH_QWEN, return_value="/usr/local/bin/qwen"):
        cmd = make_provider(model="qwen3-coder-plus")._build_qwen_command()
    assert cmd.startswith("qwen --approval-mode yolo")
    assert "--model" in cmd and "qwen3-coder-plus" in cmd


def test_build_command_excludes_native_send_message():
    """qwen-code ships a native ``send_message`` team tool whose bare name
    collides with cao-mcp-server's ``send_message`` (exposed to qwen as
    ``mcp__cao-mcp-server__send_message``). Told to "send_message", the model
    picks the native tool → "No active team" → assign/handoff callbacks never
    route back to the supervisor. We exclude it so only the MCP tool remains.
    See issue #376.
    """
    with patch(WHICH_QWEN, return_value="/usr/local/bin/qwen"):
        cmd = make_provider()._build_qwen_command()
    assert "--exclude-tools send_message" in cmd


def test_build_command_without_profile_has_no_system_prompt():
    with patch(WHICH_QWEN, return_value="/usr/local/bin/qwen"):
        cmd = make_provider()._build_qwen_command()
    assert "--append-system-prompt" not in cmd
    assert "--mcp-config" not in cmd


def test_build_command_injects_append_system_prompt():
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    profile = AgentProfile(
        name="reviewer_qwen", description="Reviewer", system_prompt="You review code."
    )
    with (
        patch(WHICH_QWEN, return_value="/usr/local/bin/qwen"),
        patch(LOAD_PROFILE, return_value=profile),
    ):
        cmd = make_provider(agent_profile="reviewer_qwen")._build_qwen_command()
    assert "--append-system-prompt" in cmd
    assert "You review code." in cmd
    # No first-turn guard: --append-system-prompt is a true system prompt.
    assert "Acknowledge your role" not in cmd


def test_build_command_appends_security_prompt_when_tool_restricted():
    from cli_agent_orchestrator.constants import SECURITY_PROMPT
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    profile = AgentProfile(
        name="reviewer_qwen", description="Reviewer", system_prompt="You review code."
    )
    with (
        patch(WHICH_QWEN, return_value="/usr/local/bin/qwen"),
        patch(LOAD_PROFILE, return_value=profile),
    ):
        cmd = make_provider(
            agent_profile="reviewer_qwen", allowed_tools=["fs_read", "fs_list"]
        )._build_qwen_command()
    assert SECURITY_PROMPT.split("\n", 1)[0] in cmd


def test_build_command_unrestricted_wildcard_omits_security_prompt():
    from cli_agent_orchestrator.constants import SECURITY_PROMPT
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    profile = AgentProfile(name="dev_qwen", description="Dev", system_prompt="You write code.")
    with (
        patch(WHICH_QWEN, return_value="/usr/local/bin/qwen"),
        patch(LOAD_PROFILE, return_value=profile),
    ):
        cmd = make_provider(agent_profile="dev_qwen", allowed_tools=["*"])._build_qwen_command()
    assert SECURITY_PROMPT.split("\n", 1)[0] not in cmd


def test_build_command_includes_skill_catalog():
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    profile = AgentProfile(name="dev_qwen", description="Dev", system_prompt="You write code.")
    with (
        patch(WHICH_QWEN, return_value="/usr/local/bin/qwen"),
        patch(LOAD_PROFILE, return_value=profile),
    ):
        cmd = make_provider(
            agent_profile="dev_qwen", skill_prompt="## Available Skills\n- foo: bar"
        )._build_qwen_command()
    assert "Available Skills" in cmd


def test_profile_model_overrides_constructor_model():
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    profile = AgentProfile(
        name="reviewer_qwen",
        description="Reviewer",
        system_prompt="You review code.",
        model="qwen3-max",
    )
    with (
        patch(WHICH_QWEN, return_value="/usr/local/bin/qwen"),
        patch(LOAD_PROFILE, return_value=profile),
    ):
        cmd = make_provider(
            agent_profile="reviewer_qwen", model="qwen3-coder-plus"
        )._build_qwen_command()
    assert "qwen3-max" in cmd  # profile wins
    assert "qwen3-coder-plus" not in cmd


def test_build_command_raises_on_bad_profile():
    with (
        patch(WHICH_QWEN, return_value="/usr/local/bin/qwen"),
        patch(LOAD_PROFILE, side_effect=RuntimeError("boom")),
    ):
        with pytest.raises(ProviderError, match="Failed to load agent profile"):
            make_provider(agent_profile="missing")._build_qwen_command()


# --------------------------------------------------------------------------- #
# MCP config (per-terminal --mcp-config temp file)
# --------------------------------------------------------------------------- #


def _mcp_config_path_from_cmd(cmd: str) -> Path:
    import shlex

    parts = shlex.split(cmd)
    idx = parts.index("--mcp-config")
    return Path(parts[idx + 1])


def test_mcp_config_written_with_terminal_id():
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    profile = AgentProfile(
        name="reviewer_qwen",
        description="Reviewer",
        system_prompt="You review code.",
        mcpServers={"cao-mcp-server": {"command": "uvx", "args": ["cao-mcp-server"]}},
    )
    p = make_provider(agent_profile="reviewer_qwen")
    with (
        patch(WHICH_QWEN, return_value="/usr/local/bin/qwen"),
        patch(LOAD_PROFILE, return_value=profile),
    ):
        cmd = p._build_qwen_command()
    cfg_path = _mcp_config_path_from_cmd(cmd)
    try:
        data = json.loads(cfg_path.read_text())
        assert "cao-mcp-server" in data["mcpServers"]
        # CAO_TERMINAL_ID forwarded so cao-mcp-server can resolve the terminal.
        assert data["mcpServers"]["cao-mcp-server"]["env"]["CAO_TERMINAL_ID"] == "test-tid"
    finally:
        cfg_path.unlink(missing_ok=True)


def test_mcp_config_accepts_pydantic_mcpserver():
    from cli_agent_orchestrator.models.agent_profile import AgentProfile, McpServer

    profile = AgentProfile(
        name="reviewer_qwen",
        description="Reviewer",
        system_prompt="You review code.",
        mcpServers={"cao-mcp-server": McpServer(command="uvx", args=["cao-mcp-server"])},
    )
    p = make_provider(agent_profile="reviewer_qwen")
    with (
        patch(WHICH_QWEN, return_value="/usr/local/bin/qwen"),
        patch(LOAD_PROFILE, return_value=profile),
    ):
        cmd = p._build_qwen_command()
    cfg_path = _mcp_config_path_from_cmd(cmd)
    try:
        data = json.loads(cfg_path.read_text())
        assert data["mcpServers"]["cao-mcp-server"]["env"]["CAO_TERMINAL_ID"] == "test-tid"
        assert data["mcpServers"]["cao-mcp-server"]["command"] == "uvx"
    finally:
        cfg_path.unlink(missing_ok=True)


def test_cleanup_deletes_mcp_temp_file():
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    profile = AgentProfile(
        name="reviewer_qwen",
        description="Reviewer",
        system_prompt="You review code.",
        mcpServers={"cao-mcp-server": {"command": "uvx", "args": ["cao-mcp-server"]}},
    )
    p = make_provider(agent_profile="reviewer_qwen")
    with (
        patch(WHICH_QWEN, return_value="/usr/local/bin/qwen"),
        patch(LOAD_PROFILE, return_value=profile),
    ):
        cmd = p._build_qwen_command()
    cfg_path = _mcp_config_path_from_cmd(cmd)
    assert cfg_path.exists()
    p.cleanup()
    assert not cfg_path.exists()
    assert p._tmp_paths == []
    assert p._initialized is False


def test_cleanup_noop_when_nothing_registered():
    p = make_provider()
    p.cleanup()  # must not raise
    assert p._tmp_paths == []


def test_cleanup_tolerates_already_deleted_file():
    p = make_provider()
    p._tmp_paths = [Path("/tmp/cao_qwen_mcp_does_not_exist.json")]
    p.cleanup()  # must not raise on missing file
    assert p._tmp_paths == []


# --------------------------------------------------------------------------- #
# Lifecycle / misc
# --------------------------------------------------------------------------- #


def test_exit_cli_is_quit():
    assert make_provider().exit_cli() == "/quit"


def test_mark_input_received_increments_turns():
    p = make_provider()
    assert p._turns == 0
    p.mark_input_received()
    assert p._turns == 1


def test_footer_patterns_smoke():
    assert re.search(PROCESSING_FOOTER_PATTERN, "esc to cancel")
    assert re.search(IDLE_FOOTER_PATTERN, "  YOLO mode (shift + tab to cycle)")
    assert re.search(IDLE_FOOTER_PATTERN, "*   Type your message or @path/to/file")
    assert re.search(IDLE_FOOTER_PATTERN, "? for shortcuts")


def test_blocks_orchestrated_input_while_waiting_user_answer():
    assert make_provider().blocks_orchestrated_input_while_waiting_user_answer is True


def test_get_idle_pattern_for_log():
    pat = make_provider().get_idle_pattern_for_log()
    assert re.search(pat, "  YOLO mode (shift + tab to cycle)")


def test_paste_enter_count_is_valid():
    assert make_provider().paste_enter_count in (1, 2)


# --------------------------------------------------------------------------- #
# initialize() — async
# --------------------------------------------------------------------------- #


class TestInitialize:
    @pytest.fixture(autouse=True)
    def _stub_startup_dialog(self):
        # initialize() polls the pane to dismiss qwen's first-run theme/trust
        # dialog; that path is exercised directly elsewhere. Stub it so the init
        # tests stay fast and independent of the (mocked) get_history return type.
        with patch.object(QwenCliProvider, "_handle_startup_dialog", return_value=None):
            yield

    @pytest.mark.asyncio
    @patch(WHICH_QWEN, return_value="/usr/local/bin/qwen")
    @patch("cli_agent_orchestrator.providers.qwen_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.qwen_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.qwen_cli.get_backend")
    async def test_initialize_success(self, mock_backend, mock_shell, mock_wait, _which):
        mock_shell.return_value = True
        mock_wait.return_value = True
        provider = make_provider()
        assert await provider.initialize() is True
        assert provider._initialized is True
        mock_backend.return_value.send_keys.assert_called_once()
        sent = mock_backend.return_value.send_keys.call_args.args[2]
        assert sent.startswith("qwen --approval-mode yolo")

    @pytest.mark.asyncio
    @patch(WHICH_QWEN, return_value="/usr/local/bin/qwen")
    @patch("cli_agent_orchestrator.providers.qwen_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.qwen_cli.get_backend")
    async def test_initialize_shell_timeout(self, mock_backend, mock_shell, _which):
        mock_shell.return_value = False
        provider = make_provider()
        with pytest.raises(TimeoutError, match="Shell initialization timed out"):
            await provider.initialize()
        mock_backend.return_value.send_keys.assert_not_called()

    @pytest.mark.asyncio
    @patch(WHICH_QWEN, return_value="/usr/local/bin/qwen")
    @patch("cli_agent_orchestrator.providers.qwen_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.qwen_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.qwen_cli.get_backend")
    async def test_initialize_cli_timeout(self, mock_backend, mock_shell, mock_wait, _which):
        mock_shell.return_value = True
        mock_wait.return_value = False
        provider = make_provider()
        with pytest.raises(TimeoutError, match="Qwen Code initialization timed out"):
            await provider.initialize()

    @pytest.mark.asyncio
    @patch(WHICH_QWEN, return_value="/usr/local/bin/qwen")
    @patch("cli_agent_orchestrator.providers.qwen_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.qwen_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.qwen_cli.get_backend")
    async def test_initialize_sends_model_flag(self, mock_backend, mock_shell, mock_wait, _which):
        mock_shell.return_value = True
        mock_wait.return_value = True
        provider = make_provider(model="qwen3-coder-plus")
        await provider.initialize()
        sent = mock_backend.return_value.send_keys.call_args.args[2]
        assert "--model qwen3-coder-plus" in sent
