"""Unit tests for the MiniMax Code provider."""

import hashlib
import json
import os
import shlex
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.minimax_code import MiniMaxCodeProvider, ProviderError

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_provider(**kwargs) -> MiniMaxCodeProvider:
    return MiniMaxCodeProvider(
        terminal_id=kwargs.pop("terminal_id", "deadbeef"),
        session_name="session",
        window_name="window",
        agent_profile=kwargs.pop("agent_profile", "developer"),
        **kwargs,
    )


def test_prepare_runtime_isolates_auth_and_generates_private_mcp_plugin(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "source"
    (source / "cli-auth" / "nested").mkdir(parents=True)
    (source / "plugins" / "personal").mkdir(parents=True)
    (source / "config.yaml").write_text("modelSource: managed\n")
    (source / "local-runtime.auth.json").write_text('{"token":"secret"}')
    (source / "cli-auth" / "nested" / "session.json").write_text("secret-session")
    (source / "plugins" / "personal" / "plugin.json").write_text("not copied")
    monkeypatch.setenv("MINIMAX_DATA_DIR", str(source))

    cao_home = tmp_path / "cao"
    profile = AgentProfile(
        name="developer",
        description="Developer",
        mcpServers={
            "cao-orchestrator": {
                "command": "/opt/cao/bin/cao-mcp-server",
                "args": ["--stdio"],
                "env": {"EXISTING": "value"},
            },
            "remote-tools": {
                "type": "http",
                "url": "https://mcp.example.test/api",
                "headers": {"Authorization": "Bearer placeholder"},
            },
        },
    )
    provider = make_provider()

    with (
        patch("cli_agent_orchestrator.providers.minimax_code.CAO_HOME_DIR", cao_home),
        patch(
            "cli_agent_orchestrator.providers.minimax_code.load_agent_profile",
            return_value=profile,
        ),
    ):
        data_dir, _ = provider._prepare_runtime()

    digest = hashlib.sha256(b"deadbeef").hexdigest()
    assert data_dir == cao_home / "providers" / "minimax_code" / digest
    assert (data_dir / "config.yaml").read_text() == "modelSource: managed\n"
    assert (data_dir / "local-runtime.auth.json").read_text() == '{"token":"secret"}'
    assert (data_dir / "cli-auth" / "nested" / "session.json").read_text() == "secret-session"
    assert not (data_dir / "plugins" / "personal").exists()

    plugin = data_dir / "plugins" / "cao-orchestrator"
    manifest = json.loads((plugin / ".minimax-plugin" / "plugin.json").read_text())
    config = json.loads((plugin / "servers.mcp.json").read_text())
    server = config["mcpServers"]["cao-orchestrator"]
    remote = config["mcpServers"]["remote-tools"]
    assert manifest["mcpServers"] == ["servers.mcp.json"]
    assert server["command"] == "cao-mcp-server"
    assert server["args"] == ["--stdio"]
    assert server["env"]["CAO_TERMINAL_ID"] == "deadbeef"
    assert server["env"]["EXISTING"] == "value"
    assert server["env"]["PATH"].split(os.pathsep)[0] == "/opt/cao/bin"
    assert server["timeout"] == 600_000
    assert remote == {
        "type": "streamable-http",
        "url": "https://mcp.example.test/api",
        "headers": {"Authorization": "Bearer placeholder"},
        "description": "CAO-managed MCP server remote-tools",
        "timeout": 600_000,
    }
    assert (plugin / "icon.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((data_dir / "local-runtime.auth.json").stat().st_mode) == 0o600


def test_build_command_bootstraps_profile_skills_and_soft_tool_policy(tmp_path: Path):
    profile = AgentProfile(
        name="reviewer",
        description="Reviewer",
        system_prompt="Review changes carefully.",
    )
    provider = make_provider(
        agent_profile="reviewer",
        allowed_tools=["fs_read", "fs_list"],
        skill_prompt="## Available Skills\n- code-review",
    )

    with (
        patch("cli_agent_orchestrator.providers.minimax_code.CAO_HOME_DIR", tmp_path),
        patch(
            "cli_agent_orchestrator.providers.minimax_code.load_agent_profile",
            return_value=profile,
        ),
        patch(
            "cli_agent_orchestrator.providers.minimax_code.shutil.which",
            return_value="/usr/local/bin/mcode",
        ),
    ):
        command = provider._build_command()

    argv = shlex.split(command)
    assert argv[:4] == [
        "env",
        f"MINIMAX_DATA_DIR={provider._data_dir}",
        "TERM=xterm-256color",
        "/usr/local/bin/mcode",
    ]
    bootstrap = argv[4]
    assert "Review changes carefully." in bootstrap
    assert "## Available Skills" in bootstrap
    assert "fs_read, fs_list" in bootstrap
    assert "Reply exactly CAO_MCODE_READY" in bootstrap


@pytest.mark.parametrize(
    ("launch_model", "expected_model"),
    [("launch/model", "launch/model"), (None, "profile/model")],
)
def test_prepare_runtime_applies_model_to_terminal_local_config(
    tmp_path: Path, monkeypatch, launch_model: str | None, expected_model: str
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text("defaultModel: original/model\n")
    monkeypatch.setenv("MINIMAX_DATA_DIR", str(source))
    profile = AgentProfile(
        name="developer",
        description="Developer",
        model="profile/model",
    )
    provider = make_provider(model=launch_model)

    with (
        patch("cli_agent_orchestrator.providers.minimax_code.CAO_HOME_DIR", tmp_path / "cao"),
        patch(
            "cli_agent_orchestrator.providers.minimax_code.load_agent_profile",
            return_value=profile,
        ),
    ):
        data_dir, _ = provider._prepare_runtime()

    config = yaml.safe_load((data_dir / "config.yaml").read_text())
    assert config["defaultModel"] == expected_model


def test_prompt_submission_and_lifecycle_properties():
    provider = make_provider(agent_profile=None)
    assert provider.paste_enter_count == 1
    assert provider.accepts_input_while_processing is True
    # Deferred delivery confirmation must wait for observed current-turn
    # activity; a synthetic PROCESSING state can hide a dropped paste/Enter.
    assert provider.assume_processing_on_dispatch is False
    assert provider.blocks_orchestrated_input_while_waiting_user_answer is True
    assert provider.supports_screen_detection is True
    assert provider.supports_direct_status_probe is True
    assert provider.exit_cli() == "/exit"


def test_status_fixtures_and_completed_turn_latch():
    provider = make_provider(agent_profile=None)
    assert provider.get_status(load_fixture("minimax_code_idle.txt")) == TerminalStatus.IDLE
    assert (
        provider.get_status(load_fixture("minimax_code_processing.txt"))
        == TerminalStatus.PROCESSING
    )
    assert (
        provider.get_status(load_fixture("minimax_code_permission.txt"))
        == TerminalStatus.WAITING_USER_ANSWER
    )

    completed = load_fixture("minimax_code_completed.txt")
    assert provider.get_status(completed) == TerminalStatus.IDLE
    provider.mark_input_received()
    provider._last_dispatch_time = 0
    new_completed = completed.replace("Explain the result.", "Explain the updated result.").replace(
        "The integration is complete.", "The updated integration is complete."
    )
    assert provider.get_status(new_completed) == TerminalStatus.COMPLETED


def test_status_reports_error_and_unknown_states():
    provider = make_provider(agent_profile=None)
    assert provider.get_status("") == TerminalStatus.UNKNOWN
    assert provider.get_status("Fatal: authentication backend unavailable") == TerminalStatus.ERROR


def test_status_rejects_previous_completion_after_next_turn_dispatch():
    provider = make_provider(agent_profile=None)
    previous = load_fixture("minimax_code_completed.txt")
    assert provider.get_status(previous) == TerminalStatus.IDLE

    provider.mark_input_received()
    provider._last_dispatch_time = 0
    assert provider.get_status(previous) == TerminalStatus.PROCESSING
    assert (
        provider.get_status(load_fixture("minimax_code_processing.txt"))
        == TerminalStatus.PROCESSING
    )
    # A full-screen redraw may replay the previous completion after showing a
    # current-turn spinner. Activity alone does not make identical completion
    # content fresh.
    assert provider.get_status(previous) == TerminalStatus.PROCESSING

    current = previous + """\
 › Give a different answer.
 ● This is the new answer.
 └ Completed in 1s · ⚡ 20 tok/s
 Message · Enter send · Shift+Enter newline
"""
    assert provider.get_status(current) == TerminalStatus.COMPLETED


def test_status_buffer_reset_does_not_treat_stale_completion_as_started():
    """A retained completion after dispatch is not current-turn activity."""

    provider = make_provider(agent_profile=None)
    previous = load_fixture("minimax_code_completed.txt")
    assert provider.get_status(previous) == TerminalStatus.IDLE

    provider.notify_status_buffer_reset(1)
    provider.mark_input_received()

    # This is the capture-pane view used by deferred-submit recovery when the
    # paste/Enter was dropped. Reporting PROCESSING here would suppress every
    # retry even though MiniMax has emitted no bytes for the new turn.
    assert provider.get_status(previous) == TerminalStatus.IDLE


def test_status_buffer_reset_accepts_fresh_byte_identical_completion_after_activity():
    """A repeated prompt/answer can complete in a fresh buffer generation."""

    provider = make_provider(agent_profile=None)
    completed = load_fixture("minimax_code_completed.txt")
    processing = load_fixture("minimax_code_processing.txt")
    assert provider.get_status(completed) == TerminalStatus.IDLE

    provider.notify_status_buffer_reset(1)
    provider.mark_input_received()
    assert provider.get_status(processing) == TerminalStatus.PROCESSING

    # StatusMonitor's fresh rolling buffer now contains both the processing
    # frame and the newly emitted completion. Its prompt/answer bytes match the
    # prior turn, but observed activity ties them to the new generation.
    assert provider.get_status(f"{processing}\n{completed}") == TerminalStatus.COMPLETED


def test_status_completes_long_response_after_assistant_marker_leaves_viewport():
    provider = make_provider(agent_profile=None)
    previous = load_fixture("minimax_code_completed.txt")
    assert provider.get_status(previous) == TerminalStatus.IDLE
    provider.mark_input_received()
    assert (
        provider.get_status(load_fixture("minimax_code_processing.txt"))
        == TerminalStatus.PROCESSING
    )

    settled_viewport = "\n".join(
        [f"  report line {index}" for index in range(50)]
        + [
            "  └ Completed in 9s · ⚡ 97.4 tok/s",
            "  Message · Enter send · Shift+Enter newline",
        ]
    )
    assert provider.get_status(settled_viewport) == TerminalStatus.COMPLETED


def test_extract_last_message_returns_only_final_assistant_answer():
    provider = make_provider(agent_profile=None)
    assert (
        provider.extract_last_message_from_script(load_fixture("minimax_code_completed.txt"))
        == "The integration is complete.\nAll focused checks pass."
    )


def test_extract_last_message_ignores_empty_bottom_composer_on_second_turn():
    output = """\
 › Create square_first_turn.
 ● def square_first_turn(n):
       return n ** 2
 › Create cube_second_turn.
 ● def cube_second_turn(n):
       value = n ** 3

       return value
   └ Completed in 2s · ⚡ 42.5 tok/s
   Message · Enter send · Shift+Enter newline
 ›  \x1b[7m█\x1b[0m
 /workspace │ Full access │ ✦ MiniMax-M3 · Thinking On
"""
    assert make_provider(agent_profile=None).extract_last_message_from_script(output) == (
        "def cube_second_turn(n):\n    value = n ** 3\n\n    return value"
    )


@pytest.mark.parametrize("output", ["Message · Enter send", "●   \n└ Completed in 1s"])
def test_extract_last_message_rejects_missing_or_empty_response(output: str):
    with pytest.raises(ValueError, match="No MiniMax Code final response found"):
        make_provider(agent_profile=None).extract_last_message_from_script(output)


@pytest.mark.asyncio
async def test_initialize_launches_mcode_and_waits_for_bootstrap_completion():
    provider = make_provider(agent_profile=None)
    backend = patch("cli_agent_orchestrator.providers.minimax_code.get_backend").start()
    backend.return_value.send_keys.return_value = None
    try:
        with (
            patch.object(provider, "_build_command", return_value="mcode bootstrap"),
            patch(
                "cli_agent_orchestrator.providers.minimax_code.wait_for_shell",
                return_value=True,
            ) as wait_shell,
            patch.object(provider, "_wait_for_bootstrap_ready", return_value=True) as wait_status,
            patch(
                "cli_agent_orchestrator.services.status_monitor.status_monitor.notify_input_sent"
            ) as notify,
        ):
            assert await provider.initialize() is True
    finally:
        patch.stopall()

    wait_shell.assert_awaited_once()
    backend.return_value.send_keys.assert_called_once_with("session", "window", "mcode bootstrap")
    wait_status.assert_awaited_once_with(120.0)
    notify.assert_called_once_with("deadbeef")
    assert provider._initialized is True


@pytest.mark.asyncio
async def test_initialize_times_out_when_bootstrap_never_becomes_ready():
    provider = make_provider(agent_profile=None)
    with (
        patch.object(provider, "_build_command", return_value="mcode bootstrap"),
        patch(
            "cli_agent_orchestrator.providers.minimax_code.wait_for_shell",
            return_value=True,
        ),
        patch("cli_agent_orchestrator.providers.minimax_code.get_backend"),
        patch.object(provider, "_wait_for_bootstrap_ready", return_value=False),
        patch.object(provider, "cleanup") as cleanup,
    ):
        with pytest.raises(TimeoutError, match="MiniMax Code initialization timed out"):
            await provider.initialize()

    cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_initialize_failure_removes_private_runtime(tmp_path: Path):
    provider = make_provider(agent_profile=None)
    cao_home = tmp_path / "cao"
    with patch("cli_agent_orchestrator.providers.minimax_code.CAO_HOME_DIR", cao_home):
        private_dir = provider._data_dir_path()
        private_dir.mkdir(parents=True)
        (private_dir / "token").write_text("secret")
        provider._data_dir = private_dir

        with patch(
            "cli_agent_orchestrator.providers.minimax_code.wait_for_shell", return_value=False
        ):
            with pytest.raises(TimeoutError, match="Shell initialization timed out"):
                await provider.initialize()

    assert not private_dir.exists()


def test_cleanup_refuses_symlinked_managed_ancestor(tmp_path: Path):
    cao_home = tmp_path / "cao"
    managed_parent = cao_home / "providers"
    managed_parent.mkdir(parents=True)
    external = tmp_path / "external"
    digest = hashlib.sha256(b"deadbeef").hexdigest()
    escaped_data_dir = external / digest
    escaped_data_dir.mkdir(parents=True)
    victim = escaped_data_dir / "must-survive.txt"
    victim.write_text("outside CAO")
    (managed_parent / "minimax_code").symlink_to(external, target_is_directory=True)

    provider = make_provider(agent_profile=None)
    with patch("cli_agent_orchestrator.providers.minimax_code.CAO_HOME_DIR", cao_home):
        with pytest.raises(ProviderError, match="managed MiniMax Code data directory"):
            provider.cleanup()

    assert victim.read_text() == "outside CAO"


def test_prepare_runtime_refuses_symlinked_managed_ancestor(tmp_path: Path):
    cao_home = tmp_path / "cao"
    managed_parent = cao_home / "providers"
    managed_parent.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    (managed_parent / "minimax_code").symlink_to(external, target_is_directory=True)

    provider = make_provider(agent_profile=None)
    with patch("cli_agent_orchestrator.providers.minimax_code.CAO_HOME_DIR", cao_home):
        with pytest.raises(ProviderError, match="managed MiniMax Code data directory"):
            provider._prepare_runtime()

    assert list(external.iterdir()) == []
