"""Unit tests for :mod:`cli_agent_orchestrator.tui.command_builder` (U3).

Covers the FR-3.1 / BR-1 byte-identical invariant (one ``preview_argv()`` feeds
both the preview string and the runner), Click-correct argv ordering, the SC-3 /
BR-4 path-validation routing (path args go through U5 ``PathInput`` before being
recorded; a rejected path is not stored), the U2 BR-5 *advisory* handling
(inferred-required positionals never hard-block run/copy), and edge cases
(no-arg command, options-only command).

No real ``cao`` binary and no real filesystem policy are exercised — ``PathInput``
is patched where its routing is under test.
"""

from __future__ import annotations

import shlex
from unittest import mock

import pytest

from cli_agent_orchestrator.tui.command_builder import BuilderState, CommandBuilder
from cli_agent_orchestrator.tui.command_catalog import Param
from cli_agent_orchestrator.tui.path_input import PathInputError
from cli_agent_orchestrator.tui.runner import CommandRunner, RunResult

# --------------------------------------------------------------------------- #
# Param fixtures (faithful to what U2's catalog produces).                       #
# --------------------------------------------------------------------------- #


def _opt(name: str, *, required: bool = False, takes_value: bool = True) -> Param:
    return Param(
        name=name,
        kind="option",
        required=required,
        takes_value=takes_value,
        choices=None,
        help="",
    )


def _arg(name: str, *, required: bool = True) -> Param:
    return Param(
        name=name,
        kind="argument",
        required=required,
        takes_value=True,
        choices=None,
        help="",
    )


# A representative `cao launch` shape: a path option, a value option, a boolean
# flag, and a positional argument.
LAUNCH_PARAMS = [
    _opt("--provider", required=True),
    _opt("--working-directory"),
    _opt("--detach", takes_value=False),
    _arg("AGENT_PROFILE", required=True),
]


# --------------------------------------------------------------------------- #
# W-2/W-3: argv ordering + string derivation.                                    #
# --------------------------------------------------------------------------- #


def test_preview_argv_orders_options_before_positionals() -> None:
    """Options (flag + value / bare flag) precede positionals, in declared order."""

    builder = CommandBuilder()
    builder.select(["launch"], params=LAUNCH_PARAMS)
    builder.set_arg("--provider", "kiro_cli")
    builder.set_arg("--detach", "true")
    builder.set_arg("AGENT_PROFILE", "backend-dev")

    assert builder.preview_argv() == [
        "cao",
        "launch",
        "--provider",
        "kiro_cli",
        "--detach",
        "backend-dev",
    ]


def test_preview_string_is_shlex_join_of_argv() -> None:
    """``preview_string()`` is exactly ``shlex.join(preview_argv())`` (W-3)."""

    builder = CommandBuilder()
    builder.select(["session", "send"], params=[_opt("--message"), _arg("SESSION")])
    builder.set_arg("--message", "hello world")  # space -> must be quoted
    builder.set_arg("SESSION", "sess-1")

    argv = builder.preview_argv()
    assert builder.preview_string() == shlex.join(argv)
    # And the quoting is real (space forces quotes), proving it is shlex-derived.
    assert "'hello world'" in builder.preview_string()


# --------------------------------------------------------------------------- #
# BR-1 / FR-3.1: byte-identical preview vs. run — ONE source.                    #
# --------------------------------------------------------------------------- #


