"""Unit tests for the official xAI Grok Build CLI provider."""

import asyncio
import os
import shlex
import signal
import stat
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import psutil
import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.grok_cli import (
    _STATUS_TAIL_CHARS,
    DIRECTORY_TRUST_PATTERN,
    GrokCliProvider,
    ProviderError,
)
from cli_agent_orchestrator.services.status_monitor import StatusMonitor, status_monitor
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_provider(
    *,
    terminal_id: str = "test-terminal",
    agent_profile: str | None = None,
    allowed_tools: list[str] | None = None,
    model: str | None = None,
    skill_prompt: str | None = None,
) -> GrokCliProvider:
    return GrokCliProvider(
        terminal_id,
        "test-session",
        "test-window",
        agent_profile,
        allowed_tools,
        model,
        skill_prompt,
    )


def _completed_turn(query: str, response: str, *, raw: bool = False) -> str:
    """Build a minimal Grok completion screen for status regressions."""

    if raw:
        return (
            f"     ❯ {query}\n\n{response}\n\n"
            "\x1b[38;6H\x1b[2mWorked for 2.0s\x1b[38;220H\x1b[22m"
            "█                               █\x1b[49;22H"
            "\x1b[1mCtrl+x\x1b[22m:shortcuts"
        )
    return (
        f"     ❯ {query}\n\n{response}\n\n"
        "     Worked for 2.0s\n\n"
        "  Shift+Tab:mode  │  Ctrl+x:shortcuts"
    )


def test_prompt_submission_and_lifecycle_properties():
    provider = make_provider()
    assert provider.paste_enter_count == 1
    assert provider.paste_submit_delay == 0.4
    assert provider.blocks_orchestrated_input_while_waiting_user_answer is True
    assert provider.exit_cli() == "/quit"
    assert provider.supports_screen_detection is False
    assert provider.supports_direct_status_probe is False


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("grok_cli_idle.txt", TerminalStatus.IDLE),
        ("grok_cli_processing.txt", TerminalStatus.PROCESSING),
        ("grok_cli_permission.txt", TerminalStatus.WAITING_USER_ANSWER),
        ("grok_cli_login.txt", TerminalStatus.WAITING_USER_ANSWER),
        ("grok_cli_telemetry_banner.txt", TerminalStatus.IDLE),
        ("grok_cli_error.txt", TerminalStatus.ERROR),
    ],
)
def test_status_fixtures(fixture, expected):
    assert make_provider().get_status(load_fixture(fixture)) == expected


def test_completed_requires_dispatched_turn():
    provider = make_provider()
    completed = load_fixture("grok_cli_completed.txt")
    assert provider.get_status(completed) == TerminalStatus.IDLE
    provider.mark_input_received()
    assert provider.get_status(completed) == TerminalStatus.COMPLETED


def test_processing_wins_even_when_empty_composer_is_visible():
    output = load_fixture("grok_cli_processing.txt")
    assert "│ ❯" in output
    assert make_provider().get_status(output) == TerminalStatus.PROCESSING


def test_stale_processing_before_current_completion_is_ignored():
    provider = make_provider()
    provider.mark_input_received()
    output = "Waiting for response…\nEsc:cancel\n" + load_fixture("grok_cli_completed.txt")
    assert provider.get_status(output) == TerminalStatus.COMPLETED


def test_stale_permission_and_error_before_current_ready_are_ignored():
    output = (
        load_fixture("grok_cli_permission.txt")
        + "\nError: old transient error\n"
        + load_fixture("grok_cli_idle.txt")
    )
    assert make_provider().get_status(output) == TerminalStatus.IDLE


def test_old_idle_then_current_processing_is_processing():
    output = load_fixture("grok_cli_idle.txt") + "\n" + load_fixture("grok_cli_processing.txt")
    assert make_provider().get_status(output) == TerminalStatus.PROCESSING


def test_unknown_and_empty_output():
    provider = make_provider()
    assert provider.get_status("") == TerminalStatus.UNKNOWN
    assert provider.get_status(None) == TerminalStatus.UNKNOWN
    assert provider.get_status("unrecognized live screen") == TerminalStatus.UNKNOWN


def test_ansi_and_cursor_sequences_are_normalized_for_status():
    output = "\x1b[2J\x1b[1G\x1b[32m⠦ Waiting for response…\x1b[0m\nEsc:cancel"
    assert make_provider().get_status(output) == TerminalStatus.PROCESSING


def test_raw_cursor_positioned_idle_composer_from_live_pipe_pane():
    """Grok positions │, ❯, │ with separate CUP sequences in raw logs."""
    output = load_fixture("grok_cli_idle.raw.ansi.txt")
    assert make_provider().get_status(output) == TerminalStatus.IDLE


def test_raw_cursor_positioned_completion_overrides_stale_processing():
    """Worked-for is CUP-positioned mid-redraw in Grok's append-only log."""
    provider = make_provider()
    provider.mark_input_received()
    output = load_fixture("grok_cli_completed.raw.ansi.txt")
    assert provider.get_status(output) == TerminalStatus.COMPLETED


def test_live_raw_completion_with_block_cursor_before_footer_is_completed():
    """Grok 1.0.0 pipe-pane output places a block cursor before Ctrl+x."""
    provider = make_provider()
    provider.mark_input_received()
    output = (
        "\x1b[38;6H\x1b[2mWorked for 24s\x1b[38;220H\x1b[22m"
        "█                               █\x1b[49;22H"
        "\x1b[1mCtrl+x\x1b[22m:shortcuts"
    )
    assert provider.get_status(output) == TerminalStatus.COMPLETED


