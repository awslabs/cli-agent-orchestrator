"""Cooperative, fail-closed permits for external decision requests."""

import errno
import fcntl
import math
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

from cli_agent_orchestrator.constants import CAO_HOME_DIR
from cli_agent_orchestrator.decisions.settings import DecisionSettings, load_settings
from cli_agent_orchestrator.decisions.types import PointState

HALT_FILE = CAO_HOME_DIR / "decisions.halt"
EGRESS_LOCK_FILE = CAO_HOME_DIR / "decisions-egress.lock"
_settings_loader: Callable[[], DecisionSettings] = load_settings


def set_settings_loader(loader: Callable[[], DecisionSettings]) -> None:
    """Bind fresh reads to the running server's startup environment snapshot."""
    global _settings_loader
    _settings_loader = loader


def is_halted() -> bool:
    """Treat an inaccessible marker as halted, including a dangling symlink."""
    try:
        HALT_FILE.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _open_lock() -> int:
    EGRESS_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    return os.open(EGRESS_LOCK_FILE, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)


@asynccontextmanager
async def egress_permit(point: str, decider: str) -> AsyncIterator[bool]:
    """Hold a shared lock throughout the send, or yield False without waiting."""
    fd: int | None = None
    allowed = False
    try:
        try:
            fd = _open_lock()
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            if not is_halted():
                configured = _settings_loader().points.get(point)
                allowed = (
                    configured is not None
                    and configured.state != PointState.OFF
                    and configured.decider == decider
                    and not is_halted()
                )
        except (OSError, ValueError, RuntimeError):
            allowed = False
        yield allowed
    finally:
        if fd is not None:
            os.close(fd)


def drain(timeout_s: float) -> bool:
    """Wait for existing shared permits, releasing the exclusive lock on return."""
    if not math.isfinite(timeout_s) or timeout_s < 0:
        return False
    fd: int | None = None
    deadline = time.monotonic() + timeout_s
    try:
        fd = _open_lock()
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError as error:
                remaining = deadline - time.monotonic()
                if error.errno not in (errno.EAGAIN, errno.EACCES) or remaining <= 0:
                    return False
                time.sleep(min(0.01, remaining))
    except OSError:
        return False
    finally:
        if fd is not None:
            os.close(fd)
