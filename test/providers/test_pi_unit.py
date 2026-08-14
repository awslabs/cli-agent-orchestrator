"""Unit tests for the Pi terminal provider adapter."""

import json
import shlex
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers import pi as pi_module
from cli_agent_orchestrator.providers.pi import PiProvider, ProviderError

FIXTURES = Path(__file__).parent / "fixtures"


def _profile(**overrides):
    values = {
        "system_prompt": "Profile system prompt",
        "prompt": "Legacy profile prompt",
        "model": None,
        "mcpServers": {
            "cao-mcp-server": {
                "type": "stdio",
                "command": "cao-mcp-server",
                "args": [],
            }
        },
        "provider_init_timeout": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_provider(
    monkeypatch,
    tmp_path: Path,
    *,
    terminal_id: str = "term-1",
    profile=None,
    agent_profile: str | None = "developer",
    allowed_tools: list[str] | None = None,
    skill_prompt: str | None = "Runtime skill prompt",
    model: str | None = None,
    executable_name: str = "pi",
):
    home = tmp_path / "cao-home"
    executable = tmp_path / "bin" / executable_name
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.touch(mode=0o755)
    executable.chmod(0o755)
    monkeypatch.setattr(pi_module, "CAO_HOME_DIR", home)
    monkeypatch.setattr(pi_module.shutil, "which", lambda name: str(executable))
    if agent_profile is not None:
        monkeypatch.setattr(pi_module, "load_agent_profile", lambda name: profile or _profile())
    return PiProvider(
        terminal_id,
        "session-1",
        "window-1",
        agent_profile=agent_profile,
        allowed_tools=allowed_tools,
        skill_prompt=skill_prompt,
        model=model,
    )


def test_build_command_is_private_explicit_and_shell_safe(monkeypatch, tmp_path):
    """Launch tokens cannot escape the shell or re-enable ambient Pi resources."""
    terminal_id = "term one;$(touch /tmp/cao-pi-terminal-injection)"
    model = "openai/gpt 5;$(touch /tmp/cao-pi-model-injection)"
    provider = make_provider(
        monkeypatch,
        tmp_path,
        terminal_id=terminal_id,
        model=model,
        executable_name="pi binary;safe",
    )

    command = provider._build_pi_command()
    tokens = shlex.split(command)

    assert tokens == [
        "env",
        f"CAO_PI_STATE_FILE={provider.state_path}",
        f"CAO_PI_MCP_CONFIG={provider.mcp_config_path}",
        f"CAO_PI_BRIDGE_PYTHON={sys.executable}",
        str(provider.pi_executable),
        "--tui-mode",
        "regular",
        "--no-approve",
        "--no-extensions",
        "--extension",
        str(provider.extension_path),
        "--no-skills",
        "--no-prompt-templates",
        "--session-id",
        terminal_id,
        "--session-dir",
        str(provider.session_dir),
        "--append-system-prompt",
        str(provider.prompt_path),
        "--model",
        model,
    ]
    assert "--no-context-files" not in tokens
    assert stat.S_IMODE((tmp_path / "cao-home").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "cao-home" / "pi").stat().st_mode) == 0o700
    assert stat.S_IMODE(provider.runtime_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(provider.session_dir.stat().st_mode) == 0o700
    for path in (provider.prompt_path, provider.mcp_config_path, provider.state_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_build_command_resolves_and_injects_mcp_terminal_id(monkeypatch, tmp_path):
    """The bridge receives a terminal-scoped config with a stable server executable."""
    provider = make_provider(monkeypatch, tmp_path)

    config = json.loads(provider._write_mcp_config().read_text(encoding="utf-8"))

    assert config["terminalId"] == "term-1"
    assert set(config) == {"terminalId", "servers"}
    assert Path(config["servers"]["cao-mcp-server"]["command"]).is_absolute()
    assert config["servers"]["cao-mcp-server"]["type"] == "stdio"


@pytest.mark.parametrize(
    ("system_prompt", "legacy_prompt", "expected_base"),
    [
        ("System wins", "Legacy loses", "System wins"),
        (None, "Legacy fallback", "Legacy fallback"),
        ("", "Legacy fallback", "Legacy fallback"),
    ],
)
def test_prompt_composes_profile_fallback_and_runtime_skills(
    monkeypatch,
    tmp_path,
    system_prompt,
    legacy_prompt,
    expected_base,
):
    """Pi receives the selected profile prompt followed by the runtime skill catalog."""
    provider = make_provider(
        monkeypatch,
        tmp_path,
        profile=_profile(system_prompt=system_prompt, prompt=legacy_prompt),
    )

    provider._build_pi_command()

    assert provider.prompt_path.read_text(encoding="utf-8") == (
        f"{expected_base}\n\nRuntime skill prompt"
    )


def test_prompt_supports_runtime_skills_without_a_profile(monkeypatch, tmp_path):
    """A profile is optional; runtime skills still become Pi system context."""
    provider = make_provider(monkeypatch, tmp_path, agent_profile=None)

    provider._build_pi_command()

    assert provider.prompt_path.read_text(encoding="utf-8") == "Runtime skill prompt"


def test_explicit_model_overrides_profile_model(monkeypatch, tmp_path):
    """A handoff model override wins over the profile's static model."""
    provider = make_provider(
        monkeypatch,
        tmp_path,
        profile=_profile(model="profile/model"),
        model="runtime/model",
    )

    tokens = shlex.split(provider._build_pi_command())

    assert tokens[tokens.index("--model") + 1] == "runtime/model"


def test_profile_model_is_used_without_an_explicit_override(monkeypatch, tmp_path):
    """The profile model is forwarded when the caller did not override it."""
    provider = make_provider(monkeypatch, tmp_path, profile=_profile(model="profile/model"))

    tokens = shlex.split(provider._build_pi_command())

    assert tokens[tokens.index("--model") + 1] == "profile/model"


def test_native_denylist_is_passed_as_one_pi_argument(monkeypatch, tmp_path):
    """The existing tool-policy helper controls Pi's comma-separated denylist."""
    calls = []

    def fake_disallowed(provider_name, allowed):
        calls.append((provider_name, allowed))
        return ["bash", "edit", "write"]

    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.tool_mapping.get_disallowed_tools",
        fake_disallowed,
    )
    provider = make_provider(monkeypatch, tmp_path, allowed_tools=["fs_read"])

    tokens = shlex.split(provider._build_pi_command())

    assert calls == [("pi", ["fs_read"])]
    assert tokens[tokens.index("--exclude-tools") + 1] == "bash,edit,write"


def test_explicit_empty_allowed_tools_still_computes_a_native_denylist(monkeypatch, tmp_path):
    """An explicit no-tools policy does not degrade to unrestricted Pi built-ins."""
    calls = []

    def fake_disallowed(provider_name, allowed):
        calls.append((provider_name, allowed))
        return ["bash", "read", "edit", "write", "grep", "find", "ls"]

    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.tool_mapping.get_disallowed_tools",
        fake_disallowed,
    )
    provider = make_provider(monkeypatch, tmp_path, allowed_tools=[])

    tokens = shlex.split(provider._build_pi_command())

    assert calls == [("pi", [])]
    assert tokens[tokens.index("--exclude-tools") + 1] == ("bash,read,edit,write,grep,find,ls")