def test_run_receives_exact_preview_argv_one_source() -> None:
    """The argv CommandRunner.run executes is the SAME list preview_argv() yields.

    Not two independent renderings that happen to match: we capture
    ``preview_argv()`` from the state and pass *that* to the runner, and assert
    the runner forwarded the identical list to ``subprocess.run`` (BR-1).
    """

    builder = CommandBuilder()
    builder.select(["launch"], params=LAUNCH_PARAMS)
    builder.set_arg("--provider", "kiro_cli")
    builder.set_arg("AGENT_PROFILE", "backend-dev")

    argv = builder.preview_argv()  # the single source

    runner = CommandRunner()
    with mock.patch("cli_agent_orchestrator.tui.runner.subprocess.run") as sp_run:
        sp_run.return_value = mock.Mock(stdout="ok", stderr="", returncode=0)
        runner.run(argv)

    # subprocess.run got the exact argv list preview produced.
    called_argv = sp_run.call_args.args[0]
    assert called_argv == argv
    # And the previewed string is derived from that same argv (no second render).
    assert builder.preview_string() == shlex.join(argv)


# --------------------------------------------------------------------------- #
# W-1 / SC-3 / BR-4: path args route through U5 PathInput.                        #
# --------------------------------------------------------------------------- #


def test_path_arg_routes_through_pathinput_validate() -> None:
    """A known path flag is validated by U5 before being recorded (SC-3)."""

    builder = CommandBuilder()
    builder.select(["launch"], params=LAUNCH_PARAMS)

    with mock.patch("cli_agent_orchestrator.tui.command_builder.PathInput") as PI:
        PI.return_value.validate.return_value = "/canonical/dir"
        stored = builder.set_arg("--working-directory", "~/work")

    PI.return_value.validate.assert_called_once()
    # The raw value was passed to the validator...
    assert PI.return_value.validate.call_args.args[0] == "~/work"
    # ...and the CANONICAL result (not the raw text) is what gets recorded.
    assert stored == "/canonical/dir"
    assert builder.state.args["--working-directory"] == "/canonical/dir"
    assert "/canonical/dir" in builder.preview_argv()


def test_invalid_path_surfaces_error_and_arg_not_recorded() -> None:
    """A rejected path raises PathInputError and the arg is left unset (BR-4)."""

    builder = CommandBuilder()
    builder.select(["launch"], params=LAUNCH_PARAMS)

    with mock.patch("cli_agent_orchestrator.tui.command_builder.PathInput") as PI:
        PI.return_value.validate.side_effect = PathInputError("Working directory not allowed: /etc")
        with pytest.raises(PathInputError):
            builder.set_arg("--working-directory", "/etc")

    # Not recorded — the field stays invalid, nothing leaks into the argv.
    assert "--working-directory" not in builder.state.args
    assert "--working-directory" not in builder.preview_argv()


def test_non_path_arg_does_not_touch_pathinput() -> None:
    """A plain value arg is stored verbatim without invoking the validator."""

    builder = CommandBuilder()
    builder.select(["launch"], params=LAUNCH_PARAMS)

    with mock.patch("cli_agent_orchestrator.tui.command_builder.PathInput") as PI:
        builder.set_arg("--provider", "kiro_cli")

    PI.return_value.validate.assert_not_called()
    assert builder.state.args["--provider"] == "kiro_cli"


def test_output_dir_path_allows_create(tmp_path) -> None:
    """``--output-dir`` routes through PathInput with ``allow_create=True``."""

    builder = CommandBuilder()
    builder.select(["profile", "generate"], params=[_opt("--output-dir")])

    with mock.patch("cli_agent_orchestrator.tui.command_builder.PathInput") as PI:
        PI.return_value.validate.return_value = "/canonical/out"
        builder.set_arg("--output-dir", str(tmp_path / "new"))

    assert PI.return_value.validate.call_args.kwargs["allow_create"] is True


# --------------------------------------------------------------------------- #
# BR-5 (advisory): inferred-required positional must NOT hard-block.             #
# --------------------------------------------------------------------------- #


