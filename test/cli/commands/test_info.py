"""Tests for info command."""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.info import info


class TestInfoCommand:
    """Test cao info command."""

    def test_info_not_in_tmux(self):
        """Test output when not running inside tmux."""
        runner = CliRunner()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = runner.invoke(info)

        assert result.exit_code == 0
        assert "Database path:" in result.output
        assert "Not currently in a CAO session." in result.output

    def test_info_in_tmux_non_cao_session(self):
        """Test output when in tmux but not a CAO session."""
        runner = CliRunner()
        mock_result = MagicMock()
        mock_result.stdout = "my-random-session\n"

        with patch("subprocess.run", return_value=mock_result):
            result = runner.invoke(info)

        assert result.exit_code == 0
        assert "Database path:" in result.output
        assert "Not currently in a CAO session." in result.output

    def test_info_in_cao_session_server_responds(self):
        """Test output when in a CAO session and server is reachable."""
        runner = CliRunner()
        mock_subprocess = MagicMock()
        mock_subprocess.stdout = "cao-test-session\n"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"terminals": [{"id": "abc"}, {"id": "def"}]}

        with patch("subprocess.run", return_value=mock_subprocess):
            with patch("requests.get", return_value=mock_response):
                result = runner.invoke(info)

        assert result.exit_code == 0
        assert "Database path:" in result.output
        assert "Session ID: cao-test-session" in result.output
        assert "Active terminals: 2" in result.output

    def test_info_in_cao_session_server_404(self):
        """Test output when in a CAO session but server returns 404."""
        runner = CliRunner()
        mock_subprocess = MagicMock()
        mock_subprocess.stdout = "cao-test-session\n"

        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch("subprocess.run", return_value=mock_subprocess):
            with patch("requests.get", return_value=mock_response):
                result = runner.invoke(info)

        assert result.exit_code == 0
        assert "Session not found in CAO server" in result.output

    def test_info_in_cao_session_server_unreachable(self):
        """Test output when in a CAO session but server is down."""
        import requests as req

        runner = CliRunner()
        mock_subprocess = MagicMock()
        mock_subprocess.stdout = "cao-test-session\n"

        with patch("subprocess.run", return_value=mock_subprocess):
            with patch(
                "requests.get",
                side_effect=req.exceptions.ConnectionError("Connection refused"),
            ):
                result = runner.invoke(info)

        assert result.exit_code == 0
        assert "Could not connect to CAO server" in result.output
