"""The one arbiter every write to a tmux pane passes through.

A pane is a single serial byte sink with no framing.  Two writers that
interleave do not produce two messages: they produce one corrupted line,
and the Enter belonging to the first submits whatever prefix of the
second has already landed.  Nothing downstream can detect this, because
the pane's contents are the only record and they look like something a
user could plausibly have typed.

The arbiter is therefore not an optimisation.  It is the property that
makes "this control was delivered exactly as written" a statement anyone
is entitled to make.  Every path that writes to a pane — the control
path and, once it is migrated, ordinary delivery — must hold the lease
for that pane across its whole write, including the trailing Enter.

Two layers, because there are two independent hazards:

- A ``threading.Lock`` per pane, because the API server writes from a
  thread pool and two requests for one terminal are concurrent by
  construction.
- An ``fcntl.flock`` on a per-pane lock file, because the CLI, a
  companion process, and the server are separate processes writing to
  the same tmux server.  An in-process lock alone would be silently
  useless in exactly the deployment the project actually runs.

Refusal, not queueing, is the default: ``timeout=0`` fails immediately
with :class:`PaneBusyError`.  A control that cannot get the lease has
provably written nothing, which is the only condition under which a
caller may safely try again.  Waiting is available but must be chosen,
because a caller that blocks indefinitely on a lease has converted a
truthful refusal into an unbounded request.

Failure mode prevented: without a shared arbiter, at-most-once delivery
of a control is unachievable no matter how careful the journal is — the
journal would faithfully record one intent while the pane received an
interleaving of two.
"""

from __future__ import annotations

import fcntl
import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, Optional

from cli_agent_orchestrator.services.control_input_contract import is_valid_pane_id

logger = logging.getLogger(__name__)

# How often a waiting acquirer retries the cross-process lock.  flock has
# no timeout, so a bounded wait has to be a poll.
_POLL_INTERVAL_SECONDS = 0.01

# Per-pane in-process locks, created on first use and kept for the life
# of the process.  Panes are bounded by the tmux server, so the table
# does not grow without limit; entries are cheap and dropping one while
# a lease is outstanding would hand a second thread its own fresh lock.
_pane_locks: Dict[str, threading.Lock] = {}

# Which thread currently holds each pane's lease.  Read to convert what
# would be a self-deadlock into a diagnosable error.
_pane_lock_owners: Dict[str, int] = {}

_registry_guard = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class PaneLease:
    """Proof that the holder is the only writer to ``pane_id`` right now.

    The lease is valid only inside the ``with`` block that produced it.
    It is deliberately not a token that can be stored and presented
    later: the guarantee is "no other writer between these two points in
    time", and an object that outlived the block would misrepresent it.
    """

    pane_id: str
    holder: str
    acquired_at: str


class PaneInputArbiterError(RuntimeError):
    """Base error for pane-lease operations."""


class PaneBusyError(PaneInputArbiterError):
    """Another writer holds the pane; this caller wrote nothing.

    Maps to a typed ``refused`` outcome with reason ``pane-busy``.  The
    refusal is decided before any byte is written, so the same control
    may be attempted again.
    """


class PaneLeaseReentryError(PaneInputArbiterError):
    """This thread already holds the lease it is asking for.

    A programming error, surfaced instead of the deadlock it would
    otherwise cause: the lease is non-reentrant on purpose, because a
    nested write is a second writer as far as the pane is concerned.
    """


def pane_lock_dir() -> Path:
    """Directory holding the per-pane cross-process lock files.

    Resolved at call time from ``CAO_HOME_DIR`` so a test or an isolated
    state root that patches the constant takes effect without a module
    reload.
    """
    from cli_agent_orchestrator.constants import CAO_HOME_DIR

    return Path(CAO_HOME_DIR) / "pane-input-locks"


def _lock_path(pane_id: str, directory: Path) -> Path:
    # pane_id is validated before it gets here, so the '%' strip cannot
    # produce a traversal or an empty name.
    return directory / f"pane-{pane_id[1:]}.lock"


