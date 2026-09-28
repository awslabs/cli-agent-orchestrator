"""Tests for `cao tui` — the bundled Rust TUI launcher (issue #321).

PLACEMENT IS DELIBERATE. These live under ``test/cli/commands/`` and carry NO
``@pytest.mark.e2e`` marker. Every pytest invocation across all 8 CI workflows applies
both ``--ignore=test/e2e`` and ``-m "not e2e"``, so a test placed under ``test/e2e/`` or
marked e2e never executes. That exclusion is accepted debt for the existing suite and
explicitly not a pattern to replicate — a guard that never runs is worse than no guard,
because it reads as coverage.

Three properties are asserted:

1. the command is registered and ``cao tui --help`` works (it did not exist before);
2. a missing binary produces the STATED error — cause and remedy — not a traceback;
3. the child is invoked with an argv VECTOR and no shell, which is the mechanism T-10
   actually requires. Asserting only "it launched" would pass with ``shell=True``.
"""

import os
import subprocess
from pathlib import Path

from click.testing import CliRunner

from cli_agent_orchestrator.cli import main as cli_main
from cli_agent_orchestrator.cli.commands import tui as tui_module
from cli_agent_orchestrator.cli.commands.tui import tui
from cli_agent_orchestrator.cli.main import cli


class TestTuiCommandRegistered:
    """Test 1 — the command exists and its help renders."""

    def test_tui_is_registered_on_the_cli_group(self):
        """`cao tui` must be reachable from the top-level group, not just importable."""
        assert "tui" in cli.commands
        assert cli.commands["tui"] is cli_main.tui

    def test_tui_help_exits_zero(self):
        """`cao tui --help` works. This command did not exist before issue #321."""
        result = CliRunner().invoke(cli, ["tui", "--help"])

        assert result.exit_code == 0, result.output
        assert "terminal ui" in result.output.lower()

    def test_tui_appears_in_top_level_help(self):
        """The command is discoverable from `cao --help`, not hidden."""
        result = CliRunner().invoke(cli, ["--help"])

        assert result.exit_code == 0, result.output
        assert "tui" in result.output


class TestMissingBinaryError:
    """Test 2 — an absent binary is explained, never a traceback."""

    def test_missing_binary_reports_cause_and_remedy(self, mocker):
        """A partially-built or platform-mismatched wheel must say what and what to do."""
        mocker.patch.object(tui_module, "_locate_binary", return_value=None)

        result = CliRunner().invoke(cli, ["tui"])

        assert result.exit_code != 0
        output = result.output
        # Names the specific missing artifact...
        assert tui_module._binary_filename() in output
        # ...states the remedy...
        assert "scripts/build_tui.py build" in output
        # ...and mentions the platform-mismatch cause.
        assert "platform" in output.lower()

    def test_missing_binary_points_at_the_remedy_an_installed_user_can_actually_use(self):
        """Since #560 the build compiles the binary itself, so a MISSING one means no cargo.

        Asserted because the pre-#560 wording led with `python scripts/build_tui.py build`,
        which an operator who installed a wheel cannot run — `scripts/` ships in the sdist,
        not the wheel. Pointing someone at a file they do not have is a dead end, so the
        message must name the toolchain-and-reinstall route too.
        """
        message = tui_module._missing_binary_message(Path("/somewhere/cao-tui"))

        assert "rustup.rs" in message, "the operator needs the toolchain link"
        assert "reinstall" in message.lower(), (
            "an installed user's actual remedy is to install Rust and reinstall, since they "
            "have no scripts/ directory to run the build from"
        )

    def test_missing_binary_raises_no_traceback(self, mocker):
        """The operator boundary contract: one styled line, never a stack trace."""
        mocker.patch.object(tui_module, "_locate_binary", return_value=None)

        result = CliRunner().invoke(cli, ["tui"])

        # CliRunner captures the raised exception in result.exception. A ClickException
        # is Click's own styled-error channel; anything else (FileNotFoundError, etc.)
        # would surface to the operator as a traceback.
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Traceback" not in result.output
        assert "FileNotFoundError" not in result.output
        assert result.output.startswith("Error:") or "\nError:" in result.output

    def test_non_executable_binary_reports_chmod_remedy(self, tmp_path, mocker):
        """A present-but-not-executable binary is a distinct, named failure."""
        staged = tmp_path / tui_module._binary_filename()
        staged.write_bytes(b"#!/bin/sh\nexit 0\n")
        staged.chmod(0o444)  # readable, NOT executable
        mocker.patch.object(tui_module, "_locate_binary", return_value=staged)
        run = mocker.patch.object(tui_module.subprocess, "run")

        result = CliRunner().invoke(cli, ["tui"])

        assert result.exit_code != 0
        assert "not executable" in result.output
        assert "Traceback" not in result.output
        # It must fail BEFORE attempting to run — otherwise the message is a guess.
        run.assert_not_called()


