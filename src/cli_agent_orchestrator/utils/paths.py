"""Filesystem-path helpers shared across services and utils."""

import logging
import os
import re
from pathlib import Path
from typing import Optional

from cli_agent_orchestrator.utils.path_validation import resolve_and_validate_path

logger = logging.getLogger(__name__)

# ``C:\x\y`` or ``C:/x/y`` — a Windows drive-letter path as pasted from an
# Explorer address bar or "Copy as path".
_WINDOWS_PATH = re.compile(r"^([A-Za-z]):[\\/](.*)$")

# Bounds on a path we are willing to CREATE. Creating is a write primitive, so
# it gets limits an operator typing a real project path will never reach:
# Linux PATH_MAX, the per-component NAME_MAX every mainstream filesystem
# shares, and a depth well past any plausible checkout. Without them a single
# request can materialize an arbitrarily deep tree. The component limit is the
# one that actually trips in practice: the OS enforces it per path segment, so
# a 300-character folder name is refused even though the whole path is short
# (plauzy, #724 review).
WORKING_DIRECTORY_MAX_LEN = 4096
WORKING_DIRECTORY_COMPONENT_MAX_LEN = 255
WORKING_DIRECTORY_MAX_DEPTH = 64


class WorkingDirectoryError(ValueError):
    """An operator-supplied working directory cannot be used.

    A ``ValueError`` so every existing ``except ValueError`` arm keeps treating
    it as a client error, and its own class so ``POST
    /sessions/{name}/terminals`` -- whose generic ``ValueError`` arm means
    "session not found" (404) -- can tell a bad directory (400) apart from a
    missing session.
    """


def normalized_path(path: "str | Path") -> str:
    """Canonical form for comparing configured directory paths (GH #280/#281).

    Disabled agent-profile directories are stored as the exact strings the UI
    sends, but the same directory can be reached via a different spelling
    (``~``, trailing slash, a symlink; e.g. the local agent-store is also a
    provider default). ``realpath`` + ``expanduser`` canonicalizes all of
    those, so the disable check matches whenever two spellings reach the same
    physical directory.

    Lives here (rather than in ``utils.agent_profiles`` or
    ``services.settings_service``) so both can import it without reaching into
    each other's private API.
    """
    return os.path.realpath(os.path.expanduser(str(path)))


def normalize_working_directory(
    working_directory: Optional[str],
    mnt_root: Path = Path("/mnt"),
) -> Optional[str]:
    """Turn an operator-typed directory spelling into a lexically clean absolute path.

    The web UI runs in the operator's browser; on WSL setups that browser is on
    WINDOWS, so operators naturally paste Windows paths (``C:\\Users\\me\\proj``,
    often wrapped in quotes by Explorer's "Copy as path"). cao-server runs
    inside WSL where those paths only exist under the ``/mnt/<drive>/`` interop
    mount, so every such launch used to fail as an opaque 500 deep in tmux.

    PURE: this function never stats, creates or otherwise touches the target
    (the one filesystem probe is whether the drive mount itself exists). It
    does exactly four things:

    - Strips surrounding quotes and whitespace.
    - Expands ``~``.
    - Translates a Windows drive path (``C:\\x\\y`` or ``C:/x/y``) to the WSL
      interop mount (``/mnt/c/x/y``) when that drive mount exists, and refuses
      a translation that would land outside that mount.
    - Normalizes lexically (``a/../b`` -> ``b``) and requires an absolute
      result, bounded by the length caps above.

    Whether the result is ALLOWED (the repository's blocked-system-directory
    policy) and whether it EXISTS are separate questions, answered by
    :func:`validate_working_directory` and :func:`ensure_working_directory`
    respectively, so a caller can reject a request before anything is created
    and create only once the request is known to succeed.

    Returns ``None`` for ``None``/blank input so callers can pass the raw
    optional parameter straight through.

    Raises:
        WorkingDirectoryError: with a human-readable message when the spelling
            cannot be used (relative, drive not mounted, escapes its mount,
            unknown ``~user``, over-long).
    """
    if working_directory is None:
        return None
    cleaned = working_directory.strip().strip("\"'").strip()
    if not cleaned:
        return None

    drive_mount: Optional[Path] = None
    drive = ""
    match = _WINDOWS_PATH.match(cleaned)
    if match:
        drive, rest = match.group(1).lower(), match.group(2).replace("\\", "/")
        drive_mount = Path(os.path.normpath(mnt_root / drive))
        if not drive_mount.is_dir():
            raise WorkingDirectoryError(
                f"{working_directory!r} is a Windows path, but drive {drive.upper()}: "
                f"is not mounted at {drive_mount}. Use the Linux path instead."
            )
        # ``C:\\Users\me`` -- an escaped paste from JSON or Python, common --
        # leaves ``rest`` starting with a separator, and ``drive_mount /
        # "/Users/me"`` is ``/Users/me``: the drive would be dropped entirely.
        # Strip leading separators so the drive always anchors the result.
        translated = drive_mount / rest.lstrip("\\/")
        logger.info("Translated Windows path %r -> %s", working_directory, translated)
        cleaned = str(translated)

    try:
        path = Path(cleaned).expanduser()
    except RuntimeError as e:
        # ``~nobody/proj`` for a user that does not exist: no home to expand
        # into. Same contract as every other unusable spelling, a clear 400.
        raise WorkingDirectoryError(f"Cannot expand {cleaned!r}: {e}")
    # Lexical normalization: ``a/../b`` cancels ``a`` on paper, but a later
    # ``mkdir(parents=True)`` walks the literal components and would
    # materialize ``a`` on its way to ``b``. Deliberately not ``resolve()``:
    # symlinks are the policy check's canonicalization concern, and following
    # them needs the path to exist.
    path = Path(os.path.normpath(path))
    if len(str(path)) > WORKING_DIRECTORY_MAX_LEN:
        raise WorkingDirectoryError(
            f"Working directory path is too long "
            f"({len(str(path))} > {WORKING_DIRECTORY_MAX_LEN} characters)"
        )
    for component in path.parts:
        if len(component.encode("utf-8", "surrogateescape")) > WORKING_DIRECTORY_COMPONENT_MAX_LEN:
            raise WorkingDirectoryError(
                f"Working directory has a folder name longer than "
                f"{WORKING_DIRECTORY_COMPONENT_MAX_LEN} bytes: {component[:40]!r}..."
            )
    if not path.is_absolute():
        raise WorkingDirectoryError(
            f"Working directory must be an absolute path, got {working_directory!r}"
        )
    if drive_mount is not None and os.path.commonpath([str(drive_mount), str(path)]) != str(
        drive_mount
    ):
        # ``C:\..\..\Windows`` normalizes to a path OUTSIDE the mount just
        # validated. Judging the drive root before ``normpath`` let it walk out.
        raise WorkingDirectoryError(
            f"{working_directory!r} leaves drive {drive.upper()}: "
            f"({path} is not under {drive_mount})"
        )
    return str(path)


