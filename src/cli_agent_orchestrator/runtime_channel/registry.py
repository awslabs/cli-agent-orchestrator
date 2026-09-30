"""Connected execution runtimes and the terminals they run (#745).

Placement is durable: a remote terminal's central row records its ``runtime_id``,
and every command path reads it from that row. This registry holds only what is
live: the open channel per runtime, the last status each runtime reported for its
terminals, and the futures of commands waiting for their results.
"""

import asyncio
import logging
import threading
import time
import uuid
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Awaitable, Callable, Dict, Optional

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.runtime_channel.protocol import Command, CommandType, Result, encode

logger = logging.getLogger(__name__)

LAUNCH_TIMEOUT = 240.0
COMMAND_TIMEOUT = 60.0


class RemoteRuntimeError(Exception):
    """A remote terminal operation could not be completed. ``status_code`` is its HTTP status."""

    status_code = 500


class RuntimeUnavailableError(RemoteRuntimeError):
    """The runtime is not connected, so nothing was sent. Safe to retry."""

    status_code = 503


class RemoteOutcomeUnknownError(RemoteRuntimeError):
    """The command was sent but no result came back; it may or may not have run."""

    status_code = 504


class RemoteCommandError(RemoteRuntimeError):
    """The runtime ran the command and reported a failure."""

    status_code = 502


class RuntimeConnection:
    """One live channel to a runtime, with op_id-correlated command calls."""

    def __init__(self, runtime_id: str, send_text: Callable[[str], Awaitable[None]]):
        self.runtime_id = runtime_id
        self.connected_at = time.time()
        self._send_text = send_text
        self._send_lock = asyncio.Lock()
        self._pending: Dict[str, "asyncio.Future[Result]"] = {}
        self.closed = False
        # Set once the hello exchange is over; commands are refused until then.
        self.active = False

    async def call(
        self,
        command_type: CommandType,
        payload: Dict[str, Any],
        terminal_id: Optional[str] = None,
        timeout: float = COMMAND_TIMEOUT,
    ) -> Dict[str, Any]:
        """Send one command and return its result payload.

        A command whose result does not arrive is reported as unknown, never
        retried here: it may already have run in the runtime.
        """
        op_id = uuid.uuid4().hex
        frame = Command(op_id=op_id, type=command_type, terminal_id=terminal_id, payload=payload)
        future: "asyncio.Future[Result]" = asyncio.get_running_loop().create_future()
        try:
            async with self._send_lock:
                # Checked under the lock: a call still queued behind another send
                # when this connection was replaced or dropped is never written.
                if self.closed:
                    raise RuntimeUnavailableError(f"runtime {self.runtime_id} is not connected")
                # Registered before the frame is written, so its result always
                # finds a waiter.
                self._pending[op_id] = future
                try:
                    await self._send_text(encode(frame))
                except Exception as exc:  # noqa: BLE001 - the frame may have been written
                    raise RemoteOutcomeUnknownError(
                        f"sending {command_type.value} to runtime {self.runtime_id} failed: {exc}"
                    ) from exc
            try:
                result = await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise RemoteOutcomeUnknownError(
                    f"{command_type.value} on runtime {self.runtime_id} timed out; outcome unknown"
                ) from exc
        finally:
            self._pending.pop(op_id, None)
            if future.done() and not future.cancelled():
                future.exception()  # the caller already has its outcome; mark it retrieved
        if not result.ok:
            raise RemoteCommandError(result.error or f"{command_type.value} failed")
        return result.payload

    def awaits(self, op_id: str) -> bool:
        """True while a call is still waiting for this op's result."""
        future = self._pending.get(op_id)
        return future is not None and not future.done()

    def resolve(self, result: Result) -> bool:
        """Deliver a result to its waiting call. False if no call is waiting for it
        any more (it timed out or was cancelled): the caller must act on it."""
        future = self._pending.get(result.op_id)
        if future is None or future.done():
            logger.warning(
                "runtime %s sent a result for an op nobody awaits: %s",
                self.runtime_id,
                result.op_id,
            )
            return False
        future.set_result(result)
        return True

    def close(self, reason: str) -> None:
        """Fail every waiting call: each was sent, so its outcome is unknown."""
        self.closed = True
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(
                    RemoteOutcomeUnknownError(
                        f"channel to runtime {self.runtime_id} closed ({reason}); outcome unknown"
                    )
                )
        self._pending.clear()


