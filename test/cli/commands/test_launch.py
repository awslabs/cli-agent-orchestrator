"""Tests for launch command."""

import os
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.launch import launch


def test_launch_includes_working_directory():
    """Test that launch command includes current working directory in the params passed to subprocess."""
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

        # Run the command
        result = runner.invoke(launch, ["--agents", "test-agent"])

        # Verify the command succeeded
        assert result.exit_code == 0

        # Verify requests.post was called with working_directory parameter
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        params = call_args.kwargs["params"]

        assert "working_directory" in params
        assert params["working_directory"] == os.path.realpath(os.getcwd())