def test_pi_executable_is_resolved_once_at_construction(monkeypatch, tmp_path):
    """A later PATH change cannot select a different Pi binary at launch."""
    first = tmp_path / "bin" / "pi-first"
    second = tmp_path / "bin" / "pi-second"
    first.parent.mkdir(parents=True)
    first.touch(mode=0o755)
    second.touch(mode=0o755)
    calls = []

    def fake_which(name):
        calls.append(name)
        return str(first if len(calls) == 1 else second)

    monkeypatch.setattr(pi_module, "CAO_HOME_DIR", tmp_path / "cao-home")
    monkeypatch.setattr(pi_module.shutil, "which", fake_which)
    provider = PiProvider("term-1", "session-1", "window-1")

    provider._build_pi_command()
    provider._build_pi_command()

    assert calls == ["pi"]
    assert provider.pi_executable == first.resolve()
    assert shlex.split(provider._build_pi_command())[4] == str(first.resolve())


def test_missing_pi_fails_without_creating_runtime_paths(monkeypatch, tmp_path):
    """Construction fails before a partial provider directory is created."""
    home = tmp_path / "cao-home"
    monkeypatch.setattr(pi_module, "CAO_HOME_DIR", home)
    monkeypatch.setattr(pi_module.shutil, "which", lambda name: None)

    with pytest.raises(ProviderError, match="Pi executable.*PATH"):
        PiProvider("term-1", "session-1", "window-1")

    assert not home.exists()


