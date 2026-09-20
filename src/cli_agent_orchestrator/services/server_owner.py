"""Single active ``cao-server`` owner, enforced in code (#745).

`replicas: 1` is not a single-owner guarantee. A Deployment's default rolling
update deliberately overlaps the old and new pod, a manual
``kubectl delete pod`` replacement can race the old process's shutdown, and an
operator can simply scale past one replica. In every one of those cases two
servers would open the same SQLite state and the same runtime channel
registry, and nothing about the replica count stops them.

So the server claims ownership of its state directory before it touches it. A
second server on the same state does not start degraded or start racing: it
refuses to serve and says who holds the lock.

Why ``fcntl.flock`` rather than a pidfile or a lease row: the kernel releases
it when the holding process dies, however it dies. A crashed server leaves no
stale lock to expire or manually clear, which is exactly the failure mode that
makes pidfile-based guards worse than nothing during a rollout.

What this does NOT cover, stated plainly: flock is per-kernel. It serializes
two processes that share a mount, which is the only case an RWO EBS volume
admits - the volume attaches to one node at a time, so a second pod either
lands on that node (and hits this lock) or fails to attach. On a hypothetical
RWX/NFS state directory spanning nodes, flock semantics are the filesystem's
to honour and this is no longer a guarantee. Multi-writer state is not
supported either way; see the rollout procedure in the EKS example README.
"""

import errno
import json
import logging
import os
import socket
import time
from pathlib import Path
from typing import Optional

from cli_agent_orchestrator.constants import DB_DIR

try:
    import fcntl

    _FCNTL_AVAILABLE = True
except ImportError:  # pragma: no cover - Windows/non-Unix
    _FCNTL_AVAILABLE = False

logger = logging.getLogger(__name__)

OWNER_LOCK_PATH = DB_DIR / "server-owner.lock"

_DISABLE_VALUES = frozenset({"0", "false", "no", "off"})

# One fd per process, reference counted. flock is associated with the open file
# description, not the process, so a second open() in this same process would
# block against the first - which is what an app started twice in-process (the
# test suite's TestClient, an embedded server) would do. Sharing the fd makes
# re-entry a no-op instead of a self-deadlock.
_lock_fd: Optional[int] = None
_holders = 0


class ServerOwnershipError(RuntimeError):
    """Another server process already owns this state directory."""

    def __init__(self, lock_path: Path, holder: str) -> None:
        self.lock_path = lock_path
        self.holder = holder
        super().__init__(
            f"another cao-server already owns {lock_path.parent} ({holder}). "
            "Only one server may own a state directory: two would write the same "
            "database and answer for the same runtimes. Stop the running server, "
            "or point this one at a different CAO_HOME_DIR."
        )


def ownership_enforced() -> bool:
    """Whether the owner lock is active.

    On by default. ``CAO_SERVER_OWNER_LOCK=0`` opts out for the one legitimate
    case - deliberately running two servers against one state directory while
    developing - and is a documented footgun, not a supported topology.
    """
    return os.environ.get("CAO_SERVER_OWNER_LOCK", "1").strip().lower() not in _DISABLE_VALUES


def _describe_holder(lock_path: Path) -> str:
    """Best-effort identity of the process currently holding the lock.

    Read, not authoritative: the holder may be mid-write, or may have written
    nothing yet. An unreadable identity must still produce a refusal, so this
    degrades to a string rather than raising.
    """
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        return (
            f"pid {data.get('pid', '?')} on {data.get('host', '?')}, "
            f"started {data.get('started_at', '?')}"
        )
    except (OSError, ValueError, AttributeError):
        return "identity unavailable"


def acquire_server_ownership(lock_path: Optional[Path] = None) -> bool:
    """Claim exclusive ownership of the server state directory.

    Returns True when this call took (or re-entered) the lock, False when
    enforcement is disabled. Raises :class:`ServerOwnershipError` when another
    process holds it - the caller must not proceed to serve.

    ``lock_path`` is resolved at call time, not bound as a default, so the
    module constant stays overridable.
    """
    global _lock_fd, _holders

    lock_path = lock_path or OWNER_LOCK_PATH

    if not ownership_enforced():
        logger.warning(
            "cao-server owner lock disabled by CAO_SERVER_OWNER_LOCK; "
            "concurrent servers on this state directory are unguarded"
        )
        return False

    if _lock_fd is not None:
        _holders += 1
        return True

    if not _FCNTL_AVAILABLE:  # pragma: no cover - non-Unix
        logger.warning("fcntl unavailable on this platform; cao-server owner lock is not enforced")
        return False

    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            raise ServerOwnershipError(lock_path, _describe_holder(lock_path)) from exc
        raise

    # Identity is written only after the lock is held, so whatever a refused
    # server reads back belongs to the real owner.
    identity = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(identity).encode("utf-8"))
        os.fsync(fd)
    except OSError:
        # The lock is what enforces ownership; a failed identity write costs a
        # future refusal its "who holds it" detail and nothing more.
        logger.warning("could not record owner identity in %s", lock_path, exc_info=True)

    _lock_fd = fd
    _holders = 1
    logger.info("cao-server owns %s (pid %s)", lock_path.parent, identity["pid"])
    return True


def release_server_ownership() -> None:
    """Drop this process's claim once the last holder is done.

    Explicit release matters for an orderly rollout: the outgoing server frees
    the lock at shutdown so its replacement can start without waiting for the
    kernel to reap the process.
    """
    global _lock_fd, _holders

    if _lock_fd is None:
        return
    _holders -= 1
    if _holders > 0:
        return
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(_lock_fd)
    except OSError:
        pass
    _lock_fd = None
    _holders = 0
