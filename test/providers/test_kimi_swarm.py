"""Bounded native swarm startup must preserve dispatch and tool ownership."""

import hashlib
import shlex
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers import kimi_cli as kimi_module
from cli_agent_orchestrator.providers import kimi_transcript as kt
from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider, KimiDialect, ProviderError
from cli_agent_orchestrator.services.status_monitor import StatusMonitor
from cli_agent_orchestrator.utils.agent_profiles import parse_agent_profile_text


@pytest.fixture
def native_provider(tmp_path, monkeypatch):
    monkeypatch.delenv(kimi_module.KIMI_SWARM_DEFAULT_ENV, raising=False)
    monkeypatch.delenv(kimi_module.KIMI_SWARM_CAP_SHA256_ENV, raising=False)
    monkeypatch.setattr(kimi_module, "CAO_HOME_DIR", tmp_path / "cao")
    binary = tmp_path / "kimi"
    binary.write_bytes(b"verified native scheduler fixture")
    provider = KimiCliProvider("swarm-fixture", "session", "window")
    provider._kimi_binary = str(binary)
    provider._dialect = KimiDialect.CODE
    provider._kimi_source_home = tmp_path / "source"
    provider._kimi_source_home.mkdir()
    return provider


def verify_binary(provider, monkeypatch):
    digest = hashlib.sha256(Path(provider._kimi_binary).read_bytes()).hexdigest()
    monkeypatch.setenv(kimi_module.KIMI_SWARM_CAP_SHA256_ENV, digest)


def set_profile(provider, monkeypatch, **kwargs):
    profile = AgentProfile(name="native-swarm", description="test", **kwargs)
    provider._agent_profile = profile.name
    monkeypatch.setattr(kimi_module, "load_agent_profile", lambda _: profile)
    monkeypatch.setattr(kimi_module, "_with_plugin_mcp", lambda profile, _: profile)
    return profile


def command_limit(command):
    prefix = kimi_module.KIMI_SWARM_MAX_CONCURRENCY_ENV + "="
    return next(
        int(part[len(prefix) :]) for part in shlex.split(command) if part.startswith(prefix)
    )


SWARM_HEADING = (
    "  \x1b[38;5;111m─ \x1b[1mAgen\x1b[38;5;116mt Swarm\x1b[0m"
    "\x1b[38;5;111m ─ \x1b[38;5;253mRead-only batch"
    "\x1b[38;5;111m ─ \x1b[38;5;244mtest-model\x1b[38;5;111m ─────\x1b[39m"
)
SWARM_MEMBER = (
    "  \x1b[38;5;111m001\x1b[39m \x1b[38;5;242m["
    "\x1b[38;5;114m⣀\x1b[38;5;244m⣀⣀⣀⣀⣀⣀⣀\x1b[38;5;242m]"
    "\x1b[39m \x1b[38;5;244mitem-0\x1b[39m"
)
SWARM_ECHO = " \x1b[38;5;222m✨ Run the batch.\x1b[39m\n\n"
SWARM_FINAL = " \x1b[38;5;253m● \x1b[39mBatch complete.\n"
SWARM_FOOTER = "\n\x1b[38;5;244mcontext: 1% (34/64k)\x1b[39m\n"


def swarm_panel(state="Working…", prefix="🌑 "):
    return (
        SWARM_HEADING
        + "\n\n"
        + SWARM_MEMBER
        + "\n\n  "
        + prefix
        + "\x1b[38;5;111m"
        + state
        + "\x1b[39m  \x1b[38;5;111m━━━━━━━━\x1b[38;5;242m━━━━\x1b[39m\n"
    )


@pytest.mark.parametrize("prefix", ["", "🌑 ", "⠙ "])
@pytest.mark.parametrize("state", ["Working…", "Orchestrating…", "Rate limited…"])
def test_native_panel_establishes_current_execution_without_thinking_spinner(
    native_provider, state, prefix, monkeypatch
):
    native_provider.mark_input_received()
    output = SWARM_ECHO + swarm_panel(state, prefix)
    backend = MagicMock()
    backend.get_history.return_value = output + SWARM_FOOTER
    monkeypatch.setattr(kimi_module, "get_backend", lambda: backend)
    assert native_provider.has_execution_evidence(output)
    assert (
        native_provider.get_status_from_screen(kt.strip_sgr(output + SWARM_FOOTER).splitlines())
        is TerminalStatus.PROCESSING
    )