def write_state(
    provider: PiProvider,
    *,
    status: str,
    last_assistant_text: str = "",
    error: str = "",
    updated_at: str = "2026-08-13T22:00:00.000Z",
    mode: int = 0o600,
    extra: dict | None = None,
) -> None:
    provider.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "status": status,
        "lastAssistantText": last_assistant_text,
        "error": error,
        "updatedAt": updated_at,
    }
    if extra:
        payload.update(extra)
    provider.state_path.write_text(json.dumps(payload), encoding="utf-8")
    provider.state_path.chmod(mode)


@pytest.mark.parametrize(
    ("sidecar_status", "expected"),
    [
        ("idle", TerminalStatus.IDLE),
        ("processing", TerminalStatus.PROCESSING),
        ("completed", TerminalStatus.COMPLETED),
        ("error", TerminalStatus.ERROR),
    ],
)
def test_status_maps_each_valid_sidecar_state(monkeypatch, tmp_path, sidecar_status, expected):
    """Every extension lifecycle value maps to the corresponding CAO status."""
    provider = make_provider(monkeypatch, tmp_path)
    write_state(provider, status=sidecar_status, error="bridge failed")

    assert provider.get_status("ambiguous terminal chrome") is expected


def test_status_prefers_sidecar_over_stale_tui(monkeypatch, tmp_path):
    """A valid completed sidecar overrides a stale visible working frame."""
    provider = make_provider(monkeypatch, tmp_path)
    write_state(
        provider,
        status="completed",
        last_assistant_text="exact answer",
    )

    assert provider.get_status("Working... stale frame") is TerminalStatus.COMPLETED
    assert provider.extract_last_message_from_script("terminal chrome") == "exact answer"


def test_completed_sidecar_preserves_exact_unicode_and_newlines(monkeypatch, tmp_path):
    """LAST output is the extension's exact assistant text, not parsed terminal chrome."""
    provider = make_provider(monkeypatch, tmp_path)
    exact = "第一行 — café ☃\n\n  indented second line\n"
    write_state(provider, status="completed", last_assistant_text=exact)

    assert provider.extract_last_message_from_script("unrelated TUI output") == exact


def test_mark_input_closes_idle_race_and_calls_base_hook(monkeypatch, tmp_path):
    """A stale idle sidecar cannot make a just-dispatched turn look ready."""
    provider = make_provider(monkeypatch, tmp_path)
    write_state(provider, status="idle")

    provider.mark_input_received()

    assert provider._task_dispatched is True
    assert provider.get_status("old idle screen") is TerminalStatus.PROCESSING


