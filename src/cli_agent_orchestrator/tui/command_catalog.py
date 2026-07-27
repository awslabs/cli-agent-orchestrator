"""Command/param catalog for the ``cao tui`` thin shell (U2, the anti-drift keystone).

:class:`CommandCatalog` discovers the live ``cao`` command surface *exclusively*
by running ``cao ... --help`` over the ``subprocess`` boundary and parsing the
Click help sections. It NEVER imports the ``cli`` object or any heavy in-process
layer (``services``/``clients``/``backends``/``providers``/``models``) — importing
``cli`` transitively pulls ``backends.registry`` + ``services.settings_service``
(ADR-007). The subprocess boundary is the whole point: the catalog reflects
whatever ``cao`` currently exposes, so adding/removing a command changes the TUI
with zero code edits here (BR-1 / BR-2). The U1 AST guard
(``test/tui/test_thin_shell_boundary.py``) enforces the import boundary.

Import rule (thin shell): only the standard library and the ``tui`` package's own
modules may be imported here — see the package docstring in ``__init__``.

Design references: business-logic-model W-1..W-3, business-rules BR-1..BR-9,
domain-entities (CommandGroup / Command / Param / CatalogError).
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from typing import List, Literal, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Default timeout (seconds) bounding every ``cao ... --help`` call (BR-7). A hang
# is turned into a CatalogError for that node rather than freezing the TUI.
DEFAULT_TIMEOUT = 10.0

# The console-script name the catalog introspects. Overridable (tests, alt paths).
DEFAULT_EXECUTABLE = "cao"

# Click's auto-generated help flag — present on every command, so it is not a
# command-specific parameter. Excluded from ``params()`` (see BR-3 / the
# "no options -> empty params" edge case).
_HELP_FLAG = "--help"

# Generic Click *group* placeholders in a usage line (``COMMAND [ARGS]...``); these
# are the sub-dispatch tokens, not real positional arguments — always skipped.
_GROUP_PLACEHOLDERS = frozenset({"COMMAND", "ARGS"})

ParamKind = Literal["option", "argument"]


# --------------------------------------------------------------------------- #
# View models — LOCAL to the tui package (NOT cli_agent_orchestrator.models,     #
# which is a forbidden import per the U1 guard deny-list). Read-only, immutable  #
# projections of parsed ``cao ... --help`` output.                               #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CommandGroup:
    """A top-level ``cao`` command group (e.g. ``session``, ``launch``)."""

    name: str
    summary: str


@dataclass(frozen=True)
class Command:
    """A subcommand within a group (or a leaf command itself)."""

    name: str
    summary: str
    path: List[str]  # full argv path prefix, e.g. ["session", "status"]


@dataclass(frozen=True)
class Param:
    """A parsed parameter of a command — an option flag or a positional argument."""

    name: str  # e.g. "--provider" for an option, or "SESSION_NAME" for an argument
    kind: ParamKind
    required: bool
    takes_value: bool
    choices: Optional[List[str]]
    help: str


class CatalogError(Exception):
    """Raised when ``cao ... --help`` cannot be run or exits non-zero (BR-8).

    Carries the failing argv and captured stderr so the App (U1) can render a
    fatal "cao not found" state. Never raised for a merely malformed/empty help
    body — that degrades gracefully to a partial/empty parse (BR-9).
    """

    def __init__(
        self, argv: Sequence[str], stderr: str = "", message: Optional[str] = None
    ) -> None:
        self.argv: List[str] = list(argv)
        self.stderr: str = stderr
        detail = stderr.strip() or "command not runnable"
        super().__init__(message or f"`{' '.join(self.argv)}` failed: {detail}")


# --------------------------------------------------------------------------- #
# Pure parsing helpers (module-level, side-effect free, unit-testable).          #
# Parsing is line/section based on Click's stable help layout.                   #
# --------------------------------------------------------------------------- #

# A new option entry: exactly two-space indent, then a dash. Continuation (wrapped
# help / choice values) is indented deeper and never starts with a dash.
_OPTION_LINE = re.compile(r"^  -")

# A command-list entry: two-space indent, a name token, optional 2+-space gap +
# summary. Deeper-indented wraps fail this (char 2 is whitespace) -> continuation.
_COMMAND_LINE = re.compile(r"^  (\S+)(?:\s{2,}(.*))?$")

# Split a stripped line into (signature/name, help/summary) on the first 2+-space gap.
_COLUMN_GAP = re.compile(r"\s{2,}")

# A bracketed choice metavar, e.g. "[global|project|session]".
_BRACKET_CHOICE = re.compile(r"^\[(.+)\]$")


def _collapse_ws(text: str) -> str:
    """Collapse runs of whitespace to single spaces and strip the ends."""

    return re.sub(r"\s+", " ", text).strip()


def _section_lines(text: str, header: str) -> List[str]:
    """Return the indented body lines under a ``header`` (e.g. ``"Options:"``).

    The body runs from the line after the header until the next non-indented,
    non-empty line (the next section header, or EOF). A missing header yields an
    empty list — tolerant parsing, never an error (BR-9).
    """

    lines = text.splitlines()
    body: List[str] = []
    in_section = False
    for line in lines:
        if not in_section:
            if line.strip() == header:
                in_section = True
            continue
        # A non-indented, non-empty line ends the section (next header / token).
        if line.strip() and not line[:1].isspace():
            break
        body.append(line)
    return body


def _usage_text(text: str) -> str:
    """Return the joined usage line (``Usage:`` + any wrapped continuation lines)."""

    out: List[str] = []
    started = False
    for line in text.splitlines():
        if not started:
            if line.startswith("Usage:"):
                started = True
                out.append(line[len("Usage:") :].strip())
            continue
        # Wrapped usage continues on indented lines until the first blank line.
        if line.strip() == "":
            break
        if line[:1].isspace():
            out.append(line.strip())
        else:
            break
    return _collapse_ws(" ".join(out))


def _parse_command_entries(text: str) -> List[Tuple[str, str]]:
    """Parse the ``Commands:`` section into ``(name, summary)`` pairs.

    Used by both :meth:`CommandCatalog.groups` and :meth:`CommandCatalog.commands`.
    A leaf command (no ``Commands:`` section) yields ``[]`` (BR-9).
    """

    entries: List[Tuple[str, str]] = []
    name: Optional[str] = None
    summary_parts: List[str] = []

    def flush() -> None:
        nonlocal name, summary_parts
        if name is not None:
            entries.append((name, _collapse_ws(" ".join(summary_parts))))
        name = None
        summary_parts = []

    for line in _section_lines(text, "Commands:"):
        match = _COMMAND_LINE.match(line)
        if match:
            flush()
            name = match.group(1)
            summary_parts = [match.group(2)] if match.group(2) else []
        elif line.strip() == "":
            flush()
        elif name is not None and line[:1].isspace():
            summary_parts.append(line.strip())
        else:
            flush()
    flush()
    return entries


def _build_option(signature: str, help_text: str) -> Optional[Param]:
    """Build an option :class:`Param` from its signature + accumulated help.

    Returns ``None`` (and logs) for a signature with no parseable flag, and for the
    universal ``--help`` flag (excluded — it is not a command-specific param). A
    param that resists parsing is omitted, never guessed (BR-3 / RD-d=A).
    """

    tokens = signature.replace(",", " ").split()
    flags = [tok for tok in tokens if tok.startswith("-")]
    metavar_tokens = [tok for tok in tokens if not tok.startswith("-")]

    if not flags:
        logger.warning("catalog: skipping unparseable option signature: %r", signature)
        return None

    # Prefer the long form (``--foo``) as the canonical name; fall back to short.
    name = next((flag for flag in flags if flag.startswith("--")), flags[0])
    if name == _HELP_FLAG:
        return None

    metavar = " ".join(metavar_tokens)
    takes_value = bool(metavar_tokens)

    choices: Optional[List[str]] = None
    if metavar:
        bracket = _BRACKET_CHOICE.match(metavar)
        if bracket and "|" in bracket.group(1):
            choices = [choice.strip() for choice in bracket.group(1).split("|") if choice.strip()]

    required = "[required]" in help_text
    clean_help = _collapse_ws(help_text.replace("[required]", ""))

    return Param(
        name=name,
        kind="option",
        required=required,
        takes_value=takes_value,
        choices=choices,
        help=clean_help,
    )


def _parse_options(text: str) -> List[Param]:
    """Parse the ``Options:`` section into option :class:`Param` objects (W-3).

    Handles Click's multi-line wrapping: a signature whose choices/help spill onto
    following indented lines is reassembled. ``--help`` is filtered out.
    """

    params: List[Param] = []
    signature: Optional[str] = None
    help_parts: List[str] = []

    def flush() -> None:
        nonlocal signature, help_parts
        if signature is not None:
            param = _build_option(signature, " ".join(help_parts))
            if param is not None:
                params.append(param)
        signature = None
        help_parts = []

    for line in _section_lines(text, "Options:"):
        if _OPTION_LINE.match(line):
            flush()
            stripped = line.strip()
            parts = _COLUMN_GAP.split(stripped, maxsplit=1)
            signature = parts[0]
            help_parts = [parts[1]] if len(parts) > 1 else []
        elif line.strip() == "":
            flush()
        elif signature is not None and line[:1].isspace():
            help_parts.append(line.strip())
        else:
            flush()
    flush()
    return params


def _build_positional(token: str) -> Optional[Param]:
    """Build a positional-argument :class:`Param` from one usage-line token (W-3).

    Required-ness is inferred conservatively from Click usage syntax (BR-5):
    ``ARG`` = required, ``[ARG]`` = optional. Trailing ``...`` (variadic) is
    stripped. The generic group placeholders ``COMMAND``/``ARGS`` are skipped, as
    is any option-like token (leading dash) — a positional never starts with a
    dash in valid Click usage, so such a token is a parse artifact, not an
    argument (BR-3: omit rather than fabricate a bogus ``--foo`` positional).
    """

    name = token
    required = True

    if name.endswith("..."):  # variadic marker; required-ness comes from brackets
        name = name[:-3]

    if name.startswith("[") and name.endswith("]"):
        required = False
        name = name[1:-1]
    elif name.startswith("{") and name.endswith("}"):  # required Choice positional
        name = name[1:-1]

    choices: Optional[List[str]] = None
    if "|" in name:  # a choice positional rendered as {a|b|c} / [a|b|c]
        choices = [choice.strip() for choice in name.split("|") if choice.strip()]

    if not name or name in _GROUP_PLACEHOLDERS or name.startswith("-"):
        return None

    return Param(
        name=name,
        kind="argument",
        required=required,
        takes_value=True,
        choices=choices,
        help="",
    )


def _parse_positionals(text: str, path: Sequence[str]) -> List[Param]:
    """Parse the usage line's positional arguments into :class:`Param` objects (W-3)."""

    usage = _usage_text(text)
    if not usage:
        return []

    tokens = usage.split()
    if "[OPTIONS]" in tokens:
        rest = tokens[tokens.index("[OPTIONS]") + 1 :]
    else:
        # No [OPTIONS] marker: drop the leading executable + command-path tokens.
        prefix = 1 + len(list(path))
        rest = tokens[prefix:] if len(tokens) > prefix else []

    params: List[Param] = []
    for token in rest:
        param = _build_positional(token)
        if param is not None:
            params.append(param)
    return params


