"""HerdrInboxService — socket event-based inbox delivery for herdr backend.

Replaces the pipe-pane + file watchdog approach with herdr's native socket API.
Subscribes to a broadcast pane.updated event (whose payload carries
agent_status) and delivers pending inbox messages when a pane transitions to
idle or done.

Design:
- Maintains a pane_id → terminal_id map for managed panes
- Subscribes once to a broadcast pane.updated (no pane_id) covering all panes,
  so a newly registered pane's events already arrive — registration updates the
  map only and never re-subscribes or forces a reconnect
- Reconnects with exponential backoff on socket disconnect
- Supplements with periodic pane read for kiro-cli (working >30s check)
"""

import asyncio
import json
import logging
import re
import subprocess
import time
from typing import Callable, Dict, Optional, Set

logger = logging.getLogger(__name__)

# Exponential backoff parameters
_BACKOFF_BASE = 1.0  # seconds
_BACKOFF_MAX = 30.0  # seconds
_BACKOFF_MULTIPLIER = 2.0

# Kiro supplement check: how long in "working" before we check pane read
_KIRO_WORKING_THRESHOLD = 30.0  # seconds


def _retain_deferred_failure_tombstone(
    terminal_id: str,
    *,
    on_cleanup_deferred: Optional[Callable[[str], None]] = None,
) -> bool:
    """Dismantle a dead deferred-init runtime but retain its durable DB tombstone.

    Returns True when the terminal is externally owned (pending or failed) and
    therefore must NOT have its row deleted. Imports are local to avoid the
    terminal_service <-> herdr service module cycle.
    """

    from cli_agent_orchestrator.clients.database import (
        get_terminal_metadata,
        update_terminal_deferred_init_runtime_reclaimed,
    )
    from cli_agent_orchestrator.services.terminal_service import (
        _is_deferred_init_external_owner_active,
        capture_terminal_snapshot,
        dismantle_terminal_runtime,
        should_retain_deferred_failure_tombstone,
    )

    # A current-process external-owner worker can legitimately appear in a
    # stale startup/reconcile discovery window while its deferred initializer
    # is still running.  Its DB row has the same ownership bit as a crash-
    # stranded tombstone, so durable metadata alone cannot distinguish them.
    # The process-local active fence is authoritative for this one case:
    # retain the row, but do NOT dismantle the live provider/FIFO/worktree.
    # Once the init task settles the fence is cleared; a durable failure can
    # then be reclaimed by the normal retry/rediscovery path.
    if _is_deferred_init_external_owner_active(terminal_id):
        return True

    try:
        metadata = get_terminal_metadata(terminal_id)
        retain = should_retain_deferred_failure_tombstone(terminal_id, metadata)
    except Exception as exc:  # noqa: BLE001 — uncertain ownership must not delete evidence
        logger.warning(
            "Deferred-init retention check failed for terminal %s; retaining fail-closed: %s",
            terminal_id,
            exc,
        )
        # Unknown ownership cannot authorize resource destruction. In particular,
        # dismantling with metadata=None bypasses exact identity proof, while a
        # cached provider can still delete its private home without reading the
        # DB. Preserve all resources until a retry can establish runtime absence.
        if on_cleanup_deferred is not None:
            on_cleanup_deferred(terminal_id)
        return True
    if not retain:
        return False

    try:
        try:
            metadata = capture_terminal_snapshot(terminal_id) or metadata
        except Exception as snapshot_exc:  # noqa: BLE001 — cleanup can proceed from DB metadata
            logger.warning(
                "Deferred-init tombstone snapshot failed for terminal %s: %s",
                terminal_id,
                snapshot_exc,
            )
        complete = dismantle_terminal_runtime(terminal_id, metadata, kill_window=False)
        if complete:
            try:
                if not update_terminal_deferred_init_runtime_reclaimed(terminal_id, True):
                    complete = False
            except Exception as update_exc:  # noqa: BLE001 — retry bookkeeping later
                complete = False
                logger.warning(
                    "Deferred-init tombstone runtime reclaim marker failed for %s: %s",
                    terminal_id,
                    update_exc,
                )
        if not complete:
            if on_cleanup_deferred is not None:
                on_cleanup_deferred(terminal_id)
            logger.warning(
                "Deferred-init tombstone runtime cleanup deferred for terminal %s",
                terminal_id,
            )
    except Exception as exc:  # noqa: BLE001 — retention must survive cleanup failure
        if on_cleanup_deferred is not None:
            on_cleanup_deferred(terminal_id)
        logger.warning(
            "Deferred-init tombstone runtime cleanup failed for terminal %s: %s",
            terminal_id,
            exc,
        )
    return True