def test_completed_native_panel_does_not_establish_execution(native_provider, monkeypatch):
    native_provider.mark_input_received()
    output = SWARM_ECHO + swarm_panel("Completed.", "✓ ") + SWARM_FINAL + SWARM_FOOTER
    backend = MagicMock()
    backend.get_history.return_value = output
    monkeypatch.setattr(kimi_module, "get_backend", lambda: backend)
    assert not native_provider.has_execution_evidence(output)
    assert not kt.has_live_swarm_progress(output)
    native_provider._execution_observed = True
    native_provider._awaiting_turn = False
    assert (
        native_provider.get_status_from_screen(kt.strip_sgr(output).splitlines())
        is TerminalStatus.COMPLETED
    )
    assert native_provider.extract_last_message_from_script(output) == "● Batch complete."


def test_native_panel_execution_requires_current_submission(native_provider):
    native_provider.mark_input_received()
    assert not native_provider.has_execution_evidence(swarm_panel())
    assert not native_provider.has_execution_evidence(swarm_panel() + SWARM_ECHO)


def test_historical_working_panel_cannot_override_new_answer(native_provider, monkeypatch):
    output = SWARM_ECHO + swarm_panel() + SWARM_FINAL + SWARM_FOOTER
    backend = MagicMock()
    backend.get_history.return_value = output
    monkeypatch.setattr(kimi_module, "get_backend", lambda: backend)
    assert not kt.has_live_swarm_progress(output)
    native_provider._has_received_input = True
    assert (
        native_provider.get_status_from_screen(kt.strip_sgr(output).splitlines())
        is TerminalStatus.COMPLETED
    )


@pytest.mark.parametrize("fence", ["```text\n", " \x1b[38;5;253m● ```text\x1b[39m\n"])
@pytest.mark.parametrize("close", ["", "```\n"])
def test_fenced_native_panel_cannot_claim_activity(native_provider, fence, close):
    native_provider.mark_input_received()
    output = SWARM_ECHO + fence + swarm_panel() + close
    assert not native_provider.has_execution_evidence(output)
    assert not kt.has_live_swarm_progress(output)


def test_native_panel_in_tool_payload_cannot_claim_activity(native_provider):
    native_provider.mark_input_received()
    tool = " \x1b[38;5;253m● \x1b[1m\x1b[38;5;111mUsed Read" "\x1b[0m (file.txt) · 10 lines\n"
    output = SWARM_ECHO + tool + swarm_panel()
    assert not native_provider.has_execution_evidence(output)
    assert not kt.has_live_swarm_progress(output)


def test_plain_panel_example_stays_in_answer(native_provider, monkeypatch):
    example = kt.strip_sgr(swarm_panel())
    output = SWARM_ECHO + SWARM_FINAL + example
    backend = MagicMock()
    backend.get_history.return_value = output + SWARM_FOOTER
    monkeypatch.setattr(kimi_module, "get_backend", lambda: backend)
    assert not kt.has_live_swarm_progress(output)
    answer = native_provider.extract_last_message_from_script(output)
    assert "Agent Swarm" in answer
    assert "Working…" in answer
    native_provider._has_received_input = True
    assert (
        native_provider.get_status_from_screen(kt.strip_sgr(output + SWARM_FOOTER).splitlines())
        is TerminalStatus.COMPLETED
    )
    backend.get_history.assert_called_once_with(
        native_provider.session_name,
        native_provider.window_name,
        strip_escapes=False,
        visible_only=True,
    )


def test_partial_swarm_status_row_cannot_latch_execution(native_provider):
    native_provider.mark_input_received()
    output = SWARM_ECHO + swarm_panel().rstrip("\n")
    assert not native_provider.has_execution_evidence(output)


