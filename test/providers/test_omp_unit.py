"""Fixture-backed tests for the OMP provider."""

import asyncio
import json
import shlex
import stat
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from cli_agent_orchestrator.constants import SECURITY_PROMPT
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.omp import OmpProvider, ProviderError

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_profile(**kwargs) -> AgentProfile:
    return AgentProfile(name="analyst", description="test profile", **kwargs)


def make_provider(**kwargs) -> OmpProvider:
    return OmpProvider(
        terminal_id="terminal-123",
        session_name="test-session",
        window_name="window-0",
        **kwargs,
    )


@pytest.fixture(autouse=True)
def native_status_is_unavailable():
    """Exercise the fixture detector, not a backend-native shortcut."""
    with patch.object(OmpProvider, "_resolve_native_status", return_value=None):
        yield


# ── Capabilities and lifecycle state ────────────────────────────────────


def test_provider_capabilities_and_exit_command():
    provider = make_provider()

    assert provider.paste_enter_count == 1
    assert provider.paste_submit_delay == 0.3
    assert provider.supports_screen_detection is True
    assert provider.supports_direct_status_probe is False
    assert provider.blocks_orchestrated_input_while_waiting_user_answer is True
    assert provider.accepts_input_while_processing is False
    assert provider.exit_cli() == "/exit"


def test_omp_restrictions_are_explicitly_soft():
    from cli_agent_orchestrator.models.provider import ProviderType
    from cli_agent_orchestrator.services.terminal_service import (
        RUNTIME_SKILL_PROMPT_PROVIDERS,
        SOFT_ENFORCEMENT_PROVIDERS,
    )
    from cli_agent_orchestrator.utils.tool_mapping import TOOL_MAPPING

    assert ProviderType.OMP.value in RUNTIME_SKILL_PROMPT_PROVIDERS
    assert ProviderType.OMP.value in SOFT_ENFORCEMENT_PROVIDERS
    assert ProviderType.OMP.value not in TOOL_MAPPING


def test_status_fixtures_and_turn_split():
    provider = make_provider()

    assert provider.get_status(load_fixture("omp_idle.txt")) == TerminalStatus.IDLE
    assert provider.get_status(load_fixture("omp_processing.txt")) == TerminalStatus.PROCESSING
    assert (
        provider.get_status(load_fixture("omp_waiting.txt")) == TerminalStatus.WAITING_USER_ANSWER
    )
    assert provider.get_status(load_fixture("omp_error.txt")) == TerminalStatus.ERROR
    assert provider.get_status(load_fixture("omp_prose.txt")) == TerminalStatus.IDLE
    assert provider.get_status("Error: failed to parse input\n╰─ ready ─╯\n") == TerminalStatus.IDLE

    provider.mark_input_received()
    assert provider.get_status(load_fixture("omp_completed.txt")) == TerminalStatus.COMPLETED


def test_status_unknown_and_native_fallback():
    provider = make_provider()
    assert provider.get_status("") == TerminalStatus.UNKNOWN
    assert provider.get_status(None) == TerminalStatus.UNKNOWN

    with patch.object(provider, "_resolve_native_status", return_value=TerminalStatus.PROCESSING):
        assert provider.get_status(load_fixture("omp_idle.txt")) == TerminalStatus.PROCESSING


def test_raw_cursor_redraw_fixture_is_processing():
    assert (
        make_provider().get_status(load_fixture("omp_processing.raw.txt"))
        == TerminalStatus.PROCESSING
    )


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("Allow tool: bash", TerminalStatus.IDLE),
        ("Error: No model selected.", TerminalStatus.IDLE),
        ("Working… ⟨esc⟩", TerminalStatus.IDLE),
    ],
)
def test_later_ready_status_line_makes_active_marker_stale(marker, expected):
    provider = make_provider()
    buffer = f"{marker}\nin: 1 out: 2 t: 1s tok/s: 1"

    assert provider.get_status(buffer) == expected


@pytest.mark.parametrize(
    "marker",
    [
        "Allow tool: bash",
        "Working… ⟨esc⟩",
        "Error: No model selected.",
    ],
)
def test_later_ready_frame_makes_active_marker_stale_without_status_line(marker):
    provider = make_provider()
    frame = f"{marker}\n╰─ ready ─╯"

    assert provider.get_status(frame) == TerminalStatus.IDLE
    provider.mark_input_received()
    assert provider.get_status(frame) == TerminalStatus.COMPLETED
    assert provider.get_status_from_screen([marker, "╰─ ready ─╯"]) == (TerminalStatus.COMPLETED)


