"""Tests for the ``cao tui`` entry command (U5).

Covers the wiring contract only — the shell itself is exercised by
``test/tui/test_app.py``:

* ``tui`` is registered on the ``cao`` CLI group (RD-b=A).
* Invoking ``cao tui`` calls :func:`cli_agent_orchestrator.tui.app.main` and
  propagates its integer exit code (0 and non-zero).
* Bare ``cao`` (no args) and ``cao --help`` still print the group help and do
  **not** launch the TUI — ``main()`` is never called and no ``App`` is
  instantiated (RD-b=A: bare ``cao`` is unchanged).
"""

from unittest.mock import patch

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.tui import tui
from cli_agent_orchestrator.cli.main import cli


class TestTuiCommandRegistration:
    """The ``tui`` command is a first-class member of the ``cao`` group."""

    def test_tui_registered_on_cli_group(self):
        """``cao tui`` resolves to the tui command on the group."""

        assert "tui" in cli.commands
        assert cli.commands["tui"] is tui

    def test_tui_help_text(self):
        """The command advertises the front-door help string."""

        runner = CliRunner()
        result = runner.invoke(tui, ["--help"])

        assert result.exit_code == 0
        assert "Launch the cao terminal UI front door." in result.output


class TestTuiCommandInvocation:
    """Invoking ``cao tui`` delegates to ``tui.app.main`` and exits with its code."""

    def test_tui_invokes_main_and_exits_zero(self):
        """``cao tui`` calls main() and propagates a 0 exit code."""

        runner = CliRunner()
        with patch("cli_agent_orchestrator.tui.app.main", return_value=0) as mock_main:
            result = runner.invoke(cli, ["tui"])

        mock_main.assert_called_once_with()
        assert result.exit_code == 0

    def test_tui_propagates_nonzero_exit_code(self):
        """A non-zero return from main() becomes the process exit code."""

        runner = CliRunner()
        with patch("cli_agent_orchestrator.tui.app.main", return_value=130) as mock_main:
            result = runner.invoke(cli, ["tui"])

        mock_main.assert_called_once_with()
        assert result.exit_code == 130


class TestBareCaoUnchanged:
    """RD-b=A: bare ``cao`` still prints help and never launches the TUI."""

    def test_bare_cao_prints_help_without_launching_tui(self):
        """``cao`` with no args shows the group help; main() is not called."""

        runner = CliRunner()
        with patch("cli_agent_orchestrator.tui.app.main") as mock_main:
            with patch("cli_agent_orchestrator.tui.app.App") as mock_app:
                result = runner.invoke(cli, [])

        assert "CLI Agent Orchestrator." in result.output
        assert "Commands:" in result.output
        assert "tui" in result.output  # listed, but not launched
        mock_main.assert_not_called()
        mock_app.assert_not_called()

    def test_cao_help_prints_help_without_launching_tui(self):
        """``cao --help`` shows the group help and the tui hint; main() not called."""

        runner = CliRunner()
        with patch("cli_agent_orchestrator.tui.app.main") as mock_main:
            with patch("cli_agent_orchestrator.tui.app.App") as mock_app:
                result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert "CLI Agent Orchestrator." in result.output
        assert "Run `cao tui` for a guided terminal UI." in result.output
        mock_main.assert_not_called()
        mock_app.assert_not_called()
