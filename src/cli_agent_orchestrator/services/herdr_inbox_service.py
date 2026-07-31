"""HerdrInboxService — socket event-based inbox delivery for herdr backend.

Replaces the pipe-pane + file watchdog approach with herdr's native socket API.
Subscribes to pane.agent_status_changed events and delivers pending inbox
messages when a pane transitions to idle or done.

Design:
- Maintains a pane_id → terminal_id map for managed panes
- Subscribes per-pane (wildcard support is unverified; see design.md)
- Reconnects with exponential backoff on socket disconnect
- Supplements with periodic pane read for kiro-cli (working >30s check)
"""

import asyncio
import json
import logging
import re
import subprocess
import threading
import time
from enum import Enum
from typing import Callable, Dict, Optional, Set

logger = logging.getLogger(__name__)

# Exponential backoff parameters
_BACKOFF_BASE = 1.0  # seconds
_BACKOFF_MAX = 30.0  # seconds
_BACKOFF_MULTIPLIER = 2.0

# Kiro supplement check: how long in "working" before we check pane read
_KIRO_WORKING_THRESHOLD = 30.0  # seconds


class PaneLiveness(str, Enum):
    """Authoritative liveness observation for a reused Herdr pane label."""

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


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
        self._ownership_lock = threading.RLock()

        # Workspace tracking for lifecycle events
        self._workspace_to_session: Dict[str, str] = {}  # workspace_id → session_name

        # Connection state
        self._connected = False
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._backoff = _BACKOFF_BASE
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lifecycle_tasks: Set[asyncio.Task] = set()
        # Delivery callbacks may block on native inbox I/O. Keep their work out
        # of the socket loop, but retain one loop-owned task and one latest
        # trailing pane snapshot per terminal to preserve delivery ordering.
        self._delivery_tasks: Dict[str, asyncio.Task] = {}
        self._delivery_reruns: Dict[str, str] = {}
        self._delivery_shutdown = False

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
        with self._ownership_lock:
            replaced_terminal = self._pane_to_terminal.get(pane_id)
            if (
                replaced_terminal
                and replaced_terminal != terminal_id
                and self._terminal_to_pane.get(replaced_terminal) == pane_id
            ):
                self._terminal_to_pane.pop(replaced_terminal, None)
                self._kiro_terminals.discard(replaced_terminal)
                self._working_since.pop(replaced_terminal, None)
            previous_pane = self._terminal_to_pane.get(terminal_id)
            if (
                previous_pane
                and previous_pane != pane_id
                and self._pane_to_terminal.get(previous_pane) == terminal_id
            ):
                self._pane_to_terminal.pop(previous_pane, None)
            self._pane_to_terminal[pane_id] = terminal_id
            self._terminal_to_pane[terminal_id] = pane_id
            if is_kiro:
                self._kiro_terminals.add(terminal_id)
            else:
                self._kiro_terminals.discard(terminal_id)

        logger.info(f"Registered terminal {terminal_id} (pane={pane_id}, kiro={is_kiro})")

        # Start streaming events for the new pane by forcing a reconnect.
        #
        # herdr (0.6.8) resets the entire connection when it receives a SECOND
        # events.subscribe on a connection that already has an active
        # subscription, and it exposes no incremental "add subscription" API.
        # So we cannot subscribe the new pane on the live connection — instead we
        # close the socket, and _socket_loop reconnects and rebuilds the single
        # combined subscription (all panes + lifecycle) in one call.
        #
        # register_terminal() may be called from a synchronous/non-event-loop
        # thread, so we schedule the reconnect onto the captured loop via
        # run_coroutine_threadsafe instead of create_task.
        if self._connected and self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._force_reconnect(), self._loop)

    def unregister_terminal(
        self,
        terminal_id: str,
        *,
        expected_pane_id: str | None = None,
    ) -> bool:
        """Compare-and-remove one terminal from the managed ownership maps.

        Args:
            terminal_id: Terminal to unregister
            expected_pane_id: When supplied, refuse if the terminal has moved
                to a different compact pane incarnation.
        """
        with self._ownership_lock:
            pane_id = self._terminal_to_pane.get(terminal_id)
            if expected_pane_id is not None and pane_id != expected_pane_id:
                return False
            self._terminal_to_pane.pop(terminal_id, None)
            if pane_id and self._pane_to_terminal.get(pane_id) == terminal_id:
                self._pane_to_terminal.pop(pane_id, None)
            self._kiro_terminals.discard(terminal_id)
            self._working_since.pop(terminal_id, None)
        logger.info(f"Unregistered terminal {terminal_id}")
        return True

    def _ownership_items(self) -> tuple[tuple[str, str], ...]:
        with self._ownership_lock:
            return tuple(self._pane_to_terminal.items())

    def _terminal_for_pane(self, pane_id: str) -> str | None:
        with self._ownership_lock:
            return self._pane_to_terminal.get(pane_id)

    async def start(self) -> None:
        """Start the event loop: wait for first terminal, then connect and listen."""
        self._loop = asyncio.get_running_loop()
        # Run DB cleanup before starting the socket loop so ghost records from
        # prior server runs are removed even when no terminals are registered yet.
        await self._startup_db_cleanup()
        kiro_task = asyncio.ensure_future(self._kiro_supplement_loop())
        try:
            await self._socket_loop()
        finally:
            kiro_task.cancel()
            await self._shutdown_delivery_tasks()
            if self._lifecycle_tasks:
                await asyncio.gather(*tuple(self._lifecycle_tasks), return_exceptions=True)

    async def _startup_db_cleanup(self) -> None:
        """Delete ghost DB terminals whose herdr tabs no longer exist.

        Runs once at server startup before any pane registrations.  Cannot
        rely on _pane_to_terminal (empty at startup) or _workspace_to_session
        (populated later by _reconcile).  Builds the workspace map directly
        from herdr workspace list.
        """
        from cli_agent_orchestrator.clients.database import list_terminals_by_session
        from cli_agent_orchestrator.services import terminal_service

        ws_result = subprocess.run(
            ["herdr", "--session", self._herdr_session, "workspace", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if ws_result.returncode != 0:
            logger.debug("Startup DB cleanup: herdr workspace list failed, skipping")
            return

        try:
            ws_data = json.loads(ws_result.stdout)
            workspaces = ws_data.get("result", {}).get("workspaces", [])
            workspace_to_session = {ws["workspace_id"]: ws["label"] for ws in workspaces}
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Startup DB cleanup: failed to parse workspace list: {e}")
            return

        tab_result = subprocess.run(
            ["herdr", "--session", self._herdr_session, "tab", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if tab_result.returncode != 0:
            logger.debug("Startup DB cleanup: herdr tab list failed, skipping")
            return

        try:
            tab_data = json.loads(tab_result.stdout)
            tabs = tab_data.get("result", {}).get("tabs", [])
            live_tabs_by_workspace: Dict[str, set] = {}
            for tab in tabs:
                ws_id = tab.get("workspace_id", "")
                label = tab.get("label", "")
                if ws_id and label:
                    live_tabs_by_workspace.setdefault(ws_id, set()).add(label)
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Startup DB cleanup: failed to parse tab list: {e}")
            return

        deleted = 0
        for ws_id, session_name in workspace_to_session.items():
            live_labels = live_tabs_by_workspace.get(ws_id, set())
            db_terminals = list_terminals_by_session(session_name)
            for term in db_terminals:
                window = term.get("tmux_window", "")
                if window and window not in live_labels:
                    logger.info(
                        f"Startup DB cleanup: deleting ghost terminal {term['id']} "
                        f"({session_name}:{window}) — tab not in herdr"
                    )
                    try:
                        if terminal_service.retire_observed_terminal(
                            term["id"], expected_session=session_name
                        ):
                            deleted += 1
                    except Exception as e:
                        logger.warning(
                            f"Startup DB cleanup: failed to delete ghost terminal "
                            f"{term['id']}: {e}"
                        )

        if deleted:
            logger.info(f"Startup DB cleanup: removed {deleted} ghost terminal(s)")
        else:
            logger.debug("Startup DB cleanup: no ghost terminals found")

    async def _kiro_supplement_loop(self) -> None:
        """Periodically check kiro terminals stuck in working state."""
        while True:
            await asyncio.sleep(10.0)
            try:
                await self.check_kiro_supplements()
            except Exception:
                logger.debug("Kiro supplement check error", exc_info=True)

    async def _socket_loop(self) -> None:
        """Connect to herdr socket and listen for events with reconnect.

        Defers connection until at least one terminal is registered. This avoids
        the disconnect/reconnect churn caused by herdr closing idle connections
        that have no active subscriptions.
        """
        while True:
            # Wait until there is at least one pane to subscribe to
            while True:
                with self._ownership_lock:
                    has_terminals = bool(self._pane_to_terminal)
                if has_terminals:
                    break
                await asyncio.sleep(0.5)

            try:
                await self._connect()
                self._connected = True

                # Reconcile map against live herdr state before subscribing
                await self._reconcile()

                # Subscribe to everything in ONE events.subscribe call: every
                # managed pane's agent-status plus the lifecycle events. herdr
                # resets the connection on a second events.subscribe, so this
                # must be a single combined call.
                await self._subscribe_all_events()

                self._backoff = _BACKOFF_BASE  # Reset backoff after successful setup

                # Listen for events
                await self._event_loop()

            except (ConnectionError, OSError, asyncio.IncompleteReadError) as e:
                logger.warning(f"Herdr socket disconnected: {e}")
                self._connected = False

                # Exponential backoff
                logger.info(f"Reconnecting in {self._backoff}s...")
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * _BACKOFF_MULTIPLIER, _BACKOFF_MAX)

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

        # Get live panes from herdr
        result = subprocess.run(
            ["herdr", "--session", self._herdr_session, "pane", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.warning(f"Reconcile: herdr pane list failed: {result.stderr}")
            return

        try:
            data = json.loads(result.stdout)
            panes = data.get("result", {}).get("panes", [])
            live_pane_ids = {p["pane_id"] for p in panes}
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Reconcile: failed to parse pane list: {e}")
            return

        # Build workspace_id -> session_name mapping
        ws_result = subprocess.run(
            ["herdr", "--session", self._herdr_session, "workspace", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if ws_result.returncode == 0:
            try:
                ws_data = json.loads(ws_result.stdout)
                workspaces = ws_data.get("result", {}).get("workspaces", [])
                self._workspace_to_session = {ws["workspace_id"]: ws["label"] for ws in workspaces}
            except (json.JSONDecodeError, KeyError):
                pass

        # DB cross-check: find terminals in DB whose tab no longer exists in herdr.
        # This catches ghost records from previous server runs where _pane_to_terminal
        # starts empty (so the stale-pane diff below produces nothing).
        tab_result = subprocess.run(
            ["herdr", "--session", self._herdr_session, "tab", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if tab_result.returncode == 0:
            try:
                tab_data = json.loads(tab_result.stdout)
                tabs = tab_data.get("result", {}).get("tabs", [])
                # Build: workspace_id -> set of live tab labels
                live_tabs_by_workspace: Dict[str, set] = {}
                for tab in tabs:
                    ws_id = tab.get("workspace_id", "")
                    label = tab.get("label", "")
                    if ws_id and label:
                        live_tabs_by_workspace.setdefault(ws_id, set()).add(label)

                from cli_agent_orchestrator.clients.database import list_terminals_by_session
                from cli_agent_orchestrator.services import terminal_service

                for ws_id, session_name in self._workspace_to_session.items():
                    live_labels = live_tabs_by_workspace.get(ws_id, set())
                    db_terminals = list_terminals_by_session(session_name)
                    for term in db_terminals:
                        window = term.get("tmux_window", "")
                        if window and window not in live_labels:
                            logger.info(
                                f"Reconcile: deleting ghost terminal {term['id']} "
                                f"({session_name}:{window}) — tab not in herdr"
                            )
                            try:
                                terminal_service.retire_observed_terminal(
                                    term["id"], expected_session=session_name
                                )
                            except Exception as e:
                                logger.warning(
                                    f"Reconcile: failed to delete ghost terminal "
                                    f"{term['id']}: {e}"
                                )
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Reconcile: failed to parse tab list: {e}")

        # Find stale panes: stored pane_id no longer in herdr's live pane list.
        #
        # A stale pane_id does NOT mean the terminal is dead. herdr renumbers
        # compact pane_ids when a sibling tab in the workspace closes, so a
        # still-running terminal's stored pane_id can fall out of the live list
        # while its tab is very much alive. Identity must come from the durable
        # tab label (tmux_window), never the ephemeral pane_id.
        with self._ownership_lock:
            stale_pane_ids = set(self._pane_to_terminal) - live_pane_ids
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
            terminal_id = self._terminal_for_pane(pane_id)
            if not terminal_id:
                continue

            # Session/window identity before any mutation.
            meta = get_terminal_metadata(terminal_id)
            term_session: Optional[str] = meta["tmux_session"] if meta else None
            term_window: Optional[str] = meta["tmux_window"] if meta else None

            # Re-map renumbered-but-live panes instead of deleting. A live tab
            # label means the pane_id was renumbered, not closed: re-resolve the
            # current pane_id and update both maps. Only when re-resolution fails
            # do we fall through to the delete path.
            liveness = self._label_still_live(term_window) if term_window else PaneLiveness.ABSENT
            if liveness == PaneLiveness.UNKNOWN:
                logger.warning("Reconcile: preserving %s; Herdr liveness is unknown", terminal_id)
                continue
            if liveness == PaneLiveness.PRESENT:
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
                        "preserving",
                        term_window,
                        terminal_id,
                        e,
                    )
                    # A live label proves that the old compact pane id was
                    # reused; a failed re-resolution is UNKNOWN, not absence.
                    # Do not let a transient backend error retire that live
                    # incarnation.
                    continue
                else:
                    with self._ownership_lock:
                        if (
                            self._pane_to_terminal.get(pane_id) != terminal_id
                            or self._terminal_to_pane.get(terminal_id) != pane_id
                        ):
                            continue
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

            # Tab label genuinely gone (or re-resolve failed): managed
            # retirement must succeed before ownership maps are pruned.
            try:
                from cli_agent_orchestrator.services import terminal_service

                retired = terminal_service.retire_observed_terminal(
                    terminal_id,
                    expected_session=term_session,
                    expected_pane_id=pane_id,
                )
                if not retired:
                    continue
                deleted += 1
            except Exception as e:
                logger.warning(f"Reconcile: failed to delete terminal {terminal_id}: {e}")
                continue

            self.unregister_terminal(terminal_id, expected_pane_id=pane_id)

            if term_session:
                affected_sessions.add(term_session)

        # Kill a workspace only when its label is gone from herdr AND no managed
        # terminal remains for the session. A live label means the workspace is
        # alive and its panes were merely renumbered — killing it would tear down
        # working agents.
        if affected_sessions:
            remaining_by_session: Dict[str, int] = {
                session: len(list_terminals_by_session(session)) for session in affected_sessions
            }

            for session_name, remaining in remaining_by_session.items():
                if remaining == 0 and session_name not in live_workspace_labels:
                    try:
                        from cli_agent_orchestrator.services import callback_recovery

                        backend = get_backend()
                        with callback_recovery.session_lifecycle_claim(
                            type(backend).__name__, session_name
                        ):
                            # Re-read while holding the shared create/delete
                            # claim: an older empty decision must not kill a
                            # newly persisted session occupant.
                            if not list_terminals_by_session(session_name):
                                backend.kill_session(session_name)
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

        herdr (0.6.8) resets the entire connection when it receives a second
        events.subscribe on a connection that already has an active
        subscription. So every subscription this service needs — one
        pane.agent_status_changed per managed pane (pane_id is required; herdr
        rejects the wildcard form with invalid_request) plus the pane.closed and
        workspace.closed lifecycle events — must be sent together in one call.

        The pane_id → terminal_id mapping in _pane_to_terminal is already current:
        a socket disconnect does not change pane_ids (only a herdr server restart
        compacts them), and _reconcile() has already pruned stale panes before
        this runs.
        """
        ownership = self._ownership_items()
        subscriptions: list = [
            {"type": "pane.agent_status_changed", "pane_id": pane_id}
            for pane_id, _terminal_id in ownership
        ]
        subscriptions.append({"type": "pane.closed"})
        subscriptions.append({"type": "workspace.closed"})

        message = {
            "id": "sub_all",
            "method": "events.subscribe",
            "params": {"subscriptions": subscriptions},
        }
        await self._send(message)
        logger.info(
            f"Subscribed to {len(ownership)} pane(s) + lifecycle events "
            f"in one events.subscribe call"
        )

    async def _force_reconnect(self) -> None:
        """Close the socket so _socket_loop reconnects and rebuilds the subscription.

        This is how a newly registered pane starts streaming events: herdr has no
        incremental subscribe, and a second events.subscribe on the live
        connection would reset it. Closing the writer makes the blocked
        readline() in _event_loop return EOF, which raises ConnectionError and
        drives _socket_loop through a fresh connect + combined re-subscribe.
        """
        writer = self._writer
        if writer is None:
            return
        try:
            writer.close()
        except Exception as e:
            logger.debug(f"Force reconnect: writer close raised (ignored): {e}")

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
                self._schedule_lifecycle_retirement(
                    event_name,
                    event.get("data", {}),
                )
                continue

            data = event.get("data", {})
            pane_id = data.get("pane_id", "")
            status = data.get("agent_status", "")

            # Only process events for managed panes
            terminal_id = self._terminal_for_pane(pane_id)
            if not terminal_id:
                continue

            if status in ("idle", "done"):
                # Clear working timestamp
                with self._ownership_lock:
                    self._working_since.pop(terminal_id, None)
                # Trigger delivery
                self._schedule_delivery(terminal_id)

            elif status == "working":
                # Track working start for kiro supplement check
                with self._ownership_lock:
                    if (
                        terminal_id in self._kiro_terminals
                        and terminal_id not in self._working_since
                    ):
                        self._working_since[terminal_id] = time.time()

    def _schedule_delivery(self, terminal_id: str, expected_pane_id: str | None = None) -> None:
        """Schedule one off-loop delivery and coalesce one trailing trigger.

        This method runs on the service event loop. It snapshots the current
        terminal/pane ownership before scheduling and never lets the worker
        touch loop-owned ownership maps.
        """
        if self._delivery_shutdown:
            return

        with self._ownership_lock:
            pane_id = self._terminal_to_pane.get(terminal_id)
            if (
                pane_id is None
                or (expected_pane_id is not None and pane_id != expected_pane_id)
                or self._pane_to_terminal.get(pane_id) != terminal_id
            ):
                return

        task = self._delivery_tasks.get(terminal_id)
        if task is not None:
            # A single value means duplicate triggers coalesce into at most one
            # trailing rerun, retaining the newest ownership snapshot. Keep a
            # just-finished task here until its loop-side completion callback
            # has observed it and cleared the map.
            self._delivery_reruns[terminal_id] = pane_id
            return

        task = asyncio.create_task(
            self._deliver_off_loop(terminal_id),
            name=f"herdr-delivery-{terminal_id}",
        )
        self._delivery_tasks[terminal_id] = task

        def completed(done: asyncio.Task) -> None:
            self._delivery_completed(terminal_id, done)

        task.add_done_callback(completed)

    async def _deliver_off_loop(self, terminal_id: str) -> None:
        """Run the synchronous delivery callback without blocking socket reads."""
        await asyncio.to_thread(self._deliver, terminal_id)

    def _delivery_completed(self, terminal_id: str, done: asyncio.Task) -> None:
        """Observe delivery completion and safely schedule one valid rerun."""
        if self._delivery_tasks.get(terminal_id) is not done:
            return
        self._delivery_tasks.pop(terminal_id, None)

        if done.cancelled():
            logger.warning("Delivery for terminal %s was cancelled", terminal_id)
        else:
            error = done.exception()
            if error is not None:
                logger.error(
                    "Delivery task failed for terminal %s",
                    terminal_id,
                    exc_info=(type(error), error, error.__traceback__),
                )

        expected_pane_id = self._delivery_reruns.pop(terminal_id, None)
        if expected_pane_id is not None and not self._delivery_shutdown:
            # Compare the loop-side snapshot before starting the trailing run.
            # A reused pane or replaced terminal is preserved, never redirected.
            self._schedule_delivery(terminal_id, expected_pane_id)

    async def _shutdown_delivery_tasks(self) -> None:
        """Await every tracked delivery task before shutdown."""
        self._delivery_shutdown = True
        self._delivery_reruns.clear()
        tasks = tuple(self._delivery_tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._delivery_tasks.clear()

    def _schedule_lifecycle_retirement(self, event_type: str, data: dict) -> None:
        """Track one off-loop retirement whose map commit returns to this loop."""
        task = asyncio.create_task(
            self._handle_lifecycle_event_async(event_type, dict(data)),
            name=(
                f"herdr-{event_type}-retirement-"
                f"{data.get('workspace_id') or data.get('pane_id') or 'unknown'}"
            ),
        )
        self._lifecycle_tasks.add(task)

        def completed(done: asyncio.Task) -> None:
            self._lifecycle_tasks.discard(done)
            if done.cancelled():
                logger.warning("%s managed retirement was cancelled", event_type)
                return
            error = done.exception()
            if error is not None:
                logger.error(
                    "%s managed retirement failed off-loop",
                    event_type,
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(completed)

    def _label_still_live(self, window_name: str) -> PaneLiveness:
        """Return the strict liveness state of a tab label in Herdr.

        Used to disambiguate herdr's reused compact pane_ids on replayed
        pane_closed events. The tab label is unique per incarnation, so a live
        label means the close event refers to an older incarnation and is stale.

        A failed query is ``UNKNOWN``, never absence. Only a uniquely parsed
        ``ABSENT`` observation may authorize retirement of a reused pane.
        """
        try:
            result = subprocess.run(
                ["herdr", "--session", self._herdr_session, "tab", "list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.warning(
                    "_label_still_live: herdr tab list failed (rc=%s): %s",
                    result.returncode,
                    result.stderr.strip(),
                )
                return PaneLiveness.UNKNOWN
            tab_data = json.loads(result.stdout)
            tabs = tab_data["result"]["tabs"]
            if not isinstance(tabs, list) or any(not isinstance(tab, dict) for tab in tabs):
                return PaneLiveness.UNKNOWN
            if any(not isinstance(tab.get("label"), str) or not tab["label"] for tab in tabs):
                return PaneLiveness.UNKNOWN
            live_labels = {tab["label"] for tab in tabs}
            return PaneLiveness.PRESENT if window_name in live_labels else PaneLiveness.ABSENT
        except (subprocess.SubprocessError, json.JSONDecodeError, KeyError, OSError) as e:
            logger.warning("_label_still_live: could not query herdr (%s)", e)
            return PaneLiveness.UNKNOWN

    def _workspace_sessions_from_herdr(self) -> Dict[str, str]:
        """Read workspace identities without touching event-loop-owned maps."""
        result = subprocess.run(
            ["herdr", "--session", self._herdr_session, "workspace", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode,
                result.args,
                output=result.stdout,
                stderr=result.stderr,
            )
        ws_data = json.loads(result.stdout)
        workspaces = ws_data.get("result", {}).get("workspaces", [])
        return {ws["workspace_id"]: ws["label"] for ws in workspaces}

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
            self._workspace_to_session = self._workspace_sessions_from_herdr()
            return self._workspace_to_session.get(workspace_id)
        except (subprocess.SubprocessError, json.JSONDecodeError, KeyError, OSError) as e:
            logger.warning("_resolve_session_from_herdr: could not query herdr (%s)", e)
            return None

    @staticmethod
    def _kill_empty_workspace_claimed(session_name: str) -> bool:
        """Re-read and remove one empty workspace under the shared session claim.

        The last terminal retirement releases its terminal claim before this
        cleanup runs.  A session claim closes that handoff race with a new
        terminal creation using the same reusable workspace name.
        """
        from cli_agent_orchestrator.backends.registry import get_backend
        from cli_agent_orchestrator.clients.database import list_terminals_by_session
        from cli_agent_orchestrator.services import callback_recovery

        backend = get_backend()
        with callback_recovery.session_lifecycle_claim(type(backend).__name__, session_name):
            if list_terminals_by_session(session_name):
                return False
            backend.kill_session(session_name)
            logger.info("pane.closed: killed empty workspace %s", session_name)
            return True

    async def _handle_lifecycle_event_async(self, event_type: str, data: dict) -> None:
        """Retire off-loop, then commit ownership-map changes on the event loop."""
        from cli_agent_orchestrator.backends.registry import get_backend
        from cli_agent_orchestrator.clients.database import (
            get_terminal_metadata,
            list_terminals_by_session,
        )
        from cli_agent_orchestrator.services import terminal_service

        if event_type == "pane.closed":
            pane_id = data.get("pane_id", "")
            terminal_id = self._terminal_for_pane(pane_id)
            if not terminal_id:
                return
            meta = await asyncio.to_thread(get_terminal_metadata, terminal_id)
            session_name = meta["tmux_session"] if meta else None
            window_name = meta["tmux_window"] if meta else None
            liveness = (
                await asyncio.to_thread(self._label_still_live, window_name)
                if window_name
                else PaneLiveness.ABSENT
            )
            if liveness == PaneLiveness.UNKNOWN:
                logger.warning("pane.closed: preserving %s; Herdr liveness is unknown", terminal_id)
                return
            if liveness == PaneLiveness.PRESENT:
                logger.info(
                    "pane.closed: ignoring stale close for %s (pane=%s) — "
                    "label %s still live in herdr (compact pane_id reused)",
                    terminal_id,
                    pane_id,
                    window_name,
                )
                return
            try:
                retired = await asyncio.to_thread(
                    terminal_service.retire_observed_terminal,
                    terminal_id,
                    expected_session=session_name,
                    expected_pane_id=pane_id,
                    unregister_inbox=False,
                )
            except Exception as exc:  # noqa: BLE001 - preserve ownership on hold
                logger.warning(
                    "pane.closed: failed to delete terminal %s: %s",
                    terminal_id,
                    exc,
                )
                return
            if not retired:
                return

            # Worker-thread retirement deliberately skips map mutation.
            # Compare-and-remove under the ownership lock so a compact pane-id
            # reuse registered while retirement was running survives.
            self.unregister_terminal(terminal_id, expected_pane_id=pane_id)
            if session_name:
                try:
                    await asyncio.to_thread(
                        self._kill_empty_workspace_claimed,
                        session_name,
                    )
                except Exception as exc:  # noqa: BLE001 - best-effort backend cleanup
                    logger.warning(
                        "pane.closed: failed to kill workspace %s: %s",
                        session_name,
                        exc,
                    )
            return

        if event_type != "workspace.closed":
            return
        workspace_id = data.get("workspace_id", "")
        session_name = self._workspace_to_session.get(workspace_id)
        if not session_name:
            try:
                workspaces = await asyncio.to_thread(self._workspace_sessions_from_herdr)
            except (
                subprocess.SubprocessError,
                json.JSONDecodeError,
                KeyError,
                OSError,
            ) as exc:
                logger.warning(
                    "workspace.closed: could not resolve workspace %s: %s",
                    workspace_id,
                    exc,
                )
                return
            # Registrations may have arrived while the subprocess was running.
            # Merge the observed identities instead of replacing the loop-owned
            # map with a snapshot that predates those registrations.
            self._workspace_to_session.update(workspaces)
            session_name = workspaces.get(workspace_id)
            if not session_name:
                return

        ownership_snapshot = self._ownership_items()

        def session_ownership() -> tuple[tuple[str, str], ...]:
            return tuple(
                (pane_id, terminal_id)
                for pane_id, terminal_id in ownership_snapshot
                if (
                    (metadata := get_terminal_metadata(terminal_id))
                    and metadata.get("tmux_session") == session_name
                )
            )

        to_remove = await asyncio.to_thread(session_ownership)
        try:
            await asyncio.to_thread(
                terminal_service.retire_closed_workspace_session,
                session_name,
                unregister_inbox=False,
            )
        except Exception as exc:  # noqa: BLE001 - preserve ownership on hold
            logger.warning(
                "workspace.closed: managed retirement held for %s: %s",
                session_name,
                exc,
            )
            return
        for pane_id, terminal_id in to_remove:
            self.unregister_terminal(terminal_id, expected_pane_id=pane_id)
        if self._workspace_to_session.get(workspace_id) == session_name:
            self._workspace_to_session.pop(workspace_id, None)
        logger.info(
            "workspace.closed: cleaned up session %s (%d terminals)",
            session_name,
            len(to_remove),
        )

    def _handle_lifecycle_event(self, event_type: str, data: dict) -> None:
        """Synchronous compatibility seam; production events use the async path."""
        from cli_agent_orchestrator.backends.registry import get_backend
        from cli_agent_orchestrator.clients.database import (
            get_terminal_metadata,
            list_terminals_by_session,
        )

        if event_type == "pane.closed":
            pane_id = data.get("pane_id", "")
            terminal_id = self._terminal_for_pane(pane_id)
            if not terminal_id:
                return

            # Get session before cleanup
            meta = get_terminal_metadata(terminal_id)
            session_name = meta["tmux_session"] if meta else None

            # Guard against herdr's compact pane_id reuse + event replay.
            #
            # herdr (0.6.8) reuses compact pane_ids when a tab is killed and a
            # new tab takes the same index, AND replays the ENTIRE pane_closed
            # history on every fresh events.subscribe (which register_terminal
            # triggers via _force_reconnect). So a replayed close for an OLD
            # incarnation of this pane_id arrives mapped to the terminal that now
            # occupies the reused index — deleting a live terminal.
            #
            # The tab label (tmux_window) is unique per incarnation, so confirm
            # the label is genuinely gone from herdr before deleting. If the
            # label is still live, this close is stale (replayed) — ignore it.
            # If herdr can't be queried, fall toward delete: never leave a
            # terminal we think is open when it may actually be closed.
            window_name = meta["tmux_window"] if meta else None
            liveness = self._label_still_live(window_name) if window_name else PaneLiveness.ABSENT
            if liveness == PaneLiveness.UNKNOWN:
                logger.warning("pane.closed: preserving %s; Herdr liveness is unknown", terminal_id)
                return
            if liveness == PaneLiveness.PRESENT:
                logger.info(
                    "pane.closed: ignoring stale close for %s (pane=%s) — "
                    "label %s still live in herdr (compact pane_id reused)",
                    terminal_id,
                    pane_id,
                    window_name,
                )
                return

            try:
                from cli_agent_orchestrator.services import terminal_service

                retired = terminal_service.retire_observed_terminal(
                    terminal_id,
                    expected_session=session_name,
                    expected_pane_id=pane_id,
                )
                if not retired:
                    return
            except Exception as e:
                logger.warning(f"pane.closed: failed to delete terminal {terminal_id}: {e}")
                return

            self.unregister_terminal(terminal_id, expected_pane_id=pane_id)

            logger.info(f"pane.closed: cleaned up terminal {terminal_id} (pane={pane_id})")

            # If session has no more terminals in our map, kill workspace
            remaining_in_session = list_terminals_by_session(session_name) if session_name else []
            if session_name and not remaining_in_session:
                try:
                    from cli_agent_orchestrator.services import callback_recovery

                    backend = get_backend()
                    with callback_recovery.session_lifecycle_claim(
                        type(backend).__name__, session_name
                    ):
                        if not list_terminals_by_session(session_name):
                            backend.kill_session(session_name)
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

            # Snapshot map ownership before managed retirement removes the DB
            # rows. The lifecycle guard owns all generation checks and refuses
            # the whole session when a callback recovery is still open.
            to_remove = [
                (pid, tid)
                for pid, tid in self._ownership_items()
                if (m := get_terminal_metadata(tid)) and m.get("tmux_session") == session_name
            ]
            try:
                from cli_agent_orchestrator.services import terminal_service

                terminal_service.retire_closed_workspace_session(session_name)
            except Exception as e:
                logger.warning(f"workspace.closed: managed retirement held for {session_name}: {e}")
                return

            # Prune maps for terminals belonging to this session. Match on each
            # terminal's DB session rather than a pane_id/workspace_id string
            # prefix: herdr renumbers compact pane_ids and does not guarantee
            # they begin with the workspace_id, so a prefix test is unreliable.
            # This mirrors the session match used in the pane.closed handler.
            for pid, tid in to_remove:
                self.unregister_terminal(tid, expected_pane_id=pid)

            self._workspace_to_session.pop(workspace_id, None)
            logger.info(
                f"workspace.closed: cleaned up session {session_name} ({len(to_remove)} terminals)"
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
        with self._ownership_lock:
            working_snapshot = tuple(self._working_since.items())
            kiro_terminals = frozenset(self._kiro_terminals)
            terminal_to_pane = dict(self._terminal_to_pane)
        for terminal_id, working_since in working_snapshot:
            if terminal_id not in kiro_terminals:
                continue

            working_duration = now - working_since
            if working_duration < _KIRO_WORKING_THRESHOLD:
                continue

            # Read pane and check for permission prompt
            pane_id = terminal_to_pane.get(terminal_id)
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
                self._schedule_delivery(terminal_id)
                # Reset the timer so we don't spam
                with self._ownership_lock:
                    if self._working_since.get(terminal_id) == working_since:
                        self._working_since[terminal_id] = now

    async def _send(self, message: dict) -> None:
        """Send a JSON message to the herdr socket."""
        assert self._writer is not None
        data = json.dumps(message).encode() + b"\n"
        self._writer.write(data)
        await self._writer.drain()
