"""Session service for session-level operations.

This module provides session management functionality for CAO, where a "session"
corresponds to a tmux session that may contain multiple terminal windows (agents).

Session Hierarchy:
- Session: A tmux session (e.g., "cao-my-project")
  - Terminal: A tmux window within the session (e.g., "developer-abc123")
    - Provider: The CLI agent running in the terminal (e.g., KiroCliProvider)

Key Operations:
- list_sessions(): Get all CAO-managed sessions (filtered by SESSION_PREFIX)
- get_session(): Get session details including all terminal metadata
- delete_session(): Clean up session, providers, database records, and tmux session

Session Lifecycle:
1. create_terminal() with new_session=True creates a new tmux session
2. Additional terminals are added via create_terminal() with new_session=False
3. delete_session() removes the entire session and all contained terminals

Transitions 1/2 and 3 are mutually exclusive per session NAME, via the lifecycle
lock in ``services/session_lock.py`` — see ``delete_session`` for why.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from cli_agent_orchestrator.backends.base import TerminalBackend
from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import (
    delete_terminals_by_ids,
    list_terminals_by_session,
    list_terminals_in_sessions,
)
from cli_agent_orchestrator.constants import SESSION_PREFIX
from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.models.terminal import Terminal
from cli_agent_orchestrator.plugins import (
    PluginRegistry,
    PostCreateSessionEvent,
    PostKillSessionEvent,
    PostKillTerminalEvent,
)
from cli_agent_orchestrator.services.plugin_dispatch import dispatch_plugin_event
from cli_agent_orchestrator.services.session_env import clear_session_env
from cli_agent_orchestrator.services.session_lock import session_lifecycle_lock
from cli_agent_orchestrator.services.terminal_service import create_terminal
from cli_agent_orchestrator.utils.agent_profiles import resolve_provider

logger = logging.getLogger(__name__)


async def create_session(
    provider: str | None,
    agent_profile: str,
    session_name: str | None = None,
    working_directory: str | None = None,
    allowed_tools: list[str] | None = None,
    registry: PluginRegistry | None = None,
    env_vars: dict[str, str] | None = None,
    engine: KiroEngine | str | None = None,
    initial_message: str | None = None,
    initial_message_orchestration_type: OrchestrationType | None = None,
    model: str | None = None,
    resume_session_id: str | None = None,
    group: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Terminal:
    """Create a new session by creating its initial terminal.

    ``env_vars`` are operator-forwarded env vars from ``cao launch --env``.
    They are persisted on the session record so every worker spawned later
    in the same session inherits them. See issue #248.

    When ``initial_message`` is provided, the initial terminal uses the
    existing deferred-init path so provider initialization and delivery can
    continue after the session response. Omitting it preserves the synchronous
    initialization behavior used by existing callers.
    On the deferred path, the ``post_create_session`` plugin event is dispatched
    before provider initialization and message delivery finish.

    ``group``/``metadata`` are the #432 discovery fields, set on the initial
    terminal at creation time (``group`` is also updatable later via
    ``PATCH /terminals/{id}/group``, ``metadata`` via the ``update_metadata``
    MCP tool).
    """
    if initial_message == "":
        raise ValueError("initial_message must not be empty")
    if initial_message is None and initial_message_orchestration_type is not None:
        raise ValueError("initial_message_orchestration_type requires initial_message")

    if provider is None:
        resolved_provider = resolve_provider(agent_profile, fallback_provider="kiro_cli")
    else:
        resolved_provider = provider

    terminal = await create_terminal(
        provider=resolved_provider,
        agent_profile=agent_profile,
        session_name=session_name,
        new_session=True,
        working_directory=working_directory,
        allowed_tools=allowed_tools,
        registry=registry,
        env_vars=env_vars,
        engine=engine,
        defer_init=initial_message is not None,
        initial_message=initial_message,
        initial_message_orchestration_type=initial_message_orchestration_type,
        model=model,
        resume_session_id=resume_session_id,
        group=group,
        metadata=metadata,
    )
    dispatch_plugin_event(
        registry,
        "post_create_session",
        PostCreateSessionEvent(
            session_id=terminal.session_name,
            session_name=terminal.session_name,
        ),
    )
    return terminal


def _terminals_grouped_by_session(session_names: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """Group the given sessions' terminals by tmux session name in ONE query.

    ``list_sessions`` used to reach ``list_terminals_by_session`` once per tmux
    session from inside ``_enrich_session_ownership`` (issue #629): a query per
    session on a read path that ``GET /sessions`` and the fleet snapshot both
    poll.

    Reads only the LIVE sessions' rows, not the whole table. That distinction is
    the point rather than an optimization detail: rows for sessions tmux no
    longer reports are never swept on a long-uptime server (see
    ``list_terminals_in_sessions``), so a whole-table read would make this path
    scale with accumulated dead rows instead of with the sessions being listed —
    slower than the per-session version it replaced once enough had piled up.

    ``list_terminals_in_sessions`` orders explicitly, so the order-sensitive
    "first known terminal" pick below is specified rather than dependent on
    whichever plan the query engine happens to choose.
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    try:
        terminals = list_terminals_in_sessions(session_names)
    except Exception:
        # Swallowed on purpose: a metadata read failure must degrade the
        # ownership fields to None rather than blank the whole session list,
        # which is what callers render. exc_info so a DB or schema problem is
        # diagnosable from the log instead of only by reproducing locally.
        logger.warning("Failed to load terminal metadata for session ownership", exc_info=True)
        return grouped

    # Every row came back matching one of session_names, so tmux_session is
    # populated by construction — no falsy-key guard needed here.
    for terminal in terminals:
        grouped.setdefault(terminal["tmux_session"], []).append(terminal)
    return grouped


def _enrich_session_ownership(
    backend: TerminalBackend,
    session_data: Dict[str, Any],
    terminals_by_session: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Add best-effort ownership metadata from the session's first known terminal.

    ``terminals_by_session`` is the pre-grouped result of one bulk terminal
    read (see ``_terminals_grouped_by_session``) rather than a per-session
    query, so enriching N sessions costs one query instead of N.
    """
    enriched = dict(session_data)
    enriched.setdefault("working_directory", None)
    enriched.setdefault("agent_profile", None)

    # `... or ""` (not `.get("id", "")`): an explicit id=None must collapse to
    # "" too, matching the sibling guard in list_sessions. `.get("id", "")`
    # would yield the truthy string "None" and try to enrich a bogus session.
    session_name = enriched.get("id") or ""
    if not session_name:
        return enriched

    terminals = terminals_by_session.get(session_name, [])

    ownership_terminal: Dict[str, Any] = {}
    for terminal in terminals:
        if terminal.get("agent_profile") or terminal.get("working_directory"):
            ownership_terminal = terminal
            break

    if not ownership_terminal:
        for terminal in terminals:
            if terminal.get("tmux_window"):
                ownership_terminal = terminal
                break

    if ownership_terminal:
        enriched["agent_profile"] = ownership_terminal.get("agent_profile")
        persisted_working_directory = ownership_terminal.get("working_directory")
        if persisted_working_directory:
            enriched["working_directory"] = persisted_working_directory
        elif ownership_terminal.get("tmux_window"):
            try:
                enriched["working_directory"] = backend.get_pane_working_directory(
                    session_name, ownership_terminal["tmux_window"]
                )
            except Exception:
                # Also swallowed on purpose — an unreadable pane cwd must not
                # drop the session from the listing. exc_info for the same
                # reason as the bulk read above.
                logger.warning(
                    "Failed to resolve working directory for %s", session_name, exc_info=True
                )

    return enriched


def list_sessions() -> List[Dict]:
    """List all sessions from tmux."""
    try:
        backend = get_backend()
        tmux_sessions = backend.list_sessions()
        cao_sessions = [
            s
            for s in tmux_sessions
            # Use .get() rather than s["id"]: a backend that returns a session
            # dict without an "id" key must not blank the entire list (KeyError
            # in this comprehension is swallowed by the outer except and returns
            # []). Shipped backends always populate "id"; this hardens against a
            # future backend that does not.
            if (s.get("id") or "").startswith(SESSION_PREFIX)
        ]
        # Filter BEFORE the terminal read: it is what bounds the read to live
        # CAO sessions, and it keeps a host running only non-CAO tmux sessions
        # at zero queries, as it was when the read was per-session and therefore
        # never reached.
        if not cao_sessions:
            return []
        terminals_by_session = _terminals_grouped_by_session([s["id"] for s in cao_sessions])
        return [_enrich_session_ownership(backend, s, terminals_by_session) for s in cao_sessions]
    except Exception:
        # Swallowed to keep a caller's listing from raising, so exc_info for the
        # same reason as the two handlers above -- more so, in fact: this one
        # blanks the ENTIRE response rather than one session's metadata, and it
        # is the net for anything the grouping/enrichment above throws.
        logger.error("Failed to list sessions", exc_info=True)
        return []


def get_session(session_name: str) -> Dict:
    """Get session with terminals, oldest first.

    ``terminals`` carries the same ordering contract as
    ``list_terminals_by_session`` (see it): index 0 is the session's oldest
    surviving terminal, normally its conductor. Stated here because this is the
    path that reaches it over HTTP via ``GET /sessions/{name}`` --
    ``examples/fleet/panel`` routes a user's message to ``terminals[0]``, and
    ``ops_mcp_server.get_session_info`` hands the array to an external
    supervisor that will read the first entry as the conductor.
    """
    try:
        if not get_backend().session_exists(session_name):
            raise ValueError(f"Session '{session_name}' not found")

        tmux_sessions = get_backend().list_sessions()
        session_data = next((s for s in tmux_sessions if s["id"] == session_name), None)

        if not session_data:
            raise ValueError(f"Session '{session_name}' not found")

        terminals = list_terminals_by_session(session_name)
        # Enrich each terminal with its live status. list_terminals_by_session
        # reads only the DB row (no status column), but callers monitoring an
        # orchestration — the web UI, and the cao-ops-mcp get_session_info tool
        # an external supervisor polls — need to distinguish
        # IDLE/PROCESSING/COMPLETED/ERROR per terminal. status_monitor is the
        # single source of truth and is backend-aware (tmux push vs herdr
        # native), so derive it here rather than persisting a stale column.
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        for terminal in terminals:
            terminal["status"] = status_monitor.get_status(terminal["id"]).value
        return {"session": session_data, "terminals": terminals}

    except Exception as e:
        logger.error(f"Failed to get session {session_name}: {e}")
        raise


def delete_session(session_name: str, registry: PluginRegistry | None = None) -> Dict:
    """Delete session and cleanup, reconciling tmux and the registry atomically.

    Two properties make the two stores impossible to diverge (#498):

    **Mutual exclusion.** The whole critical section — enumerate rows, capture
    scrollback, confirm tmux gone, dismantle runtimes, delete rows — runs under
    the per-session-name lifecycle lock (``services/session_lock.py``) — the SAME
    lock ``create_terminal`` holds while it creates a session/window and writes
    the matching row. Without it no ordering discipline helps: a create landing
    mid-teardown can put a live tmux session under this name after we have
    already decided the name is dead, and a second concurrent teardown can
    double-kill and double-sweep. Serializing per NAME leaves teardowns of
    DIFFERENT sessions fully concurrent.

    **Nothing is dismantled until the kill is confirmed.** There is therefore
    nothing to roll back, and no snapshot/restore machinery: an earlier revision
    deleted rows first and restored them from a snapshot on failure, which both
    reconstructed them lossily (``last_active`` was dropped) and restored ONLY
    the rows — the FIFO reader, status-monitor buffers and provider registration
    stayed torn down, so the "restored" terminal was a zombie, a row that looked
    live with no pipeline behind it. Ordering:

    1. Enumerate the incarnation's rows under the lock. Because the lock also
       covers creation, this list cannot grow behind us — a concurrent create is
       either fully included or has not started.
    2. Snapshot each terminal's scrollback/metadata
       (``capture_terminal_snapshot``). This must precede the kill (scrollback
       only exists while the pane does), and it is safe there because it is
       READ-ONLY with respect to terminal state — it reads tmux and writes two
       files under the log directory. If the kill then fails to confirm, no
       terminal has been touched.
    3. Check liveness STRICTLY: a lookup error must not be misread as "gone".
       ``session_exists`` collapses any error to False, which would drop the
       registry while the session may be alive.
    4. If the session is alive, ``kill_session`` — which per the backend
       contract returns True only once the session is CONFIRMED gone, and False
       for both a failed kill and an already-absent target. A False is
       disambiguated by a strict follow-up check: confirmed gone (it vanished
       between the check and the kill lookup) is SUCCESS; still provably alive
       is a real failure. Killing the SESSION kills every window in it, so no
       per-window kill is needed first — and doing it this way is what lets the
       destructive work all sit after the confirmation.
    5. Only now that tmux is provably gone: dismantle each terminal's runtime
       (``dismantle_terminal_runtime`` with ``kill_window=False`` — the windows
       died with the session), then delete the rows (scoped by id via
       ``delete_terminals_by_ids``, so a same-name session created after this
       teardown finishes keeps its own rows), and drop the forwarded-env mapping.
       Every step here is individually guarded: past the confirmation point the
       teardown has succeeded, so a failing step is reported in ``errors`` rather
       than raised — raising would both misreport a completed teardown and, since
       dispatch is deferred to 6, lose every event for it.
    6. Release the lock, THEN emit ``post_kill_terminal`` per torn-down terminal
       followed by ``post_kill_session``. No plugin code runs inside the critical
       section — see the dispatch loop for why that matters on the API path.

    A terminal whose runtime teardown DEFERS (``dismantle_terminal_runtime``
    returns False — Grok has not yet released its private home, #596) keeps its
    row: that row is the only retry handle for the deferred cleanup, so it is
    neither deleted in step 5 nor swept, the session is reported in ``errors``
    instead of ``deleted``, and a re-run finishes the job. The tmux session is
    still gone by then — the deferral is about on-disk provider state, not about
    the session — so this is a partially-complete teardown, which is exactly what
    the caller is told.

    If tmux cannot be confirmed dead we raise having changed nothing but two
    snapshot files: the surviving session keeps its rows, its FIFO readers, its
    status-monitor state and its providers, so it is still a fully working
    session rather than a half-dismantled one, and a re-run reconciles it.

    NOTE — how far the guarantee actually reaches. It rests on two backend
    properties, and is exactly as strong as the weaker one:

    * ``kill_session`` returning True only once the session is confirmed gone.
      TmuxBackend polls; HerdrBackend does not yet (it returns the close
      subprocess's exit code without a liveness check).
    * ``session_exists_strict`` distinguishing absence from an unanswerable
      lookup. TmuxBackend does, by classifying a ``list-sessions`` exit status
      itself (``clients/tmux.py``); the ABC default just delegates to the lenient
      ``session_exists``, which collapses any error to "absent" and so fails OPEN.

    On a backend with the default strict check, therefore, this teardown is no
    safer than the pre-fix behavior in the lookup-error case — a transient error
    still reads as "gone" and the rows still get dropped. The tmux path answers
    False only on positive evidence of absence (its docstring lists the residual
    holes it cannot close); herdr's does not, and it is not made worse here.
    Tracked as a follow-up.

    Returns:
        Dict with 'deleted' (list of deleted session names) and 'errors' (list of error dicts).
    """
    result: Dict = {"deleted": [], "errors": []}
    # Terminals whose row was actually dropped, with the metadata their
    # post_kill_terminal payload needs. Collected under the lock, dispatched
    # after it is released.
    torn_down: List[Tuple[str, Dict]] = []
    try:
        from cli_agent_orchestrator.services import terminal_service

        # Hold the lifecycle lock across the ENTIRE critical section. Nothing
        # inside re-acquires it: terminal_service's teardown halves and
        # plugin dispatch never touch session lifecycle, so there is no
        # self-deadlock path. Guaranteed released on every exit, exceptions
        # included (context manager).
        with session_lifecycle_lock(session_name):
            terminals = list_terminals_by_session(session_name)
            incarnation_ids = [t["id"] for t in terminals]

            # Step 2: read-only scrollback/metadata capture, which has to happen
            # while the panes still exist. ``metadata`` is kept because both
            # steps in phase 5 need it (the row-deletion half builds the
            # post_kill_terminal payload from it, after the row is gone).
            captured: List[Tuple[str, Dict | None]] = []
            for terminal in terminals:
                try:
                    metadata = terminal_service.capture_terminal_snapshot(terminal["id"])
                except Exception as e:
                    logger.warning(f"Failed to snapshot terminal {terminal['id']}: {e}")
                    metadata = None
                captured.append((terminal["id"], metadata))

            # Step 3/4: confirm the tmux session is gone BEFORE dismantling
            # anything. Any inability to confirm raises with the session fully
            # intact — never half-dismantled, never registry-less.
            backend = get_backend()
            try:
                session_still_alive = backend.session_exists_strict(session_name)
            except Exception as e:
                raise RuntimeError(
                    f"could not verify tmux session '{session_name}' liveness during "
                    f"teardown ({e}); registry left intact for reconciliation on re-run"
                ) from e

            if session_still_alive:
                killed = backend.kill_session(session_name)
                if not killed:
                    # False means "not found" OR "kill unconfirmed" (backend
                    # contract, backends/base.py). Only a still-alive session is
                    # a real failure.
                    try:
                        still_here = backend.session_exists_strict(session_name)
                    except Exception as e:
                        raise RuntimeError(
                            f"could not verify tmux session '{session_name}' liveness "
                            f"after kill_session ({e}); registry left intact for "
                            "reconciliation on re-run"
                        ) from e
                    if still_here:
                        raise RuntimeError(
                            f"tmux session '{session_name}' still exists after "
                            "kill_session; registry left intact for reconciliation "
                            "on re-run"
                        )

            # Step 5: tmux is provably gone — now, and only now, dismantle the
            # per-terminal runtime and drop the rows. kill_window=False: the
            # windows died with the session, so the tmux-facing steps would only
            # log spurious warnings.
            # ``registry=None``: the per-terminal events are dispatched together
            # with post_kill_session AFTER the lock is released — see below.
            cleanup_complete = True
            deferred_ids: List[str] = []
            # Terminals whose runtime was dismantled but whose FIRST row delete
            # raised: their rows are what the sweep below exists to remove, and
            # their per-terminal events ride on the sweep's outcome.
            row_delete_failed: List[Tuple[str, Dict]] = []
            for terminal_id, metadata in captured:
                try:
                    runtime_released = terminal_service.dismantle_terminal_runtime(
                        terminal_id, metadata, kill_window=False
                    )
                except Exception as e:
                    logger.warning(f"Failed to cleanup terminal {terminal_id}: {e}")
                    runtime_released = True
                if runtime_released is False:
                    # Grok has not yet released its private home (#596). The row
                    # is the only handle a retry has, so keep it: skip both the
                    # row delete and the sweep below for this id, and report the
                    # session as not fully deleted.
                    cleanup_complete = False
                    deferred_ids.append(terminal_id)
                    result["errors"].append(
                        {
                            "terminal_id": terminal_id,
                            "error": "cleanup deferred; retry delete_session",
                        }
                    )
                    continue
                try:
                    if terminal_service.delete_terminal_row(terminal_id, metadata, registry=None):
                        if metadata:
                            torn_down.append((terminal_id, metadata))
                except Exception as e:
                    logger.warning(f"Failed to delete registry row for {terminal_id}: {e}")
                    # The runtime IS dismantled (this is past the deferral
                    # check), so if the sweep below durably removes the row the
                    # terminal is torn down in every observable way and its
                    # post_kill_terminal is OWED — a re-run rebuilds its
                    # worklist from rows that no longer exist and can never
                    # re-emit it. Remember it; the sweep's success is what
                    # converts it into ``torn_down``.
                    if metadata:
                        row_delete_failed.append((terminal_id, metadata))

            # Both remaining steps are guarded exactly like the two above, and
            # for a sharper reason: by here the kill is CONFIRMED and every
            # runtime and row is already gone, so the teardown has SUCCEEDED and
            # is durable. Letting a raise out of this tail would report that
            # completed teardown as a total failure AND — because all plugin
            # dispatch is deferred past the lock — drop every event for it. The
            # per-terminal events would be unrecoverable, since a re-run rebuilds
            # ``torn_down`` from rows that no longer exist and can only re-emit
            # post_kill_session. Reachable, not theoretical: the sweep is a DB
            # write and the engine sets neither busy_timeout nor WAL, so
            # ``database is locked`` is an ordinary outcome under CAO's concurrent
            # writers. Nothing is swallowed — the failure is surfaced in
            # ``result["errors"]`` and the log, just not as an exception that also
            # destroys the event record.

            # Sweep any row the loop missed (e.g. a terminal whose row deletion
            # raised above), scoped to this incarnation's ids so a later
            # same-name session is never touched. Idempotent — a no-op when the
            # loop already cleared them. Deferred ids are excluded: their rows
            # are the retry handle for cleanup that has NOT happened yet.
            try:
                delete_terminals_by_ids([i for i in incarnation_ids if i not in deferred_ids])
            except Exception as e:
                logger.warning(f"Failed to sweep registry rows for {session_name}: {e}")
                result["errors"].append(
                    {
                        "session": session_name,
                        "step": "delete_terminals_by_ids",
                        "error": str(e),
                    }
                )
            else:
                # The sweep durably removed the rows whose first delete raised,
                # so those terminals are now torn down in every observable way
                # and owe their per-terminal event (fanhongy P2). Only on sweep
                # SUCCESS: if the sweep failed too, the surviving row is the
                # retry handle and the retried delete_session owns the event —
                # emitting now as well would double-fire on that retry.
                torn_down.extend(row_delete_failed)

            # Drop the per-session forwarded-env mapping (issue #248). Safe
            # even when no vars were forwarded — the helper is a no-op then.
            try:
                clear_session_env(session_name)
            except Exception as e:
                logger.warning(f"Failed to clear forwarded env for {session_name}: {e}")
                result["errors"].append(
                    {"session": session_name, "step": "clear_session_env", "error": str(e)}
                )

            if cleanup_complete:
                result["deleted"].append(session_name)
                logger.info(f"Deleted session: {session_name}")
            else:
                logger.warning(
                    "Session %s backend was removed but terminal cleanup is deferred", session_name
                )

        # ALL plugin dispatch runs OUTSIDE the lock — per-terminal events
        # included. Plugin code is third-party and unbounded, and on the API path
        # it does not merely get scheduled: ``delete_session`` runs under
        # ``asyncio.to_thread`` (api/main.py), so there is no running loop and
        # ``dispatch_plugin_event`` falls back to ``asyncio.run``, executing the
        # hook to completion inline. Dispatching from inside the critical section
        # would therefore let one slow or hanging plugin hold the lifecycle lock
        # and stall every subsequent create/teardown of this session name. Both
        # events describe work that is already complete and durable by here, so
        # emitting them after the release loses nothing.
        # Per terminal, isolated: the teardown is finished and durable by here, so
        # one unusable metadata dict (a missing ``tmux_session``, a validation
        # error) must not turn a completed delete into a failure, nor cost the
        # remaining terminals their event. ``dispatch_plugin_event`` already
        # isolates the hook itself; this covers building the event around it.
        for terminal_id, metadata in torn_down:
            try:
                dispatch_plugin_event(
                    registry,
                    "post_kill_terminal",
                    PostKillTerminalEvent(
                        session_id=metadata["tmux_session"],
                        terminal_id=terminal_id,
                        agent_name=metadata.get("agent_profile"),
                    ),
                )
            except Exception as e:
                logger.warning(f"Failed to emit post_kill_terminal for {terminal_id}: {e}")
        dispatch_plugin_event(
            registry,
            "post_kill_session",
            PostKillSessionEvent(session_id=session_name, session_name=session_name),
        )
        return result

    except Exception as e:
        logger.error(f"Failed to delete session {session_name}: {e}")
        raise