def test_worked_for_prose_and_composer_without_footer_is_not_completion():
    provider = make_provider()
    provider.mark_input_received()
    output = "     ❯ Question\nanswer\nWorked for 24s\n│ ❯ │"
    assert provider.get_status(output) == TerminalStatus.PROCESSING


def test_second_turn_prose_cannot_replace_stale_completion_fingerprint():
    provider = make_provider()
    provider.mark_input_received()
    first = load_fixture("grok_cli_completed.txt")
    assert provider.get_status(first) == TerminalStatus.COMPLETED

    provider.mark_input_received()
    output = (
        first
        + "\n     ❯ New question\n"
        + "     The benchmark Worked for 2.0s total\n"
        + "  Shift+Tab:mode  │  Ctrl+x:shortcuts\n"
    )
    assert provider.get_status(output) == TerminalStatus.PROCESSING


@pytest.mark.parametrize(
    "prose",
    [
        "The benchmark Worked for 2.0s total",
        "- Worked for 2.0s on parsing",
    ],
)
def test_worked_for_prose_during_active_turn_is_not_completion(prose):
    provider = make_provider()
    provider.mark_input_received()
    output = f"Waiting for response…\n{prose}\n│❯│\nEsc:cancel"
    assert provider.get_status(output) == TerminalStatus.PROCESSING


def test_evicted_raw_completion_ordinal_does_not_match_tail_prose():
    """A raw marker outside the 8 KiB tail must not complete same-duration prose."""

    provider = make_provider()
    provider.mark_input_received()
    old_raw = _completed_turn("first question", "first answer", raw=True)
    padding = ("padding line that evicts prior chrome\n") * 400
    current = (
        "Waiting for response…\n"
        "     ❯ second question\n"
        "     The benchmark Worked for 2.0s total\n"
        "  Shift+Tab:mode  │  Ctrl+x:shortcuts\n"
    )
    output = f"{old_raw}\n{padding}{current}"
    clean = strip_terminal_escapes(output)
    tail = clean[-_STATUS_TAIL_CHARS:]
    assert "first question" not in tail
    assert tail.count("Worked for 2.0s") == 1
    assert "The benchmark Worked for 2.0s total" in tail
    assert provider.get_status(output) == TerminalStatus.PROCESSING


def test_dispatch_before_new_output_does_not_false_complete():
    provider = make_provider()
    provider.mark_input_received()
    assert provider.get_status(load_fixture("grok_cli_idle.txt")) == TerminalStatus.PROCESSING


def test_second_dispatch_does_not_re_report_previous_completion():
    provider = make_provider()
    completed = load_fixture("grok_cli_completed.txt")
    provider.mark_input_received()
    assert provider.get_status(completed) == TerminalStatus.COMPLETED
    provider.mark_input_received()
    assert provider.get_status(completed) == TerminalStatus.PROCESSING
    assert provider.get_status(load_fixture("grok_cli_processing.txt")) == TerminalStatus.PROCESSING
    assert provider.get_status(load_fixture("grok_cli_second_turn.txt")) == TerminalStatus.COMPLETED


def test_stale_completion_guard_remains_armed_after_processing_frame():
    provider = make_provider()
    first = load_fixture("grok_cli_completed.txt")
    provider.mark_input_received()
    assert provider.get_status(first) == TerminalStatus.COMPLETED
    provider.mark_input_received()
    assert provider.get_status(load_fixture("grok_cli_processing.txt")) == TerminalStatus.PROCESSING
    # A delayed/stale raw-buffer frame from turn one must not finish turn two.
    assert provider.get_status(first) == TerminalStatus.PROCESSING


def test_previous_completion_before_new_turn_activity_stays_processing():
    provider = make_provider()
    completed = _completed_turn("first", "first response")
    provider.mark_input_received()
    assert provider.get_status(completed) == TerminalStatus.COMPLETED

    provider.mark_input_received()
    assert provider.get_status(completed) == TerminalStatus.PROCESSING


def test_processing_then_stale_completion_then_new_completion():
    provider = make_provider()
    first = _completed_turn("first", "first response")
    provider.mark_input_received()
    assert provider.get_status(first) == TerminalStatus.COMPLETED

    provider.mark_input_received()
    assert provider.get_status("Waiting for response…\nEsc:cancel") == TerminalStatus.PROCESSING
    assert provider.get_status(first) == TerminalStatus.PROCESSING
    second = first + "\n" + _completed_turn("second", "second response")
    assert provider.get_status(second) == TerminalStatus.COMPLETED


def test_long_distinct_turns_with_identical_duration_complete():
    provider = make_provider()
    first = _completed_turn("first question", "a" * 9_100)
    provider.mark_input_received()
    assert provider.get_status(first) == TerminalStatus.COMPLETED

    provider.mark_input_received()
    second = first + "\n" + _completed_turn("second question", "b" * 9_100)
    assert provider.get_status(second) == TerminalStatus.COMPLETED


def test_identical_completion_marker_survives_rolling_buffer_eviction():
    """A shifted stale marker must not pin a later long turn in PROCESSING."""

    provider = make_provider()
    buffer_limit = 1_024
    first = _completed_turn("first question", "a" * (buffer_limit + 200))[-buffer_limit:]
    second = _completed_turn("second question", "b" * (buffer_limit + 200))[-buffer_limit:]
    # Both retained suffixes have lost their query/response identity; only the
    # identical ``Worked for 2.0s`` completion chrome remains.
    assert "first question" not in first
    assert "second question" not in second

    with patch(
        "cli_agent_orchestrator.services.settings_service.get_server_settings",
        return_value={"state_buffer_max": buffer_limit},
    ):
        provider.mark_input_received()
        assert provider.get_status(first) == TerminalStatus.COMPLETED

        provider.mark_input_received()
        processing = (first + "\nWaiting for response…\nEsc:cancel")[-buffer_limit:]
        assert provider.get_status(processing) == TerminalStatus.PROCESSING
        assert provider.get_status(second) == TerminalStatus.COMPLETED


