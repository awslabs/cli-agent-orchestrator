"""Tests for launch command."""

import os
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.launch import launch


def test_launch_passes_cwd_by_default():
    """Test that launch command sends current working directory when not explicitly provided."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.subprocess.run") as mock_subprocess,
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
    ):

        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = True

        result = runner.invoke(launch, ["--agents", "test-agent", "--yolo"])

        assert result.exit_code == 0
        mock_post.assert_called_once()
        params = mock_post.call_args.kwargs["params"]
        assert "working_directory" in params
        assert params["working_directory"] == os.path.realpath(os.getcwd())


def test_launch_passes_explicit_working_directory():
    """Test that --working-directory is passed to the API when provided."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.subprocess.run") as mock_subprocess,
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
    ):

        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = True

        result = runner.invoke(
            launch,
            [
                "--agents",
                "test-agent",
                "--yolo",
                "--working-directory",
                "/remote/path",
            ],
        )

        assert result.exit_code == 0
        params = mock_post.call_args.kwargs["params"]
        assert params["working_directory"] == "/remote/path"


def test_launch_headless_message_sends_to_terminal():
    """Test headless mode with message waits for IDLE then sends and polls for output."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.requests.get") as mock_get,
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
        patch("cli_agent_orchestrator.cli.commands.launch.time.sleep"),
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = True

        poll_resp = MagicMock()
        poll_resp.raise_for_status.return_value = None
        poll_resp.json.return_value = {"status": "completed"}

        output_resp = MagicMock()
        output_resp.raise_for_status.return_value = None
        output_resp.json.return_value = {"output": "task done"}

        mock_get.side_effect = [poll_resp, output_resp]

        result = runner.invoke(
            launch,
            [
                "--agents",
                "test-agent",
                "--headless",
                "--yolo",
                "do something",
            ],
        )

        assert result.exit_code == 0
        assert "task done" in result.output
        mock_wait.assert_called_once()
        # Two POST calls: create session + send message
        assert mock_post.call_count == 2


def test_launch_invalid_provider():
    """Test launch with invalid provider."""
    runner = CliRunner()

    result = runner.invoke(launch, ["--agents", "test-agent", "--provider", "invalid-provider"])

    assert result.exit_code != 0
    assert "Invalid provider" in result.output


def test_launch_with_session_name():
    """Test launch with custom session name."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.subprocess.run") as mock_subprocess,
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "custom-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = True

        result = runner.invoke(
            launch, ["--agents", "test-agent", "--session-name", "custom-session", "--yolo"]
        )

        assert result.exit_code == 0

        call_args = mock_post.call_args
        params = call_args.kwargs["params"]
        assert params["session_name"] == "custom-session"


def test_launch_request_exception():
    """Test launch handles RequestException."""
    runner = CliRunner()

    with patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post:
        import requests

        mock_post.side_effect = requests.exceptions.RequestException("Connection refused")

        result = runner.invoke(launch, ["--agents", "test-agent", "--yolo"])

        assert result.exit_code != 0
        assert "Failed to connect to cao-server" in result.output


def test_launch_generic_exception():
    """Test launch handles generic exception."""
    runner = CliRunner()

    with patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post:
        mock_post.side_effect = Exception("Unexpected error")

        result = runner.invoke(launch, ["--agents", "test-agent", "--yolo"])

        assert result.exit_code != 0
        assert "Unexpected error" in result.output


def test_launch_headless_mode():
    """Test launch in headless mode doesn't attach to tmux."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.subprocess.run") as mock_subprocess,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None

        result = runner.invoke(launch, ["--agents", "test-agent", "--headless", "--yolo"])

        assert result.exit_code == 0
        # In headless mode, subprocess.run should not be called
        mock_subprocess.assert_not_called()


def test_launch_non_headless_waits_for_idle_before_attach():
    """Non-headless launch must wait for IDLE/COMPLETED before tmux attach.

    Regression guard for #220: attaching before the TUI finishes initializing
    races with input-handler wiring and silently drops keystrokes. The wait
    must be called with the terminal id before subprocess.run.
    """
    runner = CliRunner()

    call_order = []

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.subprocess.run") as mock_subprocess,
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None

        def record_wait(*a, **kw):
            call_order.append("wait")
            return True

        def record_attach(*a, **kw):
            call_order.append("attach")

        mock_wait.side_effect = record_wait
        mock_subprocess.side_effect = record_attach

        result = runner.invoke(launch, ["--agents", "test-agent", "--yolo"])

        assert result.exit_code == 0
        mock_wait.assert_called_once()
        wait_args = mock_wait.call_args
        assert wait_args.args[0] == "test-terminal-id"
        assert call_order == ["wait", "attach"]


def test_launch_non_headless_attaches_even_if_wait_times_out():
    """Non-headless launch warns but still attaches if the idle wait times out.

    The wait is advisory: orphaning the session in tmux (by refusing to attach)
    would be worse than letting the user inspect a slow-initializing session.
    """
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.subprocess.run") as mock_subprocess,
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = False

        result = runner.invoke(launch, ["--agents", "test-agent", "--yolo"])

        assert result.exit_code == 0
        assert "did not reach idle within 120s" in result.output
        mock_subprocess.assert_called_once()
        attach_cmd = mock_subprocess.call_args.args[0]
        assert attach_cmd[:2] == ["tmux", "attach-session"]


def test_launch_workspace_confirmation_accepted():
    """Test workspace confirmation is shown for claude_code provider and accepted."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.subprocess.run") as mock_subprocess,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None

        # Provide 'y' input to accept the confirmation prompt
        result = runner.invoke(
            launch,
            ["--agents", "test-agent", "--provider", "claude_code", "--headless"],
            input="y\n",
        )

        assert result.exit_code == 0
        # New prompt format shows tool summary
        assert "launching on claude_code" in result.output
        assert "Allowed:" in result.output
        assert "Proceed?" in result.output
        mock_post.assert_called_once()


