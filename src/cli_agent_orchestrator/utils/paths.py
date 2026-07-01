"""Working-directory normalization for operator-supplied paths.

The dashboard runs in the user's browser — on WSL setups that browser is on
WINDOWS, so users naturally paste Windows paths (``C:\\Users\\me\\project``,
often quoted by Explorer's "Copy as path"). cao-server runs inside WSL where
those paths only exist under the ``/mnt/<drive>/`` interop mount. Observed
live: every run-wizard launch with a Windows path failed as an opaque 500.
"""

import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_WINDOWS_PATH = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def normalize_working_directory(
    working_directory: Optional[str],
    mnt_root: Path = Path("/mnt"),
    create_missing: bool = True,
) -> Optional[str]:
    """Turn an operator-supplied directory into a usable absolute path.

    - Strips quotes (Windows Explorer's "Copy as path" wraps in ``"``).
    - Expands ``~``.
    - Translates Windows paths (``C:\\x\\y`` or ``C:/x/y``) to the WSL interop
      mount (``/mnt/c/x/y``) when that drive mount exists.
    - Auto-creates the directory when missing (the run wizard points at where
      the user WANTS the project; erroring on a not-yet-existing folder would
      just bounce them to a terminal — the thing the UI exists to avoid).

    Raises:
        ValueError: With a human-readable message when the path cannot be
            used (drive not mounted, creation failed, path is a file).
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
        logger.info(f"Translated Windows path {working_directory!r} -> {translated}")
        cleaned = str(translated)

    path = Path(cleaned).expanduser()
    if not path.is_absolute():
        raise ValueError(f"Working directory must be an absolute path, got {working_directory!r}")
    if path.is_file():
        raise ValueError(f"{str(path)!r} is a file, not a folder")
    if not path.exists():
        if not create_missing:
            raise ValueError(f"Folder does not exist: {path}")
        try:
            path.mkdir(parents=True)
            logger.info(f"Created working directory {path}")
        except OSError as e:
            raise ValueError(f"Folder '{path}' does not exist and could not be created: {e}")
    return str(path)
