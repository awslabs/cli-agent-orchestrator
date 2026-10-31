"""``cao-runner``: the execution-only process beside an agent CLI (#745).

It dials out to the central server's runner hub, announces what it can host,
and then does exactly three things:

1. executes the server's terminal operations against its OWN local tmux, via
   the ordinary :class:`~cli_agent_orchestrator.backends.tmux_backend.TmuxBackend`;
2. streams armed panes' output back, tagged with a per-pipe sequence number;
3. writes provider files the server stages into its own ``CAO_HOME_DIR/tmp``.

It runs no ``cao-server`` and holds no CAO state beyond what its tmux panes
need. If the connection drops, its panes keep running and their output is held
in a bounded buffer; on reconnect the runner re-announces its terminals so the
server can route to them again. Output that overflowed the buffer is lost and
shows up on the server as a sequence gap, never as silently missing bytes.
"""

import argparse
import asyncio
import base64
import collections
import json
import logging
import os
import re
import select
import socket
import ssl
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Any, Deque, Dict, Optional, Tuple

from cli_agent_orchestrator.backends.base import TerminalBackendError
from cli_agent_orchestrator.constants import CAO_HOME_DIR, FIFO_DIR
from cli_agent_orchestrator.runner.protocol import (
    CLOSE_BAD_HELLO,
    CLOSE_PROTOCOL_MISMATCH,
    HANDSHAKE_TIMEOUT,
    MAX_FRAME_BYTES,
    MAX_STAGED_FILE_BYTES,
    OPS,
    PING_INTERVAL,
    PING_TIMEOUT,
    PROTOCOL_VERSION,
    RUNNER_ID_PATTERN,
    STAGED_FILE_PATTERN,
    TERMINAL_ID_PATTERN,
    TOKEN_HEADER,
    encode_error,
)
from cli_agent_orchestrator.runner.token import runner_token

if TYPE_CHECKING:
    from cli_agent_orchestrator.backends.base import TerminalBackend

logger = logging.getLogger(__name__)

WindowKey = Tuple[str, str]

# tmux window option naming the runner that created the window (see
# Runner.recover_terminals). Like @cao_terminal_id it is a scope marker, not a
# credential: anything on the tmux socket can set it.
RUNNER_ID_OPTION = "@cao_runner_id"

# Output held while disconnected. Past this, the oldest chunks are dropped; the
# server sees the resulting seq jump and logs a gap.
_OUTBOX_MAX_BYTES = 4 * 1024 * 1024
_READ_CHUNK = 4096
_INVENTORY_INTERVAL = 10.0
# A pane pipe's reader wakes every _SELECT_INTERVAL to check its stop flag, so
# a few intervals is always enough for it to exit.
_SELECT_INTERVAL = 0.25
_PIPE_STOP_TIMEOUT = 8 * _SELECT_INTERVAL
# `tmux list-windows` on the runner's own tmux server: a local call that
# answers in milliseconds; past this the server is wedged and the listing is
# treated as unanswerable (no pruning) rather than as "no windows".
_TMUX_LIST_TIMEOUT = 10.0


