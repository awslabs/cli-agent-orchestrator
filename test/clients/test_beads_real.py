"""Unit tests for BeadsClient bd CLI wrapper."""
import json
from unittest.mock import MagicMock, patch
import pytest
from cli_agent_orchestrator.clients.beads_real import BeadsClient


@pytest.fixture
def client():
    """Create BeadsClient instance."""
    return BeadsClient()


class TestRunBd:
    """Tests for _run_bd() method."""

    def test_executes_command(self, client):
        """_run_bd executes subprocess with bd command."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="output", stderr="")
            client._run_bd("list")
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert args[0] == "bd"
            assert "list" in args

    def test_returns_stdout(self, client):
        """_run_bd returns stdout string."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="  output  \n", stderr="")
            result = client._run_bd("list")
            assert result == "output"

    def test_raises_on_error(self, client):
        """_run_bd raises RuntimeError on command failure."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="command failed")
            with pytest.raises(RuntimeError, match="bd error"):
                client._run_bd("invalid")


class TestParseCreateOutput:
    """Tests for _parse_create_output() method."""

    def test_extracts_id(self, client):
        """_parse_create_output extracts ID from create response."""
        output = "✓ Created issue: abducabd-xyz\n  Title: test"
        result = client._parse_create_output(output)
        assert result == "abducabd-xyz"

    def test_handles_missing_id(self, client):
        """_parse_create_output returns empty string for invalid output."""
        result = client._parse_create_output("invalid output")
        assert result == ""


class TestParseJson:
    """Tests for _parse_json() method."""

    def test_parses_array(self, client):
        """_parse_json parses JSON array."""
        output = '[{"id": "abc", "title": "test"}]'
        result = client._parse_json(output)
        assert isinstance(result, list)
        assert result[0]["id"] == "abc"

    def test_handles_empty(self, client):
        """_parse_json returns empty list for empty input."""
        result = client._parse_json("")
        assert result == []

    def test_handles_invalid(self, client):
        """_parse_json returns empty list for invalid JSON."""
        result = client._parse_json("not valid json")
        assert result == []
