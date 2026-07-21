"""Persistent store for per-session forwarded environment variables.

``cao launch --env KEY=VALUE`` lets operators forward arbitrary env vars to
the supervisor terminal. Those vars must also reach workers spawned later in
the same session via ``assign`` / ``handoff`` / the web UI — otherwise the
supervisor's children would not see ``MNEMOSYNE_DIR`` and the like. This
module persists the mapping so ``create_window`` calls can pick it up. See
issue #248.

The store is **write-through**: the SQLite ``session_env`` table is the source
of truth and the in-memory map is only a cache over it, so the forwarded env
survives a cao-server restart. ``set_session_env`` upserts the row, then
updates the cache; ``get_session_env`` serves the cache, falling back to a DB
read (and repopulating the cache) on miss — which is exactly the post-restart
path; ``clear_session_env`` deletes the row and the cache entry. Passing an
empty dict to ``set_session_env`` clears the mapping: it deletes the row and
never stores an empty row.

Reads **fail closed**. A *missing* row is the legitimate "no forwarded env"
case and returns ``{}``. An *unreadable* state — corrupt ``env_vars`` JSON, a
locked/unreadable DB past a short bounded retry, or a missing ``session_env``
table — raises :class:`SessionEnvStoreError` so window/provider creation
aborts before any tmux launch, instead of silently launching a terminal on
ambient (empty) env.

Deletes **fail closed too** (cond-0050). ``clear_session_env`` is strict: the
bounded durable delete must succeed *before* the cache entry is evicted, and
retry exhaustion raises :class:`SessionEnvStoreError` with the cache entry
left in place. An evicted cache over a surviving row is exactly the cold-read
resurrection path for stale routing env, so cache and DB must never disagree
in that direction. Callers that genuinely cannot propagate — a teardown path
preserving an earlier, more primary exception — catch and log at their own
call site; the shared store never swallows.

Security note: env values are stored PLAINTEXT in the CAO SQLite DB (0600 file
in a 0700 dir). Forwarded values are non-secret path/routing data by
conductor invariant (PATH/ZDOTDIR shim routing); do not forward secrets
through ``--env``.
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional, TypeVar, cast

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import SessionEnvModel

logger = logging.getLogger(__name__)

_session_forwarded_env: dict[str, dict[str, str]] = {}
_lock = threading.Lock()

# Bounded retry for transient DB contention (e.g. "database is locked" while
# another connection holds a write lock). Short on purpose: a genuinely broken
# DB must surface fast so window creation fails before any tmux launch.
_MAX_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 0.1

_T = TypeVar("_T")


class SessionEnvStoreError(RuntimeError):
    """Persisted session-env state is unreadable or unwritable; fail closed."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _with_bounded_retry(op: Callable[[], _T], description: str) -> _T:
    """Run ``op`` with a short bounded retry, raising SessionEnvStoreError on exhaustion."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return op()
        except SessionEnvStoreError:
            raise
        except Exception as e:  # SQLAlchemy OperationalError and friends
            last_exc = e
            if attempt < _MAX_ATTEMPTS:
                time.sleep(_RETRY_DELAY_SECONDS)
    raise SessionEnvStoreError(
        f"session-env store unavailable after {_MAX_ATTEMPTS} attempts ({description}): {last_exc}"
    )


def _upsert_row(session_name: str, payload: str) -> None:
    with database.SessionLocal() as db:
        row = db.query(SessionEnvModel).filter(SessionEnvModel.session_name == session_name).first()
        if row is None:
            db.add(
                SessionEnvModel(
                    session_name=session_name,
                    env_vars=payload,
                    updated_at=_utcnow_iso(),
                )
            )
        else:
            row_any = cast(Any, row)
            row_any.env_vars = payload
            row_any.updated_at = _utcnow_iso()
        db.commit()


def _delete_row(session_name: str) -> None:
    with database.SessionLocal() as db:
        db.query(SessionEnvModel).filter(SessionEnvModel.session_name == session_name).delete()
        db.commit()


def _read_row(session_name: str) -> Optional[str]:
    with database.SessionLocal() as db:
        row = db.query(SessionEnvModel).filter(SessionEnvModel.session_name == session_name).first()
        return cast(Optional[str], row.env_vars) if row is not None else None


def set_session_env(session_name: str, env_vars: dict[str, str]) -> None:
    """Register the forwarded env vars for ``session_name``.

    Overwrites any prior mapping. Passing an empty dict clears it via the
    strict :func:`clear_session_env` (the row is deleted, never stored empty;
    a delete that cannot complete durably raises instead of being swallowed).
    The DB upsert happens first — the cache is only updated after the write is
    durable, and a failed write raises
    :class:`SessionEnvStoreError` rather than caching state a restart would
    silently lose.
    """
    if not env_vars:
        clear_session_env(session_name)
        return
    payload = json.dumps(env_vars)
    _with_bounded_retry(lambda: _upsert_row(session_name, payload), f"set {session_name}")
    with _lock:
        _session_forwarded_env[session_name] = dict(env_vars)


def get_session_env(session_name: str) -> dict[str, str]:
    """Return the forwarded env vars for ``session_name`` (empty dict if none).

    Serves the in-memory cache first; on a miss (e.g. after a server restart)
    reads the DB and repopulates the cache. A missing row returns ``{}`` — the
    legitimate no-forwarded-env case. Corrupt JSON, an unreadable/locked DB
    past the bounded retry, or a missing ``session_env`` table raises
    :class:`SessionEnvStoreError` so callers fail closed before tmux launch.
    """
    with _lock:
        cached = _session_forwarded_env.get(session_name)
    if cached is not None:
        return dict(cached)
    stored = _with_bounded_retry(lambda: _read_row(session_name), f"get {session_name}")
    if stored is None:
        return {}
    try:
        data = json.loads(stored)
    except json.JSONDecodeError as e:
        raise SessionEnvStoreError(
            f"corrupt persisted session env for '{session_name}': {e}"
        ) from e
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        raise SessionEnvStoreError(
            f"corrupt persisted session env for '{session_name}': not a JSON object of strings"
        )
    with _lock:
        _session_forwarded_env[session_name] = dict(data)
    return dict(data)


def clear_session_env(session_name: str) -> None:
    """Drop the mapping for ``session_name``. Called on session teardown and
    on the no-env new-session pre-clear.

    Strict (cond-0050): the bounded durable row delete must succeed before
    the cache entry is evicted. On retry exhaustion this raises
    :class:`SessionEnvStoreError` and the cache entry is left in place — an
    evicted cache over a surviving durable row is precisely how a valid stale
    mapping gets resurrected by a later cold read and injected into a reused
    session name. Callers on exception-teardown paths that must preserve an
    earlier, more primary exception catch and log this failure at their own
    call site; this function itself never swallows it.
    """
    _with_bounded_retry(lambda: _delete_row(session_name), f"clear {session_name}")
    with _lock:
        _session_forwarded_env.pop(session_name, None)


def reconcile_session_env(session_exists: Callable[[str], bool]) -> dict:
    """Delete persisted rows whose tmux session no longer exists (startup recovery).

    Sessions torn down while the server was dead leave rows behind; without
    this sweep a later same-named session could inherit stale env. Rows whose
    liveness cannot be determined are kept (fail toward retention — a live
    session's row must never be dropped on a probe error). Deletion uses the
    strict clear: a row is recorded as ``removed`` only after its durable
    deletion is confirmed; a row whose deletion fails is retained and named
    in ``failed`` (cond-0050 — never falsely reported removed). ``session_exists``
    is injectable so the lifespan passes the active backend's check and tests
    can stub it. Returns ``{"removed": [...], "kept": [...], "failed": [...]}``.
    """
    removed, kept, failed = [], [], []
    with database.SessionLocal() as db:
        names = [cast(str, row.session_name) for row in db.query(SessionEnvModel).all()]
    for name in names:
        try:
            alive = session_exists(name)
        except Exception:
            logger.warning("session-env reconcile: liveness probe failed for %s; keeping row", name)
            kept.append(name)
            continue
        if alive:
            kept.append(name)
        else:
            try:
                clear_session_env(name)
            except SessionEnvStoreError as e:
                logger.warning(
                    "session-env reconcile: could not delete row for dead session %s: %s", name, e
                )
                failed.append(name)
            else:
                removed.append(name)
    logger.info(
        "session-env reconcile: %d removed (dead sessions), %d kept, %d failed deletions",
        len(removed),
        len(kept),
        len(failed),
    )
    return {"removed": removed, "kept": kept, "failed": failed}
