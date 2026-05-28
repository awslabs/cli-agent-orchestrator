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


def get_session_env(session_name: str) -> dict[str, str]:
    """Return the forwarded env vars for ``session_name`` (empty dict if none)."""
    with _lock:
        return dict(_session_forwarded_env.get(session_name, {}))


def clear_session_env(session_name: str) -> None:
    """Drop the mapping for ``session_name``. Called on session teardown."""
    with _lock:
        _session_forwarded_env.pop(session_name, None)
