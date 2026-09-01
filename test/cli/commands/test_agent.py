"""Tests for the `cao agent` CLI commands (issue #616).

Each subcommand is a thin wrapper around ``utils.orchestration`` -- the impl
functions are mocked here so these tests exercise only the CLI wiring
(argument/option forwarding, output rendering, exit codes), not the HTTP
logic underneath (covered separately by test/mcp_server/ and
test/utils/test_orchestration.py).
"""

import json
from unittest.mock import patch

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
            "developer", "do the thing", None, engine=None, model=None, use_worktree=False
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
            "developer", "do it", "/repo", engine="v2", model="fable-5", use_worktree=True
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
        assert json.loads(result.output) == {
            "success": True,
            "message": "ok",
            "output": "done",
            "terminal_id": "w1",
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