@pytest.mark.parametrize(
    "fixture",
    ["omp_processing.raw.txt", "omp_waiting.txt", "omp_error.txt"],
)
def test_completed_transcript_retires_stale_raw_markers(fixture):
    provider = make_provider()
    provider.mark_input_received()
    output = load_fixture(fixture) + "\n" + load_fixture("omp_completed.txt")

    assert provider.get_status(output) == TerminalStatus.COMPLETED


def test_live_model_error_companion_survives_its_ready_footer():
    assert make_provider().get_status(load_fixture("omp_error.txt")) == TerminalStatus.ERROR


@pytest.mark.parametrize(
    "output",
    [
        "Allow tool: bash\n❯ Approve\nin: 1 out: 2 t: 1s tok/s: 1",
        "Error: No model selected.\nUse /login, set an API key\nin: 1 out: 2 t: 1s tok/s: 1",
        "⠋ Working… ⟨esc⟩\nin: 1 out: 2 t: 1s tok/s: 1",
    ],
)
def test_later_status_line_is_stale_even_with_live_companion(output):
    assert make_provider().get_status(output) == TerminalStatus.IDLE


def test_live_spinner_wins_over_complete_stale_approval_block():
    output = load_fixture("omp_waiting.txt") + "\n╰─ ready ─╯\n⠋ Working… ⟨esc⟩"

    assert make_provider().get_status(output) == TerminalStatus.PROCESSING


def test_live_spinner_wins_over_complete_stale_model_error_block():
    output = load_fixture("omp_error.txt") + "\n╰─ ready ─╯\n⠋ Working… ⟨esc⟩"

    assert make_provider().get_status(output) == TerminalStatus.PROCESSING


def test_later_live_approval_wins_over_stale_error_block():
    output = load_fixture("omp_error.txt") + "\n╰─ ready ─╯\n" + load_fixture("omp_waiting.txt")

    assert make_provider().get_status(output) == TerminalStatus.WAITING_USER_ANSWER


def test_screen_detection_uses_same_precedence():
    provider = make_provider()
    assert (
        provider.get_status_from_screen(["Allow tool: bash", "in: 1 out: 2 t: 1s tok/s: 1"])
        == TerminalStatus.IDLE
    )
    assert provider.get_status_from_screen(["Working… ⟨esc⟩"]) == TerminalStatus.PROCESSING

    provider.mark_input_received()
    assert (
        provider.get_status_from_screen(["in: 1 out: 2 cache 1 t: 1s tok/s: 1"])
        == TerminalStatus.COMPLETED
    )


def test_raw_status_reports_error_when_omp_exits():
    provider = make_provider()
    provider._initialized = True
    provider.shell_baseline = "zsh"
    with patch("cli_agent_orchestrator.providers.omp.get_backend") as backend:
        backend.return_value.get_pane_current_command.return_value = "zsh"

        assert (
            provider.get_status("Allow tool: bash\n╰─ stale ready frame ─╯") == TerminalStatus.ERROR
        )

    backend.return_value.get_pane_current_command.assert_called_once_with(
        "test-session", "window-0"
    )


def test_rendered_screen_status_reports_error_when_omp_exits():
    provider = make_provider()
    provider._initialized = True
    provider.shell_baseline = "zsh"
    with patch("cli_agent_orchestrator.providers.omp.get_backend") as backend:
        backend.return_value.get_pane_current_command.return_value = "zsh"

        assert provider.get_status_from_screen(["Allow tool: bash", "╰─ ready ─╯"]) == (
            TerminalStatus.ERROR
        )

    backend.return_value.get_pane_current_command.assert_called_once_with(
        "test-session", "window-0"
    )


def test_live_omp_command_does_not_trigger_shell_exit_error():
    provider = make_provider()
    provider._initialized = True
    provider.shell_baseline = "zsh"
    with patch("cli_agent_orchestrator.providers.omp.get_backend") as backend:
        backend.return_value.get_pane_current_command.return_value = "omp"

        assert provider.get_status("Working… ⟨esc⟩") == TerminalStatus.PROCESSING

    backend.return_value.get_pane_current_command.assert_called_once_with(
        "test-session", "window-0"
    )


