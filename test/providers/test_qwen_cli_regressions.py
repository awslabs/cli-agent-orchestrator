"""Regressions raised in upstream PR #412, independent of model output."""

import asyncio
import json
import time
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import OutputExtractionError
from cli_agent_orchestrator.providers.qwen_cli import QwenCliProvider


@pytest.fixture(autouse=True)
def isolate_qwen_journals(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_agent_orchestrator.providers.qwen_cli.CAO_HOME_DIR", tmp_path)


def provider(**kwargs):
    return QwenCliProvider("qwen-test", "test-session", "test-window", **kwargs)


@pytest.mark.parametrize("screen", [False, True])
@pytest.mark.parametrize("phrase", ["Apply this change", "Do you want to proceed?", "[y/n]"])
def test_finished_prose_is_not_an_approval_dialog(screen, phrase):
    p = provider()
    p.mark_input_received()
    rows = [f"● The documentation can say {phrase}", "Type your message or @path/to/file"]
    detect = p.get_status_from_screen if screen else p.get_status
    assert detect(rows if screen else "\n".join(rows)) == TerminalStatus.COMPLETED


def test_tool_call_is_not_returned_as_assistant_answer():
    transcript = (
        "> inspect files\n"
        "● Shell  run_shell_command(ls -la)\n"
        "  ⎿ README.md\n"
        "● The project contains a README.\n" + "─" * 40
    )
    assert provider().extract_last_message_from_script(transcript) == (
        "The project contains a README."
    )


def test_tool_only_turn_has_no_publishable_response():
    with pytest.raises(OutputExtractionError):
        provider().extract_last_message_from_script(
            "> inspect files\n● Shell  run_shell_command(ls -la)\n  ⎿ README.md\n" + "─" * 40
        )


def test_modern_qwen_final_does_not_include_thought_panel():
    assert (
        provider().extract_last_message_from_script(
            "> What is 19 + 23?\n∴︎ Thought for 5s (click or ctrl+o to expand)\n"
            "  private reasoning must not be returned\n◆︎ 19 + 23 equals 42.\n" + "─" * 40
        )
        == "19 + 23 equals 42."
    )


def test_reasoning_without_final_is_rejected():
    with pytest.raises(OutputExtractionError):
        provider().extract_last_message_from_script(
            "> What is 19 + 23?\n∴︎ Thought for 5s\nprivate reasoning\n" + "─" * 40
        )


def test_mcp_fields_and_none_values_are_preserved_and_normalized():
    p = provider()
    try:
        path = p._write_mcp_config(
            {
                "test": {
                    "type": "stdio",
                    "command": "example",
                    "timeout": 120,
                    "args": None,
                    "env": None,
                }
            }
        )
        server = json.loads(path.read_text())["mcpServers"]["test"]
        assert server == {
            "type": "stdio",
            "command": "example",
            "timeout": 120,
            "args": [],
            "env": {"CAO_TERMINAL_ID": "qwen-test"},
        }
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        p.cleanup()


@pytest.mark.asyncio
async def test_startup_polling_keeps_the_event_loop_responsive():
    events = []

    async def heartbeat():
        await asyncio.sleep(0.001)
        events.append("heartbeat")

    def blocking_history(*_args, **_kwargs):
        time.sleep(0.05)
        events.append("history")
        return "YOLO mode (shift + tab to cycle)"

    with patch("cli_agent_orchestrator.providers.qwen_cli.get_backend") as backend:
        backend.return_value.get_history.side_effect = blocking_history
        tick = asyncio.create_task(heartbeat())
        await provider()._handle_startup_dialog(outer_timeout=1)
        await tick
    assert events == ["heartbeat", "history"]


def test_native_config_and_runtime_roots_reach_tmux_without_forwarding_other_qwen_env():
    from cli_agent_orchestrator.clients.tmux import TmuxClient

    with patch.dict(
        "os.environ",
        {
            "QWEN_HOME": "/native/qwen",
            "QWEN_RUNTIME_DIR": "/tmp/runtime",
            "QWEN_RANDOM_SECRET": "must not be inherited",
        },
        clear=True,
    ):
        forwarded = TmuxClient._forwarded_server_env()
    assert forwarded == {"QWEN_HOME": "/native/qwen", "QWEN_RUNTIME_DIR": "/tmp/runtime"}


def test_unprofiled_restriction_and_skill_catalog_reach_qwen():
    with patch("cli_agent_orchestrator.providers.qwen_cli.shutil.which", return_value="/bin/qwen"):
        cmd = provider(allowed_tools=[], skill_prompt="CAO_TEST_SKILL")._build_qwen_command()
    assert "CAO_TEST_SKILL" in cmd
    assert "Allowed CAO tools:" in cmd


def test_paste_submits_with_one_enter():
    assert provider().paste_enter_count == 1


def test_missing_native_channel_is_an_error_and_raw_fallback_is_disabled(tmp_path):
    from cli_agent_orchestrator.providers.qwen_output import QwenOutput

    p = provider()
    p._output = QwenOutput(tmp_path / "missing.jsonl")
    assert p.get_status_from_screen(["Type your request, or /usage"]) == TerminalStatus.ERROR
    assert p.allow_raw_transcript_fallback is False


def test_reconstructed_provider_reads_same_journal_and_waits_for_the_new_turn(
    tmp_path, monkeypatch
):
    import cli_agent_orchestrator.providers.qwen_cli as module

    monkeypatch.setattr(module, "CAO_HOME_DIR", tmp_path)
    with patch.object(module.shutil, "which", return_value="/bin/qwen"):
        original = provider()
        original._build_qwen_command()
    path = original._output.path
    with path.open("a") as stream:
        for event in [
            {"type": "user", "message": {"content": [{"type": "text", "text": "old question"}]}},
            {
                "type": "assistant",
                "message": {"id": "a", "content": [{"type": "text", "text": "old answer"}]},
            },
        ]:
            stream.write(json.dumps(event) + "\n")
    restored = provider()
    assert restored.extract_last_message_from_script("") == "old answer"
    assert (
        restored.get_status_from_screen(["Type your request, or /usage"])
        == TerminalStatus.COMPLETED
    )
    restored.mark_input_received()
    assert (
        restored.get_status_from_screen(["Type your request, or /usage"]) == TerminalStatus.UNKNOWN
    )
    with pytest.raises(OutputExtractionError):
        restored.extract_last_message_from_script("old pane")
    restored.cleanup()
    original.cleanup()  # cleanup remains idempotent across reconstruction


def test_tail_window_does_not_keep_a_long_expired_spinner():
    p = provider()
    p.mark_input_received()
    output = "esc to cancel\n" + "x" * 3000 + "\nType your message or @path/to/file"
    assert p.get_status(output) == TerminalStatus.COMPLETED


@pytest.mark.asyncio
async def test_auth_picker_is_left_for_the_operator():
    p = provider()
    with patch("cli_agent_orchestrator.providers.qwen_cli.get_backend") as backend:
        backend.return_value.get_history.return_value = "How would you like to authenticate?"
        await p._handle_startup_dialog(outer_timeout=0.02)
        backend.return_value.send_special_key.assert_not_called()


@pytest.mark.asyncio
async def test_qwen_0_28_auth_picker_fails_launch_without_selecting_a_provider():
    from cli_agent_orchestrator.providers.qwen_cli import ProviderError

    text = "Connect a Provider\nQwen Account with Subscription\nThird-party Providers"
    p = provider()
    assert p.get_status_from_screen(text.splitlines()) == TerminalStatus.WAITING_USER_ANSWER
    with patch("cli_agent_orchestrator.providers.qwen_cli.get_backend") as backend:
        backend.return_value.get_history.return_value = text
        with pytest.raises(ProviderError, match="authentication needs attention"):
            await p._handle_startup_dialog(outer_timeout=1)
        backend.return_value.send_special_key.assert_not_called()
