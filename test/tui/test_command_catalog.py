"""Unit tests for :mod:`cli_agent_orchestrator.tui.command_catalog` (U2).

These tests mock ``subprocess.run`` — they never invoke a real ``cao`` binary, so
the suite is hermetic and portable. Fixtures are synthetic but faithful to Click's
help layout (captured from real ``cao ... --help`` output at f570de1).

Covers: happy-path group/command/param parse; the BR-2 anti-drift guarantee
(modified help -> modified catalog, no code change); BR-8 error policy
(FileNotFoundError / non-zero exit / timeout -> CatalogError); BR-9 tolerance
(malformed/empty -> partial/empty, never raises); BR-5 required inference;
BR-6 choice parsing; and edge cases (per-session caching, no-option command).
"""

from __future__ import annotations

import subprocess
from typing import Dict, List, Sequence
from unittest import mock

import pytest

from cli_agent_orchestrator.tui.command_catalog import (
    CatalogError,
    Command,
    CommandCatalog,
    CommandGroup,
    Param,
)

# --------------------------------------------------------------------------- #
# Synthetic Click help fixtures (faithful to real `cao ... --help` layout).      #
# --------------------------------------------------------------------------- #

ROOT_HELP = """\
Usage: cao [OPTIONS] COMMAND [ARGS]...

  CLI Agent Orchestrator.

Options:
  -V, --version  Show the version and exit.
  --help         Show this message and exit.

Commands:
  launch   Launch cao session with specified agent profile.
  session  Manage CAO sessions.
  memory   Manage CAO memories.
"""

SESSION_GROUP_HELP = """\
Usage: cao session [OPTIONS] COMMAND [ARGS]...

  Manage CAO sessions.

Options:
  --help  Show this message and exit.

Commands:
  list    List all active CAO sessions.
  send    Send a message to a session's conductor (or specific terminal).
  status  Show status of a session's conductor (or specific terminal).
"""

SESSION_SEND_HELP = """\
Usage: cao session send [OPTIONS] SESSION_NAME MESSAGE

  Send a message to a session's conductor (or specific terminal).

Options:
  --terminal TEXT    Send to a specific terminal ID
  --async            Send and return immediately without waiting
  --timeout INTEGER  Timeout in seconds (default: 300s; ignored with --async)
  --help             Show this message and exit.
"""

# A choice option whose values wrap onto the next line (BR-6), plus a required
# positional (BR-5).
MEMORY_SHOW_HELP = """\
Usage: cao memory show [OPTIONS] KEY

  Display full content of a memory.

Options:
  --scope [global|project|session|agent|federated]
                                  Scope to search in. Searches all scopes if
                                  omitted.
  --help                          Show this message and exit.
"""

# A required option ([required]) with wrapped help, plus an optional positional.
LAUNCH_HELP = """\
Usage: cao launch [OPTIONS] [MESSAGE]

  Launch cao session with specified agent profile.

Options:
  --agents TEXT             Agent profile to launch  [required]
  --provider TEXT           Provider to use (default: profile provider or
                            kiro_cli)
  --headless                Launch in detached mode
  --help                    Show this message and exit.
"""

# A leaf command with only the universal --help flag and no positional args.
INFO_HELP = """\
Usage: cao info [OPTIONS]

  Display information about the current session.

Options:
  --help  Show this message and exit.
"""


def _fake_run(help_by_path: Dict[tuple, str]):
    """Build a ``subprocess.run`` replacement keyed by the command-path tuple.

    ``argv`` is ``["cao", *path, "--help"]``; the key is ``tuple(path)``. Unknown
    paths return a non-zero "No such command" result (mirroring Click).
    """

    calls: List[List[str]] = []

    def runner(argv: Sequence[str], **kwargs) -> mock.Mock:
        calls.append(list(argv))
        path = tuple(argv[1:-1])  # strip leading "cao" and trailing "--help"
        result = mock.Mock()
        if path in help_by_path:
            result.returncode = 0
            result.stdout = help_by_path[path]
            result.stderr = ""
        else:
            result.returncode = 2
            result.stdout = ""
            result.stderr = f"Error: No such command {' '.join(path)!r}."
        return result

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


# --------------------------------------------------------------------------- #
# Happy path — groups / commands / params.                                       #
# --------------------------------------------------------------------------- #


def test_groups_parses_command_section() -> None:
    runner = _fake_run({(): ROOT_HELP})
    with mock.patch("subprocess.run", runner):
        groups = CommandCatalog().groups()

    assert groups == [
        CommandGroup("launch", "Launch cao session with specified agent profile."),
        CommandGroup("session", "Manage CAO sessions."),
        CommandGroup("memory", "Manage CAO memories."),
    ]


