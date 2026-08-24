"""Per-session-name lifecycle lock serializing session CREATE against TEARDOWN.

Why this exists (#498): tmux and the terminal registry (SQLite) are two separate
stores, and a session's lifecycle transitions write BOTH. Without mutual
exclusion, a create landing inside a teardown's critical section interleaves
arbitrarily and orphans state in one store or the other:

* teardown snapshots the rows for ``name``, confirms tmux gone, and sweeps —
  while a concurrent create has meanwhile put a NEW tmux session under ``name``
  plus a fresh row. Scoping the sweep by row id keeps the new row alive, but
  nothing stops the reverse interleaving where the create's tmux session is
  killed by the teardown that already decided ``name`` was dead.
* two concurrent teardowns of ``name`` both dispatch a kill and both sweep.

Making each transition atomic per session NAME removes every such interleaving:
a create either completes entirely before a teardown starts (the teardown then
sees the new rows and tears them down properly) or starts entirely after it (the
name is free and the create builds a clean incarnation). Both orders leave the
two stores agreeing, which is the only invariant that matters.

Design notes
------------
``threading.Lock``, not ``asyncio.Lock``. The two call paths do not share an
execution model: teardown is a fully synchronous function that the API runs via
``asyncio.to_thread`` (``api/main.py``), and the CLI reaches it over HTTP, so it
executes on a worker thread with no running loop — an ``asyncio.Lock`` is simply
not acquirable there. Creation runs on the event loop. A threading primitive is
the only one reachable from both, and there is no single-event-loop assumption to
rely on (the MCP server is a separate process talking HTTP).

To keep that safe, the lock is held only across each path's SHORT state-transition
critical section — never across a long ``await``. In particular the create path
releases before ``provider.initialize()`` (tens of seconds), so a teardown of the
same name is never blocked behind an agent launch. See the call sites for exactly
what each critical section spans.

Locks are refcounted and dropped at zero, so the registry cannot grow without
bound as sessions come and go.
"""

import threading
from contextlib import contextmanager
from typing import Dict, Iterator, Tuple

# Guards the registry below. Only ever held for the few dict operations in
# ``session_lifecycle_lock``'s acquire/release bookkeeping — never across the
# caller's critical section, so it can't serialize different session names.
_registry_guard = threading.Lock()

# session name -> (lock, number of holders+waiters currently interested in it).
# The count is what makes eviction safe: the entry is removed only once nobody
# is using it, so two threads contending for the same name always end up on the
# SAME lock object (a plain "pop on release" would let a waiter be handed a
# fresh, uncontended lock and defeat the mutual exclusion entirely).
_session_locks: Dict[str, Tuple[threading.Lock, int]] = {}


@contextmanager
def session_lifecycle_lock(session_name: str) -> Iterator[None]:
    """Hold the lifecycle lock for ``session_name`` across the with-block.

    Different session names never contend (the whole point — a global lock would
    serialize every session operation on the server); the same name is strictly
    serialized, for create-vs-teardown and teardown-vs-teardown alike.

    Release is guaranteed on every exit path, exceptions included.

    NOT reentrant: a call path already holding the lock for ``name`` must not
    re-enter it for the same ``name``, or it self-deadlocks. The two critical
    sections are deliberately narrow and call nothing that re-acquires — plugin
    dispatch and provider initialization both run outside them.
    """
    with _registry_guard:
        lock, holders = _session_locks.get(session_name, (threading.Lock(), 0))
        _session_locks[session_name] = (lock, holders + 1)

    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _registry_guard:
            # Re-read rather than trusting the count captured above: other
            # threads have adjusted it in the meantime.
            entry = _session_locks.get(session_name)
            if entry is not None:
                held_lock, holders = entry
                if holders <= 1:
                    del _session_locks[session_name]
                else:
                    _session_locks[session_name] = (held_lock, holders - 1)
