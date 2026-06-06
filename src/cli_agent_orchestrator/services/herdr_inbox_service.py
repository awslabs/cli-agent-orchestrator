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
import time
from typing import Callable, Dict, Optional, Set

logger = logging.getLogger(__name__)

# Exponential backoff parameters
_BACKOFF_BASE = 1.0  # seconds
_BACKOFF_MAX = 30.0  # seconds
_BACKOFF_MULTIPLIER = 2.0

# Kiro supplement check: how long in "working" before we check pane read
_KIRO_WORKING_THRESHOLD = 30.0  # seconds


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

        # Connection state
        self._connected = False
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._backoff = _BACKOFF_BASE
        self._loop: Optional[asyncio.AbstractEventLoop] = None

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
        self._loop = asyncio.get_running_loop()
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
        from herdr workspace list.
        """
        from cli_agent_orchestrator.clients.database import (
            delete_terminal,
            list_terminals_by_session,
        )

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
                        delete_terminal(term["id"])
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
            while not self._pane_to_terminal:
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
            delete_terminal,
            get_terminal_metadata,
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

                from cli_agent_orchestrator.clients.database import (
                    delete_terminal,
                    list_terminals_by_session,
                )

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
                                delete_terminal(term["id"])
                            except Exception as e:
                                logger.warning(
                                    f"Reconcile: failed to delete ghost terminal "
                                    f"{term['id']}: {e}"
                                )
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Reconcile: failed to parse tab list: {e}")

        # Find and prune stale panes
        stale_pane_ids = set(self._pane_to_terminal.keys()) - live_pane_ids
        if not stale_pane_ids:
            logger.debug("Reconcile: all panes live, nothing to prune")
            return

        # Track which sessions lose terminals
        affected_sessions: Dict[str, int] = {}  # session_name -> remaining count

        for pane_id in stale_pane_ids:
            terminal_id = self._pane_to_terminal.pop(pane_id, None)
            if not terminal_id:
                continue

            # Get session name before deleting
            meta = get_terminal_metadata(terminal_id)
            session_name = meta["tmux_session"] if meta else None

            # Remove from all maps
            self._terminal_to_pane.pop(terminal_id, None)
            self._kiro_terminals.discard(terminal_id)
            self._working_since.pop(terminal_id, None)

            # Delete orphaned DB record
            try:
                delete_terminal(terminal_id)
            except Exception as e:
                logger.warning(f"Reconcile: failed to delete terminal {terminal_id}: {e}")

            if session_name:
                affected_sessions.setdefault(session_name, 0)

        # Count remaining terminals per affected session
        for tid, _ in self._terminal_to_pane.items():
            meta = get_terminal_metadata(tid)
            if meta and meta["tmux_session"] in affected_sessions:
                affected_sessions[meta["tmux_session"]] += 1

        # Kill workspaces with zero remaining terminals
        for session_name, remaining in affected_sessions.items():
            if remaining == 0:
                try:
                    get_backend().kill_session(session_name)
                    logger.info(f"Reconcile: killed empty workspace {session_name}")
                except Exception as e:
                    logger.warning(f"Reconcile: failed to kill workspace {session_name}: {e}")

        logger.info(f"Reconcile: pruned {len(stale_pane_ids)} stale panes")

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
        subscriptions: list = [
            {"type": "pane.agent_status_changed", "pane_id": pane_id}
            for pane_id in self._pane_to_terminal
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
            f"Subscribed to {len(self._pane_to_terminal)} pane(s) + lifecycle events "
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
                self._handle_lifecycle_event(event_name, event.get("data", {}))
                continue

            data = event.get("data", {})
            pane_id = data.get("pane_id", "")
            status = data.get("agent_status", "")

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

    def _handle_lifecycle_event(self, event_type: str, data: dict) -> None:
        """Handle pane.closed and workspace.closed events."""
        from cli_agent_orchestrator.backends.registry import get_backend
        from cli_agent_orchestrator.clients.database import (
            delete_terminal,
            delete_terminals_by_session,
            get_terminal_metadata,
        )

        if event_type == "pane.closed":
            pane_id = data.get("pane_id", "")
            terminal_id = self._pane_to_terminal.get(pane_id)
            if not terminal_id:
                return

            # Get session before cleanup
            meta = get_terminal_metadata(terminal_id)
            session_name = meta["tmux_session"] if meta else None

            # Remove from maps
            self._pane_to_terminal.pop(pane_id, None)
            self._terminal_to_pane.pop(terminal_id, None)
            self._kiro_terminals.discard(terminal_id)
            self._working_since.pop(terminal_id, None)

            # Delete DB record
            try:
                delete_terminal(terminal_id)
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
                return

            # Delete all DB terminals for this session
            try:
                delete_terminals_by_session(session_name)
            except Exception as e:
                logger.warning(
                    f"workspace.closed: failed to delete terminals for {session_name}: {e}"
                )

            # Prune maps for terminals belonging to this session. Match on each
            # terminal's DB session rather than a pane_id/workspace_id string
            # prefix: herdr renumbers compact pane_ids and does not guarantee
            # they begin with the workspace_id, so a prefix test is unreliable.
            # This mirrors the session match used in the pane.closed handler.
            to_remove = [
                (pid, tid)
                for pid, tid in self._pane_to_terminal.items()
                if (m := get_terminal_metadata(tid)) and m.get("tmux_session") == session_name
            ]
            for pid, tid in to_remove:
                self._pane_to_terminal.pop(pid, None)
                self._terminal_to_pane.pop(tid, None)
                self._kiro_terminals.discard(tid)
                self._working_since.pop(tid, None)

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