# --------------------------------------------------------------------------- #
# The catalog.                                                                   #
# --------------------------------------------------------------------------- #


class CommandCatalog:
    """Live, cached projection of the ``cao`` command surface via ``--help`` (W-1..W-3).

    Every lookup shells out to ``cao ... --help`` (BR-1) and parses the result;
    raw help text is cached per instance keyed by argv path, so a ``cao tui``
    session runs each ``--help`` at most once (the surface is stable for a run).
    """

    def __init__(
        self,
        executable: str = DEFAULT_EXECUTABLE,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._executable = executable
        self._timeout = timeout
        self._cache: dict[Tuple[str, ...], str] = {}

    # -- subprocess boundary ------------------------------------------------- #

    def _help_text(self, path: Sequence[str]) -> str:
        """Run ``cao <path...> --help`` (cached) and return stdout.

        Non-zero exit, missing binary, or timeout -> :class:`CatalogError` (BR-7/BR-8).
        """

        key = tuple(path)
        if key in self._cache:
            return self._cache[key]

        argv = [self._executable, *path, "--help"]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except FileNotFoundError as exc:
            raise CatalogError(argv, message=f"`{self._executable}` executable not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise CatalogError(
                argv, message=f"`{' '.join(argv)}` timed out after {self._timeout}s"
            ) from exc

        if proc.returncode != 0:
            raise CatalogError(argv, stderr=proc.stderr or "")

        self._cache[key] = proc.stdout
        return proc.stdout

    # -- public API ---------------------------------------------------------- #

    def groups(self) -> List[CommandGroup]:
        """Return the top-level command groups parsed from ``cao --help`` (W-1)."""

        text = self._help_text([])
        return [
            CommandGroup(name=name, summary=summary)
            for name, summary in _parse_command_entries(text)
        ]

    def commands(self, group: str) -> List[Command]:
        """Return a group's subcommands from ``cao <group> --help`` (W-2).

        A leaf command (no ``Commands:`` section) yields ``[]``.
        """

        text = self._help_text([group])
        return [
            Command(name=name, summary=summary, path=[group, name])
            for name, summary in _parse_command_entries(text)
        ]

    def params(self, command_path: Sequence[str]) -> List[Param]:
        """Return every parseable parameter of a command from its ``--help`` (W-3).

        Options first, then usage-line positionals. Full introspection (RD-d=A /
        BR-3): every parseable param is returned; unparseable ones are omitted and
        logged, never fabricated. A malformed/empty help body yields a partial or
        empty list rather than raising (BR-9).
        """

        text = self._help_text(command_path)
        return _parse_options(text) + _parse_positionals(text, command_path)
