"""One ``cao-server`` per ``CAO_HOME_DIR`` (issues #406, #709).

Two servers on one data directory were never supported, but until this module
nothing refused them: the TCP port was the only collision, and a second
``--port`` sidestepped it. The damage is silent and application-level, not a
SQLite error:

* the second server's startup recovery sweeps treat the first server's live
  deferred-init and initial-delivery rows as crash remnants and persist them as
  ``interrupted_init`` / ``failed``;
* both servers sweep the same pending inbox rows, so a receiver can get one
  message twice (the cross-process form of #406, which the per-terminal
  ``threading.Lock`` in ``inbox_service`` cannot see);
* both own the same tmux namespace, so one server's ``DELETE /sessions`` kills
  the other's panes.

Mechanism
---------
The server takes ``fcntl.flock(LOCK_EX | LOCK_NB)`` on ``$CAO_HOME_DIR/server.lock``
before the database, recovery sweeps, terminal registry, status monitor or port
bind, and holds the descriptor for the life of the process. ``flock`` rather
than a PID file or POSIX ``fcntl`` record locks because:

* the kernel drops it when the holder dies, however it dies (SIGKILL, panic,
  OOM), so there is no stale-lock recovery code and no PID-recycling check;
* unlike POSIX record locks it survives an unrelated ``close()`` of another
  descriptor on the same file within this process;
* the descriptor is opened close-on-exec, so a provider subprocess the server
  spawns cannot keep the lock alive after the server is gone.

The JSON written into the file (pid, port, started_at, version) is DIAGNOSTIC
ONLY: it feeds the refusal message. The kernel lock is the truth, so a stale
file left by a crash is harmless and is simply overwritten by the next holder.
The file is never unlinked on release: unlinking a lock file while another
process is about to open it leaves two holders locking two different inodes.

There is deliberately no environment escape hatch. A hatch would be the hole
this module closes.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Optional

from cli_agent_orchestrator.constants import CAO_HOME_DIR

# Read at acquire time, not import time, so tests can redirect it per test the
# way ``conftest._hermetic_cao_env`` redirects ``SETTINGS_FILE``.
SERVER_LOCK_FILE: Path = CAO_HOME_DIR / "server.lock"

_STARTED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _package_version() -> str:
    try:
        return version("cli-agent-orchestrator")
    except PackageNotFoundError:
        return "unknown"


@dataclass(frozen=True)
class ServerLockHolder:
    """Diagnostics the holding server wrote into the lock file."""

    pid: int
    port: int
    started_at: str
    version: str

    def started_at_minutes(self) -> str:
        """``2026-10-21T14:03Z`` when parseable, the raw value otherwise."""
        try:
            parsed = datetime.strptime(self.started_at, _STARTED_AT_FORMAT)
        except ValueError:
            return self.started_at
        return parsed.strftime("%Y-%m-%dT%H:%MZ")


class ServerLockHeldError(RuntimeError):
    """Another ``cao-server`` already holds the lock for this ``CAO_HOME_DIR``.

    ``holder`` is ``None`` when the lock file could not be read or parsed; the
    lock itself is still held, only the detail is missing.
    """

    def __init__(self, lock_path: Path, holder: Optional[ServerLockHolder]) -> None:
        self.lock_path = lock_path
        self.home = lock_path.parent
        self.holder = holder
        super().__init__(self.message())

    def message(self) -> str:
        return format_refusal(self.home, self.holder)


def format_refusal(home: Path, holder: Optional[ServerLockHolder]) -> str:
    """The operator-facing refusal, with the holder's detail when readable."""
    if holder is not None:
        first = (
            f"cao: another server (PID {holder.pid}, port {holder.port}, "
            f"started {holder.started_at_minutes()}) already owns"
        )
    else:
        first = "cao: another server already owns"
    return "\n".join(
        [
            first,
            f"     {home}",
            "     Stop it, or run this instance with a different CAO_HOME_DIR and --port.",
            "     Two servers on one data directory corrupt inbox delivery and terminal state.",
        ]
    )


def read_holder(lock_path: Path) -> Optional[ServerLockHolder]:
    """Parse the holder diagnostics, or ``None`` if unreadable or malformed."""
    try:
        raw: Any = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    pid = raw.get("pid")
    port = raw.get("port")
    started_at = raw.get("started_at")
    pkg_version = raw.get("version")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not isinstance(started_at, str)
        or not isinstance(pkg_version, str)
    ):
        return None
    return ServerLockHolder(pid=pid, port=port, started_at=started_at, version=pkg_version)


class ServerLock:
    """An acquired server lock. Hold it for the process lifetime; ``release()`` on clean exit."""

    def __init__(self, path: Path, fd: int, holder: ServerLockHolder) -> None:
        self.path = path
        self._fd: Optional[int] = fd
        self.holder = holder

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        """Drop the kernel lock and close the descriptor. Idempotent. Never unlinks."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "ServerLock":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def acquire_server_lock(port: int, *, path: Optional[Path] = None) -> ServerLock:
    """Take the exclusive, non-blocking server lock and write this process's diagnostics.

    Raises :class:`ServerLockHeldError` when another process holds it. Any other
    ``OSError`` (unwritable home, read-only filesystem) propagates: a server that
    cannot prove it is alone must not start.
    """
    lock_path = SERVER_LOCK_FILE if path is None else path
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # O_CLOEXEC: provider subprocesses must not inherit the descriptor. Python
    # already makes descriptors non-inheritable (PEP 446); the flag states the
    # intent at the one place it matters.
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise ServerLockHeldError(lock_path, read_holder(lock_path)) from None
    except OSError:
        os.close(fd)
        raise

    holder = ServerLockHolder(
        pid=os.getpid(),
        port=port,
        started_at=datetime.now(timezone.utc).strftime(_STARTED_AT_FORMAT),
        version=_package_version(),
    )
    try:
        payload = json.dumps(
            {
                "pid": holder.pid,
                "port": holder.port,
                "started_at": holder.started_at,
                "version": holder.version,
            },
            indent=2,
        )
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, (payload + "\n").encode("utf-8"))
    except OSError:
        # Diagnostics are best-effort; the kernel lock is what protects the home.
        pass
    return ServerLock(lock_path, fd, holder)


def refuse(exc: ServerLockHeldError) -> None:
    """Print the refusal to stderr and stop the process with a non-zero status."""
    print(exc.message(), file=sys.stderr, flush=True)
    raise SystemExit(1)
