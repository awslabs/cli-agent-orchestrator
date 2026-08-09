"""The Muse argv forms a managed native session is allowed to use.

Muse Code 0.1.0's accepted interactive lifecycle resumes a caller-provided
session id through the TUI: ``muse resume <id>`` starts the interactive,
multi-turn interface bound to exactly that id (verified on the installed
0.1.0-R708.1 build: caller-chosen id, exact fresh-pane TUI resume, context
carry, pane-kill/process exit, and explicit model/effort changes).  Like
Claude's native path, Muse's native identity is *chosen* (an input) rather
than discovered (a result): a canonical UUID is minted before any provider
I/O and handed to ``muse resume <id>``.

This supersedes the earlier one-shot ``muse exec --session-id <uuid>``
contract.  ``muse exec`` pre-bound the assigned task as the invocation's
initial prompt and produced no usable interactive provider UI; the accepted
interactive lifecycle resumes the id and admits task bytes into the running
TUI exactly like the other native harnesses.

Two argv forms exist and nothing else is accepted:

    launch    muse resume <id> [profile args]
    recover   muse resume <id> [profile args]

Every recency-derived form is refused by construction.  The identity
subcommand ``resume`` leads and its argument is the exact id; a caller
cannot smuggle a second identity option past ``_validated_extra_args``.
"""

from __future__ import annotations

import re
import uuid as _uuid_module
from typing import Iterable, List, Optional, Sequence

from cli_agent_orchestrator.services import provider_contracts

#: The subcommand that resumes an exact identity at launch.  Its argument
#: is mandatory (``muse resume <id>``), so a missing value is a startup
#: error rather than a silent fallback.
RESUME_COMMAND = "resume"

#: Options that would rebind the identity to a different session or to the
#: most recent one, or would disable the retained session log required by
#: ``muse resume``.  None may appear in a managed native Muse launch.
FORBIDDEN_OPTIONS = frozenset(
    {
        "--session-id",
        "-s",
        "--exec",
        "--last",
        "-c",
        "--continue",
        "--fork-session",
        "--no-session-log",
    }
)


class MuseNativeLaunchError(ValueError):
    """A Muse native launch contract was violated."""


class MuseNativeModelError(MuseNativeLaunchError):
    """A requested model cannot be pinned, or the running one is not it."""


def mint_session_id() -> str:
    """A fresh canonical lowercase UUID for a managed Muse session.

    Deliberately trivial, and deliberately here rather than inline at the call
    site: this is the moment a session acquires its identity, and the contract
    that it is a canonical uuid4 belongs next to the argv forms that carry it.
    """
    return str(_uuid_module.uuid4())


def validate_session_id(session_id: str) -> str:
    """Return ``session_id`` if it is a canonical lowercase UUID.

    The recorded identity, the launch argv, and the recovery argv must compare
    equal as strings; an uppercase or brace-wrapped spelling would parse to the
    same uuid and fail every one of those comparisons.
    """
    if not isinstance(session_id, str) or not session_id:
        raise MuseNativeLaunchError("muse native session id must be a non-empty string")
    try:
        parsed = _uuid_module.UUID(session_id)
    except ValueError as exc:
        raise MuseNativeLaunchError(
            f"muse native session id must be a canonical UUID; got {session_id!r}"
        ) from exc
    if str(parsed) != session_id:
        raise MuseNativeLaunchError(
            "muse native session id must be a canonical lowercase UUID; "
            f"got {session_id!r} (canonical form is {str(parsed)!r})"
        )
    return session_id


def _validated_extra_args(extra_args: Optional[Iterable[str]]) -> List[str]:
    extra = list(extra_args or [])
    for arg in extra:
        if arg in FORBIDDEN_OPTIONS or arg == RESUME_COMMAND:
            raise MuseNativeLaunchError(
                f"{arg} would violate exact session resumability and is never "
                "permitted in a managed native Muse launch"
            )
    return extra


def build_resume_argv(
    *,
    session_id: str,
    muse_binary: str = "muse",
    extra_args: Optional[Iterable[str]] = None,
) -> List[str]:
    """``muse resume <id> [profile args]``.

    The identity subcommand leads and its argument is the exact id, so no
    optional positional prompt can be confused with the session id.  Muse's
    global/profile options are placed after the identity pair.
    """
    native_id = validate_session_id(session_id)
    if not isinstance(muse_binary, str) or not muse_binary:
        raise MuseNativeLaunchError("muse_binary must be a non-empty string")
    extra = _validated_extra_args(extra_args)
    provider_contracts.validate_resume_argv(
        provider_contracts.PROVIDER_MUSE, [RESUME_COMMAND, native_id]
    )
    return [muse_binary, RESUME_COMMAND, native_id, *extra]


def resumes_exactly(argv: Sequence[str], session_id: str) -> bool:
    """Whether ``argv`` binds exactly this session and no other.

    ``muse resume <id>`` is the only accepted identity form; the check
    requires exactly one ``resume`` occurrence whose following argument
    equals the minted id and forbids every recency/identity-rebinding form.
    """
    if not argv:
        return False
    values = list(argv)
    for value in values[1:]:
        if value in FORBIDDEN_OPTIONS:
            return False
    positions = [index for index, value in enumerate(values) if value == RESUME_COMMAND]
    if len(positions) != 1:
        return False
    index = positions[0]
    return index + 1 < len(argv) and argv[index + 1] == session_id


def observed_model_matches(requested: str, observed: Optional[str]) -> bool:
    """Whether the observed process model is the requested one."""
    return bool(requested) and requested == (observed or "")


def validate_requested_model(model: Optional[str]) -> str:
    """A managed native Muse launch must pin a model."""
    if not isinstance(model, str) or not model:
        raise MuseNativeModelError("muse native launch requires a model id")
    return model


#: Provider wire name this module serves, for the launch-surface dispatcher.
PROVIDER_WIRE = provider_contracts.PROVIDER_MUSE_CLI
