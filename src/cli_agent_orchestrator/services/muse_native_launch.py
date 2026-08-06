"""The Muse argv forms a managed native session is allowed to use.

Muse Code 0.1.0 binds a caller-provided session id **only** on the headless
``muse exec`` surface (``--session-id <uuid>``); the interactive TUI rejects the
flag and mints its session internally after its first turn. So, like Claude's
native path, Muse's native identity is *chosen* (an input) rather than
discovered (a result): a canonical UUID is minted before any provider I/O and
handed to ``muse exec`` as ``--session-id <uuid>``.

Consequence to name (a documented first-version limitation, not a silent one):
``muse exec`` is a one-shot headless runner, not an interactive multi-turn TUI.
The managed launch therefore carries the assigned task as the exec invocation's
initial prompt (pre-bound in argv) rather than admitting task bytes into an
interactive prompt after launch. The pane shows the exec output, not a usable
provider UI. ``native_tui``-style interactive operation is a follow-up pending
a proven TUI session-mint.

Two argv forms exist and nothing else is accepted:

    launch    muse exec --session-id <uuid> [profile args] <initial prompt>
    recover   muse exec --session-id <uuid> [profile args] <initial prompt>

Every recency-derived form is refused by construction. The identity option
``--session-id`` leads; a caller cannot smuggle a second identity option past
``_validated_extra_args``.
"""

from __future__ import annotations

import re
import uuid as _uuid_module
from typing import Iterable, List, Optional

from cli_agent_orchestrator.services import provider_contracts

#: The flag that *assigns* an identity at launch. Its argument is mandatory
#: (``--session-id <uuid>``), so a missing value is a startup error rather
#: than a silent fallback.
LAUNCH_OPTION = "--session-id"

#: Options that would rebind the identity to a different session or to the
#: most recent one. None of these may appear in a managed native Muse launch.
FORBIDDEN_OPTIONS = frozenset(
    {
        "--resume",
        "-r",
        "--last",
        "-c",
        "--continue",
        "--fork-session",
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
        if arg in FORBIDDEN_OPTIONS or arg == LAUNCH_OPTION:
            raise MuseNativeLaunchError(
                f"{arg} would unbind the session identity and is never permitted "
                "in a managed native Muse launch"
            )
    return extra


def build_launch_argv(
    *,
    session_id: str,
    muse_binary: str = "muse",
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    extra_args: Optional[Iterable[str]] = None,
    initial_prompt: Optional[str] = None,
) -> List[str]:
    """``muse exec --session-id <uuid> [model/effort/extra] [initial prompt]``.

    The identity option leads; profile/model/effort arguments follow; the
    assigned task travels as the exec invocation's initial prompt (see module
    docstring: ``muse exec`` is one-shot, so the task is pre-bound in argv).
    """
    validate_session_id(session_id)
    extra = _validated_extra_args(extra_args)
    argv = [muse_binary, "exec", LAUNCH_OPTION, session_id, "--yolo"]
    if model:
        argv.extend(["--model", model])
    if reasoning_effort:
        argv.extend(["--reasoning-effort", reasoning_effort])
    argv.extend(extra)
    if initial_prompt:
        argv.append(initial_prompt)
    return argv


def binds_exactly(argv: List[str], session_id: str) -> bool:
    """Whether ``argv`` binds exactly this session and no other.

    ``muse exec --session-id <uuid>`` is the only accepted identity form for the
    installed 0.1.0 (the interactive TUI rejects the flag), so the check requires
    exactly one ``--session-id`` occurrence whose value equals the minted id.
    """
    if not argv:
        return False
    for arg in argv[1:]:
        if arg in FORBIDDEN_OPTIONS:
            return False
    occurrences = [index for index, arg in enumerate(argv) if arg == LAUNCH_OPTION]
    if len(occurrences) != 1:
        return False
    index = occurrences[0]
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