def test_byte_identical_consecutive_turns_have_distinct_generations():
    provider = make_provider()
    completed = _completed_turn("repeat exactly", "same response")
    provider.mark_input_received()
    assert provider.get_status(completed) == TerminalStatus.COMPLETED

    provider.mark_input_received()
    assert provider.get_status(completed + "\n" + completed) == TerminalStatus.COMPLETED


def test_buffer_clear_generation_accepts_coalesced_identical_completion():
    """A real send-input boundary must not wedge a fast repeated turn.

    This drives the same order used by ``terminal_service.send_input``:
    completed first turn, arm StatusMonitor, clear its rolling buffer while
    notifying Grok, mark the input received, then receive one FIFO chunk with
    both the current processing marker and a byte-identical completion.  The
    second direct status lookup models StatusMonitor's settled recheck.
    """

    provider = make_provider()
    monitor = StatusMonitor()
    completed = _completed_turn("repeat exactly", "same response")
    coalesced = f"Waiting for response…\nEsc:cancel\n{completed}"

    with patch("cli_agent_orchestrator.services.status_monitor.provider_manager") as manager:
        manager.get_provider.return_value = provider

        provider.mark_input_received()
        monitor._process_chunk("test-terminal", completed)
        assert monitor._last_status["test-terminal"] == TerminalStatus.COMPLETED

        monitor.notify_input_sent("test-terminal")
        monitor.clear_rolling_buffer("test-terminal", provider)
        provider.mark_input_received()
        monitor._process_chunk("test-terminal", coalesced)
        assert monitor._last_status["test-terminal"] == TerminalStatus.COMPLETED

        # While cached PROCESSING, get_status performs a direct settled
        # recheck; pin the successful state through the same code path too.
        monitor._last_status["test-terminal"] = TerminalStatus.PROCESSING
        assert monitor.get_status("test-terminal") == TerminalStatus.COMPLETED


def test_buffer_clear_generation_rejects_stale_identical_completion_without_activity():
    """An old completed screen after clear is not proof a new turn completed."""

    provider = make_provider()
    monitor = StatusMonitor()
    completed = _completed_turn("repeat exactly", "same response")

    with patch("cli_agent_orchestrator.services.status_monitor.provider_manager") as manager:
        manager.get_provider.return_value = provider

        provider.mark_input_received()
        monitor._process_chunk("test-terminal", completed)
        assert monitor._last_status["test-terminal"] == TerminalStatus.COMPLETED

        monitor.notify_input_sent("test-terminal")
        monitor.clear_rolling_buffer("test-terminal", provider)
        provider.mark_input_received()
        monitor._process_chunk("test-terminal", completed)

    assert monitor._last_status["test-terminal"] == TerminalStatus.PROCESSING


@pytest.mark.parametrize("raw", [False, True])
def test_long_turn_completion_uses_full_transcript_for_rendered_and_raw_output(raw):
    provider = make_provider()
    first = _completed_turn("first", "a" * 9_100, raw=raw)
    provider.mark_input_received()
    assert provider.get_status(first) == TerminalStatus.COMPLETED

    provider.mark_input_received()
    second = first + "\n" + _completed_turn("second", "b" * 9_100, raw=raw)
    assert provider.get_status(second) == TerminalStatus.COMPLETED


def test_extract_completed_response_preserves_markdown_and_code():
    response = make_provider().extract_last_message_from_script(
        load_fixture("grok_cli_completed.txt")
    )
    assert response == "Here is the answer with **Markdown**.\n\n```python\nprint(42)\n```"
    assert "Thought" not in response
    assert "Worked for" not in response
    assert "Return a concise answer" not in response


def test_extract_second_turn_uses_last_boundaries_only():
    combined = (
        load_fixture("grok_cli_completed.txt") + "\n" + load_fixture("grok_cli_second_turn.txt")
    )
    response = make_provider().extract_last_message_from_script(combined)
    assert response == "SECOND_TURN_OK"
    assert "Here is the answer" not in response


def test_extract_removes_tool_and_telemetry_chrome():
    output = """     ❯ Complete the task.

     ◆ Thought for 1.0s
  ┃  ◆ Run a tool
  ┃  tool output
     Final answer.
  Help improve Grok [Opt out] [Opt in]
     Worked for 2.0s
"""
    assert make_provider().extract_last_message_from_script(output) == "Final answer."


def test_extract_strips_ansi_and_terminal_timestamp():
    output = (
        "     ❯ Question                                      4:43 AM\n\n"
        "     \x1b[32mUnicode ✓\x1b[0m                         4:44 AM\n\n"
        "     Worked for 1.0s\n"
    )
    assert make_provider().extract_last_message_from_script(output) == "Unicode ✓"


def test_extract_realistic_ansi_fixture():
    assert (
        make_provider().extract_last_message_from_script(
            load_fixture("grok_cli_completed.ansi.txt")
        )
        == "ANSI-safe response."
    )


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ("     ❯ Question\nanswer", "completion boundary"),
        ("answer\nWorked for 1.0s", "user query"),
        ("     ❯ Question\n◆ Thought for 1s\nWorked for 1.0s", "Empty"),
    ],
)
def test_extract_invalid_output_raises(output, message):
    with pytest.raises(ValueError, match=message):
        make_provider().extract_last_message_from_script(output)


def _profile(**kwargs) -> AgentProfile:
    values = {
        "name": "grok-worker",
        "description": "test",
        "system_prompt": "You are a careful worker.",
    }
    values.update(kwargs)
    return AgentProfile(**values)