def test_inferred_required_positional_does_not_block_run() -> None:
    """A missing required *positional* leaves the command runnable (BR-5 advisory).

    Click renders optional positionals as ``[ARG]``, so U2 may over-report them
    as required. is_complete() must ignore positionals; the missing positional
    is a soft warning only, and run/copy stay available.
    """

    builder = CommandBuilder()
    # Only a required positional is declared, and it is NOT set.
    builder.select(["session", "status"], params=[_arg("SESSION_NAME", required=True)])

    # Not hard-blocked: is_complete() true despite the "required" positional...
    assert builder.is_complete() is True
    # ...but surfaced as an advisory warning.
    warnings = builder.soft_warnings()
    assert any("SESSION_NAME" in w and "advisory" in w for w in warnings)
    # required_missing still reports it (for callers that want the raw list).
    assert "SESSION_NAME" in builder.required_missing()
    # And the command is still runnable (argv builds fine).
    assert builder.preview_argv() == ["cao", "session", "status"]


def test_missing_required_option_marks_incomplete_but_still_runnable() -> None:
    """A missing required *option* flips is_complete() false yet never blocks run.

    Required options are a firmer signal than positionals, so is_complete()
    reflects them — but the builder still renders a runnable argv and copy/run
    are allowed (the cao CLI is the real validator).
    """

    builder = CommandBuilder()
    builder.select(["launch"], params=[_opt("--provider", required=True)])

    assert builder.is_complete() is False
    assert any("--provider is required" in w for w in builder.soft_warnings())
    # Still renders an argv (nothing raises, run path stays open).
    assert builder.preview_argv() == ["cao", "launch"]


# --------------------------------------------------------------------------- #
# Edge cases.                                                                    #
# --------------------------------------------------------------------------- #


def test_no_args_command_previews_bare_path() -> None:
    """A command with no params previews just ``cao <path>`` (edge case)."""

    builder = CommandBuilder()
    builder.select(["session", "list"], params=[])

    assert builder.preview_argv() == ["cao", "session", "list"]
    assert builder.preview_string() == "cao session list"
    assert builder.is_complete() is True
    assert builder.soft_warnings() == []


def test_options_only_command(tmp_path) -> None:
    """An options-only command (no positionals) renders flag/value pairs (edge case)."""

    builder = CommandBuilder()
    builder.select(
        ["memory", "search"],
        params=[_opt("--query"), _opt("--json", takes_value=False)],
    )
    builder.set_arg("--query", "budget")
    builder.set_arg("--json", "true")

    assert builder.preview_argv() == ["cao", "memory", "search", "--query", "budget", "--json"]


def test_select_resets_previous_args() -> None:
    """Selecting a new command clears prior args so none leak across commands."""

    builder = CommandBuilder()
    builder.select(["launch"], params=LAUNCH_PARAMS)
    builder.set_arg("--provider", "kiro_cli")
    assert builder.state.args

    builder.select(["session", "list"], params=[])
    assert builder.state.args == {}
    assert builder.preview_argv() == ["cao", "session", "list"]


def test_select_without_catalog_or_params_raises() -> None:
    """Selecting with neither a bound catalog nor explicit params is an error."""

    builder = CommandBuilder()
    with pytest.raises(ValueError):
        builder.select(["launch"])


def test_select_uses_bound_catalog_params() -> None:
    """When a catalog is bound and no params are passed, its params() is used."""

    catalog = mock.Mock()
    catalog.params.return_value = [_opt("--provider")]
    builder = CommandBuilder(catalog=catalog)

    builder.select(["launch"])

    catalog.params.assert_called_once_with(["launch"])
    assert builder.params == [_opt("--provider")]


def test_builder_state_is_local_dataclass() -> None:
    """BuilderState is a plain local dataclass (domain-entities), not a model import."""

    state = BuilderState(command_path=["launch"], args={"--provider": "kiro_cli"})
    assert state.command_path == ["launch"]
    assert state.args == {"--provider": "kiro_cli"}


def test_run_result_shape_for_builder_roundtrip() -> None:
    """Sanity: the RunResult the runner returns carries verbatim fields (cross-check)."""

    result = RunResult(stdout="out", stderr="err", exit_code=0)
    assert (result.stdout, result.stderr, result.exit_code) == ("out", "err", 0)
