"""Exact argv binding for a managed Codex native-TUI session.

Codex assigns the session id when ``thread/start`` creates a persistent
thread.  The zero-turn bootstrap records that id, exits, and the native TUI
then attaches with ``codex resume <id>``.  A picker, ``--last``, fork, or
non-persistent session is never an acceptable substitute for that exact id.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence

from cli_agent_orchestrator.services import provider_contracts

RESUME_COMMAND = "resume"
FORBIDDEN_OPTIONS = (
    "--last",
    "--all",
    "--include-non-interactive",
    "fork",
    "--ephemeral",
    "--no-session-persistence",
)
_ALLOWED_PROFILE_OPTIONS = {
    "--yolo": 0,
    "--no-alt-screen": 0,
    "--disable": 1,
    "--profile": 1,
    "--model": 1,
    "-c": 1,
}
_EARLY_EXIT_OR_DELIMITER_OPTIONS = frozenset({"--help", "-h", "--version", "-V", "--"})
_SESSION_ID = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)


class CodexNativeLaunchError(ValueError):
    """A Codex native resume argv could not be bound safely."""


def validate_session_id(session_id: object) -> str:
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise CodexNativeLaunchError(
            "codex native session id must be a canonical lowercase UUID; " f"got {session_id!r}"
        )
    return session_id


def _validated_extra_args(extra_args: Optional[Iterable[str]]) -> list[str]:
    extra = list(extra_args or ())
    index = 0
    while index < len(extra):
        arg = extra[index]
        if not isinstance(arg, str):
            raise CodexNativeLaunchError(f"extra_args[{index}] must be a string; got {arg!r}")
        if arg == RESUME_COMMAND or arg in FORBIDDEN_OPTIONS:
            raise CodexNativeLaunchError(
                f"extra_args[{index}]={arg!r} would make the resumed session "
                "depend on a second or indirect session selector"
            )
        arity = _ALLOWED_PROFILE_OPTIONS.get(arg)
        if arity is None:
            raise CodexNativeLaunchError(
                f"extra_args[{index}]={arg!r} is not an admitted Codex profile option; "
                "only the launcher's closed profile argv may precede resume"
            )
        if index + arity >= len(extra):
            raise CodexNativeLaunchError(
                f"extra_args[{index}]={arg!r} requires {arity} value argument(s)"
            )
        for offset in range(1, arity + 1):
            value = extra[index + offset]
            if not isinstance(value, str):
                raise CodexNativeLaunchError(
                    f"extra_args[{index + offset}] must be a string; got {value!r}"
                )
        index += arity + 1
    return extra


def build_resume_argv(
    *,
    session_id: str,
    codex_binary: str = "codex",
    extra_args: Optional[Iterable[str]] = None,
) -> list[str]:
    """Return ``codex [profile args] resume <exact-id>``.

    Codex's global/profile options are deliberately placed before the
    subcommand.  The identity pair remains the final two argv elements, so
    no optional positional prompt can be confused with the session id.
    """
    native_id = validate_session_id(session_id)
    if not isinstance(codex_binary, str) or not codex_binary:
        raise CodexNativeLaunchError("codex_binary must be a non-empty string")
    extra = _validated_extra_args(extra_args)
    provider_contracts.validate_resume_argv(
        provider_contracts.PROVIDER_CODEX, [RESUME_COMMAND, native_id]
    )
    return [codex_binary, *extra, RESUME_COMMAND, native_id]


def resumes_exactly(argv: Sequence[str], session_id: str) -> bool:
    """Whether argv contains exactly one ``resume <session_id>`` selector."""
    try:
        validate_session_id(session_id)
    except CodexNativeLaunchError:
        return False
    values = list(argv)
    if any(
        value in FORBIDDEN_OPTIONS or value in _EARLY_EXIT_OR_DELIMITER_OPTIONS for value in values
    ):
        return False
    positions = [index for index, value in enumerate(values) if value == RESUME_COMMAND]
    if len(positions) != 1:
        return False
    position = positions[0]
    return position == len(values) - 2 and values[position + 1] == session_id
