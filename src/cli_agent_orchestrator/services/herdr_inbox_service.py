"""HerdrInboxService — socket event-based inbox delivery for herdr backend.

Replaces the pipe-pane + file watchdog approach with herdr's native socket API.
Subscribes to per-pane pane.agent_status_changed events (plus a broadcast
pane.updated for older herdr versions that still emit it) and delivers pending
inbox messages when a pane transitions to idle or done.

Design:
- Maintains a pane_id → terminal_id map for managed panes
- Subscribes to pane.agent_status_changed per managed pane, validated against a
  fresh snapshot: herdr ≥0.9 accepts a broadcast pane.updated subscription but
  never emits the event, and rejects (and then RESETS the connection on) any
  subscription naming a pane it does not know
- herdr allows exactly ONE events.subscribe per connection (a second resets the
  connection, measured on 0.7.5 and again on 0.9.5), so a pane registered after
  connect is picked up by an intentional reconnect, not an incremental call
- Reads the events.subscribe response: an error ack means the subscription is
  NOT active, and silently carrying on would leave delivery to the slow
  reconciliation sweep
- Reconnects with exponential backoff on socket disconnect
- Supplements with periodic pane read for kiro-cli (working >30s check)
"""

import asyncio
import json
import logging
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Set

logger = logging.getLogger(__name__)

# Exponential backoff parameters
_BACKOFF_BASE = 1.0  # seconds
_BACKOFF_MAX = 30.0  # seconds
_BACKOFF_MULTIPLIER = 2.0

# Kiro supplement check: how long in "working" before we check pane read
_KIRO_WORKING_THRESHOLD = 30.0  # seconds

# How long to wait for herdr's response to events.subscribe before treating the
# connection as broken. herdr answers a subscribe immediately (it is the first
# and only in-flight request on a fresh connection), so this only fires when
# the server is wedged.
_SUBSCRIBE_ACK_TIMEOUT = 10.0  # seconds

# Max event lines tolerated before the events.subscribe ack. Normally 0-1 (the
# ack is the first line); a larger run means a herdr replay bug or a hostile
# socket, so cap the buffer and reconnect rather than accumulate memory.
_PRE_ACK_BUFFER_CAP = 1000

# Pause before reconnecting when the pane set changed, so a burst of
# registrations (one session launching several workers) coalesces into one
# resubscribe cycle instead of one per pane.
_RESUBSCRIBE_COALESCE_DELAY = 0.25  # seconds

# After an idle/done event, re-check delivery a few times over this window.
# A provider can hold PROCESSING for a post-turn "flush wait" (10s cap from
# first detection; see providers/base.py), and that cap RESETS every time the
# agent flaps back to working — e.g. while it generates its reply — so the real
# settle can be tens of seconds after the first done event. pane.agent_status_
# changed fires ONCE per transition, so a single delivery attempt at the event
# instant finds the gate closed and nothing re-opens it until the 30s
# reconciliation sweep. (The retired broadcast pane.updated hid this by firing
# on every output change, which re-checked delivery for free.) These retries
# re-run the SAME readiness gate — they never bypass the flush wait — just often
# enough to catch it opening; deliver_pending is idempotent so a tick after the
# message already landed is a cheap no-op. One loop per terminal (see
# _arm_settle_retries): a later event does NOT stack a second loop, and if the
# window lapses while the agent is still flapping, the next done event re-arms.
_SETTLE_RETRY_INTERVAL = 3.0  # seconds between re-checks
# Cover ~27s past the event — long enough for a turn that flaps several times
# while generating a reply (measured live: a settle ~27s after task start) to
# land before the reconciliation sweep (INBOX_RECONCILE_INTERVAL + grace).
# Cheap because the loop stops at the first successful delivery (see
# _settle_retry_loop), so the full tick count only runs when nothing is
# deliverable, in which case there is nothing to spam into the pane.
_SETTLE_RETRY_TICKS = 9

# herdr socket protocol versions this integration has been verified against.
# Outside this range the event contract may have changed shape again (0.9.x
# silently stopped emitting broadcast pane.updated while still acking its
# subscription), so log loudly rather than fail.
_HERDR_TESTED_PROTOCOL_MIN = 17
_HERDR_TESTED_PROTOCOL_MAX = 22


