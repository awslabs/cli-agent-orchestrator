"""Cooperative, fail-closed permits for external decision requests."""

import errno
import fcntl
import logging
import math
import os
import time
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, Callable, Iterator

from cli_agent_orchestrator.constants import CAO_HOME_DIR
from cli_agent_orchestrator.decisions.settings import DecisionSettings, load_settings
from cli_agent_orchestrator.decisions.types import PointState

HALT_FILE = CAO_HOME_DIR / "decisions.halt"
EGRESS_LOCK_FILE = CAO_HOME_DIR / "decisions-egress.lock"
_settings_loader: Callable[[], DecisionSettings] = load_settings
logger = logging.getLogger(__name__)

#: How long a halt-marker mutation waits for the exclusive lock before giving up.
#: Bounded because the shared side of this lock is held for the duration of an
#: external decision request, and no operator command may hang on one.
HALT_CONTROL_TIMEOUT_S = 5.0


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


@contextmanager
def exclusive(timeout_s: float) -> Iterator[bool]:
    """Hold the egress lock EXCLUSIVELY for the body, or yield False.

    The one acquisition routine for "nothing else may be reading or changing the
    halt state right now", shared by :func:`drain` and by the halt-marker
    mutations in ``decisions/settings.py`` and ``decisions/operator.py``. Holding
    the lock ACROSS a decision and the write it authorizes is the point: #1062's
    first attempt checked the marker in the ops MCP middleware and removed it
    later in ``set_point``, so an ``operator.off()`` landing between the two was
    erased by a mutation that had already been allowed.

    Yields whether the lock is held. It never raises and never blocks past
    ``timeout_s``: an unusable lock file must not cost an operator the command,
    and each caller decides for itself whether to proceed without it (the CLI
    does, a bound MCP caller is refused).
    """
    fd: int | None = None
    held = False
    try:
        try:
            fd = _open_lock()
            deadline = time.monotonic() + max(0.0, timeout_s)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held = True
                    break
                except OSError as error:
                    remaining = deadline - time.monotonic()
                    if error.errno not in (errno.EAGAIN, errno.EACCES) or remaining <= 0:
                        break
                    time.sleep(min(0.01, remaining))
        except OSError:
            held = False
        yield held
    finally:
        if fd is not None:
            os.close(fd)


def drain(timeout_s: float) -> bool:
    """Wait for existing shared permits, releasing the exclusive lock on return."""
    if not math.isfinite(timeout_s) or timeout_s < 0:
        return False
    with exclusive(timeout_s) as held:
        return held
