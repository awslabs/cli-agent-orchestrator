"""Tests for the init CLI command."""

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.init import init


class TestInitCommand:
    """Tests for the init command."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    @patch("cli_agent_orchestrator.cli.commands.init.init_db")
    def test_init_success(self, mock_init_db, runner):
        """Test successful initialization."""
        mock_init_db.return_value = None

        result = runner.invoke(init)

        assert result.exit_code == 0
        assert "CLI Agent Orchestrator initialized successfully" in result.output
        mock_init_db.assert_called_once()

    @patch("cli_agent_orchestrator.cli.commands.init.init_db")
    def test_init_failure(self, mock_init_db, runner):
        """Test initialization failure."""
        mock_init_db.side_effect = Exception("Database error")

        result = runner.invoke(init)

        assert result.exit_code != 0
        assert "Database error" in result.output
        mock_init_db.assert_called_once()

    @patch("cli_agent_orchestrator.cli.commands.init.init_db")
    def test_init_permission_error(self, mock_init_db, runner):
        """Test initialization with permission error."""
        mock_init_db.side_effect = PermissionError("Permission denied")

        result = runner.invoke(init)

        assert result.exit_code != 0
        assert "Permission denied" in result.output
