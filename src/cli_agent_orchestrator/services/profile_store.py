"""Local agent-store persistence for CAO agent profiles.

Single owner of the ``LOCAL_AGENT_STORE_DIR`` boundary: resolving a profile
*name* to a path inside the store, writing a profile atomically, and deleting
one. Every public function takes a name, never a caller-supplied path.

Why names and not paths: the store is a fixed root joined with a single
validated segment, which is safe to centralise. Filesystem handling of
caller-supplied *paths* deliberately stays in the CLI (see
``cli/commands/install.py::_copy_local_profile_to_store``) so that
``Path(user_input)`` never reaches the HTTP-reachable service layer -- the
property that closed the recurring ``py/path-injection`` alerts. Callers keep
their own input validation; this module validates the segment it is handed.

Ref: https://github.com/awslabs/cli-agent-orchestrator/issues/510
"""

import re
from pathlib import Path

from cli_agent_orchestrator.constants import LOCAL_AGENT_STORE_DIR
from cli_agent_orchestrator.utils.atomic_file import locked_atomic_delete, locked_atomic_write

# A profile name becomes a single filesystem segment under
# LOCAL_AGENT_STORE_DIR. Restricting to [A-Za-z0-9_-] with a 64-char cap
# blocks traversal ("../etc/passwd"), separators, and absolute paths at the
# boundary. CodeQL also recognises this regex as a path-injection sanitiser.
#
# The fullmatch below is repeated INLINE in every function that joins a name
# into a filesystem path, rather than factored into a shared helper, because
# CodeQL's py/path-injection dataflow only recognises a barrier that guards --
# in the same function as the sink -- the very variable that reaches it. The
# repetition is load-bearing, not an oversight.
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

__all__ = [
    "InvalidProfileNameError",
    "ProfileExistsError",
    "ProfileNotFoundError",
    "delete_profile",
    "replace_profile",
    "store_path",
    "write_profile",
]


class InvalidProfileNameError(ValueError):
    """Raised when a profile name is not a safe single path segment."""


class ProfileNotFoundError(FileNotFoundError):
    """Raised when a profile is not present in the local store."""


class ProfileExistsError(FileExistsError):
    """Raised when a write would clobber an existing profile."""


def store_path(name: str) -> Path:
    """Resolve ``name`` to its path inside the local agent store.

    Does not require the profile to exist: this answers "where would this
    live", not "is it there".

    Args:
        name: Profile name, used as the filename stem.

    Returns:
        The resolved absolute path of ``<store>/<name>.md``.

    Raises:
        InvalidProfileNameError: If ``name`` is not a safe single segment.
    """
    # Inline guard: see _PROFILE_NAME_RE. Must stay in the same function as the
    # path join below for CodeQL to treat it as a sanitiser.
    if not _PROFILE_NAME_RE.fullmatch(name):
        raise InvalidProfileNameError(f"Profile name '{name}' must match [A-Za-z0-9_-]{{1,64}}.")
    root = LOCAL_AGENT_STORE_DIR.resolve()
    candidate = (LOCAL_AGENT_STORE_DIR / f"{name}.md").resolve()
    # Defence in depth: the regex already rules out separators, but a symlinked
    # store root could still resolve outside itself.
    if not candidate.is_relative_to(root):
        raise InvalidProfileNameError(f"Profile name '{name}' escapes the local store.")
    return candidate


def write_profile(name: str, content: str, *, overwrite: bool = False) -> Path:
    """Write ``content`` as the profile ``name`` in the local store.

    The write is atomic and inter-process safe: ``locked_atomic_write`` holds
    an exclusive lock across the whole replace, so a concurrent ``cao profile``
    write and an HTTP write cannot interleave or leave a partial file behind.

    This module deliberately exposes no standalone ``profile_exists`` helper.
    An existence check is only meaningful to a would-be writer if it happens
    in the same critical section as the write, so ``overwrite=False`` is the
    only supported way to ask "create, but do not clobber". A caller that
    instead did ``if exists(name): reject`` before calling here would put the
    check outside the lock, letting two concurrent creators both observe an
    absent file and the second silently overwrite the first. Callers wanting
    409-style semantics should pass ``overwrite=False`` and catch
    :class:`ProfileExistsError`.

    Args:
        name: Profile name, used as the filename stem.
        content: Full profile text (frontmatter + body).
        overwrite: Permit replacing an existing profile. Defaults to False so a
            caller must opt in to clobbering. Enforced under the same lock as
            the write, so two concurrent creators cannot both succeed.

    Returns:
        The path written.

    Raises:
        InvalidProfileNameError: If ``name`` is not a safe single segment.
        ProfileExistsError: If the profile exists and ``overwrite`` is False.
    """
    # Inline guard: see _PROFILE_NAME_RE.
    if not _PROFILE_NAME_RE.fullmatch(name):
        raise InvalidProfileNameError(f"Profile name '{name}' must match [A-Za-z0-9_-]{{1,64}}.")
    root = LOCAL_AGENT_STORE_DIR.resolve()
    target = (LOCAL_AGENT_STORE_DIR / f"{name}.md").resolve()
    if not target.is_relative_to(root):
        raise InvalidProfileNameError(f"Profile name '{name}' escapes the local store.")

    # locked_atomic_write, not locked_atomic_rewrite: a profile write is a full
    # replace, so there is nothing to read. The RMW helper would decode the
    # existing file first, which means a corrupt or non-UTF-8 file in the store
    # could not be repaired by the very install that would have replaced it.
    #
    # The existence check is delegated rather than done here on purpose: it has
    # to happen inside the lock, or two concurrent creators both see an absent
    # file and the second silently clobbers the first.
    try:
        locked_atomic_write(target, content, overwrite=overwrite)
    except FileExistsError as exc:
        raise ProfileExistsError(
            f"Profile '{name}' already exists in the local store. "
            f"Pass overwrite=True to replace it."
        ) from exc
    return target