def test_mark_input_rejects_prior_completed_sidecar_and_output(monkeypatch, tmp_path):
    """The prior turn's completed snapshot cannot complete a new dispatch."""
    provider = make_provider(monkeypatch, tmp_path)
    write_state(
        provider,
        status="completed",
        last_assistant_text="answer from the prior turn",
    )
    assert provider.get_status("") is TerminalStatus.COMPLETED

    provider.mark_input_received()

    assert provider.get_status("") is TerminalStatus.PROCESSING
    with pytest.raises(ValueError, match="completed Pi response"):
        provider.extract_last_message_from_script("ambiguous terminal chrome")


def test_dispatch_accepts_new_completed_snapshot_without_processing_poll(monkeypatch, tmp_path):
    """A new completion closes the guard even when processing was not observed."""
    provider = make_provider(monkeypatch, tmp_path)
    write_state(
        provider,
        status="completed",
        last_assistant_text="answer from the prior turn",
        updated_at="2026-08-13T22:00:00.000Z",
    )
    assert provider.get_status("") is TerminalStatus.COMPLETED
    provider.mark_input_received()

    write_state(
        provider,
        status="completed",
        last_assistant_text="answer from the new turn",
        updated_at="2026-08-13T22:00:01.000Z",
    )

    assert provider.get_status("") is TerminalStatus.COMPLETED
    assert provider.extract_last_message_from_script("") == "answer from the new turn"


def test_dispatch_accepts_new_completion_already_visible_at_mark(monkeypatch, tmp_path):
    """A fast new completion is not captured as the pre-dispatch snapshot."""
    provider = make_provider(monkeypatch, tmp_path)
    write_state(
        provider,
        status="completed",
        last_assistant_text="answer from the prior turn",
        updated_at="2026-08-13T22:00:00.000Z",
    )
    assert provider.get_status("") is TerminalStatus.COMPLETED

    write_state(
        provider,
        status="completed",
        last_assistant_text="fast answer from the new turn",
        updated_at="2026-08-13T22:00:01.000Z",
    )
    provider.mark_input_received()

    assert provider.get_status("") is TerminalStatus.COMPLETED
    assert provider.extract_last_message_from_script("") == "fast answer from the new turn"


def test_sidecar_processing_then_completed_closes_dispatch_guard(monkeypatch, tmp_path):
    """Authoritative lifecycle updates advance a dispatched turn normally."""
    provider = make_provider(monkeypatch, tmp_path)
    provider.mark_input_received()
    write_state(provider, status="processing")
    assert provider.get_status("") is TerminalStatus.PROCESSING

    write_state(provider, status="completed", last_assistant_text="done")
    assert provider.get_status("") is TerminalStatus.COMPLETED


def test_sidecar_error_takes_precedence_over_idle_tui(monkeypatch, tmp_path):
    """Bridge failure remains visible even while Pi's editor is on screen."""
    provider = make_provider(monkeypatch, tmp_path)
    write_state(provider, status="error", error="proxy exited")
    idle = (FIXTURES / "pi_idle.txt").read_text(encoding="utf-8")

    assert provider.get_status(idle) is TerminalStatus.ERROR


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"unexpected": True}),
        lambda payload: payload.update({"status": "settled"}),
        lambda payload: payload.update({"lastAssistantText": ["not", "text"]}),
        lambda payload: payload.update({"updatedAt": "not-a-timestamp"}),
    ],
)
def test_invalid_sidecar_shape_falls_back_to_tui(monkeypatch, tmp_path, mutate):
    """Malformed or forward-incompatible sidecar data is never trusted."""
    provider = make_provider(monkeypatch, tmp_path)
    payload = {
        "status": "completed",
        "lastAssistantText": "must not be trusted",
        "error": "",
        "updatedAt": "2026-08-13T22:00:00Z",
    }
    mutate(payload)
    provider.state_path.parent.mkdir(parents=True, mode=0o700)
    provider.state_path.write_text(json.dumps(payload), encoding="utf-8")
    provider.state_path.chmod(0o600)
    idle = (FIXTURES / "pi_idle.txt").read_text(encoding="utf-8")

    assert provider.get_status(idle) is TerminalStatus.IDLE


