"""Unit tests for :mod:`cli_agent_orchestrator.tui.runner` (U3, the mutation seam).

Every ``subprocess`` call is mocked — no real ``cao`` binary is launched. Covers
SC-1 / BR-2 (argv is executed verbatim as a subprocess, and NO HTTP is ever
constructed), BR-3 (non-zero exit is a normal ``RunResult``, not an exception),
BR-7 (a spawn failure raises ``RunnerError``), the O-2 interactive path (the
prompt_toolkit app is suspended via ``run_in_terminal`` and ``cao`` runs with
inherited stdio — no capture), and FR-3.2 / FR-5.2 (copy sets the clipboard and
never raises; the stdout fallback survives only on the NON-live path).
"""

from __future__ import annotations

import sys
from unittest import mock

import pytest

from cli_agent_orchestrator.tui.runner import CommandRunner, RunnerError, RunResult

LAUNCH_ARGV = ["cao", "launch", "--provider", "kiro_cli", "backend-dev"]


# --------------------------------------------------------------------------- #
# W-4 captured mode: verbatim argv, subprocess, capture.                         #
# --------------------------------------------------------------------------- #


def test_run_passes_argv_verbatim_in_captured_mode() -> None:
    """run() forwards argv verbatim to subprocess.run and uses capture mode (W-4)."""

    runner = CommandRunner()
    with mock.patch("cli_agent_orchestrator.tui.runner.subprocess.run") as sp_run:
        sp_run.return_value = mock.Mock(stdout="hi", stderr="", returncode=0)
        result = runner.run(LAUNCH_ARGV)

    # argv verbatim (byte-identical to what was passed).
    assert sp_run.call_args.args[0] == LAUNCH_ARGV
    # Captured/headless mode is the one exercised in tests (not inherited stdio).
    assert sp_run.call_args.kwargs["capture_output"] is True
    assert sp_run.call_args.kwargs["text"] is True
    assert result == RunResult(stdout="hi", stderr="", exit_code=0)


def test_run_does_not_mutate_argv_list() -> None:
    """run() copies argv; the caller's list is never mutated in place."""

    runner = CommandRunner()
    original = list(LAUNCH_ARGV)
    with mock.patch("cli_agent_orchestrator.tui.runner.subprocess.run") as sp_run:
        sp_run.return_value = mock.Mock(stdout="", stderr="", returncode=0)
        runner.run(LAUNCH_ARGV)
    assert LAUNCH_ARGV == original


# --------------------------------------------------------------------------- #
# BR-3: non-zero exit is a normal RunResult, verbatim (SC-2).                     #
# --------------------------------------------------------------------------- #


def test_nonzero_exit_returns_result_not_exception() -> None:
    """A non-zero ``cao`` exit is a RunResult with the code + stderr, not a raise."""

    runner = CommandRunner()
    with mock.patch("cli_agent_orchestrator.tui.runner.subprocess.run") as sp_run:
        sp_run.return_value = mock.Mock(stdout="", stderr="Error: no such session\n", returncode=2)
        result = runner.run(["cao", "session", "status", "nope"])

    assert isinstance(result, RunResult)
    assert result.exit_code == 2
    # Verbatim: U3 does not interpret or rewrite the CLI's error text (SC-2).
    assert result.stderr == "Error: no such session\n"


def test_none_output_normalized_to_empty_string() -> None:
    """subprocess None stdout/stderr become empty strings, not None (BR-3 shape)."""

    runner = CommandRunner()
    with mock.patch("cli_agent_orchestrator.tui.runner.subprocess.run") as sp_run:
        sp_run.return_value = mock.Mock(stdout=None, stderr=None, returncode=0)
        result = runner.run(LAUNCH_ARGV)
    assert result.stdout == ""
    assert result.stderr == ""


# --------------------------------------------------------------------------- #
# BR-7: spawn failure -> RunnerError.                                            #
# --------------------------------------------------------------------------- #


def test_filenotfound_raises_runner_error() -> None:
    """A missing ``cao`` binary (FileNotFoundError) surfaces as RunnerError (BR-7)."""

    runner = CommandRunner()
    boom = FileNotFoundError("cao")
    with mock.patch("cli_agent_orchestrator.tui.runner.subprocess.run", side_effect=boom):
        with pytest.raises(RunnerError) as exc_info:
            runner.run(LAUNCH_ARGV)

    assert exc_info.value.argv == LAUNCH_ARGV
    assert exc_info.value.os_error is boom


def test_oserror_raises_runner_error() -> None:
    """Any OSError during spawn also surfaces as RunnerError (BR-7)."""

    runner = CommandRunner()
    with mock.patch(
        "cli_agent_orchestrator.tui.runner.subprocess.run",
        side_effect=OSError("exec format error"),
    ):
        with pytest.raises(RunnerError):
            runner.run(LAUNCH_ARGV)


