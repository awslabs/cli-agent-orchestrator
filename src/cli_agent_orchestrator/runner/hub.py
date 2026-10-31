"""Server side of the runner channel: accept runners, place terminals, route calls.

The hub runs its WebSocket listener on a DEDICATED thread with its own event
loop, not on uvicorn's. That is load-bearing, not tidiness: ``TerminalBackend``
methods are synchronous and some are called directly on the API's event loop.
If the frames answering such a call had to arrive on that same loop, the call
would block the loop that has to deliver its own reply — a deadlock. With a
separate loop a caller on any thread (including the API loop) blocks on a
thread-safe future that the hub thread resolves. The only thread that may not
call :meth:`RunnerHub.call` is the hub thread itself, and it is refused.

Three kinds of state, with different owners:

- **Routes** (which runner hosts which window) are rebuilt from the runners
  themselves: every ``hello`` and periodic ``inventory`` carries the terminals
  the runner is actually hosting. Nothing is guessed.
- **Placement** (which runner a terminal was created on) is persisted in the
  terminal's own row (``terminals.runner_id``) and seeded back into the routes
  at startup, so a restarted server knows who owns a terminal before that
  runner reconnects. When a runner's own report disagrees with the row, the
  report wins (the runner is the one holding the pane) and the row is updated;
  a terminal a runner reports that has NO row is unowned and is reaped.
- **Membership** (which runners exist, what they claim) and the **reap set**
  (terminals whose fate the server could not confirm) are persisted in
  ``runner-hub.json``, because they are exactly what a restarted server cannot
  learn from runners that have not reconnected yet. Membership is what makes "no route" provable as "no
  such terminal": only once every non-expired member has reported to THIS
  process are all of its routes known. A member silent for longer than
  ``expiry`` is dropped; its terminals are declared lost and queued for reaping
  should it ever return.
"""

import asyncio
import base64
import collections
import concurrent.futures
import errno
import hmac
import json
import logging
import os
import queue
import select
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from cli_agent_orchestrator.backends.base import TerminalBackendError
from cli_agent_orchestrator.runner.protocol import (
    CLOSE_BAD_HELLO,
    CLOSE_PROTOCOL_MISMATCH,
    CLOSE_REPLACED,
    HANDSHAKE_TIMEOUT,
    MAX_FRAME_BYTES,
    PING_INTERVAL,
    PING_TIMEOUT,
    PROTOCOL_VERSION,
    RUNNER_ID_PATTERN,
    SEND_TIMEOUT,
    TOKEN_HEADER,
    RunnerOutcomeUnknown,
    RunnerUnavailable,
    raise_remote_error,
)

# Binding the listener is local and immediate; failing to within this means the
# port is unusable, which start() reports instead of hanging the lifespan.
_START_TIMEOUT = 10.0
# Stopping waits for the listener to close and in-flight handlers to unwind,
# each bounded by PING_TIMEOUT at worst; this keeps shutdown below the
# server's own graceful-termination window.
_STOP_TIMEOUT = 10.0

logger = logging.getLogger(__name__)

WindowKey = Tuple[str, str]  # (session_name, window_name)

# A route missing from this many consecutive inventories is dropped. One is not
# enough: an inventory snapshot taken just before a create finished can be
# delivered just after the create's result, and would otherwise erase a route
# the runner is in fact hosting.
_INVENTORY_MISSES_BEFORE_DROP = 2

# Per-pipe buffer between the hub loop and the FIFO writer thread. The loop
# never blocks on a slow FIFO reader; past this many queued chunks the chunk is
# dropped and the gap is logged, which is what the FIFO's own bounded buffer
# would eventually force anyway.
_PIPE_QUEUE_CHUNKS = 4096

# A reported terminal with no registry row is reaped only after this many
# consecutive reports, and never within _PLACEMENT_GRACE of its placement: the
# row is written right after the create returns, and a report can land between
# the two.
_UNOWNED_REPORTS_BEFORE_REAP = 2
_PLACEMENT_GRACE = 60.0

# How often a FIFO writer that is waiting (for data, or for room in the FIFO)
# re-checks whether it has been closed.
_SINK_POLL_SECONDS = 0.25


def _decode_frame(raw: Any) -> Optional[Dict[str, Any]]:
    """A wire frame as a JSON object, or None if it is not one.

    A garbled frame from a runner is dropped, never fatal; nothing here reads
    or rewrites a file.
    """
    try:
        frame = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return frame if isinstance(frame, dict) else None


@dataclass(frozen=True)
class RunnerInfo:
    """What a runner declared about itself in ``hello``."""

    runner_id: str
    claims: FrozenSet[str]
    accepts_unclaimed: bool
    max_terminals: int
    tmp_dir: str


@dataclass
class _Connection:
    info: RunnerInfo
    ws: Any
    pending: Dict[str, concurrent.futures.Future] = field(default_factory=dict)
    closed: bool = False


@dataclass
class _Route:
    runner_id: str
    terminal_id: Optional[str]
    inventory_misses: int = 0


@dataclass
class _Member:
    claims: FrozenSet[str]
    last_seen: float  # wall clock, persisted
    reported: bool = False  # has sent a full report to THIS process