def _lock_for(pane_id: str) -> threading.Lock:
    with _registry_guard:
        lock = _pane_locks.get(pane_id)
        if lock is None:
            lock = threading.Lock()
            _pane_locks[pane_id] = lock
        return lock


def _refuse_reentry(pane_id: str) -> None:
    with _registry_guard:
        owner = _pane_lock_owners.get(pane_id)
    if owner is not None and owner == threading.get_ident():
        raise PaneLeaseReentryError(
            f"thread already holds the input lease for pane {pane_id}; "
            "a nested write is a second writer to the same pane"
        )


def _flock_until(fd: int, deadline: float, pane_id: str) -> None:
    """Take the cross-process lock, or refuse once ``deadline`` passes."""
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PaneBusyError(
                    f"pane {pane_id} input lease is held by another process"
                ) from exc
            time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))
        except OSError as exc:
            # A lock file that cannot be locked at all is not a busy
            # pane; reporting it as busy would invite an endless retry.
            raise PaneInputArbiterError(f"pane {pane_id} lock is unusable: {exc}") from exc


@contextmanager
def pane_input_lease(
    pane_id: str,
    *,
    holder: str,
    timeout: float = 0.0,
    lock_dir: Optional[Path] = None,
) -> Iterator[PaneLease]:
    """Hold the exclusive right to write to ``pane_id`` for the block.

    The caller must perform its entire write inside the block, including
    the submitting Enter.  Releasing between the text and the Enter would
    let another writer's bytes be submitted by this writer's Enter.

    Args:
        pane_id: Immutable tmux pane id (``%N``).
        holder: Short label identifying the writer, for diagnostics.
        timeout: Seconds to wait.  ``0`` refuses immediately, which is
            the default because a refusal is the honest answer that
            permits a retry.
        lock_dir: Override for the cross-process lock directory.

    Raises:
        ValueError: The pane id, holder, or timeout is invalid.
        PaneBusyError: Another writer holds the pane; nothing was written.
        PaneLeaseReentryError: This thread already holds the lease.
        PaneInputArbiterError: The lock file itself is unusable.
    """
    if not is_valid_pane_id(pane_id):
        raise ValueError(f"Invalid pane_id: {pane_id!r}")
    if not holder:
        raise ValueError("A pane input lease requires a holder label")
    if timeout < 0:
        raise ValueError("timeout must not be negative")

    directory = Path(lock_dir) if lock_dir is not None else pane_lock_dir()
    deadline = time.monotonic() + timeout

    _refuse_reentry(pane_id)
    lock = _lock_for(pane_id)
    if timeout > 0:
        acquired = lock.acquire(True, timeout)
    else:
        acquired = lock.acquire(False)
    if not acquired:
        raise PaneBusyError(f"pane {pane_id} input lease is held by another thread")

    descriptor: Optional[int] = None
    try:
        with _registry_guard:
            _pane_lock_owners[pane_id] = threading.get_ident()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(_lock_path(pane_id, directory), os.O_CREAT | os.O_RDWR, 0o600)
        _flock_until(descriptor, deadline, pane_id)
        lease = PaneLease(pane_id=pane_id, holder=holder, acquired_at=_now())
        logger.debug("pane_input_lease acquired: %s by %s", pane_id, holder)
        yield lease
    finally:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        with _registry_guard:
            _pane_lock_owners.pop(pane_id, None)
        lock.release()


def is_pane_leased(pane_id: str) -> bool:
    """Whether this process currently holds ``pane_id``'s lease.

    Diagnostics only.  It says nothing about other processes and is
    stale the instant it returns, so it must never gate a write — that
    is what taking the lease is for.
    """
    with _registry_guard:
        return pane_id in _pane_lock_owners


def reset_pane_input_arbiter() -> None:
    """Drop the per-pane lock table (tests / isolated state roots).

    Only safe when no lease is outstanding; a reset while a writer holds
    a lease would give the next caller a different lock object and
    silently permit two concurrent writers.
    """
    with _registry_guard:
        _pane_locks.clear()
        _pane_lock_owners.clear()