class _ResubscribeRequested(Exception):
    """Internal control flow: the managed pane set changed, reconnect to refresh
    the per-pane subscription list. Never leaves _socket_loop."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _delete_ghost_terminal_row(terminal_id: str) -> None:
    """Release a deleted ghost without altering reconciler sidecar handling."""
    from cli_agent_orchestrator.clients.database import delete_terminal
    from cli_agent_orchestrator.services import ephemeral_service

    if delete_terminal(terminal_id, end_reason="pane_closed"):
        ephemeral_service.release(terminal_id, "terminal_gone")


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
        delivery_callback: Optional[Callable[[str, str], int]] = None,
        herdr_session: str = "cao",
    ) -> None:
        """Initialize the inbox service.

        Args:
            socket_path: Path to herdr socket. None = auto-detect from env.
            delivery_callback: Function to call for message delivery.
                Signature: callback(terminal_id, source) → int, the number of
                messages delivered; source names the trigger for log attribution.
                The count lets the settle-retry loop stop once a message lands.
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

        # Busy-time accounting (issue #1082): the status last recorded per
        # managed terminal. herdr repeats pane.updated for an unchanged
        # agent_status, so only a change is written.
        self._last_recorded_status: Dict[str, str] = {}  # terminal_id → status value

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

        # Resubscribe signalling: register_terminal runs on request-handler
        # threads while the socket loop owns the connection, so the wake-up is
        # an asyncio.Event set via call_soon_threadsafe on the loop captured in
        # start(). Both stay None until start() runs.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._resubscribe_requested: Optional[asyncio.Event] = None

        # Protocol observability: log herdr's version/protocol once per process.
        self._protocol_logged = False

        # At most one bounded settle-retry loop per terminal, to carry a
        # delivery across the provider's flush-wait window (see
        # _SETTLE_RETRY_INTERVAL / _SETTLE_RETRY_TICKS). Mutated only on the
        # event loop (_dispatch_event, _deliver_settled_panes, _close_connection),
        # never from the request threads that call register/unregister, so no
        # cross-thread task cancellation.
        self._settle_tasks: Dict[str, "asyncio.Task"] = {}

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
        # herdr permits one events.subscribe per connection, so a new pane needs
        # a reconnect to join the per-pane subscription set. Unregistration does
        # NOT resubscribe: a subscription for a closed pane just never fires
        # again, and events for a live-but-unmanaged pane fail the map lookup.
        self._request_resubscribe()

    def _request_resubscribe(self) -> None:
        """Wake the socket loop to rebuild its subscription set (thread-safe)."""
        loop = self._loop
        event = self._resubscribe_requested
        if loop is None or event is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(event.set)

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
        self._last_recorded_status.pop(terminal_id, None)
        logger.info(f"Unregistered terminal {terminal_id}")

    async def start(self) -> None:
        """Start the event loop: wait for first terminal, then connect and listen."""
        self._loop = asyncio.get_running_loop()
        self._resubscribe_requested = asyncio.Event()
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
                            _delete_ghost_terminal_row(terminal_id)
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
                    if teardown_terminal(terminal_id, end_reason="pane_closed") is False:
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
            self._last_recorded_status.pop(terminal_id, None)
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

                # Subscribe BEFORE reconciling. herdr 0.9.0 stopped replaying
                # retained history on subscribe, so every event emitted between
                # a snapshot and a later subscribe is lost permanently — a
                # dropped idle/done means _deliver() never fires for that
                # transition (#755). Replay is what used to close that window
                # on 0.8.x, which is why this was latent.
                #
                # Everything goes in ONE events.subscribe call: the per-pane
                # agent-status subscriptions, a broadcast pane.updated for
                # older herdr versions, plus the lifecycle events. herdr
                # resets the connection on a second events.subscribe (measured
                # on 0.7.5 and 0.9.5), so a pane set change recycles the
                # connection instead (_ResubscribeRequested below).
                await self._subscribe_all_events()

                # Reconcile map against live herdr state.
                await self._reconcile()

                # A subscription only reports TRANSITIONS. A pane that settled
                # into idle/done before the subscribe existed — during the
                # deferred-connect wait above, or across a reconnect — emits
                # nothing further, so the subscription alone can never deliver
                # for it. Close that with a snapshot-driven sweep. This also
                # covers a buffered event whose pane_id _reconcile just
                # renumbered, which would miss the post-reconcile map.
                await self._deliver_settled_panes()

                self._backoff = _BACKOFF_BASE  # Reset backoff after successful setup

                # Listen for events
                await self._event_loop()

            except _ResubscribeRequested:
                logger.info(
                    "Herdr pane set changed; reconnecting to refresh per-pane subscriptions"
                )
                # Intentional recycle: no backoff, but pause briefly so a burst
                # of registrations becomes one reconnect, not one per pane.
                await asyncio.sleep(_RESUBSCRIBE_COALESCE_DELAY)

            except (ConnectionError, OSError, asyncio.IncompleteReadError) as e:
                logger.warning(f"Herdr socket disconnected: {e}")

                # Exponential backoff
                logger.info(f"Reconnecting in {self._backoff}s...")
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * _BACKOFF_MULTIPLIER, _BACKOFF_MAX)

            finally:
                await self._close_connection()

    async def _close_connection(self) -> None:
        """Best-effort close of the current socket connection.

        The loop now recycles connections on every pane registration, so a
        leaked writer per cycle would accumulate real file descriptors rather
        than only one per rare network error.
        """
        # A recycle re-runs the settled-pane sweep, which re-arms any retry that
        # still matters, so in-flight ones from the old connection can be dropped.
        self._cancel_settle_retries()
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 — closing a broken socket must not raise
            logger.debug("Herdr socket close failed", exc_info=True)

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
        live_pane_ids = {p["pane_id"] for p in snapshot.get("panes") or [] if p.get("pane_id")}
        live_pane_by_terminal = {
            str(p["terminal_id"]): str(p["pane_id"])
            for p in snapshot.get("panes") or []
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
                            _delete_ghost_terminal_row(term["id"])
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
                self._last_recorded_status.pop(terminal_id, None)
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
            self._last_recorded_status.pop(terminal_id, None)

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
                _delete_ghost_terminal_row(terminal_id)
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

        # A re-map changed a managed pane_id (herdr renumbered a live pane). The
        # active subscription is keyed on the OLD pane_id, so without a recycle
        # that terminal would get no agent-status events until some other
        # terminal happened to register — the same silent fall-through to the
        # reconcile sweep this fix exists to prevent. Request a resubscribe so
        # the next cycle subscribes on the new pane_id.
        if remapped:
            self._request_resubscribe()

    async def _deliver_settled_panes(self) -> None:
        """Deliver to managed panes already settled in idle/done at subscribe time.

        The broadcast pane.updated subscription reports transitions only, so a
        pane that reached idle/done BEFORE the subscription existed never
        produces an event we can act on. That window is not just the old
        snapshot/subscribe gap (#755): _socket_loop defers connecting until the
        first terminal registers, and every reconnect reopens it too.

        Called once per successful connection, after _reconcile has refreshed
        _pane_to_terminal, so the pane_ids read here match the current map.

        Delivery is idempotent — _deliver routes to inbox_service.deliver_pending,
        which re-reads PENDING rows under a per-terminal lock and re-checks
        status — so a pane that ALSO emits a live event costs nothing.

        A failed snapshot is not an error: the reconciliation sweep remains the
        backstop, so log and move on rather than tearing down the connection.
        """
        snapshot = self._fetch_snapshot()
        if snapshot is None:
            logger.warning("Settled-pane sweep: no snapshot, skipping")
            return

        delivered = 0
        for pane in snapshot.get("panes") or []:
            if not isinstance(pane, dict):
                continue
            terminal_id = self._pane_to_terminal.get(pane.get("pane_id") or "")
            if not terminal_id:
                continue
            if pane.get("agent_status") in ("idle", "done"):
                self._working_since.pop(terminal_id, None)
                # A pane already settled at connect time has the same flush-wait
                # gap as a live event, and no event will follow, so carry it too
                # — but only if the immediate attempt didn't already land.
                if self._deliver(terminal_id, source="settled-sweep") == 0:
                    self._arm_settle_retries(terminal_id)
                delivered += 1
            self._note_activity(terminal_id, pane.get("agent_status"))
            # A transition missed while disconnected surfaces here as the
            # pane's current status (issue #1082 busy time).
            await self._record_agent_status(terminal_id, pane.get("agent_status"), _utcnow())

        if delivered:
            logger.info("Settled-pane sweep: triggered delivery for %d pane(s)", delivered)
        else:
            logger.debug("Settled-pane sweep: no settled managed panes")

    async def _record_agent_status(
        self, terminal_id: str, agent_status: object, observed_at: datetime
    ) -> None:
        """Persist a managed pane's agent_status change for busy time (issue #1082).

        herdr's own events are the source for herdr terminals: a transition is
        recorded when herdr reports it, not when some caller happens to poll
        ``get_status()``, which would leave a finished turn counted as busy
        until the next poll. Deduplicated per terminal. The entry moves before
        the write, so a failed best-effort write drops that one transition
        rather than retrying it. The write runs off the event loop, stamped
        with ``observed_at``.
        """
        from cli_agent_orchestrator.backends.herdr_backend import (
            HERDR_AGENT_STATUS_TO_TERMINAL_STATUS,
        )
        from cli_agent_orchestrator.clients.database import record_launch_status_event

        if not isinstance(agent_status, str):
            return
        status = HERDR_AGENT_STATUS_TO_TERMINAL_STATUS.get(agent_status)
        if status is None or self._last_recorded_status.get(terminal_id) == status.value:
            return
        self._last_recorded_status[terminal_id] = status.value
        try:
            await asyncio.to_thread(
                record_launch_status_event, terminal_id, status.value, observed_at
            )
        except Exception:
            logger.debug("Launch status event recording failed for %s", terminal_id, exc_info=True)

    async def _connect(self) -> None:
        """Connect to the herdr socket."""
        self._reader, self._writer = await asyncio.open_unix_connection(self._socket_path)
        logger.info(f"Connected to herdr socket: {self._socket_path}")

    async def _subscribe_all_events(self) -> None:
        """Subscribe to all events in a SINGLE events.subscribe call and read the ack.

        herdr resets the entire connection when it receives a second
        events.subscribe on a connection that already has an active
        subscription (measured on 0.7.5 and again on 0.9.5), so this must
        remain exactly one events.subscribe per connection; a pane set change
        is handled by recycling the connection (_ResubscribeRequested).

        The subscription set is:
        - pane.agent_status_changed PER MANAGED PANE. herdr ≥0.9 (protocol 22)
          accepts a broadcast pane.updated subscription but never emits the
          event, so the per-pane form is the only one that actually delivers
          agent-status transitions there. pane_id is required by the schema.
        - a broadcast pane.updated, kept for older herdr versions (≤0.8) that
          emit it with agent_status in the payload. Duplicate signals are
          harmless: _deliver is idempotent and _record_agent_status dedupes.
        - pane.closed and workspace.closed lifecycle events.

        Per-pane entries are filtered against a fresh snapshot because herdr
        answers a subscription naming an unknown pane with a pane_not_found
        error and then RESETS the connection — one stale map entry must not be
        able to poison every retry. A pane missing from the snapshot is left to
        _reconcile to prune; a pane registered after the snapshot sets the
        resubscribe event again, so the next cycle picks it up.

        Raises ConnectionError when the snapshot cannot be fetched (the sweep
        and reconcile paths need it anyway) or when herdr rejects the
        subscription, so _socket_loop retries with backoff.
        """
        event = self._resubscribe_requested
        if event is not None:
            # Clear BEFORE reading state: a registration landing after this
            # point re-sets the event and forces another cycle, so it can never
            # be silently absorbed into a subscription that predates it.
            event.clear()

        live_panes = self._list_live_pane_ids()
        if live_panes is None:
            raise ConnectionError("herdr snapshot unavailable; cannot build subscription set")

        subscriptions: list[dict] = [
            {"type": "pane.updated"},
            {"type": "pane.closed"},
            {"type": "workspace.closed"},
        ]
        skipped = 0
        for pane_id in sorted(self._pane_to_terminal):
            if pane_id not in live_panes:
                skipped += 1
                continue
            subscriptions.append({"type": "pane.agent_status_changed", "pane_id": pane_id})

        message = {
            "id": "sub_all",
            "method": "events.subscribe",
            "params": {"subscriptions": subscriptions},
        }
        await self._send(message)
        buffered = await self._await_subscribe_ack()
        logger.info(
            "Subscribed to %d per-pane agent-status event(s) + broadcast pane.updated "
            "+ lifecycle events in one events.subscribe call%s",
            len(subscriptions) - 3,
            f" ({skipped} stale pane(s) left to reconcile)" if skipped else "",
        )
        # Dispatch any event lines that arrived before the ack AFTER the ack read
        # has completed, so a slow DB write inside _dispatch_event (via
        # _record_agent_status) can never stall the ack-critical read path.
        # Isolate each dispatch: one failing event (e.g. a transient DB error)
        # must not drop the rest and tip the whole connection into reconnect —
        # the settled-pane sweep below is the backstop for anything missed here.
        for event in buffered:
            try:
                await self._dispatch_event(event)
            except Exception:  # noqa: BLE001 — a bad pre-ack event must not abort setup
                logger.warning("Failed to dispatch buffered pre-ack event", exc_info=True)

    def _list_live_pane_ids(self) -> Optional[Set[str]]:
        """Return the pane_ids herdr currently knows, or None when unavailable.

        This is an EXISTENCE read used to filter the per-pane subscription list:
        herdr rejects an events.subscribe naming an unknown pane with
        pane_not_found and then resets the connection, so one stale map entry
        must not poison every retry. It is deliberately NOT the coverage
        snapshot the #755 guard is about — a transition between this read and
        subscription activation is recovered by _deliver_settled_panes, which
        re-reads live state AFTER the subscribe ack.
        """
        snapshot = self._fetch_snapshot()
        if snapshot is None:
            return None
        self._log_herdr_protocol(snapshot)
        return {
            pane_id
            for pane in snapshot.get("panes") or []
            if isinstance(pane, dict)
            and isinstance(pane_id := pane.get("pane_id"), str)
            and pane_id
        }

    async def _await_subscribe_ack(self) -> list:
        """Read herdr's response to events.subscribe; fail loudly on an error ack.

        The old fire-and-forget subscribe could not tell an active subscription
        from a rejected one, which turns every later delivery into a silent
        fallback to the 30s reconciliation sweep. The ack is the first line
        herdr sends on a fresh connection; any event line that somehow precedes
        it is COLLECTED and returned for the caller to dispatch AFTER the ack
        read — dispatching inline here would run a DB write (_record_agent_status
        via asyncio.to_thread) on the ack-critical path, so a wedged DB could
        stall subscription setup past the timeout. Reading only parses lines.

        Raises ConnectionError on an error ack, EOF, or timeout, so
        _socket_loop's retry/backoff path owns recovery.
        """
        assert self._reader is not None
        buffered: list = []
        deadline = time.monotonic() + _SUBSCRIBE_ACK_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConnectionError("Timed out waiting for events.subscribe ack")
            try:
                line = await asyncio.wait_for(self._reader.readline(), timeout=remaining)
            except asyncio.TimeoutError:
                raise ConnectionError("Timed out waiting for events.subscribe ack") from None
            if not line:
                raise ConnectionError("Socket closed before events.subscribe ack")
            try:
                msg = json.loads(line.decode())
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("id") == "sub_all":
                error = msg.get("error")
                if error:
                    # repr(): the error can echo pane labels; keep log lines unforgeable.
                    raise ConnectionError(f"events.subscribe rejected: {error!r}")
                return buffered
            buffered.append(msg)
            # The ack is normally the first line, so this buffer holds 0-1
            # entries. A flood before the ack is abnormal (a herdr replay bug or
            # a hostile socket); cap it so it cannot grow without bound, and let
            # the reconnect path recover rather than accumulating memory.
            if len(buffered) > _PRE_ACK_BUFFER_CAP:
                raise ConnectionError(
                    f"events.subscribe ack not seen after {_PRE_ACK_BUFFER_CAP} events"
                )

    def _log_herdr_protocol(self, snapshot: dict) -> None:
        """Log herdr's version/protocol once; warn outside the tested range.

        This integration was written against one protocol and silently broken
        by another (0.9.x stopped emitting broadcast pane.updated while still
        acking its subscription), so an untested protocol is worth a WARNING
        even though the service keeps running.
        """
        if self._protocol_logged:
            return
        self._protocol_logged = True
        protocol = snapshot.get("protocol")
        version = snapshot.get("version")
        logger.info("herdr version %s, socket protocol %s", version, protocol)
        if isinstance(protocol, int) and not (
            _HERDR_TESTED_PROTOCOL_MIN <= protocol <= _HERDR_TESTED_PROTOCOL_MAX
        ):
            logger.warning(
                "herdr socket protocol %s is outside the tested range [%d, %d]; "
                "event-driven inbox delivery may silently degrade to the "
                "reconciliation sweep — verify a pane.agent_status_changed event "
                "arrives after an agent goes idle",
                protocol,
                _HERDR_TESTED_PROTOCOL_MIN,
                _HERDR_TESTED_PROTOCOL_MAX,
            )

    async def _event_loop(self) -> None:
        """Listen for events and dispatch delivery.

        Also watches the resubscribe event: herdr allows one events.subscribe
        per connection, so a pane registered after connect needs the connection
        recycled. A completed line is processed BEFORE honouring a concurrent
        resubscribe request, so a buffered idle/done is not dropped; anything
        lost to the cancelled read is covered by _deliver_settled_panes on the
        next connection.
        """
        assert self._reader is not None
        resub_event = self._resubscribe_requested
        resub_task: Optional[asyncio.Task] = (
            asyncio.create_task(resub_event.wait()) if resub_event is not None else None
        )
        try:
            while True:
                line_task = asyncio.create_task(self._reader.readline())
                wait_set = {line_task} if resub_task is None else {line_task, resub_task}
                done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)

                if line_task in done:
                    line = line_task.result()  # propagates read errors to _socket_loop
                    if not line:
                        raise ConnectionError("Socket closed")
                    try:
                        event = json.loads(line.decode())
                    except json.JSONDecodeError:
                        event = None
                    if isinstance(event, dict):
                        await self._dispatch_event(event)
                else:
                    line_task.cancel()
                    try:
                        await line_task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass

                if resub_task is not None and resub_task.done():
                    raise _ResubscribeRequested()
        finally:
            if resub_task is not None and not resub_task.done():
                resub_task.cancel()
                try:
                    await resub_task
                except asyncio.CancelledError:
                    pass

    async def _dispatch_event(self, event: dict) -> None:
        """Route one decoded herdr event to lifecycle cleanup or delivery."""
        # herdr identifies the event in the "event" key. Lifecycle events use
        # underscore names (pane_closed / workspace_closed); the agent-status
        # event uses the dotted name (pane.agent_status_changed). Normalize the
        # name so routing does not depend on the separator herdr happens to use.
        # (Older code read "type" and matched dotted lifecycle names, which never
        # matched herdr's real wire format — lifecycle cleanup silently never ran.)
        raw_event = event.get("event", "") or event.get("type", "")
        event_name = raw_event.replace("_", ".")
        # Read before any dispatch below, so a busy-time transition is
        # stamped when herdr reported it, not when its write lands.
        observed_at = _utcnow()

        # Handle lifecycle events
        if event_name in ("pane.closed", "workspace.closed"):
            self._handle_lifecycle_event(event_name, event.get("data", {}))
            return

        data = event.get("data", {})
        if not isinstance(data, dict):
            data = {}
        # Broadcast pane.updated nests the pane under data.pane; the per-pane
        # agent-status event uses top-level data. Fall back to data, and
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
            return

        if status in ("idle", "done"):
            # Clear working timestamp
            self._working_since.pop(terminal_id, None)
            # Trigger delivery now; only arm the flush-wait re-check if nothing
            # landed. This single event is the only agent-status signal herdr
            # sends for the transition, so if the provider isn't ready yet
            # (delivered 0) the retry loop carries it; if it already delivered,
            # arming would just fire one redundant attempt (matches the loop's
            # own > 0 stop rule).
            if self._deliver(terminal_id, source="herdr-event") == 0:
                self._arm_settle_retries(terminal_id)

        elif status == "working":
            # Track working start for kiro supplement check
            if terminal_id in self._kiro_terminals:
                if terminal_id not in self._working_since:
                    self._working_since[terminal_id] = time.time()

        self._note_activity(terminal_id, status)
        await self._record_agent_status(terminal_id, status, observed_at)

    @staticmethod
    def _note_activity(terminal_id: str, agent_status: object) -> None:
        """Tell the StatusMonitor that herdr saw the agent working or blocked.

        The delegation callback only forwards a terminal that has shown work
        on its last input (``StatusMonitor.started_on_last_input``). On herdr
        that evidence exists only here, in herdr's own agent_status reports.
        """
        if agent_status not in ("working", "blocked"):
            return
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        status_monitor.note_native_activity(terminal_id)

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
                    str(p.get("pane_id") or "") == str(pane_id) for p in snapshot.get("panes") or []
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
                self._last_recorded_status.pop(terminal_id, None)
                if _retain_deferred_failure_tombstone(
                    terminal_id, on_cleanup_deferred=self._pending_tombstone_runtime_cleanup.add
                ):
                    logger.info(
                        "pane.closed: retained DB-outage terminal %s after confirmed pane loss",
                        terminal_id,
                    )
                else:
                    try:
                        teardown_terminal(terminal_id, end_reason="pane_closed")
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
            self._last_recorded_status.pop(terminal_id, None)

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
                    if teardown_terminal(terminal_id, end_reason="pane_closed") is False:
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
    def _arm_settle_retries(self, terminal_id: str) -> None:
        """Ensure a bounded re-delivery loop is running for a terminal.

        Call on the event loop only. If a loop is already active for this
        terminal, do nothing — it is already covering the window, and stacking
        a second loop per event just multiplies redundant readiness checks
        (seen live: a turn that flaps working/done fires several done events).
        A later event that arrives AFTER the loop has lapsed re-arms it, so a
        settle beyond the window is still caught (or, failing that, by the 30s
        reconciliation backstop).
        """
        loop = self._loop
        if loop is None:
            return
        existing = self._settle_tasks.get(terminal_id)
        if existing is not None and not existing.done():
            return
        self._settle_tasks[terminal_id] = loop.create_task(self._settle_retry_loop(terminal_id))

    async def _settle_retry_loop(self, terminal_id: str) -> None:
        """Re-attempt delivery over the flush-wait window, then defer to reconcile.

        TODO: this is a tactical bridge between two layers that do not talk —
        herdr's agent-status events and the provider's output-flush readiness.
        The clean fix is a provider/StatusMonitor "delivery-ready" signal that
        herdr subscribes to, so no polling is needed; that is a provider-contract
        change tracked separately. Until then, re-check the existing gate.
        """
        try:
            for _ in range(_SETTLE_RETRY_TICKS):
                # Check before sleeping so a terminal unregistered during the
                # previous tick's sleep exits now instead of burning another
                # interval.
                if terminal_id not in self._terminal_to_pane:
                    return
                await asyncio.sleep(_SETTLE_RETRY_INTERVAL)
                if terminal_id not in self._terminal_to_pane:
                    return
                # Offload the delivery: _deliver → deliver_pending reads status
                # via the herdr CLI (blocking subprocess) and types into the
                # pane, so running it inline here would block the single event
                # loop for every other terminal on each tick. deliver_pending is
                # already driven off-loop this way by the reconciliation daemon
                # (asyncio.to_thread) and the inbox POST pool, and serializes per
                # terminal under its own lock, so a concurrent on-loop live-event
                # delivery cannot double-send.
                #
                # Stop as soon as a message lands: the gate has opened, so there
                # is no point re-checking. Anything still queued is delivered by
                # the normal event/sweep paths now that the terminal is ready.
                delivered = await asyncio.to_thread(self._deliver, terminal_id, "herdr-settle")
                if delivered > 0:
                    return
        except asyncio.CancelledError:
            # Propagates automatically after finally (Python 3.8+); re-raised
            # explicitly only to make the cancel path obvious to readers.
            raise
        finally:
            # Drop self from the registry so a later event can re-arm. Guard
            # against a replacement task having already taken the slot.
            if self._settle_tasks.get(terminal_id) is asyncio.current_task():
                self._settle_tasks.pop(terminal_id, None)

    def _cancel_settle_retries(self) -> None:
        """Cancel all in-flight settle retries (event loop only)."""
        for task in list(self._settle_tasks.values()):
            task.cancel()
        self._settle_tasks.clear()

    def _deliver(self, terminal_id: str, source: str = "herdr") -> int:
        """Check and deliver pending messages for a terminal; return the count.

        ``source`` names the trigger (herdr-event, herdr-settle, settled-sweep,
        kiro-supplement) so a delivery in the logs can be told apart from the
        reconciliation sweep's — the event path dying silently is exactly how
        this backend degraded unnoticed on herdr 0.9.x. The count lets the
        settle-retry loop stop once a message lands.
        """
        logger.debug("Delivery trigger (%s) for terminal %s", source, terminal_id)
        if self._delivery_callback:
            try:
                result = self._delivery_callback(terminal_id, source)
                return result if isinstance(result, int) else 0
            except Exception as e:
                logger.error(f"Delivery failed for terminal {terminal_id}: {e}")
        return 0

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
                self._deliver(terminal_id, source="kiro-supplement")
                # Reset the timer so we don't spam
                self._working_since[terminal_id] = now

    async def _send(self, message: dict) -> None:
        """Send a JSON message to the herdr socket."""
        assert self._writer is not None
        data = json.dumps(message).encode() + b"\n"
        self._writer.write(data)
        await self._writer.drain()
