"""In-memory store for per-session forwarded environment variables.

``cao launch --env KEY=VALUE`` lets operators forward arbitrary env vars to
the supervisor terminal. Those vars must also reach workers spawned later in
the same session via ``assign`` / ``handoff`` / the web UI — otherwise the
supervisor's children would not see ``MNEMOSYNE_DIR`` and the like. This
module persists the mapping for the session lifetime so ``create_window``
calls can pick it up. See issue #248.

The store is process-local: cao-server holds it, restarts wipe it. There is
no schema migration and no on-disk format.
"""

import threading
from typing import Callable, Mapping, Optional

_session_forwarded_env: dict[str, dict[str, str]] = {}
_lock = threading.Lock()


def set_session_env(session_name: str, env_vars: dict[str, str]) -> None:
    """Register the forwarded env vars for ``session_name``.

    Overwrites any prior mapping. Passing an empty dict clears it.
    """
    with _lock:
        if env_vars:
            _session_forwarded_env[session_name] = dict(env_vars)
        else:
            _session_forwarded_env.pop(session_name, None)


def merge_session_env(
    session_name: str,
    delta: Mapping[str, str],
    *,
    validate: Optional[Callable[[dict[str, str]], object]] = None,
) -> dict[str, str]:
    """Merge ``delta`` on top of ``session_name``'s mapping in one critical section.

    Per-key overwrite; keys absent from ``delta`` are kept. Returns a copy of the
    merged mapping. Reading, merging and storing under a single lock is the point:
    a ``get_session_env`` followed by ``set_session_env`` lets two concurrent
    callers each read the old map and the second write drop the first's keys.

    ``validate``, when given, is called with the merged mapping inside the lock and
    before anything is stored. If it raises, the exception propagates and the stored
    mapping is left exactly as it was — so a caller can bound the merged result
    without a check-then-write race. It runs under ``_lock``, which is not
    re-entrant: it must not call back into this module.
    """
    with _lock:
        merged = {**_session_forwarded_env.get(session_name, {}), **delta}
        if validate is not None:
            validate(merged)
        if merged:
            _session_forwarded_env[session_name] = merged
        else:
            _session_forwarded_env.pop(session_name, None)
        return dict(merged)


def get_session_env(session_name: str) -> dict[str, str]:
    """Return the forwarded env vars for ``session_name`` (empty dict if none)."""
    with _lock:
        return dict(_session_forwarded_env.get(session_name, {}))


def clear_session_env(session_name: str) -> None:
    """Drop the mapping for ``session_name``. Called on session teardown."""
    with _lock:
        _session_forwarded_env.pop(session_name, None)