class _PipeSink:
    """Copies one pipe's streamed output into the server-local FIFO.

    The server's ``FifoManager`` has already created the FIFO and holds its
    read end (plus a keepalive write end, so it never sees EOF), exactly as it
    does for a local tmux pane. This sink takes the place of the
    ``fifo_writer.py`` process tmux would run: it opens the write end with
    ``O_NOFOLLOW``, refuses anything that is not a FIFO, and writes bytes in
    arrival order.

    Closing is immediate, not queued: :meth:`close` sets a flag the writer
    checks between chunks and while waiting for room in the FIFO, so a backlog
    is discarded rather than drained into a pipe that a replacement sink is
    about to write to. :meth:`join` lets the replacement wait for that.
    """

    def __init__(self, runner_id: str, label: str, fifo_path: str) -> None:
        self.runner_id = runner_id
        self._label = label
        self._fifo_path = fifo_path
        self._queue: "queue.Queue[bytes]" = queue.Queue(maxsize=_PIPE_QUEUE_CHUNKS)
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"runner-pipe-{label}", daemon=True)
        self.dropped_chunks = 0
        self._thread.start()

    @property
    def fifo_path(self) -> str:
        return self._fifo_path

    def feed(self, data: bytes) -> None:
        if self._closed.is_set():
            return
        try:
            self._queue.put_nowait(data)
        except queue.Full:
            self.dropped_chunks += 1
            logger.warning(f"runner pipe {self._label}: writer backlog full, dropped a chunk")

    def close(self) -> None:
        self._closed.set()

    def join(self, timeout: float = 2.0) -> None:
        self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _open(self) -> Optional[int]:
        from cli_agent_orchestrator.services.fifo_reader import _open_fifo

        # Non-blocking open: ENXIO instead of hanging when no reader has the
        # FIFO open (its terminal was already torn down).
        deadline = time.monotonic() + 2.0
        while not self._closed.is_set():
            try:
                return _open_fifo(self._fifo_path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as exc:
                if exc.errno in (errno.ENXIO, errno.ENOENT) and time.monotonic() < deadline:
                    time.sleep(0.1)
                    continue
                logger.error(f"runner pipe {self._label}: cannot open {self._fifo_path}: {exc}")
                return None
        return None

    def _write_all(self, fd: int, chunk: bytes) -> bool:
        view = memoryview(chunk)
        while view:
            if self._closed.is_set():
                return False
            _, writable, _ = select.select([], [fd], [], _SINK_POLL_SECONDS)
            if not writable:
                continue
            try:
                written = os.write(fd, view)
            except BlockingIOError:
                continue
            except BrokenPipeError:
                # The reader went away (terminal torn down). Same stance as
                # fifo_writer: nothing left to deliver to.
                return False
            view = view[written:]
        return True

    def _run(self) -> None:
        fd: Optional[int] = None
        try:
            while not self._closed.is_set():
                try:
                    chunk = self._queue.get(timeout=_SINK_POLL_SECONDS)
                except queue.Empty:
                    continue
                if fd is None:
                    fd = self._open()
                    if fd is None:
                        return
                if not self._write_all(fd, chunk):
                    return
        finally:
            if fd is not None:
                os.close(fd)


class RunnerHub:
    """Accepts runner connections and executes terminal operations on them."""

    def __init__(
        self,
        token: str,
        host: str = "127.0.0.1",
        port: int = 9891,
        call_timeout: float = 30.0,
        placement_timeout: float = 60.0,
        expiry: float = 600.0,
        reserved_sessions: Iterable[str] = (),
        state_path: Optional[Path] = None,
        ssl_context: Optional["ssl.SSLContext"] = None,
    ) -> None:
        if not token:
            raise ValueError("RunnerHub requires a non-empty runner token")
        self._token = token
        self.host = host
        self.port = port
        self.call_timeout = call_timeout
        self.placement_timeout = placement_timeout
        self.expiry = expiry
        # Sessions only a claimant may host. Claims are otherwise learned from
        # runners as they connect, which is too late for a session created
        # before its claimant's first connection.
        self.reserved_sessions = frozenset(reserved_sessions)
        self._state_path = state_path
        # Serve wss:// when set; otherwise plain ws://, for a hub reachable only
        # inside a trusted network or behind a TLS-terminating ingress/mesh.
        self._ssl_context = ssl_context

        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._conns: Dict[str, _Connection] = {}
        self._members: Dict[str, _Member] = {}
        self._reap: Dict[str, Set[str]] = {}  # runner_id -> terminal ids to kill on sight
        self._reaping: Set[Tuple[str, str]] = set()
        self._routes: Dict[WindowKey, _Route] = {}
        self._reservations: Dict[str, int] = {}
        self._sinks: Dict[str, _PipeSink] = {}  # pipe_id -> sink
        self._pipe_of: Dict[Tuple[str, str, str], str] = {}  # (runner, s, w) -> pipe_id
        # Sinks closed but possibly still finishing a write, per window, so the
        # next sink for that window can wait for them before writing.
        self._closing: Dict[Tuple[str, str, str], List[_PipeSink]] = {}
        self._last_seq: Dict[str, int] = {}  # pipe_id -> last seq
        self._placed_at: Dict[str, float] = {}  # terminal_id -> monotonic placement time
        self._unowned_strikes: Dict[Tuple[str, str], int] = {}
        # Bound by the backend (see bind()); the hub itself knows no database.
        self._ownership: Optional[Callable[[str, str], Optional[bool]]] = None
        self._relocate: Optional[Callable[[str, str], None]] = None
        self._reachability: Optional[Callable[[List[str], bool], None]] = None
        self._rearm: Optional[Callable[[List[Tuple[str, str, str]]], None]] = None
        self._reconcile_lock = threading.Lock()
        # runner_id -> (reports per terminal, relocated ids); insertion-ordered,
        # served oldest first.
        self._reconcile_pending: Dict[str, Tuple["collections.Counter[str]", Set[str]]] = {}
        self._reconcile_running = False

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._started = threading.Event()
        self._start_error: Optional[BaseException] = None
        self._load_state()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Unreadable membership means absence can never be proven from it;
            # starting empty would prove too much. Fail loudly instead.
            raise RuntimeError(f"runner hub state {self._state_path} is unreadable: {exc}") from exc
        for runner_id, entry in (data.get("members") or {}).items():
            self._members[runner_id] = _Member(
                claims=frozenset(entry.get("claims") or []),
                last_seen=float(entry.get("last_seen") or 0.0),
            )
        for runner_id, ids in (data.get("reap") or {}).items():
            if ids:
                self._reap[runner_id] = set(ids)

    def _save_state_locked(self) -> bool:
        """Persist membership and the reap set (placements live in the terminal rows).

        Returns False (and logs at ERROR) if the write failed: the in-memory
        state is still authoritative for this process, but a restart would lose
        whatever this write carried.
        """
        if self._state_path is None:
            return True
        data = {
            "members": {
                rid: {"claims": sorted(m.claims), "last_seen": m.last_seen}
                for rid, m in self._members.items()
            },
            "reap": {rid: sorted(ids) for rid, ids in self._reap.items() if ids},
        }
        tmp = self._state_path.with_suffix(f".tmp.{os.getpid()}")
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._state_path)
        except OSError as exc:
            logger.error(f"could not persist runner hub state: {exc}")
            tmp.unlink(missing_ok=True)
            return False
        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the listener thread; returns once the socket is bound."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._thread_main, name="runner-hub", daemon=True)
        self._thread.start()
        self._started.wait(timeout=_START_TIMEOUT)
        if self._start_error is not None:
            raise self._start_error
        logger.info(f"runner hub listening on {self.host}:{self.port}")

    def stop(self) -> None:
        loop, stop_event = self._loop, self._stop_event
        if loop is not None and stop_event is not None and not loop.is_closed():
            loop.call_soon_threadsafe(stop_event.set)
        if self._thread is not None:
            self._thread.join(timeout=_STOP_TIMEOUT)
        self._thread = None
        with self._lock:
            sinks = list(self._sinks.values())
            self._sinks.clear()
            self._pipe_of.clear()
        for sink in sinks:
            sink.close()

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except BaseException as exc:  # surfaced to start() if it never bound
            if not self._started.is_set():
                self._start_error = exc
                self._started.set()
            else:
                logger.exception("runner hub stopped unexpectedly")
        finally:
            loop.close()

    async def _serve(self) -> None:
        from websockets.asyncio.server import serve

        self._stop_event = asyncio.Event()
        tls: Dict[str, Any] = {"ssl": self._ssl_context} if self._ssl_context is not None else {}
        async with serve(
            self._handle,
            self.host,
            self.port,
            process_request=self._authenticate,
            max_size=MAX_FRAME_BYTES,
            ping_interval=PING_INTERVAL,
            ping_timeout=PING_TIMEOUT,
            **tls,
        ) as server:
            sockets = list(server.sockets)
            if sockets and self.port == 0:
                self.port = sockets[0].getsockname()[1]
            self._started.set()
            await self._stop_event.wait()

    def _authenticate(self, connection: Any, request: Any) -> Any:
        """Reject the WebSocket handshake unless it carries the runner token."""
        header = request.headers.get(TOKEN_HEADER, "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            presented.encode(), self._token.encode()
        ):
            return connection.respond(HTTPStatus.UNAUTHORIZED, "runner token required\n")
        return None

    # ------------------------------------------------------------------
    # Connection handling (hub thread only)
    # ------------------------------------------------------------------

    async def _handle(self, ws: Any) -> None:
        hello = await self._receive_hello(ws)
        if hello is None:
            await ws.close(CLOSE_BAD_HELLO, "expected hello")
            return
        info = self._parse_hello(hello)
        if info is None:
            await ws.close(CLOSE_BAD_HELLO, "invalid hello")
            return
        if hello.get("protocol") != PROTOCOL_VERSION:
            await ws.close(
                CLOSE_PROTOCOL_MISMATCH,
                f"server speaks runner protocol {PROTOCOL_VERSION}",
            )
            return

        conn = _Connection(info=info, ws=ws)
        with self._changed:
            existing = self._conns.get(info.runner_id)
            if existing is not None and not existing.closed:
                # A live connection is never displaced by a newcomer presenting
                # the same id: that would hand the newcomer every command meant
                # for the runner. A half-open socket is reaped by the ping
                # timeout, after which the genuine runner's retry gets in.
                refuse = True
            else:
                refuse = False
                self._conns[info.runner_id] = conn
                self._members[info.runner_id] = _Member(
                    claims=info.claims, last_seen=time.time(), reported=True
                )
                to_reap, reported, relocated, gone = self._replace_runner_routes(
                    info.runner_id, hello.get("terminals") or [], hello.get("creating") or []
                )
                lost_pipes = self._lost_pipes_locked(info.runner_id, hello.get("pipes"))
                self._save_state_locked()
                self._changed.notify_all()
        if refuse:
            logger.warning(f"refused a second connection for live runner {info.runner_id}")
            await ws.close(CLOSE_REPLACED, "runner id already connected")
            return
        logger.info(
            f"runner {info.runner_id} connected (claims={sorted(info.claims)}, "
            f"pool={info.accepts_unclaimed}, max_terminals={info.max_terminals})"
        )
        await ws.send(json.dumps({"type": "welcome", "protocol": PROTOCOL_VERSION}))
        self._after_report(info.runner_id, reported, relocated, gone)
        self._start_reaping(info.runner_id, to_reap)
        self._start_rearm(info.runner_id, lost_pipes)

        try:
            async for raw in ws:
                self._on_frame(conn, raw)
        except Exception as exc:
            logger.info(f"runner {info.runner_id} connection ended: {exc}")
        finally:
            unreachable: List[str] = []
            with self._changed:
                if self._conns.get(info.runner_id) is conn:
                    del self._conns[info.runner_id]
                    member = self._members.get(info.runner_id)
                    if member is not None:
                        member.last_seen = time.time()
                    self._save_state_locked()
                    unreachable = [
                        r.terminal_id
                        for r in self._routes.values()
                        if r.runner_id == info.runner_id and r.terminal_id
                    ]
                self._fail_pending(conn, "runner disconnected before replying")
                self._changed.notify_all()
            # Their last detected status describes panes nobody can see now.
            self._notify_reachability(unreachable, False)
            logger.info(f"runner {info.runner_id} disconnected")

    @staticmethod
    async def _receive_hello(ws: Any) -> Optional[Dict[str, Any]]:
        """The first frame as a JSON object, or None if it is missing or garbled."""
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=HANDSHAKE_TIMEOUT)
        except Exception:
            return None
        return _decode_frame(raw)

    @staticmethod
    def _parse_hello(hello: Any) -> Optional[RunnerInfo]:
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            return None
        runner_id = hello.get("runner_id")
        if not isinstance(runner_id, str) or not RUNNER_ID_PATTERN.match(runner_id):
            return None
        claims = hello.get("claims") or []
        max_terminals = hello.get("max_terminals")
        tmp_dir = hello.get("tmp_dir")
        if (
            not isinstance(claims, list)
            or not all(isinstance(c, str) and c for c in claims)
            or not isinstance(max_terminals, int)
            or max_terminals < 1
            or not isinstance(tmp_dir, str)
            or not os.path.isabs(tmp_dir)
        ):
            return None
        return RunnerInfo(
            runner_id=runner_id,
            claims=frozenset(claims),
            accepts_unclaimed=bool(hello.get("accepts_unclaimed", True)),
            max_terminals=max_terminals,
            tmp_dir=tmp_dir,
        )

    def _on_frame(self, conn: _Connection, raw: Any) -> None:
        frame = _decode_frame(raw)
        if frame is None:
            logger.warning(f"runner {conn.info.runner_id}: dropped a non-JSON frame")
            return
        kind = frame.get("type")
        if kind == "result":
            with self._lock:
                future = conn.pending.pop(str(frame.get("id")), None)
            if future is not None and not future.done():
                future.set_result(frame)
        elif kind == "output":
            self._on_output(conn.info.runner_id, frame)
        elif kind == "inventory":
            with self._changed:
                to_reap, reported, relocated, gone = self._reconcile_inventory(
                    conn.info.runner_id, frame.get("terminals") or [], frame.get("creating") or []
                )
                member = self._members.get(conn.info.runner_id)
                if member is not None:
                    member.last_seen = time.time()
                # Persisted on every heartbeat: after a crash, last_seen and the
                # last reported terminals are what expiry and reaping rely on.
                self._save_state_locked()
                self._changed.notify_all()
            self._after_report(conn.info.runner_id, reported, relocated, gone)
            self._start_reaping(conn.info.runner_id, to_reap)
        else:
            logger.warning(f"runner {conn.info.runner_id}: unknown frame type {kind!r}")

    def _on_output(self, runner_id: str, frame: Dict[str, Any]) -> None:
        pipe_id, seq = frame.get("pipe_id"), frame.get("seq")
        if not isinstance(pipe_id, str) or not isinstance(seq, int):
            return
        with self._lock:
            sink = self._sinks.get(pipe_id)
            if sink is None or sink.runner_id != runner_id:
                # A pipe that was closed (or never opened here): its bytes
                # belong to an earlier incarnation and must not reach the FIFO
                # of whatever terminal now has that name.
                return
            last = self._last_seq.get(pipe_id)
            if last is not None and seq <= last:
                # A replay of a frame already written (it was delivered just
                # before a disconnect, then re-sent from the runner's outbox).
                return
            self._last_seq[pipe_id] = seq
        expected = 1 if last is None else last + 1
        if seq != expected:
            # A gap is reported, never papered over: the bytes in between are
            # not coming back, and status derived from this stream may be built
            # on incomplete output.
            logger.warning(
                f"runner {runner_id}: output gap on pipe {pipe_id} "
                f"(expected seq {expected}, got {seq})"
            )
        try:
            data = base64.b64decode(frame.get("data") or "", validate=True)
        except (ValueError, TypeError):
            logger.warning(f"runner {runner_id}: undecodable output frame on pipe {pipe_id}")
            return
        if data:
            sink.feed(data)

    def _fail_pending(self, conn: _Connection, reason: str) -> None:
        conn.closed = True
        pending, conn.pending = conn.pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(
                    RunnerOutcomeUnknown(f"runner {conn.info.runner_id}: {reason}; outcome unknown")
                )

    # ------------------------------------------------------------------
    # Routing, membership, reaping (callers hold self._lock)
    # ------------------------------------------------------------------

    def _replace_runner_routes(
        self, runner_id: str, terminals: List[Any], creating: List[Any]
    ) -> Tuple[List[str], List[str], List[str], List[str]]:
        """Adopt a runner's full report: (to reap, reported, relocated, gone) ids."""
        reported = self._parse_terminals(terminals)
        gone: List[str] = []
        for key in [k for k, r in self._routes.items() if r.runner_id == runner_id]:
            if key not in reported:
                # Placed here (its row or an earlier report says so) but not
                # running here any more: the pane is gone. The row is left to
                # the ordinary teardown; status reads UNKNOWN (_after_report).
                gone.extend(self._drop_route_locked(key))
        relocated: List[str] = []
        for key, terminal_id in reported.items():
            existing = self._routes.get(key)
            if existing is not None and existing.runner_id != runner_id:
                # The report wins: the reporting runner is the one holding the
                # pane. The row is corrected to match (see _after_report).
                logger.warning(
                    f"runner {runner_id} reports {key[0]}:{key[1]}, previously routed to "
                    f"{existing.runner_id}; the reporting runner wins"
                )
                if terminal_id:
                    relocated.append(terminal_id)
            gone.extend(self._displace_locked(key, runner_id, terminal_id))
            self._routes[key] = _Route(runner_id=runner_id, terminal_id=terminal_id)
        ids = [tid for tid in reported.values() if tid]
        return (
            self._settle_reap_locked(runner_id, reported, creating, full_report=True),
            ids,
            relocated,
            gone,
        )

    def _reconcile_inventory(
        self, runner_id: str, terminals: List[Any], creating: List[Any]
    ) -> Tuple[List[str], List[str], List[str], List[str]]:
        """Fold a periodic report in: (to reap, reported, relocated, gone) ids."""
        reported = self._parse_terminals(terminals)
        gone: List[str] = []
        for key, route in list(self._routes.items()):
            if route.runner_id != runner_id:
                continue
            if key in reported:
                route.inventory_misses = 0
                continue
            route.inventory_misses += 1
            if route.inventory_misses >= _INVENTORY_MISSES_BEFORE_DROP:
                logger.info(f"runner {runner_id} no longer hosts {key[0]}:{key[1]}; dropping route")
                gone.extend(self._drop_route_locked(key))
        relocated: List[str] = []
        for key, terminal_id in reported.items():
            existing = self._routes.get(key)
            if (
                existing is None
                or existing.runner_id != runner_id
                or existing.terminal_id != terminal_id
            ):
                if existing is not None and existing.runner_id != runner_id and terminal_id:
                    relocated.append(terminal_id)  # same rule as a hello
                gone.extend(self._displace_locked(key, runner_id, terminal_id))
                self._routes[key] = _Route(runner_id=runner_id, terminal_id=terminal_id)
        ids = [tid for tid in reported.values() if tid]
        return (
            self._settle_reap_locked(runner_id, reported, creating, full_report=False),
            ids,
            relocated,
            gone,
        )

    def _settle_reap_locked(
        self,
        runner_id: str,
        reported: Dict[WindowKey, Optional[str]],
        creating: List[Any],
        *,
        full_report: bool,
    ) -> List[str]:
        """Terminal ids on this runner to kill now; forget those proven gone."""
        pending = self._reap.get(runner_id)
        if not pending:
            return []
        present = {tid for tid in reported.values() if tid}
        in_flight = {tid for tid in creating if isinstance(tid, str)}
        if full_report:
            # A hello is the runner's complete truth: anything it neither
            # reports nor is still creating is gone and needs no reaping. A
            # create still in flight keeps its marker until the pane exists.
            gone = pending - present - in_flight
            if gone:
                pending -= gone
                self._save_state_locked()
        return sorted(pending & present)

    @staticmethod
    def _parse_terminals(terminals: List[Any]) -> Dict[WindowKey, Optional[str]]:
        parsed: Dict[WindowKey, Optional[str]] = {}
        for entry in terminals:
            if not isinstance(entry, dict):
                continue
            session, window = entry.get("session"), entry.get("window")
            if isinstance(session, str) and isinstance(window, str):
                terminal_id = entry.get("terminal_id")
                parsed[(session, window)] = terminal_id if isinstance(terminal_id, str) else None
        return parsed

    def _displace_locked(
        self, key: WindowKey, runner_id: str, terminal_id: Optional[str]
    ) -> List[str]:
        """Retire a route a report is replacing; the displaced terminal if it differs.

        Its sink belongs to the old runner or incarnation and must stop feeding
        the FIFO, and a different terminal it named is no longer in reach.
        """
        existing = self._routes.get(key)
        if existing is None or (
            existing.runner_id == runner_id and existing.terminal_id == terminal_id
        ):
            return []
        dropped = self._drop_route_locked(key)
        return [t for t in dropped if t != terminal_id]

    def _drop_route_locked(self, key: WindowKey) -> List[str]:
        """Forget a route and close its pipe; the dropped terminal id, if any."""
        route = self._routes.pop(key, None)
        if route is None:
            return []
        pipe_key = (route.runner_id, key[0], key[1])
        pipe_id = self._pipe_of.pop(pipe_key, None)
        if pipe_id is not None:
            sink = self._sinks.pop(pipe_id, None)
            self._last_seq.pop(pipe_id, None)
            if sink is not None:
                sink.close()
                still_closing = [c for c in self._closing.get(pipe_key, []) if c.is_alive()]
                self._closing[pipe_key] = still_closing + [sink]
        return [route.terminal_id] if route.terminal_id else []

    def _expire_members_locked(self) -> None:
        now = time.time()
        expired = [
            rid
            for rid, m in self._members.items()
            if rid not in self._conns and now - m.last_seen > self.expiry
        ]
        for runner_id in expired:
            del self._members[runner_id]
            lost = [(k, r) for k, r in self._routes.items() if r.runner_id == runner_id]
            for key, route in lost:
                if route.terminal_id:
                    self._reap.setdefault(runner_id, set()).add(route.terminal_id)
                self._drop_route_locked(key)
            logger.warning(
                f"runner {runner_id} silent for over {self.expiry:.0f}s; expired, "
                f"{len(lost)} terminal(s) declared lost"
            )
        if expired:
            self._save_state_locked()

    def _load_locked(self, runner_id: str) -> int:
        routed = {r.terminal_id for r in self._routes.values() if r.runner_id == runner_id}
        hosted = sum(1 for r in self._routes.values() if r.runner_id == runner_id)
        # A terminal awaiting a reap may be occupying a slot (a create whose
        # reply was lost is still running there); it counts until it is gone.
        unconfirmed = len(self._reap.get(runner_id, set()) - routed)
        return hosted + unconfirmed + self._reservations.get(runner_id, 0)

    def _choose_locked(self, session_name: str, new_session: bool) -> Optional[str]:
        self._expire_members_locked()
        live = [c for c in self._conns.values() if not c.closed]

        def has_room(conn: _Connection) -> bool:
            return self._load_locked(conn.info.runner_id) < conn.info.max_terminals

        claimed = session_name in self.reserved_sessions or any(
            session_name in m.claims for m in self._members.values()
        )
        claimants = [c for c in live if session_name in c.info.claims]
        if new_session and claimed:
            # A claimed session goes to its claimant or nowhere — including
            # while the claimant is not connected yet. A claim is how an
            # operator pins a session (a supervisor) to one runner, and
            # spilling it onto a pool runner would put the agent in the wrong
            # pod while reporting success.
            candidates = [c for c in claimants if has_room(c)]
        else:
            hosting = {r.runner_id for k, r in self._routes.items() if k[0] == session_name}
            candidates = [c for c in live if c.info.runner_id in hosting and has_room(c)]
            if not candidates:
                candidates = [
                    c for c in live if (c.info.accepts_unclaimed or c in claimants) and has_room(c)
                ]
        if not candidates:
            return None
        return min(candidates, key=lambda c: self._load_locked(c.info.runner_id)).info.runner_id

    # ------------------------------------------------------------------
    # Durable placement, ownership and reachability
    # ------------------------------------------------------------------

    def bind(
        self,
        *,
        ownership: Optional[Callable[[str, str], Optional[bool]]] = None,
        relocate: Optional[Callable[[str, str], None]] = None,
        reachability: Optional[Callable[[List[str], bool], None]] = None,
        rearm: Optional[Callable[[List[Tuple[str, str, str]]], None]] = None,
    ) -> None:
        """Connect the hub to the server's registry and status detector.

        ``ownership(terminal_id, runner_id)`` answers whether a registry row
        exists (None when it cannot tell), recording ``runner_id`` on a row that
        names no runner or another one; ``relocate(terminal_id, runner_id)`` corrects a
        row's placement; ``reachability(terminal_ids, reachable)`` tells the
        status detector a runner went out of (or back in) reach;
        ``rearm([(session, window, fifo_path)])`` re-opens the output pipes a
        restarted runner process no longer has. The hub stays free of database
        and status imports, and testable without them.
        """
        self._ownership = ownership
        self._relocate = relocate
        self._reachability = reachability
        self._rearm = rearm

    def seed_placements(self, placements: Iterable[Dict[str, str]]) -> None:
        """Restore routes from the registry's persisted placements at startup.

        Every runner named there becomes a member that has not reported yet, so
        nothing it may host is provable absent until it reconnects or expires —
        even if ``runner-hub.json`` was lost. Until its runner reports, each
        seeded terminal is out of reach and reads UNKNOWN.
        """
        seeded: List[str] = []
        with self._changed:
            for row in placements:
                runner_id, terminal_id = row.get("runner_id"), row.get("id")
                session, window = row.get("tmux_session"), row.get("tmux_window")
                if not (runner_id and terminal_id and session and window):
                    continue
                if (session, window) in self._routes:
                    continue  # a live report beat the seed; it is the fresher truth
                self._routes[(session, window)] = _Route(runner_id, terminal_id)
                if runner_id not in self._members:
                    self._members[runner_id] = _Member(claims=frozenset(), last_seen=time.time())
                if runner_id not in self._conns:
                    seeded.append(terminal_id)
            self._save_state_locked()
            self._changed.notify_all()
        self._notify_reachability(seeded, False)

    def note_placing(self, terminal_id: str) -> None:
        """Record that ``terminal_id`` is being placed (its row is not written yet)."""
        with self._lock:
            now = time.monotonic()
            self._placed_at[terminal_id] = now
            for stale in [t for t, at in self._placed_at.items() if now - at > _PLACEMENT_GRACE]:
                del self._placed_at[stale]

    def _lost_pipes_locked(self, runner_id: str, pipes: Any) -> List[Tuple[str, str, str]]:
        """Open sinks for this runner's routed windows whose pipe it no longer runs.

        A reconnect of the same process keeps its pipes (and replays their
        buffered output). A NEW process that re-adopted surviving panes has
        none, so without re-arming, the server would detect status from a
        stream that silently stopped. A runner that does not list its pipes
        is taken to have them all.
        """
        if not isinstance(pipes, list):
            return []
        live = {p for p in pipes if isinstance(p, str)}
        lost: List[Tuple[str, str, str]] = []
        for (rid, session, window), pipe_id in self._pipe_of.items():
            sink = self._sinks.get(pipe_id)
            if rid != runner_id or pipe_id in live or sink is None:
                continue
            if (session, window) in self._routes:
                lost.append((session, window, sink.fifo_path))
        return lost

    def _start_rearm(self, runner_id: str, lost: List[Tuple[str, str, str]]) -> None:
        if not lost or self._rearm is None:
            return
        logger.info(f"runner {runner_id} lost {len(lost)} output pipe(s); re-arming")
        rearm = self._rearm

        def run() -> None:
            try:
                rearm(lost)
            except Exception:  # noqa: BLE001 - retried on the next reconnect
                logger.exception(f"could not re-arm output pipes on runner {runner_id}")

        # pipe_pane is a hub call, which the hub thread itself may not make.
        threading.Thread(target=run, name=f"runner-rearm-{runner_id}", daemon=True).start()

    def _notify_reachability(self, terminal_ids: List[str], reachable: bool) -> None:
        if not terminal_ids or self._reachability is None:
            return
        try:
            self._reachability(terminal_ids, reachable)
        except Exception:  # noqa: BLE001 - status is advisory; routing must go on
            logger.exception("runner hub: reachability callback failed")

    def _after_report(
        self, runner_id: str, reported: List[str], relocated: List[str], gone: List[str]
    ) -> None:
        """Bring the registry and status in line with a runner's report.

        Reachability is immediate (status reads stop answering UNKNOWN); row
        corrections and the ownership check touch the database, so they run on
        a worker thread, never on the hub loop.
        """
        self._notify_reachability(reported, True)
        # A pane its runner no longer has: its last status describes nothing.
        self._notify_reachability(gone, False)
        if not reported or (self._ownership is None and self._relocate is None):
            return
        # Coalesced onto ONE worker: a stalled database must not gain a
        # blocked thread per heartbeat. Reports queued for the same runner
        # merge, keeping how many reports named each terminal so the
        # consecutive-reports reaping rule still counts every one.
        with self._lock:
            counts, moved = self._reconcile_pending.setdefault(
                runner_id, (collections.Counter(), set())
            )
            counts.update(set(reported))
            moved.update(relocated)
            if self._reconcile_running:
                return
            self._reconcile_running = True
        threading.Thread(
            target=self._reconcile_worker, name="runner-reconcile", daemon=True
        ).start()

    def _reconcile_worker(self) -> None:
        while True:
            with self._lock:
                if not self._reconcile_pending:
                    self._reconcile_running = False
                    return
                # Oldest first: a runner that keeps reporting keeps its place
                # in line instead of starving the others.
                runner_id = next(iter(self._reconcile_pending))
                counts, moved = self._reconcile_pending.pop(runner_id)
            try:
                self._reconcile_registry(runner_id, sorted(counts), sorted(moved), counts)
            except Exception:  # noqa: BLE001 - the next report retries
                logger.exception(f"registry reconciliation for runner {runner_id} failed")

    def _reconcile_registry(
        self,
        runner_id: str,
        reported: List[str],
        relocated: List[str],
        report_counts: Optional[Dict[str, int]] = None,
    ) -> None:
        # One reconciliation at a time, and only for terminals still routed to
        # the reporting runner: a stalled pass for a runner that has since lost
        # a terminal must not write its stale placement back into the row.
        with self._reconcile_lock:
            self._reconcile_registry_serialized(runner_id, reported, relocated, report_counts)

    def _route_owner(self, terminal_id: str) -> Optional[str]:
        with self._lock:
            for r in self._routes.values():
                if r.terminal_id == terminal_id:
                    return r.runner_id
        return None

    def _settle_row(self, terminal_id: str, written_for: str) -> None:
        """Re-point a row just written for ``written_for`` if its route moved meanwhile.

        The database write happens outside the hub lock, so a report can move
        the terminal between the check and the write; the route, not the
        stale pass, decides what the row ends up saying.
        """
        if self._relocate is None:
            return
        for _ in range(3):
            owner = self._route_owner(terminal_id)
            if owner is None or owner == written_for:
                return
            try:
                self._relocate(terminal_id, owner)
            except Exception:  # noqa: BLE001 - retried on the next report
                logger.exception(f"could not record {terminal_id} on runner {owner}")
                return
            written_for = owner

    def _routed_to(self, terminal_id: str, runner_id: str) -> bool:
        with self._lock:
            return any(
                r.terminal_id == terminal_id and r.runner_id == runner_id
                for r in self._routes.values()
            )

    def _reconcile_registry_serialized(
        self,
        runner_id: str,
        reported: List[str],
        relocated: List[str],
        report_counts: Optional[Dict[str, int]] = None,
    ) -> None:
        if self._relocate is not None:
            for terminal_id in relocated:
                if not self._routed_to(terminal_id, runner_id):
                    continue
                try:
                    self._relocate(terminal_id, runner_id)
                except Exception:  # noqa: BLE001 - retried on the next report
                    logger.exception(f"could not record {terminal_id} on runner {runner_id}")
                    continue
                self._settle_row(terminal_id, runner_id)
        if self._ownership is None:
            return
        for terminal_id in reported:
            if not self._routed_to(terminal_id, runner_id):
                continue
            with self._lock:
                placed_at = self._placed_at.get(terminal_id)
            if placed_at is not None and time.monotonic() - placed_at < _PLACEMENT_GRACE:
                continue  # its row may simply not be written yet
            try:
                owned = self._ownership(terminal_id, runner_id)
            except Exception:  # noqa: BLE001 - unknown is not "unowned"
                owned = None
            if owned is None:
                continue
            key = (runner_id, terminal_id)
            if owned:
                # The ownership check may have written this runner into the row.
                self._settle_row(terminal_id, runner_id)
            with self._lock:
                if owned:
                    self._unowned_strikes.pop(key, None)
                    continue
                seen = (report_counts or {}).get(terminal_id, 1)
                strikes = self._unowned_strikes.get(key, 0) + max(1, seen)
                self._unowned_strikes[key] = strikes
            if strikes >= _UNOWNED_REPORTS_BEFORE_REAP:
                logger.warning(
                    f"runner {runner_id} runs terminal {terminal_id}, which has no registry "
                    f"row in {strikes} consecutive reports; reaping it"
                )
                with self._lock:
                    self._unowned_strikes.pop(key, None)
                self.mark_for_reaping(runner_id, terminal_id)

    # ------------------------------------------------------------------
    # Reaping (runs on worker threads, never the hub thread)
    # ------------------------------------------------------------------

    def mark_for_reaping(self, runner_id: str, terminal_id: str) -> None:
        """Record a terminal whose fate is unconfirmed, and reap it when seen.

        Used when the server gives up on a terminal it may have left running:
        a create whose reply was lost, a kill the runner never confirmed. The
        terminal is killed the next time its runner reports it, even across a
        server restart (the set is persisted).
        """
        with self._lock:
            self._reap.setdefault(runner_id, set()).add(terminal_id)
            if not self._save_state_locked():
                logger.error(
                    f"terminal {terminal_id} on runner {runner_id} is queued for reaping in "
                    "memory only; a server restart before the runner reports it would lose it"
                )
            connected = runner_id in self._conns and not self._conns[runner_id].closed
        if connected:
            self._start_reaping(runner_id, [terminal_id])

    def _start_reaping(self, runner_id: str, terminal_ids: List[str]) -> None:
        for terminal_id in terminal_ids:
            with self._lock:
                if (runner_id, terminal_id) in self._reaping:
                    continue
                self._reaping.add((runner_id, terminal_id))
            threading.Thread(
                target=self._reap_one,
                args=(runner_id, terminal_id),
                name=f"runner-reap-{terminal_id}",
                daemon=True,
            ).start()

    def _reap_one(self, runner_id: str, terminal_id: str) -> None:
        try:
            value = self.call(
                runner_id,
                "cleanup_terminal_exact",
                {
                    "terminal_id": terminal_id,
                    "session_name": None,
                    "window_name": None,
                    "close": True,
                },
            )
            reclaimed = isinstance(value, dict) and value.get("outcome") in ("deleted", "absent")
        except (TerminalBackendError, ValueError) as exc:
            logger.warning(f"reaping terminal {terminal_id} on runner {runner_id} failed: {exc}")
            reclaimed = False
        with self._changed:
            self._reaping.discard((runner_id, terminal_id))
            if reclaimed:
                pending = self._reap.get(runner_id)
                if pending is not None:
                    pending.discard(terminal_id)
                    if not pending:
                        del self._reap[runner_id]
                for key in [
                    k
                    for k, r in self._routes.items()
                    if r.runner_id == runner_id and r.terminal_id == terminal_id
                ]:
                    self._drop_route_locked(key)
                self._save_state_locked()
                logger.info(f"reaped unowned terminal {terminal_id} on runner {runner_id}")
            self._changed.notify_all()

    # ------------------------------------------------------------------
    # Public API (any thread except the hub thread)
    # ------------------------------------------------------------------

    def reserve(self, session_name: str, *, new_session: bool) -> str:
        """Pick a runner for a new terminal and hold a capacity slot on it.

        Waits up to ``placement_timeout`` for a runner with room to connect —
        a worker pod is usually still starting when the request that needs it
        arrives. The caller MUST pair this with :meth:`release_reservation`.
        """
        deadline = time.monotonic() + self.placement_timeout
        with self._changed:
            while True:
                runner_id = self._choose_locked(session_name, new_session)
                if runner_id is not None:
                    self._reservations[runner_id] = self._reservations.get(runner_id, 0) + 1
                    return runner_id
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RunnerUnavailable(
                        f"no connected runner has room for session '{session_name}' "
                        f"after {self.placement_timeout:.0f}s"
                    )
                self._changed.wait(remaining)

    def release_reservation(self, runner_id: str) -> None:
        with self._changed:
            count = self._reservations.get(runner_id, 0) - 1
            if count > 0:
                self._reservations[runner_id] = count
            else:
                self._reservations.pop(runner_id, None)
            self._changed.notify_all()

    def add_route(
        self, session_name: str, window_name: str, runner_id: str, terminal_id: str
    ) -> bool:
        """Route a terminal just created; False if a report already routed another there.

        The report wins: a runner that has reported a different terminal in
        this window since the create replied is the fresher truth, and the
        caller must treat its own terminal as unconfirmed.
        """
        with self._lock:
            existing = self._routes.get((session_name, window_name))
            if existing is not None and (
                existing.runner_id != runner_id or existing.terminal_id != terminal_id
            ):
                return False
            self._routes[(session_name, window_name)] = _Route(runner_id, terminal_id)
            # Saved now, not at the next heartbeat: a crash in between would
            # forget a live terminal, and a partitioned runner's expiry could
            # then never queue it for reaping.
            self._save_state_locked()
        return True

    def drop_route(
        self,
        session_name: str,
        window_name: str,
        runner_id: Optional[str] = None,
        terminal_id: Optional[str] = None,
    ) -> None:
        """Forget a window's route; with ids given, only if it is still that one.

        A kill's reply can land after a report has already routed the window to
        a newer incarnation (or another runner); that route must survive.
        """
        with self._changed:
            route = self._routes.get((session_name, window_name))
            if route is None:
                return
            if runner_id is not None and route.runner_id != runner_id:
                return
            if terminal_id is not None and route.terminal_id != terminal_id:
                return
            self._drop_route_locked((session_name, window_name))
            self._save_state_locked()
            self._changed.notify_all()

    def drop_session_routes(self, session_name: str, runner_id: str) -> None:
        with self._changed:
            for key in [
                k
                for k, r in self._routes.items()
                if k[0] == session_name and r.runner_id == runner_id
            ]:
                self._drop_route_locked(key)
            self._save_state_locked()
            self._changed.notify_all()

    # Every route read first drops members past their expiry: otherwise a dead
    # runner's routes keep a session "existing" (and its kill unconfirmed)
    # until some unrelated placement happens to run the expiry.

    def routed_sessions(self) -> List[str]:
        with self._lock:
            self._expire_members_locked()
            return sorted({k[0] for k in self._routes})

    def route(self, session_name: str, window_name: str) -> Optional[Tuple[str, Optional[str]]]:
        """``(runner_id, terminal_id)`` hosting a window, or None if unrouted."""
        with self._lock:
            self._expire_members_locked()
            route = self._routes.get((session_name, window_name))
            return (route.runner_id, route.terminal_id) if route else None

    def route_for_terminal(self, terminal_id: str) -> Optional[Tuple[str, str, str]]:
        with self._lock:
            self._expire_members_locked()
            for (session, window), route in self._routes.items():
                if route.terminal_id == terminal_id:
                    return route.runner_id, session, window
        return None

    def session_routes(self, session_name: str) -> List[Tuple[str, str, Optional[str]]]:
        """``(runner_id, window, terminal_id)`` for every routed window of a session."""
        with self._lock:
            self._expire_members_locked()
            return [
                (r.runner_id, k[1], r.terminal_id)
                for k, r in self._routes.items()
                if k[0] == session_name
            ]

    def session_runners(self, session_name: str) -> List[str]:
        with self._lock:
            self._expire_members_locked()
            return sorted({r.runner_id for k, r in self._routes.items() if k[0] == session_name})

    def runner_info(self, runner_id: str) -> Optional[RunnerInfo]:
        with self._lock:
            conn = self._conns.get(runner_id)
            return conn.info if conn is not None else None

    def connected_runners(self) -> List[str]:
        with self._lock:
            return sorted(rid for rid, c in self._conns.items() if not c.closed)

    def is_reaping(self, terminal_id: str) -> bool:
        with self._lock:
            return any(terminal_id in ids for ids in self._reap.values())

    def can_prove_unrouted_absence(self) -> bool:
        """Whether "no route" may be read as "no such terminal anywhere".

        Routes are the runners' own reports, so they are complete exactly when
        every runner that might host something has reported to this process —
        connected now or not. A member loaded from disk that has not reconnected
        since a restart is the gap; it closes when the member reports or
        expires. A hub that knows no runner at all cannot prove anything.
        """
        with self._lock:
            self._expire_members_locked()
            if not self._members:
                return False
            return all(m.reported for m in self._members.values())

    def describe(self) -> List[Dict[str, Any]]:
        """Operator view of connected runners (for ``GET /runners``)."""
        with self._lock:
            return [
                {
                    "runner_id": rid,
                    "claims": sorted(c.info.claims),
                    "accepts_unclaimed": c.info.accepts_unclaimed,
                    "max_terminals": c.info.max_terminals,
                    "terminals": sorted(
                        f"{k[0]}:{k[1]}" for k, r in self._routes.items() if r.runner_id == rid
                    ),
                    "reserved": self._reservations.get(rid, 0),
                    "reaping": sorted(self._reap.get(rid, set())),
                }
                for rid, c in sorted(self._conns.items())
                if not c.closed
            ]

    def open_pipe(
        self,
        runner_id: str,
        session_name: str,
        window_name: str,
        fifo_path: str,
        terminal_id: Optional[str] = None,
    ) -> str:
        """Create the FIFO sink for a new pipe and return its id.

        The id travels with every output frame, so bytes from an earlier pipe
        on the same window — still in flight, or replayed after a reconnect —
        are refused instead of written into this terminal's FIFO. A previous
        sink for the window is closed and joined first, so two writers never
        interleave on one FIFO.
        """
        key = (runner_id, session_name, window_name)
        with self._lock:
            if not self._route_is_locked(session_name, window_name, runner_id, terminal_id):
                # Re-routed since the caller resolved it (another runner or a
                # newer incarnation won): no sink for the stale pane.
                raise ValueError(
                    f"Window '{window_name}' not found in session '{session_name}' "
                    f"on runner {runner_id}"
                )
            previous_id = self._pipe_of.pop(key, None)
            previous = self._sinks.pop(previous_id, None) if previous_id else None
            waiting = self._closing.pop(key, [])
        if previous is not None:
            previous.close()
            waiting.append(previous)
        for closing in waiting:
            closing.join()
        pipe_id = uuid.uuid4().hex[:16]
        sink = _PipeSink(runner_id, f"{session_name}:{window_name}", fifo_path)
        with self._lock:
            # Checked again: the lock was released while the previous sink was
            # joined, and a report may have displaced the route since.
            if not self._route_is_locked(session_name, window_name, runner_id, terminal_id):
                displaced = True
            else:
                displaced = False
                self._sinks[pipe_id] = sink
                self._pipe_of[key] = pipe_id
        if displaced:
            sink.close()
            raise ValueError(
                f"Window '{window_name}' not found in session '{session_name}' "
                f"on runner {runner_id}"
            )
        return pipe_id

    def _route_is_locked(
        self, session_name: str, window_name: str, runner_id: str, terminal_id: Optional[str]
    ) -> bool:
        route = self._routes.get((session_name, window_name))
        return (
            route is not None
            and route.runner_id == runner_id
            and (terminal_id is None or route.terminal_id == terminal_id)
        )

    def close_pipe(self, runner_id: str, session_name: str, window_name: str) -> None:
        with self._lock:
            pipe_id = self._pipe_of.pop((runner_id, session_name, window_name), None)
            sink = self._sinks.pop(pipe_id, None) if pipe_id else None
            if pipe_id:
                self._last_seq.pop(pipe_id, None)
        if sink is not None:
            sink.close()
            # Joined here so a stop-then-rearm cannot overlap the two writers.
            sink.join()

    def call(
        self, runner_id: str, op: str, args: Dict[str, Any], timeout: Optional[float] = None
    ) -> Any:
        """Execute ``op`` on a runner and return its value (or raise its error)."""
        if threading.current_thread() is self._thread:
            raise RuntimeError("RunnerHub.call must not run on the hub thread")
        loop = self._loop
        with self._lock:
            conn = self._conns.get(runner_id)
            if conn is None or conn.closed or loop is None:
                raise RunnerUnavailable(f"runner {runner_id} is not connected")
            call_id = uuid.uuid4().hex
            future: concurrent.futures.Future = concurrent.futures.Future()
            conn.pending[call_id] = future
        frame = json.dumps({"type": "call", "id": call_id, "op": op, "args": args})

        async def _send() -> None:
            await conn.ws.send(frame)

        sent = asyncio.run_coroutine_threadsafe(_send(), loop)
        try:
            sent.result(timeout=SEND_TIMEOUT)
        except Exception as exc:
            with self._lock:
                conn.pending.pop(call_id, None)
            # A send that failed or timed out may still have reached the wire.
            raise RunnerOutcomeUnknown(
                f"runner {runner_id}: sending {op} failed ({exc}); outcome unknown"
            ) from exc
        try:
            reply = future.result(timeout=timeout if timeout is not None else self.call_timeout)
        except concurrent.futures.TimeoutError as exc:
            with self._lock:
                conn.pending.pop(call_id, None)
            raise RunnerOutcomeUnknown(
                f"runner {runner_id}: {op} timed out; outcome unknown"
            ) from exc
        if reply.get("ok"):
            return reply.get("value")
        raise_remote_error(reply.get("error"), runner_id)