def test_swarm_pane_read_failure_cannot_latch_completion(native_provider, monkeypatch):
    output = SWARM_ECHO + swarm_panel() + SWARM_FOOTER
    native_provider.mark_input_received()
    assert native_provider.has_execution_evidence(output)
    backend = MagicMock()
    backend.get_history.side_effect = RuntimeError("temporary pane read failure")
    monkeypatch.setattr(kimi_module, "get_backend", lambda: backend)
    screen = kt.strip_sgr(output).splitlines()
    monitor = StatusMonitor()
    terminal_id = native_provider.terminal_id
    monitor._apply_detection(terminal_id, TerminalStatus.PROCESSING)
    status = native_provider.get_status_from_screen(screen)
    assert status is TerminalStatus.UNKNOWN
    monitor._apply_detection(terminal_id, status)
    assert monitor._last_status[terminal_id] is TerminalStatus.PROCESSING
    backend.get_history.side_effect = None
    backend.get_history.return_value = output
    status = native_provider.get_status_from_screen(screen)
    assert status is TerminalStatus.PROCESSING
    monitor._apply_detection(terminal_id, status)
    assert monitor._last_status[terminal_id] is TerminalStatus.PROCESSING
    complete = SWARM_ECHO + swarm_panel("Completed.", "✓ ") + SWARM_FINAL + SWARM_FOOTER
    backend.get_history.return_value = complete
    status = native_provider.get_status_from_screen(kt.strip_sgr(complete).splitlines())
    assert status is TerminalStatus.COMPLETED
    monitor._apply_detection(terminal_id, status)
    assert monitor._last_status[terminal_id] is TerminalStatus.COMPLETED


def test_swarm_raw_pane_read_failure_keeps_live_turn_processing(native_provider, monkeypatch):
    output = SWARM_ECHO + swarm_panel() + SWARM_FOOTER
    native_provider.mark_input_received()
    assert native_provider.has_execution_evidence(output)
    native_provider._last_dispatch_time = time.time() - 10
    backend = MagicMock()
    backend.get_history.side_effect = RuntimeError("temporary pane read failure")
    monkeypatch.setattr(kimi_module, "get_backend", lambda: backend)
    assert native_provider.get_status(output) is TerminalStatus.UNKNOWN


def test_evicted_swarm_header_and_failed_probe_cannot_complete_poll(native_provider, monkeypatch):
    output = SWARM_ECHO + swarm_panel() + SWARM_FOOTER
    native_provider.mark_input_received()
    assert native_provider.has_execution_evidence(output)
    native_provider._last_dispatch_time = time.time() - 20
    backend = MagicMock()
    backend.supports_event_inbox.return_value = False
    backend.get_native_status.return_value = None
    backend.get_history.side_effect = RuntimeError("temporary pane read failure")
    monkeypatch.setattr(kimi_module, "get_backend", lambda: backend)
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry.get_backend", lambda: backend)
    manager = MagicMock()
    manager.get_provider.return_value = native_provider
    monkeypatch.setattr("cli_agent_orchestrator.services.status_monitor.provider_manager", manager)
    monitor = StatusMonitor()
    terminal_id = native_provider.terminal_id
    monitor._apply_detection(terminal_id, TerminalStatus.PROCESSING)
    monitor._buffers[terminal_id] = SWARM_FOOTER
    assert native_provider.get_status(SWARM_FOOTER) is TerminalStatus.UNKNOWN
    assert (
        native_provider.get_status_from_screen(kt.strip_sgr(SWARM_FOOTER).splitlines())
        is TerminalStatus.UNKNOWN
    )
    assert monitor.get_status(terminal_id) is TerminalStatus.PROCESSING
    assert monitor._last_status[terminal_id] is TerminalStatus.PROCESSING