def test_build_command_requires_official_binary():
    with patch("cli_agent_orchestrator.providers.grok_cli.shutil.which", return_value=None):
        with pytest.raises(ProviderError, match=r"not on \$PATH"):
            make_provider()._build_grok_command()


def test_build_command_required_flags_and_unrestricted_tools(tmp_path):
    provider = make_provider(allowed_tools=["*"])
    with (
        patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path),
        patch(
            "cli_agent_orchestrator.providers.grok_cli.shutil.which",
            return_value="/opt/grok/bin/grok",
        ),
    ):
        parts = shlex.split(provider._build_grok_command())
    assert parts[0] == "env"
    assert f"GROK_HOME={provider.grok_home}" in parts
    assert "/opt/grok/bin/grok" in parts
    assert "--no-alt-screen" in parts
    assert "--always-approve" in parts
    assert "--no-subagents" in parts
    assert "GROK_SUBAGENTS=0" in parts
    assert "GROK_WORKFLOWS=0" in parts
    assert "GROK_GOAL=0" in parts
    assert "--deny" not in parts
    assert "--disable-web-search" not in parts
    provider.cleanup()


def test_default_command_disables_every_native_worker_route(tmp_path):
    provider = make_provider()
    with (
        patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path),
        patch("cli_agent_orchestrator.providers.grok_cli.shutil.which", return_value="/bin/grok"),
    ):
        parts = shlex.split(provider._build_grok_command())
    assert "--no-subagents" in parts
    assert "GROK_SUBAGENTS=0" in parts
    assert "GROK_WORKFLOWS=0" in parts
    assert "GROK_GOAL=0" in parts
    provider.cleanup()


def test_profile_can_explicitly_enable_native_grok_workflows(tmp_path):
    provider = make_provider(agent_profile="grok-native")
    with (
        patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path),
        patch("cli_agent_orchestrator.providers.grok_cli.shutil.which", return_value="/bin/grok"),
        patch(
            "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
            return_value=_profile(grokNativeWorkflows=True),
        ),
    ):
        parts = shlex.split(provider._build_grok_command())
    assert "--no-subagents" not in parts
    assert "GROK_SUBAGENTS=1" in parts
    assert "GROK_WORKFLOWS=1" in parts
    assert "GROK_GOAL=1" in parts
    provider.cleanup()


def test_directory_trust_fixture_is_recognized():
    output = (
        "Do you trust the contents of this directory?\n"
        "Grok Build may run or modify contents in this directory, posing security risks.\n"
        "Yes, proceed  y\nNo, quit  n"
    )
    assert DIRECTORY_TRUST_PATTERN.search(output)


def test_build_command_model_precedence_rules_and_skill_prompt(tmp_path):
    profile = _profile(model="profile-model")
    provider = make_provider(
        agent_profile="grok-worker",
        model="explicit-model",
        skill_prompt="## Available Skills\n- cao-supervisor",
    )
    with (
        patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path),
        patch("cli_agent_orchestrator.providers.grok_cli.shutil.which", return_value="/bin/grok"),
        patch(
            "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
            return_value=profile,
        ),
    ):
        parts = shlex.split(provider._build_grok_command())
    assert parts[parts.index("--model") + 1] == "explicit-model"
    rules = parts[parts.index("--rules") + 1]
    assert "You are a careful worker." in rules
    assert "## Available Skills" in rules
    assert "cao-supervisor" in rules
    provider.cleanup()


def test_profile_model_is_fallback(tmp_path):
    provider = make_provider(agent_profile="grok-worker")
    with (
        patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path),
        patch("cli_agent_orchestrator.providers.grok_cli.shutil.which", return_value="/bin/grok"),
        patch(
            "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
            return_value=_profile(model="profile-model"),
        ),
    ):
        parts = shlex.split(provider._build_grok_command())
    assert parts[parts.index("--model") + 1] == "profile-model"
    provider.cleanup()


def test_restricted_command_uses_deny_by_default_with_native_denies(tmp_path):
    provider = make_provider(allowed_tools=["fs_read", "fs_list", "@cao-mcp-server"])
    with (
        patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path),
        patch("cli_agent_orchestrator.providers.grok_cli.shutil.which", return_value="/bin/grok"),
    ):
        parts = shlex.split(provider._build_grok_command())
    denied = [parts[index + 1] for index, part in enumerate(parts) if part == "--deny"]
    assert "Bash" in denied
    assert "Edit" in denied
    assert "Write" in denied
    assert "Read" not in denied
    assert "Grep" not in denied
    assert "--always-approve" not in parts
    assert parts[parts.index("--permission-mode") + 1] == "dontAsk"
    allowed = [parts[index + 1] for index, part in enumerate(parts) if part == "--allow"]
    assert {"Read", "NotebookRead", "Grep", "Glob", "MCPTool(cao-mcp-server__*)"} <= set(allowed)
    assert "Bash" not in allowed
    # Live Grok 1.0.0 probing showed --deny WebSearch alone is insufficient.
    assert "--disable-web-search" in parts
    provider.cleanup()