def test_commands_parses_subcommands_with_path() -> None:
    runner = _fake_run({("session",): SESSION_GROUP_HELP})
    with mock.patch("subprocess.run", runner):
        commands = CommandCatalog().commands("session")

    assert commands == [
        Command("list", "List all active CAO sessions.", ["session", "list"]),
        Command(
            "send",
            "Send a message to a session's conductor (or specific terminal).",
            ["session", "send"],
        ),
        Command(
            "status",
            "Show status of a session's conductor (or specific terminal).",
            ["session", "status"],
        ),
    ]


def test_params_parses_options_and_positionals() -> None:
    runner = _fake_run({("session", "send"): SESSION_SEND_HELP})
    with mock.patch("subprocess.run", runner):
        params = CommandCatalog().params(["session", "send"])

    # --help is excluded; options come first, then usage-line positionals.
    assert params == [
        Param("--terminal", "option", False, True, None, "Send to a specific terminal ID"),
        Param(
            "--async", "option", False, False, None, "Send and return immediately without waiting"
        ),
        Param(
            "--timeout",
            "option",
            False,
            True,
            None,
            "Timeout in seconds (default: 300s; ignored with --async)",
        ),
        Param("SESSION_NAME", "argument", True, True, None, ""),
        Param("MESSAGE", "argument", True, True, None, ""),
    ]


# --------------------------------------------------------------------------- #
# BR-6 — choice parsing (including wrapped values).                              #
# --------------------------------------------------------------------------- #


def test_params_parses_choices_br6() -> None:
    runner = _fake_run({("memory", "show"): MEMORY_SHOW_HELP})
    with mock.patch("subprocess.run", runner):
        params = CommandCatalog().params(["memory", "show"])

    scope = next(p for p in params if p.name == "--scope")
    assert scope.choices == ["global", "project", "session", "agent", "federated"]
    assert scope.takes_value is True
    # The wrapped help text is reassembled.
    assert scope.help == "Scope to search in. Searches all scopes if omitted."
    # The required positional is also captured.
    assert Param("KEY", "argument", True, True, None, "") in params


# --------------------------------------------------------------------------- #
# BR-5 — conservative required inference.                                        #
# --------------------------------------------------------------------------- #


def test_params_infers_required_br5() -> None:
    runner = _fake_run({("launch",): LAUNCH_HELP})
    with mock.patch("subprocess.run", runner):
        params = CommandCatalog().params(["launch"])

    by_name = {p.name: p for p in params}
    # Option marked [required] in help -> required; the marker is stripped from help.
    assert by_name["--agents"].required is True
    assert "[required]" not in by_name["--agents"].help
    # Option not marked -> not required (conservative).
    assert by_name["--provider"].required is False
    # A flag (no metavar) does not take a value.
    assert by_name["--headless"].takes_value is False
    # [MESSAGE] is an OPTIONAL positional (bracketed) -> not required.
    message = by_name["MESSAGE"]
    assert message.kind == "argument"
    assert message.required is False


# --------------------------------------------------------------------------- #
# BR-2 — anti-drift: modified help -> modified catalog, ZERO code change.        #
# --------------------------------------------------------------------------- #


def test_anti_drift_new_command_appears_br2() -> None:
    """A brand-new command added to `cao --help` shows up with no code edit."""

    modified_root = ROOT_HELP.replace(
        "  memory   Manage CAO memories.\n",
        "  memory   Manage CAO memories.\n  brandnew  A command that did not exist before.\n",
    )
    runner = _fake_run({(): modified_root})
    with mock.patch("subprocess.run", runner):
        names = [g.name for g in CommandCatalog().groups()]

    assert "brandnew" in names, "catalog must reflect the live help, not a hardcoded set"


def test_anti_drift_new_flag_appears_br2() -> None:
    """A brand-new option flag is reflected without touching the parser."""

    modified = SESSION_SEND_HELP.replace(
        "  --async            Send and return immediately without waiting\n",
        "  --async            Send and return immediately without waiting\n"
        "  --brand-new-flag   A freshly added option.\n",
    )
    runner = _fake_run({("session", "send"): modified})
    with mock.patch("subprocess.run", runner):
        params = CommandCatalog().params(["session", "send"])

    assert any(p.name == "--brand-new-flag" for p in params)


# --------------------------------------------------------------------------- #
# BR-8 — error policy: not-found / non-zero / timeout -> CatalogError.           #
# --------------------------------------------------------------------------- #


def test_missing_binary_raises_catalog_error_br8() -> None:
    def boom(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "cao")

    with mock.patch("subprocess.run", boom):
        with pytest.raises(CatalogError) as excinfo:
            CommandCatalog().groups()

    assert "not found" in str(excinfo.value)
    assert excinfo.value.argv[-1] == "--help"


def test_non_zero_exit_raises_catalog_error_br8() -> None:
    # Empty fixture map => every path returns returncode 2 (Click "No such command").
    runner = _fake_run({})
    with mock.patch("subprocess.run", runner):
        with pytest.raises(CatalogError) as excinfo:
            CommandCatalog().commands("does-not-exist")

    assert "No such command" in excinfo.value.stderr