class _PanePipe:
    """Reads one pane's pipe-pane FIFO on a thread and hands chunks to ``emit``."""

    def __init__(self, fifo_path: str, pipe_id: str, emit: Any) -> None:
        from cli_agent_orchestrator.services.fifo_reader import _ensure_fifo, _open_fifo

        self.fifo_path = fifo_path
        self.pipe_id = pipe_id
        _ensure_fifo(fifo_path)
        # Same shape as the server's FifoManager: a non-blocking read end plus a
        # keepalive write end, so a pipe-pane writer restart is not an EOF.
        self._read_fd = _open_fifo(fifo_path, os.O_RDONLY | os.O_NONBLOCK)
        self._keepalive_fd = _open_fifo(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
        self._emit = emit
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="runner-pane-pipe", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                ready, _, _ = select.select([self._read_fd], [], [], _SELECT_INTERVAL)
                if not ready:
                    continue
                try:
                    chunk = os.read(self._read_fd, _READ_CHUNK)
                except BlockingIOError:
                    continue
                if chunk:
                    self._emit(chunk)
        finally:
            for fd in (self._read_fd, self._keepalive_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=_PIPE_STOP_TIMEOUT)
        try:
            os.unlink(self.fifo_path)
        except OSError:
            pass


class Runner:
    """One runner process: a connection loop plus local terminal state."""

    def __init__(
        self,
        server_url: str,
        token: str,
        runner_id: str,
        claims: Optional[list] = None,
        accepts_unclaimed: bool = True,
        max_terminals: int = 1,
        backend: Optional["TerminalBackend"] = None,
        tmp_dir: Optional[str] = None,
        ready_file: Optional[str] = None,
        ca_file: Optional[str] = None,
    ) -> None:
        if not RUNNER_ID_PATTERN.match(runner_id):
            raise ValueError(f"invalid runner id {runner_id!r}")
        self.server_url = server_url
        self._token = token
        self.runner_id = runner_id
        self.claims = list(claims or [])
        self.accepts_unclaimed = accepts_unclaimed
        self.max_terminals = max_terminals
        self._backend: Optional["TerminalBackend"] = backend
        # Only a runner driving its own tmux tags and re-adopts its windows.
        self._owns_tmux = backend is None
        self.tmp_dir = tmp_dir or str(CAO_HOME_DIR / "tmp")
        os.makedirs(self.tmp_dir, mode=0o700, exist_ok=True)
        # Exists exactly while a handshake with the hub has completed and the
        # connection is up: the readiness signal (e.g. an exec readinessProbe).
        self.ready_file = ready_file
        # wss:// only: the CA bundle the hub's certificate is verified against
        # (default: the system trust store).
        self.ca_file = ca_file

        self._lock = threading.RLock()
        self._terminals: Dict[WindowKey, str] = {}
        self._pipes: Dict[WindowKey, _PanePipe] = {}
        self._staged: Dict[str, set] = collections.defaultdict(set)
        self._created_at: Dict[WindowKey, float] = {}
        # Re-adopted windows (recover_terminals) whose tmux pipe, if any, still
        # feeds the previous process's FIFO and must be cleared before re-arming.
        self._adopted: set = set()
        self._window_locks: Dict[WindowKey, threading.Lock] = {}
        self._async_window_locks: Dict[WindowKey, asyncio.Lock] = {}
        # Terminal ids whose create is executing right now. Reported to the hub
        # so a reap marker for one is not cleared before its pane exists.
        # Reference-counted: the call handler and the worker thread both hold
        # an id, so a dropped connection (which ends the handler) cannot clear
        # it while the worker is still creating.
        self._creating: "collections.Counter[str]" = collections.Counter()
        self._seq: Dict[str, int] = {}  # pipe_id -> last seq sent
        # (pipe_id, frame) so a stopped pipe's undelivered frames can be purged.
        self._outbox: Deque[Tuple[str, str]] = collections.deque()
        self._outbox_bytes = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._outbox_ready: Optional[asyncio.Event] = None

    @property
    def backend(self) -> "TerminalBackend":
        if self._backend is None:
            from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend

            self._backend = TmuxBackend()
        return self._backend

    # ------------------------------------------------------------------
    # Connection loop
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, InvalidStatus

        self._loop = asyncio.get_running_loop()
        self._outbox_ready = asyncio.Event()
        self._set_ready(False)  # a file left by a previous process is stale
        self.recover_terminals()
        # Only for wss://: a ws:// connect must not be handed an ssl= argument.
        tls: Dict[str, Any] = {"ssl": ctx} if (ctx := self._ssl_context()) is not None else {}
        backoff = 1.0
        while True:
            try:
                async with connect(
                    self.server_url,
                    additional_headers={TOKEN_HEADER: f"Bearer {self._token}"},
                    max_size=MAX_FRAME_BYTES,
                    ping_interval=PING_INTERVAL,
                    ping_timeout=PING_TIMEOUT,
                    open_timeout=HANDSHAKE_TIMEOUT,
                    **tls,
                ) as ws:
                    try:
                        await self._session(ws)
                    finally:
                        self._set_ready(False)
                    backoff = 1.0
            except InvalidStatus as exc:
                logger.error(f"runner hub refused the connection: {exc}")
            except ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd else None
                if code in (CLOSE_PROTOCOL_MISMATCH, CLOSE_BAD_HELLO):
                    # Retrying cannot fix a contract mismatch; fail loudly.
                    raise SystemExit(f"runner hub rejected this runner: {exc}") from exc
                logger.warning(f"connection to runner hub closed: {exc}")
            except (OSError, asyncio.TimeoutError) as exc:
                logger.warning(f"cannot reach runner hub at {self.server_url}: {exc}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _session(self, ws: Any) -> None:
        await ws.send(json.dumps(self._hello()))
        welcome = json.loads(await asyncio.wait_for(ws.recv(), timeout=HANDSHAKE_TIMEOUT))
        if welcome.get("type") != "welcome" or welcome.get("protocol") != PROTOCOL_VERSION:
            raise SystemExit(f"unexpected reply from runner hub: {welcome!r}")
        logger.info(f"runner {self.runner_id} connected to {self.server_url}")
        self._set_ready(True)
        sender = asyncio.create_task(self._drain_outbox(ws))
        inventory = asyncio.create_task(self._send_inventory(ws))
        calls: set = set()
        try:
            async for raw in ws:
                task = asyncio.create_task(self._serve_call(ws, raw))
                calls.add(task)
                task.add_done_callback(calls.discard)
        finally:
            sender.cancel()
            inventory.cancel()
            # Their replies can no longer be delivered; the server already
            # reports each of them as outcome-unknown.
            for task in calls:
                task.cancel()

    def _ssl_context(self) -> Optional[ssl.SSLContext]:
        """The TLS context for a ``wss://`` hub URL; None for ``ws://``."""
        if not self.server_url.lower().startswith("wss://"):
            if self.ca_file:
                logger.warning("CAO_RUNNER_CA_FILE is ignored for a ws:// hub URL")
            return None
        return ssl.create_default_context(cafile=self.ca_file or None)

    def _set_ready(self, ready: bool) -> None:
        if not self.ready_file:
            return
        try:
            if ready:
                with open(self.ready_file, "w", encoding="utf-8") as fh:
                    fh.write(f"{self.runner_id}\n")
            else:
                os.unlink(self.ready_file)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(f"could not update ready file {self.ready_file}: {exc}")

    def recover_terminals(self) -> int:
        """Re-adopt the CAO terminals already running in this runner's tmux.

        A runner process that restarts while its tmux server survives would
        otherwise report none of them, and every window op on them would be
        refused by the terminal guard. Each window carries the terminal id it
        was created for (``@cao_terminal_id``), so ownership is read back from
        tmux rather than guessed from names. Only windows tagged with THIS
        runner's id are adopted: a tmux server shared with a local CAO must
        never lend its terminals to the hub, which would reap them as unowned.
        """
        from cli_agent_orchestrator.clients.tmux import TERMINAL_ID_OPTION

        if not self._owns_tmux:
            return 0
        try:
            out = subprocess.run(
                [
                    "tmux",
                    "list-windows",
                    "-a",
                    "-F",
                    f"#{{session_name}}\t#{{window_name}}\t#{{{TERMINAL_ID_OPTION}}}"
                    f"\t#{{{RUNNER_ID_OPTION}}}",
                ],
                capture_output=True,
                text=True,
                timeout=_TMUX_LIST_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired):
            return 0
        if out.returncode != 0:
            return 0
        recovered = 0
        for line in out.stdout.splitlines():
            parts = line.split("\t")
            if (
                len(parts) != 4
                or parts[3] != self.runner_id
                or not TERMINAL_ID_PATTERN.match(parts[2])
            ):
                continue
            with self._lock:
                if (parts[0], parts[1]) in self._terminals:
                    continue
            self._record(parts[0], parts[1], parts[2])
            with self._lock:
                self._adopted.add((parts[0], parts[1]))
            recovered += 1
        if recovered:
            logger.info(f"runner {self.runner_id} re-adopted {recovered} running terminal(s)")
        return recovered

    def _tag_owner(self, session: str, window: str, created_session: bool) -> None:
        """Mark a window this runner created, so a restarted runner re-adopts it.

        Fail closed: an untagged window would be refused re-adoption after a
        runner restart and left running unowned, so a create whose tag cannot
        be set is undone and reported as failed.
        """
        if not self._owns_tmux:
            return
        try:
            out = subprocess.run(
                ["tmux", "set-option", "-w", "-t", f"={session}:={window}"]
                + [RUNNER_ID_OPTION, self.runner_id],
                capture_output=True,
                text=True,
                timeout=_TMUX_LIST_TIMEOUT,
            )
            failed = (
                (out.stderr.strip() or f"exit status {out.returncode}") if out.returncode else ""
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            failed = str(exc) or type(exc).__name__
        if not failed:
            return
        logger.error(f"could not tag {session}:{window} as runner-owned: {failed}")
        try:
            if created_session:
                removed = self.backend.kill_session(session)
            else:
                removed = self.backend.kill_window(session, window)
        except Exception as exc:  # noqa: BLE001 - the create is failing either way
            logger.error(f"could not remove untagged {session}:{window}: {exc}")
            removed = False
        if removed:
            self._forget((session, window))
            outcome = "the window was removed"
        else:
            # Still tracked, so it keeps being reported: the server holds no
            # row for it and reaps it as unowned (see the hub's ownership rule).
            outcome = "removing the window was not confirmed; it stays reported for reaping"
        raise TerminalBackendError(
            f"created {session}:{window} but could not tag it as owned by runner "
            f"{self.runner_id} ({failed}); {outcome}"
        )

    def _hello(self) -> Dict[str, Any]:
        with self._lock:
            terminals = [
                {"session": s, "window": w, "terminal_id": t}
                for (s, w), t in self._terminals.items()
            ]
            creating = sorted(self._creating)
            pipes = sorted(p.pipe_id for p in self._pipes.values())
        return {
            "type": "hello",
            "protocol": PROTOCOL_VERSION,
            "runner_id": self.runner_id,
            "claims": self.claims,
            "accepts_unclaimed": self.accepts_unclaimed,
            "max_terminals": self.max_terminals,
            "tmp_dir": self.tmp_dir,
            "terminals": terminals,
            "creating": creating,
            # Output pipes this process runs; the hub re-arms any it lost.
            "pipes": pipes,
        }

    async def _serve_call(self, ws: Any, raw: Any) -> None:
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(frame, dict) or frame.get("type") != "call":
            return
        call_id, op, args = frame.get("id"), frame.get("op"), frame.get("args") or {}
        creating: Optional[str] = None
        try:
            if op not in OPS or not isinstance(args, dict):
                raise ValueError(f"unsupported runner op {op!r}")
            if op in ("create_session", "create_window") and isinstance(
                args.get("terminal_id"), str
            ):
                # In flight from the moment it is received, not from when it
                # gets its window: a reap's exact cleanup arriving meanwhile
                # must answer "unknown", never "absent".
                creating = args["terminal_id"]
                with self._lock:
                    self._creating[creating] += 1
            session, window = args.get("session_name"), args.get("window_name")
            if session and window:
                # Wait for the window here, on the loop, not in a worker
                # thread: calls queued behind one slow window must not occupy
                # the executor that every other window and the inventory share.
                async with self._async_window_lock(str(session), str(window)):
                    value = await asyncio.to_thread(self.dispatch, op, args)
            else:
                value = await asyncio.to_thread(self.dispatch, op, args)
            reply = {"type": "result", "id": call_id, "ok": True, "value": value}
        except Exception as exc:
            reply = {"type": "result", "id": call_id, "ok": False, "error": encode_error(exc)}
        finally:
            if creating is not None:
                with self._lock:
                    self._end_creating(creating)
        try:
            await ws.send(json.dumps(reply))
        except Exception as exc:
            # The server will see this call as outcome-unknown, which is accurate.
            logger.warning(f"could not deliver result of {op}: {exc}")

    async def _drain_outbox(self, ws: Any) -> None:
        assert self._outbox_ready is not None
        while True:
            await self._outbox_ready.wait()
            while True:
                with self._lock:
                    if not self._outbox:
                        self._outbox_ready.clear()
                        break
                    entry = self._outbox[0]
                await ws.send(entry[1])
                with self._lock:
                    # Only now is it delivered to the socket; a send that raised
                    # leaves it queued for the next connection.
                    if self._outbox and self._outbox[0] is entry:
                        self._outbox.popleft()
                        self._outbox_bytes -= len(entry[1])

    async def _send_inventory(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(_INVENTORY_INTERVAL)
            terminals = await asyncio.to_thread(self.inventory)
            with self._lock:
                creating = sorted(self._creating)
            await ws.send(
                json.dumps({"type": "inventory", "terminals": terminals, "creating": creating})
            )

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def _emit(self, pipe_id: str, chunk: bytes) -> None:
        with self._lock:
            seq = self._seq.get(pipe_id, 0) + 1
            self._seq[pipe_id] = seq
            frame = json.dumps(
                {
                    "type": "output",
                    "pipe_id": pipe_id,
                    "seq": seq,
                    "data": base64.b64encode(chunk).decode(),
                }
            )
            self._outbox.append((pipe_id, frame))
            self._outbox_bytes += len(frame)
            while self._outbox_bytes > _OUTBOX_MAX_BYTES and len(self._outbox) > 1:
                self._outbox_bytes -= len(self._outbox.popleft()[1])
        loop, ready = self._loop, self._outbox_ready
        if loop is not None and ready is not None and not loop.is_closed():
            loop.call_soon_threadsafe(ready.set)

    # ------------------------------------------------------------------
    # Local state
    # ------------------------------------------------------------------

    def inventory(self) -> list:
        """Terminals this runner hosts, pruned of windows tmux no longer has."""
        snapshot_at = time.monotonic()
        live = self._live_windows()
        if live is not None:
            with self._lock:
                # Only a terminal that existed before the tmux listing was
                # taken can be judged by it; one created while it ran is not in
                # the listing and is not gone.
                gone = [
                    (k, t)
                    for k, t in self._terminals.items()
                    if k not in live and self._created_at.get(k, 0.0) < snapshot_at
                ]
            for key, terminal_id in gone:
                # Only the incarnation the listing judged; a replacement that
                # took the same name since then is left alone.
                self._forget(key, expected_terminal_id=terminal_id)
        with self._lock:
            return [
                {"session": s, "window": w, "terminal_id": t}
                for (s, w), t in self._terminals.items()
            ]

    @staticmethod
    def _live_windows() -> Optional[set]:
        try:
            out = subprocess.run(
                ["tmux", "list-windows", "-a", "-F", "#{session_name}\t#{window_name}"],
                capture_output=True,
                text=True,
                timeout=_TMUX_LIST_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode != 0:
            # "no server running" is a real, empty answer; anything else is not.
            return set() if "no server running" in (out.stderr or "") else None
        return {tuple(line.split("\t", 1)) for line in out.stdout.splitlines() if "\t" in line}

    def _forget(self, key: WindowKey, expected_terminal_id: Optional[str] = None) -> None:
        with self._lock:
            if (
                expected_terminal_id is not None
                and self._terminals.get(key) != expected_terminal_id
            ):
                return
            terminal_id = self._terminals.pop(key, None)
            self._created_at.pop(key, None)
            pipe = self._pipes.pop(key, None)
        if pipe is not None:
            self._stop_pipe(pipe)
        if terminal_id:
            self._discard_staged(terminal_id)

    def _stop_pipe(self, pipe: "_PanePipe") -> None:
        """Stop a pipe and drop its frames that were never delivered.

        Undelivered output of a stopped pipe belongs to a pane that is gone or
        re-armed; sending it later would only be refused by the server.
        """
        pipe.stop()
        with self._lock:
            kept = collections.deque(e for e in self._outbox if e[0] != pipe.pipe_id)
            self._outbox_bytes = sum(len(e[1]) for e in kept)
            self._outbox = kept
            self._seq.pop(pipe.pipe_id, None)

    def _record(self, session: str, window: str, terminal_id: str) -> None:
        with self._lock:
            self._terminals[(session, window)] = terminal_id
            self._created_at[(session, window)] = time.monotonic()

    @staticmethod
    def _validate_working_directory(path: Any) -> Optional[str]:
        """Apply the server's local cwd rules on the host that owns the path.

        A ValueError travels back as such, so a bad remote path is the same
        client error it is for a local terminal rather than a backend failure.
        """
        if path is None:
            return None
        from cli_agent_orchestrator.utils.path_validation import resolve_and_validate_path

        return resolve_and_validate_path(
            str(path), allow_create=False, allow_file=False, description="Working directory"
        )

    def _discard_staged(self, terminal_id: str) -> None:
        with self._lock:
            names = self._staged.pop(terminal_id, set())
        for name in names:
            try:
                os.unlink(os.path.join(self.tmp_dir, name))
            except OSError:
                pass

    def _stage_files(self, files: Any) -> int:
        if not isinstance(files, list):
            raise ValueError("stage_files expects a list")
        written = 0
        for entry in files:
            name = entry.get("name") if isinstance(entry, dict) else None
            if not isinstance(name, str) or not STAGED_FILE_PATTERN.match(name):
                raise ValueError(f"refusing to stage file {name!r}")
            data = base64.b64decode(entry.get("data") or "", validate=True)
            if len(data) > MAX_STAGED_FILE_BYTES:
                raise ValueError(f"staged file {name} exceeds {MAX_STAGED_FILE_BYTES} bytes")
            path = os.path.join(self.tmp_dir, name)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            with self._lock:
                self._staged[name[:8]].add(name)
            written += 1
        return written

    def _pipe_path(self, terminal_id: Optional[str], key: WindowKey) -> str:
        label = terminal_id or re.sub(r"[^A-Za-z0-9_-]", "_", f"{key[0]}-{key[1]}")[:64]
        return str(FIFO_DIR / f"runner-{label}.fifo")

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def dispatch(self, op: str, args: Dict[str, Any]) -> Any:
        session: str = args.get("session_name") or ""
        window: str = args.get("window_name") or ""
        if not (session and window):
            return self._dispatch_unlocked(op, session, window, args)
        # Calls run concurrently (one worker thread each). Per window, the
        # terminal guard, the operation and its bookkeeping are one step: a
        # stale send that passed the guard must not run after the window was
        # killed and re-created for another terminal.
        with self._window_lock(session, window):
            return self._dispatch_unlocked(op, session, window, args)

    def _async_window_lock(self, session: str, window: str) -> asyncio.Lock:
        # Only touched on the runner's event loop.
        return self._async_window_locks.setdefault((session, window), asyncio.Lock())

    def _end_creating(self, terminal_id: str) -> None:
        # Caller holds self._lock.
        self._creating[terminal_id] -= 1
        if self._creating[terminal_id] <= 0:
            del self._creating[terminal_id]

    def _window_lock(self, session: str, window: str) -> threading.Lock:
        with self._lock:
            return self._window_locks.setdefault((session, window), threading.Lock())

    def _dispatch_unlocked(self, op: str, session: str, window: str, args: Dict[str, Any]) -> Any:
        if op in ("create_session", "create_window"):
            terminal_id = args["terminal_id"]
            with self._lock:
                self._creating[terminal_id] += 1
            try:
                return self._create(op, session, window, args)
            finally:
                with self._lock:
                    self._end_creating(terminal_id)
        return self._dispatch_existing(op, session, window, args)

    def _create(self, op: str, session: str, window: str, args: Dict[str, Any]) -> str:
        backend = self.backend
        if op == "create_session" or (
            args.get("create_session_if_missing") and not backend.session_exists(session)
        ):
            name = backend.create_session(
                session,
                window,
                args["terminal_id"],
                self._validate_working_directory(args.get("working_directory")),
                extra_env=args.get("extra_env") or None,
            )
            self._record(session, name, args["terminal_id"])
            self._tag_owner(session, name, created_session=True)
            return name
        name = backend.create_window(
            session,
            window,
            args["terminal_id"],
            self._validate_working_directory(args.get("working_directory")),
            args.get("window_shell"),
            extra_env=args.get("extra_env") or None,
        )
        self._record(session, name, args["terminal_id"])
        self._tag_owner(session, name, created_session=False)
        return name

    def _guard_window(self, session: str, window: str, args: Dict[str, Any]) -> None:
        """Refuse a window op aimed at a terminal that no longer owns the window.

        The server sends the terminal id it resolved the route for. A late or
        retried call must not land on a newer terminal that reused the name.
        """
        expected = args.get("expected_terminal_id")
        if expected is None:
            return
        with self._lock:
            current = self._terminals.get((session, window))
        if current != expected:
            # The backend contract's "window not found" (a ValueError, as tmux
            # raises it): for terminal ``expected`` that is exactly the case.
            raise ValueError(
                f"Window '{window}' not found in session '{session}' for terminal "
                f"{expected} on this runner (now: {current or 'none'})"
            )

    def _dispatch_existing(  # noqa: C901 - flat op table
        self, op: str, session: str, window: str, args: Dict[str, Any]
    ) -> Any:
        backend = self.backend
        self._guard_window(session, window, args)
        if op == "session_exists":
            return backend.session_exists(session)
        if op == "session_exists_strict":
            return backend.session_exists_strict(session)
        if op == "list_sessions":
            return backend.list_sessions()
        if op == "kill_session":
            killed = backend.kill_session(session)
            if killed:
                with self._lock:
                    keys = [k for k in self._terminals if k[0] == session]
                for key in keys:
                    self._forget(key)
            return killed
        if op == "kill_window":
            killed = backend.kill_window(session, window)
            if killed:
                self._forget(
                    (session, window), expected_terminal_id=args.get("expected_terminal_id")
                )
            return killed
        if op == "cleanup_terminal_exact":
            with self._lock:
                in_flight = args["terminal_id"] in self._creating
            if in_flight:
                # Its pane may not exist YET; "absent" now would let the create
                # finish unowned. The caller retries when it is reported.
                return {"outcome": "unknown", "detail": "create still in progress"}
            result = backend.cleanup_terminal_exact(
                args["terminal_id"], session, window, close=bool(args.get("close", True))
            )
            if result.reclaimed:
                with self._lock:
                    keys = [k for k, t in self._terminals.items() if t == args["terminal_id"]]
                for key in keys:
                    self._forget(key)
            return {"outcome": result.outcome.value, "detail": result.detail}
        if op == "send_keys":
            backend.send_keys(
                session,
                window,
                args["keys"],
                enter_count=int(args.get("enter_count", 1)),
                force_bracketed_paste=bool(args.get("force_bracketed_paste", False)),
                submit_delay=float(args.get("submit_delay", 0.3)),
            )
            return None
        if op == "send_special_key":
            backend.send_special_key(session, window, args["key"])
            return None
        if op == "get_history":
            return backend.get_history(
                session,
                window,
                tail_lines=args.get("tail_lines"),
                strip_escapes=bool(args.get("strip_escapes")),
                full_history=bool(args.get("full_history")),
                visible_only=bool(args.get("visible_only")),
                join_wrapped=bool(args.get("join_wrapped")),
            )
        if op == "get_pane_working_directory":
            return backend.get_pane_working_directory(session, window)
        if op == "get_pane_current_command":
            return backend.get_pane_current_command(session, window)
        if op == "pipe_pane":
            key = (session, window)
            pipe_id = args.get("pipe_id")
            if not isinstance(pipe_id, str) or not pipe_id:
                raise ValueError("pipe_pane requires a pipe_id")
            with self._lock:
                previous = self._pipes.pop(key, None)
                terminal_id = self._terminals.get(key)
                adopted = key in self._adopted
                self._adopted.discard(key)
            if previous is not None:
                self._stop_pipe(previous)
            if adopted:
                # tmux's pipe-pane -o is a no-op while the old writer lives on.
                backend.stop_pipe_pane(session, window)
            pipe = _PanePipe(
                self._pipe_path(terminal_id, key), pipe_id, lambda c: self._emit(pipe_id, c)
            )
            try:
                backend.pipe_pane(session, window, pipe.fifo_path)
            except BaseException:
                self._stop_pipe(pipe)
                raise
            with self._lock:
                self._pipes[key] = pipe
            return None
        if op == "stop_pipe_pane":
            key = (session, window)
            try:
                backend.stop_pipe_pane(session, window)
            finally:
                with self._lock:
                    stopped = self._pipes.pop(key, None)
                if stopped is not None:
                    self._stop_pipe(stopped)
            return None
        if op == "stage_files":
            return self._stage_files(args.get("files"))
        raise ValueError(f"unsupported runner op {op!r}")


def _default_runner_id() -> str:
    candidate = re.sub(r"[^a-z0-9-]", "-", socket.gethostname().lower()).strip("-")
    return candidate[:63] or "runner"


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off", "")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cao-runner",
        description="Execution-only runner for a central cao-server (terminal.backend=remote).",
    )
    parser.add_argument(
        "--server-url",
        default=os.environ.get("CAO_RUNNER_SERVER_URL"),
        help="Runner hub URL, e.g. ws://cao-server:9891 (env CAO_RUNNER_SERVER_URL)",
    )
    parser.add_argument(
        "--runner-id",
        default=os.environ.get("CAO_RUNNER_ID") or _default_runner_id(),
        help="Stable id for this runner (env CAO_RUNNER_ID; default: hostname)",
    )
    parser.add_argument(
        "--claim",
        action="append",
        default=[c for c in os.environ.get("CAO_RUNNER_CLAIMS", "").split(",") if c],
        help="Session name only this runner may host; repeatable (env CAO_RUNNER_CLAIMS)",
    )
    parser.add_argument(
        "--max-terminals",
        type=int,
        default=int(os.environ.get("CAO_RUNNER_MAX_TERMINALS", "1")),
        help="Terminals this runner hosts at most (env CAO_RUNNER_MAX_TERMINALS)",
    )
    parser.add_argument(
        "--no-unclaimed",
        action="store_true",
        default=not _env_bool("CAO_RUNNER_ACCEPTS_UNCLAIMED", True),
        help="Host only claimed sessions (env CAO_RUNNER_ACCEPTS_UNCLAIMED=0)",
    )
    parser.add_argument(
        "--ready-file",
        default=os.environ.get("CAO_RUNNER_READY_FILE", str(CAO_HOME_DIR / "runner-ready")),
        help="File present only while connected to the hub; '' disables it "
        "(env CAO_RUNNER_READY_FILE)",
    )
    parser.add_argument(
        "--ca-file",
        default=os.environ.get("CAO_RUNNER_CA_FILE"),
        help="CA bundle verifying a wss:// hub (env CAO_RUNNER_CA_FILE; default: system store)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("CAO_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Removed from the environment once read: TmuxClient copies CAO_* variables
    # from this process into every pane it creates, and an agent holding the
    # runner token could register itself as a runner.
    token = runner_token()
    if not args.server_url or not token:
        parser.error(
            "--server-url and a runner token (CAO_RUNNER_TOKEN_FILE or CAO_RUNNER_TOKEN) "
            "are required"
        )
    if args.max_terminals < 1:
        parser.error("--max-terminals must be at least 1")

    runner = Runner(
        server_url=args.server_url,
        token=token,
        runner_id=args.runner_id,
        claims=args.claim,
        accepts_unclaimed=not args.no_unclaimed,
        max_terminals=args.max_terminals,
        ready_file=args.ready_file or None,
        ca_file=args.ca_file or None,
    )
    try:
        asyncio.run(runner.run_forever())
    except KeyboardInterrupt:
        return 0
    finally:
        runner._set_ready(False)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
