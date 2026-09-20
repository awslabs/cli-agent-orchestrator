"""Server-side registry for connected execution runtimes (#745).

Owns three associations the shared cao-server needs to route work:

- runtime_id → live channel connection (one persistent outbound WebSocket per
  runtime; the runtime dials the server, never the reverse),
- terminal_id → runtime_id (which runtime executes a given terminal),
- terminal_id → last worker-reported status and stream positions.

The terminal→runtime binding is rebuilt from each runtime's hello snapshot on
reconnect, so a server restart recovers routing without persisting live
channel state. A terminal whose runtime is disconnected reports UNKNOWN — the
server never guesses, and never falls back to probing local tmux for a remote
terminal.
"""

import asyncio
import logging
import time
import uuid
from typing import Awaitable, Callable, Dict, Optional, Tuple

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.runtime_channel.protocol import (
    CommandFrame,
    CommandResultFrame,
    CommandType,
    StreamName,
    encode_frame,
)

logger = logging.getLogger(__name__)

# Generous by design: LAUNCH runs full provider initialization in the runtime
# (tens of seconds for a cold CLI). Terminal-scoped commands pass a tighter
# per-call timeout at the call site.
DEFAULT_COMMAND_TIMEOUT = 60.0

# Per-operation deadlines. They live beside the registry rather than in the HTTP
# layer because non-HTTP senders need them too: terminal_service routes a remote
# send_input from any caller, and importing the endpoint module for a number
# would drag the FastAPI/auth surface into the service layer.
LAUNCH_TIMEOUT = 240.0
INPUT_TIMEOUT = 60.0
EXTRACT_TIMEOUT = 60.0
TEARDOWN_TIMEOUT = 120.0


class RuntimeUnavailableError(Exception):
    """No connected runtime can execute this operation right now."""


class RemoteCommandError(Exception):
    """The runtime executed the command and reported a failure."""

    def __init__(self, outcome: str, message: str):
        self.outcome = outcome
        super().__init__(message)


class RuntimeConnection:
    """One live channel to a runtime, with op_id-correlated command futures."""

    def __init__(self, runtime_id: str, send_text: Callable[[str], Awaitable[None]]):
        self.runtime_id = runtime_id
        self._send_text = send_text
        self._pending: Dict[str, asyncio.Future] = {}
        self.connected_at = time.time()
        self.last_seen = time.time()

    async def send_command(
        self,
        command_type: CommandType,
        payload: dict,
        terminal_id: Optional[str] = None,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> CommandResultFrame:
        """Send one correlated command and wait for its retained result.

        A timeout here means the RESPONSE is missing, not that the work did
        not happen — callers must treat it as unknown, never resubmit the same
        work blindly (#745).
        """
        op_id = uuid.uuid4().hex
        frame = CommandFrame(
            op_id=op_id, terminal_id=terminal_id, type=command_type, payload=payload
        )
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[op_id] = future
        try:
            await self._send_text(encode_frame(frame))
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(op_id, None)

    def resolve(self, result: CommandResultFrame) -> None:
        future = self._pending.get(result.op_id)
        if future is not None and not future.done():
            future.set_result(result)
        elif future is None:
            # A result for an op this process never sent (e.g. retained from
            # before a server restart). Logged, not raised: the runtime just
            # needs its ack, which the endpoint sends regardless.
            logger.info(
                "runtime %s delivered result for unknown op %s (server restart?)",
                self.runtime_id,
                result.op_id,
            )

    def fail_all_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeUnavailableError(reason))
        self._pending.clear()