def replace_profile(name: str, content: str) -> Path:
    """Replace an existing local-store profile. Never creates one.

    The update-only counterpart to :func:`write_profile`. ``write_profile`` with
    ``overwrite=True`` is an upsert, which is wrong for an HTTP ``PUT``: a request
    naming a built-in or provider-managed profile would not fail, it would create a
    *new* local file that shadows the original, silently changing which profile
    wins on load. That is exactly the shadowing the ``duplicated_in`` field exists
    to surface, so an upsert would manufacture the condition we warn about.

    Because this module resolves only inside ``LOCAL_AGENT_STORE_DIR``, a built-in's
    name is simply not present here, so requiring the target to exist rejects
    writes against built-ins at the service boundary rather than by a check in the
    caller.

    The existence requirement is enforced *inside* the write lock. A caller testing
    for the file first would leave a window where a concurrent delete turns the
    intended update back into a create.

    Args:
        name: Profile name, used as the filename stem.
        content: Full profile text (frontmatter + body).

    Returns:
        The path written.

    Raises:
        InvalidProfileNameError: If ``name`` is not a safe single segment.
        ProfileNotFoundError: If the profile is not in the local store.
    """
    # Inline guard: see _PROFILE_NAME_RE.
    if not _PROFILE_NAME_RE.fullmatch(name):
        raise InvalidProfileNameError(f"Profile name '{name}' must match [A-Za-z0-9_-]{{1,64}}.")
    root = LOCAL_AGENT_STORE_DIR.resolve()
    target = (LOCAL_AGENT_STORE_DIR / f"{name}.md").resolve()
    if not target.is_relative_to(root):
        raise InvalidProfileNameError(f"Profile name '{name}' escapes the local store.")

    try:
        locked_atomic_write(target, content, overwrite=True, must_exist=True)
    except FileNotFoundError as exc:
        raise ProfileNotFoundError(
            f"Profile '{name}' is not in the local store, so there is nothing to "
            f"replace. Built-in and provider-managed profiles are not writable; "
            f"copy one into the local store first."
        ) from exc
    return target


def delete_profile(name: str) -> None:
    """Delete the profile ``name`` from the local store.

    The existence check and the unlink both happen inside the target's write
    lock, via :func:`locked_atomic_delete`. That lock is shared with
    :func:`replace_profile`, and it has to be: an unlocked delete can slip
    between an update's ``must_exist`` check and its publish, so the update
    recreates the file and both operations report success. Deletion being the
    unlocked side of that pair would silently void the update-only guarantee
    ``replace_profile`` advertises.

    Args:
        name: Profile name, used as the filename stem.

    Raises:
        InvalidProfileNameError: If ``name`` is not a safe single segment.
        ProfileNotFoundError: If the profile is not in the local store.
    """
    # Inline guard: see _PROFILE_NAME_RE.
    if not _PROFILE_NAME_RE.fullmatch(name):
        raise InvalidProfileNameError(f"Profile name '{name}' must match [A-Za-z0-9_-]{{1,64}}.")
    root = LOCAL_AGENT_STORE_DIR.resolve()
    target = (LOCAL_AGENT_STORE_DIR / f"{name}.md").resolve()
    if not target.is_relative_to(root):
        raise InvalidProfileNameError(f"Profile name '{name}' escapes the local store.")

    try:
        locked_atomic_delete(target)
    except FileNotFoundError as exc:
        raise ProfileNotFoundError(f"Profile '{name}' not found in the local store.") from exc