def test_restricted_command_allows_only_valid_configured_mcp_servers(tmp_path):
    profile = _profile(
        mcpServers={
            "inventory": {"command": "inventory-mcp"},
            "github.com": {"command": "github-mcp"},
            "1password": {"command": "password-mcp"},
            "invalid name": {"command": "unused-mcp"},
        }
    )
    provider = make_provider(
        agent_profile="grok-worker",
        allowed_tools=[
            "@cao-mcp-server",
            "@inventory",
            "@github.com",
            "@1password",
            "@builtin",
            "@*",
            "@foo*",
            "@foo)",
            "@invalid name",
            "@unconfigured",
        ],
    )
    with (
        patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path),
        patch("cli_agent_orchestrator.providers.grok_cli.shutil.which", return_value="/bin/grok"),
        patch(
            "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
            return_value=profile,
        ),
    ):
        parts = shlex.split(provider._build_grok_command())

    allowed = [parts[index + 1] for index, part in enumerate(parts) if part == "--allow"]
    expected_mcp_rules = {
        "MCPTool(cao-mcp-server__*)",
        "MCPTool(inventory__*)",
        "MCPTool(github.com__*)",
        "MCPTool(1password__*)",
    }
    assert expected_mcp_rules <= set(allowed)
    assert not any(
        candidate.startswith("MCPTool(") and candidate not in expected_mcp_rules
        for candidate in allowed
    )
    provider.cleanup()


def test_web_capability_omits_disable_flag(tmp_path):
    provider = make_provider(allowed_tools=["web_fetch"])
    with (
        patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path),
        patch("cli_agent_orchestrator.providers.grok_cli.shutil.which", return_value="/bin/grok"),
    ):
        parts = shlex.split(provider._build_grok_command())
    assert "--disable-web-search" not in parts
    provider.cleanup()


def test_explicit_empty_allowlist_denies_every_native_surface(tmp_path):
    provider = make_provider(allowed_tools=[])
    with (
        patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path),
        patch("cli_agent_orchestrator.providers.grok_cli.shutil.which", return_value="/bin/grok"),
    ):
        parts = shlex.split(provider._build_grok_command())
    denied = [parts[index + 1] for index, part in enumerate(parts) if part == "--deny"]
    assert denied == ["*"]
    assert parts[parts.index("--permission-mode") + 1] == "dontAsk"
    assert "--allow" not in parts
    assert "--disable-web-search" in parts
    provider.cleanup()


def test_missing_profile_is_not_wrapped():
    with patch(
        "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
        side_effect=FileNotFoundError("missing"),
    ):
        with pytest.raises(FileNotFoundError, match="missing"):
            make_provider(agent_profile="missing")._load_profile()


def test_malformed_profile_is_wrapped():
    with patch(
        "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
        side_effect=ValueError("bad yaml"),
    ):
        with pytest.raises(ProviderError, match="bad yaml"):
            make_provider(agent_profile="broken")._load_profile()


def test_private_home_and_atomic_mcp_config(tmp_path):
    provider = make_provider(terminal_id="terminal/with traversal ..")
    servers = {
        "cao-mcp-server": {
            "command": "/usr/bin/cao-mcp-server",
            "args": ["--flag", "unicode-✓"],
            "env": {"EXISTING": "value"},
            "timeout": 321,
        },
        "remote": {
            "url": "https://mcp.example.invalid/mcp",
            "type": "http",
            "headers": {"Authorization": "Bearer placeholder"},
        },
        "events": {
            "url": "https://mcp.example.invalid/events",
            "type": "sse",
        },
    }
    with patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path):
        home = provider._prepare_grok_home(servers)

    home.relative_to(tmp_path / "grok" / "terminals")
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    config = home / "config.toml"
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    text = config.read_text(encoding="utf-8")
    assert '[mcp_servers."cao-mcp-server"]' in text
    assert '"CAO_TERMINAL_ID" = "terminal/with traversal .."' in text
    assert '"EXISTING" = "value"' in text
    assert "startup_timeout_sec = 321" in text
    assert "tool_timeout_sec = 321" in text
    assert 'type = "http"\nurl = "https://mcp.example.invalid/mcp"' in text
    assert 'type = "sse"\nurl = "https://mcp.example.invalid/events"' in text
    assert '[mcp_servers."remote".headers]' in text
    assert "grok mcp add" not in text
    provider.cleanup()
    assert not home.exists()


def test_auth_is_symlinked_not_copied(tmp_path):
    fake_user_home = tmp_path / "user"
    auth = fake_user_home / ".grok" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"secret":"not-copied"}', encoding="utf-8")
    cao_home = tmp_path / "cao"
    provider = make_provider()
    with (
        patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", cao_home),
        patch("cli_agent_orchestrator.providers.grok_cli.Path.home", return_value=fake_user_home),
    ):
        home = provider._prepare_grok_home(None)
    link = home / "auth.json"
    assert link.is_symlink()
    assert link.resolve() == auth.resolve()
    provider.cleanup()
    assert auth.read_text(encoding="utf-8") == '{"secret":"not-copied"}'


def test_auth_honors_existing_custom_grok_home(tmp_path, monkeypatch):
    source_home = tmp_path / "configured-grok-home"
    source_home.mkdir()
    auth = source_home / "auth.json"
    auth.write_text('{"credential":"placeholder"}', encoding="utf-8")
    monkeypatch.setenv("GROK_HOME", str(source_home))
    provider = make_provider()
    with patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path / "cao"):
        isolated_home = provider._prepare_grok_home(None)
    assert (isolated_home / "auth.json").resolve() == auth.resolve()
    provider.cleanup()


def test_distinct_terminals_get_distinct_homes(tmp_path):
    first = make_provider(terminal_id="one")
    second = make_provider(terminal_id="two")
    with patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path):
        first_home = first._prepare_grok_home(None)
        second_home = second._prepare_grok_home(None)
    assert first_home != second_home
    assert (first_home / "config.toml").exists()
    assert (second_home / "config.toml").exists()
    first.cleanup()
    assert not first_home.exists()
    assert second_home.exists()
    second.cleanup()


def test_cleanup_is_idempotent(tmp_path):
    provider = make_provider()
    with patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path):
        provider._prepare_grok_home(None)
    provider.cleanup()
    provider.cleanup()
    assert provider.grok_home is None


