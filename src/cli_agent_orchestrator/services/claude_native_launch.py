"""The two Claude argv forms a managed native session is allowed to use.

Claude's native identity is not discovered — it is *chosen*. A canonical
UUID is minted before any provider I/O and handed to the launch as
``--session-id <uuid>``, so the session's identity exists, and is
recorded, before the provider has done anything at all. Recovery then
names that same uuid with ``--resume <uuid>``.

This is the structural difference from the Kimi native path, which asks
its ACP transport to mint a session and reads the id back out of the
reply. There the id is a *result*; here it is an *input*. The
consequence worth naming: a Claude launch that dies before its first
turn still has a recorded identity, because the identity preceded the
launch rather than depending on it succeeding.

Two argv forms exist and nothing else is accepted:

    launch    claude --session-id <uuid> [profile args]
    recover   claude --resume     <uuid> [profile args]

Every recency-derived form is refused by construction — ``--continue``,
``--last``, ``-c``, ``--fork-session`` and the non-persistent modes. They
bind whichever session happens to be newest, which after any other
activity on the machine is a different session, and a resume bound to it
resumes somebody else's context. That refusal lives in
:mod:`provider_contracts` and is applied here so the argv builder cannot
produce a form its own validator would reject.
"""

from __future__ import annotations

import uuid as _uuid_module
from typing import Iterable, List, Optional

from cli_agent_orchestrator.services import provider_contracts

#: The flag that *assigns* an identity at launch. Its argument is
#: mandatory (``--session-id <uuid>``), so a missing value is a startup
#: error rather than a silent fallback.
LAUNCH_OPTION = "--session-id"

#: The flag that names an already-assigned identity. Read the installed
#: build's own help before trusting the shape of this one:
#:
#:     -r, --resume [value]   Resume a conversation by session ID, or
#:                            open interactive picker with optional
#:                            search term
#:
#: The value is **optional**. A resume that loses its id therefore does
#: not fail — it opens an interactive picker and waits, which in a managed
#: pane is a provider sitting at a menu that nobody will ever answer,
#: indistinguishable from a slow start. Every argv built here carries the
#: value, and every check below confirms the value is actually present
#: rather than merely confirming the flag is.
RESUME_OPTION = "--resume"

#: ``-r`` is the same option. Recognised for *detection* so a second
#: identity option cannot slip through spelled differently, while only
#: the long form is ever emitted.
RESUME_OPTION_ALIASES = (RESUME_OPTION, "-r")

#: Options that would defeat the exact-identity binding, whatever else the
#: argv says. Held here as well as in the resume validator because this
#: module builds argv for the *launch* too, and a launch is not validated
#: by the resume contract.
#:
#: ``--from-pr`` joins the recency forms for the same reason they are
#: there: it resolves a session indirectly (by pull request, or by an
#: interactive picker) rather than naming one, so what it binds is not
#: decided by this argv.
FORBIDDEN_OPTIONS = (
    "--continue",
    "--last",
    "-c",
    "--fork-session",
    "--ephemeral",
    "--no-session-persistence",
    "--from-pr",
)


class ClaudeNativeLaunchError(ValueError):
    """An argv was requested that would not bind the exact session."""


def mint_session_id() -> str:
    """A fresh canonical lowercase UUID for a managed Claude session.

    Deliberately trivial, and deliberately here rather than inline at the
    call site: this is the moment a session acquires its identity, and the
    contract that it is a canonical uuid4 belongs next to the argv forms
    that carry it.
    """
    return str(_uuid_module.uuid4())


def validate_session_id(session_id: str) -> str:
    """Return ``session_id`` if it is a canonical lowercase UUID.

    Canonical form is required rather than merely parseable form. Claude
    accepts a uuid in several spellings, but the recorded identity, the
    launch argv, the resume argv and the hook payload all have to compare
    equal as *strings* — an uppercase or brace-wrapped spelling would
    parse to the same uuid and fail every one of those comparisons.
    """
    if not isinstance(session_id, str) or not session_id:
        raise ClaudeNativeLaunchError("claude native session id must be a non-empty string")
    try:
        parsed = _uuid_module.UUID(session_id)
    except ValueError as exc:
        raise ClaudeNativeLaunchError(
            f"claude native session id must be a canonical UUID; got {session_id!r}"
        ) from exc
    if str(parsed) != session_id:
        raise ClaudeNativeLaunchError(
            "claude native session id must be a canonical lowercase UUID; "
            f"got {session_id!r} (canonical form is {str(parsed)!r})"
        )
    return session_id


