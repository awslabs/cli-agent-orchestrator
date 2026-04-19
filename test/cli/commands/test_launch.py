"""Tests for launch command."""

import os
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.launch import launch


def test_launch_passes_cwd_by_default():
    """Test that launch command sends current working directory when not explicitly provided."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.subprocess.run") as mock_subprocess,
    ):

        # Mock successful API response
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None

        # Run the command (--yolo to skip workspace confirmation)
        result = runner.invoke(launch, ["--agents", "test-agent", "--yolo"])

        # Verify the command succeeded
        assert result.exit_code == 0

        # Verify requests.post was called with working_directory parameter
        mock_post.assert_called_once()
        params = mock_post.call_args.kwargs["params"]
        assert "working_directory" in params
        assert params["working_directory"] == os.path.realpath(os.getcwd())


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
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "custom-session",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None

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
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None

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
