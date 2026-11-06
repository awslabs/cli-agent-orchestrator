"""Create-only, owner-only files and directories for CAO-managed stores.

Shared by the ephemeral store (``services/ephemeral_service.py``) and the
per-launch Kiro homes (``providers/kiro_launch_home.py``). A file is created
with ``O_EXCL|O_NOFOLLOW`` at 0600 and fsynced, so an existing entry of any
type, a symlink included, is never written through; its ``(dev, ino)`` is
recorded so a rollback removes only the entry this process created.
"""

import os
import stat
from pathlib import Path

# One created file: its path and the (device, inode) it had when created.
OwnedEntry = tuple[Path, int, int]


def private_directory(path: Path) -> None:
    """Make ``path`` a 0700 directory, refusing a symlink or another entry type."""
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    value = path.lstat()
    if not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode):
        raise OSError("ephemeral store directory is unsafe")
    path.chmod(0o700)


def write_exclusive(path: Path, payload: bytes, owned: list[OwnedEntry]) -> None:
    """Create ``path`` with ``payload`` at 0600, recording it in ``owned``."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        owned.append((path, info.st_dev, info.st_ino))
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def remove_owned(owned: list[OwnedEntry]) -> None:
    """Unlink each recorded file, newest first, only while it is still the one created."""
    for path, device, inode in reversed(owned):
        try:
            current = path.lstat()
            if current.st_dev == device and current.st_ino == inode:
                path.unlink()
        except FileNotFoundError:
            pass
