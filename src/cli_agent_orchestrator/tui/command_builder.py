"""Command builder + exact preview for the ``cao tui`` thin shell (U3).

:class:`CommandBuilder` owns the *command-string primitive*: it accumulates a
selected ``cao`` command plus its argument values (:class:`BuilderState`) and
renders them into a single argv list via :meth:`CommandBuilder.preview_argv`.
That one list is the **single source of truth** (FR-3.1 / BR-1): the always-
visible preview pane displays it (via :meth:`preview_string`) and
:class:`~cli_agent_orchestrator.tui.runner.CommandRunner` executes the *same*
list — guaranteeing the previewed command is byte-identical to the one that
runs.

Path-typed arguments (a known directory flag like ``--working-directory`` or a
param whose name reads as a directory) are routed through U5
:class:`~cli_agent_orchestrator.tui.path_input.PathInput` *before* the value is
recorded (SC-3 / BR-4): a rejected path raises the validator's field error and
the argument is left unset.

**U2 BR-5 is treated as ADVISORY here.** Click renders an optional positional as
``[ARG]`` and a required one as ``ARG``; the U2 catalog infers required-ness
from exactly that bracketing, so an unbracketed-but-actually-optional positional
is over-reported as required. Therefore :meth:`is_complete` never hard-blocks a
run on a missing required *positional* — it only reflects missing required
*options* (a firmer signal), and even those never block the run/copy path. The
``cao`` CLI is the real validator on run; U3 surfaces missing params as soft
warnings (:meth:`soft_warnings`) instead of gating.

Import rule (thin shell, enforced by ``test/tui/test_thin_shell_boundary.py``):
only the standard library, ``prompt_toolkit`` and the ``tui`` package's own
modules (:mod:`.command_catalog`, :mod:`.path_input`) may be imported here. No
``cli``/``services``/``clients``/``backends``/``providers``/``models`` layer.

Design references: business-logic-model W-1..W-3, business-rules BR-1/BR-4/BR-6,
domain-entities (BuilderState), code-generation-plan Step 1.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from cli_agent_orchestrator.tui.command_catalog import CommandCatalog, Param
from cli_agent_orchestrator.tui.path_input import PathInput

# The console-script name that heads every rendered argv (argv[0]). The runner
# executes exactly this token, so preview and run share one executable name.
DEFAULT_EXECUTABLE = "cao"

# Directory-style flags cao exposes whose values must route through U5
# PathInput (SC-3). Kept deliberately small and explicit; the name heuristic
# below widens coverage without hard-coding the full CLI surface.
KNOWN_PATH_FLAGS = frozenset({"--working-directory", "--output-dir"})

# Path flags whose target is *created* on run (validated with allow_create=True,
# so a not-yet-existing directory under a good ancestor is accepted). Everything
# else must already exist.
CREATE_PATH_FLAGS = frozenset({"--output-dir"})


# --------------------------------------------------------------------------- #
# Local domain model (in tui/, NOT cli_agent_orchestrator.models — a forbidden  #
# import per the U1 guard).                                                     #
# --------------------------------------------------------------------------- #


@dataclass
class BuilderState:
    """Mutable state of one command-build session (domain-entities).

    ``command_path`` is the selected ``cao`` command (e.g. ``["session",
    "status"]``); ``args`` maps a param name to its recorded value (path values
    are pre-canonicalized by U5 before landing here). ``preview_argv`` /
    ``preview_string`` are pure reads of this state — they never mutate it.
    """

    command_path: List[str] = field(default_factory=list)
    args: dict[str, str] = field(default_factory=dict)


def _is_path_param(param: Param) -> bool:
    """True when ``param`` names a directory-style path that U5 must validate.

    Detection is a small known-flag set plus a conservative name heuristic
    (names reading as a *directory* — ``PathInput`` is directory-only). File-
    valued flags are intentionally NOT matched so they are not run through a
    directory validator.
    """

    if param.name in KNOWN_PATH_FLAGS:
        return True
    normalized = param.name.lstrip("-").replace("_", "-").lower()
    return (
        normalized == "dir"
        or normalized.endswith("-dir")
        or "directory" in normalized
        or normalized == "workdir"
    )


def _allow_create_for(param: Param) -> bool:
    """Whether a path param's target may not yet exist (created on run)."""

    if param.name in CREATE_PATH_FLAGS:
        return True
    return "output" in param.name.lstrip("-").lower()


