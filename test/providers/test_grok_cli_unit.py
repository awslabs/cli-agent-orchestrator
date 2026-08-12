"""Unit tests for the official xAI Grok Build CLI provider."""

import asyncio
import os
import shlex
import stat
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider, ProviderError

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
    assert parts[:3] == ["env", f"GROK_HOME={provider.grok_home}", "/opt/grok/bin/grok"]
    assert "--no-alt-screen" in parts
    assert "--always-approve" in parts
    assert "--no-subagents" in parts
    assert "--deny" not in parts
    assert "--disable-web-search" not in parts
    provider.cleanup()


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


def test_restricted_command_has_native_denies_and_web_kill_switch(tmp_path):
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
    # Live Grok 1.0.0 probing showed --deny WebSearch alone is insufficient.
    assert "--disable-web-search" in parts
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
    assert {"Bash", "Read", "Edit", "Write", "Grep", "WebFetch", "WebSearch"} <= set(denied)
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
            "headers": {"Authorization": "Bearer placeholder"},
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
        patch(
            "cli_agent_orchestrator.providers.grok_cli.wait_until_status",
            new=AsyncMock(return_value=True),
        ),
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
        patch(
            "cli_agent_orchestrator.providers.grok_cli.wait_until_status",
            new=AsyncMock(return_value=False),
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
        patch(
            "cli_agent_orchestrator.providers.grok_cli.wait_until_status",
            new=AsyncMock(return_value=False),
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
