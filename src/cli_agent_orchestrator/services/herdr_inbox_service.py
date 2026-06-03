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

        # Subscribe to events if connected and event loop is captured.
        # register_terminal() may be called from a synchronous/non-event-loop thread,
        # so we use run_coroutine_threadsafe instead of create_task.
        if self._connected and self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._subscribe_pane(pane_id), self._loop)

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
        kiro_task = asyncio.ensure_future(self._kiro_supplement_loop())
        try:
            await self._socket_loop()
        finally:
            kiro_task.cancel()

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
                self._backoff = _BACKOFF_BASE  # Reset backoff on success

                # Re-subscribe all managed panes
                await self._resubscribe_all()

                # Listen for events
                await self._event_loop()

            except (ConnectionError, OSError, asyncio.IncompleteReadError) as e:
                logger.warning(f"Herdr socket disconnected: {e}")
                self._connected = False

                # Exponential backoff
                logger.info(f"Reconnecting in {self._backoff}s...")
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * _BACKOFF_MULTIPLIER, _BACKOFF_MAX)

    async def _connect(self) -> None:
        """Connect to the herdr socket."""
        self._reader, self._writer = await asyncio.open_unix_connection(self._socket_path)
        logger.info(f"Connected to herdr socket: {self._socket_path}")

    async def _subscribe_pane(self, pane_id: str) -> None:
        """Subscribe to agent_status_changed events for a pane.

        Herdr requires per-pane subscription (wildcard not supported).
        The correct format uses 'subscriptions' array with 'type' + 'pane_id'.
        """
        message = {
            "id": f"sub_{pane_id}",
            "method": "events.subscribe",
            "params": {
                "subscriptions": [
                    {
                        "type": "pane.agent_status_changed",
                        "pane_id": pane_id,
                    }
                ]
            },
        }
        await self._send(message)

    async def _resubscribe_all(self) -> None:
        """Re-subscribe all managed panes after socket reconnect.

        The pane_id → terminal_id mapping in _pane_to_terminal is already current —
        a socket disconnect does not change pane_ids (only a herdr server restart
        compacts them). Re-subscribing with existing pane_ids is safe and correct.

        Note: _terminal_to_pane keys are CAO UUIDs, not herdr-internal terminal_ids,
        so we cannot match them against herdr pane list output. Use the existing
        _pane_to_terminal mapping directly.
        """
        for pane_id in list(self._pane_to_terminal.keys()):
            await self._subscribe_pane(pane_id)

        logger.info(f"Re-subscribed {len(self._pane_to_terminal)} panes after reconnect")

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
                capture_output=True, text=True, timeout=5,
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
