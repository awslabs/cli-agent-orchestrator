"""Deterministic OpenCode generation and Mini protocol regression tests."""

import json
import shlex
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import ProviderExitedError
from cli_agent_orchestrator.providers.opencode_cli import OpenCodeCliProvider
from cli_agent_orchestrator.utils.opencode_cli import detect_opencode_major


def provider(major=2):
    instance = OpenCodeCliProvider(
        "worker", "session", "worker", agent_profile="developer", model="test/model"
    )
    instance._cli_major = major
    return instance


@pytest.mark.parametrize(
    "version,major", [("1.4.2", 1), ("opencode v2.2.3", 2), ("2.3.0-beta.1", 2)]
)
def test_detect_generation_and_capabilities(version, major):
    with patch("cli_agent_orchestrator.utils.opencode_cli.subprocess.run") as run:
        run.side_effect = [
            Mock(returncode=0, stdout=version, stderr=""),
            Mock(returncode=0, stdout="--agent --model --standalone", stderr=""),
        ]
        assert detect_opencode_major() == major
        assert run.call_args.args[0] == ["opencode", *(["mini"] if major == 2 else []), "--help"]


@pytest.mark.parametrize("version", ["", "3.0.0", "0.9.0", "help output", "2.2"])
def test_reject_unknown_version(version):
    with patch(
        "cli_agent_orchestrator.utils.opencode_cli.subprocess.run",
        return_value=Mock(returncode=0, stdout=version, stderr=""),
    ):
        with pytest.raises(RuntimeError, match="Unsupported OpenCode"):
            detect_opencode_major()


@pytest.mark.parametrize(
    "help_text", ["--model --standalone", "--agent --standalone", "--agent --model"]
)
def test_reject_missing_mini_capabilities(help_text):
    with patch(
        "cli_agent_orchestrator.utils.opencode_cli.subprocess.run",
        side_effect=[
            Mock(returncode=0, stdout="2.2.3", stderr=""),
            Mock(returncode=0, stdout=help_text, stderr=""),
        ],
    ):
        with pytest.raises(RuntimeError, match="lacks required interactive flags"):
            detect_opencode_major()


@pytest.mark.parametrize("error", [FileNotFoundError(), subprocess.TimeoutExpired("opencode", 15)])
def test_probe_failure_is_actionable(error):
    with patch("cli_agent_orchestrator.utils.opencode_cli.subprocess.run", side_effect=error):
        with pytest.raises(RuntimeError, match="Cannot probe OpenCode"):
            detect_opencode_major()


def test_nonzero_probe_fails():
    with patch(
        "cli_agent_orchestrator.utils.opencode_cli.subprocess.run",
        return_value=Mock(returncode=1, stdout="", stderr="bad executable"),
    ):
        with pytest.raises(RuntimeError, match="bad executable"):
            detect_opencode_major()


@pytest.mark.parametrize("major", [1, 2])
def test_launch_agent_model_and_isolation(major):
    p = provider(major)
    words = shlex.split(p._build_launch_command())
    executable = words.index("opencode")
    assert words[executable:] == [
        "opencode",
        *(["mini", "--standalone"] if major == 2 else []),
        "--agent",
        "developer",
        "--model",
        "test/model",
    ]
    env = dict(word.split("=", 1) for word in words[:executable])
    assert env["OPENCODE_CONFIG_DIR"].endswith("/.aws/opencode")
    if major == 2:
        ui = json.loads(env["OPENCODE_CLI_CONFIG_CONTENT"])
        assert ui["mini"]["turn_summary"] == "show"
        assert ui["mini"]["footer"] == "show"
        assert "permissions" not in ui
    assert "--auto" not in words


@pytest.mark.parametrize(
    "text,status",
    [
        (
            "┃ Ask anything, / for commands, @ for context…\ndeveloper · model · ctrl+p menu",
            TerminalStatus.IDLE,
        ),
        ("developer · model · esc interrupt · ctrl+p menu", TerminalStatus.PROCESSING),
        ("developer · esc stop · ctrl+p menu", TerminalStatus.PROCESSING),
        (
            "› task\n\nDone\ndeveloper · Model · 2.3s\ndeveloper · ctrl+p menu",
            TerminalStatus.COMPLETED,
        ),
        ("developer · Model · 1m 8s\n› next task\nctrl+p menu", TerminalStatus.IDLE),
        (
            "ctrl+p menu\n△ Permission required\nAllow once Always Reject",
            TerminalStatus.WAITING_USER_ANSWER,
        ),
        ("△ Always allow", TerminalStatus.WAITING_USER_ANSWER),
        ("ERROR\nUnrecognized flag: --agent in command opencode", TerminalStatus.ERROR),
        ("USAGE\nopencode mini [flags]", TerminalStatus.ERROR),
        ("", TerminalStatus.UNKNOWN),
        ("ctrl+p menu\n› new task", TerminalStatus.UNKNOWN),
    ],
)
def test_mini_states(text, status):
    p = provider()
    assert p.get_status(text) == status
    assert p.get_status_from_screen(text.splitlines()) == status


def test_mini_response_preserves_newlines_and_excludes_previous_turn():
    text = "› old task\n\nold answer\ndeveloper · Model · 1s\n› new task\ncontinued prompt\n\nNew answer\n```python\nprint(1)\n```\ndeveloper · Model · 2s\n┃ Ask anything…\nctrl+p menu"
    assert (
        provider().extract_last_message_from_script(text) == "New answer\n```python\nprint(1)\n```"
    )