def test_finished_children_still_wait_for_main_answer(native_provider, monkeypatch):
    active = SWARM_ECHO + swarm_panel() + SWARM_FOOTER
    native_provider.mark_input_received()
    assert native_provider.has_execution_evidence(active)
    native_provider._last_dispatch_time = time.time() - 20
    children_done = (
        SWARM_ECHO
        + swarm_panel("Completed.", "✓ ")
        + " \x1b[38;5;244m● Both children finished; composing the answer.\x1b[39m\n"
        + SWARM_FOOTER
    )
    backend = MagicMock()
    backend.get_history.return_value = children_done
    monkeypatch.setattr(kimi_module, "get_backend", lambda: backend)
    assert native_provider.get_status(children_done) is TerminalStatus.PROCESSING
    assert (
        native_provider.get_status_from_screen(kt.strip_sgr(children_done).splitlines())
        is TerminalStatus.PROCESSING
    )
    complete = children_done + SWARM_FINAL + SWARM_FOOTER
    backend.get_history.return_value = complete
    assert native_provider.get_status(complete) is TerminalStatus.COMPLETED
    assert (
        native_provider.get_status_from_screen(kt.strip_sgr(complete).splitlines())
        is TerminalStatus.COMPLETED
    )


def test_swarm_generation_resets_for_next_normal_turn(native_provider):
    native_provider.mark_input_received()
    assert native_provider.has_execution_evidence(SWARM_ECHO + swarm_panel())
    assert native_provider._swarm_turn_seen
    native_provider.mark_input_received()
    assert not native_provider._swarm_turn_seen


def test_swarm_is_observed_after_an_ordinary_thinking_spinner(native_provider):
    native_provider.mark_input_received()
    assert native_provider.has_execution_evidence("⠙ Thinking…\n")
    assert not native_provider._swarm_turn_seen
    native_provider.observe_execution_output(
        SWARM_ECHO + swarm_panel(), native_provider._status_buffer_epoch, truncated=False
    )
    assert native_provider._swarm_turn_seen


def test_profile_swarm_fields_roundtrip_and_omission():
    profile = parse_agent_profile_text(
        "---\nname: kimi-test\ndescription: test\nkimiSwarm: true\n"
        "kimiSwarmMaxConcurrency: 4\n---\nReview the input.",
        "kimi-test",
    )
    assert profile.kimiSwarm is True
    assert profile.kimiSwarmMaxConcurrency == 4
    default = AgentProfile(name="default", description="test")
    assert "kimiSwarm" not in default.model_dump(exclude_none=True)
    assert "kimiSwarmMaxConcurrency" not in default.model_dump(exclude_none=True)


@pytest.mark.parametrize("value", [0, -1, 11, True, "10", 2.5])
def test_profile_cannot_raise_or_coerce_concurrency_ceiling(value):
    with pytest.raises(ValueError):
        AgentProfile(name="bad", description="test", kimiSwarmMaxConcurrency=value)


def test_operator_default_requires_verified_executable(native_provider, monkeypatch):
    monkeypatch.setenv(kimi_module.KIMI_SWARM_DEFAULT_ENV, "1")
    with pytest.raises(ProviderError, match="verified executable"):
        native_provider._build_kimi_code_command()
    assert native_provider._runtime_home_builder is None


def test_operator_default_enables_swarm_with_cap_ten(native_provider, monkeypatch):
    monkeypatch.setenv(kimi_module.KIMI_SWARM_DEFAULT_ENV, "1")
    verify_binary(native_provider, monkeypatch)
    command = native_provider._build_kimi_code_command()
    assert native_provider._kimi_swarm_requested is True
    assert command_limit(command) == 10


def test_explicit_opt_out_overrides_operator_default(native_provider, monkeypatch):
    monkeypatch.setenv(kimi_module.KIMI_SWARM_DEFAULT_ENV, "1")
    set_profile(native_provider, monkeypatch, kimiSwarm=False)
    verify_binary(native_provider, monkeypatch)
    command = native_provider._build_kimi_code_command()
    assert native_provider._kimi_swarm_requested is False
    assert command_limit(command) == 10


def test_mode_opt_out_cannot_bypass_operator_resource_verification(native_provider, monkeypatch):
    set_profile(native_provider, monkeypatch, kimiSwarm=False)
    verify_binary(native_provider, monkeypatch)
    with open(native_provider._kimi_binary, "ab") as handle:
        handle.write(b"unverified replacement")
    with pytest.raises(ProviderError, match="does not match"):
        native_provider._build_kimi_code_command()


def test_unconfigured_default_off_preserves_native_launch(native_provider):
    command = native_provider._build_kimi_code_command()
    assert native_provider._kimi_swarm_requested is False
    assert kimi_module.KIMI_SWARM_MAX_CONCURRENCY_ENV + "=" not in command