def test_launch_workspace_confirmation_declined():
    """Test workspace confirmation declined cancels launch."""
    runner = CliRunner()

    # Provide 'n' input to decline the confirmation prompt
    result = runner.invoke(
        launch, ["--agents", "test-agent", "--provider", "claude_code"], input="n\n"
    )

    assert result.exit_code != 0
    assert "Launch cancelled by user" in result.output


def test_launch_workspace_confirmation_skipped_with_yolo_flag():
    """Test --yolo flag skips workspace confirmation."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.subprocess.run") as mock_subprocess,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None

        result = runner.invoke(
            launch, ["--agents", "test-agent", "--provider", "claude_code", "--headless", "--yolo"]
        )

        assert result.exit_code == 0
        # --yolo shows warning but no confirmation prompt
        assert "Proceed?" not in result.output
        assert "WARNING" in result.output
        mock_post.assert_called_once()


def test_launch_workspace_confirmation_for_default_provider():
    """Test that default provider (kiro_cli) also triggers workspace confirmation."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.subprocess.run") as mock_subprocess,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None

        # Default provider is kiro_cli, which requires workspace confirmation
        result = runner.invoke(launch, ["--agents", "test-agent", "--headless"], input="y\n")

        assert result.exit_code == 0
        assert "launching on kiro_cli" in result.output
        assert "Proceed?" in result.output


def test_launch_yolo_sets_unrestricted_allowed_tools():
    """Test --yolo flag passes allowed_tools=* to the API."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.subprocess.run") as mock_subprocess,
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = True

        result = runner.invoke(launch, ["--agents", "test-agent", "--yolo"])

        assert result.exit_code == 0
        call_args = mock_post.call_args
        params = call_args.kwargs["params"]
        assert params["allowed_tools"] == "*"


def test_launch_allowed_tools_override():
    """Test --allowed-tools CLI flag overrides profile defaults."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.subprocess.run") as mock_subprocess,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None

        result = runner.invoke(
            launch,
            [
                "--agents",
                "test-agent",
                "--allowed-tools",
                "@cao-mcp-server",
                "--allowed-tools",
                "fs_read",
                "--headless",
            ],
            input="y\n",
        )

        assert result.exit_code == 0
        call_args = mock_post.call_args
        params = call_args.kwargs["params"]
        assert params["allowed_tools"] == "@cao-mcp-server,fs_read"


def test_launch_builtin_profile_resolves_role_defaults():
    """Test that launching a built-in profile resolves role-based allowedTools."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.subprocess.run") as mock_subprocess,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None

        # code_supervisor is a built-in profile with role=supervisor
        result = runner.invoke(
            launch,
            ["--agents", "code_supervisor", "--headless"],
            input="y\n",
        )

        assert result.exit_code == 0
        call_args = mock_post.call_args
        params = call_args.kwargs["params"]
        # Supervisor should only have MCP server tools
        assert "@cao-mcp-server" in params["allowed_tools"]


def test_launch_headless_message_conductor_not_ready():
    """Test headless+message raises when conductor does not become ready."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = False

        result = runner.invoke(
            launch,
            [
                "--agents",
                "test-agent",
                "--headless",
                "--yolo",
                "do something",
            ],
        )

        assert result.exit_code != 0
        assert "did not become ready" in result.output


def test_launch_headless_message_poll_error_status():
    """Test headless+message raises when terminal reaches error status during poll."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.requests.get") as mock_get,
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
        patch("cli_agent_orchestrator.cli.commands.launch.time.sleep"),
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = True

        poll_resp = MagicMock()
        poll_resp.raise_for_status.return_value = None
        poll_resp.json.return_value = {"status": "error"}
        mock_get.return_value = poll_resp

        result = runner.invoke(
            launch,
            [
                "--agents",
                "test-agent",
                "--headless",
                "--yolo",
                "do something",
            ],
        )

        assert result.exit_code != 0
        assert "ERROR" in result.output


def test_launch_headless_message_poll_processing_then_completed():
    """Test headless+message poll loop sleeps when status is processing before completing."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.requests.get") as mock_get,
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
        patch("cli_agent_orchestrator.cli.commands.launch.time.sleep"),
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = True

        processing_resp = MagicMock()
        processing_resp.raise_for_status.return_value = None
        processing_resp.json.return_value = {"status": "processing"}

        completed_resp = MagicMock()
        completed_resp.raise_for_status.return_value = None
        completed_resp.json.return_value = {"status": "completed"}

        output_resp = MagicMock()
        output_resp.raise_for_status.return_value = None
        output_resp.json.return_value = {"output": "done"}

        mock_get.side_effect = [processing_resp, completed_resp, output_resp]

        result = runner.invoke(
            launch,
            [
                "--agents",
                "test-agent",
                "--headless",
                "--yolo",
                "do something",
            ],
        )

        assert result.exit_code == 0
        assert "done" in result.output


def test_launch_honors_profile_provider_when_flag_not_given():
    """Test that profile.provider is used when --provider is not passed."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.subprocess.run"),
        patch(
            "cli_agent_orchestrator.utils.agent_profiles.resolve_provider",
            return_value="claude_code",
        ) as mock_resolve,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None

        result = runner.invoke(
            launch,
            ["--agents", "code_supervisor", "--headless"],
            input="y\n",
        )

        assert result.exit_code == 0
        mock_resolve.assert_called_once()
        params = mock_post.call_args.kwargs["params"]
        assert params["provider"] == "claude_code"