def test_timeout_raises_catalog_error_br7() -> None:
    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=list(argv), timeout=kwargs.get("timeout", 10))

    with mock.patch("subprocess.run", slow):
        with pytest.raises(CatalogError) as excinfo:
            CommandCatalog(timeout=0.01).groups()

    assert "timed out" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# BR-9 — tolerant parsing: malformed / empty -> partial or empty, never raise.   #
# --------------------------------------------------------------------------- #


def test_empty_help_returns_empty_not_raise_br9() -> None:
    runner = _fake_run({(): ""})
    with mock.patch("subprocess.run", runner):
        assert CommandCatalog().groups() == []


def test_help_without_commands_section_returns_empty_br9() -> None:
    text = "Usage: cao thing [OPTIONS]\n\n  A leaf.\n\nOptions:\n  --help  Show this.\n"
    runner = _fake_run({("thing",): text})
    with mock.patch("subprocess.run", runner):
        assert CommandCatalog().commands("thing") == []


def test_malformed_option_line_is_skipped_not_fatal_br9() -> None:
    """A garbled option entry (no flag) is omitted + logged; the rest still parse."""

    text = (
        "Usage: cao weird [OPTIONS]\n\n  Weird.\n\n"
        "Options:\n"
        "  garbage-with-no-dash   this should be skipped\n"
        "  --good TEXT            a valid option\n"
        "  --help                 Show this message and exit.\n"
    )
    runner = _fake_run({("weird",): text})
    with mock.patch("subprocess.run", runner):
        params = CommandCatalog().params(["weird"])

    names = [p.name for p in params]
    assert names == ["--good"]  # garbage line omitted, --help excluded


# --------------------------------------------------------------------------- #
# Edge cases.                                                                    #
# --------------------------------------------------------------------------- #


def test_command_with_no_options_yields_empty_params() -> None:
    runner = _fake_run({("info",): INFO_HELP})
    with mock.patch("subprocess.run", runner):
        params = CommandCatalog().params(["info"])

    # Only --help present (excluded) and no positionals -> empty.
    assert params == []


def test_results_are_cached_per_session() -> None:
    runner = _fake_run({(): ROOT_HELP})
    with mock.patch("subprocess.run", runner):
        catalog = CommandCatalog()
        catalog.groups()
        catalog.groups()
        catalog.groups()

    # Three calls to groups() => exactly ONE subprocess invocation (cached).
    assert len(runner.calls) == 1  # type: ignore[attr-defined]


# A help body where the Options: section is immediately followed by the
# Commands: section with no blank line between the last option and the next
# header — the back-to-back case that exercises `_section_lines`' termination
# on the next non-indented, non-empty line (the section-break branch).
BACK_TO_BACK_HELP = """\
Usage: cao stacked [OPTIONS] COMMAND [ARGS]...

  A group whose Options and Commands sections abut with no blank separator.

Options:
  --flag TEXT  A flag on the group itself.
Commands:
  alpha  The first subcommand.
  beta   The second subcommand.
"""


def test_section_terminates_on_next_header_without_blank_line() -> None:
    """`_section_lines` ends a section at the next header even with no blank line.

    Options: and Commands: abut directly (no separating blank line). The Options
    parse must stop at the ``Commands:`` header (not swallow it), and the Commands
    parse must find both subcommands — proving the non-indented-line break path.
    """

    runner = _fake_run({("stacked",): BACK_TO_BACK_HELP})
    with mock.patch("subprocess.run", runner):
        catalog = CommandCatalog()
        params = catalog.params(["stacked"])
        commands = catalog.commands("stacked")

    # Options parse stopped at the abutting Commands: header — exactly one option,
    # and no stray "Commands:"/subcommand tokens leaked in as params.
    option_names = [p.name for p in params if p.kind == "option"]
    assert option_names == ["--flag"]
    assert all(p.name not in {"Commands:", "alpha", "beta"} for p in params)
    # Commands section parsed independently and completely.
    assert [c.name for c in commands] == ["alpha", "beta"]


def test_positional_token_starting_with_dash_is_skipped() -> None:
    """A usage-line token starting with ``-`` is not emitted as a positional.

    Valid Click usage never renders a positional with a leading dash, so such a
    token is a parse artifact. ``_build_positional`` omits it (BR-3: omit rather
    than fabricate a bogus ``--weird`` argument Param).
    """

    # A hand-crafted usage line with a stray dash token after [OPTIONS]. This
    # cannot arise from real Click output, but the guard must hold if it does.
    text = (
        "Usage: cao odd [OPTIONS] --stray REAL_ARG\n\n"
        "  A command with a malformed usage line.\n\n"
        "Options:\n"
        "  --help  Show this message and exit.\n"
    )
    runner = _fake_run({("odd",): text})
    with mock.patch("subprocess.run", runner):
        params = CommandCatalog().params(["odd"])

    names = [p.name for p in params]
    # The stray "--stray" token is dropped; the genuine positional survives.
    assert "--stray" not in names
    assert "REAL_ARG" in names
    assert all(not n.startswith("-") for n in names if n != "--help")