def _path_description(param: Param) -> str:
    """A human label for field-error messages (e.g. ``--working-directory`` ->
    ``"Working directory"``)."""

    words = param.name.lstrip("-").replace("_", " ").replace("-", " ").strip()
    return words.capitalize() if words else "Path"


class CommandBuilder:
    """Accumulate a ``cao`` command + args and render the canonical argv (W-1..W-3).

    A builder is bound to an optional :class:`CommandCatalog` used to resolve a
    command's parameters on :meth:`select`. Params may also be injected directly
    (tests, or a caller that already holds them), keeping the builder usable
    without shelling out.
    """

    def __init__(
        self,
        catalog: Optional[CommandCatalog] = None,
        *,
        executable: str = DEFAULT_EXECUTABLE,
    ) -> None:
        """Bind an optional catalog and the executable name that heads the argv.

        Args:
            catalog: Source of :class:`Param` metadata for a selected command.
                When ``None``, callers must pass ``params`` to :meth:`select`.
            executable: The argv[0] token (defaults to ``"cao"``); the runner
                executes exactly this, so preview and run stay identical.
        """

        self._catalog = catalog
        self._executable = executable
        self._state = BuilderState()
        self._params: List[Param] = []

    # -- state accessors ----------------------------------------------------- #

    @property
    def state(self) -> BuilderState:
        """The live :class:`BuilderState` (command_path + recorded args)."""

        return self._state

    @property
    def params(self) -> List[Param]:
        """The parameters of the currently selected command (options first)."""

        return list(self._params)

    # -- W-1: selection + arg entry ----------------------------------------- #

    def select(
        self,
        command_path: Sequence[str],
        *,
        params: Optional[Sequence[Param]] = None,
    ) -> None:
        """Select a command, resetting any in-progress args (W-1).

        Params are resolved from ``params`` when given, else from the bound
        catalog. Selecting a new command clears previously entered args so a
        stale value can never leak into a different command's argv.

        Args:
            command_path: The ``cao`` command path (e.g. ``["session",
                "status"]``).
            params: Optional explicit parameter list; when omitted the bound
                catalog is queried.

        Raises:
            ValueError: If no ``params`` are supplied and no catalog is bound.
        """

        self._state = BuilderState(command_path=list(command_path))
        if params is not None:
            self._params = list(params)
        elif self._catalog is not None:
            self._params = list(self._catalog.params(command_path))
        else:
            raise ValueError("no catalog bound and no params supplied to select()")

    def set_arg(self, param_name: str, value: str) -> str:
        """Record an argument value, validating path args through U5 first (W-1/SC-3).

        For a path-typed param the raw text is canonicalized/policy-checked by
        :meth:`PathInput.validate` *before* it is stored; a rejected path raises
        :class:`~cli_agent_orchestrator.tui.path_input.PathInputError` and the
        argument is left unrecorded (surface the error inline next to the
        field). Non-path values are stored verbatim.

        Args:
            param_name: The param name/flag to set (e.g. ``--working-directory``
                or ``SESSION_NAME``).
            value: The raw user-entered value.

        Returns:
            The value actually stored (canonical absolute path for path args,
            otherwise the raw value).

        Raises:
            PathInputError: If a path arg fails U5 validation; the arg is not
                recorded (BR-4).
        """

        param = self._param_by_name(param_name)
        if param is not None and _is_path_param(param):
            # SC-3: the shared validator is the single authority. On failure it
            # raises PathInputError; we deliberately do NOT record the arg (the
            # raise happens before assignment), so the field stays invalid.
            validated = PathInput(_path_description(param)).validate(
                value, allow_create=_allow_create_for(param)
            )
            self._state.args[param_name] = validated
            return validated

        self._state.args[param_name] = value
        return value

    def clear_arg(self, param_name: str) -> None:
        """Remove a recorded argument value if present (no-op otherwise)."""

        self._state.args.pop(param_name, None)

    # -- W-2/W-3: the single-source argv + string --------------------------- #

    def preview_argv(self) -> List[str]:
        """Render the canonical argv — the SINGLE source for display and run (W-2/BR-1).

        Pure function of the current :class:`BuilderState` (+ the selected
        command's param metadata): ``[executable, *command_path, *options,
        *positionals]`` in Click-accepted order (options first, then
        positionals, each in declaration order). This exact list is what
        :meth:`preview_string` shows and what
        :class:`~cli_agent_orchestrator.tui.runner.CommandRunner` executes, so
        the previewed command is byte-identical to the run command (FR-3.1).

        Returns:
            The argv list, e.g. ``["cao", "session", "status", "--json",
            "my-session"]``.
        """

        options_argv: List[str] = []
        positional_argv: List[str] = []
        consumed: set[str] = set()

        for param in self._params:
            if param.name not in self._state.args:
                continue
            value = self._state.args[param.name]
            consumed.add(param.name)
            if param.kind == "option":
                if param.takes_value:
                    options_argv.extend([param.name, value])
                else:
                    # Boolean/flag option: presence enables it; value ignored.
                    options_argv.append(param.name)
            else:  # positional argument — no flag token
                positional_argv.append(value)

        # Defensive: any recorded arg not matched to a known param (e.g. params
        # were never loaded) is still rendered deterministically by insertion
        # order — dashed names as options-with-value, others as positionals.
        for name, value in self._state.args.items():
            if name in consumed:
                continue
            if name.startswith("-"):
                options_argv.extend([name, value])
            else:
                positional_argv.append(value)

        return [self._executable, *self._state.command_path, *options_argv, *positional_argv]

    def preview_string(self) -> str:
        """The shell-safe single-line rendering of :meth:`preview_argv` (W-3).

        ``shlex.join(preview_argv())`` — the string shown in the preview pane
        and placed on the clipboard by copy (FR-3.2). Derived from the same argv
        the runner executes, so it can never drift from what runs.
        """

        return shlex.join(self.preview_argv())

    # -- completeness (advisory, BR-5) -------------------------------------- #

    def required_missing(self) -> List[str]:
        """Names of required params that have no value yet (both kinds).

        Feeds :meth:`soft_warnings`; positionals here are advisory only (see the
        class docstring and :meth:`is_complete`).
        """

        return [
            param.name
            for param in self._params
            if param.required and param.name not in self._state.args
        ]

    def is_complete(self) -> bool:
        """Whether every *required option* has a value — never blocks on positionals.

        BR-5 is advisory: the U2 catalog over-reports optional positionals as
        required (Click renders optional positionals as ``[ARG]``), so a missing
        required *positional* must NOT make a runnable command look incomplete.
        Only missing required *options* (a firmer signal) flip this to ``False``
        — and even that never hard-blocks run/copy, which stay allowed so the
        ``cao`` CLI can be the real validator. Callers should treat this as a
        hint and pair it with :meth:`soft_warnings`.
        """

        for param in self._params:
            if param.kind == "option" and param.required and param.name not in self._state.args:
                return False
        return True

    def soft_warnings(self) -> List[str]:
        """Advisory nudges for missing required params — never a hard gate (BR-5).

        Returns a message per missing required param. Missing required
        *positionals* are flagged as *possibly* required (the inference may be
        wrong); missing required *options* are flagged plainly. Run and copy
        remain available regardless — these are shown, not enforced.
        """

        warnings: List[str] = []
        for param in self._params:
            if not param.required or param.name in self._state.args:
                continue
            if param.kind == "argument":
                warnings.append(
                    f"{param.name} may be required " "(advisory — the cao CLI validates on run)"
                )
            else:
                warnings.append(f"{param.name} is required")
        return warnings

    # -- internals ----------------------------------------------------------- #

    def _param_by_name(self, name: str) -> Optional[Param]:
        """Return the selected command's param with ``name``, or ``None``."""

        for param in self._params:
            if param.name == name:
                return param
        return None