def _validated_extra_args(extra_args: Optional[Iterable[str]]) -> List[str]:
    extra = list(extra_args or [])
    for arg in extra:
        if arg in FORBIDDEN_OPTIONS:
            raise ClaudeNativeLaunchError(
                f"{arg} would unbind the session identity and is never permitted "
                "in a managed native launch"
            )
        if arg == LAUNCH_OPTION or arg in RESUME_OPTION_ALIASES:
            raise ClaudeNativeLaunchError(
                f"{arg} is supplied by this builder; a second one would make the "
                "effective identity depend on Claude's option precedence"
            )
    return extra


def build_launch_argv(
    *,
    session_id: str,
    claude_binary: str = "claude",
    extra_args: Optional[Iterable[str]] = None,
) -> List[str]:
    """``claude --session-id <uuid> [extra]`` — a new session with a chosen id.

    The identity option leads, and the profile's own arguments follow. A
    caller cannot smuggle a second identity option past
    :func:`_validated_extra_args`, so exactly one identity is ever present
    and it is the one this call was given.
    """
    validate_session_id(session_id)
    extra = _validated_extra_args(extra_args)
    return [claude_binary, LAUNCH_OPTION, session_id, *extra]


def build_resume_argv(
    *,
    session_id: str,
    claude_binary: str = "claude",
    extra_args: Optional[Iterable[str]] = None,
) -> List[str]:
    """``claude --resume <uuid> [extra]`` — reattach to that exact session.

    Validated through :func:`provider_contracts.validate_resume_argv`, so
    the one authority on accepted resume forms is the one that decides
    here too. A builder that agreed with itself but not with the contract
    would be the most convincing possible way to ship a forbidden form.
    """
    validate_session_id(session_id)
    extra = _validated_extra_args(extra_args)
    provider_contracts.validate_resume_argv(
        provider_contracts.PROVIDER_CLAUDE, [RESUME_OPTION, session_id]
    )
    return [claude_binary, RESUME_OPTION, session_id, *extra]


def _binds_exactly(argv: List[str], options: tuple[str, ...], session_id: str) -> bool:
    if not argv:
        return False
    for arg in argv[1:]:
        if arg in FORBIDDEN_OPTIONS:
            return False
    identity_options = (LAUNCH_OPTION, *RESUME_OPTION_ALIASES)
    occurrences = [index for index, arg in enumerate(argv) if arg in identity_options]
    if len(occurrences) != 1:
        # Zero means no identity at all; more than one means the effective
        # identity is decided by Claude's option precedence rather than by
        # this argv, which is not something a caller may assert about.
        return False
    index = occurrences[0]
    if argv[index] not in options:
        return False
    # The value has to be *there*, not merely expected. ``--resume`` with
    # its value missing is a valid invocation that opens a picker, so a
    # check that stopped at "the flag is present" would call an
    # interactive menu a bound session.
    return index + 1 < len(argv) and argv[index + 1] == session_id


def launches_exactly(argv: List[str], session_id: str) -> bool:
    """Whether ``argv`` starts exactly this session and no other."""
    return _binds_exactly(argv, (LAUNCH_OPTION,), session_id)


def resumes_exactly(argv: List[str], session_id: str) -> bool:
    """Whether ``argv`` resumes exactly this session and no other."""
    return _binds_exactly(argv, RESUME_OPTION_ALIASES, session_id)


def binds_exactly(argv: List[str], session_id: str) -> bool:
    """Whether ``argv`` binds this session, by either accepted form.

    What a pane observation needs: having read a live pane's command line,
    the question is whether that process belongs to this session — not
    whether it was started fresh or resumed, which the observer has no way
    to know and no reason to care about.
    """
    return launches_exactly(argv, session_id) or resumes_exactly(argv, session_id)