class TestArgvVectorInvocation:
    """Test 3 — the invocation MECHANISM, not merely its outcome (T-10).

    A test asserting only "the TUI launched" passes just as happily when the command is
    built by interpolating a shell string, which is the thing T-10 forbids. So the call
    shape itself is asserted: a list of argv entries, and no shell.
    """

    def _fake_binary(self, tmp_path: Path) -> Path:
        staged = tmp_path / tui_module._binary_filename()
        staged.write_bytes(b"#!/bin/sh\nexit 0\n")
        staged.chmod(0o755)
        return staged

    def test_invocation_passes_an_argv_list_and_no_shell(self, tmp_path, mocker):
        """subprocess.run receives a LIST, and shell is never requested."""
        staged = self._fake_binary(tmp_path)
        mocker.patch.object(tui_module, "_locate_binary", return_value=staged)
        run = mocker.patch.object(
            tui_module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
        )

        result = CliRunner().invoke(cli, ["tui"])

        assert result.exit_code == 0, result.output
        run.assert_called_once()
        args, kwargs = run.call_args
        command = args[0]
        assert isinstance(command, list), f"expected an argv vector, got {type(command).__name__}"
        assert command == [str(staged)]
        # shell=True is the forbidden mechanism; assert it is absent AND falsy so
        # neither omitting nor explicitly disabling it can regress silently.
        assert kwargs.get("shell", False) is False

    def test_extra_args_are_separate_argv_entries_not_a_joined_string(self, tmp_path, mocker):
        """Operator-supplied arguments stay literal argv entries.

        The shell metacharacters here are the point: under an interpolated shell string
        they would be reinterpreted (a command substitution and an argument split). As
        argv entries they reach the child verbatim, which is what makes injection
        structurally impossible rather than merely unlikely.
        """
        staged = self._fake_binary(tmp_path)
        mocker.patch.object(tui_module, "_locate_binary", return_value=staged)
        run = mocker.patch.object(
            tui_module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
        )

        result = CliRunner().invoke(cli, ["tui", "--session", "a b; echo $(id)"])

        assert result.exit_code == 0, result.output
        command = run.call_args[0][0]
        assert command == [str(staged), "--session", "a b; echo $(id)"]
        assert run.call_args[1].get("shell", False) is False

    def test_child_exit_code_is_propagated(self, tmp_path, mocker):
        """A failing TUI must not be reported as success by the Python wrapper."""
        staged = self._fake_binary(tmp_path)
        mocker.patch.object(tui_module, "_locate_binary", return_value=staged)
        mocker.patch.object(
            tui_module.subprocess, "run", return_value=subprocess.CompletedProcess([], 3)
        )

        result = CliRunner().invoke(cli, ["tui"])

        assert result.exit_code == 3

    def test_oserror_on_exec_becomes_a_styled_error(self, tmp_path, mocker):
        """A wrong-architecture binary fails at exec; the operator still gets one line."""
        staged = self._fake_binary(tmp_path)
        mocker.patch.object(tui_module, "_locate_binary", return_value=staged)
        mocker.patch.object(tui_module.subprocess, "run", side_effect=OSError("Exec format error"))

        result = CliRunner().invoke(cli, ["tui"])

        assert result.exit_code != 0
        assert "Exec format error" in result.output
        assert "Traceback" not in result.output


class TestBinaryLookupUsesImportlibResources:
    """The lookup is package-anchored, matching api/main.py:3697.

    A path derived from ``__file__`` happens to work for an editable checkout and breaks
    for a wheel install, so the mechanism is asserted rather than the happy path.
    """

    def test_locate_binary_resolves_under_the_installed_package(self):
        from importlib.resources import files as pkg_files

        expected = Path(str(pkg_files("cli_agent_orchestrator") / tui_module._binary_filename()))
        found = tui_module._locate_binary()

        # In a source checkout with no built binary this is None; when it IS present it
        # must be exactly the package-anchored path.
        assert found is None or found == expected

    def test_binary_filename_is_platform_correct(self):
        name = tui_module._binary_filename()

        assert name == ("cao-tui.exe" if os.name == "nt" else "cao-tui")


class TestWheelPackagingDeclaration:
    """The fourth artifacts glob must stay declared in pyproject.toml.

    ``scripts/build_tui.py check`` asserts the glob resolves non-empty at build time, but
    that gate only runs during a release. This runs on every PR and catches the other half
    of the failure: the glob being DELETED. It points at gitignored build output, so on a
    fresh checkout the path is empty and the line looks like dead config — exactly what a
    tidy-up removes. Losing it ships a wheel with no binary and no failing job. (#321)
    """

    def _artifacts(self):
        try:
            import tomllib
        except ModuleNotFoundError:  # Python 3.10
            import tomli as tomllib  # type: ignore[no-redef]

        repo_root = Path(__file__).resolve().parents[3]
        data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
        return data["tool"]["hatch"]["build"]["artifacts"]

    def test_tui_binary_glob_is_declared(self):
        """Without this glob hatchling silently excludes the binary from the wheel."""
        assert "src/cli_agent_orchestrator/cao-tui*" in self._artifacts()

    def test_glob_matches_the_filename_the_command_looks_for(self):
        """The glob and the runtime lookup must agree, or the wheel ships an unreachable file."""
        import fnmatch

        glob = "src/cli_agent_orchestrator/cao-tui*"
        staged = f"src/cli_agent_orchestrator/{tui_module._binary_filename()}"

        assert fnmatch.fnmatch(staged, glob), (
            f"artifacts glob {glob!r} does not match {staged!r}, the path "
            "cli/commands/tui.py resolves at runtime"
        )

    def test_build_script_enforces_the_same_glob(self):
        """One source of truth: the build gate must enforce the glob pyproject declares."""
        import importlib.util

        repo_root = Path(__file__).resolve().parents[3]
        spec = importlib.util.spec_from_file_location(
            "_build_tui", repo_root / "scripts" / "build_tui.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert module.ENFORCED_GLOB in self._artifacts()
        assert module.BINARY_STEM == tui_module._BINARY_STEM
