"""The Muse argv forms a managed native session is allowed to use.

Muse Code 0.1.0's *fresh* interactive lifecycle is a no-prompt TUI:
``muse [route/profile args]`` starts the interactive, multi-turn interface
and the provider itself generates the session id (verified on the installed
0.1.0-R708.1 build: the coordinator's real Meta canary ran
``muse --trust-workspace --yolo --reasoning-effort high --model <id>`` with
no prompt, reached the composer at zero turns, and ``/status`` reported the
provider-generated session ``ebab9822-...``).  The managed launch therefore
*discovers* the id from the provider's own ``/status`` panel — it is never
minted, chosen, or handed in before the provider runs.

``muse resume <id>`` is the separate *restoration* form: it re-opens an
exact previously-generated session (the canary proved ``muse resume
<id>`` with the same cwd/profile/route restores the same id and first
turn).  It is not a creation form — a caller-chosen UUID deterministically
exits with "retained session not found: ... has no saved log".

Three argv forms exist and nothing else is accepted:

    launch     muse [route/profile args]        (fresh TUI, no identity)
    restore    muse resume <id> [route args]    (re-open a preserved id)
    recover    muse resume <id> [route args]    (same restoration form)

Every recency-derived form is refused by construction.  The fresh form must
carry no identity subcommand, no ``--session-id``, no positional prompt, and
no task bytes; the resume form's ``resume`` leads and its argument is the
exact preserved id, so a caller cannot smuggle a second identity option
past ``_validated_extra_args``.
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

#: The installed surface that carries the CAO profile system prompt into
#: the main session as base instructions.  ``muse --agents <JSON>`` was
#: read on the installed 0.1.0-R708.1 build and does NOT compose into the
#: main session agent: the overlay registers session agent definitions for
#: the workflow/subagent ``agentType`` path, and an echo-provider probe
#: with the overlay set ran a clean turn with the profile line unchanged.
#: The env-addressed file below *does* compose: with it set, an
#: echo-provider launch and an exact ``muse resume <id>`` both refuse with
#: "provider does not support base instructions" — the same
#: run-configuration refusal a built-in preset with base instructions
#: (``--preset miniswe``) produces, which is deterministic proof the file's
#: exact bytes become the session's base instructions on the meta provider.
#:
#: The variable is an internal build surface (``TBH_EVAL_*``), not a
#: documented end-user option, so this enrollment is pinned to the exact
#: proven build (``SUPPORTED_VERSIONS["muse"] == ("0.1.0",)``) and fails
#: closed if a future build stops honoring it — an unproven build never
#: inherits the capability.  The file is generation-private and
#: content-addressed, so the same stable material and digest feed the
#: launch and any later exact ``muse resume <id>`` of the same pane.
PROFILE_SYSTEM_PROMPT_ENV = "TBH_EVAL_APPEND_SYSTEM_PROMPT_FILE"

#: The generation-private filename the profile system prompt is written
#: to, under the managed-v2 companion dir for the terminal/generation.
PROFILE_SYSTEM_PROMPT_FILENAME = "muse-profile-system-prompt.txt"


class MuseNativeLaunchError(ValueError):
    """A Muse native launch contract was violated."""


class MuseNativeModelError(MuseNativeLaunchError):
    """A requested model cannot be pinned, or the running one is not it."""


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


#: Flags that take a value, so the token that follows them is their value,
#: never a positional prompt.  A fresh launch may only carry route/profile
#: flags; anything else that does not begin with ``-`` is a prompt and is
#: refused.
_VALUE_FLAGS = frozenset(
    {
        "--agents",
        "--provider",
        "--preset",
        "--model",
        "--reasoning-effort",
        "--base-url",
        "--image",
        "--workspace",
        "--worktree",
        "--worktree-base",
        "--worktree-existing",
        "--approval-mode",
        "--approval-judge",
        "--sandbox-network",
        "--echo-delay-ms",
    }
)


def build_fresh_launch_argv(
    *,
    muse_binary: str = "muse",
    extra_args: Optional[Iterable[str]] = None,
) -> List[str]:
    """``muse [route/profile args]`` — a fresh launch with no identity.

    The fresh interactive lifecycle never names a session: the provider
    generates the id itself and the managed launch discovers it from
    ``/status``.  No ``resume``, ``--session-id``, recency form, or
    positional prompt may appear; a caller-chosen id would deterministically
    exit with "retained session not found".
    """
    if not isinstance(muse_binary, str) or not muse_binary:
        raise MuseNativeLaunchError("muse_binary must be a non-empty string")
    extra = list(extra_args or [])
    for index, arg in enumerate(extra):
        if arg in FORBIDDEN_OPTIONS or arg == RESUME_COMMAND:
            raise MuseNativeLaunchError(
                f"{arg} is an identity/recency form and never belongs in a fresh "
                "managed Muse launch; the provider generates the session id"
            )
        if not arg.startswith("-") and not (index > 0 and extra[index - 1] in _VALUE_FLAGS):
            raise MuseNativeLaunchError(
                f"{arg!r} would be a positional prompt, which a fresh Muse launch "
                "must never carry; task bytes are admitted after bind, never at launch"
            )
    return [muse_binary, *extra]


def fresh_launch_has_no_identity(argv: Sequence[str]) -> bool:
    """Whether ``argv`` is a fresh no-identity launch (nothing to restore).

    The fresh form must carry no ``resume`` subcommand, no identity/recency
    option, and no positional prompt.  Used both to build the fresh argv and
    to prove the launched pane still runs the fresh TUI (the pane's observed
    argv after creation must still be identity-free).
    """
    if not argv:
        return False
    values = list(argv)
    for value in values[1:]:
        if value in FORBIDDEN_OPTIONS or value == RESUME_COMMAND:
            return False
    for index, value in enumerate(values[1:], start=1):
        if value.startswith("-"):
            continue
        if index > 1 and values[index - 1] in _VALUE_FLAGS:
            continue
        return False
    return True


def build_resume_argv(
    *,
    session_id: str,
    muse_binary: str = "muse",
    extra_args: Optional[Iterable[str]] = None,
) -> List[str]:
    """``muse resume <id> [profile args]`` — the restoration form only.

    This re-opens an exact *preserved* provider-generated id (later
    reincarnation); it is never used for a fresh launch, which carries no
    identity.  The identity subcommand leads and its argument is the exact
    id, so no optional positional prompt can be confused with the session
    id.  Muse's global/profile options are placed after the identity pair.
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


def validate_profile_system_prompt(system_prompt: Optional[str]) -> str:
    """The CAO profile system prompt that must be composed into the session.

    Profile fidelity is not optional: the enrollment carries the exact
    composed profile text through :data:`PROFILE_SYSTEM_PROMPT_ENV`, and a
    profile with no system prompt would launch a worker whose role
    material exists only in the task prompt.  That is refused here, before
    the session id is minted, so nothing starts for a profile that cannot
    be truthfully applied.
    """
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        raise MuseNativeLaunchError(
            "the CAO agent profile carries no system prompt; the Muse enrollment "
            "composes the profile through the installed "
            f"{PROFILE_SYSTEM_PROMPT_ENV} surface, and a profile with no material to "
            "compose cannot be truthfully applied to the session"
        )
    return system_prompt


#: Provider wire name this module serves, for the launch-surface dispatcher.
PROVIDER_WIRE = provider_contracts.PROVIDER_MUSE_CLI
