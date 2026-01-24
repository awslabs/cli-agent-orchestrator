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


class TestCaoToBeadsPriority:
    """Tests for _cao_to_beads_priority() method."""

    def test_cao_priority_1_to_beads_p1(self, client):
        """CAO priority 1 (high) maps to Beads P1."""
        assert client._cao_to_beads_priority(1) == 1

    def test_cao_priority_2_to_beads_p2(self, client):
        """CAO priority 2 (medium) maps to Beads P2."""
        assert client._cao_to_beads_priority(2) == 2

    def test_cao_priority_3_to_beads_p3(self, client):
        """CAO priority 3 (low) maps to Beads P3."""
        assert client._cao_to_beads_priority(3) == 3

    def test_invalid_priority_defaults_to_p2(self, client):
        """Invalid CAO priority defaults to Beads P2."""
        assert client._cao_to_beads_priority(99) == 2
        assert client._cao_to_beads_priority(0) == 2


class TestBeadsToCaoPriority:
    """Tests for _beads_to_cao_priority() method."""

    def test_beads_p0_to_cao_1(self, client):
        """Beads P0 maps to CAO priority 1 (high)."""
        assert client._beads_to_cao_priority(0) == 1

    def test_beads_p1_to_cao_1(self, client):
        """Beads P1 maps to CAO priority 1 (high)."""
        assert client._beads_to_cao_priority(1) == 1

    def test_beads_p2_to_cao_2(self, client):
        """Beads P2 maps to CAO priority 2 (medium)."""
        assert client._beads_to_cao_priority(2) == 2

    def test_beads_p3_to_cao_3(self, client):
        """Beads P3 maps to CAO priority 3 (low)."""
        assert client._beads_to_cao_priority(3) == 3

    def test_beads_p4_to_cao_3(self, client):
        """Beads P4 maps to CAO priority 3 (low)."""
        assert client._beads_to_cao_priority(4) == 3

    def test_invalid_priority_defaults_to_2(self, client):
        """Invalid Beads priority defaults to CAO 2."""
        assert client._beads_to_cao_priority(99) == 2


class TestCaoToBeadsStatus:
    """Tests for _cao_to_beads_status() method."""

    def test_cao_open_to_beads_open(self, client):
        """CAO 'open' maps to Beads 'open'."""
        assert client._cao_to_beads_status("open") == "open"

    def test_cao_wip_to_beads_in_progress(self, client):
        """CAO 'wip' maps to Beads 'in_progress'."""
        assert client._cao_to_beads_status("wip") == "in_progress"

    def test_cao_closed_to_beads_closed(self, client):
        """CAO 'closed' maps to Beads 'closed'."""
        assert client._cao_to_beads_status("closed") == "closed"

    def test_invalid_status_defaults_to_open(self, client):
        """Invalid CAO status defaults to Beads 'open'."""
        assert client._cao_to_beads_status("invalid") == "open"


class TestBeadsToCaoStatus:
    """Tests for _beads_to_cao_status() method."""

    def test_beads_open_to_cao_open(self, client):
        """Beads 'open' maps to CAO 'open'."""
        assert client._beads_to_cao_status("open") == "open"

    def test_beads_in_progress_to_cao_wip(self, client):
        """Beads 'in_progress' maps to CAO 'wip'."""
        assert client._beads_to_cao_status("in_progress") == "wip"

    def test_beads_closed_to_cao_closed(self, client):
        """Beads 'closed' maps to CAO 'closed'."""
        assert client._beads_to_cao_status("closed") == "closed"

    def test_invalid_status_defaults_to_open(self, client):
        """Invalid Beads status defaults to CAO 'open'."""
        assert client._beads_to_cao_status("invalid") == "open"