@pytest.mark.parametrize(
    ("initialized", "baseline"),
    [(False, "zsh"), (True, None)],
)
def test_shell_exit_probe_skips_uninitialized_or_missing_baseline(initialized, baseline):
    provider = make_provider()
    provider._initialized = initialized
    provider.shell_baseline = baseline
    with patch("cli_agent_orchestrator.providers.omp.get_backend") as backend:
        assert provider.get_status("╰─ ready ─╯") == TerminalStatus.IDLE

    backend.return_value.get_pane_current_command.assert_not_called()


# ── Launch command and generated artifacts ──────────────────────────────


def _command_parts(provider: OmpProvider) -> list[str]:
    with patch(
        "cli_agent_orchestrator.providers.omp.shutil.which", return_value="/usr/local/bin/omp"
    ):
        return shlex.split(provider._build_omp_command())


def test_command_omits_empty_context_and_preserves_native_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.providers.omp.CAO_HOME_DIR", tmp_path)

    parts = _command_parts(make_provider())

    assert parts == ["omp"]
    assert not (tmp_path / "tmp" / "omp").exists()


def test_command_prefers_explicit_model_and_writes_private_append_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.providers.omp.CAO_HOME_DIR", tmp_path)
    profile = make_profile(system_prompt="Profile role", model="profile-model")
    provider = make_provider(
        agent_profile="analyst",
        allowed_tools=["fs_read", "@cao-mcp-server"],
        skill_prompt="## Available Skills\n- test-skill",
        model="explicit model",
    )

    with (
        patch(
            "cli_agent_orchestrator.providers.omp.shutil.which", return_value="/usr/local/bin/omp"
        ),
        patch(
            "cli_agent_orchestrator.providers.omp.load_agent_profile", return_value=profile
        ) as load,
    ):
        parts = shlex.split(provider._build_omp_command())

    load.assert_called_once_with("analyst")
    assert parts[:3] == ["omp", "--model", "explicit model"]
    assert "--append-system-prompt" in parts
    assert "--system-prompt" not in parts
    assert not {
        "--profile",
        "--config",
        "--no-tools",
        "--tools",
        "--no-skills",
        "--no-rules",
        "--no-extensions",
        "--auto-approve",
        "--approval-mode",
    }.intersection(parts)

    context_path = Path(parts[parts.index("--append-system-prompt") + 1])
    assert context_path.read_text(encoding="utf-8") == (
        SECURITY_PROMPT
        + "\nYou only have access to these tools: fs_read, @cao-mcp-server\n"
        + "Profile role\n\n## Available Skills\n- test-skill"
    )
    assert stat.S_IMODE(context_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(context_path.parent.stat().st_mode) == 0o700


def test_command_uses_profile_model_when_no_explicit_model(tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.providers.omp.CAO_HOME_DIR", tmp_path)
    profile = make_profile(model="profile-model")
    provider = make_provider(agent_profile="analyst")

    with (
        patch(
            "cli_agent_orchestrator.providers.omp.shutil.which", return_value="/usr/local/bin/omp"
        ),
        patch("cli_agent_orchestrator.providers.omp.load_agent_profile", return_value=profile),
    ):
        assert shlex.split(provider._build_omp_command()) == ["omp", "--model", "profile-model"]


def test_extension_root_merges_mcp_without_overriding_explicit_terminal_id(tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.providers.omp.CAO_HOME_DIR", tmp_path)
    profile = make_profile(
        mcpServers={
            "cao": {"command": "cao-mcp-server", "args": ["--stdio"], "env": {"KEEP": "yes"}},
            "custom": {"command": "custom-mcp", "env": {"CAO_TERMINAL_ID": "explicit"}},
            "remote": {"type": "http", "url": "https://mcp.example.test"},
        }
    )
    provider = make_provider(agent_profile="analyst")

    with (
        patch(
            "cli_agent_orchestrator.providers.omp.shutil.which", return_value="/usr/local/bin/omp"
        ),
        patch("cli_agent_orchestrator.providers.omp.load_agent_profile", return_value=profile),
        patch(
            "cli_agent_orchestrator.providers.omp.resolve_mcp_server_config",
            side_effect=lambda config, persisted: config,
        ) as resolve,
    ):
        parts = shlex.split(provider._build_omp_command())

    extension_dir = Path(parts[parts.index("--extension") + 1])
    assert (extension_dir / "index.js").read_text(
        encoding="utf-8"
    ) == "export default function () {}"
    config = json.loads((extension_dir / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert config["cao"]["env"] == {"KEEP": "yes", "CAO_TERMINAL_ID": "terminal-123"}
    assert config["custom"]["env"] == {"CAO_TERMINAL_ID": "explicit"}
    assert config["remote"] == {"type": "http", "url": "https://mcp.example.test"}
    assert resolve.call_count == 3
    assert all(call.kwargs["persisted"] is False for call in resolve.call_args_list)
    assert stat.S_IMODE((extension_dir / "index.js").stat().st_mode) == 0o600
    assert stat.S_IMODE((extension_dir / ".mcp.json").stat().st_mode) == 0o600


def test_missing_binary_is_actionable():
    with patch("cli_agent_orchestrator.providers.omp.shutil.which", return_value=None):
        with pytest.raises(ProviderError, match="'omp' is not on \\$PATH"):
            make_provider()._build_omp_command()


def test_cleanup_removes_only_generated_terminal_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.providers.omp.CAO_HOME_DIR", tmp_path)
    provider = make_provider(skill_prompt="skill prompt")
    _command_parts(provider)
    root = tmp_path / "tmp" / "omp" / "terminal-123"
    sibling = tmp_path / "tmp" / "omp" / "other-terminal"
    sibling.mkdir(parents=True)

    provider.cleanup()

    assert not root.exists()
    assert sibling.exists()
    assert provider._initialized is False
    assert provider._turns == 0


# ── Initialization and extraction ───────────────────────────────────────


def test_initialize_starts_omp_and_waits_for_ready():
    backend = MagicMock()
    backend.get_pane_current_command.return_value = "zsh"
    provider = make_provider()
    with (
        patch(
            "cli_agent_orchestrator.providers.omp.wait_for_shell", new=AsyncMock(return_value=True)
        ),
        patch(
            "cli_agent_orchestrator.providers.omp.wait_until_status",
            new=AsyncMock(return_value=True),
        ) as wait,
        patch("cli_agent_orchestrator.providers.omp.get_backend", return_value=backend),
        patch(
            "cli_agent_orchestrator.providers.omp.shutil.which", return_value="/usr/local/bin/omp"
        ),
    ):
        assert asyncio.run(provider.initialize()) is True

    assert provider.shell_baseline == "zsh"
    assert backend.method_calls == [
        call.get_pane_current_command("test-session", "window-0"),
        call.send_keys("test-session", "window-0", "omp"),
    ]
    backend.send_keys.assert_called_once_with("test-session", "window-0", "omp")
    assert wait.call_args.args[1] == {TerminalStatus.IDLE, TerminalStatus.COMPLETED}


def test_initialize_raises_for_shell_and_ready_timeouts():
    with patch(
        "cli_agent_orchestrator.providers.omp.wait_for_shell", new=AsyncMock(return_value=False)
    ):
        with pytest.raises(TimeoutError, match="Shell initialization"):
            asyncio.run(make_provider().initialize())

    with (
        patch(
            "cli_agent_orchestrator.providers.omp.wait_for_shell", new=AsyncMock(return_value=True)
        ),
        patch(
            "cli_agent_orchestrator.providers.omp.wait_until_status",
            new=AsyncMock(return_value=False),
        ),
        patch("cli_agent_orchestrator.providers.omp.get_backend"),
        patch(
            "cli_agent_orchestrator.providers.omp.shutil.which", return_value="/usr/local/bin/omp"
        ),
    ):
        with pytest.raises(TimeoutError, match="OMP initialization"):
            asyncio.run(make_provider().initialize())


def test_extracts_final_response_and_preserves_error_prose():
    provider = make_provider()
    two_turns = (
        "\x1b[48;2;5;5;5m\n first request\n\n\x1b[49m first answer\n\n"
        "\x1b[48;2;5;5;5m\n second request\n\n\x1b[49m second answer\n\n╰─ ready ─╯\n"
    )

    assert (
        provider.extract_last_message_from_script(load_fixture("omp_completed.txt"))
        == "RAW_FIXTURE"
    )
    assert provider.extract_last_message_from_script(load_fixture("omp_prose.txt")) == (
        "Error: working and cancel are ordinary prose."
    )
    assert provider.extract_last_message_from_script(two_turns) == "second answer"


def test_extract_accepts_box_prefixed_assistant_content():
    output = (
        "\x1b[48;2;5;5;5m\n user prompt\n\n"
        "\x1b[49m╭ assistant response starts with a box glyph\n"
        "and continues normally\n\n╰─ ready ─╯\n"
    )

    assert make_provider().extract_last_message_from_script(output) == (
        "╭ assistant response starts with a box glyph\nand continues normally"
    )


def test_extract_uses_ansi_turn_boundary_and_rejects_canceled_user_turn():
    completed = (
        "\x1b[48;2;5;5;5m\n user prompt\n\n"
        "\x1b[49m def greet(name):\n    return f'Hello, {name}'\n\n"
        "The function returns a greeting.\n\n╰─ ready ─╯\n"
    )
    canceled = "\x1b[48;2;5;5;5m\n user prompt\n\n╰─ ready ─╯\n"

    assert make_provider().extract_last_message_from_script(completed) == (
        "def greet(name):\n    return f'Hello, {name}'\n\nThe function returns a greeting."
    )
    with pytest.raises(ValueError, match="assistant block"):
        make_provider().extract_last_message_from_script(canceled)
    with pytest.raises(ValueError, match="user-turn boundary"):
        make_provider().extract_last_message_from_script("user prompt\n\n╰─ ready ─╯\n")
    with pytest.raises(ValueError, match="only chrome"):
        make_provider().extract_last_message_from_script(
            "\x1b[48;2;5;5;5m\n user prompt\n\n" "\x1b[49m ⠋ Working…\n\n╰─ ready ─╯\n"
        )
    with pytest.raises(ValueError, match="response frame"):
        make_provider().extract_last_message_from_script(
            "\x1b[48;2;5;5;5m\n user prompt\n\n\x1b[49m assistant response"
        )


@pytest.mark.parametrize("output", ["no footer", "Allow tool: bash\n╰─ frame ─╯\n"])
def test_extract_rejects_missing_or_chrome_only_response(output):
    with pytest.raises(ValueError):
        make_provider().extract_last_message_from_script(output)


def test_profile_load_failure_is_provider_specific():
    provider = make_provider(agent_profile="missing")
    with patch(
        "cli_agent_orchestrator.providers.omp.load_agent_profile",
        side_effect=RuntimeError("broken profile"),
    ):
        with pytest.raises(ProviderError, match="Failed to load agent profile"):
            provider._load_profile()


def test_extension_serializes_model_config_and_status_unknown_paths(tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.providers.omp.CAO_HOME_DIR", tmp_path)
    mcp_model = MagicMock()
    mcp_model.model_dump.return_value = {"command": "model-mcp"}
    provider = make_provider()

    with patch(
        "cli_agent_orchestrator.providers.omp.resolve_mcp_server_config",
        side_effect=lambda config, persisted: config,
    ):
        provider._write_extension_root({"model": mcp_model})

    assert mcp_model.model_dump.called
    assert provider.get_status("unrecognized OMP text") == TerminalStatus.UNKNOWN
    assert provider.get_idle_pattern_for_log().startswith("^\\s*in:")


def test_extracts_unterminated_final_block_and_cleanup_tolerates_removal_errors(tmp_path):
    provider = make_provider()
    assert (
        provider.extract_last_message_from_script(
            "\x1b[48;2;5;5;5m\n request\n\n\x1b[49m answer\n╰─ frame ─╯\n"
        )
        == "answer"
    )

    provider._artifact_dir = tmp_path / "generated"
    with patch(
        "cli_agent_orchestrator.providers.omp.shutil.rmtree",
        side_effect=[FileNotFoundError, OSError("locked")],
    ):
        provider.cleanup()
        provider._artifact_dir = tmp_path / "generated"
        provider.cleanup()

    assert provider._artifact_dir is None