@pytest.mark.parametrize("content", ["{", "", '{"status":"completed"'])
def test_corrupt_or_partial_sidecar_falls_back_without_raising(monkeypatch, tmp_path, content):
    """Atomic-write races and corrupt JSON do not break the status hot path."""
    provider = make_provider(monkeypatch, tmp_path)
    provider.state_path.parent.mkdir(parents=True, mode=0o700)
    provider.state_path.write_text(content, encoding="utf-8")
    provider.state_path.chmod(0o600)
    processing = (FIXTURES / "pi_processing.txt").read_text(encoding="utf-8")

    assert provider.get_status(processing) is TerminalStatus.PROCESSING


def test_state_growth_past_limit_on_validated_inode_is_rejected(monkeypatch, tmp_path):
    """The bounded fd read rejects same-inode growth after the metadata check."""
    provider = make_provider(monkeypatch, tmp_path)
    write_state(provider, status="completed", last_assistant_text="must not be trusted")
    initial_inode = provider.state_path.stat().st_ino
    real_fstat = pi_module.os.fstat
    grew = False

    def grow_after_fstat(fd):
        nonlocal grew
        metadata = real_fstat(fd)
        if not grew:
            with provider.state_path.open("a", encoding="utf-8") as stream:
                stream.write(" " * (pi_module._MAX_STATE_BYTES + 1))
            grew = True
        return metadata

    monkeypatch.setattr(pi_module.os, "fstat", grow_after_fstat)
    idle = (FIXTURES / "pi_idle.txt").read_text(encoding="utf-8")

    assert provider.get_status(idle) is TerminalStatus.IDLE
    assert provider.state_path.stat().st_ino == initial_inode
    assert provider.state_path.stat().st_size > pi_module._MAX_STATE_BYTES


def test_unsafe_state_mode_is_not_trusted(monkeypatch, tmp_path):
    """A group-readable sidecar cannot override conservative TUI detection."""
    provider = make_provider(monkeypatch, tmp_path)
    write_state(provider, status="completed", last_assistant_text="unsafe", mode=0o640)
    idle = (FIXTURES / "pi_idle.txt").read_text(encoding="utf-8")

    assert provider.get_status(idle) is TerminalStatus.IDLE


def test_wrong_owner_state_is_not_trusted(monkeypatch, tmp_path):
    """Only a sidecar owned by the current uid is authoritative."""
    provider = make_provider(monkeypatch, tmp_path)
    write_state(provider, status="completed", last_assistant_text="wrong owner")
    real_uid = pi_module.os.getuid()
    monkeypatch.setattr(pi_module.os, "getuid", lambda: real_uid + 1)
    idle = (FIXTURES / "pi_idle.txt").read_text(encoding="utf-8")

    assert provider.get_status(idle) is TerminalStatus.IDLE