def validate_working_directory(working_directory: str) -> str:
    """Apply the repository's path policy to a normalized working directory, side-effect free.

    The same ``resolve_and_validate_path`` the MCP tools and the tmux backend
    use (#345, design D5), with ``allow_create=True`` so a target that does not
    exist yet is judged by its nearest EXISTING ancestor: ``/etc/new-dir`` and
    ``/tmp/new-dir`` are refused, ``~/projects/new-dir`` passes. Nothing is
    created here; call :func:`ensure_working_directory` for that once the
    request is known to succeed.

    Returns:
        The canonical (``realpath``) form of the directory.

    Raises:
        WorkingDirectoryError: relative, blocked, beneath a blocked subtree,
            an existing non-directory, or no existing ancestor at all.
    """
    # Checked first so the message says what is wrong. Not just files: a FIFO,
    # socket or device node would otherwise fail later inside tmux with
    # exactly the opaque error the normalization exists to prevent. The
    # ``os.path`` predicates swallow OSError (ENAMETOOLONG, EACCES on a parent)
    # and answer False, which is fine here: a target the OS cannot even stat is
    # refused by the mkdir in ensure_working_directory instead.
    expanded = os.path.expanduser(working_directory)
    if os.path.exists(expanded) and not os.path.isdir(expanded):
        raise WorkingDirectoryError(f"{expanded!r} is not a folder")
    try:
        return resolve_and_validate_path(
            working_directory,
            allow_create=True,
            allow_file=False,
            description="Working directory",
        )
    except ValueError as e:
        raise WorkingDirectoryError(str(e)) from e
    except OSError as e:  # defensive: the policy helper swallows these today
        raise WorkingDirectoryError(f"Working directory {working_directory!r} cannot be used: {e}")


def ensure_working_directory(working_directory: str) -> str:
    """Validate a working directory and create it if it is missing.

    The write half of the pair: ``POST /sessions`` and ``POST
    /sessions/{name}/terminals`` let an operator point at where a project
    SHOULD live, so bouncing them to a terminal to ``mkdir`` first defeats the
    purpose of a browser front door. It runs at the point of use -- inside
    ``terminal_service``, after every check that can reject the request (name
    validation, session not found, duplicate session, terminal cap) -- so a
    rejected request leaves nothing behind, and it re-applies the policy check
    so the directory it creates is one the policy allows.

    Creation is bounded by ``WORKING_DIRECTORY_MAX_DEPTH``. Any ``OSError``
    from the stat or the mkdir (a component over NAME_MAX, a parent that is a
    file or not traversable, a read-only filesystem) is reported as a
    :class:`WorkingDirectoryError`, i.e. a 400 with a plain detail, never a 500.

    Returns:
        The canonical form of the directory, which now exists.
    """
    real = validate_working_directory(working_directory)
    path = Path(real)
    try:
        exists = path.is_dir()
    except OSError as e:
        raise WorkingDirectoryError(f"Working directory {real!r} cannot be used: {e}") from e
    if exists:
        return real
    if len(path.parts) > WORKING_DIRECTORY_MAX_DEPTH:
        raise WorkingDirectoryError(
            f"Working directory is nested too deeply "
            f"({len(path.parts)} > {WORKING_DIRECTORY_MAX_DEPTH} levels)"
        )
    try:
        # exist_ok: a concurrent request may create the directory between
        # the is_dir() check above and this mkdir -- that is success.
        path.mkdir(parents=True, exist_ok=True)
        logger.info("Created working directory %s", path)
    except OSError as e:
        raise WorkingDirectoryError(
            f"Folder {real!r} does not exist and could not be created: {e}"
        ) from e
    return real
