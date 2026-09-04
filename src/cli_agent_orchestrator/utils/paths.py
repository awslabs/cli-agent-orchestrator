"""Filesystem-path helpers shared across services and utils."""

import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ``C:\x\y`` or ``C:/x/y`` — a Windows drive-letter path as pasted from an
# Explorer address bar or "Copy as path".
_WINDOWS_PATH = re.compile(r"^([A-Za-z]):[\\/](.*)$")

# Bounds on a path we are willing to CREATE. Creating is a write primitive, so
# it gets limits an operator typing a real project path will never reach:
# Linux PATH_MAX, and a depth well past any plausible checkout. Without them a
# single request can materialize an arbitrarily deep tree.
WORKING_DIRECTORY_MAX_LEN = 4096
WORKING_DIRECTORY_MAX_DEPTH = 64


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
    create_missing: bool = True,
) -> Optional[str]:
    """Turn an operator-supplied directory into a usable absolute path.

    The web UI runs in the operator's browser; on WSL setups that browser is on
    WINDOWS, so operators naturally paste Windows paths (``C:\\Users\\me\\proj``,
    often wrapped in quotes by Explorer's "Copy as path"). cao-server runs
    inside WSL where those paths only exist under the ``/mnt/<drive>/`` interop
    mount, so every such launch used to fail as an opaque 500 deep in tmux.

    - Strips surrounding quotes and whitespace.
    - Expands ``~``.
    - Translates a Windows drive path (``C:\\x\\y`` or ``C:/x/y``) to the WSL
      interop mount (``/mnt/c/x/y``) when that drive mount exists.
    - Creates the directory when it is missing (``create_missing``): the
      operator is pointing at where they WANT the project to live, and
      bouncing them to a terminal to ``mkdir`` first defeats the purpose of
      a browser front door. Pass ``create_missing=False`` for read-only
      callers that must not touch the filesystem. Creation is bounded by
      ``WORKING_DIRECTORY_MAX_LEN``/``_MAX_DEPTH``; callers should also run
      their cheap validations BEFORE calling with ``create_missing=True``, so
      a request that is going to be rejected anyway leaves nothing behind.

    Returns ``None`` for ``None``/blank input so callers can pass the raw
    optional parameter straight through.

    Raises:
        ValueError: with a human-readable message when the path cannot be
            used (relative, drive not mounted, is a file, creation failed).
    """
    if working_directory is None:
        return None
    cleaned = working_directory.strip().strip("\"'").strip()
    if not cleaned:
        return None

    match = _WINDOWS_PATH.match(cleaned)
    if match:
        drive, rest = match.group(1).lower(), match.group(2).replace("\\", "/")
        drive_mount = mnt_root / drive
        if not drive_mount.is_dir():
            raise ValueError(
                f"{working_directory!r} is a Windows path, but drive {drive.upper()}: "
                f"is not mounted at {drive_mount}. Use the Linux path instead."
            )
        translated = drive_mount / rest
        logger.info("Translated Windows path %r -> %s", working_directory, translated)
        cleaned = str(translated)

    path = Path(cleaned).expanduser()
    # Length is checked BEFORE any filesystem call: an over-long path makes
    # exists()/is_dir() itself raise OSError(ENAMETOOLONG), which would escape
    # as a 500 instead of the clear 400 every other rejection here produces.
    if len(str(path)) > WORKING_DIRECTORY_MAX_LEN:
        raise ValueError(
            f"Working directory path is too long "
            f"({len(str(path))} > {WORKING_DIRECTORY_MAX_LEN} characters)"
        )
    if not path.is_absolute():
        raise ValueError(f"Working directory must be an absolute path, got {working_directory!r}")
    if path.exists() and not path.is_dir():
        # Not just files: a FIFO, socket or device node would otherwise pass
        # here and fail later inside tmux with exactly the opaque error this
        # function exists to prevent.
        raise ValueError(f"{str(path)!r} is not a folder")
    if not path.exists():
        if not create_missing:
            raise ValueError(f"Folder does not exist: {path}")
        if len(path.parts) > WORKING_DIRECTORY_MAX_DEPTH:
            raise ValueError(
                f"Working directory is nested too deeply "
                f"({len(path.parts)} > {WORKING_DIRECTORY_MAX_DEPTH} levels)"
            )
        try:
            # exist_ok: a concurrent request may create the directory between
            # the exists() check above and this mkdir — that is success.
            path.mkdir(parents=True, exist_ok=True)
            logger.info("Created working directory %s", path)
        except OSError as e:
            raise ValueError(f"Folder {str(path)!r} does not exist and could not be created: {e}")
    return str(path)