# --------------------------------------------------------------------------- #
# O-2: interactive path suspends the app and inherits the terminal.              #
# --------------------------------------------------------------------------- #


def test_run_in_app_suspends_and_inherits_terminal() -> None:
    """With a live app, run_in_app uses run_in_terminal + inherited-stdio subprocess.

    O-2 resolution: the full-screen app is suspended (run_in_terminal), and cao
    runs with the real terminal's stdio — NOT captured, NOT piped into the
    renderer.
    """

    runner = CommandRunner()

    def fake_run_in_terminal(func):
        # prompt_toolkit schedules func on the loop; simulate it running (which
        # suspends the UI, spawns cao on the bare terminal, then resumes).
        func()
        return mock.Mock()  # a Future/Awaitable the caller does NOT block on

    with (
        mock.patch("cli_agent_orchestrator.tui.runner.get_app_or_none", return_value=mock.Mock()),
        mock.patch(
            "cli_agent_orchestrator.tui.runner.run_in_terminal",
            side_effect=fake_run_in_terminal,
        ) as rit,
        mock.patch("cli_agent_orchestrator.tui.runner.subprocess.run") as sp_run,
    ):
        sp_run.return_value = mock.Mock(returncode=0)
        runner.run_in_app(LAUNCH_ARGV)

    # The app was suspended via run_in_terminal (fire-and-forget — a key handler
    # cannot block on the scheduled task's result).
    rit.assert_called_once()
    # cao ran with inherited stdio: argv verbatim, NO capture_output kwarg.
    assert sp_run.call_args.args[0] == LAUNCH_ARGV
    assert "capture_output" not in sp_run.call_args.kwargs


def test_run_in_app_without_live_app_runs_inherited_directly() -> None:
    """With no running app, run_in_app runs cao directly on the inherited terminal."""

    runner = CommandRunner()
    with (
        mock.patch("cli_agent_orchestrator.tui.runner.get_app_or_none", return_value=None),
        mock.patch("cli_agent_orchestrator.tui.runner.run_in_terminal") as rit,
        mock.patch("cli_agent_orchestrator.tui.runner.subprocess.run") as sp_run,
    ):
        sp_run.return_value = mock.Mock(returncode=0)
        runner.run_in_app(LAUNCH_ARGV)

    rit.assert_not_called()  # nothing to suspend
    assert sp_run.call_args.args[0] == LAUNCH_ARGV
    assert "capture_output" not in sp_run.call_args.kwargs


def test_run_in_app_spawn_failure_raises_runner_error() -> None:
    """A spawn failure on the no-app path surfaces as RunnerError to the caller (BR-7)."""

    runner = CommandRunner()
    with (
        mock.patch("cli_agent_orchestrator.tui.runner.get_app_or_none", return_value=None),
        mock.patch(
            "cli_agent_orchestrator.tui.runner.subprocess.run",
            side_effect=FileNotFoundError("cao"),
        ),
    ):
        with pytest.raises(RunnerError):
            runner.run_in_app(LAUNCH_ARGV)


def test_run_in_app_live_app_spawn_failure_logged_not_swallowed(caplog) -> None:
    """On the live-app path a spawn failure is logged, not silently swallowed (BR-7).

    A prompt_toolkit key handler cannot catch an exception from the task
    scheduled by ``run_in_terminal``, so a spawn failure inside the suspended
    callable is surfaced via the logger instead of raised.
    """

    runner = CommandRunner()

    def fake_run_in_terminal(func):
        func()  # simulate the scheduled task running
        return mock.Mock()

    with (
        mock.patch("cli_agent_orchestrator.tui.runner.get_app_or_none", return_value=mock.Mock()),
        mock.patch(
            "cli_agent_orchestrator.tui.runner.run_in_terminal",
            side_effect=fake_run_in_terminal,
        ),
        mock.patch(
            "cli_agent_orchestrator.tui.runner.subprocess.run",
            side_effect=FileNotFoundError("cao"),
        ),
        caplog.at_level("ERROR", logger="cli_agent_orchestrator.tui.runner"),
    ):
        # Does not raise back to the (would-be) key handler...
        runner.run_in_app(LAUNCH_ARGV)

    # ...but the failure is recorded, not swallowed.
    assert any("failed to launch" in rec.message for rec in caplog.records)


# --------------------------------------------------------------------------- #
# BR-2 / SC-1: NO HTTP is ever constructed by the runner.                        #
# --------------------------------------------------------------------------- #