class RuntimeRegistry:
    """Process-wide state for connected runtimes. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runtimes: Dict[str, RuntimeConnection] = {}
        # terminal id -> runtime id, for terminals whose status the runtime may report.
        self._placement: Dict[str, str] = {}
        # terminal id -> runtime id, for launches whose result has arrived and
        # is being recorded: not placed yet, but not unrecorded either.
        self._reserved: Dict[str, str] = {}
        self._status: Dict[str, TerminalStatus] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def register(
        self, runtime_id: str, send_text: Callable[[str], Awaitable[None]]
    ) -> RuntimeConnection:
        """Record a new channel for ``runtime_id``, replacing (and closing) any older one.

        Statuses reported over an earlier connection are dropped: the runtime's
        hello on this one says what it runs now (a replaced pod runs nothing).
        """
        conn = RuntimeConnection(runtime_id, send_text)
        with self._lock:
            previous = self._runtimes.get(runtime_id)
            self._runtimes[runtime_id] = conn
            self._loop = asyncio.get_running_loop()
            for terminal_id, placed_on in self._placement.items():
                if placed_on == runtime_id:
                    self._status.pop(terminal_id, None)
        if previous is not None:
            previous.close("replaced by a new connection")
        logger.info("runtime %s connected", runtime_id)
        return conn

    def activate(self, conn: RuntimeConnection) -> None:
        """Make ``conn`` callable: the server's hello has been sent on it."""
        with self._lock:
            if self._runtimes.get(conn.runtime_id) is conn:
                conn.active = True

    def unregister(self, runtime_id: str, conn: RuntimeConnection) -> None:
        with self._lock:
            if self._runtimes.get(runtime_id) is conn:
                del self._runtimes[runtime_id]
                logger.info("runtime %s disconnected", runtime_id)
        conn.close("disconnected")

    def connection(self, runtime_id: str) -> Optional[RuntimeConnection]:
        """The runtime's current connection, once its hello exchange is over."""
        with self._lock:
            conn = self._runtimes.get(runtime_id)
            return conn if conn is not None and conn.active else None

    def list_runtimes(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {
                runtime_id: {
                    "connected_at": conn.connected_at,
                    "terminals": sorted(t for t, r in self._placement.items() if r == runtime_id),
                }
                for runtime_id, conn in self._runtimes.items()
                if conn.active
            }

    def place(self, terminal_id: str, runtime_id: str) -> None:
        with self._lock:
            self._placement[terminal_id] = runtime_id

    def forget(self, terminal_id: str) -> None:
        with self._lock:
            self._placement.pop(terminal_id, None)
            self._status.pop(terminal_id, None)

    def unplace(self, terminal_id: str, runtime_id: str) -> None:
        """Forget a placement, but only if it is still ``runtime_id``'s."""
        with self._lock:
            if self._placement.get(terminal_id) == runtime_id:
                del self._placement[terminal_id]
                self._status.pop(terminal_id, None)

    def is_placed(self, terminal_id: str, runtime_id: str) -> bool:
        with self._lock:
            return self._placement.get(terminal_id) == runtime_id

    def reserve(self, terminal_id: str, runtime_id: str) -> None:
        """Mark a launch whose result arrived as being recorded (see ``is_known``)."""
        with self._lock:
            self._reserved[terminal_id] = runtime_id

    def release(self, terminal_id: str, runtime_id: str) -> None:
        with self._lock:
            if self._reserved.get(terminal_id) == runtime_id:
                del self._reserved[terminal_id]

    def is_known(self, terminal_id: str, runtime_id: str) -> bool:
        """Placed on the runtime, or its launch there is still being recorded."""
        with self._lock:
            return runtime_id in (self._placement.get(terminal_id), self._reserved.get(terminal_id))

    def set_status(
        self,
        terminal_id: str,
        runtime_id: str,
        status: TerminalStatus,
        conn: Optional[RuntimeConnection] = None,
    ) -> bool:
        """Record a status report. Only the runtime a terminal is placed on may report
        it and, when ``conn`` is given, only over that runtime's current connection."""
        with self._lock:
            if conn is not None and self._runtimes.get(runtime_id) is not conn:
                return False
            if self._placement.get(terminal_id) != runtime_id:
                return False
            self._status[terminal_id] = status
            return True

    def get_status(self, terminal_id: str, runtime_id: str) -> TerminalStatus:
        """The runtime's last report, or UNKNOWN while the runtime is not connected
        (or its hello exchange is not over: commands are still refused then)."""
        with self._lock:
            conn = self._runtimes.get(runtime_id)
            if conn is None or not conn.active:
                return TerminalStatus.UNKNOWN
            if self._placement.get(terminal_id) != runtime_id:
                return TerminalStatus.UNKNOWN
            return self._status.get(terminal_id, TerminalStatus.UNKNOWN)

    async def call(
        self,
        runtime_id: str,
        command_type: CommandType,
        payload: Dict[str, Any],
        terminal_id: Optional[str] = None,
        timeout: float = COMMAND_TIMEOUT,
    ) -> Dict[str, Any]:
        conn = self.connection(runtime_id)
        if conn is None:
            raise RuntimeUnavailableError(f"runtime {runtime_id} is not connected")
        return await conn.call(command_type, payload, terminal_id=terminal_id, timeout=timeout)

    def call_blocking(
        self,
        runtime_id: str,
        command_type: CommandType,
        payload: Dict[str, Any],
        terminal_id: Optional[str] = None,
        timeout: float = COMMAND_TIMEOUT,
    ) -> Dict[str, Any]:
        """:meth:`call` from a worker thread (the synchronous terminal service).

        Must not run on the channel loop itself: blocking there would stop the
        loop from delivering the result it waits for.
        """
        with self._lock:
            loop = self._loop
        if loop is None or loop.is_closed():
            raise RuntimeUnavailableError(f"runtime {runtime_id} is not connected")
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            raise RuntimeError("call_blocking used on the channel loop; await call() instead")
        future = asyncio.run_coroutine_threadsafe(
            self.call(runtime_id, command_type, payload, terminal_id=terminal_id, timeout=timeout),
            loop,
        )
        try:
            return future.result(timeout + 5.0)
        except FutureTimeoutError as exc:
            future.cancel()
            raise RemoteOutcomeUnknownError(
                f"{command_type.value} on runtime {runtime_id} timed out; outcome unknown"
            ) from exc


runtime_registry = RuntimeRegistry()