def test_cleanup_reconstructs_deterministic_home_after_restart(tmp_path):
    original = make_provider(terminal_id="restored-terminal")
    with patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path):
        home = original._prepare_grok_home(None)
        restored = make_provider(terminal_id="restored-terminal")
        assert restored.grok_home is None
        restored.cleanup()
    assert not home.exists()
    assert restored.grok_home is None


def test_cleanup_refuses_tampered_path_outside_managed_root(tmp_path):
    provider = make_provider()
    outside = tmp_path / "outside"
    outside.mkdir()
    with patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path):
        provider._grok_home = outside
        provider.cleanup()
    assert outside.exists()


def test_cleanup_unlinks_managed_home_symlink_without_following_target(tmp_path):
    provider = make_provider()
    target = tmp_path / "auth-source"
    target.mkdir()
    target_file = target / "keep.txt"
    target_file.write_text("keep", encoding="utf-8")
    with patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path):
        home = provider._home_path()
        home.parent.mkdir(parents=True)
        home.symlink_to(target, target_is_directory=True)
        provider.cleanup()
    assert not home.exists()
    assert target_file.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("symlinked_ancestor", ["cao_home", "grok", "terminals"])
def test_cleanup_refuses_symlinked_managed_ancestor(tmp_path, symlinked_ancestor):
    """Never let a lexical CAO path escape through a symlinked ancestor."""

    provider = make_provider(terminal_id=f"symlinked-{symlinked_ancestor}")
    configured_home = tmp_path / "configured-cao-home"
    outside = tmp_path / "outside"
    outside.mkdir()

    if symlinked_ancestor == "cao_home":
        real_home = outside / "real-cao-home"
        real_home.mkdir()
        configured_home.symlink_to(real_home, target_is_directory=True)
        managed_root = real_home / "grok" / "terminals"
    elif symlinked_ancestor == "grok":
        configured_home.mkdir()
        grok_target = outside / "grok"
        grok_target.mkdir()
        (configured_home / "grok").symlink_to(grok_target, target_is_directory=True)
        managed_root = grok_target / "terminals"
    else:
        (configured_home / "grok").mkdir(parents=True)
        terminals_target = outside / "terminals"
        terminals_target.mkdir()
        (configured_home / "grok" / "terminals").symlink_to(
            terminals_target, target_is_directory=True
        )
        managed_root = terminals_target

    with patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", configured_home):
        escaped_home = managed_root / provider._home_path().name
        escaped_home.mkdir(parents=True)
        sentinel = escaped_home / "must-not-delete"
        sentinel.write_text("keep", encoding="utf-8")

        provider.cleanup()

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_url_mcp_rejects_unknown_transport():
    with pytest.raises(ProviderError, match="unsupported URL transport"):
        make_provider()._render_mcp_config(
            {"unknown": {"url": "https://mcp.example.invalid", "type": "websocket"}}
        )


def test_cleanup_failure_keeps_home_retryable(tmp_path):
    provider = make_provider()
    with patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path):
        home = provider._prepare_grok_home(None)
    with patch(
        "cli_agent_orchestrator.providers.grok_cli.shutil.rmtree",
        side_effect=OSError("busy"),
    ):
        provider.cleanup()
    assert provider.grok_home == home
    provider.cleanup()
    assert provider.grok_home is None
    assert not home.exists()


def test_cleanup_stops_residual_process_before_removing_home(tmp_path):
    provider = make_provider()
    proc = MagicMock()
    with patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path):
        home = provider._prepare_grok_home(None)
        with (
            patch.object(GrokCliProvider, "_pids_using_home", side_effect=[{12345}, set()]),
            patch.object(GrokCliProvider, "_inspect_home_process", return_value=proc),
        ):
            provider.cleanup()

    proc.send_signal.assert_called_once_with(signal.SIGTERM)
    assert not home.exists()


def test_cleanup_retains_home_when_residual_process_cannot_stop(tmp_path):
    provider = make_provider()
    with patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path):
        home = provider._prepare_grok_home(None)
        with patch.object(provider, "_stop_home_processes", return_value=False):
            provider.cleanup()

    assert home.exists()
    assert provider.grok_home == home


def test_cleanup_retains_home_when_process_scan_is_unavailable(tmp_path):
    provider = make_provider()
    with patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path):
        home = provider._prepare_grok_home(None)
        with (
            patch(
                "cli_agent_orchestrator.providers.grok_cli.psutil.pids",
                side_effect=psutil.Error("blocked"),
            ),
            patch("cli_agent_orchestrator.providers.grok_cli.os.kill") as kill,
            patch("cli_agent_orchestrator.providers.grok_cli.shutil.rmtree") as rmtree,
        ):
            provider.cleanup()

    kill.assert_not_called()
    rmtree.assert_not_called()
    assert home.exists()
    assert provider.grok_home == home


def test_cleanup_is_retryable_when_portable_process_inspection_is_unavailable(tmp_path):
    """Simulate a macOS/permission failure without depending on Linux ``/proc``."""

    provider = make_provider()
    with patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path):
        home = provider._prepare_grok_home(None)
        with patch.object(GrokCliProvider, "_pids_using_home", side_effect=[None, set()]):
            assert provider.cleanup() is False
            assert home.exists()
            # The next lifecycle attempt gets a fresh process enumeration and
            # completes; this is the contract ProviderManager relies on.
            assert provider.cleanup() is True
    assert not home.exists()


def test_home_process_scan_fails_closed_for_unreadable_same_user_environment(tmp_path):
    with (
        patch("cli_agent_orchestrator.providers.grok_cli.psutil.pids", return_value=[987654]),
        patch.object(GrokCliProvider, "_pid_uses_home", return_value=None),
    ):
        assert GrokCliProvider._pids_using_home(tmp_path) is None