class HerdrInboxService:
    """Event-driven inbox delivery service using herdr socket API.

    Subscribes to agent status events for managed panes and delivers
    pending messages when agents become idle/done.
    """

    def __init__(
        self,
        socket_path: Optional[str] = None,
        delivery_callback: Optional[Callable[[str], None]] = None,
        herdr_session: str = "cao",
    ) -> None:
        """Initialize the inbox service.

        Args:
            socket_path: Path to herdr socket. None = auto-detect from env.
            delivery_callback: Function to call for message delivery.
                Signature: callback(terminal_id) → checks and delivers pending messages.
            herdr_session: Name of the herdr session to connect to. Used to
                derive the default socket path and prefix CLI calls.
        """
        self._herdr_session = herdr_session
        self._socket_path = socket_path or self._default_socket_path(herdr_session)
        self._delivery_callback = delivery_callback

        # Managed pane tracking
        self._pane_to_terminal: Dict[str, str] = {}  # pane_id → terminal_id
        self._terminal_to_pane: Dict[str, str] = {}  # terminal_id → pane_id

        # Kiro-specific tracking for supplement check
        self._kiro_terminals: Set[str] = set()  # terminal_ids using kiro-cli
        self._working_since: Dict[str, float] = {}  # terminal_id → timestamp

        # Workspace tracking for lifecycle events
        self._workspace_to_session: Dict[str, str] = {}  # workspace_id → session_name
        # A workspace-close cleanup whose DB read/teardown could not complete is
        # retried by the existing maintenance loop. This prevents one transient
        # SQLite outage from permanently stranding provider/FIFO state after the
        # herdr workspace itself is already gone.
        # Closed-workspace retries are keyed by the backend workspace identity,
        # not merely by the reusable CAO session label. This fences cleanup from
        # a replacement workspace that later reuses the same session name.
        self._pending_closed_sessions: Dict[str, str] = {}  # workspace_id -> session_name
        # Runtime dismantling can itself be retryable (e.g. a provider private
        # home still has an owner). Keep that retry independent from preserving
        # the durable tombstone row.
        self._pending_tombstone_runtime_cleanup: Set[str] = set()

        # Connection state
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._backoff = _BACKOFF_BASE

    @staticmethod
    def _default_socket_path(session_name: str = "cao") -> str:
        """Determine default herdr socket path for a named session.

        The default session (name ``"default"``) uses a flat path:
        ``~/.config/herdr/herdr.sock``.

        Named sessions use a sessions subdirectory:
        ``~/.config/herdr/sessions/<session_name>/herdr.sock``.

        Args:
            session_name: Herdr session name. Defaults to ``"cao"``.
        """
        import os
        from pathlib import Path

        # Check XDG_CONFIG_HOME first, fallback to ~/.config
        config_home = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        if session_name == "default":
            return f"{config_home}/herdr/herdr.sock"
        return f"{config_home}/herdr/sessions/{session_name}/herdr.sock"

    def register_terminal(self, terminal_id: str, pane_id: str, is_kiro: bool = False) -> None:
        """Register a terminal for event-based inbox delivery.

        Args:
            terminal_id: CAO terminal identifier
            pane_id: Current herdr compact pane_id
            is_kiro: Whether this terminal runs kiro-cli (enables supplement check)
        """
        self._pane_to_terminal[pane_id] = terminal_id
        self._terminal_to_pane[terminal_id] = pane_id
        if is_kiro:
            self._kiro_terminals.add(terminal_id)

        logger.info(f"Registered terminal {terminal_id} (pane={pane_id}, kiro={is_kiro})")

    def unregister_terminal(self, terminal_id: str) -> None:
        """Remove a terminal from managed set.

        Args:
            terminal_id: Terminal to unregister
        """
        pane_id = self._terminal_to_pane.pop(terminal_id, None)
        if pane_id:
            self._pane_to_terminal.pop(pane_id, None)
        self._kiro_terminals.discard(terminal_id)
        self._working_since.pop(terminal_id, None)
        logger.info(f"Unregistered terminal {terminal_id}")

    async def start(self) -> None:
        """Start the event loop: wait for first terminal, then connect and listen."""
        # Run DB cleanup before starting the socket loop so ghost records from
        # prior server runs are removed even when no terminals are registered yet.
        await self._startup_db_cleanup()
        kiro_task = asyncio.ensure_future(self._kiro_supplement_loop())
        try:
            await self._socket_loop()
        finally:
            kiro_task.cancel()

    async def _startup_db_cleanup(self) -> None:
        """Delete ghost DB terminals whose herdr tabs no longer exist.

        Runs once at server startup before any pane registrations.  Cannot
        rely on _pane_to_terminal (empty at startup) or _workspace_to_session
        (populated later by _reconcile).  Builds the workspace map directly
        from a herdr api snapshot.
        """
        from cli_agent_orchestrator.clients.database import (
            delete_terminal,
            list_all_terminals,
            list_terminals_by_session,
        )
        from cli_agent_orchestrator.services.session_lock import session_lifecycle_lock

        snapshot = self._fetch_snapshot()
        if snapshot is None:
            logger.debug("Startup DB cleanup: no snapshot, skipping")
            return

        # workspace_id -> label (= CAO session name). Skip malformed records.
        workspace_to_session = {
            ws["workspace_id"]: ws["label"]
            for ws in snapshot.get("workspaces", [])
            if ws.get("workspace_id") and ws.get("label")
        }

        deleted = 0
        visited_terminal_ids: Set[str] = set()
        # The startup snapshot is discovery only, just like _reconcile's first
        # snapshot. HerdrInboxService.start() is spawned as a background task
        # from FastAPI lifespan, so a request can create a replacement session
        # after this snapshot but before the DB read. Freeze that name with the
        # same lifecycle lock as creation, enumerate the CURRENT rows, then take
        # a fresh scoped snapshot before deleting anything.
        for session_name in set(workspace_to_session.values()):
            with session_lifecycle_lock(session_name):
                try:
                    db_terminals = list_terminals_by_session(session_name)
                except Exception as exc:  # noqa: BLE001 — maintenance retries elsewhere
                    logger.warning(
                        "Startup DB cleanup: could not list terminals for %s; "
                        "deferring ghost cleanup: %s",
                        session_name,
                        exc,
                    )
                    continue

                live_labels = self._fresh_live_tab_labels_for_session(session_name)
                if live_labels is None:
                    logger.warning(
                        "Startup DB cleanup: could not establish fresh tab liveness "
                        "for %s; deferring ghost cleanup",
                        session_name,
                    )
                    continue

                for term in db_terminals:
                    terminal_id = str(term["id"])
                    visited_terminal_ids.add(terminal_id)
                    window = term.get("tmux_window", "")
                    if window and window not in live_labels:
                        if _retain_deferred_failure_tombstone(
                            terminal_id,
                            on_cleanup_deferred=self._pending_tombstone_runtime_cleanup.add,
                        ):
                            logger.info(
                                "Startup DB cleanup: retaining deferred-init terminal %s "
                                "(%s:%s) as external-owner tombstone",
                                terminal_id,
                                session_name,
                                window,
                            )
                            continue
                        logger.info(
                            f"Startup DB cleanup: deleting ghost terminal {terminal_id} "
                            f"({session_name}:{window}) — absent from fresh Herdr state"
                        )
                        try:
                            delete_terminal(terminal_id)
                            deleted += 1
                        except Exception as e:
                            logger.warning(
                                f"Startup DB cleanup: failed to delete ghost terminal "
                                f"{terminal_id}: {e}"
                            )

        # A workspace may have vanished completely while cao-server was down,
        # in which case it is absent from both snapshot.workspaces and tabs and
        # the per-workspace loop above can never discover its retained rows.
        # Scan the remaining DB rows only for deferred-init retention; ordinary
        # historical rows keep the existing cleanup policy and are not deleted
        # here.  This makes restart recovery able to reclaim Kimi/Grok runtime
        # resources even when no Herdr workspace survives to identify them.
        try:
            all_terminals = list_all_terminals()
        except Exception as exc:  # noqa: BLE001 — startup remains resilient
            logger.warning("Startup DB cleanup: could not list all terminal rows: %s", exc)
            all_terminals = []
        for term in all_terminals:
            terminal_id = str(term["id"])
            if terminal_id in visited_terminal_ids:
                continue
            if _retain_deferred_failure_tombstone(
                terminal_id, on_cleanup_deferred=self._pending_tombstone_runtime_cleanup.add
            ):
                logger.info(
                    "Startup DB cleanup: retained absent-workspace deferred-init tombstone %s",
                    terminal_id,
                )

        if deleted:
            logger.info(f"Startup DB cleanup: removed {deleted} ghost terminal(s)")
        else:
            logger.debug("Startup DB cleanup: no ghost terminals found")

    async def _kiro_supplement_loop(self) -> None:
        """Periodically check kiro terminals stuck in working state."""
        while True:
            await asyncio.sleep(10.0)
            self._retry_pending_closed_sessions()
            self._retry_pending_tombstone_runtime_cleanup()
            self._rediscover_deferred_failure_tombstones()
            try:
                await self.check_kiro_supplements()
            except Exception:
                logger.debug("Kiro supplement check error", exc_info=True)

    def _cleanup_closed_session(self, session_name: str) -> bool:
        """Reconcile one already-closed herdr workspace against durable CAO rows.

        Returns False on any DB/cleanup failure so the maintenance loop retries.
        Retained deferred-init tombstones count as successfully reconciled: their
        runtime is dismantled by the retention helper and their row deliberately
        remains for the external observer.
        """

        from cli_agent_orchestrator.clients.database import list_terminals_by_session
        from cli_agent_orchestrator.services.session_lock import session_lifecycle_lock
        from cli_agent_orchestrator.services.terminal_service import (
            delete_terminal as teardown_terminal,
        )

        # Creation uses this exact per-session lock. Freeze the DB worklist
        # BEFORE consulting Herdr liveness while holding the lock; a replacement
        # session cannot publish a new row between those two reads and therefore
        # can never enter the old workspace's cleanup set.
        with session_lifecycle_lock(session_name):
            try:
                terminals = list_terminals_by_session(session_name)
            except Exception as exc:  # noqa: BLE001 — retry when SQLite recovers
                logger.warning(
                    "workspace.closed: could not list terminals for %s; deferring cleanup: %s",
                    session_name,
                    exc,
                )
                return False

            # Session labels are reusable.  Fence every destructive action with
            # the incarnation-unique tab label. Unknown Herdr liveness is never
            # evidence of death: defer rather than risking a live replacement.
            live_labels = self._live_tab_labels()
            if live_labels is None:
                logger.warning(
                    "workspace.closed: could not read live tab labels for %s; deferring cleanup",
                    session_name,
                )
                return False

            complete = True
            terminal_ids = {str(terminal["id"]) for terminal in terminals}
            for terminal in terminals:
                terminal_id = str(terminal["id"])
                window_name = str(terminal.get("tmux_window") or "")
                if window_name and window_name in live_labels:
                    terminal_ids.discard(terminal_id)
                    continue
                if _retain_deferred_failure_tombstone(
                    terminal_id, on_cleanup_deferred=self._pending_tombstone_runtime_cleanup.add
                ):
                    logger.info(
                        "workspace.closed: retaining terminal %s as deferred-init "
                        "external-owner tombstone",
                        terminal_id,
                    )
                    continue
                try:
                    if teardown_terminal(terminal_id) is False:
                        complete = False
                        logger.warning(
                            "workspace.closed: cleanup deferred for terminal %s", terminal_id
                        )
                except Exception as exc:  # noqa: BLE001 — retry the session later
                    complete = False
                    logger.warning(
                        "workspace.closed: failed to cleanup terminal %s: %s", terminal_id, exc
                    )

        # The workspace is already gone; remove in-memory pane mappings for the
        # rows we were able to enumerate. Durable tombstones do not need a live
        # pane mapping and Bridge observes them through the terminal API.
        for terminal_id in terminal_ids:
            pane_id = self._terminal_to_pane.pop(terminal_id, None)
            if pane_id:
                self._pane_to_terminal.pop(pane_id, None)
            self._kiro_terminals.discard(terminal_id)
            self._working_since.pop(terminal_id, None)
        return complete

    def _retry_pending_closed_sessions(self) -> None:
        for workspace_id, session_name in list(self._pending_closed_sessions.items()):
            if self._cleanup_closed_session(session_name):
                self._pending_closed_sessions.pop(workspace_id, None)

    def _retry_pending_tombstone_runtime_cleanup(self) -> None:
        """Retry runtime dismantling for retained tombstones until it really completes."""

        for terminal_id in list(self._pending_tombstone_runtime_cleanup):
            deferred = False

            def _mark_deferred(_terminal_id: str) -> None:
                nonlocal deferred
                deferred = True

            retained = _retain_deferred_failure_tombstone(
                terminal_id, on_cleanup_deferred=_mark_deferred
            )
            if not retained or not deferred:
                self._pending_tombstone_runtime_cleanup.discard(terminal_id)

    def _rediscover_deferred_failure_tombstones(self) -> bool:
        """Re-scan retained external-owner rows for durable failures.

        Startup recovery can persist an interrupted-init failure AFTER Herdr's
        one-time startup cleanup if SQLite was temporarily unavailable. Querying
        the small external-owner cohort on each maintenance tick lets Herdr find
        that newly durable tombstone even when its workspace vanished entirely.

        A merely-pending current-process deferred init is never dismantled: it
        has no durable failure yet, and the process-local active fence is checked
        as an additional guard. Returns False only when the DB scan itself was
        incomplete; the next maintenance tick retries automatically.
        """

        from cli_agent_orchestrator.clients.database import (
            get_terminal_metadata,
            list_pending_deferred_init_external_owner_terminal_ids,
        )
        from cli_agent_orchestrator.services.terminal_service import (
            _is_deferred_init_external_owner_active,
            get_deferred_init_failure,
        )

        try:
            terminal_ids = list_pending_deferred_init_external_owner_terminal_ids()
        except Exception as exc:  # noqa: BLE001 — maintenance retries next tick
            logger.warning(
                "Deferred-init tombstone rediscovery could not list retained rows: %s",
                exc,
            )
            return False

        complete_scan = True
        for terminal_id in terminal_ids:
            if _is_deferred_init_external_owner_active(terminal_id):
                continue
            try:
                metadata = get_terminal_metadata(terminal_id)
            except Exception as exc:  # noqa: BLE001 — retry this row next tick
                complete_scan = False
                logger.warning(
                    "Deferred-init tombstone rediscovery could not inspect %s: %s",
                    terminal_id,
                    exc,
                )
                continue
            if not metadata or metadata.get("deferred_init_runtime_reclaimed"):
                continue
            try:
                failure = get_deferred_init_failure(
                    terminal_id, metadata.get("deferred_init_failure")
                )
            except Exception as exc:  # noqa: BLE001 — sidecar/DB read is uncertain
                complete_scan = False
                logger.warning(
                    "Deferred-init tombstone rediscovery could not read failure for %s: %s",
                    terminal_id,
                    exc,
                )
                continue
            if failure is None:
                continue
            _retain_deferred_failure_tombstone(
                terminal_id,
                on_cleanup_deferred=self._pending_tombstone_runtime_cleanup.add,
            )
        return complete_scan

    async def _socket_loop(self) -> None:
        """Connect to herdr socket and listen for events with reconnect.

        Defers connection until at least one terminal is registered. This avoids
        the disconnect/reconnect churn caused by herdr closing idle connections
        that have no active subscriptions.
        """
        while True:
            # Wait until there is at least one pane to subscribe to
            while not self._pane_to_terminal:
                await asyncio.sleep(0.5)

            try:
                await self._connect()

                # Reconcile map against live herdr state before subscribing
                await self._reconcile()

                # Subscribe to everything in ONE events.subscribe call: a single
                # broadcast pane.updated (no pane_id) plus the lifecycle events.
                # herdr resets the connection on a second events.subscribe, so
                # this must be a single combined call.
                await self._subscribe_all_events()

                self._backoff = _BACKOFF_BASE  # Reset backoff after successful setup

                # Listen for events
                await self._event_loop()

            except (ConnectionError, OSError, asyncio.IncompleteReadError) as e:
                logger.warning(f"Herdr socket disconnected: {e}")

                # Exponential backoff
                logger.info(f"Reconnecting in {self._backoff}s...")
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * _BACKOFF_MULTIPLIER, _BACKOFF_MAX)

    def _fetch_snapshot(self) -> Optional[dict]:
        """Return herdr's full live session snapshot in one socket call.

        `herdr api snapshot` returns result.snapshot with panes[]/tabs[]/
        workspaces[]. Each pane carries pane_id, terminal_id, agent_status,
        tab_id, workspace_id; each tab carries tab_id, label, workspace_id;
        each workspace carries workspace_id, label. Replaces the former
        pane-list + workspace-list + tab-list subprocess fan-out.

        Returns None on any failure (non-zero exit, timeout, missing binary,
        or malformed output). This is the single entry point all snapshot reads
        route through, so it swallows the same broad error set as the file's
        other herdr-subprocess helpers rather than letting one bad response
        kill the socket loop.
        """
        try:
            result = subprocess.run(
                ["herdr", "--session", self._herdr_session, "api", "snapshot"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                # repr() the stderr: it can echo user-controlled labels/args and
                # may contain newlines/control chars that would otherwise forge
                # log lines. Matches the escaping used elsewhere in the codebase.
                logger.warning("Snapshot: `api snapshot` failed: %r", result.stderr)
                return None
            snapshot = json.loads(result.stdout)["result"]["snapshot"]
            if not isinstance(snapshot, dict):
                logger.warning("Snapshot: result.snapshot is not a dict; ignoring")
                return None
            return snapshot
        except (
            subprocess.SubprocessError,
            OSError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
        ) as e:
            logger.warning(f"Snapshot: failed to fetch/parse: {e}")
            return None

    async def _reconcile(self) -> None:
        """Reconcile _pane_to_terminal map against live herdr state.

        Prunes stale pane entries, deletes orphaned DB terminal records,
        and kills workspaces with zero live terminals.
        """
        from cli_agent_orchestrator.backends.registry import get_backend
        from cli_agent_orchestrator.clients.database import (
            delete_terminal,
            get_terminal_metadata,
            list_terminals_by_session,
        )
        from cli_agent_orchestrator.services.session_lock import session_lifecycle_lock

        # One socket call replaces the former pane-list + workspace-list +
        # tab-list subprocess fan-out. All three data structures below are derived
        # from this single snapshot.
        snapshot = self._fetch_snapshot()
        if snapshot is None:
            logger.warning("Reconcile: no snapshot, skipping")
            return

        # Live pane_ids (from snapshot.panes).
        live_pane_ids = {p["pane_id"] for p in snapshot.get("panes", []) if p.get("pane_id")}
        live_pane_by_terminal = {
            str(p["terminal_id"]): str(p["pane_id"])
            for p in snapshot.get("panes", [])
            if p.get("terminal_id") and p.get("pane_id")
        }

        # workspace_id -> label (= CAO session name), from snapshot.workspaces.
        # Skip malformed records (missing id/label) rather than letting a
        # KeyError escape _reconcile and kill the socket loop — matches the
        # defensive .get() style used in the tabs loop below.
        self._workspace_to_session = {
            ws["workspace_id"]: ws["label"]
            for ws in snapshot.get("workspaces", [])
            if ws.get("workspace_id") and ws.get("label")
        }

        # DB cross-check: find terminals in DB whose tab no longer exists in herdr.
        # This catches ghost records from previous server runs where _pane_to_terminal
        # starts empty (so the stale-pane diff below produces nothing).
        #
        # The first snapshot is DISCOVERY ONLY. Session/window labels are
        # reusable: a replacement can be created after that snapshot and before
        # the DB read. Freeze creation with the same per-session lifecycle lock,
        # enumerate the current DB worklist, and then take a fresh scoped Herdr
        # snapshot before any destructive action. Unknown fresh liveness always
        # defers instead of turning stale evidence into a delete.
        processed_sessions: Set[str] = set()
        for session_name in self._workspace_to_session.values():
            if session_name in processed_sessions:
                continue
            processed_sessions.add(session_name)
            with session_lifecycle_lock(session_name):
                try:
                    db_terminals = list_terminals_by_session(session_name)
                except Exception as exc:  # noqa: BLE001 — stale-pane pass can still recover
                    logger.warning(
                        "Reconcile: could not list DB terminals for %s: %s",
                        session_name,
                        exc,
                    )
                    continue

                live_labels = self._fresh_live_tab_labels_for_session(session_name)
                if live_labels is None:
                    logger.warning(
                        "Reconcile: could not establish fresh tab liveness for %s; "
                        "deferring DB ghost cleanup",
                        session_name,
                    )
                    continue

                for term in db_terminals:
                    window = term.get("tmux_window", "")
                    if window and window not in live_labels:
                        if _retain_deferred_failure_tombstone(
                            term["id"],
                            on_cleanup_deferred=self._pending_tombstone_runtime_cleanup.add,
                        ):
                            logger.info(
                                "Reconcile: retaining deferred-init terminal %s "
                                "(%s:%s) as external-owner tombstone",
                                term["id"],
                                session_name,
                                window,
                            )
                            continue
                        logger.info(
                            f"Reconcile: deleting ghost terminal {term['id']} "
                            f"({session_name}:{window}) — absent from fresh Herdr state"
                        )
                        try:
                            delete_terminal(term["id"])
                        except Exception as e:
                            logger.warning(
                                f"Reconcile: failed to delete ghost terminal {term['id']}: {e}"
                            )

        # Find stale panes: stored pane_id no longer in herdr's live pane list.
        #
        # A stale pane_id does NOT mean the terminal is dead. herdr renumbers
        # compact pane_ids when a sibling tab in the workspace closes, so a
        # still-running terminal's stored pane_id can fall out of the live list
        # while its tab is very much alive. Identity must come from the durable
        # tab label (tmux_window), never the ephemeral pane_id.
        stale_pane_ids = set(self._pane_to_terminal.keys()) - live_pane_ids
        if not stale_pane_ids:
            logger.debug("Reconcile: all panes live, nothing to prune")
            return

        # Live workspace labels, used to gate workspace teardown below: never
        # kill a workspace whose label is still present in herdr.
        live_workspace_labels = set(self._workspace_to_session.values())

        # Sessions that genuinely lost a terminal (deleted, not re-mapped).
        affected_sessions: Set[str] = set()
        remapped = 0
        deleted = 0

        for pane_id in stale_pane_ids:
            terminal_id = self._pane_to_terminal.get(pane_id)
            if not terminal_id:
                self._pane_to_terminal.pop(pane_id, None)
                continue

            # Session/window identity before any mutation.
            try:
                meta = get_terminal_metadata(terminal_id)
            except Exception as exc:  # noqa: BLE001 — use herdr identity when DB is unavailable
                # A compact pane id may have been renumbered while the terminal
                # itself is still live. Herdr's snapshot carries terminal_id, so
                # remap directly without the DB/window label when possible.
                new_pane_id = live_pane_by_terminal.get(str(terminal_id))
                if new_pane_id:
                    self._pane_to_terminal.pop(pane_id, None)
                    self._pane_to_terminal[new_pane_id] = terminal_id
                    self._terminal_to_pane[terminal_id] = new_pane_id
                    logger.info(
                        "Reconcile: DB unavailable but terminal %s is live; re-mapped %s -> %s",
                        terminal_id,
                        pane_id,
                        new_pane_id,
                    )
                    remapped += 1
                    continue

                logger.warning(
                    "Reconcile: metadata unavailable for stale terminal %s; "
                    "retaining evidence and dismantling dead runtime: %s",
                    terminal_id,
                    exc,
                )
                self._pane_to_terminal.pop(pane_id, None)
                self._terminal_to_pane.pop(terminal_id, None)
                self._kiro_terminals.discard(terminal_id)
                self._working_since.pop(terminal_id, None)
                _retain_deferred_failure_tombstone(
                    terminal_id, on_cleanup_deferred=self._pending_tombstone_runtime_cleanup.add
                )
                continue
            term_session: Optional[str] = meta["tmux_session"] if meta else None
            term_window: Optional[str] = meta["tmux_window"] if meta else None

            # Re-map renumbered-but-live panes instead of deleting. A live tab
            # label means the pane_id was renumbered, not closed: re-resolve the
            # current pane_id and update both maps. Only when re-resolution fails
            # do we fall through to the delete path.
            term_live = self._label_still_live(term_window) if term_window else False
            if term_live is None:
                logger.warning(
                    "Reconcile: could not establish liveness for tab %s / terminal %s; "
                    "deferring stale-pane cleanup",
                    term_window,
                    terminal_id,
                )
                continue
            if term_live:
                assert term_window is not None
                try:
                    # Invalidate pane cache so get_pane_id does a fresh label-based
                    # lookup instead of returning the stale pane_id we just proved
                    # is no longer live. See PR #309 review comment.
                    backend = get_backend()
                    if hasattr(backend, "_pane_cache"):
                        backend._pane_cache.pop(terminal_id, None)
                    new_pane_id = backend.get_pane_id(terminal_id, term_session or "", term_window)
                except Exception as e:
                    logger.warning(
                        "Reconcile: tab %s live but pane re-resolve failed for %s (%s); "
                        "deleting",
                        term_window,
                        terminal_id,
                        e,
                    )
                else:
                    self._pane_to_terminal.pop(pane_id, None)
                    self._pane_to_terminal[new_pane_id] = terminal_id
                    self._terminal_to_pane[terminal_id] = new_pane_id
                    logger.info(
                        "Reconcile: re-mapped %s %s -> %s (pane renumbered, tab still live)",
                        terminal_id,
                        pane_id,
                        new_pane_id,
                    )
                    remapped += 1
                    continue

            # Tab label genuinely gone (or re-resolve failed): prune maps and
            # delete the orphaned DB record.
            self._pane_to_terminal.pop(pane_id, None)
            self._terminal_to_pane.pop(terminal_id, None)
            self._kiro_terminals.discard(terminal_id)
            self._working_since.pop(terminal_id, None)

            if _retain_deferred_failure_tombstone(
                terminal_id, on_cleanup_deferred=self._pending_tombstone_runtime_cleanup.add
            ):
                logger.info(
                    "Reconcile: retaining stale deferred-init terminal %s as external-owner tombstone",
                    terminal_id,
                )
                if term_session:
                    affected_sessions.add(term_session)
                continue

            try:
                delete_terminal(terminal_id)
                deleted += 1
            except Exception as e:
                logger.warning(f"Reconcile: failed to delete terminal {terminal_id}: {e}")

            if term_session:
                affected_sessions.add(term_session)

        # Kill a workspace only when its label is gone from herdr AND no managed
        # terminal remains for the session. A live label means the workspace is
        # alive and its panes were merely renumbered — killing it would tear down
        # working agents.
        if affected_sessions:
            remaining_by_session: Dict[str, int] = {s: 0 for s in affected_sessions}
            for tid in self._terminal_to_pane:
                try:
                    meta = get_terminal_metadata(tid)
                except Exception:
                    continue
                if meta and meta["tmux_session"] in remaining_by_session:
                    remaining_by_session[meta["tmux_session"]] += 1

            for session_name, remaining in remaining_by_session.items():
                if remaining == 0 and session_name not in live_workspace_labels:
                    try:
                        get_backend().kill_session(session_name)
                        logger.info(f"Reconcile: killed empty workspace {session_name}")
                    except Exception as e:
                        logger.warning(f"Reconcile: failed to kill workspace {session_name}: {e}")

        logger.info(
            "Reconcile: %d stale pane(s) — %d re-mapped, %d deleted",
            len(stale_pane_ids),
            remapped,
            deleted,
        )

    async def _connect(self) -> None:
        """Connect to the herdr socket."""
        self._reader, self._writer = await asyncio.open_unix_connection(self._socket_path)
        logger.info(f"Connected to herdr socket: {self._socket_path}")

    async def _subscribe_all_events(self) -> None:
        """Subscribe to all events in a SINGLE events.subscribe call.

        herdr (0.7.5) resets the entire connection when it receives a second
        events.subscribe on a connection that already has an active
        subscription, so this must remain exactly one events.subscribe per
        connection.

        The subscription is a broadcast pane.updated (sent with NO pane_id):
        herdr streams it for every pane and its payload carries agent_status,
        so a single broadcast subscription replaces the former per-pane
        pane.agent_status_changed subscriptions. This is independent of
        _pane_to_terminal — no per-pane enumeration is needed. The pane.closed
        and workspace.closed lifecycle events are sent in the same call.
        """
        subscriptions = [
            {"type": "pane.updated"},
            {"type": "pane.closed"},
            {"type": "workspace.closed"},
        ]
        message = {
            "id": "sub_all",
            "method": "events.subscribe",
            "params": {"subscriptions": subscriptions},
        }
        await self._send(message)
        logger.info(
            "Subscribed to broadcast pane.updated + lifecycle events "
            "in one events.subscribe call"
        )

    async def _event_loop(self) -> None:
        """Listen for events and dispatch delivery."""
        assert self._reader is not None
        while True:
            line = await self._reader.readline()
            if not line:
                raise ConnectionError("Socket closed")

            try:
                event = json.loads(line.decode())
            except json.JSONDecodeError:
                continue

            # herdr identifies the event in the "event" key. Lifecycle events use
            # underscore names (pane_closed / workspace_closed); the agent-status
            # event uses the dotted name (pane.agent_status_changed). Normalize the
            # name so routing does not depend on the separator herdr happens to use.
            # (Older code read "type" and matched dotted lifecycle names, which never
            # matched herdr's real wire format — lifecycle cleanup silently never ran.)
            raw_event = event.get("event", "") or event.get("type", "")
            event_name = raw_event.replace("_", ".")

            # Handle lifecycle events
            if event_name in ("pane.closed", "workspace.closed"):
                self._handle_lifecycle_event(event_name, event.get("data", {}))
                continue

            data = event.get("data", {})
            # Broadcast pane.updated nests the pane under data.pane; retired
            # agent-status events used top-level data. Fall back to data, and
            # guard against a null/non-dict pane so one malformed event cannot
            # escape the loop and kill delivery.
            pane_obj = data.get("pane") or data
            if not isinstance(pane_obj, dict):
                pane_obj = {}
            pane_id = pane_obj.get("pane_id", "")
            status = pane_obj.get("agent_status", "")

            # Only process events for managed panes
            terminal_id = self._pane_to_terminal.get(pane_id)
            if not terminal_id:
                continue

            if status in ("idle", "done"):
                # Clear working timestamp
                self._working_since.pop(terminal_id, None)
                # Trigger delivery
                self._deliver(terminal_id)

            elif status == "working":
                # Track working start for kiro supplement check
                if terminal_id in self._kiro_terminals:
                    if terminal_id not in self._working_since:
                        self._working_since[terminal_id] = time.time()

    def _label_still_live(self, window_name: str) -> Optional[bool]:
        """Return tab liveness, or None when Herdr cannot establish it.

        Used to disambiguate herdr's reused compact pane_ids on replayed
        pane_closed events. The tab label is unique per incarnation, so a live
        label means the close event refers to an older incarnation and is stale.

        Unknown liveness must fail closed: replayed compact pane ids can point at
        a replacement live terminal, so callers may only destroy on an explicit
        False result.
        """
        live_labels = self._live_tab_labels()
        if live_labels is None:
            return None
        return window_name in live_labels

    def _live_tab_labels(self) -> Optional[Set[str]]:
        """Return current Herdr tab labels, or None when liveness is unknown."""

        try:
            result = subprocess.run(
                ["herdr", "--session", self._herdr_session, "tab", "list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.warning(
                    "_live_tab_labels: herdr tab list failed (rc=%s): %s",
                    result.returncode,
                    result.stderr.strip(),
                )
                return None
            tab_data = json.loads(result.stdout)
            tabs = tab_data.get("result", {}).get("tabs", [])
            return {str(tab.get("label") or "") for tab in tabs if tab.get("label")}
        except (subprocess.SubprocessError, json.JSONDecodeError, KeyError, OSError) as e:
            logger.warning("_live_tab_labels: could not query herdr (%s)", e)
            return None

    def _fresh_live_tab_labels_for_session(self, session_name: str) -> Optional[Set[str]]:
        """Return fresh tab labels for one CAO session, or None if identity is unreadable.

        Session labels are reusable, so destructive reconciliation cannot reuse
        labels captured by an older snapshot. This helper takes a fresh Herdr
        snapshot and scopes tabs through the workspace ids whose CURRENT label
        matches the session name. Any malformed snapshot structure fails closed.
        """

        snapshot = self._fetch_snapshot()
        if snapshot is None:
            return None
        workspaces = snapshot.get("workspaces")
        tabs = snapshot.get("tabs")
        if not isinstance(workspaces, list) or not isinstance(tabs, list):
            return None

        workspace_ids: Set[str] = set()
        for workspace in workspaces:
            if not isinstance(workspace, dict):
                return None
            if workspace.get("label") != session_name:
                continue
            workspace_id = workspace.get("workspace_id")
            if not isinstance(workspace_id, str) or not workspace_id:
                return None
            workspace_ids.add(workspace_id)

        if not workspace_ids:
            return set()

        labels: Set[str] = set()
        for tab in tabs:
            if not isinstance(tab, dict):
                return None
            if tab.get("workspace_id") not in workspace_ids:
                continue
            label = tab.get("label")
            if not isinstance(label, str) or not label:
                return None
            labels.add(label)
        return labels

    def _resolve_session_from_herdr(self, workspace_id: str) -> Optional[str]:
        """Resolve a workspace_id to its session name from live herdr state.

        Used by workspace.closed handling when the in-memory _workspace_to_session
        map (populated only by _reconcile) does not contain the closed
        workspace_id. Queries herdr workspace list, refreshes the whole map from
        the result, and returns the label for workspace_id if found.

        Returns None when herdr cannot be queried or the workspace_id is not in
        the live list, so the caller can treat the event as unresolvable and take
        no destructive action.
        """
        try:
            result = subprocess.run(
                ["herdr", "--session", self._herdr_session, "workspace", "list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.warning(
                    "_resolve_session_from_herdr: herdr workspace list failed (rc=%s): %s",
                    result.returncode,
                    result.stderr.strip(),
                )
                return None
            ws_data = json.loads(result.stdout)
            workspaces = ws_data.get("result", {}).get("workspaces", [])
            self._workspace_to_session = {ws["workspace_id"]: ws["label"] for ws in workspaces}
            return self._workspace_to_session.get(workspace_id)
        except (subprocess.SubprocessError, json.JSONDecodeError, KeyError, OSError) as e:
            logger.warning("_resolve_session_from_herdr: could not query herdr (%s)", e)
            return None

    def _handle_lifecycle_event(self, event_type: str, data: dict) -> None:
        """Handle pane.closed and workspace.closed events."""
        from cli_agent_orchestrator.backends.registry import get_backend
        from cli_agent_orchestrator.clients.database import (
            get_terminal_metadata,
            list_terminals_by_session,
        )
        from cli_agent_orchestrator.services.terminal_service import (
            delete_terminal as teardown_terminal,
        )

        if event_type == "pane.closed":
            pane_id = data.get("pane_id", "")
            terminal_id = self._pane_to_terminal.get(pane_id)
            if not terminal_id:
                return

            # Get session before cleanup
            try:
                meta = get_terminal_metadata(terminal_id)
            except Exception as exc:  # noqa: BLE001 — do not let one SQLite outage lose cleanup
                # Without DB metadata we cannot use the durable window label to
                # reject replayed compact pane ids. Ask herdr directly: if this
                # pane id is still live, treat the close as stale/uncertain and
                # leave the runtime untouched. If the snapshot confirms it is
                # gone, the retention helper safely dismantles non-DB runtime
                # state while preserving the row fail-closed.
                snapshot = self._fetch_snapshot()
                if snapshot is None:
                    logger.warning(
                        "pane.closed: metadata unavailable for %s and herdr snapshot failed; "
                        "deferring lifecycle handling: %s",
                        terminal_id,
                        exc,
                    )
                    return
                if any(
                    str(p.get("pane_id") or "") == str(pane_id) for p in snapshot.get("panes", [])
                ):
                    logger.info(
                        "pane.closed: ignoring close for %s while pane %s is still live "
                        "and DB metadata is unavailable",
                        terminal_id,
                        pane_id,
                    )
                    return

                self._pane_to_terminal.pop(pane_id, None)
                self._terminal_to_pane.pop(terminal_id, None)
                self._kiro_terminals.discard(terminal_id)
                self._working_since.pop(terminal_id, None)
                if _retain_deferred_failure_tombstone(
                    terminal_id, on_cleanup_deferred=self._pending_tombstone_runtime_cleanup.add
                ):
                    logger.info(
                        "pane.closed: retained DB-outage terminal %s after confirmed pane loss",
                        terminal_id,
                    )
                else:
                    try:
                        teardown_terminal(terminal_id)
                    except Exception as cleanup_exc:  # noqa: BLE001 — lifecycle is best-effort
                        logger.warning(
                            "pane.closed: DB-outage cleanup failed for terminal %s: %s",
                            terminal_id,
                            cleanup_exc,
                        )
                return
            session_name = meta["tmux_session"] if meta else None

            # Guard against herdr's compact pane_id reuse + event replay.
            #
            # herdr (0.6.8) reuses compact pane_ids when a tab is killed and a
            # new tab takes the same index, AND replays the ENTIRE pane_closed
            # history on every fresh events.subscribe (e.g. after a reconnect on
            # socket disconnect). So a replayed close for an OLD incarnation of
            # this pane_id arrives mapped to the terminal that now occupies the
            # reused index — deleting a live terminal.
            #
            # The tab label (tmux_window) is unique per incarnation, so confirm
            # the label is genuinely gone from herdr before deleting. If the
            # label is still live, this close is stale (replayed) — ignore it.
            # If herdr can't be queried, fall toward delete: never leave a
            # terminal we think is open when it may actually be closed.
            window_name = meta["tmux_window"] if meta else None
            window_live = self._label_still_live(window_name) if window_name else False
            if window_live is None:
                logger.warning(
                    "pane.closed: liveness unknown for %s (pane=%s, label=%s); "
                    "deferring destructive cleanup",
                    terminal_id,
                    pane_id,
                    window_name,
                )
                return
            if window_live:
                logger.info(
                    "pane.closed: ignoring stale close for %s (pane=%s) — "
                    "label %s still live in herdr (compact pane_id reused)",
                    terminal_id,
                    pane_id,
                    window_name,
                )
                return

            # Remove from maps
            self._pane_to_terminal.pop(pane_id, None)
            self._terminal_to_pane.pop(terminal_id, None)
            self._kiro_terminals.discard(terminal_id)
            self._working_since.pop(terminal_id, None)

            # Creation-time external ownership protects the interval BEFORE a
            # failure marker is persisted; the durable failure protects it
            # afterwards. In either case dismantle runtime resources now that
            # the pane is gone, but retain the row for the external observer.
            retained_tombstone = _retain_deferred_failure_tombstone(
                terminal_id, on_cleanup_deferred=self._pending_tombstone_runtime_cleanup.add
            )
            if retained_tombstone:
                logger.info(
                    "pane.closed: retaining terminal %s as deferred-init external-owner tombstone",
                    terminal_id,
                )
            else:
                # Route pane lifecycle through the normal teardown rather than a
                # direct DB delete, so a Grok private home can return explicit
                # deferred cleanup and retain its terminal row for retry.
                try:
                    if teardown_terminal(terminal_id) is False:
                        logger.warning("pane.closed: cleanup deferred for terminal %s", terminal_id)
                except Exception as e:
                    logger.warning(f"pane.closed: failed to delete terminal {terminal_id}: {e}")

            logger.info(f"pane.closed: cleaned up terminal {terminal_id} (pane={pane_id})")

            # If session has no more terminals in our map, kill workspace
            remaining_in_session = [
                t
                for t in self._pane_to_terminal.values()
                if (m := get_terminal_metadata(t)) and m.get("tmux_session") == session_name
            ]
            if session_name and not remaining_in_session:
                try:
                    get_backend().kill_session(session_name)
                    logger.info(f"pane.closed: killed empty workspace {session_name}")
                except Exception as e:
                    logger.warning(f"pane.closed: failed to kill workspace {session_name}: {e}")

        elif event_type == "workspace.closed":
            workspace_id = data.get("workspace_id", "")
            session_name = self._workspace_to_session.get(workspace_id)
            if not session_name:
                # The in-memory map is populated only by _reconcile(); a workspace
                # that closed before any reconcile cached it would otherwise be a
                # silent no-op, leaking the session's terminals as orphan rows.
                # Resolve the session identity from live herdr state instead of
                # trusting the map. Only treat the event as unresolvable after the
                # live query also fails to identify a session.
                session_name = self._resolve_session_from_herdr(workspace_id)
                if not session_name:
                    return

            if not self._cleanup_closed_session(session_name):
                self._pending_closed_sessions[workspace_id] = session_name
            else:
                self._pending_closed_sessions.pop(workspace_id, None)

            self._workspace_to_session.pop(workspace_id, None)
            logger.info(
                "workspace.closed: reconciled session %s%s",
                session_name,
                " (retry pending)" if workspace_id in self._pending_closed_sessions else "",
            )

    # TODO: _deliver() calls callback synchronously — if callback is async,
    # this will need a threadsafe bridge (out of scope for this change).
    def _deliver(self, terminal_id: str) -> None:
        """Check and deliver pending messages for a terminal."""
        if self._delivery_callback:
            try:
                self._delivery_callback(terminal_id)
            except Exception as e:
                logger.error(f"Delivery failed for terminal {terminal_id}: {e}")

    async def check_kiro_supplements(self) -> None:
        """Periodic check for kiro-cli terminals stuck in 'working' state.

        For terminals in 'working' for >30s, read pane content and check
        for permission prompt patterns.
        """
        import subprocess

        now = time.time()
        for terminal_id in list(self._working_since.keys()):
            if terminal_id not in self._kiro_terminals:
                continue

            working_duration = now - self._working_since[terminal_id]
            if working_duration < _KIRO_WORKING_THRESHOLD:
                continue

            # Read pane and check for permission prompt
            pane_id = self._terminal_to_pane.get(terminal_id)
            if not pane_id:
                continue

            result = subprocess.run(
                ["herdr", "--session", self._herdr_session, "pane", "read", pane_id],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                continue

            # Check for kiro permission prompt pattern
            # (WAITING_USER_ANSWER indicator)
            from cli_agent_orchestrator.providers.kiro_cli import TUI_PERMISSION_PATTERN

            if re.search(TUI_PERMISSION_PATTERN, result.stdout):
                logger.info(
                    f"Kiro permission prompt detected for {terminal_id} "
                    f"(working for {working_duration:.0f}s)"
                )
                self._deliver(terminal_id)
                # Reset the timer so we don't spam
                self._working_since[terminal_id] = now

    async def _send(self, message: dict) -> None:
        """Send a JSON message to the herdr socket."""
        assert self._writer is not None
        data = json.dumps(message).encode() + b"\n"
        self._writer.write(data)
        await self._writer.drain()
