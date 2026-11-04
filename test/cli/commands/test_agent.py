"""Tests for the `cao agent` CLI commands (issue #616).

Each subcommand is a thin wrapper around ``utils.orchestration`` -- the impl
functions are mocked here so these tests exercise only the CLI wiring
(argument/option forwarding, output rendering, exit codes), not the HTTP
logic underneath (covered separately by test/mcp_server/ and
test/utils/test_orchestration.py).
"""

import json
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.agent import agent
from cli_agent_orchestrator.mcp_server.models import HandoffResult


@pytest.fixture
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# --help smoke tests
# ---------------------------------------------------------------------------
class TestHelp:
    def test_group_help(self, runner):
        result = runner.invoke(agent, ["--help"])
        assert result.exit_code == 0
        assert "assign" in result.output
        assert "handoff" in result.output
        assert "send-message" in result.output
        assert "status" in result.output
        assert "result" in result.output
        assert "cancel" in result.output

    @pytest.mark.parametrize(
        "subcommand", ["assign", "handoff", "send-message", "status", "result", "cancel"]
    )
    def test_subcommand_help(self, runner, subcommand):
        result = runner.invoke(agent, [subcommand, "--help"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# assign
# ---------------------------------------------------------------------------
class TestAssign:
    @patch("cli_agent_orchestrator.cli.commands.agent._assign_impl")
    def test_success_prints_terminal_id_and_exits_zero(self, mock_impl, runner):
        mock_impl.return_value = {
            "success": True,
            "terminal_id": "a1b2c3d4",
            "message": "Task assigned to developer (terminal: a1b2c3d4).",
        }

        result = runner.invoke(agent, ["assign", "developer", "do the thing"])

        assert result.exit_code == 0
        assert "a1b2c3d4" in result.output
        mock_impl.assert_called_once_with(
            "developer",
            "do the thing",
            None,
            engine=None,
            model=None,
            use_worktree=False,
            description=None,
        )

    @patch("cli_agent_orchestrator.cli.commands.agent._assign_impl")
    def test_failure_exits_nonzero(self, mock_impl, runner):
        mock_impl.return_value = {
            "success": False,
            "terminal_id": None,
            "message": "Assignment failed: CAO_TERMINAL_ID not set",
        }

        result = runner.invoke(agent, ["assign", "developer", "do the thing"])

        assert result.exit_code == 1
        assert "CAO_TERMINAL_ID not set" in result.output

    @patch("cli_agent_orchestrator.cli.commands.agent._assign_impl")
    def test_options_forwarded(self, mock_impl, runner):
        mock_impl.return_value = {"success": True, "terminal_id": "w1", "message": "ok"}

        result = runner.invoke(
            agent,
            [
                "assign",
                "developer",
                "do it",
                "--working-directory",
                "/repo",
                "--engine",
                "v2",
                "--model",
                "fable-5",
                "--use-worktree",
            ],
        )

        assert result.exit_code == 0
        mock_impl.assert_called_once_with(
            "developer",
            "do it",
            "/repo",
            engine="v2",
            model="fable-5",
            use_worktree=True,
            description=None,
        )

    @patch("cli_agent_orchestrator.cli.commands.agent._assign_impl")
    def test_json_output(self, mock_impl, runner):
        mock_impl.return_value = {"success": True, "terminal_id": "w1", "message": "ok"}

        result = runner.invoke(agent, ["assign", "developer", "do it", "--json"])

        assert result.exit_code == 0
        assert json.loads(result.output) == {
            "success": True,
            "terminal_id": "w1",
            "message": "ok",
        }


# ---------------------------------------------------------------------------
# handoff
# ---------------------------------------------------------------------------
class TestHandoff:
    @patch("cli_agent_orchestrator.cli.commands.agent._handoff_impl")
    def test_success_prints_output_and_exits_zero(self, mock_impl, runner):
        async def _fake_handoff(*args, **kwargs):
            return HandoffResult(
                success=True, message="Successfully handed off", output="42", terminal_id="w1"
            )

        mock_impl.side_effect = _fake_handoff

        result = runner.invoke(agent, ["handoff", "developer", "compute the answer"])

        assert result.exit_code == 0, result.output
        assert "w1" in result.output
        assert "Successfully handed off" in result.output
        assert "42" in result.output

    @patch("cli_agent_orchestrator.cli.commands.agent._handoff_impl")
    def test_failure_exits_nonzero(self, mock_impl, runner):
        async def _fake_handoff(*args, **kwargs):
            return HandoffResult(
                success=False,
                message="Handoff timed out after 600 seconds",
                output=None,
                terminal_id=None,
            )

        mock_impl.side_effect = _fake_handoff

        result = runner.invoke(agent, ["handoff", "developer", "do it"])

        assert result.exit_code == 1
        assert "timed out" in result.output

    @patch("cli_agent_orchestrator.cli.commands.agent._handoff_impl")
    def test_options_forwarded(self, mock_impl, runner):
        captured = {}

        async def _fake_handoff(agent_profile, message, timeout, working_directory, **kwargs):
            captured["args"] = (agent_profile, message, timeout, working_directory)
            captured["kwargs"] = kwargs
            return HandoffResult(success=True, message="ok", output=None, terminal_id="w1")

        mock_impl.side_effect = _fake_handoff

        result = runner.invoke(
            agent,
            [
                "handoff",
                "developer",
                "do it",
                "--timeout",
                "120",
                "--working-directory",
                "/repo",
                "--engine",
                "kas",
                "--model",
                "fable-5",
                "--use-worktree",
            ],
        )

        assert result.exit_code == 0, result.output
        assert captured["args"] == ("developer", "do it", 120, "/repo")
        on_terminal_id = captured["kwargs"].pop("on_terminal_id")
        assert callable(on_terminal_id)
        assert captured["kwargs"] == {
            "engine": "kas",
            "model": "fable-5",
            "use_worktree": True,
            "description": None,
            "wait": True,
        }

    def test_handoff_exposes_no_idempotency_key_flag(self, runner):
        """`--idempotency-key` was REMOVED from this surface (review on PR #634).

        Keying terminal creation alone cannot make a handoff retry safe: the
        message is delivered after the worker exists and nothing records whether
        that delivery happened, so a retry can neither skip the send (dropping
        the task) nor repeat it (running it twice). The server-side substrate is
        retained for #715, which adds the durable run record and can then expose
        a key that actually holds. Pinned so the flag is not reintroduced ahead
        of that.
        """
        result = runner.invoke(
            agent,
            ["handoff", "developer", "do it", "--idempotency-key", "retry-1"],
        )

        assert result.exit_code != 0
        assert "no such option" in result.output.lower()

    @patch("cli_agent_orchestrator.cli.commands.agent._handoff_impl")
    def test_no_wait_forwards_wait_false_and_skips_the_waiting_line(self, mock_impl, runner):
        async def _fake_handoff(*args, **kwargs):
            return HandoffResult(
                success=True, message="Handed off; not waiting", output=None, terminal_id="w1"
            )

        mock_impl.side_effect = _fake_handoff

        result = runner.invoke(agent, ["handoff", "developer", "do it", "--no-wait"])

        assert result.exit_code == 0, result.output
        assert mock_impl.call_args.kwargs["wait"] is False
        assert "Waiting for" not in result.output

    @patch("cli_agent_orchestrator.cli.commands.agent._handoff_impl")
    def test_on_terminal_id_callback_writes_terminal_id_to_stderr(self, mock_impl, runner):
        """Review on PR #634: an operator watching stderr sees the terminal_id
        before the (possibly long) wait for completion, not just at the end."""

        async def _fake_handoff(*args, on_terminal_id=None, **kwargs):
            on_terminal_id("early-w1")
            return HandoffResult(success=True, message="ok", output="done", terminal_id="early-w1")

        mock_impl.side_effect = _fake_handoff

        result = runner.invoke(agent, ["handoff", "developer", "do it"])

        assert result.exit_code == 0, result.output
        assert "terminal_id: early-w1" in result.stderr

    def test_timeout_out_of_range_is_rejected(self, runner):
        result = runner.invoke(agent, ["handoff", "developer", "do it", "--timeout", "0"])
        assert result.exit_code != 0

        result = runner.invoke(agent, ["handoff", "developer", "do it", "--timeout", "999999"])
        assert result.exit_code != 0

    @patch("cli_agent_orchestrator.cli.commands.agent._handoff_impl")
    def test_json_output_is_model_dump(self, mock_impl, runner):
        async def _fake_handoff(*args, **kwargs):
            return HandoffResult(success=True, message="ok", output="done", terminal_id="w1")

        mock_impl.side_effect = _fake_handoff

        result = runner.invoke(agent, ["handoff", "developer", "do it", "--json"])

        assert result.exit_code == 0
        # The full dump, which is the point of this test: every HandoffResult
        # field appears, including the ones this success path leaves unset.
        # ``pending``/``job_id`` (issue #447) are None here because they are
        # only populated when a transport timeout leaves the step in flight.
        assert json.loads(result.output) == {
            "success": True,
            "message": "ok",
            "output": "done",
            "terminal_id": "w1",
            "pending": None,
            "job_id": None,
            "rule": None,
        }


# ---------------------------------------------------------------------------
# send-message
# ---------------------------------------------------------------------------
class TestSendMessage:
    @patch("cli_agent_orchestrator.cli.commands.agent._send_message_impl")
    def test_success_exits_zero(self, mock_impl, runner):
        mock_impl.return_value = {
            "success": True,
            "message_id": 7,
            "sender_id": "a1b2c3d4",
            "receiver_id": "c0ffee01",
            "created_at": "2026-08-15T00:00:00Z",
        }

        result = runner.invoke(agent, ["send-message", "hello there"])

        assert result.exit_code == 0
        mock_impl.assert_called_once_with(None, "hello there")

    @patch("cli_agent_orchestrator.cli.commands.agent._send_message_impl")
    def test_to_option_forwarded(self, mock_impl, runner):
        mock_impl.return_value = {"success": True}

        result = runner.invoke(agent, ["send-message", "hello", "--to", "c0ffee01"])

        assert result.exit_code == 0
        mock_impl.assert_called_once_with("c0ffee01", "hello")

    @patch("cli_agent_orchestrator.cli.commands.agent._send_message_impl")
    def test_failure_exits_nonzero(self, mock_impl, runner):
        mock_impl.return_value = {
            "success": False,
            "error": "receiver_id not provided and CAO_TERMINAL_ID not set",
        }

        result = runner.invoke(agent, ["send-message", "hello"])

        assert result.exit_code == 1
        assert "CAO_TERMINAL_ID not set" in result.output


# ---------------------------------------------------------------------------
# status / result
# ---------------------------------------------------------------------------
class TestStatus:
    @patch("cli_agent_orchestrator.cli.commands.agent._status_impl")
    def test_success_exits_zero(self, mock_impl, runner):
        mock_impl.return_value = {
            "success": True,
            "terminal_id": "a1b2c3d4",
            "status": "idle",
            "agent_profile": "developer",
            "provider": "kiro_cli",
            "session_name": "cao-test",
        }

        result = runner.invoke(agent, ["status", "a1b2c3d4"])

        assert result.exit_code == 0
        assert "idle" in result.output
        mock_impl.assert_called_once_with("a1b2c3d4")

    @patch("cli_agent_orchestrator.cli.commands.agent._status_impl")
    def test_not_found_exits_nonzero(self, mock_impl, runner):
        mock_impl.return_value = {
            "success": False,
            "terminal_id": "deadbeef",
            "error": "Terminal deadbeef not found",
        }

        result = runner.invoke(agent, ["status", "deadbeef"])

        assert result.exit_code == 1
        assert "not found" in result.output


class TestResult:
    @patch("cli_agent_orchestrator.cli.commands.agent._result_impl")
    def test_success_prints_output_block(self, mock_impl, runner):
        mock_impl.return_value = {
            "success": True,
            "terminal_id": "a1b2c3d4",
            "output": "line one\nline two",
        }

        result = runner.invoke(agent, ["result", "a1b2c3d4"])

        assert result.exit_code == 0
        assert "output:" in result.output
        assert "line one\nline two" in result.output
        mock_impl.assert_called_once_with("a1b2c3d4")

    @patch("cli_agent_orchestrator.cli.commands.agent._result_impl")
    def test_json_output(self, mock_impl, runner):
        mock_impl.return_value = {"success": True, "terminal_id": "a1b2c3d4", "output": "done"}

        result = runner.invoke(agent, ["result", "a1b2c3d4", "--json"])

        assert result.exit_code == 0
        assert json.loads(result.output) == {
            "success": True,
            "terminal_id": "a1b2c3d4",
            "output": "done",
        }


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------
class TestCancel:
    @patch("cli_agent_orchestrator.cli.commands.agent._cancel_impl")
    def test_default_sends_interrupt(self, mock_impl, runner):
        mock_impl.return_value = {
            "success": True,
            "terminal_id": "a1b2c3d4",
            "message": "Sent interrupt (C-c) to terminal a1b2c3d4",
        }

        result = runner.invoke(agent, ["cancel", "a1b2c3d4"])

        assert result.exit_code == 0
        mock_impl.assert_called_once_with("a1b2c3d4", delete=False)

    @patch("cli_agent_orchestrator.cli.commands.agent._cancel_impl")
    def test_delete_flag_forwarded(self, mock_impl, runner):
        mock_impl.return_value = {
            "success": True,
            "message": "Terminal a1b2c3d4 deleted successfully",
        }

        result = runner.invoke(agent, ["cancel", "a1b2c3d4", "--delete"])

        assert result.exit_code == 0
        mock_impl.assert_called_once_with("a1b2c3d4", delete=True)

    @patch("cli_agent_orchestrator.cli.commands.agent._cancel_impl")
    def test_failure_exits_nonzero(self, mock_impl, runner):
        mock_impl.return_value = {
            "success": False,
            "terminal_id": "deadbeef",
            "error": "Terminal deadbeef not found",
        }

        result = runner.invoke(agent, ["cancel", "deadbeef"])

        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# machine-mode gating (the "Waiting for..." pre-line on handoff)
# ---------------------------------------------------------------------------
class TestHandoffMachineModeGating:
    @patch("cli_agent_orchestrator.cli.commands.agent._handoff_impl")
    def test_no_waiting_line_when_not_a_tty(self, mock_impl, runner):
        """CliRunner's captured stdout is never a TTY, so the interactive
        'Waiting for...' pre-line and heartbeat are suppressed even without
        --json -- exercising the same machine-mode gating a piped/CI
        invocation would hit for real."""

        async def _fake_handoff(*args, **kwargs):
            return HandoffResult(success=True, message="ok", output=None, terminal_id="w1")

        mock_impl.side_effect = _fake_handoff

        result = runner.invoke(agent, ["handoff", "developer", "do it"])

        assert result.exit_code == 0
        assert "Waiting for" not in result.output


@pytest.mark.parametrize("command", ["assign", "handoff"])
@pytest.mark.parametrize("description", [None, "Review code"])
def test_description_flag(runner, command, description):
    target = f"cli_agent_orchestrator.cli.commands.agent._{command}_impl"
    captured = {}

    def assign(*args, **kwargs):
        captured.update(kwargs)
        return {"success": True, "terminal_id": "feed1234", "message": "ok"}

    async def handoff(*args, **kwargs):
        captured.update(kwargs)
        return HandoffResult(success=True, terminal_id="feed1234", message="ok", output="Done")

    args = [command, "developer", "Task", "--json"]
    if description is not None:
        args.extend(["--description", description])
    with patch(target, side_effect=assign if command == "assign" else handoff):
        result = runner.invoke(agent, args)
    assert result.exit_code == 0, result.output
    assert captured["description"] == description


# ---------------------------------------------------------------------------
# --message-file (issue #616: "Support messages through files/stdin")
# ---------------------------------------------------------------------------
# Every command whose task text is free-form. The enumeration test below walks
# the live Click tree, so a NEW message-taking command that forgets the option
# fails there; this list only feeds the behavioural cases.
MESSAGE_COMMANDS = ["assign", "handoff", "send-message"]


def _invoke_capturing_message(runner, command, args, input=None):
    """Run ``cao agent <command> <args>`` with its impl mocked; return (result, message).

    The task text is the SECOND positional argument of all three impl
    functions (``_assign_impl(profile, message, ...)``,
    ``_handoff_impl(profile, message, ...)``,
    ``_send_message_impl(receiver_id, message)``), so one capture works for all.
    ``message`` is None when the impl was never reached.
    """
    captured = {}

    def sync_impl(*a, **kw):
        captured["message"] = a[1]
        return {"success": True, "terminal_id": "feed1234", "message": "ok"}

    async def async_impl(*a, **kw):
        captured["message"] = a[1]
        return HandoffResult(success=True, terminal_id="feed1234", message="ok", output="Done")

    impl_name = {
        "assign": "_assign_impl",
        "handoff": "_handoff_impl",
        "send-message": "_send_message_impl",
    }
    side_effect = async_impl if command == "handoff" else sync_impl
    prefix = [command] + (["developer"] if command != "send-message" else [])
    with patch(
        f"cli_agent_orchestrator.cli.commands.agent.{impl_name[command]}", side_effect=side_effect
    ):
        result = runner.invoke(agent, prefix + args + ["--json"], input=input)
    return result, captured.get("message")


def test_every_message_taking_command_accepts_message_file():
    """Adding a message-taking `cao agent` command without --message-file fails here.

    A command "takes a message" when it declares a MESSAGE argument. For each
    such command, MESSAGE must be optional (so --message-file can stand in) and
    a --message-file option must exist and be the shared UTF-8 click.File.
    """
    message_commands = []
    for name, cmd in agent.commands.items():
        params = {p.name: p for p in cmd.params}
        if "message" not in params:
            continue
        message_commands.append(name)
        assert params["message"].required is False, f"{name}: MESSAGE must be optional"
        assert "message_file" in params, f"{name}: takes MESSAGE but has no --message-file"
        opt = params["message_file"]
        assert "--message-file" in opt.opts, name
        assert isinstance(opt.type, click.File), f"{name}: --message-file must be a click.File"
        assert opt.type.encoding == "utf-8", f"{name}: --message-file must decode as UTF-8"
    assert sorted(message_commands) == sorted(MESSAGE_COMMANDS)


@pytest.mark.parametrize("command", MESSAGE_COMMANDS)
def test_message_file_reads_file_and_drops_one_trailing_newline(runner, command, tmp_path):
    task = tmp_path / "task.md"
    task.write_text("# Task\n\nFix the flaky test — and keep the ✓ marks.\n", encoding="utf-8")

    result, message = _invoke_capturing_message(runner, command, ["--message-file", str(task)])

    assert result.exit_code == 0, result.output
    assert message == "# Task\n\nFix the flaky test — and keep the ✓ marks."


@pytest.mark.parametrize("command", MESSAGE_COMMANDS)
def test_message_file_keeps_a_deliberate_blank_last_line(runner, command, tmp_path):
    task = tmp_path / "task.md"
    task.write_text("body\n\n", encoding="utf-8")

    result, message = _invoke_capturing_message(runner, command, ["--message-file", str(task)])

    assert result.exit_code == 0, result.output
    assert message == "body\n"


@pytest.mark.parametrize("command", MESSAGE_COMMANDS)
def test_message_file_dash_reads_stdin(runner, command):
    result, message = _invoke_capturing_message(
        runner, command, ["--message-file", "-"], input="from a pipe\nsecond line\n"
    )

    assert result.exit_code == 0, result.output
    assert message == "from a pipe\nsecond line"


@pytest.mark.parametrize("command", MESSAGE_COMMANDS)
def test_message_and_message_file_together_is_a_usage_error(runner, command, tmp_path):
    task = tmp_path / "task.md"
    task.write_text("from file\n", encoding="utf-8")

    result, message = _invoke_capturing_message(
        runner, command, ["inline text", "--message-file", str(task)]
    )

    assert result.exit_code == 2, result.output
    assert "mutually exclusive" in result.output
    assert message is None, "the impl must not run when the input is ambiguous"


@pytest.mark.parametrize("command", MESSAGE_COMMANDS)
def test_neither_message_nor_message_file_is_a_usage_error(runner, command):
    result, message = _invoke_capturing_message(runner, command, [])

    assert result.exit_code == 2, result.output
    assert "--message-file" in result.output
    assert message is None


@pytest.mark.parametrize("command", MESSAGE_COMMANDS)
def test_message_file_that_does_not_exist_is_a_usage_error(runner, command, tmp_path):
    result, message = _invoke_capturing_message(
        runner, command, ["--message-file", str(tmp_path / "missing.md")]
    )

    assert result.exit_code == 2, result.output
    assert "missing.md" in result.output
    assert message is None


@pytest.mark.parametrize("command", MESSAGE_COMMANDS)
def test_empty_message_file_is_refused(runner, command, tmp_path):
    task = tmp_path / "empty.md"
    task.write_text("\n", encoding="utf-8")

    result, message = _invoke_capturing_message(runner, command, ["--message-file", str(task)])

    assert result.exit_code == 2, result.output
    assert "is empty" in result.output
    assert message is None


@pytest.mark.parametrize("command", MESSAGE_COMMANDS)
def test_positional_message_still_works_unchanged(runner, command):
    """The pre-#616-follow-up calling convention is untouched."""
    result, message = _invoke_capturing_message(runner, command, ["do the thing"])

    assert result.exit_code == 0, result.output
    assert message == "do the thing"
