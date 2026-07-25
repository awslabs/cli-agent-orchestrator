"""The exact pinned argv for resuming a Kimi session into a native TUI.

The installed Kimi Code resume option (0.29.0 and 0.29.1, verified
byte-identical) is ``-S, --session [id]`` with an **optional** argument:
given an id it resumes that session, and given *no* id it opens an
interactive picker instead.

That optionality is the hazard this module exists to remove.  A resume
that loses its session id does not fail — it launches a picker, and
whatever a human (or an errant keystroke) then selects becomes the
attached session.  CAO would hold a durable attachment record naming one
session while the pane runs a different one, and every receipt afterwards
would be confidently wrong.

So the id is validated before it is ever placed on a command line: a
missing, empty, or flag-shaped id raises rather than degrading to a bare
``--session``.  The result is argv, never a shell string, so the id
cannot be word-split or globbed on its way to the process either.
"""

from __future__ import annotations

import re
from typing import Optional, Sequence

#: The pinned resume option.  The long form is used deliberately: ``-S``
#: is a single letter away from other short flags, and a typo there fails
#: as an unknown option rather than as a wrong session.
RESUME_OPTION = "--session"

#: Provider session ids as minted by Kimi (``session_<hex>``) plus the
#: general shape of an opaque provider id.  Deliberately narrow: this
#: value is about to select which conversation a worker resumes, and a
#: permissive pattern here is how a flag-shaped or empty id reaches the
#: interactive picker.
_SESSION_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")


class KimiNativeLaunchError(ValueError):
    """The native launch argv could not be built safely."""

    code = "kimi-native-launch-error"


def validate_session_id(session_id: object) -> str:
    """Return ``session_id`` if it can never be mistaken for "no id".

    Rejects non-strings, the empty string, anything leading with ``-``
    (which the option parser would read as the next flag, leaving
    ``--session`` argument-less), and anything carrying whitespace or
    shell metacharacters.
    """
    if not isinstance(session_id, str) or not session_id:
        raise KimiNativeLaunchError(
            f"resume requires a non-empty session id; got {session_id!r} — "
            f"a missing id would make {RESUME_OPTION} open an interactive picker"
        )
    if not _SESSION_ID_PATTERN.match(session_id):
        raise KimiNativeLaunchError(
            f"session id {session_id!r} is not a plain provider id; a flag-shaped or "
            f"whitespace-bearing id can leave {RESUME_OPTION} without an argument"
        )
    return session_id


def build_resume_argv(
    *,
    session_id: str,
    kimi_binary: str = "kimi",
    extra_args: Optional[Sequence[str]] = None,
) -> list[str]:
    """Build the exact ``kimi --session <id>`` argv for a native resume.

    ``extra_args`` are placed **before** the resume option so nothing can
    be inserted between ``--session`` and its id.  Any extra argument
    that is itself a resume option is refused: two resume options on one
    command line make which session gets attached depend on the parser's
    precedence rather than on this module.

    Returned as argv for ``create_window_with_argv``, which makes the TUI
    the pane's own primary process rather than a line typed into a shell.
    """
    binary = kimi_binary if isinstance(kimi_binary, str) and kimi_binary else None
    if binary is None:
        raise KimiNativeLaunchError(f"kimi_binary must be a non-empty string; got {kimi_binary!r}")
    resume_id = validate_session_id(session_id)

    leading: list[str] = []
    for index, arg in enumerate(extra_args or ()):
        if not isinstance(arg, str):
            raise KimiNativeLaunchError(f"extra_args[{index}] must be a string; got {arg!r}")
        if arg in {RESUME_OPTION, "-S"} or arg.startswith(f"{RESUME_OPTION}="):
            raise KimiNativeLaunchError(
                f"extra_args[{index}]={arg!r} is a second resume option; the resumed session "
                "must be decided here, not by option precedence"
            )
        leading.append(arg)

    return [binary, *leading, RESUME_OPTION, resume_id]


def resumes_exactly(argv: Sequence[str], session_id: str) -> bool:
    """True when ``argv`` resumes exactly ``session_id`` and nothing else.

    Used to check an argv immediately before launch and against a durable
    attachment record afterwards, so the session CAO recorded and the
    session the pane actually runs are verified to be the same one.
    """
    resume_positions = [index for index, arg in enumerate(argv) if arg in {RESUME_OPTION, "-S"}]
    if len(resume_positions) != 1:
        return False
    position = resume_positions[0]
    if position + 1 >= len(argv):
        return False
    return argv[position + 1] == session_id