def test_explicit_limit_requires_verification_even_with_mode_off(native_provider, monkeypatch):
    set_profile(native_provider, monkeypatch, kimiSwarm=False, kimiSwarmMaxConcurrency=3)
    with pytest.raises(ProviderError, match="verified executable"):
        native_provider._build_kimi_code_command()


def test_explicit_opt_in_requires_same_binary(native_provider, monkeypatch):
    set_profile(native_provider, monkeypatch, kimiSwarm=True)
    verify_binary(native_provider, monkeypatch)
    with open(native_provider._kimi_binary, "ab") as handle:
        handle.write(b"unverified replacement")
    with pytest.raises(ProviderError, match="does not match"):
        native_provider._build_kimi_code_command()
    assert native_provider._runtime_home_builder is None


@pytest.mark.parametrize(
    ("profile_limit", "inherited", "expected"),
    [(None, None, 10), (None, "20", 10), (5, "3", 3), (3, "5", 3), (None, "1", 1)],
)
def test_effective_limit_keeps_lower_launch_shell_policy(
    native_provider, monkeypatch, profile_limit, inherited, expected
):
    set_profile(native_provider, monkeypatch, kimiSwarmMaxConcurrency=profile_limit)
    verify_binary(native_provider, monkeypatch)
    native_provider._kimi_swarm_concurrency_env = inherited
    assert command_limit(native_provider._build_kimi_code_command()) == expected


@pytest.mark.parametrize("inherited", ["0", "-1", "false", "2.5"])
def test_invalid_launch_shell_limit_fails_closed(native_provider, monkeypatch, inherited):
    verify_binary(native_provider, monkeypatch)
    native_provider._kimi_swarm_concurrency_env = inherited
    with pytest.raises(ProviderError, match="positive integer"):
        native_provider._build_kimi_code_command()


def test_swarm_does_not_add_native_tools_to_readonly_profile(native_provider, monkeypatch):
    set_profile(native_provider, monkeypatch, kimiSwarm=True, tools=["Read", "Grep", "Glob"])
    verify_binary(native_provider, monkeypatch)
    native_provider._build_kimi_code_command()
    home = native_provider._managed_runtime_home()
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    config = tomllib.loads((home / "config.toml").read_text())
    assert config["tools"]["enabled"] == ["Read", "Grep", "Glob"]


@pytest.mark.asyncio
async def test_activation_is_control_input_and_does_not_mark_a_task(native_provider, monkeypatch):
    backend = MagicMock()
    backend.get_history.return_value = "  ● Swarm activated\ncontext: 0.0% (0/100)"
    monkeypatch.setattr(kimi_module, "get_backend", lambda: backend)
    await native_provider._activate_kimi_swarm()
    assert native_provider._task_dispatched is False
    assert native_provider._has_received_input is False
    backend.send_keys.assert_called_once_with(
        "session",
        "window",
        "/swarm on",
        enter_count=native_provider.paste_enter_count,
        force_bracketed_paste=True,
        submit_delay=native_provider.paste_submit_delay,
    )
    assert backend.get_history.call_args.kwargs["visible_only"] is True


@pytest.mark.asyncio
async def test_activation_without_positive_confirmation_fails(native_provider, monkeypatch):
    backend = MagicMock()
    monkeypatch.setattr(kimi_module, "get_backend", lambda: backend)
    monkeypatch.setattr(kimi_module, "KIMI_SWARM_ACTIVATION_TIMEOUT_SECONDS", 0)
    with pytest.raises(ProviderError, match="did not confirm"):
        await native_provider._activate_kimi_swarm()
    assert native_provider._task_dispatched is False


def test_swarm_marker_is_not_startup_completion_evidence(native_provider):
    native_provider._kimi_swarm_requested = True
    marker = "● Swarm activated"
    assert native_provider._has_response_evidence(marker) is False
    native_provider.mark_input_received()
    assert native_provider._has_response_evidence(marker) is True


def test_restored_terminal_response_inference_is_unchanged(native_provider):
    assert native_provider._has_response_evidence("● Finished the task") is True