def test_home_process_fails_closed_when_candidate_environment_is_protected(tmp_path):
    proc = MagicMock()
    proc.uids.return_value.effective = os.geteuid()
    proc.name.return_value = "grok"
    proc.exe.return_value = "/usr/local/bin/grok"
    proc.cmdline.return_value = ["grok"]
    proc.environ.side_effect = psutil.AccessDenied(pid=12345)
    with patch("cli_agent_orchestrator.providers.grok_cli.psutil.Process", return_value=proc):
        assert GrokCliProvider._pid_uses_home(12345, tmp_path) is None


@pytest.mark.parametrize("blocked_attribute", ["uids", "exe", "cmdline"])
def test_cleanup_retains_home_when_process_identity_inspection_is_protected(
    tmp_path, blocked_attribute
):
    """Identity metadata is uncertain on macOS too, so cleanup must fail closed."""

    provider = make_provider()
    proc = MagicMock()
    proc.uids.return_value.effective = os.geteuid()
    proc.name.return_value = "grok"
    proc.exe.return_value = "/usr/local/bin/grok"
    proc.cmdline.return_value = ["grok"]
    getattr(proc, blocked_attribute).side_effect = psutil.AccessDenied(pid=12345)

    with (
        patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path),
        patch("cli_agent_orchestrator.providers.grok_cli.psutil.pids", return_value=[12345]),
        patch("cli_agent_orchestrator.providers.grok_cli.psutil.Process", return_value=proc),
        patch("cli_agent_orchestrator.providers.grok_cli.shutil.rmtree") as rmtree,
    ):
        home = provider._prepare_grok_home(None)
        assert provider.cleanup() is False

    proc.send_signal.assert_not_called()
    rmtree.assert_not_called()
    assert home.exists()


def test_home_process_ignores_different_uid_before_environment_inspection(tmp_path):
    proc = MagicMock()
    proc.uids.return_value.effective = os.geteuid() + 1

    with patch("cli_agent_orchestrator.providers.grok_cli.psutil.Process", return_value=proc):
        assert GrokCliProvider._pid_uses_home(12345, tmp_path) is False

    proc.exe.assert_not_called()


def test_home_process_ignores_process_that_exited_before_inspection(tmp_path):
    with patch(
        "cli_agent_orchestrator.providers.grok_cli.psutil.Process",
        side_effect=psutil.NoSuchProcess(pid=12345),
    ):
        assert GrokCliProvider._pid_uses_home(12345, tmp_path) is False


def test_home_process_stop_rechecks_home_before_signalling_reused_pid(tmp_path):
    with (
        patch.object(GrokCliProvider, "_pids_using_home", side_effect=[{12345}, set()]),
        patch.object(GrokCliProvider, "_inspect_home_process", return_value=False),
    ):
        assert GrokCliProvider._stop_home_processes(tmp_path) is True


def test_home_process_stop_does_not_signal_reused_pid_after_identity_verification(tmp_path):
    """psutil's process object rejects PID reuse between inspect and signal."""
    proc = MagicMock()
    proc.send_signal.side_effect = psutil.NoSuchProcess(pid=12345)
    with (
        patch.object(GrokCliProvider, "_pids_using_home", side_effect=[{12345}, set()]),
        patch.object(GrokCliProvider, "_inspect_home_process", return_value=proc),
        patch("cli_agent_orchestrator.providers.grok_cli.os.kill") as raw_kill,
    ):
        assert GrokCliProvider._stop_home_processes(tmp_path) is True

    proc.send_signal.assert_called_once_with(signal.SIGTERM)
    raw_kill.assert_not_called()


def test_home_process_recognizes_exact_cao_mcp_argv_with_private_home(tmp_path):
    proc = MagicMock()
    proc.uids.return_value.effective = os.geteuid()
    proc.name.return_value = "python3"
    proc.exe.return_value = "/usr/bin/python3"
    proc.cmdline.return_value = ["python3", "/usr/local/bin/cao-mcp-server"]
    proc.environ.return_value = {"GROK_HOME": str(tmp_path)}
    with (patch("cli_agent_orchestrator.providers.grok_cli.psutil.Process", return_value=proc),):
        assert GrokCliProvider._pid_uses_home(12345, tmp_path) is True


@pytest.mark.parametrize(
    "cmdline",
    [
        b"python3\0/tmp/cao-mcp-server-evil\0",
        b"python3\0-c\0cao-mcp-server\0",
    ],
)
def test_home_process_rejects_nonexact_cao_mcp_argv_token(tmp_path, cmdline):
    proc = MagicMock()
    proc.uids.return_value.effective = os.geteuid()
    proc.name.return_value = "python3"
    proc.exe.return_value = "/usr/bin/python3"
    proc.cmdline.return_value = [item.decode() for item in cmdline.split(b"\0") if item]
    with (patch("cli_agent_orchestrator.providers.grok_cli.psutil.Process", return_value=proc),):
        assert GrokCliProvider._pid_uses_home(12345, tmp_path) is False


def test_home_process_rejects_arbitrary_python_even_with_matching_home(tmp_path):
    proc = MagicMock()
    proc.uids.return_value.effective = os.geteuid()
    proc.name.return_value = "python3"
    proc.exe.return_value = "/usr/bin/python3"
    proc.cmdline.return_value = ["python3", "-c"]
    with (patch("cli_agent_orchestrator.providers.grok_cli.psutil.Process", return_value=proc),):
        assert GrokCliProvider._pid_uses_home(12345, tmp_path) is False