def test_runner_module_imports_no_http_client() -> None:
    """The runner module never imports requests/urllib/http — mutation is subprocess only."""

    import ast
    from pathlib import Path

    import cli_agent_orchestrator.tui.runner as runner_mod

    source = Path(runner_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")

    forbidden = ("requests", "urllib", "http", "httpx", "aiohttp")
    offenders = [name for name in imported if any(name.split(".")[0] == f for f in forbidden)]
    assert offenders == [], f"runner constructed HTTP imports: {offenders}"


def test_run_makes_no_http_call() -> None:
    """Executing run() touches subprocess only; no requests.request is ever called."""

    runner = CommandRunner()
    with (
        mock.patch("cli_agent_orchestrator.tui.runner.subprocess.run") as sp_run,
        mock.patch("requests.sessions.Session.request") as http,
    ):
        sp_run.return_value = mock.Mock(stdout="", stderr="", returncode=0)
        runner.run(LAUNCH_ARGV)

    http.assert_not_called()
    sp_run.assert_called_once()


# --------------------------------------------------------------------------- #
# FR-3.2: copy sets clipboard, falls back to stdout, never raises.               #
# --------------------------------------------------------------------------- #


def test_copy_sets_clipboard_when_app_present() -> None:
    """copy() places text on the running app's clipboard and reports success.

    The App now constructs its ``Application`` with a ``PyperclipClipboard``
    (FR-5.1), so this reaches the real OS clipboard rather than prompt_toolkit's
    process-local ``InMemoryClipboard`` — the ADR-013 reversal recorded in the
    module docstring.
    """

    runner = CommandRunner()
    app = mock.Mock()
    with mock.patch("cli_agent_orchestrator.tui.runner.get_app_or_none", return_value=app):
        reached_clipboard = runner.copy("cao launch --provider kiro_cli backend-dev")

    app.clipboard.set_text.assert_called_once_with("cao launch --provider kiro_cli backend-dev")
    # FR-5.2/FR-5.3: the caller renders its notice off this return value.
    assert reached_clipboard is True


def test_copy_falls_back_to_stdout_when_no_app(capsys) -> None:
    """NON-live path: with no running app, the stdout fallback SURVIVES.

    FR-11.1 is scoped to the live-app path — here no terminal has been taken over,
    so printing still hands the user the text to paste. This is the surviving half
    of the FR-5.2 ⇄ FR-11.1 resolution and must not be weakened.
    """

    runner = CommandRunner()
    with mock.patch("cli_agent_orchestrator.tui.runner.get_app_or_none", return_value=None):
        reached_clipboard = runner.copy("cao session list")

    assert capsys.readouterr().out.strip() == "cao session list"
    # No clipboard was reached — the text was printed, so the caller must not claim
    # a successful copy.
    assert reached_clipboard is False


def test_copy_falls_back_to_stdout_when_clipboard_raises(capsys) -> None:
    """A raising clipboard on the LIVE-APP path: returns False, writes ZERO bytes.

    This test's original premise — that the failure fell back to
    ``print(text, file=sys.stdout)`` — is deliberately invalidated by the recorded
    FR-5.2 ⇄ FR-11.1 resolution. Those two requirements contended for one
    mechanism: the only fallback was a direct write into the terminal the TUI has
    already taken over, which is exactly the defect class FR-11.1 forbids. The
    resolution removes the stdout write **from the live-app path only** and replaces
    it with the App's in-UI notice, keyed off this method's ``bool`` return.

    The stdout fallback survives on the NON-live path, where no terminal has been
    taken over; that is pinned by ``test_copy_falls_back_to_stdout_when_no_app`` and
    ``test_copy_never_raises_even_with_broken_stdout`` below, which are unchanged.

    Mutation target: ``runner.py``'s ``if app is not None: return False`` guard.
    Deleting it lets the ``print`` below it run on the live-app path again and the
    zero-bytes assertions RED.
    """

    runner = CommandRunner()
    app = mock.Mock()
    app.clipboard.set_text.side_effect = RuntimeError("no clipboard backend")
    capsys.readouterr()  # discard anything emitted before this point
    with mock.patch("cli_agent_orchestrator.tui.runner.get_app_or_none", return_value=app):
        reached_clipboard = runner.copy("cao memory list")  # must not raise
    captured = capsys.readouterr()

    assert reached_clipboard is False, "a failed clipboard must report False to the caller"
    assert captured.out == "", f"wrote to stdout over the live UI: {captured.out!r}"
    assert captured.err == "", f"wrote to stderr over the live UI: {captured.err!r}"


def test_copy_never_raises_even_with_broken_stdout() -> None:
    """copy() is best-effort: it prints via sys.stdout, exercised here explicitly."""

    runner = CommandRunner()
    with (
        mock.patch("cli_agent_orchestrator.tui.runner.get_app_or_none", return_value=None),
        mock.patch.object(sys, "stdout") as fake_out,
    ):
        runner.copy("cao session list")
    fake_out.write.assert_called()  # printed, did not raise