def test_symlink_state_is_not_followed_or_trusted(monkeypatch, tmp_path):
    """A swapped symlink cannot redirect the provider to attacker-controlled JSON."""
    provider = make_provider(monkeypatch, tmp_path)
    target = tmp_path / "attacker-state.json"
    target.write_text(
        json.dumps(
            {
                "status": "completed",
                "lastAssistantText": "attacker text",
                "error": "",
                "updatedAt": "2026-08-13T22:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    target.chmod(0o600)
    provider.state_path.parent.mkdir(parents=True, mode=0o700)
    provider.state_path.symlink_to(target)
    idle = (FIXTURES / "pi_idle.txt").read_text(encoding="utf-8")

    assert provider.get_status(idle) is TerminalStatus.IDLE


def test_all_three_tui_fixtures_drive_conservative_fallback(monkeypatch, tmp_path):
    """The checked-in Pi 0.84.1 regular-mode frames cover idle, working, and done."""
    provider = make_provider(monkeypatch, tmp_path)
    idle = (FIXTURES / "pi_idle.txt").read_text(encoding="utf-8")
    processing = (FIXTURES / "pi_processing.txt").read_text(encoding="utf-8")
    completed = (FIXTURES / "pi_completed.txt").read_text(encoding="utf-8")

    assert provider.get_status(idle) is TerminalStatus.IDLE
    provider.mark_input_received()
    assert provider.get_status(processing) is TerminalStatus.PROCESSING
    assert provider.get_status(completed) is TerminalStatus.COMPLETED


def test_tui_fallback_detects_obvious_startup_error(monkeypatch, tmp_path):
    """A visibly failed launch is ERROR even without a readable sidecar."""
    provider = make_provider(monkeypatch, tmp_path)

    assert provider.get_status("zsh: command not found: /missing/pi") is TerminalStatus.ERROR


def test_tui_fallback_returns_unknown_for_ambiguous_text(monkeypatch, tmp_path):
    """Unrecognized terminal content is never optimistically called idle or complete."""
    provider = make_provider(monkeypatch, tmp_path)

    assert provider.get_status("ordinary shell output") is TerminalStatus.UNKNOWN


def test_invalid_sidecar_extraction_uses_completed_fixture(monkeypatch, tmp_path):
    """The TUI parser provides a narrow fallback when sidecar JSON is corrupt."""
    provider = make_provider(monkeypatch, tmp_path)
    provider.state_path.parent.mkdir(parents=True, mode=0o700)
    provider.state_path.write_text("{", encoding="utf-8")
    provider.state_path.chmod(0o600)
    completed = (FIXTURES / "pi_completed.txt").read_text(encoding="utf-8")

    assert provider.extract_last_message_from_script(completed) == (
        "The adapter is ready — café.\nExact fallback line two."
    )


def test_error_sidecar_extraction_falls_back_to_tui(monkeypatch, tmp_path):
    """An error sidecar does not masquerade its stale assistant cache as LAST output."""
    provider = make_provider(monkeypatch, tmp_path)
    write_state(
        provider,
        status="error",
        last_assistant_text="stale sidecar answer",
        error="bridge failed",
    )
    completed = (FIXTURES / "pi_completed.txt").read_text(encoding="utf-8")

    assert provider.extract_last_message_from_script(completed).startswith("The adapter is ready")


def test_tui_extraction_rejects_processing_frame(monkeypatch, tmp_path):
    """A working indicator is not mistaken for a final assistant response."""
    provider = make_provider(monkeypatch, tmp_path)
    processing = (FIXTURES / "pi_processing.txt").read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="completed Pi response"):
        provider.extract_last_message_from_script(processing)


def test_tui_rolling_history_uses_latest_completed_frame(monkeypatch, tmp_path):
    """A historical Working redraw cannot override the latest completed frame."""
    provider = make_provider(monkeypatch, tmp_path)
    processing = (FIXTURES / "pi_processing.txt").read_text(encoding="utf-8")
    completed = (FIXTURES / "pi_completed.txt").read_text(encoding="utf-8")
    provider.mark_input_received()
    assert provider.get_status(processing) is TerminalStatus.PROCESSING

    rolling_history = processing + completed

    assert provider.get_status(rolling_history) is TerminalStatus.COMPLETED
    assert provider.extract_last_message_from_script(rolling_history) == (
        "The adapter is ready — café.\nExact fallback line two."
    )


def test_tui_rolling_history_uses_latest_processing_frame(monkeypatch, tmp_path):
    """The newest Working redraw remains processing after historical completion."""
    provider = make_provider(monkeypatch, tmp_path)
    completed = (FIXTURES / "pi_completed.txt").read_text(encoding="utf-8")
    processing = (FIXTURES / "pi_processing.txt").read_text(encoding="utf-8")
    provider.mark_input_received()
    rolling_history = completed + processing

    assert provider.get_status(rolling_history) is TerminalStatus.PROCESSING
    with pytest.raises(ValueError, match="completed Pi response"):
        provider.extract_last_message_from_script(rolling_history)


@pytest.mark.asyncio
async def test_initialize_waits_for_shell_launches_and_accepts_only_idle(monkeypatch, tmp_path):
    """Initialization monitors startup but accepts only authoritative sidecar idle."""
    provider = make_provider(monkeypatch, tmp_path, agent_profile=None)
    provider.get_init_timeout = MagicMock(return_value=7)
    backend = MagicMock()
    monkeypatch.setattr(pi_module, "get_backend", lambda: backend)
    wait_shell = AsyncMock(return_value=True)

    async def reach_tui_idle(*args, **kwargs):
        write_state(provider, status="idle")
        return True

    wait_status = AsyncMock(side_effect=reach_tui_idle)
    monkeypatch.setattr(pi_module, "wait_for_shell", wait_shell)
    monkeypatch.setattr(pi_module, "wait_until_status", wait_status)

    assert await provider.initialize() is True

    wait_shell.assert_awaited_once_with("term-1", timeout=7)
    wait_status.assert_awaited_once_with(
        "term-1",
        {
            TerminalStatus.IDLE,
            TerminalStatus.PROCESSING,
            TerminalStatus.COMPLETED,
            TerminalStatus.ERROR,
        },
        timeout=7,
        polling_interval=1.0,
    )
    backend.send_keys.assert_called_once_with("session-1", "window-1", provider._build_pi_command())
    assert provider._initialized is True


@pytest.mark.asyncio
async def test_first_dispatch_accepts_completion_visible_before_mark(monkeypatch, tmp_path):
    """Initialization's idle generation anchors a fast first-turn completion."""
    provider = make_provider(monkeypatch, tmp_path, agent_profile=None)
    provider.get_init_timeout = MagicMock(return_value=5)
    monkeypatch.setattr(pi_module, "get_backend", lambda: MagicMock())
    monkeypatch.setattr(pi_module, "wait_for_shell", AsyncMock(return_value=True))

    async def reach_authoritative_idle(*args, **kwargs):
        write_state(
            provider,
            status="idle",
            updated_at="2026-08-13T22:00:00.000Z",
        )
        return True

    monkeypatch.setattr(pi_module, "wait_until_status", reach_authoritative_idle)
    assert await provider.initialize() is True

    write_state(
        provider,
        status="completed",
        last_assistant_text="fast first-turn answer",
        updated_at="2026-08-13T22:00:01.000Z",
    )
    provider.mark_input_received()

    assert provider.get_status("") is TerminalStatus.COMPLETED
    assert provider.extract_last_message_from_script("") == "fast first-turn answer"


@pytest.mark.asyncio
async def test_initialize_rejects_tui_idle_before_late_sidecar_error(monkeypatch, tmp_path):
    """Visible Pi chrome cannot win a race with extension binding failure."""
    provider = make_provider(monkeypatch, tmp_path, agent_profile=None)
    provider.get_init_timeout = MagicMock(return_value=5)
    monkeypatch.setattr(pi_module, "get_backend", lambda: MagicMock())
    monkeypatch.setattr(pi_module, "wait_for_shell", AsyncMock(return_value=True))
    monkeypatch.setattr(pi_module, "wait_until_status", AsyncMock(return_value=True))

    async def publish_extension_error(delay):
        write_state(provider, status="error", error="late extension binding failure")

    monkeypatch.setattr(pi_module.asyncio, "sleep", publish_extension_error)

    with pytest.raises(ProviderError, match="late extension binding failure"):
        await provider.initialize()


@pytest.mark.asyncio
async def test_initialize_fails_before_launch_when_shell_times_out(monkeypatch, tmp_path):
    """A missing shell readiness signal prevents Pi from being sent to the pane."""
    provider = make_provider(monkeypatch, tmp_path, agent_profile=None)
    provider.get_init_timeout = MagicMock(return_value=3)
    backend = MagicMock()
    monkeypatch.setattr(pi_module, "get_backend", lambda: backend)
    monkeypatch.setattr(pi_module, "wait_for_shell", AsyncMock(return_value=False))

    with pytest.raises(TimeoutError, match="Shell initialization timed out after 3s"):
        await provider.initialize()

    backend.send_keys.assert_not_called()


@pytest.mark.asyncio
async def test_initialize_surfaces_sidecar_bridge_error(monkeypatch, tmp_path):
    """An extension startup failure is reported instead of flattened to a timeout."""
    provider = make_provider(monkeypatch, tmp_path, agent_profile=None)
    provider.get_init_timeout = MagicMock(return_value=5)
    backend = MagicMock()
    monkeypatch.setattr(pi_module, "get_backend", lambda: backend)
    monkeypatch.setattr(pi_module, "wait_for_shell", AsyncMock(return_value=True))

    async def fail_with_state(*args, **kwargs):
        write_state(provider, status="error", error="MCP proxy failed to initialize")
        return False

    monkeypatch.setattr(pi_module, "wait_until_status", fail_with_state)

    with pytest.raises(ProviderError, match="MCP proxy failed to initialize"):
        await provider.initialize()


@pytest.mark.asyncio
async def test_initialize_rejects_non_idle_startup_state(monkeypatch, tmp_path):
    """A completed frame is not accepted as successful first-time initialization."""
    provider = make_provider(monkeypatch, tmp_path, agent_profile=None)
    provider.get_init_timeout = MagicMock(return_value=5)
    monkeypatch.setattr(pi_module, "get_backend", lambda: MagicMock())
    monkeypatch.setattr(pi_module, "wait_for_shell", AsyncMock(return_value=True))

    async def complete_without_idle(*args, **kwargs):
        write_state(provider, status="completed", last_assistant_text="stale")
        return False

    monkeypatch.setattr(pi_module, "wait_until_status", complete_without_idle)

    with pytest.raises(ProviderError, match="unexpected completed state"):
        await provider.initialize()


@pytest.mark.asyncio
async def test_initialize_times_out_without_valid_sidecar(monkeypatch, tmp_path):
    """A missing readiness signal remains a clear bounded startup timeout."""
    provider = make_provider(monkeypatch, tmp_path, agent_profile=None)
    provider.get_init_timeout = MagicMock(return_value=4)
    monkeypatch.setattr(pi_module, "get_backend", lambda: MagicMock())
    monkeypatch.setattr(pi_module, "wait_for_shell", AsyncMock(return_value=True))
    monkeypatch.setattr(pi_module, "wait_until_status", AsyncMock(return_value=False))

    with pytest.raises(TimeoutError, match="Pi initialization timed out after 4s"):
        await provider.initialize()


def test_exit_and_paste_contract(monkeypatch, tmp_path):
    """Pi uses single-Enter submission and tmux's Ctrl-D special key for exit."""
    provider = make_provider(monkeypatch, tmp_path)

    assert provider.paste_enter_count == 1
    assert provider.exit_cli() == "C-d"


def test_cleanup_is_idempotent_and_preserves_session_retention(monkeypatch, tmp_path):
    """Cleanup removes only transient sidecars while retaining Pi session data."""
    provider = make_provider(monkeypatch, tmp_path)
    provider._build_pi_command()
    session_file = provider.session_dir / "retained.jsonl"
    session_file.write_text("session", encoding="utf-8")
    sentinel = provider.runtime_dir / "do-not-remove.txt"
    sentinel.write_text("sentinel", encoding="utf-8")

    provider.cleanup()
    provider.cleanup()

    assert not provider.prompt_path.exists()
    assert not provider.mcp_config_path.exists()
    assert not provider.state_path.exists()
    assert session_file.read_text(encoding="utf-8") == "session"
    assert sentinel.read_text(encoding="utf-8") == "sentinel"
    assert provider._initialized is False