def test_home_process_rejects_non_grok_executable_with_matching_home(tmp_path):
    proc = MagicMock()
    proc.uids.return_value.effective = os.geteuid()
    proc.exe.return_value = "/tmp/notgrok-helper"
    proc.cmdline.return_value = ["notgrok-helper"]
    proc.environ.return_value = {"GROK_HOME": str(tmp_path)}
    with patch("cli_agent_orchestrator.providers.grok_cli.psutil.Process", return_value=proc):
        assert GrokCliProvider._pid_uses_home(12345, tmp_path) is False


@pytest.mark.asyncio
async def test_startup_trust_screen_fails_explicitly_without_auto_acceptance():
    provider = make_provider()
    trust_screen = (
        "Do you trust the contents of this directory?\n"
        "Grok Build may run or modify contents in this directory, posing security risks.\n"
        "Yes, proceed  y\nNo, quit  n"
    )
    with (
        patch.object(status_monitor, "get_buffer", return_value=trust_screen),
        patch.object(status_monitor, "get_status"),
    ):
        with pytest.raises(ProviderError, match="does not automatically trust"):
            await provider._wait_for_startup_ready(timeout=1)


@pytest.mark.asyncio
async def test_startup_ready_accepts_idle_status():
    provider = make_provider()
    with (
        patch.object(status_monitor, "get_buffer", return_value="normal composer"),
        patch.object(status_monitor, "get_status", return_value=TerminalStatus.IDLE),
    ):
        await provider._wait_for_startup_ready(timeout=1)


@pytest.mark.asyncio
async def test_initialize_success_is_async_and_repairs_config_mode(tmp_path):
    provider = make_provider()
    backend = MagicMock()
    event_loop_progressed = False

    async def progress_loop():
        nonlocal event_loop_progressed
        await asyncio.sleep(0)
        event_loop_progressed = True

    with (
        patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path),
        patch("cli_agent_orchestrator.providers.grok_cli.shutil.which", return_value="/bin/grok"),
        patch(
            "cli_agent_orchestrator.providers.grok_cli.wait_for_shell",
            new=AsyncMock(return_value=True),
        ),
        patch.object(provider, "_wait_for_startup_ready", new=AsyncMock()),
        patch("cli_agent_orchestrator.providers.grok_cli.get_backend", return_value=backend),
        patch("cli_agent_orchestrator.services.status_monitor.status_monitor.notify_input_sent"),
    ):
        result, _ = await asyncio.gather(provider.initialize(), progress_loop())
    assert result is True
    assert event_loop_progressed is True
    # notify_input_sent only arms StatusMonitor stickiness. A CLI launch is not
    # a user task and must not increment the provider's turn counter.
    assert provider._turns == 0
    backend.send_keys.assert_called_once()
    assert stat.S_IMODE((provider.grok_home / "config.toml").stat().st_mode) == 0o600
    provider.cleanup()


@pytest.mark.asyncio
async def test_initialize_shell_timeout_cleans_partial_state(tmp_path):
    provider = make_provider()
    with (
        patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path),
        patch(
            "cli_agent_orchestrator.providers.grok_cli.wait_for_shell",
            new=AsyncMock(return_value=False),
        ),
    ):
        with pytest.raises(TimeoutError, match="Shell initialization"):
            await provider.initialize()
    assert provider.grok_home is None


@pytest.mark.asyncio
async def test_initialize_cli_timeout_removes_generated_home(tmp_path):
    provider = make_provider()
    backend = MagicMock()
    with (
        patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path),
        patch("cli_agent_orchestrator.providers.grok_cli.shutil.which", return_value="/bin/grok"),
        patch(
            "cli_agent_orchestrator.providers.grok_cli.wait_for_shell",
            new=AsyncMock(return_value=True),
        ),
        patch.object(
            provider,
            "_wait_for_startup_ready",
            new=AsyncMock(side_effect=TimeoutError("Grok CLI initialization timed out after 60s")),
        ),
        patch("cli_agent_orchestrator.providers.grok_cli.get_backend", return_value=backend),
        patch("cli_agent_orchestrator.services.status_monitor.status_monitor.notify_input_sent"),
    ):
        with pytest.raises(TimeoutError, match="Grok CLI initialization"):
            await provider.initialize()
    assert provider.grok_home is None


@pytest.mark.asyncio
async def test_initialize_failure_offloads_recursive_cleanup(tmp_path):
    provider = make_provider()
    backend = MagicMock()
    original_cleanup = provider.cleanup
    cleanup_threaded = False

    async def observing_to_thread(function, *args, **kwargs):
        nonlocal cleanup_threaded
        if function == original_cleanup:
            cleanup_threaded = True
        return function(*args, **kwargs)

    with (
        patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path),
        patch("cli_agent_orchestrator.providers.grok_cli.shutil.which", return_value="/bin/grok"),
        patch(
            "cli_agent_orchestrator.providers.grok_cli.wait_for_shell",
            new=AsyncMock(return_value=True),
        ),
        patch.object(
            provider,
            "_wait_for_startup_ready",
            new=AsyncMock(side_effect=TimeoutError("Grok CLI initialization timed out after 60s")),
        ),
        patch("cli_agent_orchestrator.providers.grok_cli.get_backend", return_value=backend),
        patch("cli_agent_orchestrator.providers.grok_cli.asyncio.to_thread", observing_to_thread),
        patch("cli_agent_orchestrator.services.status_monitor.status_monitor.notify_input_sent"),
    ):
        with pytest.raises(TimeoutError, match="Grok CLI initialization"):
            await provider.initialize()
    assert cleanup_threaded is True


def test_atomic_write_repairs_existing_permissive_mode(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text("old", encoding="utf-8")
    os.chmod(target, 0o664)
    make_provider()._atomic_write_private(target, "new\n")
    assert target.read_text(encoding="utf-8") == "new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
