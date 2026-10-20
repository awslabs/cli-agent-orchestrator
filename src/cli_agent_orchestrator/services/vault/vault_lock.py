"""Whole-operation serialization for mutations of one vault projection.

Filesystem inode identity unifies live aliases of one root. Processes in
different mount namespaces can still observe different device identities for
the same storage; that accepted limitation mirrors path identity in reverse.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cli_agent_orchestrator.constants import LOCK_DIR
from cli_agent_orchestrator.services.vault.config import VaultSpec

try:
    import fcntl

    _FCNTL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only on non-POSIX platforms
    fcntl = None  # type: ignore[assignment]
    _FCNTL_AVAILABLE = False

VAULT_PROJECTION_LOCK_TIMEOUT_S = 120.0
VAULT_PROJECTION_LOCK_INTERACTIVE_TIMEOUT_S = 10.0
_LOCK_POLL_INTERVAL_S = 0.05


class VaultProjectionBusyError(RuntimeError):
    """Raised when another operation holds the vault projection lock."""

    def __init__(self, vault_id: str, timeout: float) -> None:
        self.vault_id = vault_id
        self.timeout = timeout
        super().__init__(
            f"timed out after {timeout}s waiting for vault projection lock for {vault_id!r}"
        )


class VaultProjectionUnavailableError(RuntimeError):
    """Raised when the platform cannot provide inter-process serialization."""


@dataclass
class _LockRecord:
    owner_pid: int
    owner_thread: int
    depth: int
    fd: int


_thread_state = threading.local()
_registry_lock = threading.Lock()
_open_fds: set[int] = set()
_registry_pid = os.getpid()


def _records() -> dict[str, _LockRecord]:
    records = getattr(_thread_state, "records", None)
    if records is None:
        records = {}
        _thread_state.records = records
    return records


def _projection_identity(root: str) -> str:
    """Return one live-filesystem identity, with a stable path fallback."""
    resolved = str(Path(root).resolve(strict=False))
    try:
        stat_result = os.stat(resolved)
    except OSError:
        return f"path:{resolved}"
    if not stat_result.st_ino:
        return f"path:{resolved}"
    return f"inode:{stat_result.st_dev}:{stat_result.st_ino}"


def _projection_lock_path(identity: str) -> Path:
    """Map a projection identity to its CAO-owned lock file."""
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return Path(LOCK_DIR) / f"vault-projection-{digest}.lock"


def _open_registered_fd(lock_path: Path, flags: int) -> int:
    """Open and register one fd while fork is excluded from the entire window."""
    with _registry_lock:
        fd = -1
        try:
            fd = os.open(str(lock_path), flags, 0o600)
            _open_fds.add(fd)
            return fd
        except BaseException:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise


def _unregister_fd(fd: int) -> None:
    with _registry_lock:
        _open_fds.discard(fd)


def _before_fork() -> None:
    _registry_lock.acquire()


def _after_fork_parent() -> None:
    _registry_lock.release()


def _reset_inherited_state() -> None:
    global _open_fds, _registry_lock, _registry_pid, _thread_state
    for fd in tuple(_open_fds):
        try:
            os.close(fd)
        except OSError:
            pass
    _open_fds = set()
    _registry_lock = threading.Lock()
    _thread_state = threading.local()
    _registry_pid = os.getpid()


def _after_fork_child() -> None:
    _reset_inherited_state()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_before_fork,
        after_in_parent=_after_fork_parent,
        after_in_child=_after_fork_child,
    )


def _discard_inherited_record(
    records: dict[str, _LockRecord],
    key: str,
    record: _LockRecord,
) -> None:
    records.pop(key, None)
    _unregister_fd(record.fd)
    try:
        os.close(record.fd)
    except OSError:
        pass


def holds_projection_lock(vault: VaultSpec) -> bool:
    """Return whether this PID and thread already own ``vault``'s lock."""
    pid = os.getpid()
    if _registry_pid != pid:
        _reset_inherited_state()
    record = _records().get(_projection_identity(vault.root))
    return bool(
        record is not None
        and record.owner_pid == pid
        and record.owner_thread == threading.get_ident()
        and record.depth > 0
    )


@contextmanager
def vault_projection_lock(
    vault: VaultSpec,
    *,
    timeout: float = VAULT_PROJECTION_LOCK_TIMEOUT_S,
) -> Iterator[None]:
    """Serialize one vault's complete scan, decision, and commit operation.

    Re-entry is permitted only for the same process, thread, and filesystem
    identity. Platforms without ``fcntl.flock`` fail closed. The timeout is
    only the contention wait budget after this synchronous acquisition starts;
    it does not bound executor queueing or operation runtime.

    Invariant: critical sections must remain synchronous and on the acquiring
    thread. Awaiting or offloading work inside them would invalidate
    thread-bound re-entrancy and can permit overlap or self-blocking.
    """
    if not _FCNTL_AVAILABLE:
        raise VaultProjectionUnavailableError("vault projection mutation requires fcntl.flock")

    pid = os.getpid()
    if _registry_pid != pid:
        # Covers fork paths where the registered child callback did not run.
        # Do not acquire an inherited registry lock: another vanished thread
        # could have held it at the instant of the fork.
        _reset_inherited_state()

    key = _projection_identity(vault.root)
    lock_path = _projection_lock_path(key)
    thread_id = threading.get_ident()
    records = _records()
    record = records.get(key)
    if record is not None and record.owner_pid != pid:
        _discard_inherited_record(records, key, record)
        record = None
    if record is not None and record.owner_pid == pid and record.owner_thread == thread_id:
        record.depth += 1
        try:
            yield
        finally:
            record.depth -= 1
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    fd = _open_registered_fd(lock_path, flags)
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise VaultProjectionBusyError(vault.id, timeout)
                time.sleep(_LOCK_POLL_INTERVAL_S)

        records[key] = _LockRecord(pid, thread_id, 1, fd)
        try:
            yield
        finally:
            records.pop(key, None)
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        _unregister_fd(fd)
        try:
            os.close(fd)
        except OSError:
            pass