def test_real_v223_completed_capture():
    capture = (Path(__file__).parent / "fixtures/opencode_mini_completed.txt").read_text()
    p = provider()
    assert p.get_status(capture) == TerminalStatus.COMPLETED
    assert p.get_status_from_screen(capture.splitlines()) == TerminalStatus.COMPLETED
    assert p.extract_last_message_from_script(capture) == "CAO_V2_PROBE_OK"


@pytest.mark.parametrize(
    "text", ["› task\n\npartial", "developer · Model · 2s", "› task\ndeveloper · Model · 2s"]
)
def test_extraction_fails_closed(text):
    with pytest.raises(ValueError):
        provider().extract_last_message_from_script(text)


@pytest.mark.asyncio
async def test_initialization_probes_v2_and_vetoes_exited_ready_frame():
    p = provider()
    with (
        patch("cli_agent_orchestrator.providers.opencode_cli.get_backend") as backend,
        patch(
            "cli_agent_orchestrator.providers.opencode_cli.wait_for_shell",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "cli_agent_orchestrator.providers.opencode_cli.detect_opencode_major", return_value=2
        ),
        patch(
            "cli_agent_orchestrator.providers.opencode_cli.wait_until_status",
            new=AsyncMock(return_value=True),
        ),
    ):
        backend.return_value.get_pane_current_command.return_value = "bash"
        backend.return_value.get_history.return_value = "ctrl+p menu"
        with pytest.raises(ProviderExitedError, match="failed during initialization"):
            await p.initialize()
        assert not p._initialized
        assert (
            "opencode mini --standalone --agent developer"
            in backend.return_value.send_keys.call_args.args[2]
        )


def test_exited_worker_is_never_ready():
    p = provider()
    p._initialized = True
    p._shell_baseline = "bash"
    with patch("cli_agent_orchestrator.providers.opencode_cli.get_backend") as backend:
        backend.return_value.get_pane_current_command.return_value = "bash"
        assert p.get_status("ctrl+p menu") == TerminalStatus.ERROR
        assert p.get_status_from_screen(["ctrl+p menu"]) == TerminalStatus.ERROR


def test_multiline_dispatched_prompt_does_not_leak_into_result():
    p = provider()
    p.record_dispatched_message(
        "First paragraph\n\nSecond paragraph with a longword\n› quoted line"
    )
    capture = "› First paragraph\n\nSecond paragraph with a long\nword\n› quoted line\n\nActual answer\nwith two lines\ndeveloper · Model · 3s\nctrl+p menu"
    assert p.extract_last_message_from_script(capture) == "Actual answer\nwith two lines"


def test_wrong_dispatch_cannot_return_previous_result():
    p = provider()
    p.record_dispatched_message("new task")
    with pytest.raises(ValueError, match="does not match"):
        p.extract_last_message_from_script("› old task\n\nOld reply\ndeveloper · Model · 2s")


def test_launch_quotes_agent_model_and_config_paths(monkeypatch):
    from cli_agent_orchestrator.providers import opencode_cli

    monkeypatch.setattr(
        opencode_cli, "OPENCODE_CONFIG_FILE", Path("/tmp/config space/opencode.json")
    )
    monkeypatch.setattr(opencode_cli, "OPENCODE_CONFIG_DIR", Path("/tmp/config space"))
    p = provider()
    p._agent_profile = "agent with spaces; echo nope"
    p._model = "provider/model; echo nope"
    words = shlex.split(p._build_launch_command())
    assert "OPENCODE_CONFIG=/tmp/config space/opencode.json" in words
    assert words[-3:] == [p._agent_profile, "--model", p._model]


@pytest.mark.asyncio
async def test_unknown_cli_never_launches_worker():
    p = provider()
    with (
        patch("cli_agent_orchestrator.providers.opencode_cli.get_backend") as backend,
        patch(
            "cli_agent_orchestrator.providers.opencode_cli.wait_for_shell",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "cli_agent_orchestrator.providers.opencode_cli.detect_opencode_major",
            side_effect=RuntimeError("Unsupported OpenCode version"),
        ),
    ):
        with pytest.raises(RuntimeError, match="Unsupported"):
            await p.initialize()
        backend.return_value.send_keys.assert_not_called()


@pytest.mark.parametrize("major", [1, 2])
@pytest.mark.parametrize(
    "output", ["ERROR\nUnrecognized flag: --agent", "USAGE\nopencode mini [flags]"]
)
def test_help_and_launch_errors_are_never_ready(major, output):
    p = provider(major)
    assert p.get_status(output) == TerminalStatus.ERROR
    assert p.get_status_from_screen(output.splitlines()) == TerminalStatus.ERROR


@pytest.mark.asyncio
async def test_running_cli_showing_help_aborts_initialization():
    p = provider()
    p._shell_baseline = "bash"
    with patch("cli_agent_orchestrator.providers.opencode_cli.get_backend") as backend:
        backend.return_value.get_pane_current_command.return_value = "opencode"
        backend.return_value.get_history.return_value = "USAGE\nopencode mini [flags]"
        with pytest.raises(ProviderExitedError, match="initialization"):
            await p._check_startup()


@pytest.mark.parametrize(
    "dialog",
    [
        "△ Permission",
        "△ Reject permission",
        "△ Reject",
        "◆ Pick a target\nA B C\nenter choose   esc dismiss",
    ],
)
def test_mini_permission_and_question_dialogs(dialog):
    p = provider()
    assert p.get_status(dialog) == TerminalStatus.WAITING_USER_ANSWER
    assert p.get_status_from_screen(dialog.splitlines()) == TerminalStatus.WAITING_USER_ANSWER
    assert p.get_status(dialog + "\nctrl+p menu") == TerminalStatus.IDLE