class RuntimeChannelRegistry:
    """Process-wide routing state for remote execution runtimes."""

    def __init__(self) -> None:
        self._runtimes: Dict[str, RuntimeConnection] = {}
        self._terminal_runtime: Dict[str, str] = {}
        self._status: Dict[str, TerminalStatus] = {}
        # Last stream position the server has consumed, per (terminal, stream).
        # Returned to the runtime on hello so it can replay what this server
        # missed while disconnected (bounded by the runtime's replay window).
        self._positions: Dict[Tuple[str, str], int] = {}
        # Live interactive-attach clients by terminal (#776).
        self._attach_sinks: Dict[str, "asyncio.Queue"] = {}
        # The loop the channel connections belong to, captured at register().
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # --- runtime lifecycle ---

    def register(
        self, runtime_id: str, send_text: Callable[[str], Awaitable[None]]
    ) -> RuntimeConnection:
        existing = self._runtimes.get(runtime_id)
        if existing is not None:
            # A reconnect superseding a half-open connection: fail the old
            # channel's waiters rather than leaving them to time out.
            existing.fail_all_pending(f"runtime {runtime_id} reconnected on a new channel")
        conn = RuntimeConnection(runtime_id, send_text)
        self._runtimes[runtime_id] = conn
        # Capture the loop the channels live on. Command futures are created on
        # it (see RuntimeConnection.send_command), so a caller on a worker thread
        # has no way to dispatch without it — see send_terminal_command_blocking.
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:  # registered off-loop (unit tests) — nothing to capture
            pass
        logger.info("runtime channel registered: %s", runtime_id)
        return conn

    def unregister(self, runtime_id: str, conn: RuntimeConnection) -> None:
        # Guard against a stale disconnect handler unregistering the NEW
        # connection after a fast reconnect replaced it.
        if self._runtimes.get(runtime_id) is conn:
            del self._runtimes[runtime_id]
            logger.info("runtime channel disconnected: %s", runtime_id)
        conn.fail_all_pending(f"runtime {runtime_id} channel closed")

    def get_runtime(self, runtime_id: str) -> Optional[RuntimeConnection]:
        return self._runtimes.get(runtime_id)

    def list_runtimes(self) -> Dict[str, dict]:
        return {
            rid: {
                "connected_at": conn.connected_at,
                "last_seen": conn.last_seen,
                "terminals": sorted(t for t, r in self._terminal_runtime.items() if r == rid),
            }
            for rid, conn in self._runtimes.items()
        }

    # --- terminal routing ---

    def bind_terminal(self, terminal_id: str, runtime_id: str) -> None:
        self._terminal_runtime[terminal_id] = runtime_id

    def unbind_terminal(self, terminal_id: str) -> None:
        self._terminal_runtime.pop(terminal_id, None)
        self._status.pop(terminal_id, None)
        for stream in StreamName:
            self._positions.pop((terminal_id, stream.value), None)

    def is_remote(self, terminal_id: str) -> bool:
        return terminal_id in self._terminal_runtime

    def runtime_for_terminal(self, terminal_id: str) -> Optional[str]:
        return self._terminal_runtime.get(terminal_id)

    async def send_terminal_command(
        self,
        terminal_id: str,
        command_type: CommandType,
        payload: dict,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> CommandResultFrame:
        runtime_id = self._terminal_runtime.get(terminal_id)
        if runtime_id is None:
            raise RuntimeUnavailableError(f"terminal {terminal_id} is not bound to a runtime")
        conn = self._runtimes.get(runtime_id)
        if conn is None:
            raise RuntimeUnavailableError(
                f"runtime {runtime_id} for terminal {terminal_id} is not connected"
            )
        return await conn.send_command(
            command_type, payload, terminal_id=terminal_id, timeout=timeout
        )

    def send_terminal_command_blocking(
        self,
        terminal_id: str,
        command_type: CommandType,
        payload: dict,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> CommandResultFrame:
        """Route one terminal-scoped operation from a worker thread.

        Some senders are synchronous by construction and run off the loop: inbox
        delivery is the one that matters (a worker's completion callback for a
        supervisor whose session is in another pod), and it is already dispatched
        via ``asyncio.to_thread``. Without this, such a caller would reach past
        the channel into local tmux and fail with "session not found" for a
        terminal that is alive in its own runtime.

        Refuses to run on the loop thread: ``Future.result()`` there would block
        the very loop that has to deliver the frame, deadlocking until timeout.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            raise RuntimeUnavailableError(
                f"no runtime channel loop is available to reach terminal {terminal_id}"
            )
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            raise RuntimeError(
                "send_terminal_command_blocking called on the channel loop; "
                "await send_terminal_command instead"
            )
        future = asyncio.run_coroutine_threadsafe(
            self.send_terminal_command(terminal_id, command_type, payload, timeout=timeout),
            loop,
        )
        # The coroutine already enforces `timeout`; this one only bounds the
        # handoff itself, so it is the same deadline plus a small margin rather
        # than a second, independent one.
        return future.result(timeout + 5.0)

    # --- worker-reported state ---

    def set_status(self, terminal_id: str, status: TerminalStatus) -> None:
        self._status[terminal_id] = status

    def get_status(self, terminal_id: str) -> TerminalStatus:
        runtime_id = self._terminal_runtime.get(terminal_id)
        if runtime_id is None or runtime_id not in self._runtimes:
            # No live channel: the server cannot know. Explicit UNKNOWN beats
            # a stale last report presented as current (#745 failure posture).
            return TerminalStatus.UNKNOWN
        return self._status.get(terminal_id, TerminalStatus.UNKNOWN)

    def record_position(self, terminal_id: str, stream: str, end_pos: int) -> None:
        key = (terminal_id, stream)
        if end_pos > self._positions.get(key, 0):
            self._positions[key] = end_pos

    def resume_position(self, terminal_id: str, stream: str) -> int:
        return self._positions.get((terminal_id, stream), 0)

    # --- interactive attach relay (#776) ---
    #
    # One live attach client per terminal: attach bytes arriving on the
    # channel are handed to that client's queue instead of the bus. `None`
    # on the queue means the runtime-side PTY ended (EOF/detach).

    def bind_attach(self, terminal_id: str, sink: "asyncio.Queue") -> None:
        self._attach_sinks[terminal_id] = sink

    def unbind_attach(self, terminal_id: str, sink: "asyncio.Queue") -> None:
        if self._attach_sinks.get(terminal_id) is sink:
            del self._attach_sinks[terminal_id]

    def deliver_attach(self, terminal_id: str, data: Optional[bytes]) -> bool:
        sink = self._attach_sinks.get(terminal_id)
        if sink is None:
            return False
        sink.put_nowait(data)
        return True


runtime_registry = RuntimeChannelRegistry()
